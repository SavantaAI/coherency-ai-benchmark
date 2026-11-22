"""A small stand-in for the runner's D-046 behaviour, for the long-horizon
scripts, until the runner's change lands.

What it does, in the runner's terms (cohbench/runner/README.md, D-046):

- each message starts a turn that `MiniPi` (minipi.py) advances one tool
  call per `step_s` seconds of a fake clock, plus the delay the script asks
  for;
- after every tool call it evaluates the task's triggers (the subset the
  generators use: `changes`, `turns`, `handbacks`, `content_matches`,
  `fired`, `all`, `any`, with `since`) on the files the calls changed;
- a correction or a reset notice whose trigger holds mid-turn goes into the
  running turn with the next tool result, as Pi takes a steer; at a
  hand-back it goes with the next message;
- after a notice is delivered, the session is reset at the agent's next
  hand-back, or after `grace_s` of episode time, when the turn is cut off;
  the fresh context's first message is the reset's continuation, sent after
  the return delay, and the reset event fires at that send (as the runner);
- at a hand-back the output is judged by the instance's scripted policy with
  the card items unlocked by then, the reply composed as the runner composes
  it, and everything due goes in one message after the return delay; a fixed
  message with `override_policy` replaces the reply.

It is not the runner: no budgets, probes, reopens or watcher. A run that
would need a reopen is an error here. `tests/tasks/test_long.py` uses it.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cohbench.runner.policy import Entry, ScriptedPolicy, compose
from cohbench.runner.worklog import glob_match

from .minipi import FINAL_S, MiniPi, run_metric


@dataclass
class Output:
    t: float
    text: str
    label: str
    reply: str | None
    context: int  # 1 before the reset, 2 after it


@dataclass
class D046Result:
    outputs: list[Output]
    user: list[tuple[float, str]]
    fired: dict[str, float]
    notice_mid_turn: bool | None
    reset_by: str | None  # "handback" or "grace"
    notice_index: int | None  # how many tool calls had run when the notice reached the agent
    pi: MiniPi
    readings: list[tuple[float, dict[str, Any]]] = field(default_factory=list)

    @property
    def tree(self) -> dict[str, str]:
        return self.pi.tree

    def errors(self) -> list[Any]:
        return self.pi.ep.state()["errors"]


class D046Run:
    def __init__(self, inst: Path, who: str, tmp: Path, *, grace_s: float = 120.0, step_s: float = 2.0):
        self.task = json.loads((inst / "task.json").read_text())
        script = json.loads((inst / "scripted" / f"{who}-run.json").read_text())["script"]
        self.policy = ScriptedPolicy.factory(json.loads((inst / "scripted" / f"{who}-policy.json").read_text()))()
        work = tmp / "work"
        shutil.copytree(inst / "workspace", work)
        self.pi = MiniPi(script, work)
        self.metric = (self.task["watcher"]["metric"]["command"], inst / "metric") if self.task["watcher"]["metric"] else None
        self.grace_s, self.step_s = grace_s, step_s
        self.delay_s = self.task["user"]["return_delay_ms"] / 1000
        self.t = 0.0
        self.fired: dict[str, float] = {}
        self.queued: set[str] = set()
        self.changes: list[tuple[float, str]] = []
        self.turns: list[float] = []
        self.handbacks: list[float] = []
        self.transcript: list[Entry] = []
        self.outputs: list[Output] = []
        self.user: list[tuple[float, str]] = []
        self.steers: list[tuple[str, str]] = []  # (event name, text) to go into the running turn
        self.due: list[tuple[str, str, bool]] = []  # (event name, text, overrides the reply) for the next message
        self.notice_at: float | None = None
        self.notice_mid: bool | None = None
        self.notice_index: int | None = None
        self.reset_by: str | None = None
        self.context = 1
        self.readings: list[tuple[float, dict[str, Any]]] = []

    # triggers

    def _holds(self, trig: dict[str, Any]) -> bool:
        (k, v), = trig.items()
        if k == "all":
            return all(self._holds(x) for x in v)
        if k == "any":
            return any(self._holds(x) for x in v)
        if k == "count":
            return sum(self._holds(x) for x in v["of"]) >= v["at_least"]
        if k == "not":
            return not self._holds(v)
        if k == "fired":
            return v in self.fired
        if k == "content_matches":
            return any(glob_match(v["path"], p) and re.search(v["regex"], text, re.M) for p, text in self.pi.tree.items())
        if k == "path_exists":
            return any(glob_match(v, p) for p in self.pi.tree)
        since = v.get("since")
        if since is not None and since not in self.fired:
            return False
        t0 = self.fired.get(since, -1.0) if since else -1.0
        if k == "changes":
            n = len({t for t, p in self.changes if t > t0 and glob_match(v["path"], p)}) if v.get("unit") == "batch" else sum(1 for t, p in self.changes if t > t0 and glob_match(v["path"], p))
        elif k == "turns":
            n = sum(1 for t in self.turns if t > t0)
        elif k == "handbacks":
            n = sum(1 for t in self.handbacks if t > t0)
        else:
            raise AssertionError(f"a trigger the stand-in does not read: {k}")
        return n >= v["at_least"]

    def _evaluate(self, mid_turn: bool) -> None:
        for e in self.task["events"]:
            name, a = e["name"], e["action"]
            if name in self.fired or name in self.queued or not self._holds(e["trigger"]):
                continue
            if a["type"] == "observe":
                self.fired[name] = self.t
            elif a["type"] == "correction":
                self.queued.add(name)
                (self.steers.append((name, a["text"])) if mid_turn else self.due.append((name, a["text"], False)))
            elif a["type"] == "reset":
                self.queued.add(name)
                self.notice_mid = mid_turn
                (self.steers.append((name, a["notice"])) if mid_turn else self.due.append((name, a["notice"], False)))
            elif a["type"] == "message":
                self.queued.add(name)
                self.due.append((name, a["text"], a.get("override_policy", True)))

    def _delivered(self, name: str, text: str) -> None:
        self.user.append((self.t, text))
        self.transcript.append(Entry("user", text))
        a = next(e["action"] for e in self.task["events"] if e["name"] == name)
        if a["type"] == "reset":
            self.notice_at = self.t
        else:
            self.fired[name] = self.t

    # the card

    def _card(self) -> dict[str, Any]:
        c = self.task["user"]["card"]
        out = {"facts": [], "decisions": []}
        for key in ("facts", "decisions"):
            out[key] = [{k: x for k, x in it.items() if k != "available_after"} for it in c.get(key, [])
                        if it.get("available_after") in (None, *self.fired)]
        return out

    # one turn

    def _turn(self, text: str) -> tuple[str | None, bool]:
        reply = self.pi.user(text)
        while reply["calls"]:
            self.t += self.step_s + reply["delay"]
            steer = None
            if self.steers:
                name, steer = self.steers.pop(0)
                self._delivered(name, steer)
            changed, reply = self.pi.step(steer)
            if steer is not None and steer.startswith("I'm closing"):
                self.notice_index = len(self.pi.log)
            self.changes += [(self.t, p) for p in sorted(changed)]
            if changed and self.metric:
                self.readings.append((self.t, run_metric(self.metric[0], self.metric[1], self.pi.tree)))
            self._evaluate(mid_turn=True)
            if self.notice_at is not None and self.t - self.notice_at >= self.grace_s:
                return None, True
        self.t += FINAL_S
        return reply["text"], False

    def _reset(self, by: str) -> str:
        self.t += self.delay_s  # the continuation goes after the return delay, and the reset event fires at its send
        self.fired["handoff"] = self.t
        self.reset_by = by
        self.notice_at = None
        self.context = 2
        self.pi.new_context()
        return next(e["action"]["continuation"] for e in self.task["events"] if e["name"] == "handoff")

    def run(self, max_turns: int = 30) -> D046Result:
        msg: str | None = self.task["prompt"]["opening"]
        for _ in range(max_turns):
            assert msg is not None
            self.user.append((self.t, msg))
            self.transcript.append(Entry("user", msg))
            final, cut = self._turn(msg)
            if cut:  # the grace ran out: the runner interrupts the agent and resets
                msg = self._reset("grace")
                continue
            assert final is not None
            j = self.policy.judge(self._card(), list(self.transcript), final)
            card = self._card()
            texts = {f["id"]: f["text"] for f in card["facts"]}
            texts.update({d["id"]: d["answer"] for d in card["decisions"]})
            r = self.task["user"]["replies"]
            reply = compose(j, texts, list(self.transcript), r["unanswerable"], r["already_given"])
            self.outputs.append(Output(self.t, final, j.label, reply, self.context))
            self.transcript.append(Entry("agent", final))
            self.turns.append(self.t)
            self.handbacks.append(self.t)
            if self.notice_at is not None:  # the next hand-back after a notice ends the context
                msg = self._reset("handback")
                continue
            self._evaluate(mid_turn=False)
            override = any(o for _n, _t, o in self.due)
            parts = ([] if override or reply is None else [reply]) + [t for _n, t, _o in self.due]
            for name, text, _o in self.due:
                a = next(e["action"] for e in self.task["events"] if e["name"] == name)
                if a["type"] == "reset":
                    self.notice_at = self.t + self.delay_s
                else:
                    self.fired[name] = self.t + self.delay_s
            self.due = []
            if not parts:
                missing = [e["name"] for e in self.task["events"] if e["required"] and e["name"] not in self.fired]
                assert not missing, f"the stand-in does not reopen: {missing} never fired"
                break
            self.t += self.delay_s
            msg = "\n\n".join(parts)
        else:
            raise AssertionError("too many turns")
        self.user.append((self.t, self.task["awareness"]["question"]))
        self._turn(self.task["awareness"]["question"])
        return D046Result(self.outputs, self.user, dict(self.fired), self.notice_mid, self.reset_by, self.notice_index, self.pi, self.readings)
