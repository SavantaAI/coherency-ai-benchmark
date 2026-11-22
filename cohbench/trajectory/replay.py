"""Rebuild a saved episode's bundle from its raw record, and compare it with
the bundle stored beside it.

A bundle can check much on itself (`bundle.validate`), but not its
provenance: which output a hand-back closed, the runner's original probe
target, the metric values. Those are authenticated only by rebuilding the
bundle from the raw record the driver saved and comparing file digests:

    python -m cohbench.trajectory verify-episode RUN_DIR [--task TASK_JSON]

The raw record is a runner episode directory as the runner writes it:
`export/manifest.json` (the driver's export: segments, runner events, the
clock and its excluded intervals, the end mark, the opening dispatch),
`export/segment-N/` (the T3 backup and the harness sessions), the watcher's
output under `export/watcher/segment-N/`, `runner/events.jsonl` (what the
runner sent), `run.json` (its probe targets and its task) and
`episode-record.json`. The rebuild goes the way the driver's export does
(`cohbench/driver/export.py`). Runs recorded before waits named the outputs
they reply to are given `replies_to` from the wait's own target and cause;
runs recorded before D-035 (a single `cause`, charges measured from stamps)
are replayed with `Hints.legacy_waits`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from . import bundle
from .adapters import profile as get_profile
from .normalise import Hints, RunnerEvent, Segment, normalise
from .observations import TaskObservations
from .registry import TaskIdentifiers
from .t3 import from_sqlite

# The runner's send kinds -> user kinds (a refusal is a plain reply).
KINDS = {"opening_prompt": "opening_prompt", "reply": "reply", "correction": "correction", "reset": "reset", "reopen": "reopen", "post_run": "post_run", "refusal": "reply"}


def _lines(p: Path) -> list[dict[str, Any]]:
    # JSON Lines split on line feed only: U+2028, U+0085 and the like can sit
    # inside a logged string (the runner writes with ensure_ascii=False).
    return [json.loads(x) for x in p.read_text(encoding="utf-8").split("\n") if x.strip()]


def _object(path: Path, fields: dict[str, type]) -> dict[str, Any]:
    """A raw record file: a JSON object whose named fields have the given
    types, checked before anything uses it; otherwise a ReplayError."""
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{path}: not readable JSON ({type(exc).__name__}: {exc})"[:300]) from None
    if not isinstance(obj, dict):
        raise ReplayError(f"{path}: not a JSON object")
    bad = [k for k, t in fields.items() if not isinstance(obj.get(k), t)]
    if bad:
        raise ReplayError(f"{path}: {bad} missing or of the wrong type")
    return obj


def _live_excluded(m: dict[str, Any]) -> list[dict[str, Any]]:
    """The excluded intervals the live export passed: those closed when it
    ran. The export's own interval, which the driver opens at the episode's
    end and closes only after the export, is not among them, though the
    saved manifest records it closed; leaving it out reproduces the stored
    full trajectory's header (and its digest) exactly."""
    end = m.get("episode_end_utc") if isinstance(m.get("episode_end_utc"), str) else None
    out = []
    for e in m["clock"].get("excluded") or []:
        if not (isinstance(e, dict) and e.get("end_utc")):
            continue
        if e.get("reason") == "export" and end is not None and isinstance(e.get("start_utc"), str) and e["start_utc"] >= end:
            continue
        out.append({k: e[k] for k in ("start_utc", "end_utc", "reason")})
    return out


def task_of(run_dir: Path) -> Path | None:
    """The task file the run names (`run.json` `task.dir`), when it is there."""
    run = _object(Path(run_dir) / "run.json", {})
    task = run.get("task")
    tdir = task.get("dir") if isinstance(task, dict) else None
    p = Path(tdir) / "task.json" if isinstance(tdir, str) and tdir else None
    return p if p is not None and p.is_file() else None


class ReplayError(ValueError):
    """A raw record this command cannot rebuild from (a missing or malformed
    file, an unsupported harness)."""


def profile_of(manifest: dict[str, Any]) -> str:
    """The adapter profile the live export used: the harness description's
    (cohbench/driver/harness.py), for the harness the export names."""
    name = manifest.get("harness")
    if not isinstance(name, str):
        raise ReplayError("export/manifest.json names no harness")
    try:
        from cohbench.driver.harness import harness

        return harness(name).profile
    except ImportError:  # pragma: no cover
        pass
    except ValueError as exc:
        raise ReplayError(f"unsupported harness {name!r}: {exc}") from None
    return name


