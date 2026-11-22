"""Talk to a T3 Code server that runs inside a container.

Copied from driver_proof/t3client.py and kept as it was proven there.

The server listens on 127.0.0.1 inside the container only. Every request runs
in a one-shot `node -` whose program arrives on stdin, with the request data
embedded in it. Nothing is written to the container's filesystem and the
process's argv is only `node -`.

Two ways in, both authenticated with an owner session that `t3 auth session
issue` prints (the one thing done through the CLI, because every other route
needs a credential first):

* HTTP reads: `/api/orchestration/...` and `/api/projects` with
  `authorization: Bearer <owner>` and `x-t3-orchestration-protocol: 2`.
* WebSocket RPC, the route T3's own app uses: `POST
  /api/auth/websocket-ticket/rpc` with the owner bearer returns a short-lived
  ticket; `ws://127.0.0.1:3773/ws?orchestrationProtocol=2&wsTicket=<ticket>`
  opens an Effect RPC socket with JSON serialization. Client frames are
  `{"_tag":"Request","id":"<n>","tag":"<method>","payload":{...},"headers":[]}`;
  the server answers `{"_tag":"Exit","requestId":"<n>","exit":{"_tag":"Success","value":...}}`
  or `{"_tag":"Failure","cause":[...]}`. Stream results arrive as `Chunk`
  frames that the client acknowledges with `{"_tag":"Ack","requestId":"<n>"}`.

The server stamps every `orchestration.dispatchCommand` and
`orchestration.launchThread` from this socket `createdBy: "user"`, so T3 adds
no sender note to the message text (`withMessageSenderNote` in
packages/contracts/src/orchestrationV2.ts adds one only for agent-created
messages from MCP).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cohbench.env.docker import Container, DeadlinePassed, DockerError, DockerTimeout, bounded, deadline_passed

T3_PORT = 3773

ORIGIN = f"http://127.0.0.1:{T3_PORT}"
PROTOCOL_HEADER = {"x-t3-orchestration-protocol": "2"}

_HTTP_JS = r"""
(async()=>{
  const ac=new AbortController();
  const t=setTimeout(()=>ac.abort(),REQ.timeoutMs);
  try{
    const res=await fetch(REQ.url,{method:REQ.method,headers:REQ.headers,body:REQ.body,redirect:'manual',signal:ac.signal});
    const body=await res.text();
    process.stdout.write(JSON.stringify({status:res.status,headers:Object.fromEntries(res.headers),body}));
  }catch(e){process.stdout.write(JSON.stringify({error:String(e)}));}
  clearTimeout(t);
})();
"""

# One socket, the calls in order. Prints one JSON line per event so the caller
# can tell a call that was never sent from one whose answer was lost.
_RPC_JS = r"""
const out=(o)=>process.stdout.write(JSON.stringify(o)+"\n");
(async()=>{
  let ticket;
  try{
    const r=await fetch(REQ.origin+"/api/auth/websocket-ticket/rpc",{method:"POST",headers:{authorization:"Bearer "+REQ.token}});
    const b=await r.text();
    if(r.status!==200){out({fatal:"ticket",status:r.status,body:b.slice(0,300)});process.exit(3);}
    ticket=JSON.parse(b).ticket;
  }catch(e){out({fatal:"ticket",error:String(e)});process.exit(3);}
  const ws=new WebSocket(REQ.origin.replace(/^http/,"ws")+"/ws?orchestrationProtocol=2&wsTicket="+encodeURIComponent(ticket));
  const timer=setTimeout(()=>{out({fatal:"timeout"});process.exit(4);},REQ.timeoutMs);
  let i=0,current=null;const chunks=[];
  const next=()=>{
    if(i>=REQ.calls.length){clearTimeout(timer);ws.close();setTimeout(()=>process.exit(0),50);return;}
    const c=REQ.calls[i];current=String(i+1);
    ws.send(JSON.stringify({_tag:"Request",id:current,tag:c.tag,payload:c.payload,headers:[]}));
    out({sent:i});
  };
  ws.onopen=()=>{out({open:true});next();};
  ws.onmessage=(e)=>{
    let m;try{m=JSON.parse(String(e.data));}catch(err){out({unparsed:String(e.data).slice(0,200)});return;}
    if(m._tag==="Chunk"){ws.send(JSON.stringify({_tag:"Ack",requestId:m.requestId}));chunks.push(...m.values);return;}
    if(m._tag==="Exit"&&m.requestId===current){out({done:i,exit:m.exit,chunks:chunks.splice(0)});i++;next();return;}
    if(m._tag==="Pong")return;
    if(m._tag==="Defect"||m._tag==="ClientProtocolError"){out({fatal:"protocol",frame:m});process.exit(5);}
  };
  ws.onerror=(e)=>{out({socket_error:String((e&&e.message)||"error")});};
  ws.onclose=(e)=>{if(i<REQ.calls.length){out({fatal:"closed",code:e.code,reason:e.reason});process.exit(6);}};
})();
"""


class T3Error(RuntimeError):
    pass


class RpcFailure(T3Error):
    """The server answered the call with a failure exit."""

    def __init__(self, tag: str, cause: Any):
        super().__init__(f"{tag} failed: {json.dumps(cause)[:1500]}")
        self.tag = tag
        self.cause = cause


class RpcLost(T3Error):
    """The call may have reached the server, but no answer came back."""


class T3Timeout(T3Error):
    """A read or call did not finish within its limit or the caller's deadline."""


