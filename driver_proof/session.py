"""One container with T3 and one harness, plus the calls the operations need.

Every scripted interaction goes through T3's WebSocket RPC (t3client.Api):
launching a thread, sending, steering, the fresh-context send, interrupting,
answering a pending user_input request and answering a secret request. Reads
use the HTTP API. The CLI issues the owner session and nothing else.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import container as dk
from . import gatewaylog, native, t3client
from .evidence import Blocked, Recorder
from .harness import Harness

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "rolled_back"}



def q(thread_id: str) -> str:
    return urllib.parse.quote(thread_id, safe="")


@dataclass
class Sent:
    """What the script sent, kept so checks can compare it byte for byte."""

    kind: str
    thread_id: str
    message_id: str
    command_id: str
    text: str
    mode: str = "queue"
    fresh: bool = False
    run_id: str | None = None
    # Characterisation messages: their checks never decide.
    informational: bool = False
    result: Any = None
    sent_at: float = field(default_factory=time.time)

    def record(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("kind", "thread_id", "message_id", "command_id", "mode", "fresh", "run_id", "sent_at")} | {"chars": len(self.text)}


class Workspace:
    """One harness container with its own gateway topology."""

    def __init__(self, harness: Harness, image: str, key: dk.KeySource, rec: Recorder, label: str, gateway_image: str):
        self.harness = harness
        self.image = image
        self.key = key
        self.rec = rec
        self.label = label
        self.gateway_image = gateway_image
        self.c: dk.Container | None = None
        self.topo: dk.Topology | None = None
        self.api: t3client.Api | None = None
        self.project_id: str | None = None
        self.net = NetSampler()
        self.sent: list[Sent] = []
        # How many of `sent` an earlier container phase already checked.
        self.checked_sent = 0
        self.cleanup: list[dict[str, Any]] = []
        self.verify: dict[str, Any] | None = None
        # Docker resources of this container's topology, registered before creation.
        self.registered: list[tuple[str, str]] = []
        self.restarted: list[tuple[subprocess.Popen, list[str]]] = []
        self.snaps: dict[str, dict[str, Any]] = {}

    @property
    def gateway_dir(self) -> Path:
        return self.rec.raw_dir / "gateway" / self.label

    # ----- lifecycle -------------------------------------------------------

    def start(self, *, from_image: str | None = None, add_project: bool = True) -> None:
        self.topo = dk.start_topology(self.label, self.gateway_image, self.key, self.gateway_dir / "log", self.registered)
        self.rec.facts.setdefault("docker_names", {})[self.label] = dict(self.topo.names)
        self.rec.event("gateway started", label=self.label, gateway=self.topo.gateway.name, subnet=self.topo.subnet, gateway_ip=self.topo.gateway_ip)
        self.boot(from_image or self.image, add_project=add_project)

    def boot(self, image: str, *, add_project: bool, workspace_tar: bytes | None = None, phase: str = "") -> None:
        """Start a harness container on this workspace's topology and run the
        per-container hygiene checks. Used at start and for a reset handoff."""
        assert self.topo is not None
        tag = self.label + (f" {phase}" if phase else "")
        t0 = time.time()
        self.c = dk.start_harness(image, self.topo, label=self.label, cleanup=self.registered, workspace_tar=workspace_tar)
        t_started = time.time()
        self.rec.facts.setdefault("docker_names", {}).setdefault(self.label, {})[f"harness_container{(' ' + phase) if phase else ''}"] = self.c.name
        self.rec.event("container started", container=self.c.name, image=image, phase=phase)
        self.net = NetSampler()
        self.net.start(self.c)
        t3client.wait_ready(self.c)
        t_ready = time.time()
        self.rec.event("server ready", container=self.c.name)
        self.net.pause()
        self.verify = dk.verify_topology(self.topo, self.c)
        self.rec.save_text(f"gateway-verify-{self.label}{('-' + phase) if phase else ''}.txt", self.verify["output"] + "\n" + self.verify["stderr"])
        self.rec.check(f"gateway topology verified ({tag}): {self.verify['summary']}", self.verify["exit"] == 0 and not self.verify["failures"], {k: v for k, v in self.verify.items() if k != "output"}, hygiene=True)
        bridge = dk.bridge_unreachable(self.c, self.topo.bridge_ip)
        self.rec.check(f"network gateway address {self.topo.bridge_ip} does not answer at all from the harness container ({tag})", bridge["ok"], bridge, hygiene=True)
        ident = dk.identity_checks(self.c, self.topo)
        self.rec.check(f"reverse DNS of the container's own address and the gateway's names nothing telling ({tag})", ident["ok_dns"], {k: ident.get(k) for k in ("reverse", "error")}, hygiene=True)
        self.rec.check(f"/proc/self/mountinfo shows no host path ({tag})", ident["ok_mounts"], {k: ident.get(k) for k in ("bad_mount_lines", "mount_sources", "error")}, hygiene=True)
        time.sleep(1)  # let the probes' sockets leave the table before sampling resumes
        t_checked = time.time()
        self.net.resume()
        self.connect(add_project=add_project)
        self.rec.facts.setdefault("boot_timings", {})[tag] = {
            "seconds_to_container_started": round(t_started - t0, 2),
            "seconds_to_t3_ready": round(t_ready - t0, 2),
            "seconds_for_hygiene_checks": round(t_checked - t_ready, 2),
            "seconds_to_project_ready": round(time.time() - t0, 2),
        }

    def retire_container(self, phase: str) -> None:
        """Stop sampling, keep the server log, remove this harness container and
        confirm it is gone; the topology stays for the next container."""
        assert self.c is not None
        self.net.stop()
        self.rec.facts.setdefault("network_samples", {})[f"{self.label} {phase}"] = {
            "gateway_ip": self.topo.gateway_ip if self.topo else None,
            "remotes": dict(self.net.remotes),
            "states": {k: sorted(v) for k, v in self.net.states.items()},
            "only_gateway": all(r.startswith(f"{self.topo.gateway_ip}:") for r in self.net.remotes) if self.topo else None,
        }
        self.rec.save_text(f"server-log-{self.label}-{phase}.txt", self.c.logs())
        result = dk.remove_container(self.c.name)
        self.cleanup.append(result)
        self.rec.check(f"cleanup: container {self.label} ({phase}) removed and confirmed absent", result["confirmed_absent"], result, hygiene=True)
        if not result["confirmed_absent"]:
            raise dk.DockerError(f"container {self.c.name} was not removed")
        self.c = None

    def connect(self, *, add_project: bool) -> None:
        assert self.c is not None
        self.api = t3client.Api(self.c, t3client.owner_token(self.c))
        if add_project:
            for path, body in self.harness.default_model_files().items():
                self.c.exec(["sh", "-c", 'mkdir -p "$(dirname "$0")" && cat > "$0"', path], input_text=body)
                self.rec.event("harness default model pinned (driver-proof spend cap)", path=path)
            self.api.rpc(
                "projects.mutate",
                {"type": "project.create", "commandId": t3client.new_id(), "projectId": t3client.new_id(), "title": "project", "workspaceRoot": dk.WORKSPACE},
            )
        projects = self.api.get("/api/projects")["projects"]
        self.project_id = projects[0]["id"] if projects else None

    def restart_server(self) -> None:
        """Start the entrypoint again after `t3 serve` was stopped. An attached
        `docker exec` carries its output to the host, so the restarted server's
        log reaches the evidence without a file in the container."""
        assert self.c is not None
        proc = subprocess.Popen(["docker", "exec", self.c.name, "/usr/local/bin/workspace-entrypoint"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines: list[str] = []

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)

        threading.Thread(target=pump, daemon=True).start()
        self.restarted.append((proc, lines))

    def finish(self) -> None:
        """Stop sampling, keep the server logs, remove the container and the
        topology. Every removal is confirmed; a handle is cleared only once
        Docker confirms the object is gone. The registry (the harness container
        included) is removed from a finally, so an error or timeout in the first
        removal loop cannot skip it."""
        try:
            if self.c is not None:
                self.net.stop()
                self.rec.facts.setdefault("network_samples", {})[self.label] = {
                    "gateway_ip": self.topo.gateway_ip if self.topo else None,
                    "remotes": dict(self.net.remotes),
                    "states": {k: sorted(v) for k, v in self.net.states.items()},
                    "sampling_paused_during": "gateway verify, the bridge check and the identity check, which probe addresses on purpose",
                    "only_gateway": all(r.startswith(f"{self.topo.gateway_ip}:") for r in self.net.remotes) if self.topo else None,
                }
                try:
                    self.rec.save_text(f"server-log-{self.label}.txt", self.c.logs())
                except Exception as exc:  # the log is evidence, but its loss must not stop removal
                    self.rec.event("server log not saved", container=self.c.name, error=str(exc))
                result: dict[str, Any] = {"container": self.c.name, "confirmed_absent": False}
                for attempt in range(2):
                    try:
                        result = dk.remove_container(self.c.name)
                    except Exception as exc:  # recorded; the registry below still runs
                        result = {"container": self.c.name, "confirmed_absent": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                    result["attempt"] = attempt + 1
                    self.cleanup.append(result)
                    if result["confirmed_absent"]:
                        break
                    time.sleep(2)
                self.rec.check(f"cleanup: container {self.label} removed and confirmed absent", result["confirmed_absent"], result, hygiene=True)
                if result["confirmed_absent"]:
                    self.c = None
                for n, (proc, lines) in enumerate(self.restarted):
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    self.rec.save_text(f"server-log-{self.label}-restart-{n + 1}.txt", "".join(lines))
                self.restarted = []
        finally:
            if self.registered:
                results = dk.remove_registered(self.registered)
                self.cleanup += results
                ok = all(r["confirmed_absent"] for r in results)
                self.rec.check(f"cleanup: every registered container and network of {self.label} (gateway included) removed and confirmed absent", ok, results, hygiene=True)
                if ok:
                    self.registered = []
                    self.topo = None
                    self.c = None

    def gateway_records(self) -> list[dict[str, Any]]:
        return gatewaylog.load(self.gateway_dir / "log")

    # ----- driving threads (WebSocket RPC) ---------------------------------

    def launch(self, text: str, title: str, *, informational: bool = False) -> Sent:
        assert self.api is not None
        cmd, mid = t3client.new_id(), t3client.new_id()
        result = self.api.rpc(
            "orchestration.launchThread",
            {
                "commandId": cmd,
                "creationSource": "web",
                "projectId": self.project_id,
                "title": title,
                "modelSelection": self.harness.model_selection(),
                "runtimeMode": "full-access",
                "interactionMode": "default",
                "workspaceStrategy": {"type": "root"},
                "initialMessage": {"messageId": mid, "text": text, "attachments": []},
            },
            resend_if_lost=True,
        )
        tid = result["threadId"]
        sent = Sent("first", tid, mid, cmd, text, informational=informational)
        sent.run_id = self._run_for_message(tid, mid, result.get("projection"))
        sent.result = {"resumed": result.get("resumed"), "sequence": result.get("sequence")}
        self.sent.append(sent)
        self.rec.event("launchThread", thread_id=tid, run_id=sent.run_id, message_id=mid)
        return sent

    def send(self, tid: str, text: str, *, kind: str, mode: str = "queue", fresh: bool = False, target_run: str | None = None, informational: bool = False) -> Sent:
        """mode: queue (queue_after_active), steer (steer_active) or restart (restart_active)."""
        assert self.api is not None
        cmd, mid = t3client.new_id(), t3client.new_id()
        if mode == "queue":
            dispatch_mode: dict[str, Any] = {"type": "queue_after_active"}
        elif mode == "steer":
            dispatch_mode = {"type": "steer_active", "targetRunId": target_run}
        elif mode == "restart":
            dispatch_mode = {"type": "restart_active", "targetRunId": target_run}
        else:
            raise ValueError(mode)
        command: dict[str, Any] = {
            "type": "message.dispatch",
            "commandId": cmd,
            "createdBy": "user",
            "creationSource": "web",
            "threadId": tid,
            "messageId": mid,
            "text": text,
            "attachments": [],
            "dispatchMode": dispatch_mode,
        }
        if fresh:
            command["freshContext"] = True
        sent = Sent(kind, tid, mid, cmd, text, mode=mode, fresh=fresh, informational=informational)
        self.sent.append(sent)
        sent.result = self.api.dispatch(command)
        sent.run_id = self._run_for_message(tid, mid)
        self.rec.event("dispatch message", kind=kind, mode=mode, fresh=fresh, thread_id=tid, run_id=sent.run_id, message_id=mid, command_id=cmd)
        return sent

    def interrupt(self, tid: str, run_id: str) -> Any:
        assert self.api is not None
        cmd = t3client.new_id()
        out = self.api.dispatch({"type": "run.interrupt", "commandId": cmd, "threadId": tid, "runId": run_id, "holdQueue": True})
        self.rec.event("run.interrupt", thread_id=tid, run_id=run_id, command_id=cmd)
        return out

    def respond_user_input(self, tid: str, request_id: str, answers: dict[str, Any]) -> Any:
        assert self.api is not None
        cmd = t3client.new_id()
        out = self.api.dispatch({"type": "runtime-request.respond", "commandId": cmd, "threadId": tid, "requestId": request_id, "answers": answers})
        self.rec.event("runtime-request.respond", thread_id=tid, request_id=request_id[-40:], command_id=cmd)
        return out

    def answer_secret(self, tid: str, item: dict[str, Any], value: str) -> Any:
        """secrets.answerRequest. A request with `fields` needs one value per field name."""
        assert self.api is not None
        fields = item.get("fields") or []
        if fields:
            answer: dict[str, Any] = {"type": "save", "values": {f["name"]: value for f in fields}}
        else:
            answer = {"type": "save", "secret": value}
        payload = {"threadId": tid, "turnItemId": item["id"], "answer": answer}
        try:
            out = self.api.rpc("secrets.answerRequest", payload)
        except t3client.RpcLost:
            # Not idempotent by id: look before answering again.
            snap = self.snapshot(tid)
            current = next((i for i in snap["projection"]["turnItems"] if i["id"] == item["id"]), {})
            if current.get("secretStatus") == "pending":
                out = self.api.rpc("secrets.answerRequest", payload)
            else:
                out = {"reconciled": current.get("secretStatus")}
        self.rec.event("secrets.answerRequest", thread_id=tid, item=item["id"][-40:], fields=[f.get("name") for f in fields])
        return out

    def turn_item(self, tid: str, item_id: str) -> dict[str, Any] | None:
        assert self.api is not None
        return self.api.rpc("orchestration.getTurnItem", {"threadId": tid, "itemId": item_id})["item"]

    def _run_for_message(self, tid: str, message_id: str, projection: dict[str, Any] | None = None, timeout_s: float = 20) -> str | None:
        deadline = time.time() + timeout_s
        while True:
            proj = projection or self.snapshot(tid)["projection"]
            msg = next((m for m in proj.get("messages", []) if m["id"] == message_id), None)
            if msg and msg.get("runId"):
                return msg["runId"]
            run = next((r for r in proj.get("runs", []) if r.get("userMessageId") == message_id), None)
            if run:
                return run["id"]
            if time.time() > deadline:
                raise t3client.T3Error(f"no run found for message {message_id} after {timeout_s}s")
            projection = None
            time.sleep(0.5)

    # ----- reads (HTTP) ------------------------------------------------------

    def snapshot(self, tid: str) -> dict[str, Any]:
        assert self.api is not None
        snap = self.api.get(f"/api/orchestration/threads/{q(tid)}")
        self.snaps[tid] = snap
        return snap

    def snapshot_or_last(self, tid: str) -> dict[str, Any] | None:
        """The live snapshot, or the last one read if the thread is gone (state rotation)."""
        try:
            return self.snapshot(tid)
        except Exception:  # the thread may no longer exist in this T3 state
            return self.snaps.get(tid)

    def transcript_raw(self, tid: str) -> t3client.HttpResponse:
        assert self.api is not None
        return self.api.get_raw(f"/api/orchestration/threads/{q(tid)}/transcript")

    def transcript(self, tid: str) -> dict[str, Any]:
        res = self.transcript_raw(tid)
        if res.status != 200:
            raise t3client.T3Error(f"transcript: HTTP {res.status} {res.body[:300]}")
        return res.json()

    def shell(self) -> dict[str, Any]:
        assert self.api is not None
        return self.api.get("/api/orchestration/shell")

    def state(self, tid: str, run_id: str | None, snap: dict[str, Any] | None = None) -> dict[str, Any]:
        """How a script tells finished, waiting on a question and waiting on a secret apart.

        * question: a `user_input_request` turn item with status `waiting` in
          the run, whose requestId is a `runtimeRequests` entry with kind
          `user_input` and status `pending`.
        * secret: a `secret_request` turn item in the run with
          `secretStatus: "pending"`. It is not in `runtimeRequests`.
        * finished: the run's status is terminal.
        The shell snapshot's `pendingRuntimeRequest` is recorded too: kind
        `user_input` for both waits, with `userOnly: true` only for a secret.
        """
        snap = snap or self.snapshot(tid)
        proj = snap["projection"]
        if not run_id:
            raise ValueError("state() needs the run id the script's message started")
        run = next((r for r in proj["runs"] if r["id"] == run_id), None)
        if run is None:
            raise t3client.T3Error(f"run {run_id} is not in the thread snapshot")
        items = [i for i in proj["turnItems"] if run and i.get("runId") == run["id"]]
        pending_reqs = {r["id"]: r for r in proj.get("runtimeRequests", []) if r.get("status") == "pending"}
        questions = [i for i in items if i["type"] == "user_input_request" and i.get("status") == "waiting" and pending_reqs.get(i.get("requestId"), {}).get("kind") == "user_input"]
        secrets_ = [i for i in items if i["type"] == "secret_request" and i.get("secretStatus") == "pending"]
        status = run["status"] if run else None
        if questions:
            kind = "question"
        elif secrets_:
            kind = "secret"
        elif status in TERMINAL:
            kind = "finished"
        else:
            kind = "running"
        return {"kind": kind, "run_status": status, "run_id": run["id"] if run else None, "questions": questions, "secrets": secrets_, "snap": snap}

    def shell_pending(self, tid: str) -> dict[str, Any] | None:
        thread = next((t for t in self.shell().get("threads", []) if t["id"] == tid), None)
        return (thread or {}).get("pendingRuntimeRequest")

    def wait(self, tid: str, run_id: str | None, *, until: tuple[str, ...] = ("finished",), timeout_s: float = 240, poll_s: float = 1.5) -> dict[str, Any]:
        """Poll the HTTP snapshot until the run reaches one of the `until` kinds.
        Raises TimeoutError at the deadline."""
        deadline = time.time() + timeout_s
        while True:
            st = self.state(tid, run_id)
            if st["kind"] in until:
                self.rec.event("wait done", thread_id=tid, run_id=run_id, kind=st["kind"], run_status=st["run_status"])
                return st
            if st["kind"] == "finished" and "finished" not in until:
                self.rec.event("wait ended early", thread_id=tid, run_id=run_id, kind=st["kind"], run_status=st["run_status"])
                return st
            if time.time() > deadline:
                raise TimeoutError(f"run {run_id} still {st['kind']}/{st['run_status']} after {timeout_s}s")
            time.sleep(poll_s)

    def require_completed(self, st: dict[str, Any], what: str) -> bool:
        """True if the run completed. Raises Blocked when the run did not complete
        and the gateway log shows the model API refusing this run's requests
        (upstream status and error type of requests inside the run's window)."""
        if st["run_status"] == "completed":
            return True
        refused = self.gateway_refusals(st["snap"], st["run_id"])
        if refused["refused"]:
            raise Blocked("model API refused the harness's requests", {"what": what, "run_status": st["run_status"], **refused})
        return False

    def _runs_tree(self, tid: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Fresh run records of a thread and, recursively, its child threads,
        with every thread that could not be read."""
        out: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        todo = [tid]
        while todo:
            t = todo.pop()
            if t in seen:
                continue
            seen.add(t)
            try:
                proj = self.snapshot(t)["projection"]
            except Exception as exc:
                errors.append(f"{t[-24:]}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            out += [dict(r, _thread=t) for r in proj["runs"]]
            todo += [s["childThreadId"] for s in proj.get("subagents", []) if s.get("childThreadId")]
        return out, errors

    def gateway_refusals(self, snap: dict[str, Any], run_id: str | None) -> dict[str, Any]:
        """Blocked only when: the run ended and has startedAt and completedAt;
        at least one inference request is bound to it (inside its own window and
        no other run's, an open window counting as overlapping); the last user
        message in a bound request's decoded body is the run's user message;
        every bound request is a refusal; and none is ambiguous."""
        proj = snap["projection"]
        tid = proj["thread"]["id"]
        run = next((r for r in proj["runs"] if r["id"] == run_id), {})
        fresh, unreadable = self._runs_tree(tid)
        others = [r for r in fresh if not (r["_thread"] == tid and r["id"] == run_id)]
        for t, sn in self.snaps.items():
            if t not in {r["_thread"] for r in fresh}:
                others += [dict(r, _thread=t) for r in sn["projection"]["runs"]]
        texts = [m["text"] for m in proj.get("messages", []) if m.get("runId") == run_id and m.get("role") == "user"]
        bound = gatewaylog.bind_to_run(self.gateway_dir / "log", self.gateway_records(), run, others, texts)
        refused = gatewaylog.refusals(bound["bound"])
        terminal = run.get("status") in TERMINAL
        # An unreadable thread in the tree may hold a run that overlaps: no block.
        ok = terminal and not unreadable and bool(bound["bound"]) and not bound["ambiguous"] and len(refused) == len(bound["bound"]) and bool(bound["confirmed"])
        native_errors: list[dict[str, Any]] = []
        try:
            self.save_native_sessions("refusal-check", optional=True)
            native_errors = [e for s in self.sessions() for e in s.get("api_errors", [])][:5]
        except Exception as exc:  # the cross-check must not decide anything
            native_errors = [{"error": f"not read: {exc}"}]
        return {
            "refused": ok,
            "run_terminal": terminal,
            "bound_requests": len(bound["bound"]),
            "ambiguous_requests": len(bound["ambiguous"]),
            "unreadable_threads": unreadable,
            "content_confirmed": len(bound["confirmed"]),
            "gateway_refusals": refused[:5],
            "harness_native_errors": native_errors,
            "t3_run_errors": self.run_errors(snap, run_id),
            "reason": bound["reason"],
        }

    @staticmethod
    def run_errors(snap: dict[str, Any], run_id: str | None) -> list[str]:
        out = []
        for i in snap["projection"]["turnItems"]:
            if i["type"] == "error" and i.get("runId") == run_id:
                failure = i.get("failure") or {}
                out.append(" | ".join(str(x) for x in (i.get("title"), failure.get("code"), failure.get("message"), i.get("text")) if x))
        return out

    @staticmethod
    def items(snap: dict[str, Any], run_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return [i for i in snap["projection"]["turnItems"] if (run_id is None or i.get("runId") == run_id) and (kind is None or i["type"] == kind)]

    @staticmethod
    def reply(snap: dict[str, Any], run_id: str | None) -> str | None:
        msgs = [i for i in snap["projection"]["turnItems"] if i["type"] == "assistant_message" and i.get("runId") == run_id]
        return msgs[-1].get("text") if msgs else None

    @staticmethod
    def native_ids(snap: dict[str, Any], run_id: str | None) -> list[str]:
        return [a["nativeThreadId"] for a in snap["projection"]["attempts"] if a.get("runId") == run_id and a.get("nativeThreadId")]

    def turn_outcome(self, tid: str, sent: Sent, what: str, *, timeout_s: float = 240) -> dict[str, Any]:
        """Wait for the run a message started, and summarize it. Raises Blocked on refusal."""
        st = self.wait(tid, sent.run_id, timeout_s=timeout_s)
        completed = self.require_completed(st, what)
        return {
            "run_id": sent.run_id,
            "status": st["run_status"],
            "completed": completed,
            "reply": self.reply(st["snap"], sent.run_id),
            "errors": self.run_errors(st["snap"], sent.run_id),
            "native_ids": self.native_ids(st["snap"], sent.run_id),
            "snap": st["snap"],
        }

    # ----- evidence --------------------------------------------------------

    def native_dir(self) -> Path:
        return self.rec.raw_dir / "native" / self.label

    def save_native_sessions(self, tag: str, *, optional: bool = False) -> list[Path]:
        """Copy the harness's session directory out, scanned before it is written.
        With optional, a missing directory is recorded as an event, not a check."""
        assert self.c is not None
        dest = self.native_dir() / tag
        res = dk.copy_out_scanned(self.c.name, self.harness.native_session_dir, dest, self.key)
        if not res["ok"]:
            if res.get("key_hits"):
                self.rec.check(f"native sessions from {self.label} ({tag}) held the key; not written", False, res["error"], hygiene=True)
            elif optional:
                self.rec.event("native sessions not copied", tag=tag, error=res["error"])
            else:
                self.rec.check(f"native sessions copied from {self.label} ({tag})", False, res["error"])
            return []
        for path in res["files"]:
            self.rec.register(path)
        return sorted(res["files"])

    def sessions(self) -> list[dict[str, Any]]:
        """Every native session file copied from this container, newest copy of each."""
        by_rel: dict[str, Path] = {}
        root = self.native_dir()
        for tag_dir in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime) if root.exists() else []:
            for path in sorted(tag_dir.rglob("*.jsonl")):
                by_rel[str(path.relative_to(tag_dir))] = path
        return native.summarize(self.harness.name, list(by_rel.values()))["sessions"]

    def sqlite_backup(self, tag: str) -> dict[str, Any]:
        """A consistent copy of T3's database through node:sqlite's online backup,
        taken inside the container to a neutral /tmp path, copied out scanned, then removed."""
        assert self.c is not None
        remote = f"/tmp/state-{t3client.new_id()[:8]}.sqlite"
        program = r"""
const s=require('node:sqlite');
(async()=>{
  const db=new s.DatabaseSync(REQ.src,{readOnly:true});
  const pages=await s.backup(db,REQ.dest);
  db.close();
  process.stdout.write(JSON.stringify({pages}));
})().catch(e=>{process.stdout.write(JSON.stringify({error:String(e)}));});
"""
        out = json.loads(t3client.run_node(self.c, program, {"src": f"{dk.T3_BASE_DIR}/userdata/statev2.sqlite", "dest": remote}, timeout_s=120))
        result: dict[str, Any] = {"remote": remote, "backup": out}
        if "error" in out:
            return result
        dest_dir = self.rec.raw_dir / f"t3-db-{self.label}-{tag}"
        res = dk.copy_out_scanned(self.c.name, remote, dest_dir, self.key)
        rm = self.c.exec(["rm", "-f", remote], check=False)
        result.update({"copied": res["ok"], "copy_error": res["error"], "removed_in_container": rm.returncode == 0})
        if res["ok"] and res["files"]:
            self.rec.register(res["files"][0])
            result["path"] = str(res["files"][0])
        return result

    def export_scan(self, extra: dict[str, bytes] | None = None) -> dict[str, Any]:
        assert self.c is not None
        return dk.scan_container_export(self.c.name, self.key, extra)

    def harness_version(self) -> str:
        assert self.c is not None
        return self.c.exec(list(self.harness.version_cmd), check=False).stdout.strip()

    def t3_detected_provider(self) -> dict[str, Any]:
        assert self.c is not None
        raw = self.c.exec(["cat", self.harness.status_cache], check=False).stdout
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {k: data.get(k) for k in ("displayName", "version", "status", "installed", "enabled", "auth")}


class NetSampler:
    """Polls /proc/net/tcp{,6} in the container to see which remote hosts it talks to.

    Short-lived connections between polls can be missed, so the list is a lower bound.
    """

    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.remotes: dict[str, int] = {}
        self.states: dict[str, set[str]] = {}
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None

    def pause(self) -> None:
        """Stop sampling while the runner's own reachability checks probe addresses."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def start(self, c: dk.Container) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(c,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _loop(self, c: dk.Container) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                try:
                    out = c.exec(["cat", "/proc/net/tcp", "/proc/net/tcp6"], check=False, timeout=10).stdout
                    for remote, state in parse_proc_net(out):
                        self.remotes[remote] = self.remotes.get(remote, 0) + 1
                        self.states.setdefault(remote, set()).add(TCP_STATES.get(state, state))
                except Exception:  # the container may be gone between polls
                    pass
            self._stop.wait(self.interval_s)


TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}


def parse_proc_net(text: str) -> set[tuple[str, str]]:
    remotes = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "sl":
            continue
        rem = parts[2]
        if ":" not in rem:
            continue
        addr_hex, port_hex = rem.split(":")
        port = int(port_hex, 16)
        if port == 0:
            continue
        if len(addr_hex) == 8:
            ip = socket.inet_ntop(socket.AF_INET, bytes.fromhex(addr_hex)[::-1])
        elif len(addr_hex) == 32:
            raw = bytes.fromhex(addr_hex)
            raw = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
            ip = socket.inet_ntop(socket.AF_INET6, raw)
            if ip.startswith("::ffff:"):
                ip = ip[7:]
        else:
            continue
        if ip.startswith("127.") or ip == "::1":
            continue
        remotes.add((f"{ip}:{port}", parts[3]))
    return remotes


KNOWN_HOSTS = [
    "api.anthropic.com",
    "statsig.anthropic.com",
    "mcp-proxy.anthropic.com",
    "claude.ai",
    "downloads.claude.ai",
    "ingest.t3.codes",
    "app.t3.codes",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "registry.npmjs.org",
    "pi.dev",
    "models.dev",
    "sentry.io",
    "http-intake.logs.us5.datadoghq.com",
]


def name_remotes(remotes: dict[str, int]) -> list[dict[str, Any]]:
    """Best-effort names for remote IPs, from forward lookups of known hosts and reverse DNS."""
    known: dict[str, set[str]] = {}
    for host in KNOWN_HOSTS:
        try:
            for info in socket.getaddrinfo(host, 443):
                known.setdefault(info[4][0], set()).add(host)
        except OSError:
            pass
    named = []
    for remote, count in sorted(remotes.items()):
        ip, _port = remote.rsplit(":", 1)
        try:
            reverse = socket.gethostbyaddr(ip)[0]
        except OSError:
            reverse = None
        named.append({"remote": remote, "matches_known_hosts": sorted(known.get(ip, set())), "reverse_dns": reverse, "samples": count})
    return named
