"""Real end-to-end check: stock Pi against the scripted server, through the gateway.

Not collected by pytest. Run from the repository root:

    uv run python -m tests.scripted.e2e_pi [--keep-images]

No model API call is made: the gateway's Anthropic route points at the
scripted server over plain HTTP (`insecure_allow_http_upstream`), with a
random placeholder key file. Pi runs directly (`pi -p`, then `pi -p
--continue`), not through T3 Code: the harness container's entrypoint is
replaced with `sleep`, so T3 never starts.

Topology (gateway/README.md, "Docker before 28.0" on this machine):

    agent container --(internal net, opaque name, bridge address flushed)--> gateway
    gateway --(internal net sdev-up-*)--> scripted server (API :8080)
    host --(docker exec)--> scripted server admin (127.0.0.1:8081 in its container)

Names the agent can resolve (its own container, its network, the gateway) are
random. Everything else starts with `sdev-`. Every resource is registered
before it is created and removed at the end, failure or not. Any failed
check raises; the script exits non-zero.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PI_IMAGE = "t3-harness-pi:0.1.12-nightly.20261121.5051"
GATEWAY_IMAGE = "sdev-gateway:dev"
SCRIPTED_IMAGE = "sdev-scripted:dev"
HELPER_IMAGE = "alpine:3.20"
CA_IN_AGENT = "/usr/local/share/ca-certificates/local-ca.crt"
MODEL = "claude-haiku-5-5"


class CheckFailed(Exception):
    pass


def docker(*args: str, input_bytes: bytes | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise CheckFailed(f"docker {' '.join(args[:3])} failed ({p.returncode}): {p.stderr.decode()[-800:]}")
    return p


def opaque() -> str:
    return "n" + secrets.token_hex(6)


def placeholder_key() -> str:
    return "sk-ant-api03-" + secrets.token_urlsafe(70)[:93] + "AA"


def one_file_tar(name: str, data: bytes, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, 0, 0, 0
        info.uname = info.gname = "root"
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Run:
    def __init__(self, out: Path):
        self.out = out
        self.cleanup: list[tuple[str, str]] = []
        self.names: dict[str, str] = {}
        self.report: list[str] = []

    def say(self, line: str) -> None:
        print(line, flush=True)
        self.report.append(line)

    # ------------------------------------------------------------ setup

    def build(self) -> None:
        for dockerfile, tag in (("docker/gateway/Dockerfile", GATEWAY_IMAGE), ("docker/scripted/Dockerfile", SCRIPTED_IMAGE)):
            p = subprocess.run(["docker", "build", "-q", "-f", dockerfile, "-t", tag, "."], cwd=ROOT,
                               capture_output=True, timeout=900)
            if p.returncode != 0:
                raise CheckFailed(f"build {tag}: {p.stderr.decode()[-800:]}")
            self.say(f"built {tag} {p.stdout.decode().strip()[:19]}")

    def topology(self) -> None:
        up = "sdev-up-" + secrets.token_hex(4)
        self.cleanup.append(("network", up))
        docker("network", "create", "--internal", up)
        self.names["upstream_network"] = up

        scripted = "sdev-scripted-" + secrets.token_hex(4)
        self.scripted = scripted
        self.names["scripted_container"] = scripted
        slog = self.out / "scripted-log"
        slog.mkdir(parents=True)
        os.chmod(slog, 0o777)
        self.cleanup.insert(0, ("container", scripted))
        docker("run", "-d", "--name", scripted, "--network", up, "-v", f"{slog}:/var/log/scripted", SCRIPTED_IMAGE)

        # The agent's network: opaque name, internal, bridge address removed
        # before any agent container starts (Docker 27 has no isolated mode).
        agent_net = opaque()
        self.agent_net = agent_net
        self.names["agent_network"] = agent_net
        self.cleanup.append(("network", agent_net))
        for _ in range(40):
            n = secrets.randbelow(150) + 100
            p = docker("network", "create", "--internal", "--subnet", f"172.31.{n}.0/24", agent_net, check=False)
            if p.returncode == 0:
                break
            if b"overlap" not in p.stderr.lower():
                raise CheckFailed(p.stderr.decode())
        else:
            raise CheckFailed("no free subnet")
        self.gateway_ip = f"172.31.{n}.10"
        net_id = docker("network", "inspect", "-f", "{{.Id}}", agent_net).stdout.decode().strip()
        bridge = "br-" + net_id[:12]
        docker("run", "--rm", "--privileged", "--net=host", HELPER_IMAGE, "sh", "-c",
               f"sysctl -qw net.ipv6.conf.{bridge}.disable_ipv6=1 && ip addr flush dev {bridge}")
        self.say(f"agent network {agent_net} 172.31.{n}.0/24, bridge {bridge} flushed")

        # Gateway: Anthropic route to the scripted server over plain HTTP.
        cfg = json.loads((ROOT / "gateway" / "config.json").read_text())
        cfg["routes"] = [r for r in cfg["routes"] if r["name"] == "anthropic"]
        cfg["routes"][0]["upstream"] = f"http://{scripted}:8080"
        cfg["insecure_allow_http_upstream"] = True
        cfg["store_bodies"] = True
        gdir = self.out / "gateway"
        (gdir / "log").mkdir(parents=True)
        os.chmod(gdir / "log", 0o777)
        (gdir / "config.json").write_text(json.dumps(cfg, indent=1))
        keyfile = gdir / "placeholder_key"
        keyfile.write_text(placeholder_key())
        os.chmod(keyfile, 0o644)
        gw = opaque()
        self.gateway = gw
        self.names["gateway_container"] = gw
        self.cleanup.insert(0, ("container", gw))
        docker("run", "-d", "--name", gw, "--network", up,
               "-v", f"{keyfile}:/run/secrets/anthropic_key:ro",
               "-v", f"{gdir / 'log'}:/var/log/gateway",
               "-v", f"{gdir / 'config.json'}:/etc/gw/config.json:ro",
               "-e", "GATEWAY_CONFIG=/etc/gw/config.json",
               GATEWAY_IMAGE)
        docker("network", "connect", "--ip", self.gateway_ip, agent_net, gw)
        deadline = time.time() + 30
        while True:
            p = docker("exec", gw, "cat", "/run/gateway-tls/local-ca.crt", check=False)
            if p.returncode == 0 and b"BEGIN CERTIFICATE" in p.stdout:
                self.ca = p.stdout
                break
            if time.time() > deadline:
                raise CheckFailed("gateway wrote no CA: " + docker("logs", gw, check=False).stderr.decode()[-800:])
            time.sleep(0.5)
        self.say(f"gateway {gw} at {self.gateway_ip}, upstream http://{scripted}:8080")

    def start_agent(self, label: str) -> str:
        name = opaque()
        self.names[f"agent_container_{label}"] = name
        self.cleanup.insert(0, ("container", name))
        docker("create", "--init", "--name", name, "--hostname", "workstation", "--network", self.agent_net,
               "--add-host", f"api.anthropic.com:{self.gateway_ip}",
               "-e", f"NODE_EXTRA_CA_CERTS={CA_IN_AGENT}",
               "-e", f"ANTHROPIC_API_KEY={placeholder_key()}",
               "--entrypoint", "sleep", PI_IMAGE, "infinity")
        docker("cp", "-", f"{name}:{Path(CA_IN_AGENT).parent}", input_bytes=one_file_tar(Path(CA_IN_AGENT).name, self.ca))
        docker("start", name)
        settings = json.dumps({"defaultProvider": "anthropic", "defaultModel": MODEL, "defaultThinkingLevel": "off"})
        docker("exec", "-i", name, "sh", "-c", 'mkdir -p "$HOME/.pi/agent" && cat > "$HOME/.pi/agent/settings.json"',
               input_bytes=settings.encode())
        p = subprocess.run([sys.executable, "-m", "gateway", "verify", "--allow-flushed-bridge", self.gateway, name],
                           cwd=ROOT, capture_output=True, timeout=300)
        (self.out / f"verify-{label}.txt").write_bytes(p.stdout + p.stderr)
        tail = p.stdout.decode().strip().splitlines()[-1] if p.stdout else ""
        self.say(f"agent {label} {name}: gateway verify exit {p.returncode}: {tail}")
        if p.returncode != 0:
            raise CheckFailed(f"gateway verify failed for {name}; see verify-{label}.txt")
        return name

    def stop_agent(self, name: str) -> None:
        docker("rm", "-f", name, check=False)

    # ---------------------------------------------------------- helpers

    def load(self, script: dict) -> None:
        p = docker("exec", "-i", self.scripted, "python", "-m", "cohbench.scripted", "admin", "load", "-",
                   input_bytes=json.dumps(script).encode(), check=False)
        if p.returncode != 0:
            raise CheckFailed(f"script load failed: {p.stdout.decode()} {p.stderr.decode()}")

    def state(self) -> dict:
        p = docker("exec", self.scripted, "python", "-m", "cohbench.scripted", "admin", "state")
        return json.loads(p.stdout)

    def pi(self, agent: str, prompt: str, *, cont: bool = False) -> tuple[int, str, str]:
        args = ["exec", "-w", "/work/project", agent, "pi", "-p"]
        if cont:
            args.append("--continue")
        args.append(prompt)
        p = docker(*args, check=False, timeout=300)
        out, err = p.stdout.decode(), p.stderr.decode()
        self.say(f"  $ pi -p{' --continue' if cont else ''} {prompt!r}\n    exit {p.returncode}\n    stdout: {out.strip()!r}"
                 + (f"\n    stderr: {err.strip()[-400:]!r}" if err.strip() else ""))
        return p.returncode, out, err

    def sh(self, agent: str, cmd: str) -> str:
        p = docker("exec", "-w", "/work/project", agent, "sh", "-c", cmd, check=False)
        return p.stdout.decode() + p.stderr.decode()

    def expect(self, cond: bool, what: str) -> None:
        self.say(f"  {'PASS' if cond else 'FAIL'}  {what}")
        if not cond:
            raise CheckFailed(what)

    # ----------------------------------------------------------- remove

    def teardown(self, keep_images: bool) -> None:
        order = {"container": 0, "network": 1, "image": 2}
        if not keep_images:
            self.cleanup += [("image", GATEWAY_IMAGE), ("image", SCRIPTED_IMAGE)]
        for kind, name in sorted(self.cleanup, key=lambda x: order[x[0]]):
            if kind == "container":
                docker("rm", "-f", name, check=False)
                gone = docker("container", "inspect", name, check=False).returncode != 0
            elif kind == "network":
                docker("network", "rm", name, check=False)
                gone = docker("network", "inspect", name, check=False).returncode != 0
            else:
                docker("image", "rm", name, check=False)
                gone = docker("image", "inspect", name, check=False).returncode != 0
            print(f"removed {kind} {name}: {'gone' if gone else 'STILL PRESENT'}", flush=True)


# ------------------------------------------------------------ scenarios


def scenario_command(r: Run) -> None:
    r.say("scenario 1: run a command and report its output")
    r.load({"version": 1, "handlers": [{
        "steps": [{"do": "bash", "command": "printf 'alpha\\nbeta\\ngamma\\n' | wc -l", "say": "Counting the lines."}],
        "final": "The command printed {{last_output}}"}]})
    agent = r.start_agent("1")
    code, out, _ = r.pi(agent, "How many lines does that printf produce?")
    r.expect(code == 0, "pi exited 0")
    r.expect(out.strip() == "The command printed 3", "Pi printed the scripted final message with the real command output")
    st = r.state()
    r.expect(st["handlers"]["main"][0]["status"] == "done" and not st["errors"], "handler 0 done, no errors")
    r.stop_agent(agent)


def scenario_write_edit(r: Run) -> None:
    r.say("scenario 2: write a file, then edit it")
    r.load({"version": 1, "handlers": [{
        "steps": [
            {"do": "write", "path": "/work/project/notes/todo.txt", "content": "first line\nsecond line\n"},
            {"do": "edit", "path": "/work/project/notes/todo.txt",
             "edits": [{"oldText": "second line", "newText": "edited line"}]},
            {"do": "read", "path": "/work/project/notes/todo.txt"},
        ],
        "final": "Done. The file now reads:\n{{last_output}}"}]})
    agent = r.start_agent("2")
    code, out, _ = r.pi(agent, "Create notes/todo.txt and fix its second line.")
    r.expect(code == 0, "pi exited 0")
    content = r.sh(agent, "cat notes/todo.txt")
    r.say(f"    notes/todo.txt: {content!r}")
    r.expect(content == "first line\nedited line\n", "the file was written by Pi's write tool and changed by its edit tool")
    r.expect("edited line" in out, "the read tool's output reached the final message")
    r.expect(not r.state()["errors"], "no errors")
    r.stop_agent(agent)


def scenario_branch(r: Run) -> None:
    r.say("scenario 3: branch on a failing command")
    # A script naming a variable nothing sets must be refused at load.
    p = docker("exec", "-i", r.scripted, "python", "-m", "cohbench.scripted", "admin", "load", "-",
               input_bytes=json.dumps({"version": 1, "handlers": [{"steps": [], "final": "{{nope}}"}]}).encode(), check=False)
    r.expect(p.returncode != 0 and b"never set" in p.stdout, "a script naming an unset variable is refused at load")
    r.load({"version": 1, "handlers": [{
        "steps": [
            {"do": "bash", "command": "ls /work/project/missing.txt",
             "capture": [{"var": "ls_exit", "regex": "Command exited with code (\\d+)"}]},
            {"do": "branch", "cases": [
                {"when": {"exit": 0}, "steps": [{"do": "reply", "text": "The file already exists."}]},
                {"when": {"all": [{"not": {"exit": 0}}, {"contains": "No such file"}]}, "steps": [
                    {"do": "bash", "command": "echo created > /work/project/missing.txt"},
                    {"do": "reply", "text": "It was missing (ls exited {{ls_exit}}), so I created it."},
                ]},
            ], "else": [{"do": "reply", "text": "Something else happened."}]},
        ]}]})
    agent = r.start_agent("3")
    code, out, _ = r.pi(agent, "Make sure missing.txt exists.")
    r.expect(code == 0, "pi exited 0")
    r.expect(out.strip() == "It was missing (ls exited 2), so I created it.", "the failing-exit branch ran")
    r.expect(r.sh(agent, "cat missing.txt") == "created\n", "the branch's command ran in the container")
    st = r.state()
    r.expect(not st["errors"], "no errors")
    r.stop_agent(agent)


def scenario_capture_multiturn(r: Run) -> None:
    r.say("scenario 4: capture a value, use it in the reply, multi-turn with --continue, then a fresh container")
    r.load({"version": 1, "handlers": [
        {"id": "queue", "steps": [{"do": "bash",
                                   "command": "printf 'queued job %s\\n' \"$(od -An -N4 -tx1 /dev/urandom | tr -d ' \\n')\"",
                                   "capture": [{"var": "job", "regex": "queued job ([0-9a-f]{8})", "required": True}]}],
         "final": "Job {{job}} is queued. Should I start it?", "end": "question"},
        {"id": "start", "expect": {"contains": "start"},
         "steps": [{"do": "bash", "command": "echo 'started {{job}}' > /work/project/started.txt"}],
         "final": "Started job {{job}}."},
        {"id": "after-restart", "steps": [{"do": "read", "path": "/work/project/started.txt"}],
         "final": "After the restart: {{last_output}}"},
    ]})
    agent = r.start_agent("4a")
    code, out1, _ = r.pi(agent, "Queue a job for me.")
    r.expect(code == 0, "turn 1: pi exited 0")
    m = re.search(r"Job ([0-9a-f]{8}) is queued", out1)
    r.expect(m is not None, "turn 1: the reply names the captured job id")
    job = m.group(1)
    code, out2, _ = r.pi(agent, "Yes, start it.", cont=True)
    r.expect(code == 0, "turn 2: pi exited 0")
    r.expect(out2.strip() == f"Started job {job}.", "turn 2 (pi --continue): handler 1 used the value captured in handler 0")
    r.expect(r.sh(agent, "cat started.txt") == f"started {job}\n", "turn 2: the command used the captured value")
    workspace = docker("exec", agent, "tar", "-C", "/work/project", "-cf", "-", ".").stdout
    r.stop_agent(agent)
    # A fresh container with the workspace copied across, as in D-010.
    agent_b = r.start_agent("4b")
    docker("cp", "-", f"{agent_b}:/work/project", input_bytes=workspace)
    code, out3, _ = r.pi(agent_b, "I'm back. Where were we?")
    r.expect(code == 0, "turn 3 (fresh container): pi exited 0")
    r.expect(out3.strip() == f"After the restart: started {job}", "turn 3 in a fresh container got handler 2")
    code, out4, _ = r.pi(agent_b, "Anything else?", cont=True)
    r.expect(code == 0, "turn 4: pi exited 0")
    st = r.state()
    r.expect(out4.strip() == "Sorry, I can't continue with this right now.", "turn 4: the exhausted script gives the neutral fallback")
    r.expect([e["code"] for e in st["errors"]] == ["script_exhausted"], "the exhaustion is in the server's error list, and nothing else")
    idx = [u["index"] for u in st["user_messages"]["main"]]
    r.expect(idx == [0, 1, 2, 3], f"four distinct user messages counted across two containers: {idx}")
    hs = st["handlers"]["main"]
    r.expect([h.get("ended_with") for h in hs[:3]] == ["question", "statement", "statement"], "handler 0 ended with a question")
    r.expect(hs[3]["status"] == "error" and hs[3]["error"] == "script_exhausted",
             "the fourth message is recorded as exhausted")
    r.stop_agent(agent_b)


def scenario_optimise_and_answer(r: Run) -> None:
    r.say("scenario 5: a bounded repeat with a numeric comparison, then a condition and a capture on the user's answer")
    measure = ('n=$(cat ops.txt); if [ "$n" -gt 300 ]; then n=$((n-100)); fi; '
               'echo $n > ops.txt; echo "operations: $n"')
    r.load({"version": 1, "vars": {"best": "1000000"}, "handlers": [
        {"id": "optimise", "end": "question", "steps": [
            {"do": "repeat", "max": 6, "until": {"var": {"name": "improved", "equals": "no"}}, "steps": [
                {"do": "bash", "command": measure,
                 "capture": [{"var": "ops", "regex": "operations: (\\d+)", "required": True}]},
                {"do": "branch", "cases": [{"when": {"var": {"name": "ops", "lt": {"var": "best"}}}, "steps": [
                    {"do": "set", "var": "best", "value": "{{ops}}"},
                    {"do": "set", "var": "improved", "value": "yes"}]}],
                 "else": [{"do": "set", "var": "improved", "value": "no"}]},
            ]}],
         "final": "Best operation count is {{best}}. Should I record it?"},
        {"id": "answer", "capture": [{"var": "run_id", "regex": "run (R-\\d+)"}], "steps": [
            {"do": "branch", "cases": [
                {"when": {"all": [{"var": {"name": "user_message", "contains": "record"}}, {"var_set": "run_id"}]},
                 "steps": [{"do": "write", "path": "/work/project/record.txt", "content": "{{run_id}} {{best}}\n"},
                           {"do": "reply", "text": "Recorded {{best}} as {{run_id}}."}]},
                {"when": {"var": {"name": "user_message", "contains": "I don't know"}},
                 "steps": [{"do": "reply", "text": "I mean the operation count of {{best}}. Should I record it?",
                            "end": "question"}]},
            ], "else": [{"do": "reply", "text": "Not recorded."}]}]},
    ]})
    agent = r.start_agent("5")
    r.sh(agent, "echo 500 > ops.txt")
    code, out1, _ = r.pi(agent, "Make the job cheaper. Stop when it no longer improves.")
    r.expect(code == 0, "turn 1: pi exited 0")
    r.expect(out1.strip() == "Best operation count is 300. Should I record it?", "the repeat kept the best count")
    r.expect(r.sh(agent, "cat ops.txt") == "300\n", "the commands ran in the container: 500, 400, 300, 300")
    log = [json.loads(x) for x in (r.out / "scripted-log" / "requests.jsonl").read_text().splitlines()]
    reps = [x["repeats"] for x in log if x.get("repeats")]
    r.say(f"    repeat record: {reps[-1] if reps else None}")
    r.expect(bool(reps) and reps[-1][0]["iterations"] == 3 and reps[-1][0]["stopped"] == "until",
             "three rounds, stopped by the condition, not by the maximum")
    answer = "Yes, record it as run R-42."
    p = docker("exec", r.scripted, "python", "-m", "cohbench.scripted", "admin", "next", "1", answer, check=False)
    r.expect(p.returncode == 0, "the runner announced user message 1 through the admin listener")
    code, out2, _ = r.pi(agent, answer, cont=True)
    r.expect(code == 0, "turn 2: pi exited 0")
    r.expect(out2.strip() == "Recorded 300 as R-42.", "turn 2 branched on the user's message and used its capture")
    r.expect(r.sh(agent, "cat record.txt") == "R-42 300\n", "the write used values from the user's message and the repeat")
    st = r.state()
    r.expect(not st["errors"] and st["announcements"][0]["outcome"] == "arrived", "no errors; the announcement arrived")
    r.stop_agent(agent)


def summarize(r: Run) -> None:
    log = (r.out / "scripted-log" / "requests.jsonl").read_text().splitlines()
    recs = [json.loads(x) for x in log]
    api = [x for x in recs if "seq" in x or x.get("path", "").startswith("/v1")]
    r.say(f"scripted server: {len(api)} API requests")
    for x in api:
        r.say("  seq={seq} status={status} thread={thread} match={match} conv={conversation} handler={handler} "
              "step={step} decision={decision} stop={stop_reason} users={users} errors={errors}".format(
                  seq=x.get("seq"), status=x.get("status"), thread=x.get("thread"), match=x.get("match"),
                  conversation=x.get("conversation"), handler=x.get("handler"), step=x.get("step"),
                  decision=x.get("decision"), stop_reason=x.get("stop_reason"),
                  users=[u["index"] for u in x.get("user_messages", [])],
                  errors=[e["code"] for e in x.get("errors", [])]))
    bad = [x for x in api if x.get("status") != 200]
    r.expect(not bad, "every API request the server got was answered 200")
    glog = [json.loads(x) for x in (r.out / "gateway" / "log" / "requests.jsonl").read_text().splitlines()]
    calls = [g for g in glog if g.get("route") == "anthropic"]
    r.say(f"gateway: {len(calls)} forwarded requests; usage_status {sorted({g.get('usage_status') for g in calls})}; "
          f"statuses {sorted({g.get('status') for g in calls})}; paths {sorted({g.get('path') for g in calls})}")
    r.expect(calls and all(g.get("status") == 200 and g.get("usage_status") == "complete" and g.get("relay_complete")
                           for g in calls), "the gateway relayed every response in full and parsed complete usage")
    r.expect(all(g.get("response_model") == MODEL for g in calls), "every response names the requested model")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out) if a.out else ROOT / "runs" / "scripted-e2e" / time.strftime("%Y-%m-%dT%H%M%S", time.gmtime())
    out.mkdir(parents=True)
    r = Run(out)
    ok = False
    try:
        r.build()
        r.topology()
        scenario_command(r)
        scenario_write_edit(r)
        scenario_branch(r)
        scenario_capture_multiturn(r)
        scenario_optimise_and_answer(r)
        summarize(r)
        ok = True
    except CheckFailed as e:
        r.say(f"CHECK FAILED: {e}")
    finally:
        try:
            for c in (getattr(r, "scripted", None), getattr(r, "gateway", None)):
                if c:
                    (out / f"logs-{'scripted' if c == getattr(r, 'scripted', None) else 'gateway'}.txt").write_bytes(
                        docker("logs", c, check=False).stderr + docker("logs", c, check=False).stdout)
        finally:
            (out / "names.json").write_text(json.dumps(r.names, indent=1))
            (out / "report.txt").write_text("\n".join(r.report) + "\n")
            r.teardown(a.keep_images)
    print(f"{'PASSED' if ok else 'FAILED'}; evidence in {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