class T3NotStarted(T3Timeout):
    """Too little time was left to start the call at all: no process was
    started in the container, so nothing can have reached T3."""


class RpcUnsent(T3Error):
    """The call certainly did not reach T3: no attempt was started, or the
    socket never carried it. Nothing changed."""


# What one `docker exec node -` costs beyond the program's own timer: the
# exec round trip, node's start and its exit. The program's timer gets the
# limit less this, the exec gets the whole limit, so a call never outlasts it.
NODE_MARGIN_S = 1.5


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> Any:
        return json.loads(self.body)


def run_node(container: Container, program: str, req: dict[str, Any], *, timeout_s: float) -> str:
    """Run a node program fed on stdin, with REQ embedded. Returns stdout.
    The whole call, exec included, ends within `timeout_s` capped by the
    current deadline; the program's own timer (REQ.timeoutMs) is that limit
    less NODE_MARGIN_S. Raises T3Timeout when there is not that much time."""
    try:
        limit = bounded(timeout_s)
    except DockerTimeout as exc:
        raise T3NotStarted(str(exc)) from None
    inner = limit - NODE_MARGIN_S
    if inner <= 0.1:
        raise T3NotStarted(f"{limit:.2f} s left, too little for a call")
    source = "const REQ = " + json.dumps({**req, "timeoutMs": int(inner * 1000)}) + ";\n" + program
    try:
        proc = container.exec(["node", "-"], input_text=source, check=False, timeout=limit)
    except DeadlinePassed as exc:
        # Checked before docker exec was spawned: nothing was started.
        raise T3NotStarted(str(exc)) from None
    except DockerTimeout as exc:
        raise T3Timeout(str(exc)) from None
    if proc.returncode not in (0, 3, 4, 5, 6) or (proc.returncode != 0 and not proc.stdout.strip()):
        raise T3Error(f"node exited {proc.returncode}: {proc.stderr.strip()[-800:]}")
    return proc.stdout


def http(
    container: Container,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_s: float = 120,
) -> HttpResponse:
    req = {"method": method, "url": ORIGIN + path, "headers": headers or {}, "body": body}
    out = json.loads(run_node(container, _HTTP_JS, req, timeout_s=timeout_s))
    if "error" in out:
        raise T3Error(f"{method} {path}: {out['error']}")
    return HttpResponse(out["status"], out["headers"], out["body"])


