"""The scoring bundle: every file a scorer may read for one episode.

| File | Made by | Holds |
|---|---|---|
| `scorer.jsonl` | `view.scorer_view` from the trajectory | what the user saw and said, the main agent's turns, the runner's events |
| `identifiers.json` | `registry.scorer_identifiers` from the view and the change log | identifiers, when each became known, each disclosure while private |
| `changes.jsonl` | `changelog.build` from the watcher | task state by batch, marks, metric readings |
| `observations.jsonl` | `observations.build` from the trajectory and the change log | task-declared attempts with their state and result |
| `episode.json` | `make`, from the runner's episode record | grouping keys, budgets, planted events and reach, the answer key, controls, probes, the awareness outcome, the end |
| `blobs/<two hex>/<sha256>` | `write`, from the watcher's content store | exactly the contents named by the change log and collected records |

`make` is the one function the runner calls. `write` copies the referenced
blobs out of the watcher's store, checking each digest. `validate` checks each
file's schema, the references between them (verdict coverage and delivered
corrections' batches included), the content store, and runs the blindness
gate over every file. Nothing else is a scorer input: the full
trajectory, the audit registry and the run manifest stay outside.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import changelog, episode, io, observations
from .blindness import scan_doc, scan_view
from .changelog import DEFAULT_DRIVER_REF_NAMESPACES, DEFAULT_INSTRUCTION_FILES, ChangeLog
from .episode import EpisodeInput
from .format import EPISODE_FORMAT, VERSION
from .io import Trajectory
from .observations import Observations, TaskObservations
from .registry import TaskIdentifiers, scorer_identifiers, validate_identifiers
from .validate import validate_view
from .view import scorer_view

FILES = ("scorer.jsonl", "identifiers.json", "changes.jsonl", "observations.jsonl", "episode.json")

# Where each document holds content (counted tells) rather than structure.
# Task-declared labels (observation names, identifier kinds) are structure:
# a tell there fails.
CONTENT = {
    "identifiers.json": ("identifiers.*.text",),
    # Names the agent chose are content (M4 of round 11): paths, link
    # targets, the git head and ref names (the keys of `refs`), the paths a
    # reading's copy lacked. The strict D-026 gate on instruction files and
    # driver ref namespaces is separate (`_gate_changes`).
    "changes.jsonl": ("*.path", "*.from", "*.target", "*.head", "*.refs.*", "*.absent.*", "*.metrics.*"),
    "observations.jsonl": (),
    # Task-authored structure (planted schedules, the answer key, control
    # expectations) is not content: a tell there fails.
    "episode.json": ("probes.*.answer",),
}


class BundleError(ValueError):
    pass


class NoTarget(BundleError):
    """A probe target the agent's run gave nothing for (no attempt completed
    by the mark, a mark's batch with no changed path, no output before the
    copy). Probes are diagnostic and never exclude an episode (SPEC.md
    section 3): the record says `no_target`."""


@dataclass
class WatcherInput:
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    store: Path | None = None  # the watcher's blobs/ directory
    status: str | None = None  # heartbeat.json's status; None when there is none
    settle_ms: int = 100  # the watcher's settling window (config.json quiet_ms)
    # heartbeat.json's shutdown facts the manifest reports (HEARTBEAT_FACTS).
    # Never scored.
    heartbeat: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dir(cls, d: str | Path) -> "WatcherInput":
        d = Path(d)
        events, metrics, status = changelog.read_dir(d)
        settle = 100
        if (d / "config.json").exists():
            q = json.loads((d / "config.json").read_text(encoding="utf-8")).get("quiet_ms")
            settle = q if isinstance(q, int) and not isinstance(q, bool) else settle
        hb: dict[str, Any] = {}
        if (d / "heartbeat.json").exists():
            raw = json.loads((d / "heartbeat.json").read_text(encoding="utf-8"))
            hb = {k: raw[k] for k in HEARTBEAT_FACTS if isinstance(raw, dict) and k in raw}
        return cls(events, metrics, d / "blobs" if (d / "blobs").is_dir() else None, status, settle, hb)


HEARTBEAT_FACTS = ("final_settled", "late_events", "late_overflow", "late_capped", "still_changing", "metric_isolation", "event_log_broken")


def _watcher_notes(logs: list[WatcherInput]) -> list[str]:
    """What the watchers said at shutdown that qualifies the change log, for
    the manifest: an unsettled final batch, inotify events after the final
    batch closed (an overflow among them), readings not run isolated."""
    notes = []
    for n, w in enumerate(logs):
        hb, seg = w.heartbeat, f"watcher log {n + 1}"
        if hb.get("event_log_broken"):
            # The watcher then stops `failed`, so only an end mark before the
            # failure lets the log be read at all (changelog.check_status).
            notes.append(f"{seg}: the event log could not be cut back after a failed write ({str(hb['event_log_broken'])[:120]})")
        if hb.get("final_settled") is False:
            notes.append(f"{seg}: its final batch was committed unsettled")
        if hb.get("late_overflow") is True:
            notes.append(f"{seg}: the inotify queue overflowed after the final batch closed")
        if hb.get("late_capped") is True:
            notes.append(f"{seg}: the count of events after the final batch closed reached the watcher's cap")
        elif hb.get("still_changing") is True or _int_pos(hb.get("late_events")):
            notes.append(f"{seg}: {hb.get('late_events', 'some')} inotify events arrived after the final batch closed")
        if hb.get("metric_isolation") not in (None, "users"):
            notes.append(f"{seg}: metric readings ran with isolation {hb['metric_isolation']!r}, not as a separate user")
    return notes


BLOB_SCAN_CHUNK = 1 << 20  # the blob audit reads each text blob whole, this much at a time


def _blob_tells(changes: ChangeLog, stores: list[Path]) -> dict[str, Any]:
    """The lexical gate over the bundle's text blobs (file contents), read
    whole and incrementally (a code point split across reads is decoded
    once): tells found, by blob digest and tell, for the audit record. Never
    a failure: files the agent wrote are evidence. Binary blobs (not UTF-8,
    or with a NUL byte) are skipped."""
    import codecs

    from .blindness import scan_stream

    found: dict[str, dict[str, int]] = {}
    scanned = 0
    for sha in sorted({r["sha256"] for r in changes.records if r["type"] == "change" and r["content"] == "stored" and r["sha256"]}):
        src = next((s / sha[:2] / sha for s in stores if (s / sha[:2] / sha).is_file()), None)
        if src is None:
            continue
        dec = codecs.getincrementaldecoder("utf-8")()
        pieces: list[str] = []
        try:
            with open(src, "rb") as fh:
                while True:
                    data = fh.read(BLOB_SCAN_CHUNK)
                    if b"\0" in data:
                        raise UnicodeDecodeError("utf-8", data, 0, 1, "a NUL byte: binary")
                    pieces.append(dec.decode(data, final=not data))
                    if not data:
                        break
        except UnicodeDecodeError:
            continue
        scanned += 1
        hits = scan_stream(iter([x for x in pieces if x] or [""]))
        if hits:
            found[sha] = dict(sorted(hits.items()))
    return {"text_blobs_scanned": scanned, "tells": found}


def _int_pos(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


@dataclass
class Bundle:
    view: Trajectory
    identifiers: dict[str, Any]
    changes: ChangeLog
    observations: Observations
    episode: dict[str, Any] | None = None
    store: Path | list[Path] | None = None  # one store per watcher log; a blob is taken from whichever holds it
    own_store: bool = False  # the store is the bundle's own blobs/ (nothing else may be in it)
    notes: list[str] = field(default_factory=list, compare=False)  # for the run manifest and the report; never written
    directory: Path | None = field(default=None, compare=False)  # the directory a read bundle came from
    extra_blobs: dict[str, bytes] = field(default_factory=dict, compare=False, repr=False)


def _stores(store: Any) -> list[Path]:
    if store is None:
        return []
    return [Path(x) for x in (store if isinstance(store, (list, tuple)) else [store])]


def _blob(stores: list[Path], sha: str) -> Path | None:
    return next((d / sha[:2] / sha for d in stores if (d / sha[:2] / sha).is_file()), None)


# The runner's probe `about` names (cohbench/runner/task.py PROBE_ABOUT) -> the episode record's kinds.
RUNNER_ABOUT = {"output": "output", "message": "user_message", "change": "change", "attempt": "attempt"}
# How far the runner's own elapsed time may differ from the view's, which
# the episode record takes. Both count from the same instants (the opening
# prompt's dispatch, the episode end, the excluded intervals, D-020); the
# runner rounds its span to the nearest millisecond and the bundle rounds
# the same span down (clock.to_ms), so the two can differ by one rounding,
# at most 1 ms, and by nothing else.
ELAPSED_TOLERANCE_MS = 1
# The view's wait charges are the runner's own charges, so the sums agree
# exactly (unless the view cut a wait at the end, when no check is made).
USER_WAIT_TOLERANCE_MS = 0


def net_change(changes: ChangeLog, batch: int) -> bool:
    """D-025, as `ChangeLog.net_change`."""
    return changes.net_change(batch)


def _resolve_runner_target(pid: Any, tgt: Any, manifest: dict[str, Any], view: Trajectory, changes: ChangeLog,
                           obs: Observations, copy: dict[str, Any], probe_us: int) -> dict[str, Any]:
    """A probe target as the runner names it: {"about": output | message |
    change | attempt, "ref": {"item" | "message" | "run" | "mark": id}, ...}.
    Returns the record's `about`: {kind, ref, boundary}, the target's own
    boundary being its view seq (an output or a message) or the batch at
    which it was complete (a change, an attempt). A malformed target is a
    BundleError; one the run gave nothing for, an ineligible one, or one
    after the copy, is NoTarget.

    - output: by item, the user-channel output made from it; by run, that
      run's last output before the probe mark.
    - message: by message id, that user message; by item, the message made
      from that item, or the answer to the question that item (or request id)
      asked; by run, the run's last user message (or merged answer) before
      the probe mark.
    - change: by mark, the change-log batch the mark closed, eligible only
      with a net tree change (D-025), at or before the copy's batch.
    - attempt: by mark, the last attempt completed at or before the mark
      (and by the copy), on wall microseconds.
    """
    about = tgt.get("about") if isinstance(tgt, dict) else None
    ref = tgt.get("ref") if isinstance(tgt, dict) else None
    kind = RUNNER_ABOUT.get(about) if isinstance(about, str) else None
    if kind is None or not isinstance(ref, dict) or len(ref) != 1 or not isinstance(next(iter(ref.values())), str):
        raise BundleError(f"probe {pid}: target is {{about: one of {sorted(RUNNER_ABOUT)}, ref: {{item | message | run | mark: id}}}}")
    k, v = next(iter(ref.items()))
    if kind in ("output", "user_message"):
        table = {"item": manifest.get("events", {}), "message": manifest.get("event_messages", {}), "run": manifest.get("event_runs", {})}.get(k)
        if table is None:
            raise BundleError(f"probe {pid}: an {about} is named by item, message or run, not {k}")
        srcs = {int(s_) for s_, x in table.items() if x == v}
        if kind == "user_message" and k == "item":
            # A message delivered as the answer to a pending question has no
            # message id; the runner names it by the question's item (or request) id.
            srcs |= {int(s_) for s_, ids in manifest.get("event_answers", {}).items() if v in ids}
        want = "to_user" if kind == "output" else "user_message"
        cands = [n for n, src in sorted(view.sources.items()) if src in srcs and view.events[n]["type"] == want and n < copy["seq"]]
        if not cands:
            raise NoTarget(f"probe {pid}: {k} {v} names no {want} of the scorer view before the probe mark")
        return {"kind": kind, "ref": cands[-1], "boundary": cands[-1]}
    if k == "utc":
        # The tree in effect at an instant: the batch whose last write is the
        # latest at or before it. Inside a batch's write span it is ambiguous.
        try:
            at_us = _wall_us_from(v, manifest["episode_start_utc"])
        except (ValueError, TypeError) as exc:
            raise BundleError(f"probe {pid}: utc {v!r} is not an ISO 8601 instant with a zone ({exc})") from None
        if at_us > probe_us:
            raise NoTarget(f"probe {pid}: utc {v} is after the probe's copy")
        inside = [b for b, (s0, s1) in changes.spans.items() if changelog.straddles(s0, s1, at_us)]
        inside += [f"{x['batch']} (cut at the end)" for x in changes.outside_end
                   if x.get("first_write_us") is not None and changelog.straddles(x["first_write_us"], x["last_write_us"], at_us)]
        if inside:
            raise NoTarget(f"probe {pid}: utc {v} falls inside batch {inside[0]}'s writes; the tree in effect there is ambiguous")
        b = changes.batch_at_us(at_us) if len(changes.wall_us) == len(changes.records) else None
        if b is None:
            raise NoTarget(f"probe {pid}: no batch was in effect at utc {v}")
        m, mark_us, v = {"batch": b}, at_us, f"utc {v}"
    elif k != "mark":
        raise BundleError(f"probe {pid}: a {about} is named by a watcher mark or an instant (utc)")
    else:
        num = changes.mark_ids.get(v)
        at = next((n for n, r in enumerate(changes.records) if r["type"] == "mark" and r["mark"] == num), None) if num is not None else None
        if at is None:
            raise NoTarget(f"probe {pid}: mark {v} is not in the change log")
        m = changes.records[at]
        mark_us = changes.wall_us[at] if len(changes.wall_us) == len(changes.records) else None
        if mark_us is not None and mark_us > probe_us:
            # The same copy-time guard as an instant's: a mark after the copy names nothing.
            raise NoTarget(f"probe {pid}: mark {v}'s instant is after the probe's copy")
        if m.get("straddled") is not None or m["mark"] in changes.straddled_outside:
            # D-037: a batch's writes went on across the mark's instant; the
            # tree in effect there is ambiguous.
            raise NoTarget(f"probe {pid}: mark {v}'s instant falls inside batch {m['straddled']}'s writes; the tree in effect there is ambiguous")
    if kind == "change":
        if not net_change(changes, m["batch"]):
            raise NoTarget(f"probe {pid}: mark {v} closes batch {m['batch']}, which made no net change to the tree (D-025)")
        if copy["batch"] is None or m["batch"] > copy["batch"]:
            raise NoTarget(f"probe {pid}: mark {v}'s batch {m['batch']} is after the copy's")
        return {"kind": "change", "ref": m["batch"], "boundary": m["batch"]}
    done = []
    for o in obs.records:
        if o.get("source", "shell") != "shell":
            continue
        if o["t_end"] is None or o["result"]["status"] == "unfinished":
            continue
        end_us = obs.wall.get(o["seq"], (None, None))[1]
        if end_us is None or mark_us is None or end_us > mark_us or end_us > probe_us:
            continue
        done.append((end_us, o["seq"], o["batch_end"]))
    if not done:
        raise NoTarget(f"probe {pid}: no attempt completed by mark {v}")
    _, seq, b_end = max(done)
    return {"kind": "attempt", "ref": seq, "boundary": b_end}


def _wall_us_from(utc: str, origin: str) -> int:
    from datetime import datetime

    t, o = (datetime.fromisoformat(str(x).replace("Z", "+00:00")) for x in (utc, origin))
    if t.tzinfo is None:
        raise ValueError("no zone")
    d = t - o
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def _probe_record(pid: Any, tgt: Any, p: dict[str, Any], manifest: dict[str, Any], traj: Trajectory, view: Trajectory,
                  changes: ChangeLog, obs: Observations, attempt_patterns: bool = True) -> dict[str, Any]:
    """The probe record. Nothing about a probe fails the bundle except a
    malformed record: a target that cannot be resolved, is not eligible, or
    lies after the copy is recorded `no_target`."""
    if not isinstance(tgt, dict) or not isinstance(tgt.get("event"), str) or not (tgt.get("deferred") is None or isinstance(tgt.get("deferred"), bool)):
        raise BundleError(f"probe {pid}: a target names its scheduled event and says whether the copy was deferred (a boolean or null)")
    mark = next((x for x in view.events if x["type"] == "probe" and x.get("probe") == pid), None)
    mark_traj = next((x for x in traj.events if x["type"] == "probe" and x.get("probe") == pid), None)
    if mark is None or mark_traj is None:
        raise BundleError(f"probe {pid}: no probe mark in the scorer view")
    walled = len(changes.wall_us) == len(changes.records) and changes.records
    batch = changes.batch_at_us(mark_traj["us"]) if walled else None
    copy = {"seq": mark["seq"], "batch": batch}
    if tgt.get("about") == "attempt" and not attempt_patterns:
        raise BundleError(f"probe {pid}: about an attempt, and the task declares no attempt pattern")
    base = {"probe": pid, "event": tgt["event"], "boundary": copy, "deferred": tgt.get("deferred"), "question": p.get("question"), "expected": p.get("expected")}
    try:
        about = _resolve_runner_target(pid, tgt, manifest, view, changes, obs, copy, mark_traj["us"])
    except NoTarget as exc:
        # The agent's run gave nothing to ask about: recorded, never fatal.
        return {**base, "about": {"kind": RUNNER_ABOUT[tgt["about"]], "ref": None, "boundary": None}, "answer": None, "status": "no_target", "_why": str(exc)}
    return {**base, "about": about, "answer": p.get("answer"), "status": p.get("status")}


def _corrections(traj: Trajectory, view: Trajectory, changes: ChangeLog) -> list[str]:
    """Each delivered correction's mark, by its neutral number, and the batch
    that mark closed (D-025). A delivery whose mark is unavailable (the driver
    saw the delivery and got no mark, or the mark is not in the log) has
    `mark` and `batch` null and does not fail the bundle: the
    post-correction Steering share is then undefined. The view's deliveries
    are the trajectory's, in order. Returns notes for the manifest."""
    notes = []
    labels = [e.get("mark") for e in traj.events if e["type"] == "correction_delivered"]
    cds = [e for e in view.events if e["type"] == "correction_delivered"]
    for e, label in zip(cds, labels):
        num = changes.mark_ids.get(label) if label is not None else None
        m = next((r for r in changes.records if r["type"] == "mark" and r["mark"] == num), None) if num is not None else None
        if m is None:
            e["mark"] = e["batch"] = e["straddled"] = e["straddled_last"] = None
            why = "" if not label else (f" ({label!r} was not accepted by the watcher)" if any(x.get("label") == label for x in changes.rejected_marks)
                                        else f" ({label!r} is not in the change log)")
            notes.append(f"correction delivery at view seq {e['seq']}: no delivery mark{why}; "
                         "the post-correction Steering share is undefined for this episode (D-030)")
            continue
        # D-037: changes in batches up to `batch` are before the correction,
        # in later ones after it, except those of every batch from
        # `straddled` to `straddled_last`, which count on neither side.
        e["mark"], e["batch"], e["straddled"], e["straddled_last"] = num, m["batch"], m["straddled"], m["straddled_last"]
        if m["batch"] in changes.unsettled:
            notes.append(f"correction delivery at view seq {e['seq']}: its mark falls on unsettled batch {m['batch']}")
    return notes


