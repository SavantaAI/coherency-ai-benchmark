"""An in-memory episode that implements the runner's port, for stage 1.

It behaves like the documented driver (cohbench/driver/README.md) where the
runner depends on it:

- turns are scripted as `Act`s; each user message the runner sends (start,
  reply, answer, secret refusal, reset) starts the next act; an act runs for
  `duration_s` on a fake clock, sends its `messages` through the message tool
  and makes its `writes` and metric readings at their offsets;
- `answer()` returns no message id (T3 records an answer on the request);
- `probe()` refuses while a turn runs or a request is pending, as the idle
  gate does;
- writes land in a watcher output directory in the watcher's documented
  format; a batch commits `commit_delay_s` after its first write (or at a
  mark), and a mark produces a metric reading of its batch when the task has
  a metric;
- usage can be unavailable, incomplete, growing over an act, and lagging;
- a correction can be delivered at once, later, or never;
- `export()` runs the trajectory unit's checks that need no T3 record
  (`check_handoff`) and refuses a handoff they reject, as the real export
  would.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cohbench.runner.port import (Delivery, Description, EpisodeFailure, ExportHandoff, ExportRejected, ExportResult, Interval, MarkAck, Option, Output,
                                  Pending, ProbeOutcome, Prompt, Sent, Turn, TurnNotRunning, Usage)
from cohbench.trajectory import Hints, RunnerEvent, changelog
from cohbench.trajectory import episode as tepisode
from cohbench.trajectory import format as F
from cohbench.trajectory.bundle import RUNNER_ABOUT
from cohbench.trajectory.normalise import check_runner_inputs
from cohbench.trajectory.observations import TaskObservations
from cohbench.trajectory.registry import TaskIdentifiers

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

# The reference kinds the runner may use for each runner event (never a run id).
REF_KINDS = {"verdict": {"item"}, "wait": {"item"}, "unjudged": {"item"}, "handback": {"item"}, "correction_delivered": {"message", "item"}, "controlled_reset": {"message", "item"},
             "reopen": {"message", "item"}, "notice_delivered": {"message", "item"}}


def iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_us(t: datetime) -> str:
    return t.isoformat(timespec="microseconds").replace("+00:00", "Z")


class FakeClock:
    """`clock` is the episode clock; `wall` also moves during excluded intervals."""

    def __init__(self) -> None:
        self.clock = 0.0
        self.wall = T0

    def advance(self, s: float, excluded: bool = False) -> None:
        self.wall += timedelta(seconds=s)
        if not excluded:
            self.clock += s

    def sleep(self, s: float) -> None:
        self.advance(s)

    def utcnow(self) -> datetime:
        return self.wall


@dataclass
class Act:
    duration_s: float = 10.0
    final: str | None = "Done."  # None: the turn ends after a tool call with no final text
    state: str = "finished"  # finished, question, secret, failed, refused, interrupted
    ask: str | None = None  # the question's or secret request's text
    options: tuple[str, ...] = ()
    questions: int = 1  # a question request with this many questions
    messages: list[tuple[float, str]] = field(default_factory=list)  # (offset, text) through the message tool
    writes: list[tuple[float, str, str]] = field(default_factory=list)  # (offset, path, content)
    metrics: list[tuple[float, dict[str, Any]]] = field(default_factory=list)  # (offset, reading)
    late: list[str] = field(default_factory=list)  # message-tool texts the driver reports only after an interrupt
    late_at_s: float | None = None  # when those were delivered, from the act's start (None: when the drain reports them)
    # D-029: the main run ends while a subagent still works for this long; then T3 starts a
    # continuation run of its own, which ends with `continuation_final` (None: silent).
    subagent_s: float = 0.0
    # Questions two subagents ask while the main question is pending: returned in the same wait,
    # before the main one, which is marked; each is marked in turn once the previous is answered.
    child_asks: tuple[str, ...] = ()
    child_kinds: tuple[str, ...] = ()  # each child request's kind, question (the default) or secret
    continuation_final: str | None = None
    continuation_live_s: float | None = None  # this far into that time T3's continuation run is live: steerable, and reported
    tokens: int = 1000


class FakeWatcher:
    """As the real watcher (cohbench/watcher, D-037): one net record per path
    within a batch; a batch closed only by its quiet period after the last
    write (`commit_delay_s`) or its hold limit (`hold_s`), its state rows
    published together with its closer, which carries `first_write` and
    `last_write`; a mark names an instant and waits until everything written
    up to it is committed, never closing a batch itself."""

    def __init__(self, root: Path, tree: dict[str, str], clock: FakeClock, *, commit_delay_s: float = 0.0, metric: bool = False,
                 hold_s: float = 2.0):
        self.root = root
        self.clock = clock
        self.commit_delay_s = commit_delay_s
        self.hold_s = hold_s
        self.has_metric = metric
        self.measure_batches = True  # D-036: every committed batch gets a reading
        self.metric_delay_s = 0.0
        self.pending_readings: list[tuple[float, dict[str, Any], int]] = []
        self.last_reading: dict[str, Any] = {}
        root.mkdir(parents=True)
        (root / "blobs").mkdir()
        self.seq = 0
        self.batch = 0
        self.run = 0
        self.open_since: float | None = None
        self.last_write_clock: float | None = None
        self.unpublished: dict[str, dict[str, Any]] = {}  # path -> its one net record in the open batch
        self.last_commit_wall: datetime | None = None
        self.first_wall: datetime | None = None
        self.last_wall: datetime | None = None
        self.spans: list[tuple[int, datetime | None, datetime | None]] = []  # committed batches: (no, first_write_after, last write)
        self.tree = dict(tree)
        for p, c in sorted(tree.items()):
            self._hold({"kind": "created", "path": p, "type": "file", "mode": "0644", **self._store(c)})
        self._close("baseline")
        if metric:
            self.metric({"logs": 0}, reason="baseline")

    def _store(self, content: str) -> dict[str, Any]:
        b = content.encode()
        sha = hashlib.sha256(b).hexdigest()
        d = self.root / "blobs" / sha[:2]
        d.mkdir(exist_ok=True)
        (d / sha).write_bytes(b)
        return {"sha256": sha, "size": len(b), "content": "stored"}

    def _line(self, r: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        return {"seq": self.seq, "batch": self.batch, "wall": iso_us(self.clock.wall), "mono": self.clock.clock, **r}

    def _hold(self, r: dict[str, Any]) -> None:
        prev = self.unpublished.get(r["path"])
        if prev is not None and prev["kind"] == "created":
            r = {**r, "kind": "created"}  # created and changed within one batch: one net creation
        self.unpublished[r["path"]] = r

    def _rec(self, r: dict[str, Any]) -> None:
        with open(self.root / "events.jsonl", "a") as fh:
            if r.get("kind") == "batch":
                for x in self.unpublished.values():
                    fh.write(json.dumps(self._line(x), sort_keys=True) + "\n")
            fh.write(json.dumps(self._line(r), sort_keys=True) + "\n")
        if r.get("kind") == "batch":
            self.unpublished = {}

    def _close(self, reason: str) -> int:
        b = self.batch
        # As the real watcher (round 6): write instants only when known; a lower bound for the
        # first write (the previous batch's commit, or the first write itself); no `mark` reason.
        prev = self.last_commit_wall
        # The fake looks at every path the moment it is written, so its lower bound is the
        # microsecond before the first write (every write of the batch came after it).
        lo = self.first_wall - timedelta(microseconds=1) if self.first_wall else None
        times = ({"first_write": iso_us(self.first_wall), "last_write": iso_us(self.last_wall), "write_times": "known",
                  "first_write_after": iso_us(lo)} if self.first_wall and reason != "baseline"
                 else {"first_write": None, "last_write": None, "write_times": "none", "first_write_after": iso_us(prev) if prev else None})
        self._rec({"kind": "batch", "reason": reason, "resync": False, "recovered": False, "unwatched": {}, **times})
        self.spans.append((b, lo if reason != "baseline" else None, self.last_wall))
        self.last_commit_wall = self.clock.wall
        if self.has_metric and b > 0 and self.measure_batches:
            # As the real watcher: the reading is published on its own, after the batch closes.
            self.pending_readings.append((self.clock.clock + self.metric_delay_s, dict(self.last_reading) or {"logs": 0}, b))
            self.publish_readings()
        self.batch += 1
        self.open_since = self.last_write_clock = None
        self.first_wall = self.last_wall = None
        return b

    def publish_readings(self) -> None:
        due = [x for x in self.pending_readings if x[0] <= self.clock.clock + 1e-9]
        self.pending_readings = [x for x in self.pending_readings if x[0] > self.clock.clock + 1e-9]
        for _t, reading, b in due:
            self.metric(reading, reason="change", batch=b)

    def _due(self) -> float | None:
        """When the open batch closes by itself: its quiet period or its hold limit."""
        if self.open_since is None:
            return None
        return min(self.last_write_clock + self.commit_delay_s, self.open_since + self.hold_s)  # type: ignore[operator]

    def tick(self) -> None:
        self.publish_readings()
        due = self._due()
        if due is not None and self.clock.clock >= due - 1e-9:
            self._close("settled")

    def write(self, path: str, content: str) -> None:
        self.tick()
        kind = "modified" if path in self.tree else "created"
        self.tree[path] = content
        self._hold({"kind": kind, "path": path, "type": "file", "mode": "0644", **self._store(content)})
        self.first_wall = self.first_wall or self.clock.wall
        self.last_wall = self.clock.wall
        if self.open_since is None:
            self.open_since = self.clock.clock
        self.last_write_clock = self.clock.clock
        self.tick()

    def mark(self, label: str, accepted: bool = True, at: datetime | None = None) -> tuple[int, int | None]:
        """D-037: name the instant `at` (now when None). The mark waits until
        everything written up to `at` is committed by the batching itself (the
        fake's clock moves on while it waits), then names the batch in effect
        at `at` and whether a batch straddles it. An unaccepted mark names no
        state and takes no reading."""
        at = at or self.clock.wall
        if not accepted:
            self._rec({"kind": "mark", "label": label, "request": f"r-{label}", "reason": "mark", "batch": self.batch - 1, "accepted": False,
                       "at": iso_us(at)})
            self.last_answer = (self.batch - 1, None, None)
            return self.batch - 1, None
        if self.first_wall is not None and self.first_wall <= at:
            due = self._due()
            if due is not None and due > self.clock.clock:
                self.clock.advance(due - self.clock.clock)
            self._close("settled")
        # As the real watcher (replay.answer_for): the run of batches with first_write_after < at <
        # last_write straddles the instant; the batch named is the last one ended by `at` before it.
        run = [n for n, lo, last in self.spans if n > 0 and lo is not None and last is not None and lo < at < last]
        straddled, straddled_last = (min(run), max(run)) if run else (None, None)
        in_effect = max((n for n, _f, last in self.spans if (last is None or last <= at) and (straddled is None or n < straddled)), default=0)
        self._rec({"kind": "mark", "label": label, "request": f"r-{label}", "reason": "mark", "batch": in_effect, "accepted": True,
                   "at": iso_us(at), "straddled": straddled, "straddled_last": straddled_last})
        self.last_answer = (in_effect, straddled, straddled_last)  # the whole answer, `straddled_last` included
        return in_effect, straddled  # as the real watcher (D-036): readings are of committed batches, not of marks

    def metric(self, reading: dict[str, Any], reason: str = "change", batch: int | None = None, label: str | None = None) -> None:
        self.run += 1
        self.last_reading = dict(reading)
        line = {"run": self.run, "batch": max(self.batch - 1, 0) if batch is None else batch, "reason": reason, "ok": True, "metrics": reading,
                "cmd": ["x"], "start": iso(self.clock.wall), "end": iso(self.clock.wall)}
        if label:
            line["label"] = label
        with open(self.root / "metrics.jsonl", "a") as fh:
            fh.write(json.dumps(line) + "\n")


class FakeEpisode:
    def __init__(self, acts: list[Act], out: Path, clock: FakeClock, *, tree: dict[str, str] | None = None,
                 probe: ProbeOutcome | str | None = "no", correct_race: bool = False, reject_export: bool = False,
                 fail_on: str | None = None, mechanism: str = "native_steer", delivery_delay_s: float | None = 0.0,
                 commit_delay_s: float = 0.0, metric: bool = False, usage: str = "ok", usage_lag_s: float = 0.0,
                 close_error: bool = False, no_item_ids: bool = False, delivery_mark: bool = True, usage_seq: list[int] | None = None,
                 delivery_skew_us: int = 0, mark_settled: bool | None = None, delivery_at_s: float | None = None,
                 fail_marks: tuple[str, ...] = (), unaccepted_marks: int = 0, finish_s: float = 0.0, drain_never_stops: bool = False,
                 slow_usage: tuple[float, float] | None = None, dispatch_s: float = 0.0, hand_back_lag_s: float = 0.0, metric_delay_s: float = 0.0, collect=(), collected_files=None):
        from cohbench.trajectory.collection import Collector
        self.collector = Collector(collect)
        self.collected_files = dict(collected_files or {})
        self.acts = list(acts)
        self.out = Path(out)
        self.c = clock
        self.probe_reply = probe
        self.correct_race = correct_race
        self.reject_export = reject_export
        self.fail_on = fail_on
        self.mechanism = mechanism
        self.delivery_delay_s = delivery_delay_s  # None: never delivered
        self.delivery_at_s = delivery_at_s  # a correction sent before this episode-clock time is delivered at it, whenever it was sent
        self.commit_delay_s = commit_delay_s
        self.metric = metric
        self.usage_mode = usage
        self.usage_lag_s = usage_lag_s
        self.close_error = close_error
        self.no_item_ids = no_item_ids
        self.delivery_mark = delivery_mark  # False: the driver could take no mark at the observed delivery
        self.usage_seq = list(usage_seq) if usage_seq is not None else None  # scripted readings, the last repeated
        self.delivery_skew_us = delivery_skew_us  # outputs dated this far ahead of the runner's clock
        self.mark_settled = mark_settled  # the watcher's word on every mark (None: not said)
        self.fail_marks = set(fail_marks)  # these mark labels are never acknowledged
        self.unaccepted_marks = unaccepted_marks  # this many marks, from now on, come back not accepted
        self.finish_s = finish_s
        self.slow_usage = slow_usage  # (from this episode-clock time, the usage read takes this long once)
        self.dispatch_s = dispatch_s  # how long each dispatch takes
        self.reset_continuation_unsent = False  # the first reset's continuation does not reach T3 (the handoff is done)
        self.handoff_done_only = False
        self.continuation_attempt_s = 0.0  # an unsent continuation's launch attempt takes this long (counted time) before it fails
        self.delivery_straddle: dict[str, int | None] = {}
        self.mark_answers: dict[str, tuple[int, int | None, int | None]] = {}  # label: (batch, straddled, straddled_last)
        self.lag: tuple[str | None, str, float] | None = None
        self.hand_back_lag_s = hand_back_lag_s  # the driver confirms a finished turn this long after its final output
        self.drain_never_stops = drain_never_stops
        self.delivery_calls: dict[str, int] = {}
        self.mechanism_after_calls = 0  # the route is known only after this many delivery reads
        self.settled_mechanism: str | None = None  # the route the driver reports at a delayed delivery
        self.segment = 1
        self.watchers = [FakeWatcher(self.out / "watcher" / "segment-1", tree or {"README.md": "Read me.\n"}, clock,
                                     commit_delay_s=commit_delay_s, metric=metric)]
        self.watchers[0].metric_delay_s = metric_delay_s
        self.n_run = 0
        self.n_msg = 0
        self.cur: Act | None = None
        self.cur_run = ""
        self.cur_started = 0.0
        self.cur_done: set[tuple[str, int]] = set()
        self.pending: Pending | None = None
        self.started_acts: list[tuple[float, Act]] = []
        self.received: list[tuple[str, Any]] = []  # (via, text or answer value)
        self.excluded_list: list[Interval] = []
        self.user_waits: list[tuple[Any, str, str]] = []
        self.handoff: ExportHandoff | None = None
        self.handoff_problems: list[str] = []
        self.closed = False
        self.interrupts = 0
        self.probes_asked: list[str] = []
        self.marks: dict[str, int] = {}
        self.items: set[str] = set()
        self.messages: set[str] = set()
        self.requests: set[str] = set()
        self.corrections: list[tuple[Sent, float]] = []
        self.start_utc: str | None = None
        self.mark_times: dict[str, str] = {}
        self.answered: list[tuple[str | None, str]] = []
        self.drain: tuple[str, list[str]] | None = None
        self.late_src: list[str] = []
        self.late_at: float | None = None
        self.in_continuation = False
        self.correct_calls = 0
        self.child_queue: list[Pending] = []
        self.cont_live_s: float | None = None
        self.marked_fresh = False  # a request already returned is marked to answer at the next wait
        self.started_by_t3: list[str] = []
        self.output_items: set[str] = set()

    # ----- helpers ---------------------------------------------------------------------

    def _check(self, op: str) -> None:
        if self.fail_on == op:
            raise EpisodeFailure(f"fake failure in {op}")  # fatal, as a driver failure is unless it says otherwise

    def _take(self, timeout_s: float | None) -> None:
        """As the driver: a dispatch takes `dispatch_s`; with less time than that it
        is never started and raises a non-fatal "unsent" failure."""
        if timeout_s is not None and timeout_s < self.dispatch_s:
            raise EpisodeFailure(f"unsent: {timeout_s:.3f} s left, the call needs {self.dispatch_s} s", fatal=False)
        if self.dispatch_s:
            self.c.advance(self.dispatch_s)

    def _sent(self, via: str, text: Any, *, message: bool = True, **kw: Any) -> Sent:
        self.received.append((via, text))
        self._next_act()
        mid = None
        if message:
            self.n_msg += 1
            mid = f"msg-{self.n_msg}"
            self.messages.add(mid)
        return Sent(utc=iso(self.c.wall), clock_s=self.c.clock, run_id=self.cur_run, thread_id=f"thread-{self.segment}", message_id=mid, **kw)

    def _next_act(self) -> None:
        self.n_run += 1
        self.cur_run = f"run-{self.n_run}"
        self.cur = self.acts.pop(0) if self.acts else Act()
        self.late_src = list(self.cur.late)
        self.cur_started = self.c.clock
        self.late_at = None if self.cur.late_at_s is None else self.cur_started + self.cur.late_at_s  # from this act's own start
        self.cur_done = set()
        self.pending = None
        self.started_acts.append((self.c.clock, self.cur))

    def _when(self, at_clock: float | None = None) -> str:
        """A delivery time: now, or the scripted instant `at_clock` when it is already past."""
        wall = self.c.wall - timedelta(seconds=self.c.clock - at_clock) if at_clock is not None and at_clock < self.c.clock else self.c.wall
        return iso_us(wall + timedelta(microseconds=self.delivery_skew_us)) if self.delivery_skew_us else iso(wall)

    def _item(self, name: str) -> str | None:
        if self.no_item_ids:
            return None
        self.items.add(name)
        self.output_items.add(name)
        return name

    # ----- the port ----------------------------------------------------------------------

    def start(self, text: str) -> Sent:
        self._check("start")
        s = self._sent("start", text)
        self.start_utc = s.utc
        return s

    def reply(self, text: str, *, timeout_s: float | None = None) -> Sent:
        self._take(timeout_s)
        self._check("reply")
        if self.cur is not None or self.pending is not None:
            raise EpisodeFailure("reply needs a finished turn")
        return self._sent("reply", text)

    def answer(self, pending: Pending, value: dict[str, str], *, timeout_s: float | None = None) -> Sent:
        self._take(timeout_s)
        if self.pending is None or self.pending.kind != "question" or pending.item_id != self.pending.item_id:
            raise EpisodeFailure("no such pending question")
        if set(value) != {q.id for q in pending.prompts}:
            raise EpisodeFailure(f"an answer per question id: {sorted(value)}")
        self.pending = None
        self.answered.append((pending.request_id, pending.item_id))
        # As the driver: a text equal to an option's label goes as that option's value; the Sent says what went.
        norm = {q.id: next((o.value or o.label for o in q.options if o.label == value[q.id]), value[q.id]) for q in pending.prompts}
        if self.child_queue:
            # Another request is still pending: the tree is not idle, the main turn does not go on yet.
            self.pending = self.child_queue.pop(0)
            self.marked_fresh = True
            self.received.append(("answer", dict(value)))
            return Sent(utc=iso(self.c.wall), clock_s=self.c.clock, run_id=self.cur_run, thread_id=pending.thread_id, request_id=pending.request_id,
                        submitted=norm)
        return self._sent("answer", dict(value), message=False, request_id=pending.request_id, submitted=norm)

    def refuse_secret(self, pending: Pending, text: str, *, timeout_s: float | None = None) -> Sent:
        self._take(timeout_s)
        if self.pending is None or self.pending.kind != "secret":
            raise EpisodeFailure("no pending secret")
        self.pending = None
        return self._sent("refuse_secret", text)

    def _continuation_live(self) -> bool:
        return self.cont_live_s is not None and self.c.clock - self.cur_started >= self.cont_live_s - 1e-6

    def correct(self, text: str, *, timeout_s: float | None = None) -> Sent:
        self._take(timeout_s)
        self.correct_calls += 1
        if self.cur is None or self.correct_race:
            raise TurnNotRunning("the turn already ended")
        if self.in_continuation and not self._continuation_live():
            # D-029, as the adapter maps the driver: the main run has ended while a subagent works; nothing to steer.
            raise TurnNotRunning("the main run has ended while subagent work continues")
        self.received.append(("correct", text))
        self.n_msg += 1
        mid = f"msg-{self.n_msg}"
        self.messages.add(mid)
        now = iso(self.c.wall)
        at_once = self.delivery_delay_s == 0 and self.delivery_at_s is None
        ack = self.mark(f"correction-{mid}", at=now) if at_once and self.delivery_mark else None
        mark = ack.label if ack is not None else None
        # As the driver: an immediate delivery carries its mark's whole answer, the straddle included.
        s = Sent(utc=now, clock_s=self.c.clock, run_id=self.cur_run, thread_id=f"thread-{self.segment}", message_id=mid, mechanism=self.mechanism,
                 delivered_utc=now if at_once else None, delivery_mark=mark,
                 delivery_settled=self.mark_settled if mark else None, delivery_quiescent=self.mark_settled if mark else None,
                 delivery_straddled=ack.straddled if ack is not None else None, delivery_straddled_last=ack.straddled_last if ack is not None else None)
        self.corrections.append((s, self.c.clock))
        return s

    def correction_delivery(self, sent: Sent) -> Delivery | None:
        """As the driver: the delivery time, and a watcher mark taken the first time the delivery is observed."""
        if self.delivery_at_s is not None:
            if self.c.clock < self.delivery_at_s:
                return None
            label = f"correction-{sent.message_id}" if self.delivery_mark else None
            when = iso(T0 + timedelta(seconds=self.delivery_at_s))
            if label is not None and label not in self.marks:
                self.mark(label, at=when)  # D-037: the boundary mark names the delivery's instant
            self.delivery_calls[sent.message_id or ""] = self.delivery_calls.get(sent.message_id or "", 0) + 1
            mech = self.settled_mechanism if self.delivery_calls[sent.message_id or ""] > self.mechanism_after_calls else None
            ans = self.mark_answers.get(label or "", (None, None, None))
            return Delivery(when, label, mechanism=mech, straddled=ans[1], straddled_last=ans[2])
        if self.delivery_delay_s is None:
            return None
        clock0 = next(c for s, c in self.corrections if s.message_id == sent.message_id)
        if self.c.clock >= clock0 + self.delivery_delay_s:
            label = f"correction-{sent.message_id}" if self.delivery_mark else None
            when = iso(datetime.fromisoformat(sent.utc.replace("Z", "+00:00")) + timedelta(seconds=self.delivery_delay_s))
            if label is not None and label not in self.marks:
                self.mark(label, at=when)
            ans = self.mark_answers.get(label or "", (None, None, None))
            return Delivery(when, label, self.mark_settled if label else None, self.mark_settled if label else None,
                            straddled=ans[1], straddled_last=ans[2])
        return None

    def interrupt(self) -> None:
        """As the driver: stops the whole tree, running or pending; the next
        wait reports it stopped, with what another thread delivered meanwhile."""
        self.interrupts += 1
        if self.cur is not None or self.pending is not None:
            self.drain = (self.cur_run, list(self.late_src))
        self.cur = None
        self.pending = None

    def reset(self, text: str, *, timeout_s: float | None = None, dispatch_timeout_s: float | None = None) -> Sent:
        """As the driver: `timeout_s` bounds the handoff (excluded time, 30 s here), `dispatch_timeout_s`
        the continuation's launch after it."""
        if timeout_s is not None and timeout_s < 30:
            raise EpisodeFailure(f"the handoff did not finish within {timeout_s} s")  # fatal, as the driver's
        if self.handoff_done_only:
            # A second call after an unsent continuation launches only the continuation.
            self.handoff_done_only = False
            self._take(dispatch_timeout_s)
            return self._sent("reset", text)
        self._check("reset")
        if self.cur is not None or self.pending is not None:
            raise EpisodeFailure("reset needs an idle turn boundary")
        try:
            self.collect()
        except ExportRejected as exc:
            self.collector.error = str(exc)
        self.collected_files = {}
        w0 = iso(self.c.wall)
        self.c.advance(30, excluded=True)
        self.excluded_list.append(Interval(w0, iso(self.c.wall), "handoff"))
        self.segment += 1
        self.watchers.append(FakeWatcher(self.out / "watcher" / f"segment-{self.segment}", self.watchers[-1].tree, self.c,
                                         commit_delay_s=self.commit_delay_s, metric=self.metric))
        if self.metric:
            self.watchers[-1].last_reading = dict(self.watchers[-2].last_reading)
        if self.reset_continuation_unsent:
            self.reset_continuation_unsent = False
            self.handoff_done_only = True
            self.c.advance(self.continuation_attempt_s)
            raise EpisodeFailure("the continuation never reached T3", fatal=False, unsent=True, done=["handoff"], not_sent=["continuation"])
        self._take(dispatch_timeout_s)
        return self._sent("reset", text)

    def probe(self, question: str) -> ProbeOutcome:
        self.probes_asked.append(question)
        if self.cur is not None or self.pending is not None:
            return ProbeOutcome("unavailable", reason="not at a turn boundary")
        w0 = iso(self.c.wall)
        self.c.advance(20, excluded=True)
        self.excluded_list.append(Interval(w0, iso(self.c.wall), "probe"))
        if isinstance(self.probe_reply, BaseException):
            raise self.probe_reply  # the main episode could not be recovered after the copy
        if isinstance(self.probe_reply, ProbeOutcome):
            return self.probe_reply
        if self.probe_reply is None:
            return ProbeOutcome("unavailable", reason="session resume is not supported")
        return ProbeOutcome("answered", self.probe_reply)

    def mark(self, label: str, at: str | None = None) -> MarkAck:
        if label in self.fail_marks:
            raise EpisodeFailure(f"the watcher did not acknowledge mark {label}", fatal=False)  # as the adapter maps a mark not acknowledged
        if self.unaccepted_marks > 0:
            self.unaccepted_marks -= 1
            b, _ = self.watchers[-1].mark(label, accepted=False)
            return MarkAck(label, b, self.mark_settled, self.mark_settled, accepted=False)
        at_dt = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None
        # The real mark waits for the batching; meanwhile the agent goes on writing, which
        # can extend the open batch (its quiet period restarts at each write).
        w = self.watchers[-1]
        while self.cur is not None and w.first_wall is not None and w.first_wall <= (at_dt or self.c.wall):
            due = w._due()
            nxt = self._next_write_at()
            if due is None or nxt is None or nxt > due:
                break
            self._play_writes_until(nxt)
        w.mark(label, at=at_dt)
        b, straddled, straddled_last = w.last_answer
        self.marks[label] = b
        self.mark_times[label] = at or iso(self.c.wall)
        self.mark_answers[label] = (b, straddled, straddled_last)
        return MarkAck(label, b, self.mark_settled, self.mark_settled, accepted=True, straddled=straddled, covered_utc=at or iso_us(self.c.wall),
                       straddled_last=straddled_last)

    def _next_write_at(self) -> float | None:
        act = self.cur
        if act is None:
            return None
        times = [self.cur_started + off for i, (off, *_r) in enumerate(act.writes) if ("w", i) not in self.cur_done and self.cur_started + off >= self.c.clock]
        return min(times) if times else None

    def _play_writes_until(self, t: float) -> None:
        act = self.cur
        assert act is not None
        for i, (off, path, content) in sorted(enumerate(act.writes), key=lambda x: x[1][0]):
            at = self.cur_started + off
            if at <= t + 1e-9 and ("w", i) not in self.cur_done:
                if at > self.c.clock:
                    self.c.advance(at - self.c.clock)
                self.watchers[-1].tick()
                self.cur_done.add(("w", i))
                self.watchers[-1].write(path, content)

    def clock(self) -> float:
        return self.c.clock

    def origin_utc(self) -> str | None:
        return self.start_utc

    def excluded(self) -> list[Interval]:
        return list(self.excluded_list)

    def note_user_wait(self, ref: Any, start_utc: str, end_utc: str) -> None:
        self.user_waits.append((ref, start_utc, end_utc))

    def _tokens_at(self, t: float) -> int:
        total = 0.0
        for start, act in self.started_acts:
            if start > t:
                continue
            if self.usage_mode == "growing":
                total += act.tokens * min(max((t - start) / max(act.duration_s, 1e-9), 0.0), 1.0)
            else:
                total += act.tokens
        return int(total)

    def usage(self) -> Usage:
        if self.slow_usage is not None and self.c.clock >= self.slow_usage[0]:
            self.c.advance(self.slow_usage[1])  # a gateway read that takes this long, once
            self.slow_usage = None
        if self.usage_seq is not None:
            v = self.usage_seq.pop(0) if len(self.usage_seq) > 1 else self.usage_seq[0]
            return Usage(v, True, {"calls": 1, "tokens": v})
        if self.usage_mode == "none":
            return Usage(None, False, {"calls": 0})
        tokens = self._tokens_at(self.c.clock - self.usage_lag_s)
        return Usage(tokens, self.usage_mode != "partial", {"calls": len(self.started_acts), "tokens": tokens})

    def watcher_dirs(self) -> list[Path]:
        for w in self.watchers:
            w.publish_readings()  # readings due by now are on disk, as the watcher writes them on its own
        return [w.root for w in self.watchers]

    def describe(self) -> Description:
        return Description("pi", "1.6.0", "0.1.12-nightly.20261121.5051", "driver-1", "claude-haiku-5-5")

    def wait(self, timeout_s: float) -> Turn:
        self._check("wait")
        if self.marked_fresh and self.pending is not None:
            self.marked_fresh = False
            return Turn(self.pending.kind, self.cur_run, (), pending=self.pending, closing=self.pending.item_id, at=iso_us(self.c.wall))
        act = self.cur
        if act is None and self.drain is not None:
            run, late = self.drain
            if self.drain_never_stops:
                return Turn("time_limit", run, ())  # the tree never reads stopped
            self.drain = None
            when = iso(T0 + timedelta(seconds=self.late_at)) if self.late_at is not None else iso(self.c.wall)
            outs = tuple(Output("message", t, self._item(f"item-{run}-late{i}"), run, when) for i, t in enumerate(late))
            return Turn("interrupted", run, outs)
        if act is None:
            raise EpisodeFailure("no running turn")
        run = self.cur_run
        # The driver's finish allowance: a wait may run past its timeout by up to this much.
        step = min(timeout_s + self.finish_s, max(act.duration_s - (self.c.clock - self.cur_started), 0.0))
        outputs: list[Output] = []
        events = sorted([(o, "w", i) for i, (o, *_r) in enumerate(act.writes)] + [(o, "m", i) for i, (o, _t) in enumerate(act.messages)]
                        + [(o, "r", i) for i, (o, _x) in enumerate(act.metrics)])
        target = self.c.clock + step
        for off, kind, i in events:
            at = self.cur_started + off
            if at <= target and (kind, i) not in self.cur_done:
                if at > self.c.clock:
                    self.c.advance(at - self.c.clock)
                self.watchers[-1].tick()
                self.cur_done.add((kind, i))
                if kind == "w":
                    _o, path, content = act.writes[i]
                    self.watchers[-1].write(path, content)
                elif kind == "r":
                    self.watchers[-1].metric(act.metrics[i][1])
                else:
                    outputs.append(Output("message", act.messages[i][1], self._item(f"item-{run}-m{i}"), run, self._when(at)))
        if target > self.c.clock:
            self.c.advance(target - self.c.clock)
        self.watchers[-1].tick()
        if self.c.clock - self.cur_started < act.duration_s - 1e-6:  # float sums of short sleeps fall a hair short
            live = (run,) if self.in_continuation and self._continuation_live() else ()
            return Turn("time_limit", run, tuple(outputs), started_by_t3=live)
        st = act.state
        if st == "finished" and act.subagent_s and not self.in_continuation:
            # The main run ends, a subagent still works: not a hand-back (D-029). Its final output is reported now.
            if act.final is not None:
                outputs.append(Output("final", act.final, self._item(f"item-{run}-final"), run, self._when()))
            self.in_continuation = True
            self.n_run += 1
            self.cur_run = f"run-{self.n_run}"
            self.cur_started = self.c.clock
            self.cur = Act(duration_s=act.subagent_s, final=act.continuation_final, tokens=0)
            self.cont_live_s = act.continuation_live_s
            self.started_by_t3.append(self.cur_run)
            return Turn("time_limit", run, tuple(outputs))
        if st == "finished" and self.hand_back_lag_s and not self.in_continuation and act.final is not None:
            # As the real driver: the final output is reported in a `time_limit` slice while it
            # confirms the tree is idle; the hand-back follows `hand_back_lag_s` later, dated at the run's end.
            if self.lag is None:
                closing = self._item(f"item-{run}-final")
                self.lag = (closing, iso_us(self.c.wall), self.c.clock + self.hand_back_lag_s)
                return Turn("time_limit", run, tuple(outputs) + (Output("final", act.final, closing, run, self._when()),))
            closing, ended, until = self.lag
            if self.c.clock < until - 1e-9:
                self.c.advance(min(timeout_s, until - self.c.clock))
            if self.c.clock < until - 1e-9:
                return Turn("time_limit", run, tuple(outputs))
            self.lag = None
            self.cur = None
            return Turn("finished", run, tuple(outputs), closing=closing, at=ended)
        self.cur = None
        cont = self.in_continuation
        self.in_continuation = False
        t3_runs = (run,) if cont else ()
        if st == "finished":
            closing = None
            if act.final is not None:
                closing = self._item(f"item-{run}-final")
                outputs.append(Output("final", act.final, closing, run, self._when()))
            return Turn("finished", run, tuple(outputs), closing=closing, started_by_t3=t3_runs, at=iso_us(self.c.wall))
        if st == "question":
            prompts = tuple(Prompt(f"q{k + 1}", act.ask or "?", options=tuple(Option(o, o.lower()) for o in act.options)) for k in range(act.questions))
            p = Pending("question", self._item(f"item-{run}-question") or "", f"thread-{self.segment}", f"req-{run}", prompts)
            self.requests.add(f"req-{run}")
            for k, ask in enumerate(act.child_asks):
                kind = act.child_kinds[k] if k < len(act.child_kinds) else "question"
                if kind == "secret":
                    c = Pending("secret", self._item(f"item-{run}-child{k}") or "", f"thread-child{k}", None, label=ask)
                else:
                    c = Pending("question", self._item(f"item-{run}-child{k}") or "", f"thread-child{k}", f"req-{run}-child{k}", (Prompt("q1", ask),))
                    self.requests.add(c.request_id or "")
                self.child_queue.append(c)
                outputs.append(Output(kind, c.text(), c.item_id, run, self._when()))
        elif st == "secret":
            p = Pending("secret", self._item(f"item-{run}-secret") or "", f"thread-{self.segment}", None, label=act.ask or "A credential")
        else:
            return Turn(st, run, tuple(outputs), detail=f"the scripted turn ended {st}")
        self.pending = p
        outputs.append(Output(st, p.text(), p.item_id or None, run, self._when()))
        return Turn(st, run, tuple(outputs), pending=p, closing=p.item_id or None, at=iso_us(self.c.wall))

    def collect(self):
        from cohbench.trajectory.collection import captured

        def read(path):
            value = self.collected_files.get(path)
            if isinstance(value, Exception):
                raise value
            return captured(value.encode() if isinstance(value, str) else value)
        try:
            self.collector.capture(self.segment, read)
            return self.collector.result()
        except Exception as exc:
            raise ExportRejected("collection", [str(exc)]) from exc

    def export(self, dest: Path, handoff: ExportHandoff) -> ExportResult:
        self.handoff = handoff
        collected = self.collect()
        if collected.records:
            handoff.episode_record["collected"] = collected.records
            collected.write(dest / "collected" / "blobs")
        w0 = iso(self.c.wall)
        self.c.advance(5, excluded=True)
        self.excluded_list.append(Interval(w0, iso(self.c.wall), "export"))
        self.handoff_problems = check_handoff(handoff, self)
        if self.handoff_problems:
            raise ExportRejected("handoff", self.handoff_problems)
        if self.reject_export:
            raise ExportRejected("bundle", ["scorer.jsonl: a stand-in rejection"])
        files = {
            "trajectory": ["trajectory/trajectory.jsonl"],
            "identifier_registry": ["bundle/identifiers.json", "trajectory/registry.json"],
            "classifier_labels": ["bundle/scorer.jsonl"],
            "transcript": ["bundle/scorer.jsonl"],
            "file_diffs": ["bundle/changes.jsonl", "watcher/segment-1"],
            "probe_answers": ["bundle/episode.json"],
            "driver_tool_calls": ["trajectory/trajectory.jsonl"],
            "scoring_bundle": ["bundle"],
            "raw_record": ["manifest.json"],
            "harness": ["manifest.json"],
            "driver_version": ["manifest.json"],
            "user_wait": ["bundle/scorer.jsonl"],
        }
        for rel in {p for v in files.values() for p in v}:
            p = dest / rel
            if "." in p.name:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"stand_in": rel}) + "\n")
            else:
                p.mkdir(parents=True, exist_ok=True)
                (p / "x.json").write_text("{}\n")
        (dest / "bundle" / "episode.json").write_text(json.dumps(handoff.episode_record) + "\n")
        return ExportResult(files)

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise EpisodeFailure("a container could not be removed")


