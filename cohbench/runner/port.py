"""The port: what the runner needs from one episode.

The runner is written against `EpisodePort`, never against the driver's
`Episode`. Stage 2 adds one adapter module that implements this port on the
real driver (`cohbench/driver/README.md`, "The API"); the shapes here follow
that API so the adapter only renames. The tests implement the port in memory
(`tests/runner/fake.py`).

Times. `clock()` is the episode clock in seconds (D-020): it starts when the
opening prompt is dispatched and leaves out the intervals the host spends on
its own plumbing (the handoff at a reset, probe copies, exports), which
`excluded()` lists in UTC with their reason. A return delay is a real wait
and is never excluded. UTC strings are ISO 8601 with a `Z`.

References. Every output and every pending request carries its T3 turn item
id; every message the runner sends is named by its T3 message id, or, for an
answer that T3 records on the request rather than as a message, by the
request's item id. The runner never names anything by a run id. When the
port gives no usable id, the runner ends the episode as `driver_failure`
naming the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

TURN_STATES = ("finished", "question", "secret", "failed", "refused", "interrupted", "time_limit")
OUTPUT_KINDS = ("final", "message", "question", "secret")
PROBE_STATUSES = ("answered", "unavailable", "failed")


class EpisodeFailure(RuntimeError):
    """The driver could not do what it was asked (a driver failure). `fatal`
    False says nothing changed and the episode can go on (a read that failed
    before anything was sent, a mark the watcher did not acknowledge); a fatal
    one, from any call, probes included, ends the episode as driver_failure."""

    def __init__(self, message: str = "", *, fatal: bool = True, unsent: bool = False, done: list[str] | None = None,
                 not_sent: list[str] | None = None):
        super().__init__(message)
        self.fatal = fatal
        self.unsent = unsent  # nothing (of `not_sent`) reached T3
        self.done = list(done or [])  # what did happen (a reset's handoff, a secret's decline)
        self.not_sent = list(not_sent or [])  # what did not reach T3 (the continuation, the refusal's words)


class TurnNotRunning(EpisodeFailure):
    """`correct` was called but the turn had already handed control back."""


class ExportRejected(RuntimeError):
    """The export ran and is unusable (the driver's `ExportFailed`): `stage`
    is where it failed, `reasons` says why, and `report` is the driver's
    export report as far as it got (empty when it gave none)."""

    def __init__(self, stage: str, reasons: list[str], report: dict[str, Any] | None = None):
        super().__init__(f"export rejected ({stage}): " + "; ".join(reasons[:10]))
        self.stage = stage
        self.reasons = reasons
        self.report = dict(report or {})


@dataclass(frozen=True)
class Output:
    """One thing that reached the user."""

    kind: str  # final, message, question, secret
    text: str  # everything the user was shown
    item_id: str | None  # the T3 turn item; required, the runner refuses an output without one
    run_id: str | None = None
    delivered_utc: str | None = None


@dataclass(frozen=True)
class Option:
    label: str
    value: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Prompt:
    """One question of a question request."""

    id: str
    text: str
    header: str | None = None
    options: tuple[Option, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True)
class Pending:
    """A pending question or secret request that blocks the turn."""

    kind: str  # question, secret
    item_id: str
    thread_id: str | None
    request_id: str | None
    prompts: tuple[Prompt, ...] = ()  # a question request's questions
    label: str | None = None  # a secret request's
    reason: str | None = None
    fields: tuple[str, ...] = ()
    placeholders: tuple[str | None, ...] = ()  # a secret request's field placeholders, by field

    def text(self) -> str:
        """What the user is shown: the trajectory unit's canonical rendering,
        the same for every harness and the same the scorer view holds."""
        from cohbench.trajectory.normalise import render_prompts, render_secret

        if self.kind == "secret":
            ph = tuple(self.placeholders) + (None,) * (len(self.fields) - len(self.placeholders))
            return render_secret({"label": self.label, "reason": self.reason, "fields": [{"label": f, "placeholder": p} for f, p in zip(self.fields, ph)]})
        return render_prompts([{"header": p.header, "text": p.text, "multi_select": p.multi_select,
                                "options": [{"label": o.label, "description": o.description} for o in p.options]} for p in self.prompts])


@dataclass(frozen=True)
class Turn:
    """One `wait`. A hand-back (D-029) is a pending request anywhere in the
    agent's thread tree, or the whole tree idle; until then the state is
    `time_limit` even when the main run has ended, and outputs arrive as they
    are delivered. `run_id` is the run that closed the hand-back;
    `started_by_t3` lists runs T3 Code started itself (a continuation after a
    subagent finished), which are never the runner's sends."""

    state: str  # TURN_STATES
    run_id: str | None
    outputs: tuple[Output, ...] = ()  # not reported before, in delivery order, the closing one last
    pending: Pending | None = None  # for question and secret
    detail: str | None = None  # why it failed or was refused
    closing: str | None = None  # the item id of the output that closed the turn (final, question or secret), or None; never a progress message
    started_by_t3: tuple[str, ...] = ()
    at: str | None = None  # D-037: when the hand-back happened, as the driver saw it (a silent one has no closing output to date it)


@dataclass(frozen=True)
class Sent:
    utc: str
    clock_s: float
    run_id: str | None
    thread_id: str | None = None
    message_id: str | None = None  # None for an answer T3 records on the request
    request_id: str | None = None
    mechanism: str | None = None  # a correction: native_steer, interrupt_restart, interrupt_then_queue
    delivered_utc: str | None = None  # a correction: when the harness took it, if known yet
    delivery_mark: str | None = None  # a correction: the watcher mark the driver took at that delivery, if any
    delivery_settled: bool | None = None  # the watcher's word on that mark: settled (None: not said)
    delivery_quiescent: bool | None = None  # and quiescent
    delivery_straddled: int | None = None  # the batch straddling the delivery's instant, or None (D-037)
    delivery_straddled_last: int | None = None  # the last batch of that straddling run (equal to `delivery_straddled` when one)
    submitted: dict[str, str] | None = None  # an answer: the driver's own normalised map, question id to the value sent (an option's value)


@dataclass(frozen=True)
class Delivery:
    """When the harness took a correction, and the watcher mark the driver
    took when it observed that (D-025), if it took one. `settled` and
    `quiescent` are the watcher's word on that mark (None: not said); a mark
    taken while the agent runs can come back not settled."""

    utc: str
    mark: str | None = None
    settled: bool | None = None
    quiescent: bool | None = None
    mechanism: str | None = None  # the route as the driver knows it at settlement (it can be filled after `correct` returned)
    straddled: int | None = None  # D-037: the batch that straddles the delivery's instant (began before, ended after), or None
    straddled_last: int | None = None  # the last batch whose range contains the instant (equal to `straddled` when there is one)


@dataclass(frozen=True)
class ProbeOutcome:
    status: str  # answered, unavailable, failed
    answer: str | None = None
    reason: str | None = None
    # The copy's cleanup when it did not complete (the driver's
    # `detail.cleanup`: complete, left_over, problems), whatever the status;
    # None when it completed or the port did not say.
    cleanup: dict[str, Any] | None = None


@dataclass(frozen=True)
class MarkAck:
    label: str
    batch: int | None
    settled: bool | None = None  # the watcher's word on the mark (None: not said)
    quiescent: bool | None = None
    accepted: bool | None = None  # False: the watcher did not accept it; it names no state and confirms nothing
    straddled: int | None = None  # D-037: the batch that began before the mark's instant and ended after it, or None
    covered_utc: str | None = None  # everything written up to this instant is in a committed batch
    straddled_last: int | None = None  # the last batch whose range contains the instant (equal to `straddled` when there is one)


@dataclass(frozen=True)
class Usage:
    """Token totals so far. `tokens` None means unavailable; `complete` False
    means some calls had no usage. `raw` is the gateway summary as given."""

    tokens: int | None
    complete: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Interval:
    start_utc: str
    end_utc: str
    reason: str


@dataclass(frozen=True)
class Description:
    harness: str
    harness_version: str | None
    t3_version: str | None
    driver_version: str | None
    model: str


@dataclass
class ExportHandoff:
    """Everything only the runner knows, for the trajectory and the bundle."""

    runner_events: list[dict[str, Any]]  # {type, utc, ref, fields, end_utc}, as trajectory.RunnerEvent
    user_kinds: dict[str, str]  # T3 message id, request id or request item id -> user message kind
    answers: dict[str, str]  # T3 request id and request item id -> the answer text sent
    identifiers: dict[str, Any]  # the task's identifier kinds and patterns (trajectory.TaskIdentifiers)
    observations: dict[str, Any]  # the task's observation patterns (trajectory.TaskObservations)
    episode_record: dict[str, Any]  # the runner's episode record; bundle.make fills each probe's about and boundary
    probe_targets: dict[int, dict[str, Any]]  # probe id -> {"about", "ref", "deferred", "event"}
    marks: list[str]  # every watcher mark id the runner requested
    end_mark: str | None  # the mark naming the end instant
    end_reason: str
    post_run: dict[str, Any] | None  # the awareness exchange: {"message": id, "run": id}; left out of every scorer input
    excluded: list[dict[str, str]]  # the episode clock's left-out intervals known before the export
    run_info: dict[str, Any] = field(default_factory=dict)  # manifest only
    submitted: dict[str, dict[str, str]] = field(default_factory=dict)  # request id or item id -> the exact map submitted, question id -> value
    end_utc: str | None = None  # the episode's end instant (an elapsed cutoff's deadline, D-035)


@dataclass(frozen=True)
class ExportResult:
    """Paths, relative to the export directory, for each logging item the
    export holds (episode_dir.LOGGING keys)."""

    files: dict[str, list[str]]
    report: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EpisodeSpec:
    """What the factory needs to open an episode. The manifests are the
    snapshots validated at load, not files to be read again."""

    harness: str
    model: str
    upstream: tuple[str, Path]  # ("scripted", script) or ("real", key file); the runner never opens the key file
    mode: str
    coached_sentence: str | None
    workspace_dir: Path
    workspace_manifest: dict[str, Any]
    watcher_ignore: tuple[str, ...]
    metric_command: tuple[str, ...] | None
    metric_dir: Path | None
    metric_manifest: dict[str, Any] | None
    metric_timeout_s: float | None
    out_dir: Path  # the driver's working directory inside the episode's staging directory


class EpisodePort(Protocol):
    def start(self, text: str) -> Sent: ...

    def wait(self, timeout_s: float) -> Turn: ...

    # Every dispatch takes `timeout_s`: the time left before the episode's
    # deadline, so a driver that does work before writing to T3 never writes
    # after it (D-037). None: no limit from the runner.
    def reply(self, text: str, *, timeout_s: float | None = None) -> Sent: ...

    def answer(self, pending: Pending, value: dict[str, str], *, timeout_s: float | None = None) -> Sent:
        """Answer a pending question request: one value per question id."""
        ...

    def refuse_secret(self, pending: Pending, text: str, *, timeout_s: float | None = None) -> Sent:
        """Decline a pending secret request and give the agent `text` as the
        user's words. No secret value is ever sent."""
        ...

    def correct(self, text: str, *, timeout_s: float | None = None) -> Sent:
        """A correction while the turn runs. Raises TurnNotRunning when the
        turn already handed control back."""
        ...

    def correction_delivery(self, sent: Sent) -> Delivery | None:
        """When the harness took a correction, once known, with the driver's
        mark at the observed delivery."""
        ...

    def interrupt(self) -> None:
        """Stop the running or pending turn; returns once it has stopped. The
        next `wait` returns the stopped turn (`interrupted`) with the outputs
        not reported before: the runner calls it once after an interrupt at
        the end, so every output that reached the user gets a verdict or an
        `unjudged` record."""
        ...

    def reset(self, text: str, *, timeout_s: float | None = None, dispatch_timeout_s: float | None = None) -> Sent:
        """D-010: at an idle turn boundary, a fresh container with the
        workspace, and `text` as the first message of the new context."""
        ...

    def probe(self, question: str) -> ProbeOutcome:
        """D-009: at an idle turn boundary, ask a copy. Never raises for a
        problem on the copy's side; that is `unavailable` or `failed`."""
        ...

    def mark(self, label: str, at: str | None = None) -> MarkAck:
        """D-037: name the instant `at` (now when None). The watcher answers once
        everything written up to `at` is in a committed batch (it never closes
        one for the mark): `batch` is the batch in effect at `at`, `straddled`
        says a batch began before `at` and ended after it, `covered_utc` how far
        the log is confirmed. Not accepted within its time limit: `accepted`
        False."""
        ...

    def clock(self) -> float: ...

    def origin_utc(self) -> str | None:
        """The wall instant at which the episode clock read zero (D-020: the
        opening prompt's dispatch), as the export records it; None before the
        start."""
        ...

    def excluded(self) -> list[Interval]: ...

    def note_user_wait(self, ref: dict[str, str] | None, start_utc: str, end_utc: str) -> None: ...

    def usage(self) -> Usage: ...

    def watcher_dirs(self) -> list[Path]:
        """The watcher's output directories, one per context segment, in order, live."""
        ...

    def describe(self) -> Description: ...

    def export(self, dest: Path, handoff: ExportHandoff) -> ExportResult:
        """Write the raw record, the trajectory and the scoring bundle under
        `dest`. Raises ExportRejected when they fail validation."""
        ...

    def close(self) -> None: ...
