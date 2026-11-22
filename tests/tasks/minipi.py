"""A stand-in for Pi in this process, for running the generated scripts.

`MiniPi` sends requests shaped like Pi 1.6.0's (a thread create, then
continues with only the new messages; the system prompt and tools of a
stored Pi request) to the real scripted model engine
(`cohbench.scripted.Episode`), and runs each tool call it gets back on a
local copy of the workspace: `bash` through `bash -c` with `/work/project`
replaced by the copy's path, `write`, `edit` and `read` on files. Results
come back as Pi shapes them: plain output on exit 0, otherwise the output, a
blank line and `Command exited with code N`, flagged as an error.

`ScriptedEpisode` plugs it into the runner's in-memory episode
(`tests/runner/fake.py`): each message the runner sends starts a live turn
that advances one tool call per `STEP_S` of the fake clock, the files a call
changed become the fake watcher's writes at that instant, a correction the
runner steers in goes into the next tool result's request as Pi delivers a
steer, and a reset starts a new conversation on the same files. With a
metric, every write gets the metric's reading of the tree after it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from cohbench.scripted import Episode, load_script
from cohbench.scripted.wire import parse_request

from cohbench.runner.port import TurnNotRunning
from tests.runner.fake import Act, FakeEpisode

FIXTURE = sorted((Path(__file__).parents[1] / "scripted" / "fixtures" / "steering").glob("*.request"))[0]
_PI = json.loads(FIXTURE.read_text())
PI_SYSTEM, PI_TOOLS = _PI["system"], _PI["tools"]
WORKSPACE = "/work/project"
STEP_S = 2.0
FINAL_S = 0.5
SKIP = ("__pycache__", ".git")


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not any(s in rel.split("/") for s in SKIP):
            out[rel] = p.read_text(encoding="utf-8")
    return out


class MiniPi:
    def __init__(self, script: dict[str, Any], work: Path, model: str = "claude-haiku-5-5"):
        self.ep = Episode()
        self.ep.load(load_script(script))
        self.work = work
        self.home = work.parent / "home"
        self.home.mkdir(exist_ok=True)
        self.model = model
        self.last_id: str | None = None
        self.calls: list[dict[str, Any]] = []  # tool calls not yet answered
        self.log: list[dict[str, Any]] = []  # every tool call and its result, in order
        self.tree = snapshot(work)

    def new_context(self) -> None:
        self.last_id = None

    def _send(self, new: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "max_tokens": 128000, "stream": True, "thinking": {"type": "disabled"}}
        if self.last_id is None:
            body.update(messages=new, system=PI_SYSTEM, tools=PI_TOOLS, thread={"type": "create"})
        else:
            body.update(messages=new, thread={"type": "continue", "previous_message_id": self.last_id})
        out = self.ep.handle(parse_request(body))
        self.ep.reply_attempt(out.msg_id)
        self.ep.reply_sent(out.msg_id, True)
        self.last_id = out.msg_id
        self.calls = [b for b in out.blocks if b["type"] == "tool_use"]
        text = "".join(b["text"] for b in out.blocks if b["type"] == "text")
        return {"text": text, "stop": out.stop_reason, "calls": list(self.calls), "delay": float(out.delay or 0.0)}

    def user(self, text: str) -> dict[str, Any]:
        """Start a turn with a user message."""
        return self._send([{"role": "user", "content": [{"type": "text", "text": text}]}])

    def step(self, steer: str | None = None) -> tuple[dict[str, str], dict[str, Any]]:
        """Run the pending tool calls, send their results (and a steer after
        them, as Pi does), and return (files changed, the next reply)."""
        results = []
        for c in self.calls:
            text, err = self._run(c["name"], c["input"])
            self.log.append({"tool": c["name"], "input": c["input"], "result": text, "is_error": err})
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": text, "is_error": err})
        new = [{"role": "user", "content": results}]
        if steer is not None:
            new.append({"role": "user", "content": [{"type": "text", "text": steer}]})
        now = snapshot(self.work)
        gone = sorted(set(self.tree) - set(now))
        if gone:
            raise AssertionError(f"a scripted step removed files, which the fake watcher cannot record: {gone}")
        changed = {p: t for p, t in now.items() if self.tree.get(p) != t}
        self.tree = now
        return changed, self._send(new)

    def _path(self, p: str) -> Path:
        if not p.startswith(WORKSPACE + "/"):
            raise AssertionError(f"a scripted path outside the workspace: {p}")
        return self.work / p[len(WORKSPACE) + 1:]

    def _run(self, name: str, inp: dict[str, Any]) -> tuple[str, bool]:
        if name == "bash":
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state")}
            r = subprocess.run(["bash", "-c", inp["command"].replace(WORKSPACE, str(self.work))], cwd=self.work, capture_output=True,
                               text=True, env=env, timeout=120)
            out = (r.stdout + r.stderr).replace(str(self.work), WORKSPACE).rstrip("\n")
            return (out, False) if r.returncode == 0 else (f"{out}\n\nCommand exited with code {r.returncode}", True)
        if name == "write":
            f = self._path(inp["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(inp["content"], encoding="utf-8")
            return f"Successfully wrote {len(inp['content'].encode())} bytes to {inp['path']}", False
        if name == "edit":
            f = self._path(inp["path"])
            text = f.read_text(encoding="utf-8")
            for e in inp["edits"]:
                if text.count(e["oldText"]) != 1:
                    return f"Could not find the exact text to replace in {inp['path']}", True
                text = text.replace(e["oldText"], e["newText"])
            f.write_text(text, encoding="utf-8")
            return f"Successfully replaced text in {inp['path']}.", False
        if name == "read":
            return self._path(inp["path"]).read_text(encoding="utf-8"), False
        raise AssertionError(f"a tool the scripts should not call: {name}")


def run_metric(command: list[str], metric_dir: Path, tree: dict[str, str]) -> dict[str, Any]:
    """The task's metric command on a copy of `tree`, as the watcher runs it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "work"
        root.mkdir()
        home, scratch = Path(tmp) / "home", Path(tmp) / "temp"
        home.mkdir()
        scratch.mkdir()
        for p, t in tree.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text(t, encoding="utf-8")
        argv = [sys.executable if command[0] == "python3" else command[0]] + [a.replace("/opt/task", str(metric_dir)) for a in command[1:]]
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state"), "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"})
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