def _notices(traj: Trajectory, changes: ChangeLog | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Each reset notice's delivery (D-046), for the run manifest: the
    notice's trajectory seq, the delivery mark's label, and the batch that
    mark closed with the run of batches straddling it, as a correction's
    would be. Never a bundle record and never a Steering input."""
    out, notes = [], []
    for e in traj.events:
        if e["type"] != "notice_delivered":
            continue
        label = e.get("mark")
        rec = {"seq": e["seq"], "target": e["target"], "mark": label, "batch": None, "straddled": None, "straddled_last": None}
        num = changes.mark_ids.get(label) if changes is not None and label is not None else None
        m = next((r for r in changes.records if r["type"] == "mark" and r["mark"] == num), None) if num is not None else None
        if m is None:
            if label is not None:
                rejected = changes is not None and any(x.get("label") == label for x in changes.rejected_marks)
                notes.append(f"reset notice delivery at trajectory seq {e['seq']}: mark {label!r} "
                             f"{'was not accepted by the watcher' if rejected else 'is not in the change log'}")
        else:
            rec.update(batch=m["batch"], straddled=m["straddled"], straddled_last=m["straddled_last"])
        out.append(rec)
    return out, notes


def make(result: Any, watcher: WatcherInput | list[WatcherInput] | None = None, identifiers_task: TaskIdentifiers | None = None, observations_task: TaskObservations | None = None,
         episode_input: EpisodeInput | None = None, *, episode_record: dict[str, Any] | None = None, collected=None, legacy_source=False, probe_targets: dict[Any, dict[str, Any]] | None = None,
         end_mark: str | None = None) -> Bundle:
    """The bundle for one episode, from the normaliser's result (trajectory and
    manifest), the watcher's output, the task's declarations and the runner's
    record (`episode_record` with `probe_targets`, or `EpisodeInput`).
    `end_mark` is the runner's end mark, the only mark that selects state."""
    traj, manifest = result.trajectory, result.manifest
    if isinstance(identifiers_task, dict):
        identifiers_task = TaskIdentifiers.from_dict(identifiers_task)
    if isinstance(observations_task, dict):
        observations_task = TaskObservations.from_dict(observations_task)
    logs = watcher if isinstance(watcher, list) else [watcher or WatcherInput()]
    view = scorer_view(traj)
    # Only a reply the view scores must have its text (an audit-only
    # trajectory may miss one in the post-run exchange or after the end).
    scored = set(view.sources.values())
    missing = [e["seq"] for e in traj.events if e["type"] == "answer" and e.get("missing") and e["seq"] in scored]
    if missing:
        raise BundleError(f"answers at trajectory seq {missing} are scored replies and missing (audit-only normalisation); no bundle")
    end = next((e for e in traj.events if e["type"] == "episode_end"), None)
    if episode_input is not None:
        probe_targets = {p.probe: p.about for p in episode_input.probes}
    # The marks a bundle record refers to: the end, each correction's
    # delivery, each probe's target. Every other mark is dropped.
    refs = {e["mark"] for e in traj.events if e["type"] == "correction_delivered" and isinstance(e.get("mark"), str)}
    refs |= {t["ref"]["mark"] for t in (probe_targets or {}).values() if isinstance(t, dict) and isinstance(t.get("ref"), dict) and isinstance(t["ref"].get("mark"), str)}
    # One watcher log per context segment (a controlled reset starts a new
    # watcher on the cloned volume), joined into one task-state sequence.
    # The change log ends at the episode end, and never later than the
    # post-run exchange's first event (named by reference in the header),
    # decided on wall microseconds.
    post = set(traj.header.get("post_run") or [])
    cuts = ([end["us"]] if end else []) + [e["us"] for e in traj.events if e["seq"] in post]
    end_us = min(cuts) if cuts else None
    # D-035: an elapsed cutoff ends at its deadline, a few seconds before the
    # runner stops the agent and takes its end mark. Its last tree is the one
    # in effect at the end instant, so the end mark (after it) selects nothing
    # and is audit only. Every other end keeps the end mark as the state.
    elapsed = end is not None and end.get("reason") == "budget_cutoff" and any(
        e["type"] == "budget_cutoff" and e.get("budget") == "time" for e in traj.events)
    selecting = None if elapsed else end_mark
    try:
        changelog.check_status([w.status for w in logs], end_mark if not elapsed else "the end instant")
        changes = changelog.join([(w.events, w.metrics) for w in logs], manifest["episode_start_utc"], excluded=traj.header.get("excluded", []),
                                 end_us=end_us, end_mark=selecting, keep_marks=refs, settle_ms=max([w.settle_ms for w in logs], default=100))
    except changelog.ChangeLogError as exc:
        raise BundleError(str(exc)) from None
    notes: list[str] = _corrections(traj, view, changes) + _watcher_notes(logs)
    # D-046: a reset notice's delivery mark is audit. Its batch position goes
    # to the run manifest, worked out on a second reading of the same logs
    # that keeps the notice marks too, so changes.jsonl (a Steering input)
    # holds no notice mark and its numbering is unchanged.
    notice_labels = {e["mark"] for e in traj.events if e["type"] == "notice_delivered" and isinstance(e.get("mark"), str)}
    with_notices = None
    if notice_labels:
        try:
            with_notices = changelog.join([(w.events, w.metrics) for w in logs], manifest["episode_start_utc"], excluded=traj.header.get("excluded", []),
                                          end_us=end_us, end_mark=selecting, keep_marks=refs | notice_labels, settle_ms=max([w.settle_ms for w in logs], default=100))
        except changelog.ChangeLogError as exc:
            raise BundleError(str(exc)) from None
    manifest["notices"], notice_notes = _notices(traj, with_notices)
    notes += notice_notes
    if elapsed:
        rejected = {r.get("label") for w in logs for r in w.events if r.get("kind") == "mark" and r.get("accepted") is False}
        held = {r.get("label") for w in logs for r in w.events if r.get("kind") == "mark"}
        state = "missing from the watcher log" if end_mark is None or end_mark not in held else "not accepted by the watcher" if end_mark in rejected else "after the end instant"
        manifest["end_mark_audit"] = {"label": end_mark, "state": state}
        notes.append(f"elapsed cutoff (D-035): the last tree is the one in effect at the end instant; the end mark {end_mark!r} is audit only ({state})")
    # The change log's audit detail: which reading each batch kept, and why a
    # batch has none (the bundle's `no_metric.reason` is the closed set).
    manifest["change_log_audit"] = {"readings": {str(b): v for b, v in sorted(changes.reading_sources.items())},
                                    "no_reading": {str(b): v for b, v in sorted(changes.no_reading.items())},
                                    "outside_end": changes.outside_end, "rejected_marks": changes.rejected_marks}
    manifest["blob_tells"] = _blob_tells(changes, [w.store for w in logs if w.store is not None])
    for x in changes.outside_end:
        notes.append(f"watcher batch {x['batch']} is outside the episode: its writes went on past the end"
                     f"{' (it straddles the end)' if x['straddles'] else ''}; the last tree is the batch before it")
    if selecting is not None and selecting in changes.mark_ids:
        m = next(r for r in changes.records if r["type"] == "mark" and r["mark"] == changes.mark_ids[end_mark])
        if m["batch"] in changes.unsettled:
            notes.append(f"the end mark falls on unsettled batch {m['batch']}")
    ids = scorer_identifiers(view, changes, identifiers_task, traj.header["workspace"])
    obs = observations.build(traj, changes, observations_task or TaskObservations(), collected=collected, origin=manifest.get("episode_start_utc"), legacy_source=legacy_source)
    doc = None
    if episode_input is not None:
        episode_record = {
            "format": EPISODE_FORMAT, "version": VERSION, "episode": episode_input.episode, "grouping": episode_input.grouping,
            "budget": {"limits": episode_input.limits, "consumed": {"elapsed_ms": view.header["duration_ms"], "user_wait_ms": episode_input.user_wait_ms}},
            "planted": episode_input.planted, "answer_key": episode_input.answer_key, "control": episode_input.control,
            "probes": [{"probe": p.probe, "question": p.question, "expected": p.expected, "answer": p.answer, "status": p.status} for p in episode_input.probes],
            "awareness": episode_input.awareness, "end": {"reason": end["reason"] if end else None, "duration_ms": view.header["duration_ms"]},
            "poll_s": episode_input.poll_s, "task_sha256": episode_input.task_sha256,
        }
    if episode_record is not None:
        # The runner's record (cohbench/runner/record.py): everything but the
        # probe references and the times, which the bundle finishes.
        if end is None:
            raise BundleError("an episode record needs the runner's episode_end")
        if traj.header.get("clock_origin") != "dispatch":
            # D-020: the runner counts from the opening prompt's dispatch; a
            # bundle on another origin would disagree with it by the delivery delay.
            raise BundleError("an episode record needs the episode clock's origin, the opening prompt's dispatch (Hints.episode_start_utc)")
        rec = dict(episode_record)
        if collected is not None:
            rec["collected"] = collected.records
        logs_by_attempt = {a.name: a.log for a in (observations_task or TaskObservations()).attempts if a.log}
        if logs_by_attempt:
            rec["attempt_logs"] = logs_by_attempt
        duration = view.header["duration_ms"]
        for where, got in (("end.duration_ms", (rec.get("end") or {}).get("duration_ms")),
                           ("budget.consumed.elapsed_ms", ((rec.get("budget") or {}).get("consumed") or {}).get("elapsed_ms"))):
            if isinstance(got, int) and abs(got - duration) > ELAPSED_TOLERANCE_MS:
                raise BundleError(f"{where}: the runner says {got} ms, the view {duration} ms (more than {ELAPSED_TOLERANCE_MS} ms apart)")
        rec["end"] = {**(rec.get("end") or {}), "duration_ms": duration}
        # The user-wait time is the view's charges, as elapsed is; the
        # runner's figure must agree unless the view cut a wait at the end.
        waits = [e for e in view.events if e["type"] == "wait"]
        if any(e.get("replies_to") is None for e in waits):
            raise BundleError("with an episode record, every wait names the outputs it replies to (replies_to)")
        if manifest.get("legacy_wait_cause") and traj.header.get("wait_charges") != "measured":
            raise BundleError("with an episode record, every wait carries `causes`, everything due at its hand-back; a single `cause` is accepted only without one")
        charged = sum(e["charged_ms"] for e in waits if isinstance(e.get("charged_ms"), int))
        cut = any(e["ended"] == "episode_end" or (e["ended"] == "budget_cutoff" and e["t_end"] == view.header["duration_ms"]) for e in waits)
        got = ((rec.get("budget") or {}).get("consumed") or {}).get("user_wait_ms")
        if isinstance(got, int) and not cut and abs(got - charged) > USER_WAIT_TOLERANCE_MS:
            raise BundleError(f"budget.consumed.user_wait_ms: the runner says {got} ms, the view's charges {charged} ms (more than {USER_WAIT_TOLERANCE_MS} ms apart)")
        budget = dict(rec.get("budget") or {})
        budget["consumed"] = {**(budget.get("consumed") or {}), "elapsed_ms": duration, "user_wait_ms": charged}
        rec["budget"] = budget
        aw = rec.get("awareness") or {}
        rec["awareness"] = {"asked": aw.get("asked"), "outcome": aw.get("outcome")}  # the reply text stays in the run manifest
        if "poll_s" not in rec:
            raise BundleError("an episode record carries the runner's polling interval (poll_s, D-040)")
        if "task_sha256" not in rec:
            raise BundleError("an episode record carries the task instance's digest (task_sha256, D-046)")
        rec["reach"] = episode.reach(rec.get("planted") or [], view)
        family = (rec.get("grouping") or {}).get("family")
        payload = (rec.get("answer_key") or {}).get("payload") or {}
        after = payload.get("answers_after") if isinstance(payload, dict) and isinstance(payload.get("answers_after"), str) else None
        rec["answers"] = episode.explanation_answers(view, after, rec.get("planted") or []) if family == "explanation" else None
        targets = {int(k): v for k, v in (probe_targets or {}).items()}
        probes = []
        for p in rec.get("probes", []):
            pid = p.get("probe") if isinstance(p, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid not in targets:
                raise BundleError(f"probe {pid!r:.20}: needs a target")
            probes.append(_probe_record(pid, targets[pid], p, manifest, traj, view, changes, obs, bool((observations_task or TaskObservations()).attempts)))
        why = [{"probe": x["probe"], "why": x.pop("_why")} for x in probes if "_why" in x]
        if why:
            # The reasons go to the run manifest (audit), never to the bundle.
            manifest.setdefault("probes_no_target", []).extend(why)
            notes += [f"{w['why']} (recorded as no_target)" for w in why]
        rec["probes"] = probes
        doc = rec
    if notes:
        manifest.setdefault("bundle_notes", []).extend(notes)
    return Bundle(view, ids, changes, obs, doc, [w.store for w in logs if w.store is not None] or None, False, notes, extra_blobs=collected.blobs if collected is not None else {})


def stored_hashes(changes: ChangeLog) -> set[str]:
    return {r["sha256"] for r in changes.records if r.get("type") == "change" and r.get("content") == "stored" and isinstance(r.get("sha256"), str)}


def blob_hashes(b):
    return stored_hashes(b.changes) | {r["sha256"] for r in (b.episode or {}).get("collected", {}).values() if r.get("status") == "collected"}


def dumps(b: Bundle) -> dict[str, str]:
    out = {
        "scorer.jsonl": b.view.dumps(),
        "identifiers.json": json.dumps(b.identifiers, sort_keys=True, indent=1, ensure_ascii=True) + "\n",
        "changes.jsonl": b.changes.dumps(),
        "observations.jsonl": b.observations.dumps(),
    }
    if b.episode is not None:
        out["episode.json"] = json.dumps(b.episode, sort_keys=True, indent=1, ensure_ascii=True) + "\n"
    return out


def write(b: Bundle, directory: str | Path) -> None:
    """Publish the bundle at `directory`: built in a fresh staging directory
    beside it (blobs verified first, then the files), then swapped into
    place, so no stale file (an old `episode.json`, a stray blob) survives and
    nothing is published when a blob is missing or wrong."""
    d = Path(directory)
    need = blob_hashes(b)
    if need - set(b.extra_blobs) and b.store is None:
        raise BundleError("the change log names stored contents, and no content store was given")
    stores = _stores(b.store)
    srcs = {}
    for sha in sorted(need):
        src = _blob(stores, sha)
        data = b.extra_blobs.get(sha) if sha in b.extra_blobs else src.read_bytes() if src is not None else None
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            raise BundleError(f"blob {sha} is missing from the store or does not match its digest")
        srcs[sha] = b.extra_blobs[sha] if sha in b.extra_blobs else src
    d.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{d.name}.staging-", dir=d.parent))
    try:
        for sha, src in srcs.items():
            dest = stage / "blobs" / sha[:2] / sha
            dest.parent.mkdir(parents=True, exist_ok=True)
            # A copy, not a hard link: a change to the bundle's file must never reach the watcher's store.
            if isinstance(src, bytes):
                dest.write_bytes(src)
            else:
                shutil.copyfile(src, dest)
            if hashlib.sha256(dest.read_bytes()).hexdigest() != sha:
                raise BundleError(f"blob {sha} changed while it was copied")
        if not need:
            (stage / "blobs").mkdir()
        for name, text in dumps(b).items():
            (stage / name).write_text(text, encoding="utf-8")
        old = None
        if d.exists():
            old = d.parent / f".{d.name}.old-{os.getpid()}"
            os.replace(d, old)
        try:
            os.replace(stage, d)
        except BaseException:
            if old is not None:
                os.replace(old, d)  # the earlier bundle back in place
                old = None
            raise
        finally:
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def read(directory: str | Path) -> Bundle:
    d = Path(directory)
    return Bundle(
        view=io.read(d / "scorer.jsonl"),
        identifiers=json.loads((d / "identifiers.json").read_text(encoding="utf-8")),
        changes=changelog.read(d / "changes.jsonl"),
        observations=observations.read(d / "observations.jsonl"),
        episode=episode.read(d / "episode.json") if (d / "episode.json").exists() else None,
        store=d / "blobs", own_store=True, directory=d,
    )


def _directory_errors(b: Bundle) -> list[str]:
    """A bundle directory holds the five files and `blobs/`, nothing else."""
    if b.directory is None:
        return []
    allowed = set(FILES) | {"blobs"}
    extra = sorted(p.name for p in Path(b.directory).iterdir() if p.name not in allowed or (p.name in FILES and not p.is_file()) or (p.name == "blobs" and not p.is_dir()))
    return [f"the bundle directory holds files outside the allowlist: {extra[:10]}"] if extra else []


def _store_errors(b: Bundle) -> list[str]:
    need = blob_hashes(b)
    errors = []
    stores = [d for d in _stores(b.store) if d.is_dir()]
    if not stores and not b.extra_blobs:
        return [f"{len(need)} stored contents and no content store"] if need else []
    for sha in sorted(need):
        p = _blob(stores, sha)
        data = b.extra_blobs.get(sha) if sha in b.extra_blobs else p.read_bytes() if p else None
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            errors.append(f"blob {sha} missing or not matching its digest")
    for name, r in (b.episode or {}).get("collected", {}).items():
        if r.get("status") == "collected":
            p = _blob(stores, r["sha256"])
            data = b.extra_blobs.get(r["sha256"]) if r["sha256"] in b.extra_blobs else p.read_bytes() if p else None
            if data is None or len(data) != r["bytes"]:
                errors.append(f"collected.{name}: blob byte count differs")
    if b.own_store:
        allowed = {f"{h[:2]}/{h}" for h in need}
        extra = sorted(p.relative_to(stores[0]).as_posix() for p in stores[0].rglob("*") if p.is_file() and p.relative_to(stores[0]).as_posix() not in allowed)
        if extra:
            errors.append(f"the bundle's store holds blobs no record names: {extra[:5]}")
    return errors


def _gate_changes(changes: ChangeLog, instruction_files: tuple[str, ...], namespaces: tuple[str, ...]) -> list[str]:
    """D-026 leaves instruction files and driver refs out at the source and
    D-028 gives marks neutral numbers; this checks both held."""
    errors = []

    def instr(v: Any) -> bool:
        return isinstance(v, str) and v.rsplit("/", 1)[-1] in instruction_files

    def driver_ref(v: Any) -> bool:
        if not isinstance(v, str):
            return False
        v = v[5:] if v.startswith("ref: ") else v
        return v.startswith(namespaces)

    for r in changes.records:
        if not isinstance(r, dict):
            continue
        for k in ("path", "from", "target"):
            if instr(r.get(k)):
                errors.append(f"blindness: instruction file {r[k]!r} in changes.jsonl")
        if r.get("type") == "metric" and isinstance(r.get("absent"), list) and any(instr(q) for q in r["absent"]):
            errors.append("blindness: an instruction file in a reading's absent paths")
        if r.get("type") == "git":
            refs = r.get("refs") if isinstance(r.get("refs"), dict) else {}
            if any(driver_ref(n) for n in refs) or driver_ref(r.get("head")):
                errors.append("blindness: a driver ref in changes.jsonl")
        if r.get("type") in ("mark", "metric") and r.get("mark") is not None and not (isinstance(r["mark"], int) and not isinstance(r["mark"], bool)):
            errors.append(f"blindness: a mark label {r['mark']!r:.40} in changes.jsonl, not a neutral number")
    return errors


def verdict_coverage(view: Trajectory) -> list[str]:
    """D-023: in a bundle with an episode record, every output on the user
    channel has exactly one verdict, or exactly one `unjudged` naming it (an
    output delivered so close to a cutoff that the runner recorded none)."""
    errors = []
    judged: dict[int, list[str]] = {}
    for e in view.events:
        if e.get("type") in ("verdict", "unjudged") and isinstance(e.get("target"), int) and not isinstance(e.get("target"), bool):
            judged.setdefault(e["target"], []).append(e["type"])
    for e in view.events:
        if e.get("type") == "to_user":
            got = judged.get(e["seq"], [])
            if len(got) != 1:
                errors.append(f"view event {e['seq']} (to_user): {len(got)} verdicts or unjudged records, exactly one is due")
    # An output goes unjudged only when a cutoff ended the episode: the
    # cutoff event and the end's reason both.
    end = next((e for e in view.events if isinstance(e, dict) and e.get("type") == "episode_end"), None)
    cutoff = any(isinstance(e, dict) and e.get("type") == "budget_cutoff" for e in view.events)
    if any(isinstance(e, dict) and e.get("type") == "unjudged" for e in view.events) and (end is None or end.get("reason") != "budget_cutoff" or not cutoff):
        errors.append("unjudged outputs need a budget cutoff at the end")
    return errors


MARK_TOLERANCE_MS = 2000  # a delivery mark is requested at the delivery and acknowledged within this


def _correction_errors(view: Trajectory, changes: ChangeLog) -> list[str]:
    """A delivered correction carries its delivery mark (a neutral number)
    and the batch that mark closed (D-025). The mark is tied to the
    delivery: at or after it, within MARK_TOLERANCE_MS, and no other
    delivery uses it. With a change log that has batches, every delivery
    has one."""
    errors = []
    marks = {r["mark"]: r for r in changes.records if isinstance(r, dict) and r.get("type") == "mark" and isinstance(r.get("mark"), int)}
    used: set[Any] = set()
    for e in view.events:
        if not isinstance(e, dict) or e.get("type") != "correction_delivered":
            continue
        mk, b = e.get("mark"), e.get("batch")
        if mk is None:
            if b is not None or e.get("straddled") is not None or e.get("straddled_last") is not None:
                errors.append(f"view event {e.get('seq')} (correction_delivered): a batch without a delivery mark")
            continue
        m = marks.get(mk) if isinstance(mk, int) and not isinstance(mk, bool) else None
        if m is None or m.get("batch") != b:
            errors.append(f"view event {e.get('seq')} (correction_delivered): mark {mk!r} at batch {b!r} is not in changes.jsonl")
        elif (m.get("straddled"), m.get("straddled_last")) != (e.get("straddled"), e.get("straddled_last")):
            errors.append(f"view event {e.get('seq')} (correction_delivered): straddled {e.get('straddled')!r} to {e.get('straddled_last')!r}, "
                          f"its mark says {m.get('straddled')!r} to {m.get('straddled_last')!r}")
        elif not (isinstance(e.get("t"), int) and isinstance(m.get("t"), int) and e["t"] <= m["t"] <= e["t"] + MARK_TOLERANCE_MS):
            errors.append(f"view event {e.get('seq')} (correction_delivered): mark {mk} at {m.get('t')} is not the delivery's (at {e.get('t')}, within {MARK_TOLERANCE_MS} ms)")
        elif mk in used:
            errors.append(f"view event {e.get('seq')} (correction_delivered): mark {mk} belongs to another delivery")
        used.add(mk)
    return errors


def _changes_by_the_end(changes: ChangeLog, view: Trajectory) -> list[str]:
    """Every batch, change and git record is dated at or before the episode
    end (a batch whose writes went on past it is outside the episode). The
    end mark may be acknowledged a little later and is not checked here."""
    d = view.header.get("duration_ms")
    if not isinstance(d, int) or not any(e.get("type") == "episode_end" for e in view.events if isinstance(e, dict)):
        return []
    return [f"change record {n} ({r.get('type')}): dated {r['t']}, after the episode end at {d}" for n, r in enumerate(changes.records)
            if isinstance(r, dict) and r.get("type") in ("batch", "change", "git") and isinstance(r.get("t"), int) and r["t"] > d]


def _record_waits(view: Trajectory) -> list[str]:
    """What `make` guarantees when there is a runner record: the clock starts
    at the opening prompt's dispatch (D-020), and each wait names the outputs
    it replies to (a list, possibly empty)."""
    e = [] if view.header.get("clock_origin") == "dispatch" else ["header: with an episode record, the clock's origin is the opening prompt's dispatch"]
    return e + [f"view event {x.get('seq')} (wait): with an episode record, replies_to is a list" for x in view.events
                if x.get("type") == "wait" and not isinstance(x.get("replies_to"), list)]


def _attempts_by_the_end(obs: Observations, view: Trajectory) -> list[str]:
    """No attempt starts or completes after the episode end (an attempt
    still running then is `unfinished`, with no end)."""
    d = view.header.get("duration_ms")
    if not isinstance(d, int) or not any(e.get("type") == "episode_end" for e in view.events):
        return []
    late = [f"observation {r.get('seq')}: completes at {r['t_end']}, after the episode end at {d}" for r in obs.records
            if isinstance(r, dict) and isinstance(r.get("t_end"), int) and r["t_end"] > d]
    # An attempt started after the end is not in the bundle at all.
    return late + [f"observation {r.get('seq')}: starts at {r['t']}, after the episode end at {d}" for r in obs.records
                   if isinstance(r, dict) and isinstance(r.get("t"), int) and r["t"] > d]


def validate(b: Bundle, require_episode: bool = True, instruction_files: tuple[str, ...] = DEFAULT_INSTRUCTION_FILES,
             driver_ref_namespaces: tuple[str, ...] = DEFAULT_DRIVER_REF_NAMESPACES) -> dict[str, Any]:
    errors: dict[str, list[str]] = {}

    def guarded(name: str, check: Any) -> list[str]:
        """A check that reports, never raises (malformed rows a validator
        did not anticipate are still errors, not crashes)."""
        try:
            return list(check())
        except Exception as exc:  # noqa: BLE001
            return [f"{name}: malformed beyond validation ({type(exc).__name__}: {exc})"[:300]]

    # Each file on its own first; the checks between files run only when
    # every file they read passed its own.
    errors["scorer.jsonl"] = guarded("scorer.jsonl", lambda: validate_view(b.view).errors)
    errors["identifiers.json"] = guarded("identifiers.json", lambda: validate_identifiers(b.identifiers, None))
    errors["changes.jsonl"] = guarded("changes.jsonl", lambda: changelog.validate(b.changes) + _gate_changes(b.changes, tuple(instruction_files), tuple(driver_ref_namespaces)))
    errors["observations.jsonl"] = guarded("observations.jsonl", lambda: observations.validate(b.observations, None))
    errors["blobs"] = guarded("blobs", lambda: _store_errors(b) + _directory_errors(b))
    if b.episode is None:
        errors["episode.json"] = ["missing"] if require_episode else []
    else:
        errors["episode.json"] = guarded("episode.json", lambda: episode.validate(b.episode))
    view_ok, changes_ok = not errors["scorer.jsonl"], not errors["changes.jsonl"]
    if view_ok and not errors["identifiers.json"]:
        errors["identifiers.json"] += guarded("identifiers.json", lambda: validate_identifiers(b.identifiers, b.view))
    if changes_ok and not errors["observations.jsonl"]:
        errors["observations.jsonl"] += guarded("observations.jsonl", lambda: observations.validate(b.observations, b.changes))
    if b.episode is not None and not errors["episode.json"] and view_ok and changes_ok and not errors["observations.jsonl"]:
        errors["episode.json"] += guarded("episode.json", lambda: episode.validate(b.episode, b.view, b.changes, b.observations))
    if b.episode is not None and view_ok:
        errors["scorer.jsonl"] += guarded("scorer.jsonl", lambda: verdict_coverage(b.view) + _record_waits(b.view))
    if view_ok and not errors["observations.jsonl"]:
        errors["observations.jsonl"] += guarded("observations.jsonl", lambda: _attempts_by_the_end(b.observations, b.view))
    if view_ok and changes_ok:
        errors["scorer.jsonl"] += guarded("scorer.jsonl", lambda: _correction_errors(b.view, b.changes))
        errors["changes.jsonl"] += guarded("changes.jsonl", lambda: _changes_by_the_end(b.changes, b.view))
    scans = {
        "scorer.jsonl": lambda: scan_view(b.view),
        "identifiers.json": lambda: scan_doc(b.identifiers, CONTENT["identifiers.json"]),
        "changes.jsonl": lambda: scan_doc({"header": b.changes.header, "records": b.changes.records}, tuple("records." + p for p in CONTENT["changes.jsonl"])),
        "observations.jsonl": lambda: scan_doc({"header": b.observations.header, "records": b.observations.records}, ()),
    }
    if b.episode is not None:
        scans["episode.json"] = lambda: scan_doc(b.episode, CONTENT["episode.json"])
    blind = {}
    for name, scan in scans.items():
        # Shape errors are reported, never raised: a file malformed past what
        # the scan reads has already failed its own validator.
        try:
            blind[name] = scan()
        except Exception as exc:  # noqa: BLE001
            errors.setdefault(name, []).append(f"blindness: malformed beyond scanning ({type(exc).__name__}: {exc})"[:300])
    for name, rep in blind.items():
        for h in rep.structural:
            errors.setdefault(name, []).append(f"blindness: structural tell {h.tell!r} at {h.field}: {h.sample!r}")
        for h in rep.decorations:
            errors.setdefault(name, []).append(f"blindness: decoration {h.tell!r} at {h.field}")
    ok = all(not v for v in errors.values())
    return {"ok": ok, "errors": {k: v[:50] for k, v in errors.items() if v}, "content_tells": {k: dict(v.content) for k, v in blind.items() if v.content}}
