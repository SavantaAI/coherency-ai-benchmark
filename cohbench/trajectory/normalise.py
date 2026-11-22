"""Raw T3 Code records of an episode -> one normalised trajectory and its run manifest.

The code here is shared by every harness. The harness profile
(`adapters/`) answers four questions: which neutral tool a native tool name
is, how its arguments map to neutral fields, how to clean its output text,
and how to read its session files. Everything else, including every event
the trajectory holds, is built here from T3 Code's records, which have the
same shape for every harness, and from the runner's own events.

Order and ids. Every event gets a time. Events are sorted once, by time and
then by the order they were made, and only then do turns, calls, questions
and subagents get their local numbers, in file order. Causal order is kept by
construction: a turn starts no later than its first event and ends no
earlier than its last, a child's events come no earlier than its
subagent_start, and a subagent_end comes no earlier than the child's last
event.

Everything removed on the way (native ids, tool names, withheld arguments
and outputs, decorations, dropped items, steering mechanics, cross-check
findings) goes into the manifest, keyed by the event's final `seq`.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import format as F
from .adapters.base import NativeSession, Profile, ToolArgs
from .io import Trajectory
from .t3 import Thread, tool_text

DRIVER_SUFFIX = re.compile(r"\n?… output truncated for transport\Z")
SENDER_NOTE = re.compile(r"\A\[T3 Code:[^\]\n]*\]\n\n")
ROLE_PREFIX = re.compile(r"\AAct as the [^\n.]{1,60} sub-agent for this task\.\n\n")
RUN_END = {"completed": "finished", "failed": "failed", "cancelled": "interrupted", "interrupted": "interrupted"}
RUN_OPEN = ("pending", "queued", "starting", "running", "waiting")
ITEM_STATUS = {"completed": "ok", "failed": "error", "cancelled": "cancelled", "interrupted": "cancelled", "declined": "cancelled"}
ITEM_OPEN = ("pending", "running", "waiting", "inProgress", "in_progress", "started")
SECRET_STATUS = {"saved": "saved", "declined": "declined", "cancelled": "cancelled", "dismissed": "cancelled", "pending": "open"}
T3_ONLY = ("checkpoint",)
# T3's own records of things the trajectory already holds, left out with a
# reason in the manifest's `dropped` list (seen in a real capture on
# 2026-11-22: an interrupt of a tree whose child had a pending question).
T3_RECORDS = {
    "run_interrupt_result": "T3's record that an interrupt took effect; the run's end records it",
    "notification": "T3's notice to a parent of a delegated task's outcome; the subagent's end records it",
}
CLASS_RE = re.compile(r"\A[a-z][a-z0-9_]{0,40}\Z")
TOOL_ITEM_TYPES = ("command_execution", "dynamic_tool", "mcp_tool_call", "file_change", "file_read", "file_search", "web_search")


class NormaliseError(ValueError):
    """Raised on records the normaliser cannot interpret (fail closed)."""


@dataclass
class Segment:
    """One source of context segments: the threads of one T3 database (or
    export), the episode's main thread among them, and the harness sessions
    copied from the same container."""

    threads: dict[str, Thread]
    main: str | None = None
    sessions: list[NativeSession] = field(default_factory=list)

    def main_thread(self) -> Thread:
        if self.main:
            return self.threads[self.main]
        roots = [t for t in self.threads.values() if not (t.thread.get("lineage") or {}).get("parentThreadId")]
        if len(roots) != 1:
            raise NormaliseError(f"segment has {len(roots)} root threads; name the main one")
        return roots[0]


@dataclass
class RunnerEvent:
    """An event the runner authors. `utc` is the runner's UTC time. `ref`
    names what it is about, by T3 id: {"item": turn item id}, {"run": run id}
    (that run's last user-channel output), {"message": user message id} or
    {"thread": thread id}. `fields` are the event's own fields
    (format.RUNNER_FIELDS) other than `target` and `t_end`; a wait gives
    `end_utc`."""

    type: str
    utc: str | datetime
    ref: dict[str, str] | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    end_utc: str | datetime | None = None


@dataclass
class Hints:
    """What only the runner knows. Keys are T3 ids the runner chose or read."""

    workspace: str = "/work/project"
    user_kinds: dict[str, str] = field(default_factory=dict)  # T3 messageId -> user kind
    answers: dict[str, str] = field(default_factory=dict)  # T3 request id or question item id -> answer text
    runner: list[RunnerEvent] = field(default_factory=list)
    run_info: dict[str, Any] = field(default_factory=dict)  # harness, versions, model: manifest only
    # Workspace paths of the harness's instruction files (coached mode), and
    # Git ref namespaces the driver writes. Both are excluded from the bundle.
    instruction_files: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
    driver_ref_namespaces: tuple[str, ...] = ("refs/t3/",)
    # Intervals the episode clock leaves out (D-020): {"start_utc", "end_utc", "reason"}
    # with reason handoff, probe or export.
    excluded: list[dict[str, Any]] = field(default_factory=list)
    # Normalise an old capture whose answers cannot all be recovered: the
    # answer is marked missing, and the trajectory cannot make a bundle.
    audit_only: bool = False
    # D-020: the driver's dispatch of the opening prompt (UTC), time 0 of
    # every bundle time. The runner's elapsed time counts from the same
    # instant; without it, time 0 falls back to T3's opening item and the
    # trajectory cannot make a bundle with a runner record.
    episode_start_utc: str | None = None
    # A replay of an episode an older runner recorded (before D-035): its
    # waits carry a single `cause` and charges measured from its own stamps.
    # The trajectory says so (`wait_charges: measured`); a charge may then be
    # 1 ms above the span its floored stamps give, and the single cause is
    # accepted with a runner record.
    legacy_waits: bool = False
    # Whether the runner's hand-back events are the record of the hand-backs
    # (the current runner: pass True). None: inferred, True when any
    # `handback` event is given (an older record), else False.
    handbacks_recorded: bool | None = None


    @classmethod
    def from_dict(cls, h: dict[str, Any], workspace: str | None = None) -> "Hints":
        """Hints from JSON: every field, with runner events as
        {type, utc, ref, fields, end_utc}."""
        known = {"workspace", "user_kinds", "answers", "runner", "run_info", "instruction_files", "driver_ref_namespaces", "excluded", "audit_only", "episode_start_utc", "legacy_waits",
                 "handbacks_recorded"}
        extra = set(h) - known
        if extra:
            raise NormaliseError(f"hints: unknown field(s) {sorted(extra)}")
        runner = [RunnerEvent(x["type"], x["utc"], x.get("ref"), x.get("fields", {}), x.get("end_utc")) for x in h.get("runner", [])]
        out = cls(user_kinds=dict(h.get("user_kinds", {})), answers=dict(h.get("answers", {})), runner=runner, run_info=dict(h.get("run_info", {})),
                  excluded=list(h.get("excluded", [])), audit_only=bool(h.get("audit_only", False)), episode_start_utc=h.get("episode_start_utc"),
                  legacy_waits=bool(h.get("legacy_waits", False)), handbacks_recorded=h.get("handbacks_recorded"))
        out.workspace = workspace or h.get("workspace") or out.workspace
        if "instruction_files" in h:
            out.instruction_files = tuple(h["instruction_files"])
        if "driver_ref_namespaces" in h:
            out.driver_ref_namespaces = tuple(h["driver_ref_namespaces"])
        return out


# Which reference kinds each runner event takes (`resolve`).
RUNNER_REFS: dict[str, tuple[str, ...]] = {
    "verdict": ("item", "run"), "wait": ("item", "run"), "unjudged": ("item", "run"), "handback": ("item",),
    "correction_delivered": ("item", "message"), "controlled_reset": ("item", "run", "message", "thread"),
    "reopen": ("item", "run", "message", "thread"), "notice_delivered": ("item", "message"),
}
RUNNER_NEEDS_REF = ("verdict", "correction_delivered", "unjudged", "notice_delivered")
# A delivery event and the user kind it names.
DELIVERY_KINDS = {"correction_delivered": "correction", "notice_delivered": "reset_notice"}
# `utc`: the tree in effect at that instant (ISO 8601 with a zone), the
# batch whose last write is the latest at or before it; an instant inside a
# batch's first-to-last write span is ambiguous and gives no target.
PROBE_TARGET_REFS = {"output": ("item", "run"), "message": ("message", "item", "run"), "change": ("mark", "utc"), "attempt": ("mark", "utc")}


def wait_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """A wait's fields with `causes` (everything due at the hand-back, a
    sorted list). The single `cause` of an older runner becomes a one-item
    `causes`; the second value says so (accepted only for an episode with
    no runner record)."""
    if "cause" in fields and "causes" not in fields:
        f = {k: v for k, v in fields.items() if k != "cause"}
        f["causes"] = [fields["cause"]]
        return f, True
    if isinstance(fields.get("causes"), list) and all(isinstance(x, str) for x in fields["causes"]):
        return {**fields, "causes": sorted(set(fields["causes"])) if len(set(fields["causes"])) == len(fields["causes"]) else fields["causes"]}, False
    return fields, False


def check_runner_inputs(hints: Hints, probe_targets: dict[Any, Any] | None = None) -> list[str]:
    """The runner's inputs checked on their own, without a T3 trajectory:
    runner event types, fields and their types, references by kind, times;
    user kinds; answers; excluded intervals; probe targets. Every error
    `normalise` would raise for the shape of these inputs is here; what needs
    the trajectory (that a reference names something) is not."""
    from .validate import _type_ok

    errors: list[str] = []
    for n, r in enumerate(hints.runner):
        where = f"runner event {n} ({r.type})"
        if r.type not in F.RUNNER_FIELDS:
            errors.append(f"{where}: unknown type; one of {sorted(F.RUNNER_FIELDS)}")
            continue
        try:
            if _ts(r.utc) is None:
                errors.append(f"{where}: no time")
            if r.end_utc is not None:
                _ts(r.end_utc)
        except (ValueError, TypeError, AttributeError):
            errors.append(f"{where}: a time is not ISO 8601")
        spec = F.RUNNER_INPUT_FIELDS[r.type]
        if not isinstance(r.fields, dict):
            errors.append(f"{where}: fields is an object")
            continue
        fields_in = wait_fields(r.fields)[0] if r.type == "wait" else r.fields
        extra = set(fields_in) - (set(spec) - {"target", "t_end"})
        if extra:
            errors.append(f"{where}: undeclared field(s) {sorted(extra)}")
        for k, typ in spec.items():
            if k in ("target", "t_end", "replies_to"):
                continue
            _type_ok(fields_in.get(k), typ, f"{where}.{k}", errors)
        if r.type == "wait":
            rt = r.fields.get("replies_to")
            if rt is not None and not (isinstance(rt, list) and all(isinstance(x, dict) and len(x) == 1 and next(iter(x)) in ("item", "run")
                                                                     and isinstance(next(iter(x.values())), str) for x in rt)):
                errors.append(f"{where}: replies_to is a list of references by item or run")
            elif isinstance(fields_in.get("causes"), list) and "verdict_reply" in fields_in["causes"] and not (rt if rt is not None else r.ref):
                errors.append(f"{where}: a wait for the policy's reply names the outputs it replies to")
        if r.ref is None:
            if r.type in RUNNER_NEEDS_REF:
                errors.append(f"{where}: needs a reference")
            continue
        if "target" not in spec:
            errors.append(f"{where}: takes no reference")
        elif not isinstance(r.ref, dict) or len(r.ref) != 1 or not isinstance(next(iter(r.ref.values())), str):
            errors.append(f"{where}: a reference is one {{kind: id}}")
        elif next(iter(r.ref)) not in RUNNER_REFS.get(r.type, ()):
            errors.append(f"{where}: a reference by {next(iter(r.ref))} is not taken; one of {RUNNER_REFS.get(r.type, ())}")
    for k, v in hints.user_kinds.items():
        if not isinstance(k, str) or v not in F.USER_KINDS:
            errors.append(f"user kind {k!r}: {v!r} is not one of {F.USER_KINDS}")
    for k, v in hints.answers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            errors.append(f"answer {k!r}: an answer is text keyed by a request or item id")
    for n, x in enumerate(hints.excluded):
        if not (isinstance(x, dict) and set(x) == {"start_utc", "end_utc", "reason"}):
            errors.append(f"excluded interval {n}: {{start_utc, end_utc, reason}}")
            continue
        try:
            a, b = _ts(x["start_utc"]), _ts(x["end_utc"])
            if a is None or b is None or b <= a:
                errors.append(f"excluded interval {n}: a positive length")
        except (ValueError, TypeError, AttributeError):
            errors.append(f"excluded interval {n}: times are ISO 8601")
        from .clock import EXCLUDED_REASONS

        if x["reason"] not in EXCLUDED_REASONS:
            errors.append(f"excluded interval {n}: reason one of {EXCLUDED_REASONS}")
    spans = []
    for x in hints.excluded:
        try:
            a, b = _ts(x["start_utc"]), _ts(x["end_utc"])
            if a is not None and b is not None and b > a:
                spans.append((a, b))
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
    spans.sort()
    for (a0, b0), (a1, b1) in zip(spans, spans[1:]):
        if a1 < b0:
            errors.append(f"excluded intervals overlap: {_iso(a0)} to {_iso(b0)} and {_iso(a1)} to {_iso(b1)} (m3 of round 11)")
    if hints.episode_start_utc is not None:
        try:
            if _ts(hints.episode_start_utc) is None:
                errors.append("episode_start_utc: the opening prompt's dispatch, ISO 8601 with a zone")
        except (ValueError, TypeError, AttributeError):
            errors.append("episode_start_utc: the opening prompt's dispatch, ISO 8601 with a zone")
    for pid, t in (probe_targets or {}).items():
        where = f"probe target {pid}"
        if not isinstance(t, dict) or not {"about", "ref"} <= set(t) or set(t) - {"about", "ref", "deferred", "event", "coincides"}:
            errors.append(f"{where}: {{about, ref, deferred, event}} (coincides, if given, is ignored)")
            continue
        if not isinstance(t.get("event"), str):
            errors.append(f"{where}: event, the planted event's name")
        if t.get("deferred") is not None and not isinstance(t["deferred"], bool):
            errors.append(f"{where}: deferred is a boolean")
        refs = PROBE_TARGET_REFS.get(t["about"]) if isinstance(t["about"], str) else None
        if refs is None:
            errors.append(f"{where}: about one of {sorted(PROBE_TARGET_REFS)}")
        elif not isinstance(t["ref"], dict) or len(t["ref"]) != 1 or next(iter(t["ref"])) not in refs or not isinstance(next(iter(t["ref"].values())), str):
            errors.append(f"{where}: a {t['about']} is named by one of {refs}")
        elif "utc" in t["ref"]:
            try:
                if _ts(t["ref"]["utc"]) is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                errors.append(f"{where}: utc is an ISO 8601 instant with a zone")
    return errors


@dataclass
class Result:
    trajectory: Trajectory
    manifest: dict[str, Any]


def _first(d: dict[str, Any], *keys: Any) -> Any:
    """The value of the first key present in d (a present empty string counts)."""
    return next((d[k] for k in keys if k is not None and k in d), None)


def _place_unmapped(events: list[dict[str, Any]]) -> None:
    """An `unknown` event takes the place of its run's nearest event in time
    (just after it), so an item no mapping covers can never break ordering
    or turn structure. One with no run, or alone in it, keeps its own time."""
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("_run") and e["type"] != "unknown":
            by_run.setdefault(e["_run"], []).append(e)
    for e in events:
        if e["type"] == "unknown" and by_run.get(e.get("_run")):
            e["_anchor"] = min(by_run[e["_run"]], key=lambda x: (abs((x["_ts"] - e["_ts"]).total_seconds()), x["_ts"], x["_order"]))
            e["_ts"] = e["_anchor"]["_ts"]


def _sort_key(e: dict[str, Any]) -> tuple[Any, ...]:
    # The runner's episode_end comes after everything else at its instant.
    a = e.get("_anchor")
    if a is not None:
        e["_ts"] = a["_ts"]  # the anchor may have been re-dated (a delivered correction)
        return (a["_ts"], a["type"] == "episode_end", a["_order"], 1, e["_order"])
    return (e["_ts"], e["type"] == "episode_end", e["_order"], 0, e["_order"])


def _ts(s: str | datetime | None) -> datetime | None:
    if not s:
        return None
    t = s if isinstance(s, datetime) else datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time {s!r} has no zone")
    return t


def _iso_us(t: datetime) -> str:
    return t.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"... ({len(s)} chars)"


def ws_relative(path: str | None, ws: str) -> str | None:
    """A workspace path relative to the root ('.' for the root itself); a
    path outside the workspace unchanged."""
    if not isinstance(path, str) or not path:
        return path
    ws = ws.rstrip("/")
    if path == ws:
        return "."
    if path.startswith(ws + "/"):
        rel = posixpath.normpath(path[len(ws) + 1 :])
        return rel if not rel.startswith("..") else path
    if path.startswith("/") or path.startswith("~"):
        return path
    rel = posixpath.normpath(path)
    return rel


class _Builder:
    def __init__(self, profile: Profile, hints: Hints) -> None:
        self.p = profile
        self.h = hints
        self.ws = hints.workspace.rstrip("/")
        self.events: list[dict[str, Any]] = []
        self.order = 0
        self.segment = 0
        self.counter = {"turn": 0, "call": 0, "question": 0, "agent": 0}
        self.open_turn: dict[int, int] = {}
        self.turn_events: dict[int, list[dict[str, Any]]] = {}
        self.turn_end_kind: dict[int, str] = {}
        self.agent0_user_seen = False
        self.segment_user_seen = False
        self.call_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.by_item: dict[str, dict[str, Any]] = {}
        self.user_by_message: dict[str, dict[str, Any]] = {}
        self.native_user_time: dict[str, datetime] = {}
        self.visited: set[str] = set()
        self.m: dict[str, Any] = {
            "segments": {}, "agents": {}, "turns": {}, "calls": {}, "questions": {}, "items": {},
            "withheld": [], "decorations": [], "dropped": [], "notes": [], "output_sources": {}, "crosscheck": [], "steering": [], "runner_audit": [],
        }
        # The runner's declarations, read before anything is built.
        self.reset_refs: set[str] = set()
        self.reopen_refs: set[str] = set()
        self.delivered: dict[str, datetime] = {}
        self.fill_content: dict[str, str] = {}
        for r in hints.runner:
            ref = next(iter((r.ref or {}).values()), None)
            if r.type == "controlled_reset" and ref:
                self.reset_refs.add(ref)
            if r.type == "reopen" and ref:
                self.reopen_refs.add(ref)
            if r.type in DELIVERY_KINDS and r.ref and not set(r.ref) <= {"item", "message"}:
                raise NormaliseError(f"{r.type} names an item or a message")
            if r.type in DELIVERY_KINDS and ref:
                self.delivered[ref] = _ts(r.utc)  # a message id or an item id

    # -- event plumbing ------------------------------------------------------

    def ev(self, etype: str, agent: int, ts: datetime | None, item: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        if ts is None:
            raise NormaliseError(f"{etype}: no time on {item.get('id') if item else 'record'}")
        e = {"type": etype, "agent": agent, "segment": self.segment, **fields, "_ts": ts, "_order": self.order}
        self.order += 1
        self.events.append(e)
        if item is not None:
            self.m["items"][e["_order"]] = item["id"]
            self.by_item.setdefault(item["id"], e)
            e["_run"] = item.get("runId")
        if fields.get("turn") is not None:
            self.turn_events.setdefault(fields["turn"], []).append(e)
        return e

    def new_id(self, kind: str) -> int:
        self.counter[kind] += 1
        return self.counter[kind]

    def note(self, e: dict[str, Any] | None, field_: str, value: Any, key: str = "withheld", **extra: Any) -> None:
        self.m[key].append({"_order": e["_order"] if e else None, "field": field_, "value": value, **extra})

    def new_segment(self, ts: datetime, thread: Thread, run: dict[str, Any] | None, cause: str) -> None:
        if 0 in self.open_turn:
            raise NormaliseError("a segment boundary inside an open turn")
        self.segment += 1
        self.segment_user_seen = False
        self.ev("segment_start", 0, ts, cause=cause)
        self.m["segments"][self.segment] = {"thread": thread.id, "run": run["id"] if run else None, "source": thread.source, "cause": cause}

    def start_turn(self, agent: int, ts: datetime, run: dict[str, Any] | None) -> int:
        turn = self.new_id("turn")
        self.open_turn[agent] = turn
        self.ev("turn_start", agent, ts, turn=turn)
        self.m["turns"][turn] = {"runs": [run["id"]] if run else [], "agent": agent}
        return turn

    def end_turn(self, agent: int, ts: datetime, end: str) -> None:
        turn = self.open_turn.pop(agent)
        self.turn_end_kind[agent] = end
        last = max([ts] + [e["_ts"] for e in self.turn_events.get(turn, [])])
        self.ev("turn_end", agent, last, turn=turn, end=end)

    # -- threads ---------------------------------------------------------------

    def kind_of(self, item: dict[str, Any] | None, agent: int, prev_end: str | None, new_context: bool, declared: bool) -> str | None:
        if item is None:
            return None
        kind = self.h.user_kinds.get(item.get("messageId"))
        if kind is not None:
            return kind
        if agent != 0 or item.get("createdBy") not in (None, "user"):
            return "from_agent"
        if not self.agent0_user_seen:
            return "opening_prompt"
        if new_context and declared:
            return "reset"
        if item.get("inputIntent") == "steer" or (prev_end == "interrupted" and not new_context):
            return "correction"
        return "reply"

    def thread(self, seg: Segment, thr: Thread, agent: int, first_source: bool, new_source: bool) -> None:
        if thr.id in self.visited:
            raise NormaliseError(f"thread {thr.id} reached twice (a cycle or a repeated child)")
        self.visited.add(thr.id)
        self.m["agents"].setdefault(agent, {"thread": thr.id, "source": thr.source})
        fill = self.crosscheck(thr, agent, seg.sessions)
        by_run: dict[str | None, list[dict[str, Any]]] = {}
        for item in thr.items:
            by_run.setdefault(item.get("runId"), []).append(item)
        runs = list(thr.runs)
        known = {r["id"] for r in runs}
        orphans = [i for rid, items in by_run.items() if rid not in known for i in items]
        user_items = {i.get("messageId"): i for i in thr.items if i["type"] == "user_message"}
        requests = {r["id"]: r for r in thr.runtime_requests}

        plans = []
        prev_status = None
        for n, run in enumerate(runs):
            items = by_run.get(run["id"], [])
            um = user_items.get(run.get("userMessageId")) or next((i for i in items if i["type"] == "user_message"), None)
            fresh = bool(run.get("freshContext") or (um or {}).get("freshContext"))
            boundary = agent == 0 and (n == 0 and (first_source or new_source) or (n > 0 and fresh))
            declared = bool({run["id"], (um or {}).get("messageId"), (um or {}).get("id"), thr.id if n == 0 else None} & self.reset_refs)
            # A restart after an interrupt continues the interrupted turn: one
            # shape for both steering mechanisms.
            prev_end = RUN_END.get(prev_status) if prev_status else None
            continues = n > 0 and not boundary and prev_end == "interrupted" and um is not None and (
                self.h.user_kinds.get(um.get("messageId"), "correction" if um.get("createdBy") in (None, "user") else "from_agent") in F.DELIVERED_KINDS)
            plans.append((run, items, um, boundary, declared, continues))
            prev_status = run.get("status")
        # A trusted next-message delivery closes the preceding turn and opens
        # the next one. A late run-completion stamp alone can be reconciled;
        # actual recorded work after that delivery cannot.
        for n in range(1, len(plans)):
            run, items, um, boundary, declared, continues = plans[n]
            delivery = _first(self.delivered, (um or {}).get("messageId"), (um or {}).get("id"))
            previous = plans[n - 1]
            done = _ts(previous[0].get("completedAt"))
            if delivery is not None and not continues:
                stamps = [_ts(x.get(k)) for x in previous[1] for k in ("startedAt", "completedAt") if x.get(k)]
                start = _ts(previous[0].get("startedAt") or previous[0].get("requestedAt"))
                if (start is not None and start > delivery) or any(t > delivery for t in stamps if t is not None):
                    raise NormaliseError("irreconcilable clocks: previous run work extends past the trusted correction delivery " + _iso_us(delivery))
                if done is not None and delivery < done:
                    self.m["notes"].append(f"run {previous[0]['id']}: completion {_iso_us(done)} reconciled to trusted delivery {_iso_us(delivery)}")
                    plans[n - 1] = ({**previous[0], "completedAt": _iso_us(delivery)}, *previous[1:])
        prev_done: datetime | None = None
        for n, (run, items, um, boundary, declared, continues) in enumerate(plans):
            starts = [_ts(x) for x in [run.get("requestedAt"), run.get("startedAt")] + [i.get("startedAt") for i in items[:1]] if x]
            # A next-message correction opens this turn at the runner's
            # recorded delivery, before T3 may have stamped the dispatch.
            delivery = _first(self.delivered, (um or {}).get("messageId"), (um or {}).get("id"))
            ts0 = delivery if delivery is not None else (min(starts) if starts else None)
            # A queued run (a message T3 accepted while the previous run was
            # still going, such as a refusal queued after a declined secret
            # request) reaches the agent only once that run has completed:
            # its turn cannot start before then, whenever it was requested.
            if ts0 is not None and prev_done is not None and ts0 < prev_done:
                ts0 = prev_done
            prev_done = _ts(run.get("completedAt")) or prev_done
            if boundary:
                cause = "start" if (n == 0 and first_source) else ("reset" if declared else "context")
                self.new_segment(ts0, thr, run, cause)
            keep_open = n + 1 < len(plans) and plans[n + 1][5]
            self.run(seg, thr, run, items, agent, ts0, fill, requests, continues=continues, keep_open=keep_open, new_context=boundary and not (n == 0 and first_source), declared=declared)
        if orphans:
            self.m["notes"].append(f"agent {agent}: {len(orphans)} item(s) belong to no run of the thread; kept in a turn of their own")
            ts0 = _ts(orphans[0].get("startedAt") or orphans[0].get("updatedAt"))
            if agent == 0 and not plans and (first_source or new_source):
                self.new_segment(ts0, thr, None, "start" if first_source else "context")
            self.run(seg, thr, None, orphans, agent, ts0, fill, requests)

    def run(self, seg: Segment, thr: Thread, run: dict[str, Any] | None, items: list[dict[str, Any]], agent: int, ts0: datetime | None,
            fill: dict[str, str], requests: dict[str, Any], continues: bool = False, keep_open: bool = False, new_context: bool = False, declared: bool = False) -> None:
        if ts0 is None:
            raise NormaliseError(f"run {run and run['id']}: no time")
        if continues and agent in self.open_turn:
            self.m["turns"][self.open_turn[agent]]["runs"].append(run["id"])
        else:
            self.start_turn(agent, ts0, run)
        resume_at: datetime | None = None
        last_ts = ts0
        ctx = {"new_context": new_context, "declared": declared, "restart": continues, "run": run}
        for item in items:
            if agent not in self.open_turn:
                if resume_at is None:
                    self.m["notes"].append(f"item {item['id']} after an unanswered question; kept in a new turn")
                    resume_at = _ts(item.get("startedAt")) or last_ts
                self.start_turn(agent, resume_at, run)
                resume_at = None
            resume_at = self.item(seg, thr, item, agent, fill, requests, ctx) or resume_at
            last_ts = max([last_ts] + [t for t in (_ts(item.get("startedAt")), _ts(item.get("completedAt"))) if t])
        status = run["status"] if run else "running"
        if status in RUN_END:
            end = RUN_END[status]
        elif status in RUN_OPEN:
            end = "open"
        else:
            raise NormaliseError(f"run {run and run['id']}: unknown status {status!r}")
        # T3's completion time when it has one: a background child or a tool
        # result that arrives later does not keep the turn open.
        end_ts = (_ts(run.get("completedAt")) if run else None) or last_ts
        if agent not in self.open_turn:
            if resume_at is None:
                return  # the turn ended waiting on a question nobody answered
            self.start_turn(agent, resume_at, run)
        if keep_open:
            self.m["steering"].append({"_turn": self.open_turn[agent], "interrupted_run": run["id"] if run else None, "interrupted_utc": _iso(end_ts)})
            return
        self.end_turn(agent, end_ts, end)

    # -- items -----------------------------------------------------------------

    def item(self, seg: Segment, thr: Thread, item: dict[str, Any], agent: int, fill: dict[str, str], requests: dict[str, Any], ctx: dict[str, Any]) -> datetime | None:
        """Emit the events for one item. Returns the time a waiting turn resumes, if it does."""
        typ = item["type"]
        ts = _ts(item.get("startedAt")) or _ts(item.get("updatedAt"))
        turn = self.open_turn[agent]
        if typ == "user_message":
            self.user_message(item, agent, ts, turn, ctx)
        elif typ == "assistant_message":
            nid = (item.get("nativeItemRef") or {}).get("nativeId")
            if nid and any(nid in s.synthetic_ids for s in seg.sessions):
                e = self.ev("error", agent, ts, item, turn=turn, **{"class": "harness_message"})
                self.note(e, "text", item.get("text"))
            else:
                e = self.ev("assistant_message", agent, ts, item, turn=turn, role="narration", text=item.get("text") or "", user_channel=False, user_text=None,
                            t_end=None)
                # When the item finished: a final message reached the user then
                # (the driver's item delivery time, which the runner judged at).
                e["_ts_end"] = _ts(item.get("completedAt"))
        elif typ == "command_execution":
            if "workspace-preparation" in item["id"]:
                self.m["dropped"].append({"item": item["id"], "type": typ, "reason": "T3 workspace preparation step"})
                return None
            title = item.get("title")
            if title not in self.p.shell_titles:
                self.unknown(item, agent, ts, turn, f"command item title {title!r} is not this profile's shell tool")
                return None
            ta = self.p.tool_args("shell", {"command": item.get("input")})
            self.tool_call(item, agent, ts, turn, ta, title, "harness", self.output_text(item, fill), exit_code=item.get("exitCode"))
        elif typ == "file_change":
            self.file_change(item, agent, ts, turn)
        elif typ in ("dynamic_tool", "mcp_tool_call"):
            self.dynamic(item, agent, ts, turn, fill)
        elif typ in ("file_read", "file_search", "web_search"):
            # UNVERIFIED: T3 has these item types; no run produced one.
            tool = {"file_read": "file_read", "file_search": "search", "web_search": "web_search"}[typ]
            ta = self.p.tool_args(tool, item.get("input") if isinstance(item.get("input"), dict) else {})
            self.tool_call(item, agent, ts, turn, ta, item.get("title"), "harness", self.output_text(item, fill))
        elif typ == "subagent":
            self.subagent(seg, item, agent, ts, turn)
        elif typ in ("user_input_request", "secret_request"):
            return self.question(item, agent, ts, turn, requests)
        elif typ == "compaction":
            # Mapped from T3's source (orchestrationV2.ts, provider-pi adapter.ts):
            # status, the harness in `driver`, an optional summary and token counts.
            e = self.ev("compaction", agent, ts, item, turn=turn, status=self.call_status(item))
            for k in ("driver", "summary", "beforeTokenCount", "afterTokenCount", "title"):
                if item.get(k) is not None:
                    self.note(e, k, item.get(k))
        elif typ in T3_ONLY:
            self.m["dropped"].append({"item": item["id"], "type": typ, "reason": "T3 checkpoint record"})
        elif typ in T3_RECORDS:
            self.m["dropped"].append({"item": item["id"], "type": typ, "reason": T3_RECORDS[typ]})
        elif typ == "run_interrupt_request":
            # The driver's own interrupt request: audit only. Its turn is set
            # after sorting, from the turn open at its time.
            self.ev("interrupt_request", agent, _ts(item.get("startedAt")) or _ts(item.get("updatedAt")) or ts, item, turn=None)
        elif typ == "error":
            cls = (item.get("failure") or {}).get("class")
            e = self.ev("error", agent, ts, item, turn=turn, **{"class": cls if isinstance(cls, str) and CLASS_RE.match(cls) else "other"})
            self.note(e, "failure", item.get("failure"))
        else:
            self.unknown(item, agent, ts, turn, f"T3 item type {typ!r} has no mapping")
        return None

    def unknown(self, item: dict[str, Any], agent: int, ts: datetime | None, turn: int | None, reason: str) -> None:
        typ = re.sub(r"[^a-z0-9_]", "_", str(item.get("type", "")).lower())[:40] or "unnamed"
        e = self.ev("unknown", agent, ts or _ts(item.get("updatedAt")), item, turn=turn, item_type=typ)
        self.note(e, "item", item, reason=reason)

    def user_message(self, item: dict[str, Any], agent: int, ts: datetime, turn: int, ctx: dict[str, Any]) -> None:
        text = item.get("text") or ""
        removed = []
        for rx in (SENDER_NOTE, ROLE_PREFIX if agent != 0 else None):
            m = rx.match(text) if rx else None
            if m:
                removed.append(m.group(0))
                text = text[m.end():]
        first_in_context = ctx["new_context"] and not self.segment_user_seen and agent == 0
        prev_end = "interrupted" if ctx["restart"] else self.turn_end_kind.get(agent)
        kind = self.kind_of(item, agent, prev_end, first_in_context, ctx["declared"])
        run_first = (ctx["run"] or {}).get("userMessageId") == item.get("messageId")
        # A reopen named by message or item relabels that message. Named by run,
        # it relabels the run's first message only when that would otherwise be
        # a plain reply (never the opening prompt or a reset).
        if agent == 0 and item.get("messageId") not in self.h.user_kinds and (
                {item.get("messageId"), item["id"]} & self.reopen_refs or (run_first and kind == "reply" and (ctx["run"] or {}).get("id") in self.reopen_refs)):
            kind = "reopen"
        if kind not in F.USER_KINDS:
            raise NormaliseError(f"user kind {kind!r}")
        if agent == 0:
            self.agent0_user_seen = True
            self.segment_user_seen = True
        if kind in F.DELIVERED_KINDS:
            ts = self.correction_time(item, ts, ctx, kind)
        e = self.ev("user_message", agent, ts, item, turn=turn, kind=kind, text=text)
        e["_message"] = item.get("messageId")
        for r in removed:
            self.note(e, "text", None, key="decorations", removed=r)
        if item.get("messageId"):
            self.user_by_message[item["messageId"]] = e

    def correction_time(self, item: dict[str, Any], queued: datetime, ctx: dict[str, Any], kind: str = "correction") -> datetime:
        """When the agent received a correction (or a reset notice steered into
        the running turn, D-046): the runner's word, else the harness
        session's record of the message, else (after an interrupt) the
        restarted run's start, else the time it was queued."""
        mid = item.get("messageId")
        run = ctx["run"] or {}
        done = _ts(item.get("completedAt"))
        if mid in self.delivered or item["id"] in self.delivered:
            t, src = self.delivered.get(mid) or self.delivered[item["id"]], "runner"
        elif item.get("inputIntent") == "steer" and done and done > queued:
            t, src = done, "T3 item completion"  # T3 completes a steer item when the harness takes it
        elif item["id"] in self.native_user_time:
            t, src = self.native_user_time[item["id"]], "harness session"
        elif ctx["restart"] and _ts(run.get("startedAt")):
            t, src = _ts(run.get("startedAt")), "restarted run start"
        else:
            t, src = queued, "unknown: queue time used"
        # The runner and watcher name one delivery instant. T3 may record
        # the message later, after dispatch, and must not move that instant.
        if src != "runner":
            t = max(t, queued)
        self.m["steering"].append({"_message": item["id"], "mechanism": "restart" if ctx["restart"] else ("native" if item.get("inputIntent") == "steer" else "other"),
                                   "queued_utc": _iso(queued), "delivered_utc": _iso(t), "delivery_source": src, "kind": kind})
        return t

    def output_text(self, item: dict[str, Any], fill: dict[str, str]) -> tuple[str | None, bool]:
        """(text, available). From T3's record; from the harness session when T3 omitted it."""
        if "output" in item and not item.get("outputOmitted"):
            self.m["output_sources"][item["id"]] = item.get("_output_source", "t3")
            out = item.get("output")
            if isinstance(out, dict) and isinstance(out.get("content"), list):
                other = [c for c in out["content"] if not (isinstance(c, dict) and c.get("type") == "text")]
                if other:
                    # Not text (an image, for one): kept in the manifest, never dropped silently.
                    self.m["withheld"].append({"_order": self.order, "field": "output.non_text", "value": other})
                    self.m["notes"].append(f"item {item['id']}: {len(other)} non-text output part(s) withheld")
            return tool_text(out), True
        if item["id"] in fill:
            self.m["output_sources"][item["id"]] = "harness session"
            return fill[item["id"]], True
        self.m["output_sources"][item["id"]] = "none"
        return None, False

    def tool_call(self, item: dict[str, Any], agent: int, ts: datetime, turn: int, ta: ToolArgs, native_name: str | None, origin: str,
                  out: tuple[str | None, bool], exit_code: Any = None, raw_output: Any = None) -> dict[str, Any]:
        tool = ta.tool
        cls = F.TOOLS[tool]
        text, available = out
        detail = None
        removed: list[str] = []
        withhold_output = tool in ("tool_discovery", "harness_other")
        if not withhold_output and origin == "harness" and available:
            c = self.p.clean_output(tool, text)
            text, detail, removed = c.text, c.truncated, c.removed
            text, dsuf = _strip_driver_suffix(text)
            if dsuf:
                # The driver cuts the end for transport, after any harness cut.
                prior = (detail or {}).get("edge", "none")
                edge = {"none": "tail", "tail": "tail", "head": "both", "both": "both", "middle": "middle"}.get(prior, "tail")
                # T3 cuts at a byte and appends "\n… output truncated for transport":
                # the kept text ends with a line break exactly when its last line is whole.
                detail, removed = {"by": "driver", "lines_omitted": None, "edge": edge, "last_line_whole": isinstance(text, str) and text.endswith("\n")}, removed + [dsuf]
            if tool == "shell" and isinstance(text, str) and text.endswith("\n"):
                text = text[:-1]  # one final newline: Pi's truncated output has none, its full output has one
        args = dict(ta.args)
        for k in F.PATH_FIELDS:
            if k in args:
                args[k] = ws_relative(args[k], self.ws)
        e = self.ev(
            "tool_call", agent, ts, item, turn=turn, call=self.new_id("call"), tool=tool, input=args,
            output={"text": None if withhold_output else text, "available": bool(available and not withhold_output and text is not None), "truncated": detail is not None,
                    "clipped": (detail.get("edge") or "middle") if detail is not None else "none",
                    "last_line_whole": bool(detail.get("last_line_whole")) if detail is not None else True,
                    "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None},
            status=self.call_status(item), t_end=None, user_channel=False, user_text=None, question=None, subagent=None,
            **{"class": cls},
        )
        e["_ts_end"] = _ts(item.get("completedAt"))
        if withhold_output and (text is not None or raw_output is not None):
            self.note(e, "output", raw_output if raw_output is not None else text)
        for k, v in ta.withheld.items():
            self.note(e, f"input.{k}", v)
        for r in removed:
            self.note(e, "output.text", None, key="decorations", removed=r, truncation=detail)
        nref = item.get("nativeItemRef") or {}
        self.m["calls"][e["_order"]] = {"item": item["id"], "native_id": nref.get("nativeId"), "native_name": native_name, "origin": origin}
        self.call_items.append((item, e))
        return e

    def call_status(self, item: dict[str, Any]) -> str:
        st = item.get("status")
        if st in ITEM_STATUS:
            return ITEM_STATUS[st]
        if st in ITEM_OPEN:
            return "open"
        raise NormaliseError(f"item {item['id']}: unknown status {st!r}")

    def file_change(self, item: dict[str, Any], agent: int, ts: datetime, turn: int) -> None:
        title = item.get("title")
        tool = self.p.file_change_titles.get(title)
        if tool is None:
            self.unknown(item, agent, ts, turn, f"file change title {title!r} is not one of this profile's file tools")
            return
        cut = []
        fields = {}
        if item.get("newStr") is None and item.get("diffStr") is None and item["id"] in self.fill_content:
            item = {**item, "newStr": self.fill_content[item["id"]]}
            self.m["output_sources"][item["id"]] = "harness session (file content)"
        for k in ("newStr", "oldStr", "diffStr"):
            v = item.get(k)
            if isinstance(v, str):
                m = DRIVER_SUFFIX.search(v)
                if m:
                    cut.append((k, m.group(0)))
                    v = v[: m.start()]
            fields[k] = v
        path = item.get("fileName") or item.get("path") or item.get("filePath")
        if tool == "file_write":
            args = {"path": path, "content": fields["newStr"], "content_truncated": bool(cut)}
        else:
            args = {"path": path, "old_text": fields["oldStr"], "new_text": fields["newStr"], "diff": fields["diffStr"], "content_truncated": bool(cut)}
        known = {"fileName", "newStr", "oldStr", "diffStr", "path", "filePath"}
        base = {"id", "threadId", "runId", "nodeId", "providerThreadId", "providerTurnId", "nativeItemRef", "parentItemId", "ordinal", "status", "title", "startedAt", "completedAt", "updatedAt", "type", "outputOmitted"}
        extra = {k: v for k, v in item.items() if k not in known | base}
        # T3 does not keep a file change's result message; output is unavailable.
        e = self.tool_call(item, agent, ts, turn, ToolArgs(tool, args, extra), title, "harness", (None, False))
        for k, suffix in cut:
            self.note(e, f"input.{k}", None, key="decorations", removed=suffix, truncation={"by": "driver", "lines_omitted": None})

    def dynamic(self, item: dict[str, Any], agent: int, ts: datetime, turn: int, fill: dict[str, str]) -> None:
        name = item.get("toolName") or item.get("title") or ""
        raw_in = item.get("input")
        driver = next((name[len(p):] for p in F.DRIVER_PREFIXES if name.startswith(p)), None)
        if driver is not None:
            if driver not in F.DRIVER_TOOLS:
                self.unknown(item, agent, ts, turn, f"driver tool {driver!r} is not in the format's list")
                return
            self.driver_tool(item, agent, ts, turn, F.DRIVER_TOOLS[driver], name, raw_in, fill)
            return
        tool = self.p.harness_tool(name)
        if tool is None:
            self.unknown(item, agent, ts, turn, f"harness tool {name!r} is not in the profile")
            return
        ta = self.p.tool_args(tool, raw_in)
        out = self.output_text(item, fill)
        self.tool_call(item, agent, ts, turn, ta, name, "harness", out, raw_output=item.get("output"))

    def driver_tool(self, item: dict[str, Any], agent: int, ts: datetime, turn: int, tool: str, native_name: str, raw_in: Any, fill: dict[str, str]) -> None:
        a = raw_in if isinstance(raw_in, dict) else {}
        if tool == "subagent":  # delegate_task
            args = {"title": a.get("title") if isinstance(a.get("title"), str) else None, "prompt": a.get("task") if isinstance(a.get("task"), str) else None}
            withheld = {k: v for k, v in a.items() if k not in ("title", "task")}
        elif F.input_fields(tool) is F.RAW_INPUT:
            args, withheld = {"raw": F.canonical(raw_in) if raw_in is not None else None}, {}
        else:
            spec = F.input_fields(tool)
            args = {}
            for k in spec:
                if k == "fields":
                    args[k] = [{"name": f.get("name"), "label": f.get("label"), "secret": bool(f.get("secret"))} for f in a.get("fields") or [] if isinstance(f, dict)]
                else:
                    v = a.get(k)
                    args[k] = v if v is None or isinstance(v, str) else F.canonical(v)
            withheld = {k: v for k, v in a.items() if k not in spec}
            if tool == "request_secret":
                extra = [{k: v for k, v in f.items() if k not in ("name", "label", "secret")} for f in a.get("fields") or [] if isinstance(f, dict)]
                if any(extra):
                    withheld["fields.extra"] = extra
        raw_out = item.get("output")
        text, available = self.output_text(item, fill)
        structured = None
        if isinstance(raw_out, dict) and isinstance(raw_out.get("structuredContent"), dict):
            structured = raw_out["structuredContent"]
        elif isinstance(raw_out, dict) and "content" not in raw_out:
            structured = raw_out  # the HTTP snapshot's summary of an omitted output
        elif text:
            try:
                parsed = json.loads(text)
                structured = parsed if isinstance(parsed, dict) else None
            except ValueError:
                structured = None
        out_text: str | None = None
        if tool == "request_secret" and structured is not None and "status" in structured:
            # Status and delivered file paths only. A secret value never enters the trajectory.
            out_text = F.canonical({k: structured[k] for k in ("status", "files") if k in structured})
        e = self.tool_call(item, agent, ts, turn, ToolArgs(tool, args, withheld), native_name, "driver", (out_text, out_text is not None))
        if raw_out is not None or text is not None:
            self.note(e, "output", raw_out if raw_out is not None else text)
        if tool == "send_to_user":
            sent = (structured or {}).get("sentMessage")
            if isinstance(sent, str) and e["status"] == "ok":
                e["user_channel"] = True
                e["user_text"] = sent

    def subagent(self, seg: Segment, item: dict[str, Any], agent: int, ts: datetime, turn: int) -> None:
        sub = self.new_id("agent")
        child_id = item.get("childThreadId")
        cands = [(i, e) for i, e in self.call_items if e["class"] == "subagent" and e["subagent"] is None and e["agent"] == agent and i.get("providerTurnId") == item.get("providerTurnId")]
        named = [(i, e) for i, e in cands if child_id and child_id in json.dumps(i.get("output"))]
        link = (named or cands or [(None, None)])[-1][1]
        if link is not None:
            link["subagent"] = sub
        prompt = item.get("prompt")
        removed = None
        if isinstance(prompt, str):
            m = ROLE_PREFIX.match(prompt)
            if m:
                removed, prompt = m.group(0), prompt[m.end():]
        start = self.ev("subagent_start", agent, ts, item, turn=turn, subagent=sub, call=link["call"] if link else None, title=item.get("title"), prompt=prompt)
        if removed:
            self.note(start, "prompt", None, key="decorations", removed=removed)
        self.m["agents"][sub] = {"subagent_item": item["id"], "thread": child_id, "native_task": item.get("nativeTaskRef"), "origin": "driver" if link and self.m["calls"][link["_order"]]["origin"] == "driver" else "harness"}
        before = len(self.events)
        child = seg.threads.get(child_id) if child_id else None
        if child is not None:
            self.thread(seg, child, sub, first_source=False, new_source=False)
            if sub in self.open_turn:
                raise NormaliseError(f"subagent {sub}: turn left open")
        else:
            self.m["notes"].append(f"subagent {sub}: child thread {child_id!r} is not in the source; no child events")
        child_events = self.events[before:]
        for ce in child_events:
            if ce["_ts"] < start["_ts"]:
                ce["_ts"] = start["_ts"]
        last_child = max((ce["_ts"] for ce in child_events), default=start["_ts"])
        st = item.get("status")
        if st in ITEM_OPEN:
            self.m["notes"].append(f"subagent {sub} still running at export; no subagent_end")
            return
        status = ITEM_STATUS.get(st)
        if status is None:
            raise NormaliseError(f"subagent item {item['id']}: unknown status {st!r}")
        self.ev("subagent_end", agent, max(_ts(item.get("completedAt")) or last_child, last_child), item, subagent=sub, status=status, result=item.get("result"))

    def question(self, item: dict[str, Any], agent: int, ts: datetime, turn: int, requests: dict[str, Any]) -> datetime | None:
        q = self.new_id("question")
        prompts = secret = None
        if item["type"] == "user_input_request":
            kind = "question"
            prompts = []
            for x in item.get("questions") or []:
                h = x.get("header")
                prompts.append({"header": h if isinstance(h, str) else None, "text": x.get("question") or "",
                                "options": [{"label": o["label"], "description": o.get("description") if isinstance(o.get("description"), str) else None}
                                            for o in x.get("options") or [] if isinstance(o, dict) and isinstance(o.get("label"), str)],
                                "multi_select": bool(x.get("multiSelect"))})
            user_text = render_prompts(prompts)
            texts = [x["text"] for x in prompts if x["text"]]
            cands = [(i, e) for i, e in self.call_items if e["tool"] == "ask_user" and e["question"] is None and i.get("providerTurnId") == item.get("providerTurnId")
                     and all(json.dumps(t)[1:-1] in json.dumps(i.get("input"), ensure_ascii=True) or t in json.dumps(i.get("input"), ensure_ascii=False) for t in texts)]
        else:
            kind = "secret"
            secret = {"label": item.get("label"), "reason": item.get("reason"),
                      "fields": [{"label": f.get("label") or f.get("name"), "placeholder": f.get("placeholder"), "secret": bool(f.get("secret"))} for f in item.get("fields") or [] if isinstance(f, dict)]}
            user_text = render_secret(secret)
            cands = [(i, e) for i, e in self.call_items if e["tool"] == "request_secret" and e["question"] is None and (i.get("input") or {}).get("label") == item.get("label")
                     and i.get("providerTurnId") == item.get("providerTurnId")]
        link = cands[0][1] if len(cands) == 1 else None
        if len(cands) > 1:
            self.m["notes"].append(f"question {item['id']}: {len(cands)} candidate calls; not linked")
        if link is not None:
            link["question"] = q
        qe = self.ev("question", agent, ts, item, turn=turn, question=q, kind=kind, call=link["call"] if link else None, user_channel=True, user_text=user_text, prompts=prompts, secret=secret)
        req = requests.get(item.get("requestId")) or {}
        self.m["questions"][qe["_order"]] = {"item": item["id"], "request": item.get("requestId")}
        self.end_turn(agent, ts, "waiting")
        if kind == "secret":
            status = SECRET_STATUS.get(item.get("secretStatus"))
            if status is None:
                raise NormaliseError(f"secret request {item['id']}: unknown secretStatus {item.get('secretStatus')!r}")
            # A saved secret never has text. A refused one may carry the user's
            # fixed refusal words when the runner passes them (D-023).
            text = _first(self.h.answers, item["id"], item.get("requestId")) if status in ("declined", "cancelled") else None
        else:
            st = req.get("status") or item.get("status")
            status = {"resolved": "answered", "completed": "answered", "cancelled": "cancelled", "failed": "cancelled", "pending": "open", "waiting": "open"}.get(st)
            if status is None:
                raise NormaliseError(f"question {item['id']}: unknown status {st!r}")
            text = _first(self.h.answers, item.get("requestId"), item["id"])  # an empty string is a deliberate answer
            if text is not None:
                self.m["questions"][qe["_order"]]["answer_source"] = "runner"
            elif status == "answered" and link is not None and link["output"]["available"]:
                text = link["output"]["text"]
                self.m["questions"][qe["_order"]]["answer_source"] = "the question tool's output"
            if status != "answered":
                text = None
        missing = kind == "question" and status == "answered" and text is None
        if missing and not self.h.audit_only:
            raise NormaliseError(f"question {item['id']}: answered, but neither the runner nor the question tool's output gives the answer "
                                 "(pass Hints.answers, or the harness session; Hints.audit_only marks it missing)")
        if status == "open":
            return None
        ans_ts = max(_ts(req.get("resolvedAt")) or _ts(item.get("completedAt")) or ts, ts)
        # The runner dated the delivery of what it sent as the answer (a
        # correction or a reopen merged into it): the answer and the resumed
        # turn take that time, never before the question.
        given = _first(self.delivered, item["id"], item.get("requestId"))
        if given is not None:
            ans_ts = max(given, ts)  # and no later than the resumed work's first item (normalise clamps it)
        akind = self.h.user_kinds.get(item.get("requestId") or "") or self.h.user_kinds.get(item["id"]) or "reply"
        if akind not in F.ANSWER_KINDS:
            raise NormaliseError(f"question {item['id']}: an answer counts as one of {F.ANSWER_KINDS}, not {akind!r}")
        if agent == 0 and ({item.get("requestId"), item["id"]} & self.reopen_refs):
            akind = "reopen"
        ae = self.ev("answer", agent, ans_ts, None, question=q, status=status, text=text, missing=missing, kind=akind)
        ae["_run"] = item.get("runId")
        ae["_q_ts"] = ts
        ae["_runner_dated"] = given is not None
        ae["_request"] = item.get("requestId")
        ae["_question_item"] = item["id"]
        if missing:
            self.m["notes"].append(f"question {item['id']}: answer text missing (audit only)")
        return ans_ts

    # -- runner events --------------------------------------------------------

    def runner_events(self) -> None:
        for r in self.h.runner:
            if r.type not in F.RUNNER_FIELDS:
                raise NormaliseError(f"runner event type {r.type!r}")
            ts = _ts(r.utc)
            if ts is None:
                raise NormaliseError(f"runner event {r.type}: no time")
            spec = F.RUNNER_INPUT_FIELDS[r.type]
            given = r.fields
            if r.type == "wait":
                given, legacy = wait_fields(r.fields)
                if legacy:
                    self.m.setdefault("legacy_wait_cause", 0)
                    self.m["legacy_wait_cause"] += 1
            extra = set(given) - set(spec)
            if extra:
                raise NormaliseError(f"runner event {r.type}: undeclared field(s) {sorted(extra)}")
            if r.ref and "target" not in spec:
                raise NormaliseError(f"runner event {r.type} takes no reference")
            kept = F.RUNNER_EVENT_FIELDS[r.type]
            fields = {k: given.get(k) for k in kept if k not in ("target", "t_end", "replies_to")}
            if "target" in spec:
                fields["target"] = None
            if "t_end" in spec:
                fields["t_end"] = None
            if "replies_to" in spec:
                fields["replies_to"] = None
            e = self.ev(r.type, 0, ts, None, **fields)
            e["_runner"] = True
            e["_ref"] = r.ref
            e["_ts_end"] = _ts(r.end_utc)
            if "replies_to" in spec:
                e["_replies_refs"] = r.fields.get("replies_to")
            for k in {**F.RUNNER_AUDIT_FIELDS.get(r.type, {}), **F.RUNNER_AUDIT_PENDING.get(r.type, {})}:
                if given.get(k) is not None:
                    self.note(e, k, given[k], key="runner_audit")  # audit only: never in the trajectory or the bundle

    def resolve(self, e: dict[str, Any], ordered: list[dict[str, Any]], turn_end_ts: dict[int, datetime]) -> dict[str, Any] | None:
        """The event a runner event is about, by purpose. A verdict or a wait:
        by item, the user-channel event made from it; by run, the last
        user-channel output of that raw run delivered at or before the runner
        event. A correction delivery: the correction, by item or message. A
        reset or a reopen: the first user message of the run, message, item or
        thread named."""
        ref = e.get("_ref")
        if not ref:
            if e["type"] in RUNNER_NEEDS_REF:
                raise NormaliseError(f"runner event {e['type']} needs a reference")
            return None
        if not isinstance(ref, dict) or len(ref) != 1:
            raise NormaliseError(f"runner event {e['type']}: a reference is one of item, run, message, thread")
        kind, value = next(iter(ref.items()))

        def delivered(x: dict[str, Any]) -> datetime:
            # The one delivery rule (format.delivered_at), as the view applies it.
            return F.delivered_at(x["type"], x["_ts"], x.get("_ts_end"), turn_end_ts.get(x.get("turn")))

        by_item = [x for x in ordered if self.m["items"].get(x["_order"]) == value] if kind == "item" else []
        if e["type"] in ("verdict", "wait", "unjudged", "handback"):
            if kind == "item":
                target = next((x for x in by_item if x.get("user_channel")), None)
                if target is None:
                    raise NormaliseError(f"runner event {e['type']}: item {value} is not user-channel output")
                if delivered(target) > e["_ts"]:
                    raise NormaliseError(f"runner event {e['type']} at {_iso(e['_ts'])}: item {value} reached the user only at {_iso(delivered(target))}")
                return target
            if kind != "run":
                raise NormaliseError(f"runner event {e['type']}: reference by {kind} is not supported; name an item or a run")
            cands = sorted((x for x in ordered if x.get("_run") == value and x.get("user_channel") and delivered(x) <= e["_ts"]), key=lambda x: (delivered(x), x["_order"]))
            if not cands:
                raise NormaliseError(f"runner event {e['type']}: run {value} delivered no output to the user by {_iso(e['_ts'])}")
            return cands[-1]
        want = {**DELIVERY_KINDS, "controlled_reset": "reset", "reopen": "reopen"}[e["type"]]
        if e["type"] in DELIVERY_KINDS and kind not in ("item", "message"):
            raise NormaliseError(f"{e['type']} names an item or a message")
        if kind == "item":
            cands = [x for x in by_item if x["type"] == "user_message"] or [x for x in ordered if x["type"] == "answer" and value in (x.get("_request"), x.get("_question_item"))]
        elif kind == "message":
            cands = [x for x in ordered if x["type"] == "user_message" and x.get("_message") == value]
        elif kind == "run":
            # The runner names a run when it has no message id. A reset is the
            # run's first user message; a reopen or a correction is the user
            # message or merged answer of that run with that kind, the latest
            # at or before the runner event (the earliest after it if none).
            in_run = [x for x in ordered if x["type"] in ("user_message", "answer") and x.get("_run") == value and x["agent"] == 0]
            if e["type"] == "controlled_reset":
                cands = [x for x in in_run if x["type"] == "user_message"]
            else:
                same = [x for x in in_run if x.get("kind") == want]
                before = [x for x in same if x["_ts"] <= e["_ts"]]
                cands = before[-1:] or same[:1] or in_run[:1]
        elif kind == "thread":
            seg = next((k for k, v in self.m["segments"].items() if v["thread"] == value), None)
            cands = [x for x in ordered if x["type"] == "user_message" and x["agent"] == 0 and x["segment"] == seg]
        else:
            cands = []
        target = cands[0] if cands else None
        if target is None:
            raise NormaliseError(f"runner event {e['type']}: nothing in the episode matches {ref}")
        if target["kind"] != want:
            raise NormaliseError(f"runner event {e['type']}: {ref} names a {target['kind']} message, not a {want}")
        return target

    # -- native cross-check ---------------------------------------------------

    def crosscheck(self, thr: Thread, agent: int, sessions: list[NativeSession]) -> dict[str, str]:
        """Compare this thread's items with the harness session(s) its runs
        name. Returns item id -> native output text for items whose output
        T3 omitted, and records when the harness received each user message.
        Findings go to the manifest and never change the trajectory."""
        fill: dict[str, str] = {}
        if not sessions:
            return fill
        find = self.m["crosscheck"]
        groups: dict[str, tuple[NativeSession, list[str]]] = {}
        for run in thr.runs:
            nids = [a.get("nativeThreadId") for a in thr.attempts if a.get("runId") == run["id"] and a.get("nativeThreadId")]
            s = next((self.p.session_for(sessions, n) for n in nids if self.p.session_for(sessions, n)), None)
            if s is None:
                find.append({"agent": agent, "run": run["id"], "finding": "no harness session for the run", "native": nids})
                continue
            groups.setdefault(s.path, (s, []))[1].append(run["id"])
        for s, run_ids in groups.values():
            items = [i for i in thr.items if i.get("runId") in run_ids]
            user_items = [i for i in items if i["type"] == "user_message"]
            users = [i.get("text") or "" for i in user_items]
            native_users = [SENDER_NOTE.sub("", u, count=1) for u in s.user_texts]
            if users != native_users:
                find.append({"agent": agent, "session": s.path, "finding": "user messages differ", "t3": [_short(u) for u in users], "native": [_short(u) for u in native_users]})
            for k, (i, u) in enumerate(zip(user_items, users)):
                if k < len(native_users) and native_users[k] == u and k < len(s.user_times) and s.user_times[k]:
                    self.native_user_time[i["id"]] = _ts(s.user_times[k])
            asst = [i.get("text") or "" for i in items if i["type"] == "assistant_message" and (i.get("nativeItemRef") or {}).get("nativeId") not in s.synthetic_ids]
            if asst != s.assistant_texts:
                find.append({"agent": agent, "session": s.path, "finding": "assistant messages differ", "t3": len(asst), "native": len(s.assistant_texts),
                             "first_difference": next((k for k, (x, y) in enumerate(zip(asst, s.assistant_texts)) if x != y), min(len(asst), len(s.assistant_texts)))})
            seen: set[str] = set()
            for i in items:
                if i["type"] not in TOOL_ITEM_TYPES or "workspace-preparation" in i["id"]:
                    continue
                nid = (i.get("nativeItemRef") or {}).get("nativeId")
                call, result = s.calls.get(nid), s.results.get(nid)
                if call is None:
                    find.append({"agent": agent, "item": i["id"], "finding": "no native call for the item"})
                    continue
                seen.add(nid)
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                if i["type"] == "file_change":
                    path = args.get("path", args.get("file_path"))
                    content = args.get("content", args.get("newText", args.get("new_string")))
                    if i.get("fileName") != path:
                        find.append({"agent": agent, "item": i["id"], "finding": "file change path differs", "t3": i.get("fileName"), "native": path})
                    if content is not None and i.get("newStr") is not None and i.get("newStr") != content:
                        find.append({"agent": agent, "item": i["id"], "finding": "file change text differs", "t3_bytes": len((i.get("newStr") or "").encode()), "native_bytes": len(content.encode()),
                                     "t3_cut_by_driver": bool(DRIVER_SUFFIX.search(i.get("newStr") or ""))})
                    elif isinstance(content, str) and i.get("newStr") is None and i.get("diffStr") is None:
                        self.fill_content[i["id"]] = content  # the snapshot left the text out
                    continue
                want = args.get("command") if i["type"] == "command_execution" else args
                if i.get("input") != want:
                    find.append({"agent": agent, "item": i["id"], "finding": "tool input differs", "t3": _short(i.get("input")), "native": _short(want), "t3_output_omitted": bool(i.get("outputOmitted"))})
                if result is None:
                    find.append({"agent": agent, "item": i["id"], "finding": "no native result for the call"})
                    continue
                if "output" in i and not i.get("outputOmitted"):
                    got = tool_text(i.get("output"))
                    if got != result["text"]:
                        find.append({"agent": agent, "item": i["id"], "finding": "tool output differs", "t3_chars": len(got or ""), "native_chars": len(result["text"])})
                else:
                    fill[i["id"]] = result["text"]
            missing = [cid for cid in s.calls if cid not in seen]
            if missing:
                find.append({"agent": agent, "session": s.path, "finding": "native tool calls with no T3 item", "native": missing[:10]})
        return fill


def _strip_driver_suffix(text: str | None) -> tuple[str | None, str | None]:
    if isinstance(text, str):
        m = DRIVER_SUFFIX.search(text)
        if m:
            return text[: m.start()], m.group(0)
    return text, None


def render_prompts(prompts: list[dict[str, Any]]) -> str:
    """The text a user is shown for a question: header, question, one line per
    option with its description when it differs from the label."""
    blocks = []
    for p in prompts:
        lines = ([p["header"]] if p.get("header") else []) + [p["text"] + (" (choose one or more)" if p.get("multi_select") else "")]
        for o in p["options"]:
            d = o.get("description")
            lines.append(f"- {o['label']}" + (f": {d}" if d and d != o["label"] else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_secret(secret: dict[str, Any]) -> str:
    lines = [x for x in (secret.get("label"), secret.get("reason")) if x]
    for f in secret["fields"]:
        lines.append(f"- {f['label'] or ''}" + (f" ({f['placeholder']})" if f.get("placeholder") else ""))
    return "\n".join(lines)


def normalise(segments: list[Segment], profile: Profile, hints: Hints | None = None) -> Result:
    """Normalise one episode: its sources in order (one per T3 database, a
    handoff giving two), each with its harness sessions, plus the runner's
    events."""
    hints = hints or Hints()
    problems = check_runner_inputs(hints)
    if problems:
        raise NormaliseError("runner inputs: " + "; ".join(problems[:10]))
    b = _Builder(profile, hints)
    raw_items = 0
    for n, seg in enumerate(segments):
        main = seg.main_thread()
        raw_items += sum(len(t.items) for t in seg.threads.values())
        b.thread(seg, main, 0, first_source=n == 0, new_source=n > 0)
        unvisited = [tid for tid in seg.threads if tid not in b.visited]
        if unvisited:
            b.m["notes"].append(f"{len(unvisited)} thread(s) in the source are neither the main thread nor a subagent child; not normalised")
            b.m["unvisited_threads"] = b.m.get("unvisited_threads", []) + unvisited
    if b.open_turn:
        raise NormaliseError(f"turns left open: {b.open_turn}")
    if not b.events:
        raise NormaliseError("the source holds no runs and no items: nothing to normalise")
    # A runner-dated answer (and the turn it resumes) lies between its
    # question and the resumed work's first item.
    for ae in [e for e in b.events if e["type"] == "answer" and e.get("_runner_dated")]:
        resumed = next((x for x in b.events if x["type"] == "turn_start" and x["agent"] == ae["agent"] and x["_order"] > ae["_order"] and x["_ts"] == ae["_ts"]), None)
        after = resumed["_order"] if resumed is not None else ae["_order"]
        work = [x["_ts"] for x in b.events if x["agent"] == ae["agent"] and x["_order"] > after and x["type"] not in ("turn_start", "segment_start", "answer")]
        if work and min(work) < ae["_ts"]:
            ae["_ts"] = max(ae["_q_ts"], min(work))
            if resumed is not None:
                resumed["_ts"] = ae["_ts"]
    first_ts = min(e["_ts"] for e in b.events)
    b.runner_events()
    # D-035: an elapsed cutoff ends at its deadline, the runner's episode_end
    # time. The runner may notice the cutoff a little later (its poll); the
    # score view carries the cutoff at the deadline, and the instant noticed
    # goes to the audit record.
    end_ev = next((e for e in b.events if e.get("_runner") and e["type"] == "episode_end"), None)
    if end_ev is not None and end_ev.get("reason") == "budget_cutoff":
        for e in b.events:
            if e.get("_runner") and e["type"] == "budget_cutoff" and e.get("budget") == "time" and e["_ts"] > end_ev["_ts"]:
                b.note(e, "noticed_utc", _iso_us(e["_ts"]), key="runner_audit")
                e["_ts"] = end_ev["_ts"]
    # D-041: a probe never moves the end. A probe due at the episode's last
    # hand-back may have its copy taken after the end instant; it stays,
    # attached to that hand-back (dated at it), and the copy's own time goes
    # to the audit record. Any other event after the end is still outside.
    if end_ev is not None:
        last_hb = max((e for e in b.events if e.get("_runner") and e["type"] == "handback" and e["_ts"] <= end_ev["_ts"]),
                      key=lambda e: (e["_ts"], e["_order"]), default=None)
        for e in b.events:
            if e.get("_runner") and e["type"] == "probe" and e["_ts"] > end_ev["_ts"] and last_hb is not None:
                b.note(e, "copied_utc", _iso_us(e["_ts"]), key="runner_audit")
                # Right after its hand-back, whatever order the runner's events arrive in.
                e["_ts"] = last_hb["_ts"]
                e["_anchor"] = last_hb
    early = [e["type"] for e in b.events if e.get("_runner") and e["_ts"] < first_ts]
    if early:
        raise NormaliseError(f"runner event(s) before the episode's first event: {early}")

    _place_unmapped(b.events)
    ordered = sorted(b.events, key=_sort_key)

    # Segments after sorting: an event belongs to the segment current at its
    # place in the file (an async child finishing after a boundary included).
    segment = 0
    for e in ordered:
        if e["type"] == "segment_start":
            segment = e["segment"]
        e["segment"] = max(segment, 1)

    # Final messages: the last assistant message of a finished turn with no
    # tool call, question or subagent start after it in that turn.
    by_turn: dict[int, list[dict[str, Any]]] = {}
    ends: dict[int, str] = {}
    for e in ordered:
        if e["type"] == "turn_end":
            ends[e["turn"]] = e["end"]
        elif e.get("turn") is not None and e["type"] in ("assistant_message", "tool_call", "question", "subagent_start"):
            by_turn.setdefault(e["turn"], []).append(e)
    for turn, evs in by_turn.items():
        if ends.get(turn) == "finished" and evs and evs[-1]["type"] == "assistant_message":
            last = evs[-1]
            last["role"] = "final"
            if last["agent"] == 0:
                last["user_channel"] = True
                last["user_text"] = last["text"]

    # Runner targets, by purpose, now that the user channel is known.
    turn_end_ts = {e["turn"]: e["_ts"] for e in ordered if e["type"] == "turn_end"}
    for e in ordered:
        if e.get("_runner"):
            e["_target"] = b.resolve(e, ordered, turn_end_ts)
            if e.get("_replies_refs") is not None:
                # Every output whose verdict reply the wait carries, each by item or run.
                # M2 of round 11: a wait may reply to outputs delivered up to
                # its own end (a second request can arrive while the first
                # is pending; both are answered when the wait ends).
                upto = e.get("_ts_end") or max(x["_ts"] for x in ordered)
                e["_replies"] = [b.resolve({**e, "type": "verdict", "_ref": ref, "_ts": max(upto, e["_ts"])}, ordered, turn_end_ts) for ref in e["_replies_refs"]]
            if e["type"] in DELIVERY_KINDS and e["_target"] is not None:
                e["_ts"] = e["_target"]["_ts"]  # the message (or answer) is dated at delivery; the event agrees with it
    ordered.sort(key=_sort_key)

    # An interrupt request belongs to the turn its agent had open at its
    # place in the file, or none (a request during a wait on a question).
    open_at: dict[int, Any] = {}
    for e in ordered:
        if e["type"] == "turn_start":
            open_at[e["agent"]] = e["turn"]
        elif e["type"] == "turn_end" and open_at.get(e["agent"]) == e["turn"]:
            del open_at[e["agent"]]
        elif e["type"] == "interrupt_request":
            e["turn"] = open_at.get(e["agent"])

    # Local ids in file order.
    remap: dict[str, dict[int, int]] = {k: {} for k in ("turn", "call", "question", "agent")}
    remap["agent"][0] = 0
    for e in ordered:
        for key, kind, et in (("turn", "turn", "turn_start"), ("call", "call", "tool_call"), ("question", "question", "question"), ("subagent", "agent", "subagent_start")):
            if e["type"] == et:
                remap[kind].setdefault(e[key], len(remap[kind]) + (1 if kind != "agent" else 0))

    # The origin of every time (D-020): the driver's dispatch of the opening
    # prompt, as the runner counts; without it, T3's opening item (or the
    # earliest event when there is none), which cannot make a bundle with a
    # runner record. A run request before it has a negative time.
    opening = next((e for e in ordered if e["type"] == "user_message" and e["agent"] == 0 and e.get("kind") == "opening_prompt"), None)
    if hints.episode_start_utc is not None:
        t0, origin = _ts(hints.episode_start_utc), "dispatch"
    elif opening is not None:
        t0, origin = opening["_ts"], "opening_message"
    else:
        t0, origin = first_ts, "first_event"

    def us(t: datetime) -> int:
        d = t - t0
        return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds

    from .clock import to_ms

    seq_of = {e["_order"]: n for n, e in enumerate(ordered)}
    events: list[dict[str, Any]] = []
    for seq, e in enumerate(ordered):
        out = {k: v for k, v in e.items() if not k.startswith("_")}
        out["seq"] = seq
        out["us"] = us(e["_ts"])
        out["t"] = to_ms(out["us"])
        out["agent"] = remap["agent"][e["agent"]]
        if e.get("_runner"):
            if "target" in out:
                out["target"] = seq_of[e["_target"]["_order"]] if e.get("_target") else None
            if "t_end" in out:
                out["us_end"] = us(e["_ts_end"]) if e.get("_ts_end") else None
                out["t_end"] = to_ms(out["us_end"]) if out["us_end"] is not None else None
            if "replies_to" in out:
                out["replies_to"] = sorted({seq_of[x["_order"]] for x in e["_replies"]}) if e.get("_replies") is not None else None
        for key, kind in (("turn", "turn"), ("call", "call"), ("question", "question"), ("subagent", "agent")):
            if out.get(key) is not None and key in out:
                out[key] = remap[kind][out[key]]
        if out["type"] in ("tool_call", "assistant_message"):
            te = e.get("_ts_end")
            out["us_end"] = max(us(te), out["us"]) if te else None
            out["t_end"] = to_ms(out["us_end"]) if te else None
        events.append(out)

    from .clock import Clock, ClockError

    try:
        clock = Clock([{"us": us(_ts(x["start_utc"])), "us_end": us(_ts(x["end_utc"])), "reason": x["reason"]} for x in hints.excluded])
    except (KeyError, TypeError, ClockError) as exc:
        raise NormaliseError(f"excluded intervals: {exc}") from None
    for e in events:
        for key in ("us", "us_end"):
            if isinstance(e.get(key), int) and clock.inside(e[key]):
                raise NormaliseError(f"{e['type']} (seq {e['seq']}, item {m_items_get(b, e['seq'], seq_of)}) at {e[key]} us falls inside an excluded interval {clock.inside(e[key])}")
    # The post-run exchange, by reference: the run each post_run message
    # starts, its turn, and everything after the first one in file order.
    # Runner events are the runner's record of the episode and stay out of it.
    post_msgs = [e for e in ordered if e["type"] == "user_message" and e["agent"] == 0 and e.get("kind") == "post_run"]
    post_seqs: list[int] = []
    anomalies: list[dict[str, Any]] = []
    if post_msgs:
        first = min(seq_of[e["_order"]] for e in post_msgs)
        runs = {e.get("_run") for e in post_msgs if e.get("_run")}
        turns = {events[seq_of[e["_order"]]]["turn"] for e in post_msgs}
        post_seqs = sorted(seq_of[e["_order"]] for e in ordered if not e.get("_runner") and (
            seq_of[e["_order"]] >= first or (e.get("_run") in runs) or (e["agent"] == 0 and events[seq_of[e["_order"]]].get("turn") in turns)))
        end_ev = next((e for e in events if e["type"] == "episode_end"), None)
        for e in post_msgs:
            s_ = seq_of[e["_order"]]
            if end_ev is None or events[s_]["us"] <= end_ev["us"]:
                anomalies.append({"seq": s_, "message": e.get("_message"), "t": events[s_]["t"], "episode_end_t": end_ev["t"] if end_ev else None,
                                  "note": "a post_run message at or before the episode end (or with none): left out of every scorer input by reference"})
        inside = set(post_seqs)
        for e in events:
            if e["type"] in F.RUNNER_TYPES and e.get("target") in inside:
                raise NormaliseError(f"runner event {e['type']} (seq {e['seq']}) names the post-run exchange (seq {e['target']})")
    last_t = max([e["t"] for e in events] + [e["t_end"] for e in events if e.get("t_end") is not None])
    header = {
        "type": "header", "format": F.FORMAT, "version": F.VERSION, "workspace": b.ws, "excluded": clock.as_header(), "post_run": post_seqs, "clock_origin": origin,
        "wait_charges": "measured" if hints.legacy_waits else "scheduled",
        "handbacks": "recorded" if (hints.handbacks_recorded if hints.handbacks_recorded is not None else any(r.type == "handback" for r in hints.runner)) else "inferred",
        "events": len(events),
        "segments": sum(1 for e in events if e["type"] == "segment_start"), "agents": len(remap["agent"]), "turns": len(remap["turn"]), "duration_ms": last_t,
    }
    traj = Trajectory(header=header, events=events)

    m = b.m

    def reseq(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            r = dict(r)
            if "_order" in r:
                o = r.pop("_order")
                r["seq"] = seq_of.get(o) if o is not None else None
            out.append(r)
        return out

    by_order = {e["_order"]: e for e in b.events}
    steering = []
    for s in m["steering"]:
        s = dict(s)
        if "_turn" in s:
            s["turn"] = remap["turn"].get(s.pop("_turn"))
        if "_message" in s:
            ev = b.by_item.get(s.pop("_message"))
            s["seq"] = seq_of.get(ev["_order"]) if ev else None
        steering.append(s)
    manifest: dict[str, Any] = {
        "format": F.MANIFEST_FORMAT,
        "version": F.VERSION,
        "trajectory_sha256": traj.sha256(),
        "adapter": {"profile": profile.name, "verified": profile.verified},
        "run_info": hints.run_info,
        "observed": _observed(segments),
        "episode_start_utc": t0.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "instruction_files": list(hints.instruction_files),
        "driver_ref_namespaces": list(hints.driver_ref_namespaces),
        "sources": [{"source": n + 1, "threads": {tid: t.source for tid, t in seg.threads.items()}, "sessions": [s.path for s in seg.sessions]} for n, seg in enumerate(segments)],
        "segments": {str(k): v for k, v in m["segments"].items()},
        "agents": {str(remap["agent"][k]): v for k, v in m["agents"].items() if k in remap["agent"]},
        "turns": {str(remap["turn"][k]): v for k, v in m["turns"].items() if k in remap["turn"]},
        "calls": {str(remap["call"][by_order[o]["call"]]): v for o, v in m["calls"].items()},
        "questions": {str(remap["question"][by_order[o]["question"]]): v for o, v in m["questions"].items()},
        "events": {str(seq_of[o]): i for o, i in m["items"].items()},
        # For the bundle maker's probe references: each event's T3 run, and a user message's id.
        "event_runs": {str(seq_of[e["_order"]]): e["_run"] for e in ordered if e.get("_run")},
        "event_messages": {str(seq_of[e["_order"]]): e["_message"] for e in ordered if e.get("_message")},
        # An answer has no message id: the runner names it by the question's item id or request id.
        "event_answers": {str(seq_of[e["_order"]]): [x for x in (e.get("_question_item"), e.get("_request")) if x] for e in ordered if e["type"] == "answer"},
        "steering": steering,
        "post_run": {"events": len(post_seqs), "messages": [seq_of[e["_order"]] for e in post_msgs]},
        "clock_anomalies": anomalies,
        "withheld": reseq(m["withheld"]),
        "decorations": reseq(m["decorations"]),
        "dropped": m["dropped"],
        # The runner's audit-only fields (format.RUNNER_AUDIT_FIELDS), by trajectory seq.
        "runner_audit": reseq(m["runner_audit"]),
        # Waits the runner gave a single `cause` (before `causes`): accepted
        # only for an episode with no runner record.
        "legacy_wait_cause": m.get("legacy_wait_cause", 0),
        "output_sources": m["output_sources"],
        "crosscheck": m["crosscheck"],
        "notes": m["notes"],
        "raw_sizes": {
            "t3_items": raw_items,
            "t3_item_bytes": sum(len(json.dumps(i, ensure_ascii=False).encode("utf-8", "surrogatepass")) for seg in segments for t in seg.threads.values() for i in t.items),
            "trajectory_bytes": len(traj.dumps().encode()),
        },
    }
    if "unvisited_threads" in m:
        manifest["unvisited_threads"] = m["unvisited_threads"]
    from .blindness import scan, scan_view
    from .view import scorer_view

    view = scorer_view(traj)
    manifest["scorer_view_sha256"] = view.sha256()
    manifest["blindness"] = scan(traj).as_dict()
    manifest["blindness_scorer_view"] = scan_view(view).as_dict()
    return Result(traj, manifest)


def m_items_get(b: "_Builder", seq: int, seq_of: dict[int, int]) -> str | None:
    order = next((o for o, n in seq_of.items() if n == seq), None)
    return b.m["items"].get(order)


def _observed(segments: list[Segment]) -> dict[str, Any]:
    """Harness-identifying values seen in the records, for the manifest."""
    inst, models, drivers = set(), set(), set()
    for seg in segments:
        for t in seg.threads.values():
            inst.add(t.thread.get("providerInstanceId"))
            models.add(((t.thread.get("modelSelection") or {}).get("model")))
            for r in t.runs:
                inst.add(r.get("providerInstanceId"))
                models.add((r.get("modelSelection") or {}).get("model"))
            for i in t.items:
                d = (i.get("nativeItemRef") or {}).get("driver")
                if d:
                    drivers.add(d)
    return {"provider_instances": sorted(x for x in inst if x), "models": sorted(x for x in models if x), "native_drivers": sorted(drivers)}