# The driver's send kinds the live export maps to user kinds (cohbench/driver/export.py KINDS).
DRIVER_KINDS = {"opening": "opening_prompt", "reply": "reply", "correction": "correction", "reset": "reset", "refusal": "reply"}


def hints_from_sends(log: list[dict[str, Any]], sent: list[dict[str, Any]] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """The user kinds and answers the live export passes, from the raw
    record, in the live export's order (M5 of round 11). First the driver's
    own record of what it sent (the export manifest's `sent`): every accepted
    message by its id, and every accepted answer's text by its question's
    item id (`request_id`) and T3's runtime request id (`rpc_request_id`).
    Then the runner's send log over it (`runner/events.jsonl`: `send` and
    `dispatched_after_deadline` records): every message by its id; an
    answer to a pending question by its request id and its question's item
    id, with its text; a refused secret request by its item id."""
    kinds: dict[str, str] = {}
    answers: dict[str, str] = {}
    for s in sent or []:
        if not isinstance(s, dict) or s.get("status") != "accepted":
            continue
        if s.get("kind") in DRIVER_KINDS and isinstance(s.get("message_id"), str) and s["message_id"]:
            kinds[s["message_id"]] = DRIVER_KINDS[s["kind"]]
        if s.get("kind") == "answer" and isinstance(s.get("text"), str):
            for k in (s.get("request_id"), s.get("rpc_request_id")):
                if isinstance(k, str) and k:
                    answers[k] = s["text"]
    for r in log:
        if r.get("type") not in ("send", "dispatched_after_deadline") or not isinstance(r.get("kind"), str):
            continue
        kind = KINDS.get(r["kind"], r["kind"])
        ref = r.get("ref") if isinstance(r.get("ref"), dict) else {}
        keys = [x for x in (r.get("message"), ref.get("message"), ref.get("item")) if isinstance(x, str) and x]
        if r.get("via") == "answer" and isinstance(r.get("request"), str) and r["request"]:
            keys.append(r["request"])
        for k in keys:
            kinds[k] = kind
        if r.get("via") == "answer" and isinstance(r.get("text"), str):
            for k in (r.get("request"), ref.get("item")):
                if isinstance(k, str) and k:
                    answers[k] = r["text"]
    return kinds, answers


def rebuild(run_dir: str | Path, task: str | Path | None = None, *, verdicts_at_as_of: bool = False, profile: str | None = None,
            workspace: str = "/work/project") -> tuple[Any, bundle.Bundle]:
    """The normalisation result and the bundle, from the raw record.
    `verdicts_at_as_of` dates each verdict at its classifier record's `as_of`
    (the output's item delivery), as the current runner stamps them."""
    run_dir = Path(run_dir)
    task = Path(task) if task is not None else task_of(run_dir)
    if task is None:
        raise bundle.BundleError(f"{run_dir}: no task file (run.json names none that exists); pass one")
    ex = run_dir / "export"
    as_of = {}
    if verdicts_at_as_of:
        for c in _lines(run_dir / "classifier.jsonl"):
            if "as_of" in c:  # the last line is the awareness record
                as_of[c["output"]["item"]] = c["as_of"]
    m = _object(ex / "manifest.json", {"segments": list, "runner_events": list, "clock": dict})
    try:
        prof = get_profile(profile or profile_of(m))
    except ValueError as exc:
        raise ReplayError(str(exc)) from None
    segments = []
    for s in m["segments"]:
        sdir = ex / f"segment-{s['segment']}"
        files = sorted(glob.glob(str(sdir / "sessions" / "**" / "*.jsonl"), recursive=True))
        segments.append(Segment(from_sqlite(sdir / "t3-state.sqlite"), s["thread_id"], [prof.read_session(f) for f in files]))
    log = _lines(run_dir / "runner" / "events.jsonl")
    if not all(isinstance(r, dict) for r in log):
        raise ReplayError("runner/events.jsonl: every line is a JSON object")
    if not all(isinstance(e, dict) for e in m["runner_events"]) or not all(isinstance(x, dict) for x in m["segments"]):
        raise ReplayError("export/manifest.json: runner_events and segments are lists of objects")
    kinds, answers = hints_from_sends(log, m.get("sent") if isinstance(m.get("sent"), list) else [])
    runner = []
    for e in m["runner_events"]:
        fields = dict(e.get("fields") or {})
        if e["type"] == "wait" and "replies_to" not in fields:
            # Recorded before waits named the outputs they reply to: a wait
            # for the policy's reply carried its target's, the others none.
            fields["replies_to"] = [e["ref"]] if fields.get("cause") == "verdict_reply" and e.get("ref") else []
        if e["type"] == "correction_delivered":
            # Recorded by the runner's round 5 only; D-037 replaced it with the
            # mark's instant and straddle, which the watcher's mark carries.
            fields.pop("mark_settled", None)
        utc = e["utc"]
        if e["type"] == "verdict" and isinstance(e.get("ref"), dict) and e["ref"].get("item") in as_of:
            utc = as_of[e["ref"]["item"]]
        runner.append(RunnerEvent(e["type"], utc, e.get("ref"), fields, e.get("end_utc")))
    legacy = any(e["type"] == "wait" and "cause" in (e.get("fields") or {}) for e in m["runner_events"])
    hints = Hints(workspace=workspace, user_kinds=kinds, answers=answers, runner=runner,
                  episode_start_utc=m["episode_start_utc"], legacy_waits=legacy,
                  # A record under the current runner contract is authoritative
                  # about hand-backs; an older one is inferred from its events.
                  handbacks_recorded=True if not legacy else None,
                  excluded=_live_excluded(m))
    res = normalise(segments, prof, hints)
    scoring = json.loads(task.read_text())["scoring"]
    run = _object(run_dir / "run.json", {})
    targets = {p["probe"]: {"about": p["about"], "ref": p["target"], "event": p["event"], "deferred": p["deferred"]} for p in run.get("probes", [])}
    watchers = [bundle.WatcherInput.from_dir(ex / "watcher" / f"segment-{s['segment']}") for s in m["segments"]]
    record = _object(run_dir / "episode-record.json", {})
    # Recorded before the runner carried its polling interval (D-040).
    record.setdefault("poll_s", None)
    # ... and before it carried the task instance's digest (D-046). The
    # bundle made then had no such key; the rebuild leaves it out again, as
    # the readback allows, so the stored bundle compares equal.
    undigested = "task_sha256" not in record
    record.setdefault("task_sha256", None)
    seed = (record.get("grouping") or {}).get("seed")
    if isinstance(seed, str) and seed[:1] == "s" and seed[1:].isdigit():
        # Recorded before the seed was a number: "s17" is seed 17.
        record["grouping"] = {**record["grouping"], "seed": int(seed[1:])}
    from .collection import Collected
    collected = Collected.read(m["collected"], ex / "collected" / "blobs") if "collected" in m else None
    legacy_source = not m.get("observation_sources", False)
    b = bundle.make(res, watchers, TaskIdentifiers.from_dict(scoring["identifiers"]), TaskObservations.from_dict(scoring["observations"]),
                    episode_record=record, probe_targets=targets, end_mark=m["end_mark"], collected=collected, legacy_source=legacy_source)
    if undigested:
        del b.episode["task_sha256"]
    return res, b


def digests(directory: str | Path) -> dict[str, str]:
    """Every file of a bundle directory, by relative path -> SHA-256."""
    d = Path(directory)
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def compare(a: str | Path, b: str | Path) -> list[str]:
    """The relative paths whose content differs between two bundle
    directories, or that only one holds. Empty when they are the same."""
    da, db = digests(a), digests(b)
    return sorted(p for p in set(da) | set(db) if da.get(p) != db.get(p))


def verify(run_dir: str | Path, task: str | Path | None = None, stored: str | Path | None = None) -> dict[str, Any]:
    """Rebuild the bundle and compare it with the stored one (by default
    `export/bundle`): `{"ok", "differs", "validation"}`. A stored bundle made
    by an earlier version of this unit differs wherever the format changed."""
    run_dir = Path(run_dir)
    stored = Path(stored) if stored is not None else run_dir / "export" / "bundle"
    if not stored.is_dir():
        raise ReplayError(f"{stored}: no stored bundle to compare with")
    import sqlite3

    try:
        _, b = rebuild(run_dir, task)
    except ReplayError:
        raise
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        # A missing or malformed raw record, or one the normaliser or the
        # bundle maker refuses: a command error, not a crash.
        raise ReplayError(f"{run_dir}: cannot rebuild from the raw record: {type(exc).__name__}: {exc}"[:600]) from None
    rep = bundle.validate(b)
    if not rep["ok"]:
        # Validation errors come back before anything is written.
        return {"ok": False, "differs": None, "validation": rep["errors"]}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bundle.write(b, Path(tmp) / "bundle")
            diff = compare(stored, Path(tmp) / "bundle")
    except (OSError, bundle.BundleError) as exc:
        raise ReplayError(f"{run_dir}: cannot write the rebuilt bundle: {type(exc).__name__}: {exc}"[:600]) from None
    return {"ok": not diff, "differs": diff, "validation": rep["errors"]}
