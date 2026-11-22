"""One episode, driven through T3 Code: the operations a runner calls.

    images, _ = cohbench.env.provision("pi")                  # once, shared by episodes
    with Episode("pi", model="claude-haiku-5-5", upstream=Upstream.scripted(script), images=images,
                 workspace=Path("task/ws"), manifest=Path("task/ws.json"), out_dir=Path("runs/e1")) as ep:
        ep.start("Opening prompt")
        turn = ep.wait(timeout_s=600)          # finished, question, secret, failed, refused, interrupted, time_limit
        ep.answer(turn.question, "blue")       # a question or secret request carries its thread
        ep.record_user_wait(turn, started_utc, ended_utc)
        ep.reply("Next message")
        ep.correct("Stop after this step")     # native steer, or interrupt and restart (marked)
        ep.reset("Fresh-context message")      # D-010 handoff, next segment
        p = ep.probe("Closed question?", scripted_reply="no")   # outcome: answered, unavailable or failed
        ep.export(Path("runs/e1/export"), task=TaskInfo(...), end_mark="end")   # raw record, trajectory, scoring bundle

Every public call takes `timeout_s` and holds to it: one monotonic deadline
is passed down to every T3 read and call and every Docker command it runs
(`cohbench.env.docker.deadline`). Removing what was created is the one
exception: cleanup is never cut short. `wait` is the one call allowed past
its timeout, by at most `finish_s` (see `wait`).

Every send records its `Sent`, with the ids the driver generated, before it
is dispatched, and reconciles a lost or timed-out answer by those ids:
accepted, rejected (EpisodeError, or TurnNotRunning for a correction that
lost the race with the run's end), or unknown, which raises DriverFailure.

`close` (also on leaving the `with` block, after an exception, or after a
SIGTERM turned into SystemExit by `install_sigterm`) removes every
container, network, volume and snapshot image the episode made; the shared
images from `provision` stay.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from cohbench.env import Environment, HarnessPaths, Upstream, WatcherSpec, utc_now
from cohbench.env.docker import Container, DockerError, KeyLeak, deadline as docker_deadline, no_deadline, write_scanned
from cohbench.env.environment import WORKSPACE, HandoffFailed, MainNotResumed, ProbeUnavailable
from cohbench.env.images import Images

from . import turns
from .harness import DRIVER_REF_NAMESPACES, PI_DIALOG_EXTENSION_NAME, Harness, harness as get_harness, instruction_files
from .t3 import T3
from .t3client import NODE_MARGIN_S, RpcFailure, RpcUnsent, T3Error, new_id
from .usage import instant, read_calls, settled_usage


class EpisodeError(RuntimeError):
    pass


class TurnNotRunning(EpisodeError):
    """A correction found the turn already handed back: before it was sent,
    or T3 rejected it because the run had ended in between."""


class DriverFailure(EpisodeError):
    """The driver could not establish what happened. `fatal` True (a send
    whose acceptance stayed unknown, a main container that could not be
    resumed, a handoff that stopped part way): the episode cannot go on and
    every later call raises again; close it. `fatal` False (a read of T3
    failed before anything was sent): nothing changed, and the call may be
    retried."""

    def __init__(self, message: str, *, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


class MainRunEnded(EpisodeError):
    """A correction found the main run ended while child work remains (a
    live, queued or background subagent run, or a continuation T3
    reserved). Nothing was sent; retryable: hold the correction for the
    continuation run or the hand-back, whichever comes first."""

    retryable = True


DRIVER_EVENTS = ("user_kinds", "controlled_reset", "correction_delivered", "probe", "episode_end")

# The bound on how far `wait` may run past its timeout: the reads that
# collect the hand-back's outputs (send_to_user payloads, refusal evidence)
# and the turn-end watcher mark share it.
FINISH_S = 15.0
# How long the tree must stay idle, read twice this far apart, before it
# counts as handed back or stopped. T3 marks a delegated completion pending
# on the parent's run in the same projection (`delegatedCompletion`); the
# second read covers a child whose end and that reservation are written
# separately, which I have not confirmed either way.
SETTLE_S = 1.0
# A target `interrupt` sent a stop to and still finds busy is sent again
# after this long.
RETRY_S = 1.5
# Reads in a row with a thread that could not be read before `wait` gives up
# with a fatal DriverFailure instead of reporting time limits.
UNREADABLE_LIMIT = 10
# Reads of one send_to_user payload that fail before `wait` gives up with a
# fatal DriverFailure naming the item.
UNRESOLVED_LIMIT = 10


class _Rejected(Exception):
    """T3 answered a send with a failure."""

    def __init__(self, exc: RpcFailure):
        super().__init__(str(exc))
        self.rpc = exc


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Delivery:
    """When the harness took a correction, and the watcher mark the driver
    asked for the moment it observed that: the correction's boundary in the
    change log. `mark_taken` False says no mark could be taken, with why in
    `mark_error`; `delivered_utc` None says delivery was not observed yet."""

    delivered_utc: str | None
    mark_label: str | None = None
    mark_batch: int | None = None
    mark_requested_utc: str | None = None
    mark_acked_utc: str | None = None
    mark_taken: bool = False
    mark_error: str | None = None
    # The instant the mark names (the delivery, D-037), the batch that
    # straddles it (began before, ended after) or None, and the instant up to
    # which the watcher confirmed coverage.
    mark_at: str | None = None
    mark_straddled: int | None = None
    mark_covered_utc: str | None = None


@dataclass
class Sent:
    """What one operation sent, to line it up with the export later. Recorded
    (status `dispatching`) before the send, with the ids the driver chose."""

    kind: str  # opening, reply, correction, reset, answer, secret, refusal
    segment: int
    thread_id: str
    message_id: str | None
    command_id: str | None
    run_id: str | None  # None when T3 accepted the message but its run was not seen yet
    sent_utc: str
    episode_s: float  # on the episode clock
    text: str | None = None  # never set for a secret
    mechanism: str | None = None  # a correction: as observed once its run is known; a refusal: the route taken
    request_id: str | None = None  # an answer or a secret: the request's T3 turn item id
    delivered_utc: str | None = None  # a correction: when the harness took it, once known
    status: str = "dispatching"  # accepted, rejected, unknown (then DriverFailure was raised)
    rpc_request_id: str | None = None  # an answer: T3's runtime request id
    acked_utc: str | None = None  # when T3's acceptance was known
    delivery: Delivery | None = None  # a correction: delivery and the driver's mark, once observed
    route_basis: str | None = None  # a refusal: why its route (a live main run, the run had ended, time ran out)
    submitted: dict[str, str] | None = None  # an answer: exactly what was sent to T3, question id to value (an option's value where the answer matched one)


@dataclass
class TurnResult:
    state: str  # finished, question, secret, failed, refused, interrupted, time_limit
    segment: int
    thread_id: str
    run_id: str | None
    run_status: str | None
    final_text: str | None = None
    question: turns.Question | None = None
    secret: turns.SecretRequest | None = None
    user_channel: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refusal: dict[str, Any] | None = None
    waited_s: float = 0.0
    ended_utc: str = ""
    mark: dict[str, Any] | None = None
    final_item_id: str | None = None
    # Every user-channel output not reported by an earlier wait of this
    # episode (each turn item once), in delivery order: {kind (final,
    # message, question, secret), text, item_id, run_id, thread_id,
    # delivered_utc}.
    outputs: list[dict[str, Any]] = field(default_factory=list)
    # The main thread's runs since the previous hand-back, in order: {run_id,
    # started_by (user, t3 or unknown), status, message_id}. `run_id` above is
    # the one that closed this hand-back.
    runs: list[dict[str, Any]] = field(default_factory=list)
    # Every request pending in the tree, in delivery order: {kind, item_id,
    # thread_id, request_id, actionable (the one to answer, also in
    # `question` or `secret`), new (not returned by an earlier wait)}.
    requests: list[dict[str, Any]] = field(default_factory=list)
    # send_to_user items whose payload could not be read yet; the next wait
    # retries them, the hand-back is not final meanwhile, and no output
    # delivered at or after the earliest of them is given out yet.
    unresolved: list[str] = field(default_factory=list)
    # The hand-back's instant: the delivery of the output that closed it, or,
    # with none, the instant the agent stopped: the latest completion among
    # the tree's runs (D-035 as refined). The turn-end mark names it;
    # `ended_utc` is when the driver reported it.
    handback_utc: str | None = None


@dataclass
class ProbeResult:
    """`answered` (a non-empty answer, with the continuity evidence),
    `unavailable` (setup failed, checks failed, the copy did not resume the
    session, time limit) or `failed` (the copy's run failed or gave no
    answer, or the copy's cleanup found the key)."""

    outcome: str
    question: str
    answer: str | None
    reason: str | None
    detail: Any
    continuity: dict[str, Any]
    seconds: float
    sent_utc: str | None = None
    taken_utc: str | None = None
    # False when the copy's removal did not complete (what is left stays
    # registered and close() retries it); `detail["cleanup"]` says what.
    cleanup_complete: bool | None = None


@dataclass
class TaskInfo:
    """What the task gives the export (D-018): identifier kinds and
    observation patterns as the trajectory unit takes them, and the episode
    record the runner writes."""

    identifiers: Any = None
    observations: Any = None
    episode: dict[str, Any] | None = None


def install_sigterm() -> None:
    """Turn SIGTERM into SystemExit in the main thread, so `with Episode(...)`
    and `try/finally` blocks run `close`."""

    def handler(signum, frame):  # noqa: ARG001
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def utc_us() -> str:
    """UTC with microseconds: the episode clock's interval stamps."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Clock:
    """The episode clock: starts when the opening prompt is dispatched and
    leaves out host-side infrastructure intervals (handoff plumbing, probes,
    exports), each recorded with its reason and microsecond start and end
    times. An interval of zero length is not recorded. The raw wall clock is
    the environment's."""

    def __init__(self) -> None:
        self.t0: float | None = None
        self.start_utc: str | None = None
        self.excluded: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []
        self.end_utc: str | None = None
        self._frozen: float | None = None

    def begin(self) -> None:
        if self.t0 is None:
            self.t0, self.start_utc = time.monotonic(), utc_now()

    def elapsed_at(self, utc: str) -> float:
        """Episode-clock seconds at a wall instant (UTC): the time since the
        start less the excluded time before that instant."""
        import datetime as _dt

        def ts(x: str) -> _dt.datetime:
            return _dt.datetime.fromisoformat(x.replace("Z", "+00:00"))

        if self.start_utc is None:
            return 0.0
        end = ts(utc)
        total = (end - ts(self.start_utc)).total_seconds()
        for e in self.excluded + [{"start_utc": h["start_utc"], "end_utc": None} for h in self._open if h["outer"]]:
            a = max(ts(e["start_utc"]), ts(self.start_utc))
            b = min(ts(e["end_utc"]) if e["end_utc"] else end, end)
            if b > a:
                total -= (b - a).total_seconds()
        return max(0.0, total)

    def freeze(self, end_utc: str) -> None:
        """Stop the episode clock at the scored end, which may be earlier than
        now (D-035: an elapsed cutoff ends at its deadline). Later time,
        cleanup included, is on the wall clock only."""
        if self._frozen is None:
            self.end_utc = end_utc
            self._frozen = self.elapsed_at(end_utc)

    def elapsed(self) -> float:
        if self._frozen is not None:
            return self._frozen
        if self.t0 is None:
            return 0.0
        now = time.monotonic()
        out = sum(e["seconds"] for e in self.excluded) + sum(now - h["mono"] for h in self._open[:1])
        return max(0.0, now - self.t0 - out)

    def open(self, reason: str) -> dict[str, Any]:
        """Start leaving time out; returns the handle `close` takes. Its
        `start_utc` is the stamp the driver's event for the operation
        carries. Nested intervals count once (the outer one)."""
        h = {"reason": reason, "mono": time.monotonic(), "start_utc": utc_us(), "outer": not self._open}
        self._open.append(h)
        return h

    def close(self, h: dict[str, Any]) -> None:
        if h in self._open:
            self._open.remove(h)
        if h["outer"] and self.t0 is not None:
            end_utc = utc_us()
            seconds = time.monotonic() - max(h["mono"], self.t0)
            if end_utc > h["start_utc"]:
                self.excluded.append({"reason": h["reason"], "start_utc": h["start_utc"], "end_utc": end_utc, "seconds": round(max(0.0, seconds), 6)})

    @contextlib.contextmanager
    def exclude(self, reason: str) -> Iterator[str]:
        """Leave an interval out of the episode clock; yields its start time
        (UTC, microseconds), which the driver's event for the operation
        carries."""
        h = self.open(reason)
        try:
            yield h["start_utc"]
        finally:
            self.close(h)


def _left(at: float) -> float:
    return at - time.monotonic()


# Child threads read at once by one tree read.
TREE_READERS = 4


def _shell_key(row: dict[str, Any] | None) -> tuple | None:
    """What says a thread changed, from its shell row (one tree-wide read)."""
    if row is None:
        return None
    return (row.get("updatedAt"), row.get("status"), row.get("itemCount"), row.get("latestRunId"), row.get("activityRunStatus"),
            bool(row.get("pendingRuntimeRequest")), len(row.get("pendingBackgroundTasks") or []))


def _settled(proj: dict[str, Any]) -> bool:
    """A thread read with nothing that can still move on its own: every run
    terminal, nothing pending, reserved or in the background."""
    qs, ss = turns.pending_requests(proj)
    return (all(r.get("status") in turns.TERMINAL for r in proj.get("runs", [])) and not qs and not ss and not turns.pending_continuations(proj)
            and not any(pt.get("pendingBackgroundTasks") for pt in proj.get("providerThreads", [])))


class Sample(dict):
    """A tree read: thread id -> projection or the exception its read raised,
    plus `consistent` and `reason` (see `read_tree`)."""

    consistent: bool = False
    reason: str = ""


def read_tree(t3: Any, root: str, at: float, cache: dict[str, tuple[dict[str, Any], tuple]]) -> Sample:
    """One sample of the thread tree, used only if it is consistent.

    In order: T3's shell rows (one read of every thread's summary row); the
    main thread; the children, level by level, up to TREE_READERS at once, a
    child read as settled earlier and whose shell row is unchanged taken
    from the cache instead (nothing can restart a settled thread without T3
    updating its row); then the shell rows again. The sample is consistent
    only when both shell reads succeeded, no row of a thread in the sample
    differs between them, every thread was read, and every child any
    projection names was read. Otherwise `consistent` is False with the
    reason, and the caller discards the sample whole. The cost: a tree that
    is busy can take several polls to give a consistent sample."""
    import concurrent.futures as cf
    import contextvars

    out = Sample()

    def shell() -> dict[str, dict[str, Any]] | None:
        try:
            return turns.shell_rows(t3.shell(timeout_s=_left(at)))
        except Exception:
            return None

    rows = shell()
    proj = t3.snapshot(root, timeout_s=_left(at))["projection"]
    out[root] = proj

    def children(p: dict[str, Any]) -> list[str]:
        return [x["childThreadId"] for x in p.get("subagents", []) if x.get("childThreadId")]

    def one(t: str) -> dict[str, Any] | Exception:
        key = _shell_key(rows.get(t)) if rows is not None else None
        hit = cache.get(t)
        if hit is not None and key is not None and hit[1] == key:
            return hit[0]
        try:
            p = t3.snapshot(t, timeout_s=_left(at))["projection"]
        except Exception as exc:
            cache.pop(t, None)
            return exc
        if key is not None and _settled(p):
            cache[t] = (p, key)
        else:
            cache.pop(t, None)
        return p

    level = [t for t in children(proj) if t not in out]
    with cf.ThreadPoolExecutor(max_workers=TREE_READERS) as pool:
        while level:
            futs = {t: pool.submit(contextvars.copy_context().run, one, t) for t in dict.fromkeys(level) if t not in out}
            nxt: list[str] = []
            for t, f in futs.items():
                out[t] = f.result()
                if isinstance(out[t], dict):
                    nxt += [c for c in children(out[t]) if c not in out]
            level = nxt
    rows2 = shell()
    reason = ""
    if rows is None or rows2 is None:
        reason = "a shell read failed"
    elif any(not isinstance(p, dict) for p in out.values()):
        reason = "a thread could not be read"
    else:
        changed = [t for t in out if _shell_key(rows.get(t)) != _shell_key(rows2.get(t))]
        unread = [c for p in out.values() for c in children(p) if c not in out]
        if changed:
            reason = f"threads changed while the sample was read: {[t[-16:] for t in changed][:5]}"
        elif unread:
            reason = f"children not in the sample: {[t[-16:] for t in unread][:5]}"
    out.consistent = not reason
    out.reason = reason
    if not out.consistent:
        # Nothing of an inconsistent sample is kept.
        for t in list(out):
            if t != root:
                cache.pop(t, None)
    return out


def _ts(x: str | None) -> Any:
    """A T3 or driver UTC stamp, parsed for ordering; a missing one sorts last."""
    import datetime as _dt

    if not x:
        return _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
    return _dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _reserve(at: float) -> float:
    """The time a send keeps for reconciling a lost answer: a third of what
    is left, at most 10 s, but never so much that the dispatch itself has
    too little to start (NODE_MARGIN_S plus half a second)."""
    left = max(0.0, _left(at))
    return max(0.0, min(min(10.0, left / 3), left - NODE_MARGIN_S - 0.5))


# ---------------------------------------------------------------------------
# The episode
# ---------------------------------------------------------------------------


class Episode:
    def __init__(
        self,
        harness: str,
        *,
        model: str,
        upstream: Upstream,
        images: Images,
        workspace: Path | bytes,
        manifest: Any,
        out_dir: Path,
        mode: str = "default",
        coached_sentence: str | None = None,
        options: dict[str, str] | None = None,
        harness_default_model: bool | None = None,
        diagnostic_question_tool: bool = False,
        watcher: WatcherSpec | None = WatcherSpec(),
        title: str = "task",
        workspace_path: str = WORKSPACE,
        driver_events: tuple[str, ...] = DRIVER_EVENTS,
    ):
        if mode not in ("default", "coached"):
            raise ValueError("mode is default or coached")
        if mode == "coached" and not coached_sentence:
            raise ValueError("coached mode needs the sentence")
        if images is None:
            raise ValueError("pass the images from cohbench.env.provision")
        self.h: Harness = get_harness(harness)
        self.model = model
        self.options = dict(self.h.options if options is None else options)
        self.upstream = upstream
        self.provider = upstream.provider.name
        self.h.model_slug(model, self.provider)  # fails early for an unknown provider
        # Reference harness: its own default model set to the model under
        # test (a recorded run setting). Native harness: nothing written.
        self.harness_default_model = self.h.reference if harness_default_model is None else harness_default_model
        if self.harness_default_model and not self.h.reference:
            raise ValueError(f"{self.h.name} is a native harness; it runs at its defaults")
        self.diagnostic_question_tool = diagnostic_question_tool
        self.mode = mode
        self.coached_sentence = coached_sentence
        self.title = title
        self.out_dir = Path(out_dir).resolve()
        self.workspace_path = workspace_path
        self.images = images
        self._workspace = workspace
        self._manifest = manifest
        if watcher is not None:
            # D-026, from the harness descriptions: every harness's instruction
            # file and T3's ref namespace are never recorded.
            watcher = dataclasses.replace(watcher, ignore_names=watcher.ignore_names if watcher.ignore_names is not None else instruction_files(),
                                          ignore_git_refs=watcher.ignore_git_refs if watcher.ignore_git_refs is not None else DRIVER_REF_NAMESPACES)
        self._watcher = watcher
        unknown = set(driver_events) - set(DRIVER_EVENTS)
        if unknown:
            raise ValueError(f"unknown driver events {sorted(unknown)}; known: {DRIVER_EVENTS}")
        # What the driver records itself; the caller turns one off to own it.
        self.driver_events = tuple(driver_events)
        self.env: Environment | None = None
        self.t3: T3 | None = None
        self.thread_id: str | None = None
        self.run_id: str | None = None
        self._pending_message: str | None = None
        self.segment = 1
        self.clock = Clock()
        self.sent: list[Sent] = []
        self.turn_results: list[dict[str, Any]] = []
        self.corrections: list[dict[str, Any]] = []
        self.probes: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.marks: list[dict[str, Any]] = []
        self.runner_events: list[dict[str, Any]] = []
        self.user_waits: list[dict[str, Any]] = []
        self.segment_threads: dict[int, str] = {}
        self.answers: dict[str, str] = {}
        self._reported: set[str] = set()
        self._cycle_after = 0  # the main thread's run ordinal that closed the last hand-back
        self._idle_seen: tuple[Any, float] | None = None  # (tree signature, monotonic) of the first idle read, kept across waits
        self._unreadable_reads = 0
        self._unreadable_reason = ""
        self._unresolved_reads: dict[str, int] = {}
        self.reads = 0  # tree reads `wait` attempted, for tests and diagnosis
        self.reads_ok = 0  # of those, the ones that returned the main thread's projection
        self._tree_cache: dict[str, tuple[dict[str, Any], tuple]] = {}  # settled children, by shell key (read_tree)
        self._usage_interrupts: list[dict[str, str]] = []
        self._usage_snapshot: dict[str, Any] | None = None
        self._interrupted = False  # interrupt stopped something; the next hand-back is interrupted
        self._last_interrupt = ""  # the receipt of the interrupt-then-queue path's interrupt
        self._force_refusal_queue = False  # test hook: refuse_secret takes the queued route
        self.awaiting_launch: str | None = None  # a handoff's continuation not yet sent (reset retries it)
        self._handoff_started: str = ""  # turn item ids already given out in TurnResult.outputs
        self.captured: dict[int, dict[str, Any]] = {}  # segment -> its T3 record, taken from the stopped container
        self.exports: list[Path] = []
        self.failure: str | None = None
        self.closed = False
        self.settings_written: dict[str, str] = {}

    # ----- lifecycle --------------------------------------------------------------------

    def __enter__(self) -> "Episode":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def left_over(self) -> list[tuple[str, str]]:
        """Resources still registered after close (a later close retries them)."""
        return self.env.left_over if self.env is not None else []

    @property
    def started_utc(self) -> str | None:
        return self.clock.start_utc

    def elapsed(self) -> float:
        """Seconds on the episode clock (see `Clock`)."""
        return self.clock.elapsed()

    def wall_elapsed(self) -> float:
        """Raw wall-clock seconds since the environment began starting."""
        return self.env.wall_elapsed() if self.env else 0.0

    def usage(self) -> dict[str, Any]:
        """Gateway tokens, with calls canceled by this driver settled at their interrupt."""
        if self.env is None:
            return {}
        if not self._usage_interrupts:
            return self.env.usage()
        if getattr(self.env, "final_usage", None) is None:
            self._usage_snapshot = read_calls(self.env)
        if self._usage_snapshot is None:
            return self.env.usage()
        return settled_usage(self._usage_snapshot, self._usage_interrupts)

    def _prepare(self, c: Container, role: str) -> None:
        """Run in every harness container before the checks: the settings this
        project writes, then T3 ready (and, for a segment, the project)."""
        if role == "segment":
            files = dict(self.h.default_model_files(self.model, self.options, self.provider)) if self.harness_default_model else {}
            if self.diagnostic_question_tool:
                from .harness import PI_DIALOG_EXTENSION

                files[f"/home/node/.pi/agent/extensions/{PI_DIALOG_EXTENSION_NAME}"] = PI_DIALOG_EXTENSION
            for path, body in files.items():
                c.exec(["sh", "-c", 'mkdir -p "$(dirname "$0")" && cat > "$0"', path], input_text=body)
            self.settings_written = {p: "written" for p in files}
            t3 = T3(c)
            t3.connect()
            t3.create_project(self.workspace_path)
            self.t3 = t3
        else:
            T3(c).connect()

    def open(self) -> None:
        """Start the environment (checks included), connect to T3 and create
        the project. `start` calls this when it has not run."""
        if self.env is not None:
            return
        instruction = (self.h.instruction_file, self.coached_sentence) if self.mode == "coached" else None
        text_files = tuple(self.h.default_model_files(self.model, self.options, self.provider)) if self.harness_default_model else ()
        if self.diagnostic_question_tool:
            text_files += (f"/home/node/.pi/agent/extensions/{PI_DIALOG_EXTENSION_NAME}",)
        self.env = Environment(
            self.images, self.upstream, workspace=self._workspace, manifest=self._manifest, out_dir=self.out_dir / "env", watcher=self._watcher,
            instruction=instruction, prepare=self._prepare, harness_paths=HarnessPaths(self.h.home_dirs, text_files), workspace_path=self.workspace_path,
            key_guard=(self.out_dir,),
        )
        self.env.start()

    def close(self) -> list[dict]:
        """Remove everything. Safe to call more than once and after a
        failure; a later call retries whatever is still registered
        (`left_over`)."""
        if self.closed:
            return self.env.close() if self.env is not None and self.env.left_over else []
        self.closed = True
        results: list[dict] = []
        try:
            if self.env is not None:
                if self._update_exports not in self.env.on_closed:
                    self.env.on_closed.append(self._update_exports)
                results = self.env.close()
        finally:
            self._write_state()
        return results

    def _keys(self) -> list:
        return self.env.keys() if self.env is not None else [k for k in (self.upstream.key,) if k is not None]

    def _update_exports(self, env: Environment) -> None:
        """Each published export gets the final removal results, and its
        manifest the end of the export's own excluded interval (open when it
        was written); scanned, then renamed into place."""
        for d in self.exports:
            if not d.exists():
                continue
            write_scanned(d / "environment.json", json.dumps(env.record(), indent=1, default=str), self._keys())
            try:
                m = json.loads((d / "manifest.json").read_text())
                for e in m.get("clock", {}).get("excluded", []):
                    if e.get("reason") == "export" and e.get("end_utc") is None:
                        done = next((x for x in self.clock.excluded if x["reason"] == "export" and x["start_utc"] == e.get("start_utc")), None)
                        if done is not None:
                            e["end_utc"] = done["end_utc"]
                write_scanned(d / "manifest.json", json.dumps(m, indent=1, ensure_ascii=False, default=str), self._keys())
            except (OSError, json.JSONDecodeError):
                pass

    def _write_state(self) -> None:
        try:
            write_scanned(self.out_dir / "episode.json", json.dumps(self.state(), indent=1, default=str), self._keys())
        except Exception:
            pass

    def state(self) -> dict[str, Any]:
        return {
            "harness": self.h.name, "model": self.model, "provider": self.provider, "options": self.options, "mode": self.mode,
            "harness_default_model": self.harness_default_model, "diagnostic_question_tool": self.diagnostic_question_tool,
            "upstream": self.upstream.kind, "started_utc": self.started_utc, "segment": self.segment, "segment_threads": self.segment_threads,
            "clock": {"episode_s": round(self.elapsed(), 3), "wall_s": round(self.wall_elapsed(), 3), "excluded": self.clock.excluded},
            "sent": [asdict(s) for s in self.sent], "turns": self.turn_results, "corrections": self.corrections, "probes": self.probes,
            "handoffs": self.handoffs, "marks": self.marks, "runner_events": self.runner_events, "user_waits": self.user_waits, "failure": self.failure,
        }

    def _fail(self, message: str) -> DriverFailure:
        self.failure = message
        return DriverFailure(message)

    def _require_live(self) -> None:
        if self.failure is not None:
            raise DriverFailure(f"the episode already failed: {self.failure}")
        if self.t3 is None or self.thread_id is None:
            raise EpisodeError("no turn yet; call start first")


    def _guard(self, *texts: str | None) -> None:
        """Every outbound user text is checked for the upstream credential
        before anything is sent."""
        for t in texts:
            if t and any(k.secret in t for k in self._keys()):
                raise EpisodeError("refused: the text holds the upstream credential; nothing was sent")


    # ----- runs ---------------------------------------------------------------------------

    def _snapshot(self, at: float, tid: str | None = None) -> dict[str, Any]:
        assert self.t3 is not None
        return self.t3.snapshot(tid or self.thread_id or "", timeout_s=_left(at))["projection"]

    def _run_known(self, s: Sent, run_id: str) -> None:
        """A send's run became known: record it, and for a correction the
        mechanism as observed (a steer T3 answered with a new run is
        recorded as what happened)."""
        s.run_id = run_id
        if s.message_id == self._pending_message:
            self.run_id, self._pending_message = run_id, None
        if s.kind == "correction":
            rec = next((c for c in self.corrections if c["message_id"] == s.message_id), None)
            if rec is not None:
                rec["run_after"] = run_id
                observed = rec["requested"]
                if rec["requested"] == "native_steer" and run_id != rec["run_before"]:
                    observed = "steer_requested_new_run"
                rec["observed"] = s.mechanism = observed

    def _current_run(self, proj: dict[str, Any] | None) -> str | None:
        """The run of the last message sent to the main thread, resolved from
        `proj` when it was not known yet."""
        if self.run_id is None and self._pending_message is not None and proj is not None:
            msg = next((m for m in proj.get("messages", []) if m["id"] == self._pending_message), None)
            run = (msg or {}).get("runId") or next((r["id"] for r in proj.get("runs", []) if r.get("userMessageId") == self._pending_message), None)
            if run:
                for s in self.sent:
                    if s.message_id == self._pending_message:
                        self._run_known(s, run)
        return self.run_id

    def _resolve_run(self, s: Sent, at: float, projection: dict[str, Any] | None = None) -> None:
        """Look up the run of an accepted message within the deadline. A run
        that reconciliation already found is kept. A failed or slow lookup
        leaves it pending: `wait` resolves it later."""
        assert self.t3 is not None and s.message_id is not None
        if s.run_id is not None:
            self._run_known(s, s.run_id)
            self.run_id, self._pending_message = s.run_id, None
            return
        self.run_id, self._pending_message = None, s.message_id
        try:
            with docker_deadline(at):
                run = self.t3.run_for_message(s.thread_id, s.message_id, projection, timeout_s=_left(at))
            self._run_known(s, run)
        except (T3Error, DockerError):
            pass


    def _tree(self, at: float) -> tuple[dict[str, Any], dict[str, Any]]:
        """One projection of the whole thread tree and its state
        (`turns.busy_state`, the one definition of busy). A failed read of the
        main thread raises DriverFailure (not fatal); a child that cannot be
        read is in the state's `unreadable`, which is neither idle nor busy."""
        assert self.t3 is not None and self.thread_id is not None
        try:
            with docker_deadline(at):
                tree = read_tree(self.t3, self.thread_id, at, self._tree_cache)
        except (T3Error, DockerError) as exc:
            raise DriverFailure(f"the thread tree could not be read: {exc}"[:300], fatal=False) from None
        root = tree.get(self.thread_id)
        if not isinstance(root, dict):
            raise DriverFailure(f"the main thread could not be read: {type(root).__name__}: {root}"[:300], fatal=False)
        self._current_run(root)
        st = turns.busy_state(tree, self.thread_id)
        st["consistent"] = getattr(tree, "consistent", True)
        st["sample_reason"] = getattr(tree, "reason", "")
        if not st["consistent"]:
            st["idle"] = False
        if self._pending_message is not None:
            # An accepted message whose run has not appeared yet: the tree is
            # busy, whatever the older runs say (it is never matched to one).
            st["pending_message"] = self._pending_message
            st["idle"] = False
        return tree, st



    def _mark(self, label: str, at: float | None = None, *, instant: str | None = None) -> dict[str, Any] | None:
        """A watcher mark naming `instant` (the event it stands for: a turn
        end, a delivery; D-037), within the deadline `at`; recorded, never
        fatal to the turn. A mark that is not accepted is recorded as such
        and names no state."""
        if self.env is None or self._watcher is None:
            return None
        try:
            if at is not None and _left(at) <= 0.2:
                raise DockerError("no time left before the deadline")
            with docker_deadline(at):
                ack = self.env.mark(label, at=instant)
        except Exception as exc:
            ack = {"ok": False, "accepted": False, "label": label, "at": instant, "error": str(exc)[:300]}
        self.marks.append({"label": label, "segment": self.segment, **{k: ack.get(k) for k in ("ok", "accepted", "at", "batch", "straddled", "covered_utc", "requested_utc", "acked_utc", "seconds", "error")}})
        return ack

    # ----- sending ----------------------------------------------------------------------------

    def _new_sent(self, kind: str, tid: str, *, text: str | None = None, mechanism: str | None = None, with_message: bool = True) -> Sent:
        s = Sent(kind, self.segment, tid, new_id() if with_message else None, new_id(), None, utc_now(), round(self.elapsed(), 3), text, mechanism)
        self.sent.append(s)
        return s

    def _reconcile(self, s: Sent, at: float, check: Callable[[], bool | None], what: str) -> None:
        """After a send whose answer was lost or late: read until the deadline
        whether T3 has it. `check` returns True (it is there), or raises on a
        failed read. Unknown at the deadline is a driver failure."""
        last = "no read completed"
        while True:
            try:
                with docker_deadline(at):
                    if check():
                        s.status, s.acked_utc = "accepted", utc_now()
                        return
                last = f"{what} not in T3's record yet"
            except (T3Error, DockerError) as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
            if _left(at) <= 0.6:
                s.status = "unknown"
                raise self._fail(f"{s.kind} {s.command_id}: acceptance unknown at the deadline ({last})")
            time.sleep(max(0.0, min(0.5, _left(at))))

    def _send(self, s: Sent, at: float, dispatch: Callable[[float], Any], check: Callable[[], bool | None], what: str) -> Any:
        """Dispatch with the ids already recorded in `s`. A third of the time
        left (at most 10 s) is kept for reconciliation."""
        reserve = _reserve(at)
        try:
            with docker_deadline(at - reserve):
                out = dispatch(_left(at - reserve))
            s.status, s.acked_utc = "accepted", utc_now()
            return out
        except RpcFailure as exc:
            s.status = "rejected"
            raise _Rejected(exc) from None
        except RpcUnsent as exc:
            raise self._unsent(s, exc) from None
        except (T3Error, DockerError):
            self._reconcile(s, at, check, what)
            return None

    def _unsent(self, s: Sent, exc: Exception) -> DriverFailure:
        """The call never reached T3 (no process was started, or the socket
        never carried it): nothing changed. Recorded as `unsent`; a non-fatal
        DriverFailure with `unsent` True, no reconciliation."""
        s.status = "unsent"
        err = DriverFailure(f"{s.kind} not sent: {exc}"[:300], fatal=False)
        err.unsent = True
        return err

    def _message_check(self, s: Sent, *, any_thread: bool = False) -> Callable[[], bool]:
        assert self.t3 is not None

        def check() -> bool:
            found = self.t3.find_message(s.message_id, thread_id=None if any_thread else s.thread_id, timeout_s=_left_now())
            if found is None:
                return False
            s.thread_id = found["thread_id"]
            if found.get("run_id"):
                s.run_id = found["run_id"]
            return True

        return check

    def _dispatch_message(self, s: Sent, at: float, *, mode: str = "queue", target_run: str | None = None) -> None:
        assert self.t3 is not None
        self._send(s, at, lambda left: self.t3.dispatch_message(s.thread_id, s.text or "", mode=mode, target_run=target_run, ids=(s.command_id, s.message_id), timeout_s=left),
                   self._message_check(s), "the message")

    def _launch(self, s: Sent, at: float) -> dict[str, Any] | None:
        assert self.t3 is not None
        info = self._send(s, at, lambda left: self.t3.launch(s.text or "", model_selection=self.h.model_selection(self.model, self.options, self.provider), title=self.title,
                                                             ids=(s.command_id, s.message_id), timeout_s=left),
                          self._message_check(s, any_thread=True), "the opening message")
        if info is not None:
            s.thread_id = info["thread_id"]
        return info

    def start(self, opening_prompt: str, *, timeout_s: float = 120) -> Sent:
        """Launch the thread with the opening prompt. The episode clock starts
        at this dispatch. Returns once T3 accepted it. `timeout_s` covers the
        launch and the run lookup, not the environment's start (`open`)."""
        self._guard(opening_prompt)
        self.open()
        assert self.t3 is not None
        if self.thread_id is not None:
            raise EpisodeError("the episode already started")
        at = time.monotonic() + timeout_s
        self.clock.begin()
        s = self._new_sent("opening", "", text=opening_prompt)
        try:
            info = self._launch(s, at)
        except _Rejected as exc:
            raise EpisodeError(f"T3 rejected the launch: {exc}") from None
        self.thread_id = s.thread_id
        self.segment_threads[self.segment] = self.thread_id
        self._resolve_run(s, at, (info or {}).get("projection"))
        return s


    def _require(self, at: float, need: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """`need` is `handed_back` (the tree idle, nothing pending: reply) or
        `steerable` (correct; see `_steer_gate`). A tree with a thread that
        cannot be read raises DriverFailure (not fatal)."""
        self._require_live()
        tree, st = self._tree(at)
        if st["unreadable"] or not st["consistent"]:
            raise DriverFailure(f"the tree could not be read consistently: {st['unreadable'][:3] or st['sample_reason']}", fatal=False)
        if need == "handed_back":
            if st["requests"]:
                raise EpisodeError("a request to the user is pending; answer it first")
            if not st["idle"]:
                raise EpisodeError(f"the agent is still working: {turns.busy_summary(st)}")
        else:
            self._steer_gate(st)
        return tree, st


    def _steer_gate(self, st: dict[str, Any]) -> str:
        """For a correction, from one tree state, in this order: a request to
        the user pending anywhere means the hand-back has happened
        (TurnNotRunning), whatever the main run's status; a live main run is
        steered (returns its id); a main run that has ended while child work
        remains (a live, queued or background child, or a continuation T3
        reserved) raises MainRunEnded, retryable; an idle tree is a hand-back
        (TurnNotRunning)."""
        if st["unreadable"] or not st.get("consistent", True):
            # The tree's state is unknown (an unreadable thread, or a sample
            # that changed while it was read): it may hold a request. Nothing
            # was sent; defer, retryable.
            raise DriverFailure(f"the tree's state is unknown, so the correction is deferred: {st['unreadable'][:2] or st.get('sample_reason')}", fatal=False)
        if st["requests"]:
            raise TurnNotRunning("a request to the user is pending: the turn has handed control back")
        root_live = [r for t, r, _ in st["live"] if t == self.thread_id]
        if root_live:
            return root_live[0]
        if st["live"] or st["queued"] or st["continuations"] or st["background"]:
            raise MainRunEnded(f"the main run has ended while subagent work continues: {turns.busy_summary(st)}")
        raise TurnNotRunning("the turn has handed control back")



    def reply(self, text: str, *, timeout_s: float = 60) -> Sent:
        """The next user message, at a hand-back with nothing pending (the
        whole tree idle)."""
        self._guard(text)
        at = time.monotonic() + timeout_s
        self._require(at, "handed_back")
        assert self.thread_id is not None
        s = self._new_sent("reply", self.thread_id, text=text)
        try:
            self._dispatch_message(s, at)
        except _Rejected as exc:
            raise EpisodeError(f"T3 rejected the reply: {exc}") from None
        self._resolve_run(s, at)
        return s


    def answer(self, question: turns.Question, answer: str | dict[str, str] | list[str], *, timeout_s: float = 60) -> Sent:
        """Answer a pending question, in whichever thread asked it. A single
        string answers a one-question prompt; several questions take a dict by
        question id or a list in order. An answer matching an option is sent
        as that option's value. `Sent.request_id` is the question's turn item.

        Accepted only when T3's runtime request is `resolved` by this answer:
        the recorded answers are the submitted ones, or, where T3 records
        none (the diagnostic dialog's requests), T3 confirms this command id
        (it replays the receipt of a command it took). A request that ended
        `cancelled` or `expired`, or was resolved otherwise, is a rejection
        (EpisodeError, `Sent.status` rejected); still pending at the deadline
        is unknown (DriverFailure)."""
        if not isinstance(question, turns.Question):
            raise TypeError("answer takes the Question from TurnResult.question")
        self._require_live()
        assert self.t3 is not None
        at = time.monotonic() + timeout_s
        answers = turns.answers_for(question, answer)
        text = answer if isinstance(answer, str) else json.dumps(answers, ensure_ascii=False)
        self._guard(text, *answers.values())
        s = self._new_sent("answer", question.thread_id, text=text, with_message=False)
        s.request_id, s.rpc_request_id, s.run_id = question.item_id, question.request_id, question.raw.get("runId")
        s.submitted = dict(answers)
        reserve = _reserve(at)

        def send(limit_at: float) -> bool | None:
            """True: T3 accepted this command id (a resend replays the
            receipt); None: the answer was lost; RpcFailure propagates."""
            try:
                with docker_deadline(limit_at):
                    self.t3.respond(question.thread_id, question.request_id, answers, cmd=s.command_id, timeout_s=_left(limit_at))
                return True
            except (RpcFailure, RpcUnsent):
                raise
            except (T3Error, DockerError):
                return None

        try:
            command_ok = send(at - reserve)
        except RpcFailure as exc:
            s.status = "rejected"
            raise EpisodeError(f"T3 rejected the answer: {exc}") from None
        except RpcUnsent as exc:
            raise self._unsent(s, exc) from None
        last = "no read completed"
        while True:
            try:
                with docker_deadline(at):
                    proj = self._snapshot(at, question.thread_id)
                req = next((r for r in proj.get("runtimeRequests", []) if r["id"] == question.request_id), {})
                status = req.get("status")
                if status in ("cancelled", "expired"):
                    s.status = "rejected"
                    raise EpisodeError(f"the question's request ended {status}; the answer was not taken")
                if status == "resolved":
                    stored = req.get("answers")
                    if stored is not None and not turns.answers_match(stored, answers):
                        s.status = "rejected"
                        raise EpisodeError(f"the request was resolved with other answers than the ones sent: {str(stored)[:200]}")
                    if stored is None and not command_ok:
                        # T3 did not record the answers: ask by the same command
                        # id, whose receipt T3 replays if it took this answer.
                        try:
                            command_ok = send(at)
                        except RpcUnsent:
                            command_ok = None
                        except RpcFailure:
                            s.status = "rejected"
                            raise EpisodeError("the request was resolved, not by this answer") from None
                    if stored is not None or command_ok:
                        s.status, s.acked_utc = "accepted", utc_now()
                        break
                last = f"request {status}"
            except (T3Error, DockerError) as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
            if _left(at) <= 0.6:
                s.status = "unknown"
                raise self._fail(f"answer {s.command_id}: not known to be taken at the deadline ({last})")
            time.sleep(max(0.0, min(0.3, _left(at))))
        self.answers[question.request_id] = text
        self.answers[question.item_id] = text
        return s


    def answer_secret(self, secret: turns.SecretRequest, values: dict[str, str] | str, *, timeout_s: float = 60) -> Sent:
        """Answer a pending secret request, in whichever thread asked it: one
        value per field, or one secret when the request has no fields. The
        value is never recorded. A value holding the upstream key is refused
        before anything is sent. Accepted when the item went to `saved`;
        `declined`, `cancelled` or `dismissed` is a rejection."""
        if not isinstance(secret, turns.SecretRequest):
            raise TypeError("answer_secret takes the SecretRequest from TurnResult.secret")
        self._require_live()
        assert self.t3 is not None
        at = time.monotonic() + timeout_s
        if secret.fields:
            if isinstance(values, str):
                if len(secret.fields) != 1:
                    raise EpisodeError(f"the request has {len(secret.fields)} fields; give a value for each")
                values = {secret.fields[0]["name"]: values}
            missing = [f["name"] for f in secret.fields if f["name"] not in values]
            if missing:
                raise EpisodeError(f"secret fields without a value: {missing}")
            answer: dict[str, Any] = {"type": "save", "values": {f["name"]: values[f["name"]] for f in secret.fields}}
            given = list(answer["values"].values())
        else:
            if isinstance(values, dict):
                if len(values) != 1:
                    raise EpisodeError("the request takes one secret")
                values = next(iter(values.values()))
            if not values:
                raise EpisodeError("an empty secret")
            answer = {"type": "save", "secret": values}
            given = [values]
        if any(k.secret in str(v) for k in self._keys() for v in given):
            raise EpisodeError("refused: the value holds the upstream credential; nothing was sent")
        s = self._new_sent("secret", secret.thread_id, with_message=False)
        s.request_id, s.run_id = secret.item_id, secret.raw.get("runId")
        self._settle_secret(s, secret, answer, at, want="saved")
        return s


    def _settle_secret(self, s: Sent, secret: turns.SecretRequest, answer: dict[str, Any], at: float, *, want: str) -> None:
        """Send `secrets.answerRequest` once (it has no command id) and read
        the item until it leaves `pending`: `want` is acceptance, any other
        end a rejection, still pending at the deadline unknown."""
        assert self.t3 is not None
        reserve = _reserve(at)
        try:
            with docker_deadline(at - reserve):
                self.t3.answer_secret(secret.thread_id, secret.item_id, answer, timeout_s=_left(at - reserve))
        except RpcFailure as exc:
            s.status = "rejected"
            raise EpisodeError(f"T3 rejected the secret answer: {exc}") from None
        except RpcUnsent as exc:
            raise self._unsent(s, exc) from None
        except (T3Error, DockerError):
            pass
        last = "no read completed"
        while True:
            try:
                with docker_deadline(at):
                    proj = self._snapshot(at, secret.thread_id)
                status = next((i for i in proj.get("turnItems", []) if i["id"] == secret.item_id), {}).get("secretStatus")
                if status == want:
                    s.status, s.acked_utc = "accepted", utc_now()
                    return
                if status not in (None, "pending"):
                    s.status = "rejected"
                    raise EpisodeError(f"the secret request ended {status}, not {want}")
                last = f"item {status}"
            except (T3Error, DockerError) as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
            if _left(at) <= 0.6:
                s.status = "unknown"
                raise self._fail(f"secret answer for {secret.item_id}: not known to be taken at the deadline ({last})")
            time.sleep(max(0.0, min(0.3, _left(at))))


    def correct(self, text: str, *, timeout_s: float = 60) -> Sent:
        """A mid-turn correction into the main thread's live run. Waits
        (within `timeout_s`) for the provider session to report its turn
        capabilities, never guessing from an empty list, then: native
        steering into the running run; else interrupt and restart; else
        interrupt, wait for the run to end, and queue. The mechanism
        requested is in `Episode.corrections`; `Sent.mechanism` is the one
        observed once the new message's run is known (updated when that
        becomes known later), since SPEC section 3 requires restarted runs to
        be marked. `Sent.delivery` is set when delivery is already known as
        this returns.

        The tree's state decides, before dispatch, after every read while
        waiting for capabilities, and again after a rejection (`_steer_gate`):
        TurnNotRunning when a request is pending anywhere or the tree is idle;
        MainRunEnded (retryable: hold the correction for the continuation run
        or the hand-back, whichever comes first) when the main run has ended
        and only child work remains. Before anything is sent, a failed read
        is DriverFailure (not fatal). After the interrupt-then-queue path has
        interrupted the run, every failure is fatal."""
        self._guard(text)
        at = time.monotonic() + timeout_s
        tree, st = self._require(at, "steerable")
        assert self.t3 is not None and self.thread_id is not None
        prev = self._steer_gate(st)
        self.run_id = prev
        caps = turns.turn_capabilities(tree[self.thread_id])
        while not caps:
            if _left(at) <= 1.0:
                raise EpisodeError("the provider session reported no turn capabilities; no correction sent")
            time.sleep(max(0.0, min(0.5, _left(at))))
            tree, st = self._tree(at)
            prev = self._steer_gate(st)
            caps = turns.turn_capabilities(tree[self.thread_id])
        requested = turns.steering_mechanism(caps)
        s = self._new_sent("correction", self.thread_id, text=text, mechanism=requested)
        self.corrections.append({"message_id": s.message_id, "requested": requested, "observed": None, "capabilities": caps, "run_before": prev, "run_after": None, "state_before": "running"})
        if requested == "interrupt_then_queue":
            try:
                self._interrupt_and_wait(prev, at)
                self._dispatch_message(s, at)
            except _Rejected as exc:
                self.corrections[-1]["rejected"] = str(exc)[:300]
                raise self._fail(f"T3 rejected the correction after the driver's interrupt: {exc}") from None
            except EpisodeError as exc:
                if isinstance(exc, DriverFailure) and exc.fatal:
                    raise
                if getattr(exc, "unsent", False) and getattr(exc, "done", None) == []:
                    self.corrections.pop()  # nothing reached T3
                    s.status = "unsent"
                    raise
                if getattr(exc, "unsent", False):
                    # The correction never reached T3. `done` lists only what T3
                    # accepted: the interrupt when its receipt said so.
                    self.corrections[-1]["unsent"] = True
                    accepted = self._last_interrupt == "accepted"
                    err = DriverFailure(f"the correction was not sent (the interrupt: {self._last_interrupt}): {exc}", fatal=False)
                    err.unsent, err.done, err.not_sent = True, (["interrupt"] if accepted else []), ["correction"]
                    err.unknown = ["interrupt"] if self._last_interrupt == "unknown" else []
                    raise err from None
                raise self._fail(f"the correction failed after the driver's interrupt: {exc}") from None
        else:
            try:
                self._dispatch_message(s, at, mode="steer" if requested == "native_steer" else "restart", target_run=prev)
            except DriverFailure as exc:
                if getattr(exc, "unsent", False):
                    # Nothing reached T3: no correction happened.
                    self.corrections.pop()
                raise
            except _Rejected as exc:
                # Rejected: nothing changed. The tree's state says why.
                self.corrections[-1]["rejected"] = str(exc)[:300]
                _, st = self._tree(at)
                self._steer_gate(st)
                raise EpisodeError(f"T3 rejected the correction with the main run still live: {exc}") from None
        self._resolve_run(s, at)
        if s.run_id is None:
            s.mechanism = None  # observed once the run is known (`wait` or `correction_delivery`)
        try:
            self.correction_delivery(s, timeout_s=max(0.0, _left(at)))
        except EpisodeError:
            pass
        return s



    def _interrupt_and_wait(self, run_id: str, at: float, tid: str | None = None) -> str | None:
        """`run.interrupt` (queue not held) of one run and wait until it is
        terminal. A rejected or lost interrupt is reconciled by the run's
        status; not stopped by the deadline is EpisodeError."""
        assert self.t3 is not None and self.thread_id is not None
        tid = tid or self.thread_id
        self._last_interrupt = "unknown"
        try:
            with docker_deadline(at):
                self.t3.interrupt(tid, run_id, hold_queue=False, cmd=new_id(), timeout_s=_left(at))
            self._last_interrupt = "accepted"
        except RpcFailure:
            self._last_interrupt = "rejected"
        except RpcUnsent as exc:
            err = DriverFailure(f"the interrupt was not sent: {exc}", fatal=False)
            err.unsent, err.done = True, []
            raise err from None
        except (T3Error, DockerError):
            pass  # reconciled below: the run ends, or the deadline says unknown
        while True:
            try:
                with docker_deadline(at):
                    st = turns.classify(self._snapshot(at, tid), run_id)
                if st["kind"] == "finished":
                    return st["run_status"]
            except (T3Error, DockerError):
                pass
            if _left(at) <= 0.6:
                raise EpisodeError("the run did not stop within the time limit after the interrupt")
            time.sleep(max(0.0, min(0.3, _left(at))))



    def correction_delivery(self, sent: Sent, *, timeout_s: float = 20) -> Delivery:
        """When the harness took a correction (T3's `completedAt` on its
        message) and the watcher mark the driver took the moment it observed
        that (label, batch, requested and acknowledged times), as a
        Delivery; `delivered_utc` None when not delivered yet. Asks T3 again
        when not yet known; `wait` and `correct` also observe delivery and
        take the mark at once, so the mark does not wait for this call. Also
        resolves the correction's run and observed mechanism when they were
        not known. `Sent.delivery` holds the same once observed."""
        rec = next((c for c in self.corrections if c["message_id"] == sent.message_id), None)
        if rec is None:
            raise EpisodeError("not a correction this episode sent")
        at = time.monotonic() + timeout_s
        if (not rec.get("delivered_utc") or sent.run_id is None or not (rec.get("delivery") or {}).get("mark_taken")) and self.t3 is not None and sent.thread_id == self.thread_id:
            try:
                with docker_deadline(at):
                    proj = self._snapshot(at)
                self._current_run(proj)
                if sent.run_id is None:
                    # Resolved by its own message id, also when the episode's
                    # pending message has moved on.
                    msg = next((m for m in proj.get("messages", []) if m["id"] == sent.message_id), None)
                    run = (msg or {}).get("runId") or next((r["id"] for r in proj.get("runs", []) if r.get("userMessageId") == sent.message_id), None)
                    if run:
                        self._run_known(sent, run)
                self._mark_deliveries(proj, at)
            except (T3Error, DockerError):
                pass
        sent.delivered_utc = rec.get("delivered_utc")
        if sent.delivery is None:
            return Delivery(None)
        return sent.delivery

    def refuse_secret(self, secret: turns.SecretRequest, text: str, *, timeout_s: float = 60) -> Sent:
        """Decline a pending secret request and give the agent `text` as the
        user's words. T3's `secrets.answerRequest` with `{type: "decline"}`
        resolves the agent's `request_secret` call with `{status: "declined",
        note: "The user declined. Don't ask for it in chat or another way; say
        what's blocked and go on without it."}` (SECRET_REQUEST_UNSAVED_NOTE in
        T3's contracts), confirmed by the item going to `declined`. The
        decline carries no words of its own: `text` is the only user message.
        It goes to the main thread by whichever route applies at that moment:
        steered (or restarted, per the harness) into the main run while it
        runs, else queued as the next message; a steer that loses the race
        with the run's end, or a tree that cannot be read, is queued instead,
        so the words are never lost. Once the decline has been sent, every
        failure is fatal (DriverFailure): nothing after it is retryable.
        Recorded as kind `refusal` with the route in `mechanism` (the export
        gives it the user kind `reply`); never a correction, and it gets no
        `correction_delivered` event. No secret value is sent."""
        if not isinstance(secret, turns.SecretRequest):
            raise TypeError("refuse_secret takes the SecretRequest from TurnResult.secret")
        self._guard(text)
        self._require_live()
        assert self.t3 is not None and self.thread_id is not None
        at = time.monotonic() + timeout_s
        d = self._new_sent("secret", secret.thread_id, with_message=False)
        d.request_id, d.run_id, d.mechanism = secret.item_id, secret.raw.get("runId"), "decline"
        self._settle_secret(d, secret, {"type": "decline"}, at, want="declined")
        # From here the decline is in T3: no failure is retryable.
        try:
            # The words go to the main thread: the user talks to the main agent,
            # also when a subagent asked.
            # The route comes from a consistent sample. After the decline the
            # run resumes and the tree changes, so an inconsistent or failed
            # sample leads to another one while the call has time; the words
            # are queued only when a consistent sample shows the main run
            # ended, or when the time for sampling runs out. Which of the
            # two is recorded in `Sent.route_basis`.
            root_live: list[str] = []
            caps: dict[str, Any] = {}
            basis = "time ran out"
            while _left(at) > NODE_MARGIN_S + 2.0:
                try:
                    tree, st = self._tree(at - NODE_MARGIN_S - 1.0)
                except DriverFailure:
                    st = None
                if st is not None and st["consistent"]:
                    root_live = [r for t, r, _ in st["live"] if t == self.thread_id]
                    caps = turns.turn_capabilities(tree[self.thread_id])
                    if not root_live:
                        basis = "the main run had ended"
                        break
                    if caps:
                        basis = "the main run was live"
                        break
                time.sleep(max(0.0, min(0.3, _left(at) - NODE_MARGIN_S - 2.0)))
            if self._force_refusal_queue:  # test hook: the documented queued route
                root_live, basis = [], "forced (test hook)"
            mech = turns.steering_mechanism(caps) if root_live and caps else None
            s = self._new_sent("refusal", self.thread_id, text=text)
            s.request_id = secret.item_id
            s.route_basis = basis
            route = "queued"
            if mech in ("native_steer", "interrupt_restart"):
                try:
                    self._dispatch_message(s, at, mode="steer" if mech == "native_steer" else "restart", target_run=root_live[0])
                    route = mech
                except _Rejected:
                    # The run ended in between: queue the same words, new ids.
                    s = self._new_sent("refusal", self.thread_id, text=text)
                    s.request_id, s.route_basis = secret.item_id, "the steer was rejected: the run had ended"
            if route == "queued":
                try:
                    self._dispatch_message(s, at)
                except _Rejected as exc:
                    raise self._fail(f"T3 rejected the refusal's words as a queued message: {exc}") from None
        except DriverFailure as exc:
            if exc.fatal:
                raise
            if getattr(exc, "unsent", False):
                # The decline is in T3; the words never reached it. Say which.
                err = DriverFailure(f"the secret request was declined; the refusal's words were not sent: {exc}", fatal=False)
                err.unsent, err.done, err.not_sent = True, ["decline"], ["refusal words"]
                raise err from None
            raise self._fail(f"after the decline, the refusal's words could not be sent: {exc}") from None
        except EpisodeError as exc:
            raise self._fail(f"after the decline, the refusal's words could not be sent: {exc}") from None
        s.mechanism = route
        self._resolve_run(s, at)
        return s


    def _stop(self, op: str, tid: str, run_id: str | None, at: float) -> str:
        """One stop command: `run.interrupt` (queue not held) of a live run,
        `queued-run.cancel` of a queued one, `thread.stop` of a thread whose
        only work is provider background work. `accepted`, `rejected: ...`
        (T3 answered no; nothing changed) or `lost: ...` (unknown whether T3
        got it)."""
        assert self.t3 is not None
        try:
            with docker_deadline(at):
                if op == "run.interrupt":
                    self.t3.interrupt(tid, run_id or "", hold_queue=False, cmd=new_id(), timeout_s=_left(at))
                elif op == "queued-run.cancel":
                    self.t3.cancel_queued(tid, run_id or "", timeout_s=_left(at))
                else:
                    self.t3.stop_thread(tid, timeout_s=_left(at))
            return "accepted"
        except RpcFailure as exc:
            return f"rejected: {exc}"
        except RpcUnsent as exc:
            return f"unsent: {exc}"
        except (T3Error, DockerError) as exc:
            return f"lost: {type(exc).__name__}: {exc}"



    def interrupt(self, *, timeout_s: float = 60) -> dict[str, Any]:
        """Stop the whole tree. Each round reads the tree (`turns.busy_state`)
        and, for what is busy: `run.interrupt` (queue not held) of every live
        run in every thread, which also cancels the requests pending in it;
        `queued-run.cancel` of every queued run; `thread.stop` of a thread
        whose only work left is provider background work. A target still busy
        after RETRY_S is sent again (T3 can reject a queued run it then
        starts). Returns once the tree reads quiet (nothing live, queued,
        reserved, in the background or pending, every thread read) twice,
        SETTLE_S apart. The result lists every attempt with its outcome and
        what was `stopped`; the next `wait` reports the cycle as interrupted.

        On a quiet tree it sends nothing and returns normally (`stopped`
        empty). Before anything was sent, a failed read is DriverFailure (not
        fatal); after, the tree not quiet by the deadline is a fatal
        DriverFailure."""
        self._require_live()
        assert self.t3 is not None and self.thread_id is not None
        at = time.monotonic() + timeout_s
        t = utc_now()
        attempts: list[dict[str, Any]] = []
        changed = False  # a stop that reached T3, or may have
        tried: dict[tuple, float] = {}
        quiet_since: float | None = None
        st: dict[str, Any] | None = None
        last_tree: dict[str, Any] = {}
        while True:
            try:
                last_tree, st = self._tree(at)
            except DriverFailure as exc:
                if not changed:
                    exc.unsent = bool(attempts) and all(x["outcome"].startswith("unsent") for x in attempts)
                    raise
                st = None
                quiet_since = None  # a failed read breaks the quiet confirmation
            if st is not None:
                quiet = st["consistent"] and not (st["requests"] or st["live"] or st["queued"] or st["continuations"] or st["background"] or st["unreadable"]
                                                  or st.get("pending_message"))
                if quiet:
                    # Quiet is confirmed by a second read SETTLE_S later, also
                    # when nothing was sent: a continuation can start between
                    # the parent's read and its children's.
                    now = time.monotonic()
                    quiet_since = quiet_since or now
                    if now - quiet_since >= SETTLE_S:
                        break
                else:
                    quiet_since = None
                if not quiet and st["consistent"]:
                    # Stop targets come from a consistent sample only.
                    busy_threads = {x for x, _, _ in st["live"]} | {x for x, _ in st["queued"]}
                    bg = dict(st["background"])
                    targets = ([("queued-run.cancel", x, r) for x, r in st["queued"]] + [("run.interrupt", x, r) for x, r, _ in st["live"]]
                               + [("thread.stop", x, None) for x, _ in st["background"] if x not in busy_threads])
                    for op, x, r in targets:
                        if time.monotonic() - tried.get((op, x, r), -1e9) < RETRY_S:
                            continue
                        tried[(op, x, r)] = time.monotonic()
                        before = {"background": bg.get(x, 0)} if op == "thread.stop" else {"status": next((s_ for t_, r_, s_ in st["live"] if r_ == r), "queued")}
                        stop_started = utc_now()
                        outcome = self._stop(op, x, r, at)
                        attempts.append({"started_utc": stop_started, "op": op, "thread_id": x, "run_id": r, "utc": utc_now(), "outcome": outcome, "before": before})
                        changed = changed or not outcome.startswith(("unsent", "rejected"))
            if _left(at) <= 0.6:
                if not changed:
                    err = DriverFailure(f"the tree could not be read as quiet within {timeout_s} s; nothing reached T3", fatal=False)
                    err.unsent = bool(attempts) and all(a["outcome"].startswith("unsent") for a in attempts)
                    raise err
                raise self._fail(f"the tree did not stop within {timeout_s} s: {turns.busy_summary(st) if st else 'unreadable'}")
            time.sleep(min(0.3, max(0.0, _left(at))))
        # What stopped is read from the confirmed tree, not only from
        # receipts, by comparing each target before the attempt and now: a
        # run now interrupted or cancelled was stopped; a thread whose
        # background work is gone after a stop whose receipt was lost was
        # stopped.
        run_records = {r["id"]: r for p in last_tree.values() if isinstance(p, dict) for r in p.get("runs", [])}
        runs_now = {rid: r.get("status") for rid, r in run_records.items()}
        bg_now = dict(st["background"]) if st else {}
        for a in attempts:
            a["observed"] = {"status": runs_now.get(a["run_id"])} if a.get("run_id") else {"background": bg_now.get(a["thread_id"], 0)}

        def was_stopped(a: dict[str, Any]) -> bool:
            if a["outcome"] == "accepted":
                return True
            if a["op"] == "thread.stop":
                return a["outcome"].startswith("lost") and a["before"].get("background", 0) > 0 and a["observed"]["background"] == 0
            return a["observed"].get("status") in ("interrupted", "cancelled")

        stopped = [a for a in attempts if was_stopped(a)]
        if stopped:
            self._interrupted = True
            for a in stopped:
                # Bound cancellation by the recorded run end, never by the
                # time a quiet-tree poll happened to return. A background
                # stop without a run uses the stop receipt's instant.
                ended = run_records.get(a.get("run_id"), {}).get("completedAt") or a["utc"]
                if instant(ended) >= instant(a["started_utc"]):
                    self._usage_interrupts.append({"start": a["started_utc"], "end": ended})
        latest = st["latest"] or {}
        if latest.get("id") and self._pending_message is None:
            self.run_id = latest["id"]
        return {"sent_utc": t, "episode_s": round(self.elapsed(), 3), "stopped": stopped, "attempts": attempts,
                "interrupted": [dict(x) for x in dict.fromkeys((("thread_id", a["thread_id"]), ("run_id", a["run_id"])) for a in stopped)],
                "run_id": latest.get("id"), "run_status": latest.get("status")}


    # ----- waiting ----------------------------------------------------------------------------

    def wait(self, *, timeout_s: float = 600, poll_s: float = 1.0, finish_s: float = FINISH_S) -> TurnResult:
        """Block until the agent hands control back (D-029): a question or
        secret request is pending anywhere in the thread tree (of a run that
        has not ended), or the whole tree is idle (`turns.busy_state`): the
        main thread's latest run terminal, nothing live, queued, reserved by
        T3 for a continuation, or in the provider's background, every thread
        read; and that idle state confirmed. A main run that ends while a
        subagent works is not a hand-back. A run T3 starts with its own
        continuation prompt belongs to the same hand-back cycle.

        Idle confirmation lasts across calls: the first idle read stores the
        tree's signature (every run with its status, item and message
        counts) and the time; a later read, in this call or a later one,
        hands back once SETTLE_S has passed since then with the signature
        unchanged; any change, or any busy read, starts it again. So a caller
        polling in slices shorter than SETTLE_S still gets the hand-back.

        Every read may use the finish allowance (the deadline is the
        original `timeout_s` plus `finish_s`), so even a slice shorter than
        one read makes one; polling stops at `timeout_s`; collecting outputs
        and the turn-end mark are bounded by the same `timeout_s` +
        `finish_s`. A thread that cannot be read is its own state (neither
        idle nor busy); after UNREADABLE_LIMIT reads in a row with one, a
        fatal DriverFailure.

        `requests` lists every pending request in delivery order, with the
        one the caller must answer marked `actionable` (also in `question` or
        `secret`), each new one also given as an output. A send_to_user
        payload that could not be read is listed in `unresolved` and retried
        next time; the hand-back is not final while one is outstanding.
        After `interrupt` stopped something, the hand-back that follows is
        `interrupted`. `outputs` holds each user-channel output once across
        the episode, keyed by turn item id."""
        self._require_live()
        assert self.t3 is not None and self.thread_id is not None
        t0 = time.monotonic()
        at = t0 + timeout_s
        fin = at + finish_s
        tid = self.thread_id
        tree: dict[str, Any] = {}
        st: dict[str, Any] | None = None
        handed_back = False
        first = True
        while first or _left(at) > 0:
            first = False
            self.reads += 1
            try:
                seen_tree, seen = self._tree(fin)
            except DriverFailure as exc:
                seen_tree, seen = None, None
                self._unreadable_reads += 1
                self._unreadable_reason = str(exc)
                self._idle_seen = None  # a failed read breaks the idle confirmation
            if seen is not None:
                self.reads_ok += 1
                if seen["consistent"]:
                    # Corrections' deliveries, from a consistent sample only.
                    self._mark_deliveries(seen_tree[tid], fin)
                if not seen["consistent"]:
                    # Discarded whole: nothing given out or counted as given, no
                    # idle recorded, and the next poll starts over.
                    self._idle_seen = None
                    if seen["unreadable"]:
                        self._unreadable_reads += 1
                        self._unreadable_reason = str(seen["unreadable"][:2])
                else:
                    tree, st = seen_tree, seen
                    self._unreadable_reads = 0
                    if st["requests"]:
                        self._idle_seen = None
                        handed_back = True
                        break
                    if st["idle"]:
                        now = time.monotonic()
                        if self._idle_seen is None or self._idle_seen[0] != st["signature"]:
                            self._idle_seen = (st["signature"], now, utc_now())
                        elif now - self._idle_seen[1] >= SETTLE_S:
                            handed_back = True
                            break
                    else:
                        self._idle_seen = None
            if self._unreadable_reads >= UNREADABLE_LIMIT:
                raise self._fail(f"the thread tree stayed unreadable for {self._unreadable_reads} reads: {self._unreadable_reason}")
            left = _left(at)
            if left <= 0:
                break
            nap = poll_s
            if self._idle_seen is not None:
                nap = min(nap, max(0.05, self._idle_seen[1] + SETTLE_S - time.monotonic()))
            time.sleep(max(0.0, min(nap, left)))
        idle_utc = self._idle_seen[2] if self._idle_seen is not None else None
        st = st or {"requests": [], "latest": None, "root_runs": [], "live": [], "idle": False}
        proj = tree.get(tid) if isinstance(tree.get(tid), dict) else {}
        latest = st["latest"] or {}
        rid = latest.get("id") or self.run_id
        cycle = [r for r in st["root_runs"] if (r.get("ordinal") or 0) > self._cycle_after]
        res = TurnResult(state="time_limit", segment=self.segment, thread_id=tid, run_id=rid, run_status=latest.get("status"), waited_s=round(time.monotonic() - t0, 2), ended_utc=utc_now())
        res.runs = [{"run_id": r["id"], "started_by": turns.started_by(proj, r), "status": r.get("status"), "message_id": r.get("userMessageId")} for r in cycle]
        outputs: list[dict[str, Any]] = []
        held_from = None
        if tree:
            res.user_channel, res.unresolved, held_from = self._user_channel(tree, tid, fin)
            outputs += [{"kind": "message", "text": u["text"], "item_id": u["item_id"], "run_id": u.get("run_id") or rid, "thread_id": u["thread_id"], "delivered_utc": u.get("completed_at") or u.get("at"),
                         "raw": u.get("raw")} for u in res.user_channel]
            for r in cycle:
                fi = turns.final_item(proj, r["id"]) if r.get("status") == "completed" else None
                if fi is not None:
                    outputs.append({"kind": "final", "text": fi.get("text") or "", "item_id": fi["id"], "run_id": r["id"], "thread_id": tid, "delivered_utc": fi.get("completedAt") or fi.get("startedAt"),
                                    "raw": fi})
            if rid:
                res.errors = turns.run_errors(proj, rid)
        if res.unresolved:
            handed_back = False  # not final while a user-channel payload is unread
        requests = st["requests"]
        for n, (kind, thread, obj) in enumerate(requests):
            entry = {"kind": kind, "item_id": obj.item_id, "thread_id": thread, "request_id": getattr(obj, "request_id", None), "actionable": n == 0,
                     "new": obj.item_id not in self._reported}
            res.requests.append(entry)
            if kind == "question":
                outputs.append({"kind": "question", "text": obj.text, "item_id": obj.item_id, "run_id": obj.raw.get("runId"), "thread_id": thread, "delivered_utc": obj.raw.get("startedAt"),
                                "options": list(obj.options), "actionable": n == 0, "raw": obj.raw})
            else:
                text = "\n".join(x for x in [obj.label, obj.reason or "", *[f.get("label") or f.get("name") or "" for f in obj.fields]] if x)
                outputs.append({"kind": "secret", "text": text, "item_id": obj.item_id, "run_id": obj.raw.get("runId"), "thread_id": thread, "delivered_utc": obj.raw.get("startedAt"),
                                "actionable": n == 0, "raw": obj.raw})
        if handed_back and requests:
            kind, _, obj = requests[0]
            if kind == "question":
                res.state, res.question = "question", obj
            else:
                res.state, res.secret = "secret", obj
        elif handed_back:
            status = latest.get("status")
            if self._interrupted:
                res.state = "interrupted"
            elif status == "completed":
                fi = turns.final_item(proj, rid)
                res.state, res.final_text = "finished", (fi or {}).get("text")
                res.final_item_id = (fi or {}).get("id")
            elif status in ("interrupted", "cancelled"):
                res.state = "interrupted"
            else:
                res.refusal = self._refusal(tree, proj, latest, fin)
                res.state = "refused" if res.refusal["refused"] else "failed"
        if handed_back and rid:
            # The main thread's current run is the one that closed the hand-back
            # (a pending message cannot be outstanding here: it keeps the tree busy).
            self.run_id = rid
            if not requests:
                self._cycle_after = max([r.get("ordinal") or 0 for r in st["root_runs"]], default=self._cycle_after)
                self._interrupted = False
                self._idle_seen = None
        # Each output once per episode, strictly in delivery order. While a
        # payload is unresolved, nothing delivered at or after it is given
        # out (or counted as given): the transcript never skips an output.
        fresh = [o for o in outputs if o["item_id"] not in self._reported]
        fresh.sort(key=lambda o: (_ts(o.get("delivered_utc")), o["kind"] in ("question", "secret"), not o.get("actionable", True)))
        if held_from is not None:
            fresh = [o for o in fresh if _ts(o.get("delivered_utc")) < held_from]
        res.outputs = fresh
        self._reported.update(o["item_id"] for o in res.outputs)
        if res.state != "time_limit":
            # The hand-back's instant: the delivery of the output that closed
            # it (the actionable request, or the final message), else when
            # the driver first saw the idle state it reports.
            closing = (requests[0][2].raw.get("startedAt") if requests else None) or (res.final_item_id and next((o["delivered_utc"] for o in outputs if o["item_id"] == res.final_item_id), None))
            # A silent hand-back (no closing output) is the instant the agent
            # stopped: the latest completion among the tree's runs in the
            # consistent sample that showed it idle, from T3's own records,
            # so it does not depend on polling. `ended_utc` stays the report time.
            stopped = [r.get("completedAt") for p in tree.values() if isinstance(p, dict) for r in p.get("runs", []) if r.get("completedAt")]
            res.handback_utc = closing or (max(stopped, key=_ts) if stopped else None) or idle_utc or res.ended_utc
            res.mark = self._mark(f"turn-{len(self.turn_results) + 1}-{res.state}", fin, instant=res.handback_utc)
        self.turn_results.append({"state": res.state, "segment": res.segment, "run_id": rid, "run_status": res.run_status, "runs": res.runs, "waited_s": res.waited_s,
                                  "user_channel": len(res.user_channel), "outputs": [o["item_id"] for o in res.outputs], "unresolved": res.unresolved,
                                  "requests": [r["item_id"] for r in res.requests], "ended_utc": res.ended_utc, "handback_utc": res.handback_utc, "episode_s": round(self.elapsed(), 3),
                                  "mark_batch": (res.mark or {}).get("batch"), "mark_at": (res.mark or {}).get("at"),
                                  "mark_straddled": (res.mark or {}).get("straddled")})
        return res



    def _mark_deliveries(self, proj: dict[str, Any], at: float | None = None) -> None:
        """A correction is delivered when T3 records the harness took it: its
        user message item gets `completedAt`. The driver then asks the watcher
        for a mark, recording when it asked (`mark_requested_utc`) and when
        the watcher acknowledged (`mark_acked_utc`): the delivery-to-mark lag
        is measured to the request."""
        for c in self.corrections:
            if c.get("delivered_utc"):
                d0 = c.get("delivery") or {}
                # A delivery mark that failed is tried again, up to 3 times;
                # it still names the delivery's instant.
                if d0.get("mark_taken") or d0.get("mark_error") == "no watcher in this episode" or c.get("mark_tries", 0) >= 3:
                    continue
                item = {"completedAt": c["delivered_utc"]}
            else:
                item = next((i for i in proj.get("turnItems", []) if i["type"] == "user_message" and str(i["id"]).endswith(str(c["message_id"]))), None)
            if item and item.get("completedAt"):
                c["mark_tries"] = c.get("mark_tries", 0) + 1
                c["delivered_utc"] = item["completedAt"]
                label = f"correction-{c['message_id'][:8]}"
                ack = self._mark(label, at, instant=c["delivered_utc"])
                if ack is None:
                    d = Delivery(c["delivered_utc"], mark_error="no watcher in this episode")
                elif not ack.get("ok") or ack.get("accepted") is False:
                    d = Delivery(c["delivered_utc"], label, mark_requested_utc=ack.get("requested_utc"), mark_error=str(ack.get("error") or "the watcher did not accept the mark")[:300])
                else:
                    d = Delivery(c["delivered_utc"], label, ack.get("batch"), ack.get("requested_utc"), ack.get("acked_utc"), True,
                                 mark_at=ack.get("at") or c["delivered_utc"], mark_straddled=ack.get("straddled"), mark_covered_utc=ack.get("covered_utc"))
                c["delivery"] = asdict(d)
                c["mark_batch"], c["mark_requested_utc"], c["mark_acked_utc"] = d.mark_batch, d.mark_requested_utc, d.mark_acked_utc
                for s in self.sent:
                    if s.message_id == c["message_id"]:
                        s.delivered_utc, s.delivery = c["delivered_utc"], d

    def _user_channel(self, tree: dict[str, Any], tid: str, at: float) -> tuple[list[dict[str, Any]], list[str], Any]:
        """Completed send_to_user calls in the observed tree, exact tool
        names, not reported before, with the text T3 showed the user (the
        result's sentMessage, read within the deadline); the item ids whose
        payload could not be read (retried by the next wait); and the
        delivery time of the earliest of those, from which nothing may be
        given out yet (None when all were read)."""
        assert self.t3 is not None
        out, unresolved = [], []
        held_from = None
        for t, p in tree.items():
            if isinstance(p, Exception):
                continue
            for item in p.get("turnItems", []):
                if not turns.is_send_to_user(item, self.h.send_to_user_tools) or item["id"] in self._reported or item.get("status") != "completed":
                    continue
                # A usable payload already in the projection is used; the full
                # item is read only when the projection omits it.
                full, text = item, turns.sent_message(item)
                if text is None:
                    try:
                        with docker_deadline(at):
                            full = self.t3.turn_item(t, item["id"], timeout_s=_left(at)) or item
                        text = turns.sent_message(full)
                    except (T3Error, DockerError):
                        text = None
                if text is None:
                    n = self._unresolved_reads.get(item["id"], 0) + 1
                    self._unresolved_reads[item["id"]] = n
                    if n >= UNRESOLVED_LIMIT:
                        raise self._fail(f"the payload of send_to_user item {item['id']} in thread {t} stayed unreadable for {n} reads")
                    unresolved.append(item["id"])
                    t_item = _ts(item.get("completedAt") or item.get("startedAt"))
                    held_from = t_item if held_from is None else min(held_from, t_item)
                    continue
                self._unresolved_reads.pop(item["id"], None)
                out.append({"text": text, "item_id": item["id"], "thread_id": t, "run_id": item.get("runId"), "at": item.get("startedAt"), "completed_at": full.get("completedAt"), "subagent": t != tid,
                            "raw": full})
        out.sort(key=lambda u: _ts(u.get("completed_at") or u.get("at")))
        return out, unresolved, held_from


    def _refusal(self, tree: dict[str, Any], proj: dict[str, Any], run: dict[str, Any], at: float) -> dict[str, Any]:
        """Refusal evidence from the gateway log, copied out of the gateway
        container through Docker (the host may not read its files), scanned,
        unpacked in a private temporary directory, and read before `at`."""
        assert self.env is not None and self.env.site is not None
        import shutil
        import tempfile

        if _left(at) <= 0:
            return {"refused": False, "reason": "no time left to read the gateway log"}
        others = [r for t, p in tree.items() if not isinstance(p, Exception) for r in p.get("runs", []) if r["id"] != run["id"]]
        unreadable = [t for t, p in tree.items() if isinstance(p, Exception)]
        texts = [m["text"] for m in proj.get("messages", []) if m.get("runId") == run["id"] and m.get("role") == "user"]
        d = Path(tempfile.mkdtemp())
        try:
            with docker_deadline(at):
                got = self.env.site.copy_logs("gateway", d / "log", self._keys())
            return turns.refusal(got["dir"], run, others, texts, unreadable_threads=unreadable, deadline=at)
        except (DockerError, turns.ScanDeadline) as exc:
            return {"refused": False, "reason": f"the gateway log could not be read: {type(exc).__name__}: {exc}"[:300]}
        finally:
            shutil.rmtree(d, ignore_errors=True)


    # ----- what the runner reads -------------------------------------------------------------

    def mark(self, label: str, *, at: str | None = None, timeout_s: float | None = None, limit_s: float | None = None) -> dict[str, Any]:
        """A watcher mark naming the instant `at` (UTC; default the time of
        the request), D-037: it never closes a batch; the watcher answers once
        everything written up to `at` is committed, with `accepted`, `at`,
        `batch` (in effect at `at`), `straddled` and `covered_utc`, plus
        `requested_utc` and `acked_utc`. Raises if it is not accepted within
        `timeout_s` (default the watcher spec's); `limit_s` is the watcher's
        own limit for confirming coverage."""
        if self.env is None:
            raise EpisodeError("the environment is not started")
        ack = self.env.mark(label, at=at, timeout_s=timeout_s, limit_s=limit_s)
        self.marks.append({"label": label, "segment": self.segment, "by": "caller", **{k: ack.get(k) for k in ("ok", "accepted", "at", "batch", "straddled", "covered_utc", "requested_utc", "acked_utc", "seconds", "error")}})
        return ack

    def excluded(self) -> list[dict[str, Any]]:
        """The episode clock's left-out intervals: start_utc, end_utc, reason, seconds."""
        return [dict(e) for e in self.clock.excluded]

    def watcher_dirs(self) -> list[Path]:
        """The watcher's output directories, one per segment, in order; live
        while the episode runs."""
        if self.env is None:
            return []
        return [s.watcher_out for s in self.env.segments if s.watcher_out is not None]

    def describe(self, *, timeout_s: float = 30) -> dict[str, Any]:
        """Harness name and version, T3 version, driver version, model, mode.
        The versions are read from the running container, or from the last
        capture once it has stopped."""
        hv = t3v = None
        if self.env is not None and self.env.segments:
            c = self.env.harness
            try:
                with docker_deadline(time.monotonic() + timeout_s):
                    if c.running():
                        hv = c.exec(list(self.h.version_cmd), check=False).stdout.strip() or None
                        t3v = c.exec(["t3", "--version"], check=False).stdout.strip() or None
            except DockerError:
                pass
        if hv is None and self.captured:
            last = self.captured[max(self.captured)]
            hv, t3v = last.get("harness_version"), last.get("t3_version")
        from . import driver_version

        return {"harness": self.h.name, "harness_version": hv, "t3_version": t3v, "driver_version": driver_version(), "model": self.model, "mode": self.mode}

    # ----- runner events and waits ---------------------------------------------------------

    def record_user_wait(self, after: TurnResult | str | dict[str, str] | None, started_utc: str, ended_utc: str, *, charged_ms: int | None = None, ended: str = "reply",
                         cause: str = "verdict_reply", replies_to: Any = None) -> dict[str, Any]:
        """The simulated user's return delay, charged by the runner against a
        specific user-channel output: a TurnResult (its closing output's item,
        else its run), a turn item id, or a ref dict as the trajectory unit
        takes it. `cause` is one of the trajectory unit's wait causes
        (verdict_reply, fixed_message, reopen, reset, refusal); `replies_to`,
        when given, is passed to the trajectory unit in the event's fields."""
        if isinstance(after, TurnResult):
            ref = {"item": after.outputs[-1]["item_id"]} if after.outputs and after.outputs[-1].get("item_id") else {"run": after.run_id}
        elif isinstance(after, str):
            ref = {"item": after}
        else:
            ref = after
        fields: dict[str, Any] = {"charged_ms": charged_ms, "ended": ended, "cause": cause}
        if replies_to is not None:
            fields["replies_to"] = replies_to
        ev = {"type": "wait", "utc": started_utc, "end_utc": ended_utc, "ref": ref, "fields": fields}
        self.user_waits.append(ev)
        self.runner_events.append(ev)
        return ev

    def add_runner_event(self, type: str, utc: str | None = None, *, ref: dict[str, str] | None = None, end_utc: str | None = None, **fields: Any) -> dict[str, Any]:
        """Any runner event the trajectory unit takes (verdict, planted,
        reopen, budget_cutoff, episode_end, ...)."""
        ev = {"type": type, "utc": utc or utc_now(), "ref": ref, "fields": fields, "end_utc": end_utc}
        self.runner_events.append(ev)
        return ev

    # ----- reset (D-010) --------------------------------------------------------------------

    def idle_gate(self, *, timeout_s: float = 60) -> list[str]:
        """Problems that make this moment not a turn boundary (empty when idle):
        every thread in the tree readable and idle, a shell row for each
        (archived rows count) with a terminal or idle status and no activity
        or pending work, the current run present and terminal. A read that
        fails or runs out of time is a problem, never a pass."""
        assert self.t3 is not None and self.thread_id is not None
        at = time.monotonic() + timeout_s
        try:
            with docker_deadline(at):
                tree = read_tree(self.t3, self.thread_id, at, self._tree_cache)
        except (T3Error, DockerError) as exc:
            return [f"thread tree unreadable: {type(exc).__name__}"]
        try:
            with docker_deadline(at):
                shell: dict[str, Any] | Exception = turns.shell_rows(self.t3.shell(timeout_s=_left(at)))
        except (T3Error, DockerError) as exc:
            shell = exc
        root = tree.get(self.thread_id)
        problems = turns.idle_problems(tree, shell, self.thread_id, self._current_run(root if isinstance(root, dict) else None))
        if not getattr(tree, "consistent", True):
            problems.append(f"the tree sample was not consistent: {getattr(tree, 'reason', '')}")
        if self._pending_message is not None:
            problems.append(f"message {self._pending_message[-12:]} was accepted and has no run yet")
        return problems

    def reset(self, next_message: str, *, timeout_s: float = 900, dispatch_timeout_s: float | None = None) -> Sent:
        """The fresh-container handoff, at a turn boundary only: the idle gate,
        then the environment's handoff (T3 stopped, container stopped, final
        watcher mark, the segment's T3 record captured from the stopped
        container, container removed, watcher stopped, volume cloned, T3's
        refs removed from the clone, new watcher, new container), then the
        next user message in a new thread. Everything up to that message is
        left out of the episode clock, from the moment reset is called; the
        driver's controlled_reset event carries that moment. A rejected idle
        gate raises EpisodeError and changes nothing; a handoff that fails
        part way raises DriverFailure.

        Two limits: `timeout_s` covers the gate and the handoff (the
        infrastructure, excluded from the clock); `dispatch_timeout_s`, when
        given, covers the continuation's launch and run lookup, counted from
        when the handoff ended (the runner passes the episode time left).
        Without it the launch shares `timeout_s`. After the handoff every
        failure is fatal, except a launch that never reached T3 (`unsent`):
        then the handoff stands, the new segment has no dispatch yet
        (`awaiting_launch`, recorded in `Episode.handoffs`), and a non-fatal
        DriverFailure with `unsent` and `handoff_done` True is raised. The
        caller can end the episode cleanly (export) or call `reset` again,
        which then only launches the continuation."""
        self._guard(next_message)
        if self.awaiting_launch is not None:
            if self.failure is not None:
                raise DriverFailure(f"the episode already failed: {self.failure}")
            return self._launch_continuation(next_message, time.monotonic() + (dispatch_timeout_s if dispatch_timeout_s is not None else timeout_s), self._handoff_started)
        self._require_live()
        at = time.monotonic() + timeout_s
        assert self.env is not None
        with self.clock.exclude("handoff") as started:
            problems = self.idle_gate(timeout_s=min(60.0, max(0.0, _left(at))))
            if problems:
                raise EpisodeError(f"not at a turn boundary: {problems}")
            from .export import capture_stopped

            seg, tid = self.segment, self.thread_id
            versions = self.describe(timeout_s=min(30.0, max(0.0, _left(at))))

            def on_stopped(c: Container) -> None:
                self.captured[seg] = capture_stopped(self, c, seg, tid, versions)

            try:
                with docker_deadline(at):
                    rec = self.env.handoff(on_stopped=on_stopped)
            except HandoffFailed as exc:
                raise self._fail(f"the handoff stopped part way, at {exc.stage}: {type(exc.cause).__name__}: {exc.cause}"[:400]) from exc
            except Exception as exc:
                raise self._fail(f"the handoff stopped part way: {type(exc).__name__}: {exc}"[:400]) from exc
            self.handoffs.append({k: v for k, v in rec.items() if k != "git"} | {"git": {r: {k: g.get(k) for k in ("t3_refs_deleted", "unreachable_before", "skipped", "ok")} for r, g in rec["git"].items()}})
            self.segment += 1
            # The context reset is the handoff: recorded once it completed,
            # whether or not its continuation is then launched.
            self.runner_events.append({"type": "controlled_reset", "utc": started, "ref": None, "fields": {}, "end_utc": None, "by_driver": True,
                                       "continuation": "pending", "segment": self.segment})
        assert self.t3 is not None
        if dispatch_timeout_s is not None:
            at = time.monotonic() + dispatch_timeout_s
        # The new segment has no thread until its first dispatch.
        self.thread_id, self.run_id, self._pending_message = None, None, None
        self.awaiting_launch, self._handoff_started = next_message, started
        return self._launch_continuation(next_message, at, started)

    def _launch_continuation(self, next_message: str, at: float, started: str) -> Sent:
        """The first dispatch of a segment a handoff opened."""
        assert self.t3 is not None
        s = self._new_sent("reset", "", text=next_message)
        try:
            info = self._launch(s, at)
        except _Rejected as exc:
            raise self._fail(f"T3 rejected the reset's launch: {exc}") from None
        except DriverFailure as exc:
            if getattr(exc, "unsent", False):
                self.handoffs[-1]["continuation"] = "unsent"
                self._reset_event()["continuation"] = "unsent"
                err = DriverFailure(f"the handoff stands; the continuation was not sent: {exc}", fatal=False)
                err.unsent, err.handoff_done, err.done, err.not_sent = True, True, ["handoff"], ["continuation"]
                raise err from None
            if exc.fatal:
                raise
            raise self._fail(f"the reset's launch did not reach T3 after the handoff: {exc}") from None
        self.handoffs[-1]["continuation"] = "launched"
        ev = self._reset_event()
        ev["continuation"], ev["ref"] = "launched", {"message": s.message_id}
        self.awaiting_launch = None
        self.thread_id = s.thread_id
        self.segment_threads[self.segment] = self.thread_id
        # A new thread: a new hand-back cycle, nothing carried over from the
        # old one (an interrupt of the old tree included).
        self._cycle_after, self._interrupted, self._idle_seen, self._unreadable_reads = 0, False, None, 0
        self._resolve_run(s, at, (info or {}).get("projection"))
        return s

    def _reset_event(self) -> dict[str, Any]:
        return next(e for e in reversed(self.runner_events) if e["type"] == "controlled_reset" and e.get("by_driver"))

    # ----- probe (D-009) ---------------------------------------------------------------------

    def probe(self, question: str, *, scripted_reply: str | dict | None = None, timeout_s: float = 300) -> ProbeResult:
        """At a turn boundary only: a paused capture of the main container and
        volume, the copy on its own site, the question sent to the same thread
        in the copy, the answer, and the copy removed. The whole operation is
        left out of the episode clock from the moment probe is called (idle
        gate and session lookup included); the driver's probe event carries
        that moment.

        Every copy-side problem, cleanup included, is an outcome, never an
        exception. The one exception is DriverFailure when the main container
        could not be resumed after the capture. Cleanup that does not
        complete stays registered and close() retries it.

        Observable effect on the main container: it is paused for the
        capture (commit and volume clone) and its workspace files are read to
        hash them; the agent's processes are frozen meanwhile, nothing in its
        conversation changes.

        With the scripted upstream the copy's server is seeded with the main
        server's exported state, and `scripted_reply` (a text or one handler,
        which the runner takes from the task) answers the probe as the next
        script's handler 0; without it the probe is unavailable."""
        self._guard(question)
        self._require_live()
        t0 = time.monotonic()
        at = t0 + timeout_s
        with self.clock.exclude("probe") as taken:
            try:
                result = self._probe(question, scripted_reply, at, t0)
            except MainNotResumed as exc:
                cleanup = self._copy_cleanup()
                result = ProbeResult("failed", question, None, "the main container could not be resumed", {"reason_detail": str(exc), "cleanup": cleanup}, {},
                                     round(time.monotonic() - t0, 2), cleanup_complete=cleanup["complete"])
                self._probe_done(result, taken)
                raise self._fail(f"probe: {exc}") from exc
            except Exception as exc:  # a driver-side fault is still an outcome
                cleanup = self._copy_cleanup()
                result = ProbeResult("unavailable", question, None, "driver error", {"reason_detail": f"{type(exc).__name__}: {exc}", "cleanup": cleanup}, {},
                                     round(time.monotonic() - t0, 2), cleanup_complete=cleanup["complete"])
            return self._probe_done(result, taken)

    def _probe(self, question: str, scripted_reply: str | dict | None, at: float, t0: float) -> ProbeResult:
        def out(outcome: str, reason: str | None, detail: Any = None, continuity: dict | None = None, answer: str | None = None, sent: str | None = None) -> ProbeResult:
            return ProbeResult(outcome, question, answer, reason, detail, continuity or {}, round(time.monotonic() - t0, 2), sent)

        problems = self.idle_gate(timeout_s=min(60.0, max(0.0, _left(at))))
        if problems:
            return out("unavailable", "not at a turn boundary", problems)
        assert self.env is not None and self.thread_id is not None and self.t3 is not None
        main_run = self.run_id
        try:
            with docker_deadline(at):
                main_native = self._native_session(self._snapshot(at), main_run)
        except (T3Error, DockerError) as exc:
            return out("unavailable", "setup failed", f"the main thread could not be read: {exc}"[:300])
        up = self.upstream
        if up.kind == "scripted":
            if scripted_reply is None:
                return out("unavailable", "setup failed", "a probe on the scripted upstream needs scripted_reply")
            handler = {"steps": [], "final": scripted_reply} if isinstance(scripted_reply, str) else scripted_reply
            try:
                with docker_deadline(at):
                    up = Upstream.scripted(seed_state=self.env.site.export_state(), next_script={"version": 1, "description": "probe", "handlers": [handler]})
            except Exception as exc:
                return out("unavailable", "setup failed", f"{type(exc).__name__}: {exc}"[:400])
        copy = None
        try:
            with docker_deadline(at):
                copy = self.env.probe_copy(upstream=up)
        except MainNotResumed:
            raise
        except ProbeUnavailable as exc:
            r = out("unavailable", exc.reason, {"reason_detail": exc.detail, "cleanup": self._copy_cleanup()})
            r.cleanup_complete = r.detail["cleanup"]["complete"]
            return r
        except Exception as exc:
            r = out("unavailable", "setup failed", {"reason_detail": f"{type(exc).__name__}: {exc}"[:400], "cleanup": self._copy_cleanup()})
            r.cleanup_complete = r.detail["cleanup"]["complete"]
            return r
        result: ProbeResult
        try:
            with docker_deadline(at):
                ct3 = T3(copy.container)
                ct3.connect(timeout_s=min(120.0, _left(at)))
                sent_utc = utc_now()
                info = ct3.dispatch_message(self.thread_id, question, timeout_s=_left(at))
                run = ct3.run_for_message(self.thread_id, info["message_id"], timeout_s=_left(at))
                # The copy's whole tree, by the same definition of busy as the
                # main run's: done when idle, confirmed SETTLE_S later.
                proj: dict[str, Any] = {}
                bs: dict[str, Any] | None = None
                idle_seen: tuple[Any, float] | None = None
                done = False
                copy_cache: dict[str, Any] = {}
                while _left(at) > 2.0:
                    try:
                        ctree = read_tree(ct3, self.thread_id, at - 1.0, copy_cache)
                    except (T3Error, DockerError):
                        idle_seen = None  # a failed read breaks the idle confirmation
                        time.sleep(max(0.0, min(1.0, _left(at))))
                        continue
                    if not ctree.consistent:
                        idle_seen = None  # an inconsistent sample is discarded whole
                    elif isinstance(ctree.get(self.thread_id), dict):
                        proj = ctree[self.thread_id]
                        bs = turns.busy_state(ctree, self.thread_id)
                        if bs["requests"]:
                            break
                        if bs["idle"]:
                            if idle_seen is None or idle_seen[0] != bs["signature"]:
                                idle_seen = (bs["signature"], time.monotonic())
                            elif time.monotonic() - idle_seen[1] >= SETTLE_S:
                                done = True
                                break
                        else:
                            idle_seen = None
                    time.sleep(max(0.0, min(0.5 if idle_seen else 1.0, _left(at))))
                st = turns.classify(proj, run) if proj else {"kind": "running", "run_status": None}
            copy_native = self._native_session(proj, run) if proj else None
            continuity = {"main_session": main_native, "copy_session": copy_native, "same_session": bool(main_native) and main_native == copy_native}
            latest = (bs or {}).get("latest") or {}
            answer = turns.final_message(proj, latest.get("id")) if done and latest.get("status") == "completed" else None
            if bs is not None and bs["requests"]:
                result = out("failed", "the copy asked the user", [r[2].item_id for r in bs["requests"]], continuity, sent=sent_utc)
            elif not done:
                result = out("unavailable", "time limit", {"run_status": st["run_status"], "busy": turns.busy_summary(bs) if bs else None}, continuity, sent=sent_utc)
            elif latest.get("status") != "completed":
                result = out("failed", f"the copy's run is {latest.get('status')}", turns.run_errors(proj, latest.get("id") or run), continuity, sent=sent_utc)
            elif not continuity["same_session"]:
                result = out("unavailable", "the copy did not resume the session", continuity, continuity, sent=sent_utc)
            elif not (answer or "").strip():
                result = out("failed", "the copy's run completed without a final message", None, continuity, sent=sent_utc)
            else:
                result = out("answered", None, None, continuity, answer=answer, sent=sent_utc)
        except Exception as exc:
            result = out("unavailable", "the copy did not answer", f"{type(exc).__name__}: {exc}"[:400])
        # Cleanup runs outside the call's deadline, which the copy may have used up.
        try:
            with no_deadline():
                removal = copy.close()
            removed = all(r["confirmed_absent"] for r in removal) and not copy.left_over
        except KeyLeak as exc:
            result = out("failed", "the copy's cleanup found the key", str(exc)[:300], result.continuity, sent=result.sent_utc)
            removed = not copy.left_over
        except Exception as exc:
            copy.close_problems.append(f"{type(exc).__name__}: {exc}"[:300])
            removed = False
        result.seconds = round(time.monotonic() - t0, 2)
        # What could not be removed stays registered; close() retries it.
        cleanup = {"complete": removed and not copy.left_over, "removed": removed, "left_over": [list(x) for x in copy.left_over], "problems": copy.close_problems}
        result.cleanup_complete = cleanup["complete"]
        result.detail = {"capture": copy.capture, "cleanup": cleanup} if result.outcome == "answered" else {"reason_detail": result.detail, "cleanup": cleanup}
        return result

    def _copy_cleanup(self) -> dict[str, Any]:
        """The last probe copy's cleanup, after a failed setup."""
        copy = self.env.copies[-1] if self.env is not None and self.env.copies else None
        if copy is None:
            return {"complete": True, "left_over": []}
        return {"complete": not copy.left_over and not copy.key_leak, "left_over": [list(x) for x in copy.left_over], "problems": copy.close_problems, "key_leak": copy.key_leak}

    def _native_session(self, proj: dict[str, Any], run_id: str | None) -> str | None:
        ids = [a.get("nativeThreadId") for a in proj.get("attempts", []) if a.get("runId") == run_id and a.get("nativeThreadId")]
        return ids[-1] if ids else None

    def _probe_done(self, r: ProbeResult, taken_utc: str) -> ProbeResult:
        """`taken_utc` is when probe was called, which is also the start of
        its excluded interval (an event exactly at an interval's start is on
        the episode clock; one inside it is not)."""
        n = len(self.probes) + 1
        self.probes.append({"probe": n, "outcome": r.outcome, "question": r.question, "answer": r.answer, "reason": r.reason, "continuity": r.continuity, "seconds": r.seconds,
                            "sent_utc": r.sent_utc, "taken_utc": taken_utc, "cleanup_complete": r.cleanup_complete,
                            "cleanup": (r.detail or {}).get("cleanup") if isinstance(r.detail, dict) else None})
        self.runner_events.append({"type": "probe", "utc": taken_utc, "ref": None, "fields": {"probe": n}, "end_utc": None, "by_driver": True})
        r.taken_utc = taken_utc
        return r

    # ----- export --------------------------------------------------------------------------------

    def export(self, dest: Path | None = None, *, task: TaskInfo | None = None, end_reason: str = "finished", end_mark: str | None = None,
               user_kinds: dict[str, str] | None = None, answers: dict[str, str] | None = None, marks: list[str] | None = None,
               probe_targets: dict[int, dict[str, Any]] | None = None, end_utc: str | None = None, timeout_s: float = 900) -> dict[str, Any]:
        """Write the episode's raw record, its normalised trajectory and its
        scoring bundle (D-018). Quiesces first (T3 and the harness container
        stopped, the watcher's shutdown mark, the watcher stopped, the gateway
        and the scripted server stopped), then copies everything from the
        stopped containers and the finished logs, scans it, and publishes the
        directory only when every check passed; raises ExportFailed naming
        the stage otherwise. The episode ends here.

        `end_mark` is the runner's end mark (a label it requested with
        `mark`): the bundle's final filesystem state is the state at that
        mark. Without it, the shutdown mark ("export") is the end. The
        shutdown mark is kept in the raw manifest either way. `user_kinds` and
        `answers` from the caller win over the driver's own; `marks` lists the
        watcher marks the caller requested. The driver's episode_end event
        (when the caller added none) is dated at the one canonical end
        instant, by default when export was called; the shutdown mark names
        it too (straddled accepted only at an elapsed cutoff).
        Without a watcher there is no end mark and `end_mark` is refused. The
        result's `files` gives, per SPEC section 3 "Logging" item, the files
        that hold it. Every failure is ExportFailed with its stage.

        `end_utc` is the episode's scored end when the caller has one (D-035:
        an elapsed cutoff ends at its deadline instant), which may be earlier
        than this call and than the end mark. The driver's own episode_end is
        then dated there, and the episode clock is frozen there: `elapsed()`
        and the manifests give the time up to that instant, and the export's
        own interval and cleanup, which come after it, are on the wall clock
        only. Without it the end is the start of the export's interval."""
        from .export import export

        from .export import ExportFailed

        # One end instant: the caller's `end_utc`, else its own episode_end
        # event's, else the end mark's instant; each given one must agree
        # (compared as instants, not strings).
        caller_end = [e["utc"] for e in self.runner_events if e["type"] == "episode_end" and not e.get("by_driver")]
        mark_at = next((m.get("at") for m in reversed(self.marks) if end_mark is not None and m["label"] == end_mark and m.get("ok") is not False), None)
        given = [x for x in (end_utc, *caller_end, mark_at) if x]
        if given and any(_ts(x) != _ts(given[0]) for x in given):
            raise ExportFailed("artifacts", [f"the end instants disagree: end_utc {end_utc}, episode_end events {caller_end}, end mark {end_mark!r} at {mark_at}"])
        # An elapsed cutoff: the caller gave the end instant (the deadline)
        # and its end reason is budget_cutoff.
        reasons = [end_reason] + [(e.get("fields") or {}).get("reason") for e in self.runner_events if e["type"] == "episode_end" and not e.get("by_driver")]
        elapsed_cutoff = bool(given) and "budget_cutoff" in reasons
        end_utc = end_utc or (caller_end[0] if caller_end else None) or mark_at
        if end_utc is not None:
            if self.clock.start_utc is None or not (_ts(self.clock.start_utc) <= _ts(end_utc) <= _ts(utc_us())):
                raise EpisodeError(f"the end instant {end_utc} is not between the episode's start and now")
        at = time.monotonic() + timeout_s
        handles: list[dict[str, Any]] = []

        def open_exclusion() -> str:
            if not handles:
                handles.append(self.clock.open("export"))
            return handles[0]["start_utc"]

        # With the runner's end mark the export is excluded from the call on
        # and the episode ends at the call. Without it the episode ends after
        # the shutdown mark, so quiescing is on the clock and the interval
        # starts there.
        call_start = open_exclusion()
        # One canonical end instant: the one given, else the call.
        scored_end = end_utc or call_start
        try:
            with docker_deadline(at):
                out = export(self, Path(dest) if dest else self.out_dir / "export", task=task or TaskInfo(), end_reason=end_reason,
                             end_utc=scored_end, open_exclusion=open_exclusion, elapsed_cutoff=elapsed_cutoff, end_mark=end_mark, user_kinds=user_kinds or {},
                             answers=answers or {}, marks=marks, probe_targets=probe_targets)
        finally:
            for h in handles:
                self.clock.close(h)
            self.clock.freeze(scored_end)
        self.exports.append(Path(out["dest"]))
        return out


def _left_now(margin: float = 10.0) -> float:
    """A reconciliation read's own limit; the enclosing deadline caps it."""
    return margin