class ScriptedEpisode(FakeEpisode):
    """The fake episode, with turns computed live by `MiniPi`."""

    def __init__(self, pi: MiniPi, out: Path, clock: Any, *, metric: tuple[list[str], Path] | None = None, **kw: Any):
        self.pi = pi
        self.steers: list[str] = []
        self.next_t: float | None = None
        self.live = False
        self.metric_cmd = metric
        self.readings: list[tuple[float, dict[str, Any]]] = []  # (episode clock, reading) after each changing step
        self.turn_texts: list[str] = []
        self.notice_indices: list[int] = []
        super().__init__([], out, clock, tree=dict(pi.tree), metric=metric is not None, **kw)
        self._wrap(self.watchers[-1])

    def _wrap(self, w: Any) -> None:
        if self.metric_cmd is None:
            return
        orig = w.write

        def write(path: str, content: str, _orig=orig, _w=w) -> None:
            tree = dict(_w.tree)
            tree[path] = content
            _w.last_reading = run_metric(self.metric_cmd[0], self.metric_cmd[1], tree)
            _orig(path, content)

        w.write = write

    def reset(self, text: str, **kw: Any) -> Any:
        n = len(self.watchers)
        s = super().reset(text, **kw)
        if len(self.watchers) > n:
            self._wrap(self.watchers[-1])
        return s

    def _next_act(self) -> None:
        super()._next_act()
        via, text = self.received[-1]
        if via == "reset":
            self.pi.new_context()
        assert isinstance(text, str), "the scripts never leave a question pending"
        self.cur = Act(duration_s=1e9, final=None)
        self.started_acts[-1] = (self.c.clock, self.cur)
        self.live = True
        self.steers = []
        self.turn_texts.append(text)
        self._reply(self.pi.user(text), self.c.clock)

    def _reply(self, reply: dict[str, Any], at: float) -> None:
        assert self.cur is not None
        if reply["stop"] == "end_turn" or not reply["calls"]:
            self.cur.final = reply["text"] or None
            self.cur.duration_s = at - self.cur_started + FINAL_S
            self.live = False
            self.next_t = None
        else:
            self.next_t = at + STEP_S + reply["delay"]

    def correct(self, text: str, **kw: Any) -> Any:
        if self.cur is not None and not self.live:
            # The turn's last reply has been decided: Pi would take the text as the next turn's message.
            raise TurnNotRunning("the turn is ending")
        s = super().correct(text, **kw)
        self.steers.append(text)
        return s

    def _advance(self, horizon: float) -> None:
        while self.live and self.next_t is not None and self.next_t <= horizon + 1e-9:
            assert self.cur is not None
            at = self.next_t
            steer = self.steers.pop(0) if self.steers else None
            changed, reply = self.pi.step(steer)
            if steer and steer.startswith("I'm closing this session"):
                self.notice_indices.append(len(self.pi.log))
            for p, t in sorted(changed.items()):
                self.cur.writes.append((at - self.cur_started, p, t))
            if changed and self.metric_cmd is not None:
                self.readings.append((at, run_metric(self.metric_cmd[0], self.metric_cmd[1], self.pi.tree)))
            self._reply(reply, at)

    def wait(self, timeout_s: float) -> Any:
        if self.cur is not None and self.live:
            self._advance(self.c.clock + timeout_s + self.finish_s)
        return super().wait(timeout_s)


def copy_workspace(instance: Path, dest: Path) -> Path:
    shutil.copytree(instance / "workspace", dest)
    return dest
