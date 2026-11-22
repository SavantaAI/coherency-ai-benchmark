"""The episode loop: the user side of the conversation, the task's events,
reachability and budgets, for one episode on an `EpisodePort`.

README.md, "What the runner does", gives the rules in order. Every decision
is written to the event log as it is made.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cohbench.trajectory import format as F

from . import triggers
from .eventlog import EventLog, utc_iso
from .policy import AGENT, ASKING, AWARENESS, USER, Entry, PolicyError, UserPolicy, checked, compose
from .port import Delivery, EpisodeFailure, EpisodePort, MarkAck, Output, Pending, Sent, Turn, TurnNotRunning
from .task import Event, Task
from .words import message_words


@dataclass(frozen=True)
class RunConfig:
    episode_id: str
    harness: str
    model: str
    upstream: tuple[str, Path]
    mode: str  # default, coached
    poll_s: float = 5.0
    stall_s: float = 3600.0  # a turn with no hand-back for this long is a harness failure
    max_turns: int = 1000  # a safety bound on hand-backs, not a budget: reaching it is a runner abort
    awareness_timeout_s: float = 600.0
    usage_grace_s: float = 60.0  # with a token limit: unavailable or incomplete usage for longer is a driver failure
    scorer_id: str | None = None  # test and replay identity; random when omitted

    def __post_init__(self):
        import re

        if self.scorer_id is not None and (not isinstance(self.scorer_id, str) or re.fullmatch(r"e[0-9a-f]{16}", self.scorer_id) is None):
            raise ValueError("scorer_id: e followed by exactly 16 lower-case hex digits")


class _Ended(Exception):
    pass


@dataclass
class _EventState:
    status: str = "idle"  # idle, due, queued (a correction or a reset's notice steered, not yet taken), notice_sent (a reset), fired
    reached_utc: str | None = None
    fired_utc: str | None = None
    fired_dt: datetime | None = None  # when it happened: the card unlocks for outputs delivered at or after this
    delivery: str | None = None  # correction: mid_turn or next_message
    handback: int | None = None  # the hand-back the event fired at, or the one that closed its turn
    message: dict[str, str] | None = None


@dataclass
class _Seen:
    output: Output
    label: str
    reply: str | None  # the fixed reply the verdict maps to
    verdict: dict[str, Any]  # the runner event; its reply is set once the message is sent


@dataclass
class _Probe:
    event: Event
    handback: int  # the event's own hand-back
    target: dict[str, str] | None  # what it is about, once fixed
    waiting_next_output: bool = False
    missing: str | None = None  # why the target cannot be named (never taken then)


@dataclass
class _HB:
    """A hand-back as `_due_as_of` reads it."""

    h: int
    outs: list[Any]
    held_now: list[Any]
    marked: str | None
    secret: bool
    closing: Any


@dataclass
class _Due:
    """What is due at a hand-back as of an instant (`_due_as_of`)."""

    parts: list[str] = field(default_factory=list)
    meta: list[dict[str, Any]] = field(default_factory=list)
    causes: list[str] = field(default_factory=list)
    delivered: list[Any] = field(default_factory=list)
    used: list[tuple[Any, str]] = field(default_factory=list)
    replies_to: list[Any] = field(default_factory=list)
    reopen: Any = None
    override: bool = False

    @property
    def kind(self) -> str:
        if any(e.action.type == "correction" for e in self.delivered):
            return "correction"
        return "reopen" if self.reopen is not None else "reply"

    @property
    def cause(self) -> str:
        return next((c for c in ("verdict_reply", "fixed_message", "reopen", "refusal", "reset") if c in self.causes), "verdict_reply")


@dataclass
class _Queued:
    event: Event
    sent: Sent
    ref: dict[str, str]
    record: dict[str, Any]


@dataclass
class _Line:
    utc: datetime
    n: int
    entry: Entry
    meta: dict[str, Any] = field(default_factory=dict)


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _settled_fields(settled: bool | None) -> dict[str, Any]:
    """`mark_settled` on the correction_delivered event, once the trajectory
    unit's event table takes it; until then it is in the event log and the
    run manifest only."""
    return {"mark_settled": settled} if "mark_settled" in F.RUNNER_FIELDS["correction_delivered"] else {}


def _shown_as(kind: str, meta: list[dict[str, Any]]) -> str:
    """How the transcript files label a message the user sent: what it was made
    of, in order (a reset notice and a fixed task message are not replies to
    anything). The kind sent to the trajectory unit is unchanged."""
    names = {"verdict": "reply", "secret_refusal": "refusal", "reopen": "reopen", "opening_prompt": "opening prompt", "awareness": "post-run question"}
    actions = {"message": "fixed message", "correction": "correction", "reset_notice": "reset notice", "reset_continuation": "reset continuation"}
    out: list[str] = []
    for m in meta:
        label = actions.get(m.get("action", "")) if m.get("source") == "event" else names.get(m.get("source", ""))
        if label and label not in out:
            out.append(label)
    return ", ".join(out) or kind


# The least time left before the elapsed deadline for a dispatch to be attempted
# (D-037): a measured dispatch takes 0.6 to 1.1 s on the real driver, so with less
# than this left nothing is sent and the episode runs to its deadline.
DISPATCH_ALLOWANCE_S = 3.0

# How long coverage waits for a batch's metric reading record when the task has a
# metric trigger (G5); past it the episode is unusable.
METRIC_READING_WAIT_S = 30.0
# S5, G3: how long the runner waits for the driver to confirm a final output's
# hand-back once a budget end has come (the driver confirms the tree idle).
CONFIRM_WAIT_S = 5.0

# D-046: the user kind of a reset's notice steered into a running turn: its own
# kind once the trajectory unit's table has it, else a reply (the notice is the
# user's message, not a correction of the work).
NOTICE_KIND = "reset_notice" if "reset_notice" in F.USER_KINDS else "reply"  # never "correction"


def _has_metric(t: Any) -> bool:
    if not isinstance(t, dict) or len(t) != 1:
        return False
    (k, v), = t.items()
    if k == "metric":
        return True
    if k == "count":
        return any(_has_metric(x) for x in v["of"])
    if k in ("all", "any"):
        return any(_has_metric(x) for x in v)
    return k == "not" and _has_metric(v)


def utc_us(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Runner:
    def __init__(self, task: Task, cfg: RunConfig, ep: EpisodePort, policy: UserPolicy, log: EventLog,
                 *, sleep: Callable[[float], None] = time.sleep, utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.task, self.cfg, self.ep, self.policy, self.log = task, cfg, ep, policy, log
        self.scorer_id = cfg.scorer_id if cfg.scorer_id is not None else "e" + secrets.token_hex(8)  # the id in the scorer record: generated, opaque, never the caller's label
        self.sleep, self.utcnow = sleep, utcnow
        log.set_clock(self._log_clock)
        self.ctx = triggers.Context()
        self.ev = {e.name: _EventState() for e in task.events}
        self.lines: list[_Line] = []
        self.runner_events: list[dict[str, Any]] = []
        self.user_kinds: dict[str, str] = {}
        self.answers: dict[str, str] = {}
        self.submitted: dict[str, dict[str, str]] = {}
        self.classifier: list[dict[str, Any]] = []
        self.probes: list[dict[str, Any]] = []
        self.probe_targets: dict[int, dict[str, Any]] = {}
        self.probe_log: list[dict[str, Any]] = []
        self.due_probes: list[_Probe] = []
        self.marks: list[str] = []
        self.end_mark: str | None = None
        self.corrections: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []  # a reset's notice steered into a running turn (D-046), as a correction's record
        self.reset_info: dict[str, dict[str, Any]] = {}  # per reset event: how its notice went and when the reset came (D-046)
        self._notice_at: datetime | None = None  # the delivery instant of the notice of the reset in progress
        self._reset_how: dict[str, Any] | None = None  # the reset in progress came at a hand-back or at the grace
        self.queued: list[_Queued] = []
        self.sends: list[dict[str, Any]] = []
        self.seen: list[_Seen] = []
        self.closing: dict[int, Output | None] = {}
        self.user_wait_ms = 0
        self.reopens = 0
        self.tokens: int | None = None
        self.tokens_complete: bool | None = None
        self.usage_raw: dict[str, Any] = {}
        self.usage_bad_since: float | None = None
        self.start_clock: float | None = None
        self.agent = "idle"  # running, finished, pending
        self.pending: Pending | None = None
        self.reset_in_progress: str | None = None
        self.end: dict[str, Any] | None = None
        self.probes_not_taken: list[dict[str, Any]] = []
        self.not_reached: list[str] = []
        self.post_run: dict[str, Any] | None = None
        self.awareness: dict[str, Any] = {"asked": False, "reply": None, "outcome": None}
        self.awareness_record: dict[str, Any] | None = None
        self.mark_acks: dict[str, Any] = {}  # the runner's own marks' acknowledgements
        self.waiting_requests: dict[str, _Seen] = {}  # judged requests not answered yet, by item id
        self.corrections_held = False  # set when the port says there is no run to steer, until the next hand-back
        self.handback_log: list[dict[str, Any]] = []  # D-028: each hand-back with the output that closed it, or none
        self._last_stamp: datetime | None = None
        self._replayed_to: datetime | None = None  # the end of the last slice replayed
        self._final_replay = False  # the replay in progress is bounded by the episode's end
        self._carry: list[Output] = []  # outputs dated past the last confirmed instant
        self._pending_deliveries: dict[int, Delivery] = {}
        self._end_at: datetime | None = None
        self.overruns: list[dict[str, Any]] = []
        self._hb_at: datetime | None = None
        self._correction_sent: dict[str, Sent] = {}
        self._shared_wait = False
        self._mark_n = 0
        self._report_at: datetime | None = None  # when the driver reported the hand-back being processed
        self._final_unconfirmed = False
        self._coverage_failed = False
        self._waited: set[str] = set()  # outputs a wait has already answered
        self._noticed_ms: int | None = None
        self._uw_reached_at: datetime | None = None
        self._drained_handback: Turn | None = None
        self._last_wait: dict[str, Any] | None = None  # the wait just recorded, until its message goes
        self._last_wait_end: datetime | None = None
        self._wait_line: dict[str, Any] | None = None
        self._drained: list[Output] = []  # what a drain collected so far
        self._reads_metric = any(_has_metric(e.trigger) for e in task.events)  # when the user-wait allowance ran out (a wait's clip)
        self.after_deadline: list[dict[str, Any]] = []  # dispatches that landed after the deadline: audit only
        self._cap: datetime | None = None  # once a deadline has passed: no stamp after it
        self._t3_runs: set[str] = set()

    # ----- small helpers -----------------------------------------------------------------

    def _utc(self) -> str:
        return utc_iso(self.utcnow())

    def _stamp(self, not_before: str | datetime | None = None, *, capped: bool = True) -> str:
        """A runner event time: microseconds, never before `not_before` (the
        delivery of what the event refers to), never before the previous one."""
        t = self.utcnow()
        nb = _dt(not_before) if isinstance(not_before, str) else not_before
        for x in (self._last_stamp, nb):
            if x is not None and t < x:
                t = x
        if capped and self._cap is not None and t > self._cap:
            t = self._cap  # processing a hand-back that happened before the deadline, after it (D-037)
        self._last_stamp = t
        return utc_us(t)

    def _clear_ms(self) -> None:
        """Before a call the driver leaves out of the clock (a probe, a reset,
        the export): wait into the millisecond after the last stamp. The
        driver dates such an interval at millisecond precision, and no runner
        event may fall inside one."""
        if self._last_stamp is None:
            return
        floor = self._last_stamp.replace(microsecond=self._last_stamp.microsecond // 1000 * 1000)
        for _ in range(20):
            if (self.utcnow() - floor).total_seconds() >= 0.001:
                return
            self.sleep(0.0005)

    def _log_clock(self) -> float | None:
        if self.start_clock is None:
            return None
        try:
            return self.ep.clock() - self.start_clock
        except Exception:  # noqa: BLE001
            return None

    def _origin(self) -> datetime | None:
        try:
            return _dt(self.ep.origin_utc())
        except Exception:  # noqa: BLE001
            return None

    def elapsed_ms(self) -> int:
        """The episode clock now: from the clock's origin over the excluded
        intervals, whole milliseconds rounded down, the same conversion as
        every other instant's (no rounding branch). Without an origin, the
        port's monotonic clock since the opening prompt."""
        if self.start_clock is None:
            return 0
        origin = self._origin()
        if origin is not None:
            return self._clock_ms_between(origin, self.utcnow())
        return max(0, int((self.ep.clock() - self.start_clock) * 1000))

    def _elapsed_at(self, t: datetime | None) -> int:
        """The episode clock at the wall instant `t` (now when None): one
        conversion for every elapsed time (triggers, thresholds, classifier and
        transcript rows), from the clock's origin over the excluded intervals,
        whatever the instant is relative to the runner's clock."""
        if t is None:
            return self.elapsed_ms()
        origin = self._origin()
        if origin is not None:
            return self._clock_ms_between(origin, t)
        now = self.utcnow()
        now_ms = self.elapsed_ms()
        if t >= now:
            return now_ms + self._clock_ms_between(now, t)
        return max(0, now_ms - self._clock_ms_between(t, now))

    def _dispatch_ms(self, sent: Sent) -> int:
        """The episode clock at a dispatch: at its send instant, by the one conversion."""
        t = _dt(sent.utc)
        if t is not None and self._origin() is not None:
            return self._elapsed_at(t)
        return max(0, int((sent.clock_s - (self.start_clock or 0.0)) * 1000))

    def _rev(self, type: str, utc: str, ref: dict[str, str] | None = None, end_utc: str | None = None, **fields: Any) -> dict[str, Any]:
        """A runner event for the trajectory (trajectory.RunnerEvent's fields). An
        instant that a coarser clock puts strictly inside an excluded interval (the
        driver dates a reset's continuation to the millisecond, the handoff ends
        within that millisecond) is moved to the interval's end: the clock does not
        advance inside one, so the event happened at its end."""
        e = {"type": type, "utc": self._outside_excluded(utc), "ref": ref, "fields": fields, "end_utc": end_utc}
        self.runner_events.append(e)
        return e

    def _outside_excluded(self, utc: str) -> str:
        t = _dt(utc)
        if t is None:
            return utc
        try:
            ivs = list(self.ep.excluded())
        except Exception:  # noqa: BLE001
            return utc
        for iv in ivs:
            a, b = _dt(iv.start_utc), _dt(iv.end_utc) if iv.end_utc else None
            if a is not None and b is not None and a < t < b:
                return utc_us(b)
        return utc

    def _mark(self, why: str, at: datetime | None = None, *, label: str | None = None) -> tuple[str, tuple[int, int]]:
        """A mark naming the instant `at` (now when None, D-037): the watcher
        answers once everything written up to `at` is committed, and never
        closes a batch for it. Then the log is read, so what follows sees the
        mark's state. Returns the label and the position of the batch in
        effect at `at`; a straddled boundary is logged as such."""
        self._mark_n += 1
        label = label or f"m{self._mark_n}"  # never reused, even after a failed mark
        at_s = utc_us(at) if at is not None else None
        ack = self.ep.mark(label, at_s) if at_s is not None else self.ep.mark(label)
        self.marks.append(label)
        if ack is not None:
            self.mark_acks[label] = ack
        self._refresh()
        pos = self.ctx.work.marks.get(label)
        if pos is None:
            seg = max(len(self.ep.watcher_dirs()) - 1, 0)
            pos = (seg, ack.batch) if ack is not None and ack.batch is not None else self.ctx.work.position()
        self.log.write("mark", label=label, why=why, pos=list(pos), at=at_s, accepted=getattr(ack, "accepted", None),
                       straddled=getattr(ack, "straddled", None), covered_utc=getattr(ack, "covered_utc", None))
        return label, pos

    def _try_mark(self, why: str, at: datetime | None = None) -> tuple[str, tuple[int, int]] | None:
        """A mark whose non-fatal failure is logged and never fails the episode
        (the state is then read as it is); a fatal failure propagates."""
        try:
            return self._mark(why, at)
        except EpisodeFailure as exc:
            if exc.fatal:
                raise
            self.log.write("error", where=f"mark ({why})", error=repr(exc))
            self._refresh()
            return None

    def _refresh(self) -> None:
        self.ctx.work.refresh(self.ep.watcher_dirs())
        self.ctx.elapsed_ms = self.elapsed_ms()

    def _line(self, role: str, text: str, utc: str, **meta: Any) -> None:
        """A transcript row, with the episode clock at its own instant (m2), not at the read."""
        self.lines.append(_Line(_dt(utc), len(self.lines), Entry(role, text), {"utc": utc, "elapsed_ms": self._elapsed_at(_dt(utc)), **meta}))  # type: ignore[arg-type]

    def transcript(self, as_of: datetime | None = None) -> list[Entry]:
        """The user-visible transcript in order of delivery, as it stood at
        `as_of`: what the policy gets."""
        return [x.entry for x in self.ordered() if as_of is None or x.utc <= as_of]

    def ordered(self) -> list[_Line]:
        return sorted(self.lines, key=lambda x: (x.utc, x.n))

    def _fired_by(self, t: datetime | None) -> frozenset[str]:
        return frozenset(n for n, st in self.ev.items() if st.status == "fired" and (t is None or st.fired_dt is None or st.fired_dt <= t))

    def _need_item(self, o: Output) -> None:
        if not o.item_id:
            raise EpisodeFailure(f"the port reported a {o.kind} output without a T3 item id; the runner cannot name it")

    def _msg_ref(self, sent: Sent, pending: Pending | None = None) -> dict[str, str]:
        if sent.message_id:
            return {"message": sent.message_id}
        if pending is not None and pending.item_id:
            return {"item": pending.item_id}
        raise EpisodeFailure("the port returned a sent message without a T3 message id; the runner cannot name it")

    # ----- the episode -------------------------------------------------------------------

    def run(self) -> None:
        """Run to the episode's end and the awareness check. Never raises an
        Exception: a failure ends the episode with its reason."""
        try:
            self._start()
            self._loop()
        except _Ended:
            pass
        except PolicyError as exc:
            self._finish("runner_abort", f"user policy: {exc}", judge=False)
        except EpisodeFailure as exc:
            self._finish("driver_failure", f"driver: {exc}")
        except Exception as exc:  # noqa: BLE001 - a runner bug still ends the episode with a record
            self.log.write("error", where="loop", error=repr(exc), traceback=traceback.format_exc())
            self._finish("runner_abort", f"runner: {exc!r}", judge=False)
        self._awareness()

    def _end(self, reason: str, detail: str | None = None, budget: str | None = None, at: datetime | None = None) -> None:
        self._finish(reason, detail, budget, at)
        raise _Ended()

    def _unjudged(self, o: Output) -> None:
        if not o.item_id:
            return
        self._rev("unjudged", utc_us(_dt(o.delivered_utc)) if o.delivered_utc else self._stamp(), {"item": o.item_id}, reason="cutoff")
        self._line(AGENT, o.text, o.delivered_utc or self._utc(), label=None, output_kind=o.kind, item=o.item_id, unjudged="cutoff")
        self.log.write("unjudged", output={"item": o.item_id}, output_kind=o.kind, text=o.text, reason="cutoff")

    def _finish(self, reason: str, detail: str | None, budget: str | None = None, at: datetime | None = None, *, judge: bool = True) -> None:
        """The one ending path (D-035, D-037). The end instant is `at` (an
        elapsed cutoff's deadline on the episode clock, or a hand-back before
        it) or the moment the end was decided. Stop and drain the agent's
        work, take the end mark naming the end instant (which confirms the
        file log up to it), then one replay with the end filter: what the
        drain returned and every correction delivery first seen now, judged
        and settled when dated at or before the end, audit only after it.
        Record the end; the awareness check comes after. When the agent was
        actually stopped is `stopped_utc`, when the end was noticed
        `noticed_ms`, both audit only. `judge` False when the policy itself
        or the runner failed."""
        if self.end is not None:
            return
        if self._wait_line is not None:  # a wait whose message never went: the raw line says what the exported wait says
            self._flush_wait(self._last_wait["fields"].get("ended", "reply") if self._last_wait is not None else "budget_cutoff")
        notes: list[str] = []
        if self._noticed_ms is None:
            self._noticed_ms = self.elapsed_ms()
        # One end instant for the end mark, `episode_end` and the export's `end_utc` (never strictly
        # inside an excluded interval, where the clock does not advance).
        end_at = _dt(self._outside_excluded(utc_us(at or self.utcnow())))
        assert end_at is not None
        self._end_at = end_at
        stopped_utc: str | None = None
        drained: list[Output] = []
        if self.agent in ("running", "pending"):
            try:
                drained = self._interrupt_and_drain(f"ending: {reason}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"interrupt failed: {exc!r}")
                self.log.write("error", where="interrupt at the end", error=repr(exc))
                if isinstance(exc, EpisodeFailure) and reason in ("finished", "budget_cutoff"):
                    detail, reason, budget = f"{reason} ({detail}), then the tree could not be stopped", "driver_failure", None
            self.agent, self.pending = "finished", None
            stopped_utc = utc_us(self.utcnow())
        self._set_end_bound(end_at)  # nothing is stamped after the end, nothing after it counts (D-037)
        drained = list(self._drained) + drained  # what an earlier drain collected before it raised
        self._drained = []
        try:
            # The end mark (the same instant as `episode_end` and the export's `end_utc`) is the
            # final replay's coverage, with the readings when the task has a metric trigger (G5).
            self.end_mark = "end"
            self._replay_upto(drained, until=end_at, why="end", label="end", final=True, judge=judge)
            ack = self.mark_acks.get("end")
            if ack is not None and ack.straddled is not None:
                # At an elapsed cutoff a batch may straddle the end (its writes run past the
                # deadline): accepted, outside the episode by decision (D-035), recorded, no retry.
                self.log.write("end_mark_straddled", straddled=ack.straddled, straddled_last=ack.straddled_last, batch=ack.batch,
                               elapsed_cutoff=budget == "time")
        except EpisodeFailure as exc:
            notes.append(f"end mark failed: {exc!r}")
            self.log.write("error", where="end mark", error=repr(exc))
            if reason in ("finished", "budget_cutoff"):
                detail, reason, budget = f"{reason} ({detail}), then the watcher did not confirm the file log up to the end", "driver_failure", None
        except PolicyError as exc:
            notes.append(f"user policy at the end: {exc}")
            if reason in ("finished", "budget_cutoff"):
                detail, reason, budget = f"{reason} ({detail}), then the user policy failed on an output inside the episode", "runner_abort", None
        except Exception as exc:  # noqa: BLE001
            notes.append(f"replay at the end failed: {exc!r}")
            self.log.write("error", where="replay at the end", error=repr(exc), traceback=traceback.format_exc())
        hb = self._drained_handback
        if hb is not None and hb.at and _dt(hb.at) is not None and _dt(hb.at) <= end_at:  # type: ignore[operator]
            # A hand-back the drain confirmed, before the end: recorded with its closing item and instant.
            self.ctx.handbacks += 1  # the manifest's count agrees with the hand-back log
            if hb.state == "finished":
                self.ctx.turns += 1
            self.handback_log.append({"utc": utc_us(_dt(hb.at)), "handback": self.ctx.handbacks, "closing": hb.closing,  # type: ignore[arg-type]
                                      "kind": "finished" if hb.state == "finished" else "pending"})
            self.log.write("hand_back", handback=self.ctx.handbacks, state=hb.state, closing=hb.closing, drained=True)
        self._refresh_mechanisms()
        read_at = self.utcnow()
        try:
            u = self.ep.usage()
            self.tokens, self.tokens_complete, self.usage_raw = u.tokens, u.complete, dict(u.raw)
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="usage at the end", error=repr(exc))
        cap = self.task.budgets["tokens"]
        if reason in ("finished", "budget_cutoff") and cap is not None and self.tokens is not None and self.tokens >= cap:
            # The final usage read is a candidate end like any other (G3): a token cutoff at the
            # instant this read began, against the end already chosen; the earlier stands.
            _at, r2, b2 = self._choose_end([(end_at, reason, budget), (read_at, "budget_cutoff", "tokens")])
            if (r2, b2) != (reason, budget):
                reason, budget, detail = r2, b2, f"tokens budget reached at the final usage read ({self.tokens} of {cap})"
        # Nothing after the end counts (D-037, G4): an event reached or fired after the end
        # instant, in a replay that ran before the end was known, is not reached.
        late = [n for n, st in self.ev.items() if st.status in ("due", "fired", "queued", "notice_sent")
                and (_dt(st.reached_utc) or end_at) > end_at]
        if late:
            self.runner_events = [x for x in self.runner_events if not (x["type"] == "planted" and x["fields"].get("name") in late)]
            self.due_probes = [p for p in self.due_probes if p.event.name not in late]  # an event not reached has no probe due
            for n in late:
                self.ev[n].status = "idle"
                self.ctx.fired.pop(n, None)
            self.log.write("events_after_the_end", names=late)
        end_utc = utc_us(end_at)
        if budget:
            self._rev("budget_cutoff", end_utc, budget=budget)
        for e in self.task.events:
            if self.ev[e.name].status != "fired":
                self._rev("planted", end_utc, name=e.name, phase="not_reached")
        self.not_reached = [e.name for e in self.task.events if e.required and self.ev[e.name].status != "fired"]
        self._rev("episode_end", end_utc, reason=reason, **({"stopped_utc": stopped_utc} if "stopped_utc" in F.RUNNER_FIELDS["episode_end"] else {}))
        if notes:
            detail = "; ".join(([detail] if detail else []) + notes)
        elapsed = self.task.budgets["elapsed_ms"] if at is not None and budget == "time" else self._clock_ms_at(end_at)
        self.end = {"reason": reason, "detail": detail, "budget": budget, "utc": end_utc, "elapsed_ms": elapsed, "stopped_utc": stopped_utc,
                    "noticed_ms": self._noticed_ms}
        self._cap = None
        self.probes_not_taken = [{"event": p.event.name, "about": p.event.probe.about, "question": p.event.probe.question,
                                  "target_fixed": p.target is not None, "missing": p.missing} for p in self.due_probes if p.event.probe]
        self.log.write("episode_end", reason=reason, detail=detail, budget=budget, elapsed_ms=self.end["elapsed_ms"], end_utc=end_utc,
                       stopped_utc=stopped_utc, noticed_ms=self.end["noticed_ms"], end_mark=self.end_mark,
                       user_wait_ms=self.user_wait_ms, not_reached=self.not_reached, probes_not_taken=self.probes_not_taken)

    def _refresh_mechanisms(self) -> None:
        """m7: a correction's route can be resolved after its delivery was
        settled; every mid-turn correction still without one is asked again."""
        for name, sent in self._correction_sent.items():
            rec = next((r for r in self.corrections + self.notices if r["name"] == name and r["delivery"] == "mid_turn"), None)
            if rec is None or rec.get("mechanism"):
                continue
            try:
                d = self.ep.correction_delivery(sent)
            except Exception as exc:  # noqa: BLE001
                self.log.write("error", where="mechanism at the end", name=name, error=repr(exc))
                continue
            if d is not None and d.mechanism:
                rec["mechanism"] = d.mechanism
                self.log.write("correction_mechanism", name=name, queued=None, settled=d.mechanism, at="end")

    def _interrupt_and_drain(self, why: str) -> list[Output]:
        """Stop the whole tree, then wait until the driver reports it stopped.
        Returns every output it reported meanwhile, for the caller's replay
        (B2, D-037): nothing the driver captured is left out."""
        self.ep.interrupt()
        self.log.write("interrupt", why=why)
        # What each read returns is kept in `_drained` until the caller takes it, so
        # a read that raises after an earlier one returned outputs loses none: the
        # end replay records them.
        for _ in range(10):
            t = self.ep.wait(min(self.cfg.poll_s, 5.0))  # a failure here propagates (m2)
            self.log.write("drain", state=t.state, outputs=len(t.outputs))
            self._drained += list(t.outputs)
            if t.state in ("finished", "question", "secret"):
                self._drained_handback = t  # its metadata is kept (S5)
            if t.state != "time_limit":
                got, self._drained = self._drained, []
                return got
        raise EpisodeFailure("the tree did not read stopped after the interrupt (ten waits)")

    def _start(self) -> None:
        self._guard(self.task.opening)
        sent = self.ep.start(self.task.opening)
        self.start_clock = sent.clock_s
        self.agent = "running"
        ref = self._msg_ref(sent)
        self._record_send(sent, self.task.opening, "opening_prompt", "start", [{"source": "opening_prompt"}], ref=ref)
        self._line(USER, self.task.opening, sent.utc, kind="opening_prompt", ref=ref, shown_as="opening prompt")
        self._refresh()

    def _slice(self) -> float:
        s = self.cfg.poll_s
        lim = self.task.budgets["elapsed_ms"]
        if lim is not None:
            s = min(s, max(self._remaining_s() or 0.0, 0.001))
        g = self._grace_at()
        if g is not None:
            s = min(s, max((g - self.utcnow()).total_seconds(), 0.001))  # the runner looks again at the grace
        return s

    def _grace_at(self) -> datetime | None:
        """D-046: the instant a reset's grace runs out: `grace_ms` of episode time
        after its notice's delivery, counted over the excluded intervals; None when
        no notice is delivered and waiting for its reset."""
        if self.reset_in_progress is None or self._notice_at is None:
            return None
        return self._wall_after(self._notice_at, self.task.event(self.reset_in_progress).action.grace_ms)

    def _deadline_at(self) -> datetime | None:
        """The wall instant at which the elapsed budget runs out (past or future),
        or None: from the clock's origin over the excluded intervals."""
        lim = self.task.budgets["elapsed_ms"]
        if lim is None:
            return None
        ahead = self._wall_forward(lim)
        if ahead is not None:
            return ahead
        now_ms = self.elapsed_ms()
        return self._wall_back(now_ms - lim) if now_ms >= lim else self.utcnow() + timedelta(milliseconds=lim - now_ms)

    def _remaining_s(self) -> float | None:
        """Seconds left before the elapsed deadline, for a dispatch's own limit (D-037)."""
        at = self._deadline_at()
        return None if at is None else max(0.0, (at - self.utcnow()).total_seconds())

    def _deadline(self) -> datetime | None:
        """The instant the elapsed budget ran out, if it has: on the episode
        clock, not when the runner noticed (D-035). Compared as instants."""
        at = self._deadline_at()
        return at if at is not None and at <= self.utcnow() else None

    def _wall_forward(self, ms: float) -> datetime | None:
        """The wall instant at which the episode clock reads `ms`, counted
        forward from the clock's origin (the instant the scoring bundle counts
        from) over every excluded interval that began before it. The same
        value on every call, to the microsecond: it does not depend on when
        the runner asks. None when the port gives no origin, or when an
        interval that began before the instant is still open."""
        try:
            origin = _dt(self.ep.origin_utc())
            ivs = sorted(((_dt(i.start_utc), _dt(i.end_utc) if i.end_utc else None) for i in self.ep.excluded() if i.start_utc), key=lambda x: x[0])  # type: ignore[arg-type, return-value]
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="the clock origin for the deadline", error=repr(exc))
            return None
        if origin is None:
            return None
        t = origin + timedelta(milliseconds=ms)
        for s0, s1 in ivs:
            if s0 is None or s0 >= t:
                continue
            if s1 is None:
                return None
            t += s1 - max(s0, origin)
        return t

    def _wall_back(self, ms: float) -> datetime:
        """The wall instant `ms` of episode clock before now: walking back over
        every excluded interval (a probe, a handoff), in which the clock does
        not advance, so the instant never falls strictly inside one."""
        t, left = self.utcnow(), float(ms)
        try:
            ivs = sorted(((_dt(i.start_utc), _dt(i.end_utc)) for i in self.ep.excluded() if i.start_utc and i.end_utc), key=lambda x: x[1], reverse=True)  # type: ignore[arg-type, return-value]
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="excluded intervals for the deadline", error=repr(exc))
            ivs = []
        for s0, s1 in ivs:
            if s0 is None or s1 is None or s1 > t:
                continue
            gap = (t - s1) / timedelta(milliseconds=1)
            if gap >= left:
                break
            left -= gap
            t = s0
        return t - timedelta(milliseconds=left)

    def _budget_candidates(self) -> list[tuple[datetime, str, str | None]]:
        """The budget ends known now, each with its own instant (G3): a token cutoff
        at the instant the usage read that showed the cap began, a user-wait cutoff
        at the clip of the wait that used the allowance up, the elapsed deadline.
        Usage unavailable for longer than the grace period is a driver failure."""
        lim = self.task.budgets
        read_at = self.utcnow()
        u = self.ep.usage()
        self.tokens, self.tokens_complete, self.usage_raw = u.tokens, u.complete, dict(u.raw)
        out: list[tuple[datetime, str, str | None]] = []
        if lim["tokens"] is not None:
            if u.tokens is None or not u.complete:
                now = self.ep.clock()
                if self.usage_bad_since is None:
                    self.usage_bad_since = now
                    self.log.write("usage_unreliable", tokens=u.tokens, complete=u.complete)
                elif now - self.usage_bad_since > self.cfg.usage_grace_s:
                    self._end("driver_failure", f"token usage unavailable or incomplete for more than {self.cfg.usage_grace_s:g} s with a token limit set")
            else:
                self.usage_bad_since = None
            if u.tokens is not None and u.tokens >= lim["tokens"]:
                out.append((read_at, "budget_cutoff", "tokens"))
        if lim["user_wait_ms"] is not None and self.user_wait_ms >= lim["user_wait_ms"]:
            out.append((self._uw_reached_at or read_at, "budget_cutoff", "user_wait"))
        deadline = self._deadline()
        if deadline is not None:
            out.append((deadline, "budget_cutoff", "time"))
        return out

    @staticmethod
    def _choose_end(cands: list[tuple[datetime, str, str | None]]) -> tuple[datetime, str, str | None]:
        """The one place an end is chosen (G3): of the candidate ends known now (a
        hand-back's finish, a wait's reply, the elapsed deadline, a user-wait clip, a
        token cutoff), the earliest, with its reason. On a tie the budget wins: a budget
        seen exhausted at an instant was exhausted by it, so neither a finish nor a reply
        at that same instant comes first."""
        order = {"budget_cutoff": 0, "finished": 1, "grace": 2, "reply": 3}
        return min(cands, key=lambda c: (c[0], order.get(c[1], 1)))

    def _end_earliest(self, cands: list[tuple[datetime, str, str | None]], why: str | None = None) -> None:
        at, reason, budget = self._choose_end(cands)
        if reason == "finished":
            self._end("finished", why, at=at)
        self._cutoff(budget or "time", at=at)

    def _check_budgets(self) -> None:
        """All three budgets read again (before every dispatch too, D-035), the elapsed
        deadline last; when any is exhausted the episode ends at the earliest. A final
        output whose hand-back the driver has not confirmed yet gets one more wait
        first: a hand-back before the earliest budget end is a candidate too (G3)."""
        cands = self._budget_candidates()
        if not cands:
            return
        if self._final_unconfirmed and self.agent == "running":
            self._confirm_late_hand_back(self._choose_end(cands)[0])
        self._end_earliest(cands)

    def _cutoff(self, budget: str, at: datetime | None = None) -> None:
        if self._noticed_ms is None:
            self._noticed_ms = self.elapsed_ms()  # when the cutoff was detected (audit)
        self.log.write("budget_cutoff", budget=budget, elapsed_ms=self.elapsed_ms(), tokens=self.tokens, user_wait_ms=self.user_wait_ms, agent=self.agent,
                       at=utc_us(at) if at else None)
        self._end("budget_cutoff", f"{budget} budget reached", budget, at=at)

    def _coverage(self, why: str, at: datetime, label: str | None = None) -> str:
        """D-037: before anything is judged or a trigger evaluated up to `at`,
        the watcher confirms everything written up to `at` is committed (a
        mark naming `at`; it waits for the batching, never closes a batch). If
        it cannot within the mark's time limit, the episode is unusable and
        nothing is judged on that log. Only `_replay_upto` calls it."""
        try:
            label, _ = self._mark(why, at, label=label)
        except EpisodeFailure as exc:
            raise EpisodeFailure(f"the watcher did not confirm the file log up to {utc_us(at)}: {exc}") from exc
        ack = self.mark_acks.get(label)
        if ack is not None and ack.accepted is False:
            raise EpisodeFailure(f"the watcher did not confirm the file log up to {utc_us(at)} (mark {label} not accepted)")
        if self._reads_metric:
            # G5: with a metric trigger, coverage also needs every batch in effect up to `at`
            # to have its reading record; waited for within METRIC_READING_WAIT_S.
            waited = 0.0
            while True:
                self._refresh()
                missing = self.ctx.work.unread_batches(at)
                if not missing:
                    break
                if waited >= METRIC_READING_WAIT_S:
                    raise EpisodeFailure(f"the watcher gave no metric reading for batches {missing} within {METRIC_READING_WAIT_S:g} s")
                self.sleep(0.5)
                waited += 0.5
        return label

    def _replay_upto(self, outputs: tuple[Output, ...] | list[Output], deliveries: list[tuple[datetime, _Queued, Delivery]] | None = None, *,
                     until: datetime, why: str, final: bool = False, judge: bool = True, label: str | None = None) -> str:
        """The one entry point that replays observations up to an instant, and the
        only way outputs get judged: the normal slice, the reset drain, the late
        confirmation, the dispatch instant and the final replay all call it with
        their bound. It always applies the deadline bound (`until` is clipped to the
        elapsed deadline and the episode's end, so nothing after either is judged),
        and always requires coverage first (the file log confirmed up to the bound,
        and with a metric trigger every reading too). When coverage fails, the
        outputs are kept: carried to the end replay, or at the end recorded
        unjudged; none is dropped. `final`: the bound is the episode's end.
        Returns the coverage mark's label."""
        self._bound_from_deadline()
        dl = self._deadline()
        if dl is not None:
            self._set_end_bound(dl)
        bound = until
        ahead = self._deadline_at()
        for b in (ahead, self._cap):
            if b is not None and b < bound:
                bound = b
        final = final or (dl is not None and bound >= dl)
        if deliveries is None:
            deliveries = self._observe_deliveries()
        try:
            label = self._coverage(why, bound, label)
        except EpisodeFailure:
            self._coverage_failed = True
            if final:
                self._replay(outputs, deliveries, until=bound, judge=False, final=True)  # recorded unjudged
            else:
                self._carry += list(outputs)  # recorded at the end, unjudged (no confirmed log to judge them on)
                self._requeue(deliveries)
            raise
        self._replay(outputs, deliveries, until=bound, judge=judge and not self._coverage_failed, final=final)
        return label

    def _loop(self) -> None:
        turn_t0 = self.ep.clock()
        while True:
            self._check_budgets()
            turn = self.ep.wait(self._slice())
            self.log.write("wait_result", state=turn.state, run=turn.run_id, outputs=len(turn.outputs), closing=turn.closing, detail=turn.detail,
                           started_by_t3=list(turn.started_by_t3))
            handback = turn.state in ("finished", "question", "secret")
            new_t3 = [x for x in turn.started_by_t3 if x not in self._t3_runs]
            if new_t3:
                self._t3_runs.update(new_t3)
                if self.corrections_held and turn.state == "time_limit":
                    self.corrections_held = False  # T3's continuation run can be steered: try again (D-029)
                    self.log.write("correction_hold_released", runs=new_t3)
            slice_at = self.utcnow()  # the instant this slice covers up to, and the instant the hand-back was reported
            self._report_at = slice_at
            hb_instant = (_dt(turn.at) if turn.at else None) or slice_at  # when the hand-back happened, as the driver saw it
            if turn.state != "time_limit":
                self.agent = "pending" if turn.pending else "finished"
                self.pending = turn.pending
            elif any(o.kind == "final" for o in turn.outputs):
                self._final_unconfirmed = True  # a final output whose hand-back the driver has not confirmed yet
            deliveries = self._observe_deliveries()
            # The newest instant this slice replays: now, or a delivery dated a hair later on
            # the driver's clock; `_replay_upto` clips it to the deadline. One mark per slice.
            newest = max([slice_at, *(t for t, _q, _d in deliveries), *(d for o in turn.outputs for d in [_dt(o.delivered_utc)] if d is not None)])
            self._replay_upto(turn.outputs, deliveries, until=newest, why="hand-back" if handback else "coverage")
            g = self._grace_at()
            if g is not None and ((handback and hb_instant > g) or (not handback and self.utcnow() >= g)):
                # D-046: the reset's grace ran out before any hand-back. Which came first is
                # decided from instants, with the budget ends, by the one function.
                if self._grace_first(g):
                    turn_t0 = self.ep.clock()
                    continue
            deadline = self._deadline()  # read after coverage and replay, which take time (judging) (D-037)
            if deadline is not None:
                self._set_end_bound(deadline)
                if handback and hb_instant <= deadline:
                    # A hand-back before the deadline, processed after it: its wait runs to the
                    # deadline and nothing is sent (D-037).
                    self._hand_back(turn, hb_instant)
                elif not handback and self._final_unconfirmed:
                    self._confirm_late_hand_back(deadline)
                self._cutoff("time", at=deadline)
            if turn.state == "time_limit":
                self._check_budgets()
                self._mid_turn_corrections()
                if self.ep.clock() - turn_t0 > self.cfg.stall_s:
                    self._end("harness_failure", f"no hand-back within {self.cfg.stall_s:g} s")
                continue
            if turn.state in ("failed", "refused", "interrupted"):
                self._end("harness_failure", f"turn {turn.state}" + (f": {turn.detail}" if turn.detail else ""))
            if turn.state not in ("finished", "question", "secret"):
                raise EpisodeFailure(f"unknown turn state {turn.state!r}")
            if turn.state in ("question", "secret") and (turn.pending is None or turn.pending.kind != turn.state or not turn.pending.item_id):
                raise EpisodeFailure(f"a {turn.state} hand-back without its pending request and T3 item id")
            self._hand_back(turn, hb_instant)
            turn_t0 = self.ep.clock()

    def _grace_first(self, g: datetime) -> bool:
        """The grace instant against the budget ends known now (`_choose_end`): a
        budget end that came first ends the episode; otherwise a final output not
        confirmed yet gets its one more wait (a hand-back before the grace resets
        there), and else the runner interrupts, drains and resets at the grace.
        True when the reset was performed."""
        cands = self._budget_candidates()
        _at, reason, _b = self._choose_end(cands + [(g, "grace", None)])
        if reason != "grace":
            self._end_earliest(cands)
        if self._final_unconfirmed and self.agent == "running":
            self._confirm_late_hand_back(g)
            if self.reset_in_progress is None:
                return True  # reset at that hand-back
        self._grace_reset(g)
        return True

    def _grace_reset(self, g: datetime) -> None:
        """D-046: the agent did not hand back within the grace: the runner interrupts
        and drains the tree (what the drain returns goes through the one replay), and
        resets as at a hand-back whose instant is the grace's. The outputs since the
        notice are judged and get no reply."""
        e = self.task.event(self.reset_in_progress)  # type: ignore[arg-type]
        self.log.write("reset_grace", name=e.name, at=utc_us(g), grace_ms=e.action.grace_ms)
        drained = self._interrupt_and_drain(f"the reset's grace of {e.action.grace_ms} ms ran out")
        stopped = utc_us(self.utcnow())
        at = max([self.utcnow(), *(d for o in drained for d in [_dt(o.delivered_utc)] if d is not None)])
        self._replay_upto(drained, until=at, why="reset")
        self.agent, self.pending, self._final_unconfirmed, self._drained_handback = "finished", None, False, None
        self._hb_at = g
        self._reset_how = {"at": "grace", "utc": utc_us(g), "stopped_utc": stopped}
        self.ctx.handbacks += 1
        h = self.ctx.handbacks
        self.closing[h] = None
        self.corrections_held = False
        self._shared_wait = False
        self.handback_log.append({"utc": utc_us(g), "handback": h, "closing": None, "kind": "interrupted"})
        self.log.write("hand_back", handback=h, turns=self.ctx.turns, state="interrupted", closing=None, utc=utc_us(g))
        self._evaluate(h, as_of=g)
        hb = _HB(h, list(self.seen), [], None, False, None)
        self.seen = []
        self._do_reset(hb, self._budget_candidates())

    def _set_end_bound(self, end: datetime) -> None:
        """The episode's end is known (an elapsed deadline): no runner event is
        stamped after it, and file activity after it is outside the episode."""
        self._cap = end if self._cap is None else min(self._cap, end)
        self.ctx.work.end_bound = self._cap

    def _bound_from_deadline(self) -> None:
        """G2: the elapsed deadline's wall instant is known from the origin before it
        arrives (given the excluded intervals so far), so it bounds every replay from
        the start: a batch whose last write is after it is outside the trigger view.
        Recomputed every slice, since an excluded interval moves it."""
        lim = self.task.budgets["elapsed_ms"]
        if lim is None:
            return
        ahead = self._wall_forward(lim)
        if ahead is not None:
            self.ctx.work.end_bound = min(ahead, self._cap) if self._cap is not None else ahead

    def _requeue(self, deliveries: list[tuple[datetime, _Queued, Delivery]]) -> None:
        for _t, q, d in deliveries:
            self.queued.append(q)
            self._pending_deliveries[id(q)] = d

    def _confirm_late_hand_back(self, deadline: datetime) -> None:
        """S5: the driver reported a final output in a `time_limit` slice while it
        confirmed the tree was idle, and a budget end (`deadline`: the elapsed
        deadline or the earliest budget end) came first. One more wait lets it
        confirm: a hand-back that happened before it keeps its closing item, its
        instant and its wait (cut at the earliest end, nothing sent); otherwise
        what it reported goes to the end replay."""
        self._final_unconfirmed = False
        t = self.ep.wait(CONFIRM_WAIT_S)  # fixed, whatever the polling interval
        self.log.write("wait_result", state=t.state, run=t.run_id, outputs=len(t.outputs), closing=t.closing, detail=t.detail,
                       started_by_t3=list(t.started_by_t3), confirming=True)
        at = (_dt(t.at) if t.at else None)
        if t.state in ("finished", "question", "secret") and at is not None and at <= deadline:
            self.agent = "pending" if t.pending else "finished"
            self.pending = t.pending
            self._report_at = self.utcnow()
            newest = max([at, *(d for o in t.outputs for d in [_dt(o.delivered_utc)] if d is not None)])
            self._replay_upto(t.outputs, until=newest, why="hand-back", final=True)
            self._hand_back(t, at)
        else:
            self._carry += list(t.outputs)

    # ----- one timeline -------------------------------------------------------------------

    def _replay(self, outputs: tuple[Output, ...] | list[Output], deliveries: list[tuple[datetime, _Queued, Delivery]] | None = None, *,
                until: datetime | None = None, judge: bool = True, final: bool = False) -> None:
        """A slice as a replay in time order of everything that happened in
        it (V2, S3, B3): watcher batches beginning and coming into effect,
        elapsed-time thresholds (also those that become known when their
        `since` event fires during the replay), correction deliveries and
        outputs. At each instant the triggers read the state, the fired events
        and the counters as of that instant, events fire at that instant, and
        an output is judged with the card and transcript of its own instant.
        So the same episode gets the same verdicts and firings at any polling
        interval. Called only through `_replay_upto`, which bounds and covers
        it. `until`: how far the slice is replayed (the instant the
        watcher confirmed, never past the episode's end); an output or a
        delivery after it is audit only (an output `unjudged`) and nothing
        after it is replayed; the triggers are evaluated at `until` itself
        too. The same replay serves a slice, the drain at a cutoff and the
        end. `judge` False: every output is recorded `unjudged` (the policy
        itself failed). `final`: `until` is the episode's end; otherwise an
        output or delivery dated after it waits for the next slice's replay."""
        now = self.utcnow()
        end = until if until is not None else now  # the confirmed instant (it can be a hair past the runner's clock)
        self._final_replay = final
        if deliveries is None:
            deliveries = self._observe_deliveries()
        outs = sorted(list(self._carry) + list(outputs), key=lambda o: (_dt(o.delivered_utc) or now))
        self._carry = []
        after = [o for o in outs if (_dt(o.delivered_utc) or now) > end]
        if not final and judge:
            self._carry = after  # dated past the confirmed instant: replayed with the next slice
            outs = [o for o in outs if o not in after]
        late = [o for o in outs if not judge or (_dt(o.delivered_utc) or now) > end]
        for o in late:
            self._unjudged(o)  # delivered after what is replayed (the episode's end, D-035)
        outs = [o for o in outs if o not in late]
        at: dict[datetime, list[Any]] = {end: []}
        for t in self.ctx.work.fresh_instants(end) + self._elapsed_instants(self._replayed_to, end):
            at.setdefault(t, [])
        for t, q, d in deliveries:
            if t > end:
                self._delivery_after(q, d)
                continue
            at.setdefault(t, []).append(("delivery", q, d))
        for o in outs:
            at.setdefault(_dt(o.delivered_utc) or now, []).append(("output", o))
        pending = list(outs)
        done: set[datetime] = set()
        while True:
            todo = sorted(t for t in at if t not in done)
            if not todo:
                break
            t = todo[0]
            done.add(t)
            for x in at[t]:
                if x[0] == "delivery":
                    self._deliver(x[1].event, x[1].ref, x[2], x[1].record, self.ctx.handbacks + 1)
            self._evaluate(as_of=t)
            judged = False
            for x in at[t]:
                if x[0] != "output":
                    continue
                try:
                    self._output(x[1], t)
                    pending.remove(x[1])
                    judged = True
                except PolicyError:
                    for rest in pending:
                        self._unjudged(rest)
                    raise
            if judged:
                self._evaluate(as_of=t)  # the output and its verdict count from this instant
            for t2 in self._elapsed_instants(t, end):  # m3: a threshold that became known at t joins this replay
                at.setdefault(t2, [])
        self._replayed_to = max(end, self._replayed_to) if self._replayed_to else end

    def _delivery_after(self, q: _Queued, d: Delivery) -> None:
        """A correction delivered after what is replayed (after the episode's
        end): audit only, never fired (D-037). If the episode goes on it is
        replayed with the next slice."""
        if not self._final_replay:
            self.queued.append(q)  # not past the end: the next slice replays it at its own instant
            self._pending_deliveries[id(q)] = d
            return
        q.record["delivered_utc"], q.record["after_end"] = d.utc, True
        if d.mark is not None and d.mark not in self.marks:
            self.marks.append(d.mark)  # the driver's mark is in the watcher log; the runner names it, though nothing counts from it
        self.log.write("delivery_after_end", name=q.event.name, utc=d.utc, mark=d.mark)

    def _observe_deliveries(self) -> list[tuple[datetime, _Queued, Delivery]]:
        """Correction deliveries the driver now reports, each once, in time order."""
        out = []
        for q in list(self.queued):
            d = self._pending_deliveries.pop(id(q), None) or (
                Delivery(q.sent.delivered_utc, q.sent.delivery_mark, q.sent.delivery_settled, q.sent.delivery_quiescent,
                         straddled=q.sent.delivery_straddled, straddled_last=q.sent.delivery_straddled_last) if q.sent.delivered_utc
                else self.ep.correction_delivery(q.sent))
            if d is not None and d.utc:
                self.queued.remove(q)
                out.append((_dt(d.utc), q, d))
        return sorted(out, key=lambda x: x[0])  # type: ignore[arg-type, return-value]

    def _elapsed_instants(self, after: datetime | None, until: datetime) -> list[datetime]:
        """When an idle event's `elapsed_ms` threshold was crossed in (after, until]."""
        now = self.utcnow()
        el_now = self._elapsed_at(now)
        out = []

        def walk(t: Any) -> None:
            if not isinstance(t, dict) or len(t) != 1:
                return
            (k, v), = t.items()
            if k in ("all", "any"):
                for x in v:
                    walk(x)
            elif k == "count":
                for x in v["of"]:
                    walk(x)
            elif k == "not":
                walk(v)
            elif k == "elapsed_ms":
                base = 0
                if "since" in v:
                    snap = self.ctx.fired.get(v["since"])
                    if snap is None:
                        return
                    base = snap.elapsed_ms
                target = base + v["at_least"]
                if target <= el_now:
                    t_at = self._wall_forward(target) or now - timedelta(milliseconds=el_now - target)  # forward from the origin
                    if (after is None or t_at > after) and t_at <= until:
                        out.append(t_at)

        for e in self.task.events:
            if self.ev[e.name].status == "idle":
                walk(e.trigger)
        return out

    # ----- outputs and verdicts ------------------------------------------------------------

    def _output(self, o: Output, t: datetime) -> None:
        """Judge one output, once, against the transcript and card as they stood at its delivery."""
        self._need_item(o)
        self.ctx.outputs += 1
        card = self.task.card_for(self._fired_by(t))
        texts = Task.card_texts(card)  # before the call: what the policy does to its copy changes nothing here
        tr = self.transcript(as_of=t)
        try:
            j = self.policy.judge(card, tuple(tr), o.text)
        except PolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - the policy is outside the runner; its crash is a runner abort
            raise PolicyError(f"judge raised {exc!r}") from exc
        j = checked(j)
        reply = compose(j, texts, tr, self.task.replies.unanswerable, self.task.replies.already_given)
        # One delivery time (D-027, sixth trajectory review): the verdict, the
        # classifier row and the judging instant are all the driver's delivery
        # time of the item, never the time the runner read it.
        utc = utc_us(t) if o.delivered_utc else self._stamp()
        verdict = self._rev("verdict", utc, {"item": o.item_id}, label=j.label, reply=None)  # type: ignore[dict-item]
        self.ctx.verdicts[j.label] += 1
        self.seen.append(_Seen(o, j.label, reply, verdict))
        self._line(AGENT, o.text, o.delivered_utc or utc, label=j.label, output_kind=o.kind, item=o.item_id)
        row = {"utc": utc, "elapsed_ms": self._elapsed_at(t), "output": {"item": o.item_id}, "output_kind": o.kind, "text": o.text, "label": j.label,
               "answer_ids": list(j.answer_ids), "quote_of": j.quote_of, "quote": j.quote, "reply": reply, "record": dict(j.record),
               "card_items": sorted(texts), "as_of": utc_us(t)}
        self.classifier.append(row)
        self.log.write("verdict", **row)

    # ----- triggers and actions ------------------------------------------------------------

    def _evaluate(self, handback: int | None = None, as_of: datetime | None = None) -> None:
        """Evaluate every idle event, on the state now or as of a time (the
        workspace, the clock and the fired events as they stood then).
        `handback` is the hand-back being processed, or None (an event then
        belongs to the hand-back that will close this turn)."""
        hb = handback if handback is not None else self.ctx.handbacks + 1
        if as_of is None:
            self.ctx.elapsed_ms = self._elapsed_at(self.utcnow())
            ctx, view = self.ctx, None
        else:
            view = self.ctx.work.as_of(as_of)
            fired = {n: snap for n, snap in self.ctx.fired.items() if self.ev[n].fired_dt is None or self.ev[n].fired_dt <= as_of}
            ctx = dataclasses.replace(self.ctx, work=view, elapsed_ms=self._elapsed_at(as_of), fired=fired)
        changed = True
        while changed:
            changed = False
            for e in self.task.events:
                st = self.ev[e.name]
                if st.status != "idle" or not triggers.holds(e.trigger, ctx):
                    continue
                st.reached_utc = utc_us(as_of) if as_of is not None else self._stamp()  # at its own instant (D-037)
                if e.action.type == "observe":
                    if view is None:
                        self._fire(e, hb)
                    else:
                        self._fire(e, hb, at=as_of, pos=view.position(), elapsed_ms=ctx.elapsed_ms)
                        ctx.fired[e.name] = self.ctx.fired[e.name]
                    changed = True
                else:
                    st.status = "due"
                    self._rev("planted", st.reached_utc, name=e.name, phase="reached")
                    self.log.write("event_due", name=e.name, action=e.action.type)

    def _fire(self, e: Event, handback: int, message: dict[str, str] | None = None, *, at: datetime | None = None,
              mark: tuple[str, tuple[int, int]] | None = None, pos: tuple[int, int] | None = None, elapsed_ms: int | None = None,
              skip: frozenset[tuple[int, int]] = frozenset()) -> None:
        """The event happened at `at`. Its `since` baseline is the watcher
        position given (or a mark naming now) and the clock at that moment. A
        probe about a change is about the batch in effect at that instant,
        named by a mark naming the event's instant (D-037; a straddled
        boundary is logged); an attempt probe is named by the event's instant
        itself, never by a later mark (the bundle resolves attempts by
        completion time)."""
        st = self.ev[e.name]
        label: str | None
        if mark is not None:
            label, pos = mark
        elif pos is None:
            got = self._try_mark(f"{e.name} fired", at)
            label, pos = got if got else (None, self.ctx.work.position())
        else:
            label = None
        at = at or self.utcnow()
        st.status, st.fired_dt, st.fired_utc, st.handback, st.message = "fired", at, utc_us(at), handback, message
        self.ctx.fired[e.name] = self.ctx.snapshot(elapsed_ms if elapsed_ms is not None else self.elapsed_ms(), pos, skip)
        self._rev("planted", utc_us(at), name=e.name, phase="fired")  # at the instant it fired (D-037)
        if e.probe:
            about = e.probe.about
            target: dict[str, str] | None = None
            missing = None
            if about == "message":
                target = message
            elif about == "attempt":
                target = {"utc": utc_us(at)}
            elif about == "change":
                if label is None:
                    got = self._try_mark(f"{e.name} probe boundary", at)
                    label = got[0] if got else None
                target = {"mark": label} if label is not None else {"utc": utc_us(at)}
            elif handback in self.closing:
                out = self.closing[handback]
                target = {"item": out.item_id} if out is not None else None  # type: ignore[dict-item]
            p = _Probe(e, handback, target, waiting_next_output=about == "output" and target is None and handback in self.closing and missing is None,
                       missing=missing)
            self.due_probes.append(p)
        self.log.write("event_fired", name=e.name, action=e.action.type, delivery=st.delivery, message=message, mark=label, handback=handback,
                       at=st.fired_utc, since_pos=list(pos), since_ms=self.ctx.fired[e.name].elapsed_ms)

    def _mid_turn_corrections(self) -> None:
        """Steer what is due into the running turn: corrections, and (D-046) the
        first due reset's notice, by the same route, with the same mark and
        settlement; when there is no run to steer, both go with the next message."""
        if self.corrections_held:
            return  # the port said there is no run to steer; the correction goes at the hand-back
        for e in self.task.events:
            if self.ev[e.name].status != "due" or e.action.type not in ("correction", "reset"):
                continue
            notice = e.action.type == "reset"
            if notice and (self.reset_in_progress is not None or any(self.ev[x.name].status == "queued" for x in self.task.events if x.action.type == "reset")):
                continue  # one reset at a time
            text = e.action.notice if notice else e.action.text
            assert text is not None
            self._guard(text)
            limit = self._dispatch_gate()  # D-037: the budgets, the deadline last, and the dispatch allowance
            try:
                sent = self.ep.correct(text, **limit)
            except TurnNotRunning as exc:
                # The turn has ended, or (D-029) the main run has ended while a subagent
                # works: nothing to steer. Held until the hand-back, sent there as an
                # ordinary next message and recorded as such.
                self.log.write("correction_deferred", name=e.name, why=str(exc))
                self.corrections_held = True
                return
            ref = self._msg_ref(sent)
            st = self.ev[e.name]
            st.status, st.delivery = "queued", "mid_turn"
            rec = {"name": e.name, "delivery": "mid_turn", "mechanism": sent.mechanism, "queued_utc": sent.utc, "delivered_utc": None, "mark": None,
                       "mark_settled": None, "mark_quiescent": None}
            (self.notices if notice else self.corrections).append(rec)
            self._record_send(sent, text, NOTICE_KIND if notice else "correction", "correct",
                              [{"source": "event", "name": e.name, "action": "reset_notice" if notice else "correction"}], ref=ref)
            # Queued for settlement first: a late steer's delivery still reaches the end replay (audit).
            self.queued.append(_Queued(e, sent, ref, rec))
            self._correction_sent[e.name] = sent  # its delivery is replayed at its own instant with the next slice (D-037)
            if self._late(sent):  # the same late-send check and audit as every dispatch
                self._after_deadline(sent, text, NOTICE_KIND if notice else "correction", "correct")

    def _deliver(self, e: Event, ref: dict[str, str], d: Delivery, rec: dict[str, Any], handback: int) -> None:
        """A mid-turn correction taken by the harness. The boundary is the
        driver's: its mark at the observed delivery. Without one the event
        says so (mark null), and `since` counts from the state as of the
        delivery time; no later mark stands in for it."""
        when, wdt = d.utc, _dt(d.utc)
        rec["delivered_utc"], rec["mark"] = when, d.mark
        rec["mark_settled"], rec["mark_quiescent"] = d.settled, d.quiescent
        if d.mechanism and d.mechanism != rec.get("mechanism"):
            self.log.write("correction_mechanism", name=e.name, queued=rec.get("mechanism"), settled=d.mechanism)
            rec["mechanism"] = d.mechanism  # as the driver knows it at settlement
        self._refresh()
        if e.action.type == "reset":
            self._notice_delivered(e, ref, d, rec)
            return
        assert e.action.text is not None
        if d.mark is not None:
            if d.mark not in self.marks:
                self.marks.append(d.mark)
            pos = self.ctx.work.marks.get(d.mark) or self.ctx.work.as_of(wdt).position()
        else:
            pos = self.ctx.work.as_of(wdt).position()
            self.log.write("delivery_mark_unavailable", name=e.name, utc=when)
        self._rev("correction_delivered", when, ref, mark=d.mark, **_settled_fields(d.settled))
        self._line(USER, e.action.text, when, kind="correction", ref=ref, shown_as="correction")
        skip = self._straddle(wdt, pos, d if d.mark is not None else None)  # type: ignore[arg-type]
        rec["straddled"] = min((n for _s, n in skip), default=None)
        self.log.write("correction_delivered", name=e.name, utc=when, ref=ref, mark=d.mark, mark_settled=d.settled, mark_quiescent=d.quiescent,
                       straddled=sorted(n for _s, n in skip), pos=list(pos))
        self._fire(e, handback, ref, at=wdt, mark=(d.mark, pos) if d.mark else None, pos=pos, elapsed_ms=self._elapsed_at(wdt), skip=skip)

    def _notice_delivered(self, e: Event, ref: dict[str, str], d: Delivery, rec: dict[str, Any]) -> None:
        """D-046: a reset's notice steered into the running turn was taken by the
        harness at `d.utc`: the reset is now in progress, at the next hand-back or
        when its grace runs out, whichever comes first. The event fires when the
        reset is performed (its continuation's send)."""
        assert e.action.notice is not None
        wdt = _dt(d.utc)
        if d.mark is not None and d.mark not in self.marks:
            self.marks.append(d.mark)  # the driver's mark at the delivery, as a correction's
        pos = (self.ctx.work.marks.get(d.mark) if d.mark is not None else None) or self.ctx.work.as_of(wdt).position()
        skip = self._straddle(wdt, pos, d if d.mark is not None else None)  # type: ignore[arg-type]
        run = sorted(n for _s, n in skip)
        rec["straddled"] = run[0] if run else None
        self._line(USER, e.action.notice, d.utc, kind=NOTICE_KIND, ref=ref, shown_as="reset notice")
        if "notice_delivered" in F.RUNNER_FIELDS:
            self._rev("notice_delivered", d.utc, ref, **({"mark": d.mark} if "mark" in F.RUNNER_FIELDS["notice_delivered"] else {}))
        st = self.ev[e.name]
        st.status = "notice_sent"
        self.reset_in_progress = e.name
        self._notice_at = _dt(d.utc)
        # Its own record (D-046): never a correction's, so no `correction_delivered` event.
        self.reset_info.setdefault(e.name, {})["notice"] = {"delivery": "mid_turn", "utc": d.utc, "message": ref, "mechanism": rec.get("mechanism"),
                                                             "mark": d.mark, "mark_settled": d.settled, "queued_utc": rec.get("queued_utc"),
                                                             "straddled": run[0] if run else None, "straddled_last": run[-1] if run else None,
                                                             "pos": list(pos)}
        self.log.write("reset_notice_sent", name=e.name, delivery="mid_turn", utc=d.utc, ref=ref, mark=d.mark, mechanism=rec.get("mechanism"))

    def _straddle(self, at: datetime, pos: tuple[int, int], ack: MarkAck | Delivery | None) -> frozenset[tuple[int, int]]:
        """A correction's boundary straddle (D-037, G6), for mid-turn and next-message
        deliveries alike: the batches whose writes run across the delivery instant,
        excluded from `since` counts on both sides. An accepted mark that answered for
        the instant names the run, `straddled` through `straddled_last`; with no such
        mark the run is derived from the file log by the watcher's rule
        (`first_write_after < at < last_write`)."""
        if ack is not None:
            first, last = ack.straddled, getattr(ack, "straddled_last", None)
            if not isinstance(first, int) or isinstance(first, bool):
                return frozenset()
            hi = last if isinstance(last, int) and not isinstance(last, bool) and last >= first else first
            return frozenset((pos[0], n) for n in range(first, hi + 1))
        return frozenset(self.ctx.work.straddling(at, pos[0]))

    # ----- the hand-back -------------------------------------------------------------------

    def _hand_back(self, turn: Turn, hb_at: datetime | None = None) -> None:
        self.corrections_held = False
        self._hb_at = hb_at or self.utcnow()
        self.ctx.handbacks += 1
        if turn.state == "finished":
            self.ctx.turns += 1
        h = self.ctx.handbacks
        outs = self.seen
        self.seen = []
        # Several requests can be pending at once (two subagents can each ask); the
        # port returns each once and marks the one to answer. Each is judged when it
        # arrives. Only the marked one is answered now; another request's reply waits
        # for the hand-back that marks it.
        marked = turn.pending.item_id if turn.pending is not None else None
        # A request already pending at an earlier hand-back shares that hand-back's
        # wait (D-035): its own reply goes now, with no second wait.
        self._shared_wait = marked is not None and marked in self.waiting_requests
        if marked is not None and marked in self.waiting_requests and all(s.output.item_id != marked for s in outs):
            outs.append(self.waiting_requests.pop(marked))
        held_now: list[_Seen] = []  # requests pending at this hand-back besides the marked one: they share its wait
        for s in list(outs):
            if s.output.kind in ("question", "secret") and marked is not None and s.output.item_id != marked:
                outs.remove(s)
                self.waiting_requests[s.output.item_id] = s
                held_now.append(s)
                self.log.write("request_waits", item=s.output.item_id, answering=marked)
        if marked is not None:
            self.waiting_requests.pop(marked, None)
        closing = next((s.output for s in outs if s.output.item_id == turn.closing), None) if turn.closing else None
        self.closing[h] = closing
        self._final_unconfirmed = False
        self.handback_log.append({"utc": utc_us(self._hb_at), "handback": h,  # at the hand-back's own instant (D-037)
                                  "closing": closing.item_id if closing else None, "kind": "finished" if turn.state == "finished" else "pending"})
        self.log.write("hand_back", handback=h, turns=self.ctx.turns, state=turn.state, pending=turn.pending.kind if turn.pending else None,
                       outputs=[s.output.item_id for s in outs], closing=turn.closing)
        # The budgets as candidate ends; none ends the episode here: the earliest of them
        # and of this hand-back's own end (finished, or its wait) wins (_choose_end).
        cands = self._budget_candidates()
        for p in self.due_probes:
            if p.target is None and p.event.probe and p.event.probe.about == "output" and (p.handback == h or p.waiting_next_output):
                if closing is not None:
                    p.target, p.waiting_next_output = {"item": closing.item_id}, False  # type: ignore[dict-item]
                else:
                    p.waiting_next_output = True
        self._evaluate(h, as_of=self._hb_at)  # as of the hand-back's own instant (D-037)
        secret = self.pending is not None and self.pending.kind == "secret"
        hb = _HB(h, outs, held_now, marked, secret, closing)
        if self.reset_in_progress is not None and (self._notice_at is None or self._notice_at <= self._hb_at):
            self._reset_how = {"at": "handback", "utc": utc_us(self._hb_at), "handback": h}
            self._do_reset(hb, cands)
            return
        if self.pending is None:
            # G1: probes due at a hand-back are taken at it whenever the runner gets to it (the
            # agent is idle; the copy is excluded time), for events fired by its instant.
            self._take_probes(h)
        # G4: finish, reopen or continue, decided from the state at the hand-back's instant.
        first = self._due_as_of(hb, self._hb_at)
        if not first.parts:
            unreached = [e.name for e in self.task.events if e.required and not self._fired_as_of(e, self._hb_at)]
            why = ("no request, and every required event fired" if not unreached
                   else f"no request; required events {unreached} not fired and no reopen applies")
            self._end_earliest(cands + [(self._hb_at, "finished", None)], why)  # the final hand-back's instant, unless a budget ran out first
        if h >= self.cfg.max_turns:
            self._end("runner_abort", f"the safety limit of {self.cfg.max_turns} hand-backs was reached")
        # A wait already covered this hand-back only when every output it answers was
        # answered under an earlier wait; a request that arrived since (a third
        # subagent's, say) gets this hand-back's own wait, and so does a held secret
        # request's refusal (it is no reply of the earlier wait).
        fresh = [o for o in first.replies_to if o.item_id not in self._waited]
        if secret and marked is not None and marked not in self._waited:
            fresh.append(next(s.output for s in outs if s.output.item_id == marked))
        if self._shared_wait and not fresh:
            self.log.write("wait_shared", request=marked, why="pending at an earlier hand-back, whose wait covered it")
            due = first
        else:
            def compose(dispatch_at: datetime) -> _Due:
                # G4: the message is decided at its scheduled dispatch instant (the hand-back plus
                # the return delay): what fell due during the wait goes with it, an overriding
                # message replaces the policy's replies; the reopen decided at the hand-back stays.
                self._replay_upto([], until=dispatch_at, why="dispatch")
                return self._due_as_of(hb, dispatch_at, reopen=first.reopen)
            due = self._user_wait(closing, first, compose, cands)
            self._waited.update(o.item_id for o in due.replies_to if o.item_id)
            if secret and marked is not None:
                self._waited.add(marked)
        text = "\n\n".join(due.parts)
        self._guard(text)
        if due.override:
            self.log.write("policy_overridden", by=[e.name for e in due.delivered if e.action.type == "message" and e.action.override_policy])
        via = "refuse_secret" if secret else "answer" if self.pending is not None else "reply"
        pending = self.pending
        sent = self._send(text, due.kind, via, due.meta)
        ref = self._msg_ref(sent, pending)
        for s, reply in due.used:
            s.verdict["fields"]["reply"] = reply
        self._delivered_with(due.delivered, sent, ref, h)
        if due.reopen is not None:
            self.reopens += 1  # counted once it was sent
            self._rev("reopen", utc_us(_dt(sent.utc)), ref, name=due.reopen.name)  # at its send time (D-037)
            self.log.write("reopen", name=due.reopen.name, used=self.reopens, limit=self.task.max_reopens)

    def _due_as_of(self, hb: "_HB", t: datetime, *, reopen: Event | None = None, continuation: Event | None = None) -> "_Due":
        """What is due at a hand-back as of the instant `t` (G4): the one function the
        finish-or-continue decision (at the hand-back's instant), the message's
        composition (at its dispatch instant) and the reset's continuation call. The
        verdict replies, unless a due message overrides them; the refusal of a pending
        secret request; every correction, fixed message and reset notice due by `t`;
        the reopen (decided at the hand-back's instant, kept at dispatch); and the
        outputs answered, with the causes."""
        due = [e for e in self.task.events if self.ev[e.name].status == "due" and self._due_by(e, t)
               and (continuation is None or e.name != continuation.name)]
        override = continuation is None and any(e.action.type == "message" and e.action.override_policy for e in due)
        d = _Due(override=override)
        if continuation is not None:
            assert continuation.action.continuation is not None
            d.parts.append(continuation.action.continuation)
            d.meta.append({"source": "event", "name": continuation.name, "action": "reset_continuation"})
            d.causes.append("reset")
            return d  # D-046: the fresh context opens with the continuation alone
        else:
            if not override:
                for s in hb.outs:
                    if s.reply is None or (hb.secret and s.output.kind == "secret"):
                        continue
                    d.used.append((s, s.reply))
                    if s.reply not in d.parts:
                        d.parts.append(s.reply)
                        d.meta.append({"source": "verdict", "output": {"item": s.output.item_id}, "label": s.label})
                if d.used:
                    d.causes.append("verdict_reply")
            if hb.secret:
                refusal = self.task.replies.secret_refusal
                d.parts.append(refusal)
                d.meta.append({"source": "secret_refusal"})
                d.used += [(s, refusal) for s in hb.outs if s.output.kind == "secret" and s.output.item_id == hb.marked]
                d.causes.append("refusal")
        d.delivered = self._due_actions(due, d.parts, d.meta, notices=continuation is None)
        if d.delivered:
            d.causes.append("fixed_message")
        if continuation is None:
            d.reopen = reopen if reopen is not None else (self._reopen_candidate(t) if not d.parts else None)
            if d.reopen is not None:
                assert d.reopen.reopen is not None
                d.parts.append(d.reopen.reopen)
                d.meta.append({"source": "reopen", "name": d.reopen.name})
                d.causes.append("reopen")
            d.replies_to = [s.output for s, _r in d.used if not (hb.secret and s.output.kind == "secret")]
            if not override:
                d.replies_to += [s.output for s in hb.held_now if s.reply is not None and s.output.kind != "secret"]
                if any(s.reply is not None and s.output.kind != "secret" for s in hb.held_now) and "verdict_reply" not in d.causes:
                    d.causes.append("verdict_reply")
        return d

    def _fired_as_of(self, e: Event, t: datetime | None) -> bool:
        st = self.ev[e.name]
        return st.status == "fired" and (t is None or st.fired_dt is None or st.fired_dt <= t)

    def _due_actions(self, due: list[Event], parts: list[str], meta: list[dict[str, Any]], *, notices: bool) -> list[Event]:
        """Append every due correction, fixed message and (with `notices`) the
        first due reset's notice to the message being built; the events
        delivered with it."""
        delivered: list[Event] = []
        for kind in ("correction", "message"):
            for e in due:
                if e.action.type == kind:
                    assert e.action.text is not None
                    parts.append(e.action.text)
                    meta.append({"source": "event", "name": e.name, "action": kind})
                    delivered.append(e)
        resets = [e for e in due if e.action.type == "reset"]
        if notices and resets:
            e = resets[0]
            assert e.action.notice is not None
            parts.append(e.action.notice)
            meta.append({"source": "event", "name": e.name, "action": "reset_notice"})
            delivered.append(e)
        return delivered

    def _delivered_with(self, delivered: list[Event], sent: Sent, ref: dict[str, str], h: int) -> None:
        """The due actions a message carried: each fires with it."""
        dispatch = self._dispatch_ms(sent)
        for e in delivered:
            st = self.ev[e.name]
            if e.action.type == "correction":
                st.delivery = "next_message"
                rec = {"name": e.name, "delivery": "next_message", "mechanism": sent.mechanism, "queued_utc": sent.utc, "delivered_utc": None, "mark": None,
                       "mark_settled": None, "mark_quiescent": None}
                self.corrections.append(rec)
                self._deliver_next(e, ref, sent, rec, h)
            elif e.action.type == "message":
                self._fire(e, h, ref, at=_dt(sent.utc), elapsed_ms=dispatch)
            else:
                st.status, st.delivery = "notice_sent", "next_message"
                self.reset_in_progress = e.name
                self._notice_at = _dt(sent.utc)  # delivered with the message: its grace counts from its send (D-046)
                self.reset_info.setdefault(e.name, {})["notice"] = {"delivery": "next_message", "utc": sent.utc, "message": ref, "mechanism": None,
                                                                     "mark": None}
                self.log.write("reset_notice_sent", name=e.name, delivery="next_message", utc=sent.utc)

    def _deliver_next(self, e: Event, ref: dict[str, str], sent: Sent, rec: dict[str, Any], h: int) -> None:
        """A correction sent as the next message: delivered with it, and
        recorded as soon as that is known. The runner's own mark at the send is
        its boundary; if the mark fails (B1) the delivery is recorded with a
        null mark, the failure is logged, and `since` counts from the state as
        of the send. A failed mark never fails the episode (D-030)."""
        rec["delivered_utc"] = sent.utc
        mark: tuple[str, tuple[int, int]] | None
        try:
            mark = self._mark(f"{e.name} delivered with the next message", at=_dt(sent.utc))  # its send time (D-037)
        except EpisodeFailure as exc:
            if exc.fatal:
                raise
            mark = None
            self.log.write("error", where="correction delivery mark", name=e.name, error=repr(exc))
            self.log.write("delivery_mark_unavailable", name=e.name, utc=sent.utc)
        ack = self.mark_acks.get(mark[0]) if mark else None
        if ack is not None and ack.accepted is False:
            mark = None  # a rejected mark names nothing: the historical position stands in
        rec["mark"] = mark[0] if mark else None
        rec["mark_settled"], rec["mark_quiescent"] = (ack.settled, ack.quiescent) if ack is not None else (None, None)
        if mark:
            pos = mark[1]
        else:
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001
                self.log.write("error", where="refresh after a failed mark", error=repr(exc))
            pos = self.ctx.work.as_of(_dt(sent.utc)).position()
        skip = self._straddle(_dt(sent.utc), pos, ack if mark else None)  # type: ignore[arg-type]
        rec["straddled"] = min((n for _s, n in skip), default=None)
        self.log.write("correction_delivered", name=e.name, utc=sent.utc, ref=ref, mark=rec["mark"], mark_settled=rec["mark_settled"],
                       mark_quiescent=rec["mark_quiescent"], straddled=sorted(n for _s, n in skip), pos=list(pos))
        self._rev("correction_delivered", sent.utc, ref, mark=rec["mark"], **_settled_fields(rec["mark_settled"]))
        self._fire(e, h, ref, at=_dt(sent.utc), mark=mark, pos=pos, elapsed_ms=self._dispatch_ms(sent), skip=skip)

    def _reopen_candidate(self, as_of: datetime | None = None) -> Event | None:
        """The earliest declared required event not fired by `as_of` whose
        prerequisites (positive hard dependencies) had fired by then, while
        reopens are left; the state as of the hand-back, never "current" (G4)."""
        if self.reopens >= self.task.max_reopens:
            return None
        fired = {e.name for e in self.task.events if self._fired_as_of(e, as_of)}
        for e in self.task.events:
            st = self.ev[e.name]
            if e.required and e.name not in fired and (st.status == "idle" or not self._due_by(e, as_of)) and e.prerequisites() <= fired:
                return e
        return None

    def _line_of(self, about: str, ref: dict[str, str]) -> _Line | None:
        """The transcript line a probe target names: an agent output by its item,
        or a user message by its reference (a message id, or for an answer the
        request's item id, which its question output shares)."""
        for x in self.lines:
            if about == "output" and x.entry.role == AGENT and "item" in ref and x.meta.get("item") == ref["item"]:
                return x
            if about == "message" and x.entry.role == USER and x.meta.get("ref") == ref:
                return x
        return None

    def _take_probes(self, h: int) -> None:
        """At an idle boundary: a copy for every due probe whose target is fixed."""
        for p in list(self.due_probes):
            if p.target is None:
                continue
            if self._hb_at is not None and not self._fired_as_of(p.event, self._hb_at):
                continue  # G1: only for an event fired at or before the hand-back's instant
            pr = p.event.probe
            assert pr is not None
            self._guard(pr.question)
            pid = len(self.probes) + 1
            # Immediately before the copy, outside its excluded interval; at its own instant even
            # after the end (a probe that belongs to the last hand-back, G1).
            utc = self._stamp(capped=False)
            at = _dt(utc)
            expected = pr.expected
            if pr.expected_from:
                shown = pr.expected_from["shown_to_user"]
                expected = "yes" if any(x.entry.role == AGENT and shown in x.entry.text and x.utc <= at for x in self.lines) else "no"  # type: ignore[operator]
            deferred = h != p.handback  # D-028: taken at a later hand-back than the first one at or after the event
            self._rev("probe", utc, probe=pid)
            self._clear_ms()
            # A copy-side problem is an outcome; a raise is about the main episode and ends it (B4).
            out = self.ep.probe(pr.question)
            status, answer, reason, cleanup = out.status, out.answer, out.reason, getattr(out, "cleanup", None)
            if status == "answered" and not isinstance(answer, str):
                status, reason = "failed", "answered without an answer"
            if status not in ("answered", "unavailable", "failed"):
                status, reason = "failed", f"unknown probe status {status!r}"
            self.probes.append({"probe": pid, "question": pr.question, "expected": expected,
                                "answer": answer if status == "answered" else None, "status": status})
            self.probe_targets[pid] = {"about": pr.about, "ref": p.target, "deferred": deferred, "event": p.event.name}
            row = {"probe": pid, "event": p.event.name, "about": pr.about, "target": p.target, "deferred": deferred,
                   "handback": h, "event_handback": p.handback, "expected": expected,
                   "status": status, "answer": answer, "reason": reason, "cleanup_incomplete": cleanup, "utc": utc}
            self.probe_log.append(row)
            self.log.write("probe", **row)
            self.due_probes.remove(p)

    def _do_reset(self, hb: _HB, cands: list[tuple[datetime, str, str | None]], *, probes: bool = True) -> None:
        """The reset after its notice (D-023, D-046), at the next hand-back or at the
        grace (`_grace_reset`): that context ends. A pending request is left
        unanswered (the tree is stopped and drained, and what the drain returns goes
        through the one replay); every output since the notice is judged and gets no
        reply. At a hand-back the due probes are taken. After the wait the
        continuation alone opens the fresh context. Due corrections follow by
        the ordinary delivery rule; fixed messages wait for its hand-back."""
        e = self.task.event(self.reset_in_progress)  # type: ignore[arg-type]
        assert e.action.continuation is not None
        if self.agent == "pending":
            drained = self._interrupt_and_drain("the pending request is left unanswered at the reset")
            at = max([self.utcnow(), *(d for o in drained for d in [_dt(o.delivered_utc)] if d is not None)])  # covers every drained output
            self._replay_upto(drained, until=at, why="reset")
            self.pending = None
            self.agent = "finished"
        unanswered = [x.output.item_id for x in list(hb.outs) + list(self.seen) if x.output.item_id]
        if unanswered:  # judged as they arrived; the context they belong to ends, so no reply goes to them
            self.log.write("unanswered_at_reset", outputs=unanswered)
        self.seen = []
        self.reset_info.setdefault(e.name, {})["unanswered"] = unanswered
        if probes:
            self._take_probes(hb.h)
        if hb.h >= self.cfg.max_turns:
            self._end("runner_abort", f"the safety limit of {self.cfg.max_turns} hand-backs was reached")
        first = self._due_as_of(hb, self._hb_at, continuation=e)

        def compose(dispatch_at: datetime) -> _Due:
            self._replay_upto([], until=dispatch_at, why="dispatch")
            return self._due_as_of(hb, dispatch_at, continuation=e)
        due = self._user_wait(hb.closing, first, compose, cands)  # from the hand-back (D-035)
        text = "\n\n".join(due.parts)
        self._guard(text)
        sent = self._send(text, "reset", "reset", due.meta, check_late=False)
        ref = self._msg_ref(sent)
        if self._late(sent):  # compared on the episode clock, after the excluded handoff
            self._reset_event(utc_us(_dt(sent.utc)), ref, "launched")  # recorded before the cutoff that follows
            self.reset_in_progress = None
            self._after_deadline(sent, text, "reset", "reset")
        # The event fires at the continuation's send, then the reset is recorded at that same
        # instant: in the view the firing comes first, the reset (and the message it names) after.
        self._fire(e, hb.h, ref, at=_dt(self._outside_excluded(utc_us(_dt(sent.utc)))), elapsed_ms=self._dispatch_ms(sent))
        self._reset_event(utc_us(_dt(sent.utc)), ref, "launched")
        self.reset_in_progress = None
        self._mid_turn_corrections()

    def _reset_event(self, utc: str, ref: dict[str, str] | None, continuation: str) -> None:
        """A context reset is the handoff: `controlled_reset` is recorded once per
        handoff, carrying its continuation's state (launched or unsent), as the
        driver records it, and (D-046) how its notice went (`notice`: mid_turn or
        next_message) and whether the reset came at a hand-back or at the grace
        (`reset_at`); each a field of the event once the trajectory unit's table
        has it. The run manifest's event record has all of it."""
        name = self.reset_in_progress
        info = self.reset_info.setdefault(name or "", {})
        how = dict(self._reset_how or {"at": "handback", "utc": utc_us(self._hb_at) if self._hb_at else utc})
        info["reset"] = {**how, "continuation": continuation, "continuation_utc": utc}
        spec = F.RUNNER_FIELDS.get("controlled_reset", {})
        f: dict[str, Any] = {}
        if "continuation" in spec:
            f["continuation"] = continuation
        if "notice" in spec:
            f["notice"] = (info.get("notice") or {}).get("delivery")
        if "reset_at" in spec:
            f["reset_at"] = how["at"]
        self._rev("controlled_reset", utc, ref, **f)["continuation"] = continuation
        self.log.write("controlled_reset", name=name, utc=utc, ref=ref, continuation=continuation, notice=(info.get("notice") or {}).get("delivery"),
                       reset_at=how["at"])
        self._reset_how, self._notice_at = None, None

    # ----- waiting on the user and sending ---------------------------------------------------

    def _due_by(self, e: Event, t: datetime | None) -> bool:
        r = _dt(self.ev[e.name].reached_utc)
        return t is None or r is None or r <= t

    def _user_wait(self, target: Output | None, first: _Due, compose: Callable[[datetime], _Due] | None,
                   cands: list[tuple[datetime, str, str | None]]) -> _Due:
        """The return delay, a real wait (D-020, D-033, D-035, D-037). It starts at the
        hand-back: the delivery of the output that closed it, or, with none, when the
        agent stopped (the driver's `handback_utc`, D-041); a held request's wait never
        starts before the end of the wait it follows. The reply is due at that start
        plus the delay; judging counts towards it and only what is left is slept. The
        wait ends at the earliest of that due instant and the candidate ends known now
        (_choose_end: the user-wait clip, the elapsed deadline, a token cutoff): the
        charge is the scheduled delay cut there, whatever judging took (an overrun stays
        in elapsed time). At the due instant `compose` decides the message as of it;
        ended by a budget, the episode ends at that instant and nothing is sent."""
        ref = {"item": target.item_id} if target is not None else None
        planned = self.task.return_delay_ms
        start = (_dt(target.delivered_utc) if target is not None and target.delivered_utc else None) or self._hb_at or self.utcnow()
        if self._last_wait_end is not None and start < self._last_wait_end:
            start = self._last_wait_end  # never two charges over one interval
        now = self.utcnow()
        u0 = utc_us(start)  # a delivery dated a hair after the runner's clock stays as dated
        already = self._clock_ms_between(start, now)  # judging and polling since the hand-back
        overrun = max(0, already - planned)
        # Where the time went: from the hand-back to the driver's report of it, and from that report on.
        rep_at = self._report_at or now
        reported = self._clock_ms_between(start, rep_at) if rep_at > start else 0
        lim = self.task.budgets
        ends: list[tuple[datetime, str, str | None]] = [(self._wall_after(start, planned), "reply", None)]
        if lim["user_wait_ms"] is not None:
            uw_left = max(0, lim["user_wait_ms"] - self.user_wait_ms)
            ends.append((self._wall_after(start, uw_left), "budget_cutoff", "user_wait"))
        dl = self._deadline_at()
        if dl is not None:
            ends.append((dl, "budget_cutoff", "time"))
        ends += [c for c in cands if c[2] == "tokens"]
        at, reason, budget = self._choose_end(ends)
        at = max(at, start)
        charge = min(planned, self._clock_ms_between(start, at))
        if budget == "user_wait" and lim["user_wait_ms"] is not None:
            charge = min(planned, max(0, lim["user_wait_ms"] - self.user_wait_ms))
        elif budget == "time" and lim["elapsed_ms"] is not None:
            charge = min(charge, max(0, lim["elapsed_ms"] - self._elapsed_at(start)))
        sleep_ms = self._clock_ms_between(now, at) if at > now else 0
        self.log.write("user_wait_start", target=ref, cause=first.cause, causes=sorted(set(first.causes)), start=u0, planned_ms=planned,
                       already_ms=already, reported_ms=reported, charge_ms=charge, sleep_ms=sleep_ms, overrun_ms=overrun, clipped_by=budget)
        if sleep_ms > 0:
            self.sleep(sleep_ms / 1000)
        if budget == "user_wait":
            self._uw_reached_at = at
        self.user_wait_ms += charge
        d = compose(at) if budget is None and compose is not None else first
        cs = sorted(set(d.causes))
        rt = [{"item": o.item_id} for o in d.replies_to if o.item_id]
        why: dict[str, Any] = {"causes": cs} if "causes" in F.RUNNER_FIELDS["wait"] else {"cause": d.cause}
        u1 = utc_us(at) if budget else self._stamp(at)
        ended = "budget_cutoff" if budget else "reply"
        extra = {"overrun_ms": overrun} if "overrun_ms" in F.RUNNER_FIELDS["wait"] else {}
        if overrun:
            self.overruns.append({"start": u0, "reported_utc": utc_us(rep_at), "planned_ms": planned, "overrun_ms": overrun, "target": ref,
                                  "reported_ms": reported, "after_report_ms": max(0, already - reported)})
        self._last_wait = self._rev("wait", u0, ref, u1, charged_ms=charge, ended=ended, replies_to=rt, **why, **extra)  # type: ignore[arg-type]
        self._last_wait_end = at
        try:
            self.ep.note_user_wait(ref, u0, u1)
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="note_user_wait", error=repr(exc))
        # The raw line is written once its ending is decided: here when a budget ended it,
        # else when the reply went or the dispatch gate held it back (_flush_wait).
        self._wait_line = {"target": ref, "cause": d.cause, "causes": cs, "replies_to": rt, "charged_ms": charge, "overrun_ms": overrun}
        if budget:
            self._cutoff(budget, at=at)
        return d

    def _flush_wait(self, ended: str) -> None:
        if self._wait_line is not None:
            self.log.write("user_wait_end", **self._wait_line, user_wait_ms=self.user_wait_ms, ended=ended)
            self._wait_line = None

    def _clock_ms_at(self, t: datetime) -> int:
        """The episode clock at the wall instant `t`, counted from the clock's origin
        over the excluded intervals: the one conversion the bundle uses too."""
        try:
            origin = _dt(self.ep.origin_utc())
        except Exception:  # noqa: BLE001
            origin = None
        if origin is None:
            return self._elapsed_at(t)
        return self._clock_ms_between(origin, t)  # the same conversion as _elapsed_at

    def _wall_after(self, start: datetime, ms: float) -> datetime:
        """The wall instant `ms` of episode clock after `start`, counted over every
        excluded interval that begins after it (a probe taken at the hand-back)."""
        t = start + timedelta(milliseconds=ms)
        try:
            ivs = sorted(((_dt(i.start_utc), _dt(i.end_utc)) for i in self.ep.excluded() if i.start_utc and i.end_utc), key=lambda x: x[0])  # type: ignore[arg-type, return-value]
        except Exception:  # noqa: BLE001
            return t
        for s0, s1 in ivs:
            if s0 is None or s1 is None or s1 <= start or s0 >= t:
                continue
            t += s1 - max(s0, start)
        return t

    def _clock_ms_between(self, a: datetime, b: datetime) -> int:
        """Whole milliseconds of the episode clock between two wall times: the
        wall interval less every excluded interval inside it, rounded down."""
        if b <= a:
            return 0
        us = (b - a) / timedelta(microseconds=1)
        try:
            for iv in self.ep.excluded():
                s0, s1 = _dt(iv.start_utc), _dt(iv.end_utc)
                if s0 is not None and s1 is not None:
                    lo, hi = max(a, s0), min(b, s1)
                    if hi > lo:
                        us -= (hi - lo) / timedelta(microseconds=1)
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="excluded intervals for a wait", error=repr(exc))
        return max(0, int(us // 1000))

    def _guard(self, text: str) -> None:
        words = message_words(text, self.task.canary)
        if words:
            self._end("runner_abort", f"a message for the agent holds telling words {sorted(set(words))}")

    def _dispatch_gate(self) -> dict[str, Any]:
        """Before any dispatch (D-037): the budgets read again, the elapsed
        deadline last; with less than DISPATCH_ALLOWANCE_S left before the
        deadline nothing is sent, and the episode runs to its deadline and
        cuts off there. Returns the driver call's own limit."""
        self._check_budgets()
        rem = self._remaining_s()
        if rem is not None and rem < DISPATCH_ALLOWANCE_S:
            self.log.write("dispatch_not_attempted", remaining_s=round(rem, 3), allowance_s=DISPATCH_ALLOWANCE_S)
            self._wait_ended_by_cutoff()
            self._run_to_deadline()
        return {"timeout_s": rem} if rem is not None else {}

    def _wait_ended_by_cutoff(self) -> None:
        """The wait just recorded ended without its reply: the dispatch gate decided."""
        if self._last_wait is not None and self._last_wait["fields"].get("ended") == "reply":
            self._last_wait["fields"]["ended"] = "budget_cutoff"
        self._last_wait = None
        self._flush_wait("budget_cutoff")

    def _run_to_deadline(self) -> None:
        """Nothing more is sent: the episode runs to its elapsed deadline and ends there."""
        rem = self._remaining_s() or 0.0
        if rem > 0:
            self.sleep(rem)
        self._cutoff("time", at=self._deadline_at())

    def _late(self, sent: Sent) -> bool:
        """A dispatch that landed after the deadline: its send instant against the
        deadline's (a reset's handoff is excluded time, which moves the deadline)."""
        if self.task.budgets["elapsed_ms"] is None:
            return False
        at, dl = _dt(self._outside_excluded(sent.utc)) if sent.utc else None, self._deadline_at()
        if at is None or dl is None:
            return self._dispatch_ms(sent) > self.task.budgets["elapsed_ms"]
        return at > dl

    def _after_deadline(self, sent: Sent, text: str, kind: str, via: str) -> None:
        """Not sent, as far as the episode goes (D-037): kept in the audit record; the episode ends at its deadline."""
        self._wait_ended_by_cutoff()
        self.log.write("dispatched_after_deadline", kind=kind, via=via, sent_utc=sent.utc, dispatch_ms=self._dispatch_ms(sent), message=sent.message_id)
        if sent.message_id:
            self.user_kinds[sent.message_id] = kind
        self.after_deadline.append({"kind": kind, "via": via, "sent_utc": sent.utc, "message": sent.message_id, "text": text})
        self._cutoff("time", at=self._deadline_at())

    def _send(self, text: str, kind: str, via: str, meta: list[dict[str, Any]], *, check_late: bool = True) -> Sent:
        limit = self._dispatch_gate()
        if via == "reset":
            # The handoff has its own infrastructure timeout (the driver's), not the episode
            # time left (the handoff is excluded time); the episode time left bounds only the
            # continuation's launch, counted from the handoff's end.
            limit = {"dispatch_timeout_s": limit["timeout_s"]} if "timeout_s" in limit else {}
        pending = self.pending
        submitted: dict[str, str] | None = None
        try:
            sent = self._dispatch(text, via, pending, limit)
        except EpisodeFailure as exc:
            if exc.fatal:
                raise
            # The driver did not start the call, or a part of it did not reach T3 ("unsent"): the
            # unsent part was not sent. What did happen (a reset's handoff, a secret's decline) is logged.
            self.log.write("dispatch_unsent", kind=kind, via=via, error=str(exc)[:300], done=exc.done, not_sent=exc.not_sent)
            if via == "reset" and "handoff" in exc.done:
                rem = self._remaining_s()
                if rem is None or rem >= DISPATCH_ALLOWANCE_S:
                    # The handoff happened; calling reset again only launches the continuation.
                    sent = self._dispatch(text, via, pending, {"dispatch_timeout_s": rem} if rem is not None else {})
                    self.log.write("reset_continuation_retried")
                else:
                    # The handoff is the reset; its continuation never went.
                    self._reset_event(utc_us(self.utcnow()), None, "unsent")
                    self.reset_in_progress = None
                    self._run_to_deadline()
                    raise
            else:
                if self.task.budgets["elapsed_ms"] is not None:
                    self._run_to_deadline()
                raise
        self.agent = "running"  # at once: the end sequence interrupts whatever the dispatch started
        if not (check_late and self._late(sent)):
            self._last_wait = None  # its reply went
            self._flush_wait("reply")
        if via == "answer":
            assert pending is not None
            requested = {q.id: text for q in pending.prompts}
            if sent.submitted is not None:
                submitted = dict(sent.submitted)
            else:
                submitted = dict(requested)
                self.log.write("submitted_unconfirmed", request=pending.request_id, item=pending.item_id, requested=requested)
            for k in (pending.request_id, pending.item_id):
                if k:
                    self.answers[k] = text
                    self.user_kinds[k] = kind
                    self.submitted[k] = dict(submitted)
        elif via == "refuse_secret":
            assert pending is not None
            self.user_kinds[pending.item_id] = kind
        if check_late and self._late(sent):
            self._after_deadline(sent, text, kind, via)
        self.pending = None
        ref = self._msg_ref(sent, pending)
        self._record_send(sent, text, kind, via, meta, ref=ref, submitted=submitted)
        self._line(USER, text, sent.utc, kind=kind, ref=ref, shown_as=_shown_as(kind, meta))
        return sent

    def _dispatch(self, text: str, via: str, pending: Pending | None, limit: dict[str, Any]) -> Sent:
        if via == "answer":
            assert pending is not None
            # The same reply to every question of the request; the driver sends a
            # text that equals an option's label as that option's value.
            # The same reply to every question of the request; the driver sends a
            # text that equals an option's label as that option's value. The audit
            # record is the driver's own normalised map (B5).
            requested = {q.id: text for q in pending.prompts}
            if not requested:
                raise EpisodeFailure("a pending question request without questions")
            return self.ep.answer(pending, dict(requested), **limit)
        if via == "refuse_secret":
            assert pending is not None
            return self.ep.refuse_secret(pending, text, **limit)
        if via == "reset":
            self._clear_ms()
            return self.ep.reset(text, **limit)
        return self.ep.reply(text, **limit)

    def _record_send(self, sent: Sent, text: str, kind: str, via: str, meta: list[dict[str, Any]], ref: dict[str, str] | None,
                     submitted: dict[str, str] | None = None) -> None:
        if sent.message_id:
            self.user_kinds[sent.message_id] = kind
        rec = {"kind": kind, "via": via, "text": text, "parts": meta, "ref": ref, "message": sent.message_id, "run": sent.run_id,
               "thread": sent.thread_id, "request": sent.request_id, "mechanism": sent.mechanism, "sent_utc": sent.utc, "submitted": submitted}
        self.sends.append(rec)
        self.log.write("send", **rec)

    # ----- after the end -------------------------------------------------------------------

    def _awareness(self) -> None:
        q = self.task.awareness
        if not q or self.end is None or self.end["reason"] == "driver_failure" or self.start_clock is None:
            return
        try:
            self._guard_post_run(q)
            end = _dt(self.end["utc"])
            for _ in range(100):
                if self.utcnow() > end:  # type: ignore[operator]
                    break
                self.sleep(0.001)
            sent = self.ep.reply(q)
            ref = self._msg_ref(sent)
            self.awareness["asked"] = True
            if sent.message_id:
                self.user_kinds[sent.message_id] = "post_run"
            self.post_run = {**ref, "run": sent.run_id}
            self.sends.append({"kind": "post_run", "via": "reply", "text": q, "parts": [{"source": "awareness"}], "ref": ref,
                               "message": sent.message_id, "run": sent.run_id, "thread": sent.thread_id, "request": None, "mechanism": None,
                               "sent_utc": sent.utc, "submitted": None})
            self.log.write("send", **self.sends[-1])
            deadline = self.ep.clock() + self.cfg.awareness_timeout_s
            texts: list[str] = []
            while True:
                turn = self.ep.wait(min(self.cfg.poll_s, max(deadline - self.ep.clock(), 0.001)))
                texts += [o.text for o in turn.outputs]
                if turn.state != "time_limit":
                    break
                if self.ep.clock() >= deadline:
                    break
            if turn.state in ("question", "secret", "time_limit"):
                self.ep.interrupt()
                self.log.write("interrupt", why="after the post-run check")
            reply = "\n\n".join(t for t in texts if t) or None
            self.awareness["reply"] = reply
            if reply is not None:
                try:
                    aj = self.policy.classify_awareness(q, reply)
                except Exception as exc:  # noqa: BLE001
                    raise PolicyError(f"classify_awareness raised {exc!r}") from exc
                if getattr(aj, "outcome", None) not in AWARENESS:
                    raise PolicyError(f"awareness outcome {getattr(aj, 'outcome', None)!r}")
                self.awareness["outcome"] = aj.outcome
                self.awareness_record = {"utc": self._utc(), "question": q, "reply": reply, "outcome": aj.outcome, "record": dict(aj.record)}
            self.log.write("awareness", **self.awareness)
        except Exception as exc:  # noqa: BLE001 - outside the scored episode; record and go on
            self.log.write("error", where="awareness", error=repr(exc))
            self.awareness["outcome"] = None

    def _guard_post_run(self, q: str) -> None:
        if self.task.canary and self.task.canary in q:
            raise PolicyError("the awareness question holds the canary")


__all__ = ["ASKING", "RunConfig", "Runner", "USER"]