def check_handoff(h: ExportHandoff, fake: FakeEpisode) -> list[str]:
    """What the trajectory unit would reject in the runner's handoff, using its
    own code and tables wherever they need no T3 record."""
    e: list[str] = []
    known = {"item": fake.items, "message": fake.messages}
    events: list[RunnerEvent] = []
    for n, r in enumerate(h.runner_events):
        where = f"runner event {n} ({r.get('type')})"
        if r.get("type") == "controlled_reset":
            if r.get("continuation") not in (None, "launched"):
                continue  # as the driver's export: a handoff whose continuation never went is not in the trajectory
            r = {k: v for k, v in r.items() if k != "continuation"}  # the driver's own key, not a RunnerEvent field
        try:
            events.append(RunnerEvent(**r))
        except TypeError as exc:
            e.append(f"{where}: {exc}")
            continue
        spec = F.RUNNER_FIELDS.get(r["type"])
        if spec is None:
            e.append(f"{where}: not a runner event type")
            continue
        own = set(spec) - {"target", "t_end"}
        extra = set(r["fields"]) - own
        if extra:
            e.append(f"{where}: undeclared fields {sorted(extra)}")
        missing = own - set(r["fields"])
        if missing:
            e.append(f"{where}: missing fields {sorted(missing)}")
        for k, t in spec.items():
            if isinstance(t, tuple) and t[0] == "enum" and k in r["fields"] and r["fields"][k] not in t[1] and not (t[2] and r["fields"][k] is None):
                e.append(f"{where}: {k} {r['fields'][k]!r} is not one of {t[1]}")
        ref = r.get("ref")
        allowed = REF_KINDS.get(r["type"], set())
        if ref is None:
            if r["type"] in ("verdict", "unjudged", "correction_delivered", "controlled_reset", "reopen"):
                e.append(f"{where}: needs a reference")
            if r["type"] == "wait" and "verdict_reply" in (r["fields"].get("causes") or [r["fields"].get("cause")]) and not r["fields"].get("replies_to"):
                e.append(f"{where}: a verdict-reply wait needs the outputs it replies to")
        elif not allowed:
            e.append(f"{where}: takes no reference")
        elif not isinstance(ref, dict) or len(ref) != 1 or next(iter(ref)) not in allowed:
            e.append(f"{where}: reference {ref} is not one of {sorted(allowed)}")
        else:
            (k, v), = ref.items()
            if v not in known[k]:
                e.append(f"{where}: {k} {v!r} was never given out by the episode")
        if r.get("end_utc") is not None and r["type"] != "wait":
            e.append(f"{where}: only a wait has an end")
        if r["type"] == "correction_delivered" and r["fields"].get("mark") is not None and r["fields"].get("mark") not in h.marks:  # null: no mark (D-030)
            e.append(f"{where}: its mark {r['fields'].get('mark')!r} is not one the runner took")
    try:
        hints = Hints(user_kinds=h.user_kinds, answers=h.answers, runner=events, excluded=h.excluded)
        e += [f"check_runner_inputs: {x}" for x in check_runner_inputs(hints, h.probe_targets)]
    except Exception as exc:  # noqa: BLE001
        e.append(f"hints: {exc!r}")
    for k, kind in h.user_kinds.items():
        if kind not in F.USER_KINDS:
            e.append(f"user_kinds[{k}]: {kind!r}")
        if k not in fake.messages | fake.items | fake.requests:
            e.append(f"user_kinds: {k!r} was never given out by the episode")
    for k in h.answers:
        if k not in fake.items | fake.requests:
            e.append(f"answers: {k!r} is not a question request")
    # Every output given out has a verdict or an unjudged record.
    judged = {json.dumps(r["ref"], sort_keys=True) for r in h.runner_events if r["type"] in ("verdict", "unjudged")}
    post_run = (h.post_run or {}).get("run")
    for item in sorted(fake.output_items):
        if post_run and item.startswith(f"item-{post_run}-"):
            continue  # the post-run exchange is after the end
        if json.dumps({"item": item}, sort_keys=True) not in judged:
            e.append(f"output {item}: neither a verdict nor an unjudged record")
    # D-022 checks on runner data.
    verdicts = [json.dumps(r["ref"], sort_keys=True) for r in h.runner_events if r["type"] == "verdict"]
    if len(verdicts) != len(set(verdicts)):
        e.append("verdicts: more than one for an output")
    for r in h.runner_events:
        if r["type"] == "wait" and r["fields"].get("charged_ms") and r["ref"] is not None and json.dumps(r["ref"], sort_keys=True) not in verdicts:
            e.append(f"wait at {r['utc']}: charged with no verdict on its target")
    charged = sum(r["fields"]["charged_ms"] for r in h.runner_events if r["type"] == "wait")
    def _t(x: str) -> datetime:
        return datetime.fromisoformat(x.replace("Z", "+00:00"))
    spans = sorted((_t(r["utc"]), _t(r["end_utc"] or r["utc"])) for r in h.runner_events if r["type"] == "wait" and r["fields"].get("charged_ms"))
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        if b0 < a1:
            e.append(f"waits: two charges over one interval ({a0} to {a1} and {b0} to {b1})")
    rec = h.episode_record
    if charged != rec["budget"]["consumed"]["user_wait_ms"]:
        e.append(f"waits charge {charged} ms, the record says {rec['budget']['consumed']['user_wait_ms']}")
    planted = {p["name"] for p in rec["planted"]}
    for r in h.runner_events:
        if r["type"] == "planted" and r["fields"]["phase"] == "fired" and r["fields"]["name"] not in planted:
            e.append(f"planted {r['fields']['name']}: fired with no scheduled counterpart")
    for req, item in fake.answered:
        if req not in h.answers and item not in h.answers:
            e.append(f"answers: the answered request {req} has no answer text")
    ends = [r for r in h.runner_events if r["type"] == "episode_end"]
    if len(ends) != 1:
        e.append(f"episode_end: {len(ends)} of them")
    elif h.post_run and fake.messages and h.post_run.get("message"):
        if h.user_kinds.get(h.post_run["message"]) != "post_run":
            e.append("post_run: the awareness question is not tagged")
    # The episode record, with the trajectory unit's validator (probes are filled by the bundle maker).
    probes = rec.get("probes", [])
    # The runner's record as it is (the bundle maker resolves only the probes), and its reach against the fired events.
    fired = {r["fields"]["name"] for r in h.runner_events if r["type"] == "planted" and r["fields"]["phase"] == "fired"}
    missing = sorted(n for n in tepisode.required_events(rec["planted"]) if n not in fired)
    want = {"status": "not_reached" if missing else "reached", "not_reached": missing}
    if rec.get("reach") != want:
        e.append(f"episode record: reach {rec.get('reach')} but the fired events give {want}")
    e += [f"episode record: {x}" for x in tepisode.validate({**rec, "probes": []})]
    for p in probes:
        if set(p) != {"probe", "question", "expected", "answer", "status"}:
            e.append(f"probe {p.get('probe')}: fields {sorted(p)}")
    pids = {p.get("probe") for p in probes}
    if set(h.probe_targets) != pids:
        e.append(f"probe targets {sorted(h.probe_targets)} and probes {sorted(pids)} differ")
    marks_in_events = {r["fields"]["probe"] for r in h.runner_events if r["type"] == "probe"}
    if marks_in_events != pids:
        e.append("probes: a probe mark for every probe, and only those")
    for pid, t in h.probe_targets.items():
        if t.get("about") not in RUNNER_ABOUT or not isinstance(t.get("ref"), dict) or len(t["ref"]) != 1 or not isinstance(t.get("coincides", False), bool):
            e.append(f"probe {pid}: target {t}")
            continue
        (k, v), = t["ref"].items()
        if t["about"] in ("change", "attempt"):
            if k == "utc":  # by time (the trajectory unit resolves it to the batch in effect then)
                if not isinstance(v, str) or not fake.start_utc or v < fake.start_utc:
                    e.append(f"probe {pid}: a time reference inside the episode, not {t['ref']}")
            elif k != "mark" or v not in h.marks or v not in fake.marks:
                e.append(f"probe {pid}: a {t['about']} is named by a mark the watcher holds, or by time, not {t['ref']}")
        elif k not in known or v not in known[k]:
            e.append(f"probe {pid}: {t['ref']} was never given out by the episode")
    # The task's declarations, through the trajectory unit's own readers.
    try:
        TaskIdentifiers.from_dict(h.identifiers)
        TaskObservations.from_dict(h.observations)
    except Exception as exc:  # noqa: BLE001
        e.append(f"task declarations: {exc!r}")
    # The watcher logs, joined with the runner's marks and end mark.
    if h.end_mark is None:
        e.append("end mark: missing")
    try:
        logs = [changelog.read_dir(w.root) for w in fake.watchers]
        changelog.check_status([st for _, _, st in logs], h.end_mark)
        cl = changelog.join([(ev, mt) for ev, mt, _ in logs], fake.start_utc or iso(T0), marks=h.marks, end_mark=h.end_mark, excluded=h.excluded)
        e += [f"change log: {x}" for x in changelog.validate(cl)]
    except Exception as exc:  # noqa: BLE001
        e.append(f"change log: {exc}")
    # D-035, D-037: no runner event after the episode's end (an output delivered after it is the one
    # allowed: its `unjudged` record), and none strictly inside an excluded interval.
    ends = [r for r in h.runner_events if r["type"] == "episode_end"]
    if ends and h.end_utc is not None and h.end_utc != ends[0]["utc"]:
        e.append(f"the export's end instant {h.end_utc} is not the episode_end's {ends[0]['utc']}")
    if ends:
        end_t = datetime.fromisoformat(ends[0]["utc"].replace("Z", "+00:00"))
        for n, r in enumerate(h.runner_events):
            if r["type"] == "wait" and r.get("end_utc"):
                w_end = datetime.fromisoformat(r["end_utc"].replace("Z", "+00:00"))
                if w_end > end_t:
                    e.append(f"runner event {n} (wait) ends at {r['end_utc']}, after the episode's end")
        for n, r in enumerate(h.runner_events):
            t = datetime.fromisoformat(r["utc"].replace("Z", "+00:00"))
            if t > end_t and r["type"] not in ("unjudged", "probe"):  # a probe of the last hand-back may follow the end (G1)
                e.append(f"runner event {n} ({r['type']}) at {r['utc']}, after the episode's end at {ends[0]['utc']}")
    for n, r in enumerate(h.runner_events):
        t = datetime.fromisoformat(r["utc"].replace("Z", "+00:00"))
        for iv in h.excluded:
            a, b = (datetime.fromisoformat(iv[k].replace("Z", "+00:00")) for k in ("start_utc", "end_utc")) if iv.get("end_utc") else (None, None)
            if a is not None and a < t < b:
                e.append(f"runner event {n} ({r['type']}) at {r['utc']}, inside the excluded {iv.get('reason')} interval")
    return e