def wait_ready(container: Container, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            res = http(container, "GET", "/.well-known/t3/environment", timeout_s=max(0.1, min(5.0, deadline - time.monotonic())))
            if res.status == 200:
                return
            last = f"status {res.status}"
        except (T3Error, DockerError) as exc:
            if deadline_passed():
                raise
            last = str(exc)
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
    try:
        logs = container.logs()[-3000:] if not deadline_passed() and time.monotonic() < deadline else "(not read: no time left)"
    except DockerError:
        logs = "(not read)"
    raise T3Error(f"T3 server did not become ready: {last}\n{logs}")


def owner_token(container: Container, label: str = "workspace") -> str:
    """CLI only: `t3 auth session issue` mints the owner session every other route needs."""
    proc = container.exec(["t3", "auth", "session", "issue", "--token-only", "--ttl", "1d", "--label", label], check=False)
    if proc.returncode != 0:
        raise T3Error(f"t3 auth session issue exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise T3Error("t3 auth session issue printed no token")
    return lines[-1]


def new_id() -> str:
    return str(uuid.uuid4())


class Api:
    """HTTP reads and WebSocket RPC calls with one owner session."""

    def __init__(self, container: Container, token: str):
        self.container = container
        self.token = token
        self.calls: list[dict[str, Any]] = []

    # ----- HTTP --------------------------------------------------------------

    def get(self, path: str, *, timeout_s: float = 120, ok: tuple[int, ...] = (200,)) -> Any:
        res = http(
            self.container,
            "GET",
            path,
            headers={"authorization": f"Bearer {self.token}", **PROTOCOL_HEADER},
            timeout_s=timeout_s,
        )
        if res.status not in ok:
            raise T3Error(f"GET {path}: HTTP {res.status} {res.body[:300]}")
        return res.json() if res.body else None

    def get_raw(self, path: str, *, timeout_s: float = 120) -> HttpResponse:
        return http(
            self.container,
            "GET",
            path,
            headers={"authorization": f"Bearer {self.token}", **PROTOCOL_HEADER},
            timeout_s=timeout_s,
        )

    # ----- WebSocket RPC -----------------------------------------------------

    def rpc_batch(self, calls: list[dict[str, Any]], *, timeout_s: float = 90) -> list[dict[str, Any]]:
        """Send calls in order over one socket. Returns one record per call:
        {"state": "answered", "exit": ...} or {"state": "lost"|"unsent"}."""
        started = time.time()
        try:
            stdout = run_node(self.container, _RPC_JS, {"origin": ORIGIN, "token": self.token, "calls": calls}, timeout_s=timeout_s)
        except T3NotStarted:
            # Nothing was started in the container: every call is unsent.
            stdout = ""
            unknown = None
        except T3Error as exc:
            # The exec ended without its output (timed out or failed): whether
            # any call reached the server is unknown.
            stdout = ""
            unknown = f"{type(exc).__name__}: {exc}"[:300]
        else:
            unknown = None
        records: list[dict[str, Any]] = [{"state": "lost" if unknown else "unsent", "unknown": unknown} if unknown else {"state": "unsent"} for _ in calls]
        fatal = None
        for line in stdout.splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            if "sent" in msg:
                records[msg["sent"]] = {"state": "lost"}
            elif "done" in msg:
                records[msg["done"]] = {"state": "answered", "exit": msg["exit"], "chunks": msg.get("chunks", [])}
            elif "fatal" in msg:
                fatal = msg
        for call, record in zip(calls, records):
            self.calls.append(
                {
                    "tag": call["tag"],
                    "type": call["payload"].get("type") if isinstance(call["payload"], dict) else None,
                    "commandId": call["payload"].get("commandId") if isinstance(call["payload"], dict) else None,
                    "state": record["state"],
                    "exit": (record.get("exit") or {}).get("_tag"),
                    "at": round(started, 3),
                    "seconds": round(time.time() - started, 3),
                    "fatal": fatal,
                }
            )
        if fatal and fatal.get("fatal") == "ticket" and all(r["state"] == "unsent" for r in records):
            # The socket was never opened: no call reached T3.
            raise RpcUnsent(f"websocket ticket request failed, nothing sent: {json.dumps(fatal)[:400]}")
        if fatal and fatal.get("fatal") == "ticket":
            raise T3Error(f"websocket ticket request failed: {json.dumps(fatal)[:400]}")
        return records

    def rpc(self, tag: str, payload: dict[str, Any], *, timeout_s: float = 90, resend_if_lost: bool = False) -> Any:
        """One call. With resend_if_lost, a call whose answer was lost is sent
        again with the same payload while time is left; for dispatchCommand
        the server replays an accepted commandId from its receipt instead of
        running it twice. All attempts share `timeout_s`."""
        attempts = 2 if resend_if_lost else 1
        end = time.monotonic() + timeout_s
        lost = False
        for attempt in range(attempts):
            left = end - time.monotonic()
            if attempt > 0 and left <= NODE_MARGIN_S + 0.1:
                break
            try:
                record = self.rpc_batch([{"tag": tag, "payload": payload}], timeout_s=left)[0]
            except RpcUnsent:
                # This attempt provably sent nothing; an earlier lost one still
                # makes the outcome unknown.
                record = {"state": "unsent"}
            if record["state"] == "answered":
                exit_ = record["exit"]
                if exit_.get("_tag") == "Success":
                    return exit_.get("value")
                raise RpcFailure(tag, exit_.get("cause"))
            lost = lost or record["state"] == "lost"
        if lost:
            raise RpcLost(f"{tag}: sent, but no answer arrived")
        raise RpcUnsent(f"{tag}: the call was never started or never carried")

    def dispatch(self, command: dict[str, Any], *, timeout_s: float = 90) -> dict[str, Any]:
        return self.rpc("orchestration.dispatchCommand", command, timeout_s=timeout_s, resend_if_lost=True)


def cli(container: Container, args: list[str], *, input_text: str | None = None, timeout: float = 900):
    """Run `t3 <args>` inside the container and return the CompletedProcess."""
    return container.exec(["t3", *args], input_text=input_text, check=False, timeout=timeout)
