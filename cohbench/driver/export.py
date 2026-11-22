"""The episode's raw record, its normalised trajectory and its scoring bundle.

    <dest>/
      manifest.json                 ties everything below together
      segment-<n>/t3-state.sqlite   T3's database, copied from the stopped container (WAL folded in)
      segment-<n>/threads.json      every thread of that database (parent and subagents), as JSON
      segment-<n>/sessions/         the harness's own session files
      segment-<n>/server-log.txt    the T3 server's output
      gateway/                      the main gateway's request log and stored bodies
      scripted/                     the scripted server's log and state (scripted upstream)
      watcher/segment-<n>/          the watcher's output directory, one per segment
      environment.json              names, checks, timings; rewritten at close with the removal results
      trajectory/trajectory.jsonl   cohbench/trajectory's full trajectory (audit record)
      trajectory/manifest.json      its run manifest
      bundle/                       the scoring bundle (D-018): the scorer's whole input

Quiesce first, then copy: T3 and the harness container are stopped, the
watcher takes its shutdown mark and is stopped, the gateway and the
scripted server are stopped; only then is anything copied, from the stopped
containers and the finished host logs, so every artifact is from the same
moment. A segment that ended in a handoff was captured the same way from
its stopped container before it was removed. A source that cannot be read
fails the export with its stage. Every file is scanned for the key (each
key in play) in memory before it is written, into a staging directory
beside `dest`, which becomes `dest` only when every artifact is present,
the watcher shut down cleanly, the full trajectory validates, the bundle
validates, and the blindness gate passes. Otherwise it is removed and
`ExportFailed` names the stage.
"""

from __future__ import annotations

import glob
import io
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cohbench.env.docker import Container, copy_bytes, extract, image_info, key_hits, write_scanned
from cohbench.env.environment import utc_now

from .harness import DRIVER_REF_NAMESPACES, instruction_files
from .t3 import T3_BASE_DIR

if TYPE_CHECKING:
    from .episode import Episode, TaskInfo

DB = f"{T3_BASE_DIR}/userdata/statev2.sqlite"
SHUTDOWN_MARK = "export"


class ExportFailed(RuntimeError):
    """The export is unusable. `stage` is where it failed (quiesce, capture,
    artifacts, watcher, trajectory, bundle, blindness) and `reasons` lists why."""

    def __init__(self, stage: str, reasons: list[str], report: dict[str, Any] | None = None):
        super().__init__(f"export unusable ({stage}): " + "; ".join(reasons[:10]))
        self.stage = stage
        self.reasons = reasons
        self.report = report or {}


def _one_file(tar_bytes: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        member = next(m for m in t.getmembers() if m.isfile())
        return t.extractfile(member).read()


def _scan(keys: list, name: str, *blobs: bytes) -> None:
    for blob in blobs:
        for k in keys:
            if key_hits(blob, k):
                raise RuntimeError(f"the {name} held the key; nothing kept")


def _database(c: Container, keys: list) -> bytes:
    """T3's database from the stopped container: the main file and its WAL
    (when there is one) copied into memory, scanned, then folded into one
    file through SQLite's backup in a private temporary directory."""
    main_tar, err = copy_bytes(c.name, DB)
    if main_tar is None:
        raise RuntimeError(f"T3's database not copied: {err}")
    wal_tar, werr = copy_bytes(c.name, DB + "-wal")
    if wal_tar is None and not any(w in (werr or "").lower() for w in ("could not find", "no such file")):
        raise RuntimeError(f"T3's database log not copied: {werr}")
    _scan(keys, "database", main_tar, wal_tar or b"")
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o700)
        path = Path(d) / "state.sqlite"
        path.write_bytes(_one_file(main_tar))
        if wal_tar is not None:
            (Path(d) / "state.sqlite-wal").write_bytes(_one_file(wal_tar))
        src = sqlite3.connect(path)
        try:
            dst = sqlite3.connect(":memory:")
            src.backup(dst)
            data = dst.serialize()
            dst.close()
        finally:
            src.close()
    _scan(keys, "database", data)
    return data


def _threads_json(db: bytes) -> tuple[str, list[str]]:
    """Every thread of a T3 database as JSON (the trajectory unit's reader),
    and their ids."""
    from cohbench.trajectory import t3 as t3read

    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o700)
        path = Path(d) / "state.sqlite"
        path.write_bytes(db)
        threads = t3read.from_sqlite(path)
    out = []
    for t in threads.values():
        rec = asdict(t) if is_dataclass(t) else dict(t)
        rec.pop("source", None)
        out.append(rec)
    return json.dumps({"threads": out, "source": "t3-state.sqlite"}, ensure_ascii=False), list(threads)


def capture_stopped(ep: "Episode", c: Container, segment: int, thread_id: str | None, versions: dict[str, Any]) -> dict[str, Any]:
    """One segment's T3 record from its stopped harness container: the
    database (WAL folded in), every thread in it as JSON, the harness's
    session files and the server's output, each held in memory and scanned
    for every key before anything is kept. Raises on any source that cannot
    be read."""
    keys = ep._keys()
    db = _database(c, keys)
    threads_text, ids = _threads_json(db)
    sessions, err = copy_bytes(c.name, ep.h.session_dir)
    if sessions is None:
        raise RuntimeError(f"harness sessions not copied: {err}")
    log = c.logs()
    _scan(keys, "captured record", threads_text.encode(), sessions, log.encode())
    return {
        "segment": segment, "thread_id": thread_id, "container": c.name, "captured_utc": utc_now(),
        "sqlite": db, "threads_text": threads_text, "thread_ids": ids, "sessions_tar": sessions, "server_log": log,
        "harness_version": versions.get("harness_version"), "t3_version": versions.get("t3_version"),
    }


def _copy_tree_scanned(src: Path, dst: Path, keys: list) -> list[str]:
    """Copy a host directory, reading each file into memory and scanning it
    before writing it. Returns the files that held the key (not written)."""
    held = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        data = p.read_bytes()
        if any(key_hits(data, k) for k in keys):
            held.append(str(rel))
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(data)
    return held


def _in_time(stage: str) -> None:
    """Between the export's host-only stages (normalisation, validation,
    publication), which cannot be cut short inside: refuse to start the
    next one after the deadline."""
    from cohbench.env.docker import deadline_passed

    if deadline_passed():
        raise ExportFailed(stage, ["the export's deadline passed before this stage"])


def _runner_events(ep: "Episode", end_reason: str, end_utc: str) -> list[Any]:
    from cohbench.trajectory.normalise import RunnerEvent

    evs = []
    on = set(ep.driver_events)
    if "correction_delivered" in on:
        for c in ep.corrections:
            if c.get("delivered_utc") and c.get("message_id"):
                # The driver's own delivery mark, which the bundle maps to its batch.
                d = c.get("delivery") or {}
                evs.append(RunnerEvent("correction_delivered", c["delivered_utc"], {"message": c["message_id"]}, {"mark": d.get("mark_label") if d.get("mark_taken") else None}))
    for e in ep.runner_events:
        if e.get("by_driver") and e["type"] not in on:
            continue
        if e["type"] == "controlled_reset" and e.get("continuation") not in (None, "launched"):
            continue  # a handoff whose segment never got a dispatch is not in the trajectory
        # The caller's episode_end is normalised to the one end instant (the
        # export checked they agree).
        utc = end_utc if e["type"] == "episode_end" else e["utc"]
        evs.append(RunnerEvent(e["type"], utc, e.get("ref"), dict(e.get("fields") or {}), e.get("end_utc")))
    if "episode_end" in on and not any(e.type == "episode_end" for e in evs):
        # Dated at the start of the export's excluded interval: when export
        # was called with the runner's end mark, else just after the
        # shutdown mark (see `export`).
        evs.append(RunnerEvent("episode_end", end_utc, None, {"reason": end_reason}))
    return evs


# The driver's send kinds as the trajectory unit's user kinds. A refusal's
# words answer a hand-back (a secret request), so they are a reply, whether
# they were steered in or queued.
KINDS = {"opening": "opening_prompt", "reply": "reply", "correction": "correction", "reset": "reset", "refusal": "reply"}


def export(ep: "Episode", dest: Path, **kw: Any) -> dict[str, Any]:
    """`_export`, holding to the ExportFailed contract: any other error is
    reported as ExportFailed with the stage it happened in."""
    progress = {"stage": "artifacts"}
    try:
        return _export(ep, dest, progress=progress, **kw)
    except ExportFailed:
        raise
    except Exception as exc:
        raise ExportFailed(progress["stage"], [f"{type(exc).__name__}: {exc}"]) from exc


def _export(ep: "Episode", dest: Path, *, progress: dict[str, str], task: "TaskInfo", end_reason: str, end_utc: str | None, open_exclusion: Any, end_mark: str | None = None,
            elapsed_cutoff: bool = False,
            user_kinds: dict[str, str] | None = None, answers: dict[str, str] | None = None, marks: list[str] | None = None,
            probe_targets: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    from cohbench.trajectory import Hints, Segment, normalise, validate
    from cohbench.trajectory import bundle as tbundle
    from cohbench.trajectory import t3 as t3read
    from cohbench.trajectory.adapters import profile as get_profile

    assert ep.env is not None and ep.env.site is not None
    env = ep.env
    keys = ep._keys()
    dest = Path(dest).resolve()
    if dest.exists():
        raise ExportFailed("artifacts", [f"{dest} exists; an export is published once"])
    if end_mark is not None and env.watcher_spec is None:
        raise ExportFailed("artifacts", ["an end mark was given, and this episode has no watcher"])
    if end_utc is None:
        raise ExportFailed("artifacts", ["no end instant"])
    if end_mark is not None and not any(m["label"] == end_mark and m.get("ok") for m in ep.marks if m.get("segment") == ep.segment):
        raise ExportFailed("artifacts", [f"the end mark {end_mark!r} was not acknowledged in the current segment"])
    progress["stage"] = "quiesce"
    described = ep.describe()  # versions, while the container still runs
    live = env.harness
    # Quiesce: nothing writes once copying starts.
    reasons: list[str] = []
    stopped = env.stop_server()
    if not stopped["stopped"]:
        reasons.append(f"t3 serve did not stop: {stopped}")
    try:
        # The watcher's contract: the agent's container is stopped before
        # the final mark, so no write races it.
        env.stop_container()
    except Exception as exc:
        reasons.append(f"the harness container did not stop: {exc}")
    shutdown = None
    if env.watcher_spec is not None:
        try:
            # Taken with the agent's container stopped: strict, it must be
            # accepted and straddled by no batch.
            # It names the episode's end instant (D-037): the watcher confirms
            # coverage up to the end, whatever happened during quiescing.
            # At an elapsed cutoff (the end is the deadline instant the caller
            # gave) a batch may straddle the end: it is outside the episode by
            # decision (D-035), so a straddled answer is accepted then only.
            shutdown = env.mark(SHUTDOWN_MARK, at=end_utc, final=True, allow_straddle=elapsed_cutoff)
        except Exception as exc:
            reasons.append(f"the watcher's shutdown mark failed: {exc}")
        stop = env.stop_watcher()
        if not (stop or {}).get("clean"):
            reasons.append(f"the watcher did not shut down cleanly: {stop}")
    try:
        site_stop = env.stop_site()
    except Exception as exc:
        site_stop = None
        reasons.append(f"the gateway or scripted server did not stop: {exc}")
    if reasons:
        raise ExportFailed("quiesce", reasons)
    interval_start = open_exclusion()  # opened when export was called
    # The bundle's end: the runner's mark, else the shutdown mark; none
    # without a watcher.
    bundle_end = end_mark if end_mark is not None else (SHUTDOWN_MARK if env.watcher_spec is not None else None)
    progress["stage"] = "capture"
    # Capture from the stopped containers.
    captured = dict(ep.captured)
    awaiting = ep.thread_id is None and ep.awaiting_launch is not None
    if awaiting:
        # A handoff whose continuation was never sent: the new segment has no
        # thread. It is left out of the trajectory and the bundle (its watcher
        # log stays in the raw record), and the bundle's end is the end
        # instant, not a mark of that segment.
        bundle_end = None
    else:
        try:
            captured[ep.segment] = capture_stopped(ep, live, ep.segment, ep.thread_id, described)
        except Exception as exc:
            raise ExportFailed("capture", [f"segment {ep.segment}: {type(exc).__name__}: {exc}"]) from None
        ep.captured[ep.segment] = captured[ep.segment]
    missing = [n for n in range(1, ep.segment + (0 if awaiting else 1)) if n not in captured]
    if missing:
        raise ExportFailed("capture", [f"no record of segment(s) {missing}"])
    progress["stage"] = "artifacts"
    stage = dest.parent / f".{dest.name}.staging-{secrets.token_hex(4)}"
    stage.mkdir(parents=True)

    def put(rel: str, data: str | bytes) -> None:
        write_scanned(stage / rel, data, keys)

    try:
        manifest: dict[str, Any] = {
            "format": "cohbench-episode-raw", "version": 2, "written_utc": utc_now(),
            "episode_start_utc": ep.clock.start_utc, "environment_start_utc": env.started_utc, "episode_end_utc": end_utc,
            # The episode clock at the episode's end (which may precede this
            # export, D-035); the export's own interval is still open here and
            # gets its end at close.
            "clock": {"episode_s": round(ep.clock.elapsed_at(end_utc), 3), "wall_s": round(ep.wall_elapsed(), 3),
                      "excluded": ep.clock.excluded + [{"reason": "export", "start_utc": interval_start, "end_utc": None}]},
            "harness": ep.h.name, "harness_exercised": ep.h.exercised, "model": ep.model, "provider": ep.provider, "options": ep.options,
            "harness_default_model": ep.harness_default_model, "settings_written": ep.settings_written, "diagnostic_question_tool": ep.diagnostic_question_tool,
            "mode": ep.mode, "coached": env.instruction_record, "workspace": env.workspace_record, "upstream": ep.upstream.kind,
            "images": env.record()["images"], "image_ids": {}, "docker_names": dict(env.names), "segments": [],
            "sent": ep.state()["sent"], "turns": ep.turn_results, "corrections": ep.corrections, "probes": ep.probes, "handoffs": ep.handoffs,
            "marks": ep.marks, "runner_marks": list(marks or []), "end_mark": bundle_end,
            "shutdown_mark": ({k: shutdown.get(k) for k in ("batch", "at", "straddled", "covered_utc", "requested_utc", "acked_utc")} | {"label": SHUTDOWN_MARK}) if shutdown else None,
            "quiesce": {"server": stopped, "site": site_stop},
            "runner_events": ep.runner_events, "user_waits": ep.user_waits, "gateway_usage": None, "describe": described,
        }
        for role, tag in manifest["images"].items():
            manifest["image_ids"][role] = image_info(tag)["id"]
        for n in sorted(captured):
            rec = captured[n]
            sdir = stage / f"segment-{n}"
            sdir.mkdir(parents=True)
            put(f"segment-{n}/t3-state.sqlite", rec["sqlite"])
            put(f"segment-{n}/threads.json", rec["threads_text"])
            put(f"segment-{n}/server-log.txt", rec["server_log"])
            ex = extract(rec["sessions_tar"], sdir / "sessions")
            if not ex["ok"]:
                reasons.append(f"segment {n} sessions: {ex['error']}")
            files = sorted(glob.glob(str(sdir / "sessions" / "**" / "*.jsonl"), recursive=True))
            if not files:
                reasons.append(f"segment {n}: no harness session file")
            seg_env = next((s for s in env.segments if s.number == n), None)
            entry = {k: rec[k] for k in ("segment", "thread_id", "container", "captured_utc", "thread_ids", "harness_version", "t3_version")}
            entry.update({"sqlite": f"segment-{n}/t3-state.sqlite", "threads_file": f"segment-{n}/threads.json", "session_files": [str(Path(f).relative_to(stage)) for f in files]})
            if seg_env is not None:
                entry.update({"started_utc": seg_env.started_utc, "ended_utc": seg_env.ended_utc, "volume": seg_env.volume, "watcher_stop": seg_env.watcher_stop})
                if env.watcher_spec is not None:
                    if seg_env.watcher_out is None or not (seg_env.watcher_out / "events.jsonl").exists():
                        reasons.append(f"segment {n}: no watcher log")
                    else:
                        held = _copy_tree_scanned(seg_env.watcher_out, stage / "watcher" / f"segment-{n}", keys)
                        if held:
                            reasons.append(f"segment {n} watcher files held the key: {held[:5]}")
                        entry["watcher_out"] = f"watcher/segment-{n}"
                    if not (seg_env.watcher_stop or {}).get("clean"):
                        reasons.append(f"segment {n}: the watcher did not shut down cleanly: {seg_env.watcher_stop}")
            manifest["segments"].append(entry)
        if awaiting:
            seg_env = env.segments[-1]
            entry = {"segment": ep.segment, "thread_id": None, "dispatch": "none: the continuation was never sent", "started_utc": seg_env.started_utc,
                     "watcher_stop": seg_env.watcher_stop}
            if seg_env.watcher_out is not None and (seg_env.watcher_out / "events.jsonl").exists():
                held = _copy_tree_scanned(seg_env.watcher_out, stage / "watcher" / f"segment-{ep.segment}", keys)
                if held:
                    reasons.append(f"segment {ep.segment} watcher files held the key: {held[:5]}")
                entry["watcher_out"] = f"watcher/segment-{ep.segment}"
            manifest["segments"].append(entry)
        # The service logs, copied out of the stopped containers through Docker
        # (the host may not read the files those services' users own),
        # scanned in memory, unpacked into a private directory, then staged.
        import tempfile

        for name in ("gateway", "scripted"):
            if (env.site.gateway if name == "gateway" else env.site.scripted) is None:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                got = env.site.copy_logs(name, Path(tmp) / "log", keys)
                if not (got["dir"] / "requests.jsonl").exists():
                    reasons.append(f"{name}: no requests.jsonl")
                    continue
                held = _copy_tree_scanned(got["dir"], stage / name, keys)
                if held:
                    reasons.append(f"{name} files held the key: {held[:5]}")
        if (stage / "gateway" / "requests.jsonl").exists():
            from cohbench.env.environment import gateway_episode_usage

            env.final_usage = gateway_episode_usage(stage / "gateway")
            manifest["gateway_usage"] = env.final_usage
        if reasons:
            raise ExportFailed("artifacts", reasons)

        _in_time("trajectory")
        progress["stage"] = "trajectory"
        # The full trajectory.
        try:
            prof = get_profile(ep.h.profile)
            segments = []
            compactions = []
            for n in sorted(captured):
                sdir = stage / f"segment-{n}"
                threads = t3read.from_sqlite(sdir / "t3-state.sqlite")
                compactions += [(n, tid, i["id"]) for tid, t in threads.items() for i in t.items if i.get("type") == "compaction"]
                files = sorted(glob.glob(str(sdir / "sessions" / "**" / "*.jsonl"), recursive=True))
                segments.append(Segment(threads, captured[n]["thread_id"], [prof.read_session(f) for f in files]))
            first = manifest["segments"][0]
            kinds = {s.message_id: KINDS[s.kind] for s in ep.sent if s.kind in KINDS and s.message_id and s.status == "accepted"} if "user_kinds" in ep.driver_events else {}
            kinds.update(user_kinds or {})
            hints = Hints(
                workspace=ep.workspace_path, user_kinds=kinds, answers={**ep.answers, **(answers or {})}, runner=_runner_events(ep, end_reason, end_utc),
                run_info={"harness": ep.h.name, "harness_version": first.get("harness_version"), "t3_version": first.get("t3_version"), "model": ep.model, "options": ep.options, "mode": ep.mode},
                # D-026: every harness's instruction file and the driver's ref
                # namespaces, from the harness descriptions (the same lists the
                # watcher was given).
                instruction_files=instruction_files(),
                driver_ref_namespaces=DRIVER_REF_NAMESPACES,
                # D-020: the episode clock starts at the opening prompt's
                # dispatch, the same instant the raw manifest records.
                episode_start_utc=ep.clock.start_utc,
                # The current runner (it passes hand-back events or an episode
                # record) is authoritative about hand-backs: none recorded
                # means none happened, so the bundle does not infer them.
                handbacks_recorded=True if (task.episode is not None or any(e["type"] == "handback" for e in ep.runner_events)) else None,
            )
            # The episode clock's left-out intervals: every bundle time is on that clock.
            hints.excluded = [{k: e[k] for k in ("start_utc", "end_utc", "reason")} for e in ep.clock.excluded]
            res = normalise(segments, prof, hints)
        except ExportFailed:
            raise
        except Exception as exc:
            raise ExportFailed("trajectory", [f"{type(exc).__name__}: {exc}"]) from None
        put("trajectory/trajectory.jsonl", res.trajectory.dumps())
        put("trajectory/manifest.json", json.dumps(res.manifest, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        rep = validate(res.trajectory)
        report: dict[str, Any] = {"trajectory": rep.as_dict(), "trajectory_blindness": res.manifest["blindness"]}
        if compactions:
            mapped = sum(1 for e in res.trajectory.events if e["type"] == "compaction")
            if mapped < len(compactions):
                raise ExportFailed("trajectory", [f"T3 recorded {len(compactions)} compaction item(s) ({compactions[:3]}); the trajectory maps {mapped}"], report)
        if not rep.ok:
            reasons = list(rep.errors[:20])
            if rep.unknown:
                # T3 items the trajectory unit could not map: by type, with counts.
                types: dict[str, int] = {}
                for e in res.trajectory.events:
                    if e["type"] == "unknown":
                        types[str(e.get("item_type"))] = types.get(str(e.get("item_type")), 0) + 1
                reasons.append(f"{rep.unknown} T3 item(s) the trajectory unit does not map, by type: {types}")
            raise ExportFailed("trajectory", reasons or ["the trajectory did not validate"], report)
        if not res.manifest["blindness"]["ok"]:
            raise ExportFailed("blindness", [f"full trajectory: {h}" for h in (res.manifest["blindness"].get("structural") or [])][:20] or ["full trajectory blindness check failed"], report)

        _in_time("bundle")
        progress["stage"] = "bundle"
        # The scoring bundle (D-018): one watcher input per segment, joined by
        # the trajectory unit; the runner's end mark bounds the final state.
        try:
            watchers = [tbundle.WatcherInput.from_dir(stage / "watcher" / f"segment-{n}") for n in sorted(captured)] if env.watcher_spec is not None else None
            b = tbundle.make(res, watchers, task.identifiers, task.observations, None, episode_record=task.episode, probe_targets=probe_targets, end_mark=bundle_end)
        except Exception as exc:
            raise ExportFailed("bundle", [f"{type(exc).__name__}: {exc}"], report) from None
        bcheck = tbundle.validate(b, require_episode=task.episode is not None, instruction_files=instruction_files(), driver_ref_namespaces=DRIVER_REF_NAMESPACES)
        report["bundle"] = bcheck
        bad = {k: v for k, v in (bcheck.get("errors") or {}).items() if v}
        if bad:
            stage_name = "blindness" if any("blindness" in e for v in bad.values() for e in v) else "bundle"
            raise ExportFailed(stage_name, [f"{k}: {e}" for k, v in bad.items() for e in v][:30], report)
        _write_bundle(tbundle, b, stage / "bundle", keys)
        manifest["trajectory"] = {"path": "trajectory/trajectory.jsonl", "manifest": "trajectory/manifest.json", "sha256": res.trajectory.sha256(), "events": len(res.trajectory.events)}
        manifest["bundle"] = {"path": "bundle", "files": sorted(p.name for p in (stage / "bundle").iterdir())}
        env.write_record()
        put("environment.json", json.dumps(env.record(), indent=1, default=str))
        put("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False, default=str))
        _in_time("artifacts")
        # A last pass over everything staged.
        if keys:
            held = [str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file() and any(key_hits(p.read_bytes(), k) for k in keys)]
            if held:
                raise ExportFailed("artifacts", [f"staged files held the key: {held[:5]}"], report)
        os.replace(stage, dest)
    except ExportFailed as exc:
        if exc.stage in ("trajectory", "bundle", "blindness"):
            _keep_rejected(stage, dest, exc, manifest, keys)
        else:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"dest": str(dest), "manifest": manifest, "report": report, "files": logging_files(dest, captured)}


def _keep_rejected(stage: Path, dest: Path, exc: "ExportFailed", manifest: dict[str, Any], keys: list) -> None:
    """A rejection by the trajectory unit or the bundle keeps the raw record
    for examination and replay: everything a published export would hold
    except the bundle (T3's databases, threads, sessions, server logs, the
    service and watcher logs, the trajectory written so far, the manifest),
    each file already scanned when staged, plus `rejected.json` with the
    stage and reasons, at `<dest>.rejected`. Never published as `dest`."""
    try:
        shutil.rmtree(stage / "bundle", ignore_errors=True)
        write_scanned(stage / "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False, default=str), keys)
        write_scanned(stage / "rejected.json", json.dumps({"stage": exc.stage, "reasons": exc.reasons, "report": exc.report}, indent=1, default=str), keys)
        target = dest.parent / f"{dest.name}.rejected"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        os.replace(stage, target)
        exc.raw_record = str(target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)


def _write_bundle(tbundle: Any, b: Any, d: Path, keys: list) -> None:
    """The bundle's files, each scanned before it is written, and the blobs
    its change log names, read from the staged (already scanned) watcher
    stores and scanned again."""
    import hashlib

    for name, text in tbundle.dumps(b).items():
        write_scanned(d / name, text, keys)
    need = tbundle.stored_hashes(b.changes)
    stores = [Path(x) for x in (b.store if isinstance(b.store, (list, tuple)) else [b.store])] if b.store is not None else []
    for sha in sorted(need):
        src = next((s / sha[:2] / sha for s in stores if (s / sha[:2] / sha).is_file()), None)
        data = src.read_bytes() if src is not None else None
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            raise ExportFailed("bundle", [f"blob {sha} is missing from the watcher store or does not match its digest"])
        write_scanned(d / "blobs" / sha[:2] / sha, data, keys)


def logging_files(dest: Path, captured: dict[int, Any]) -> dict[str, list[str]]:
    """Per SPEC section 3 "Logging" item (the runner's episode_dir.LOGGING
    keys), the files of this export that hold it, relative to `dest`. Items
    only the runner holds (transcript, classifier labels) are not here."""
    segs = sorted(captured)

    def present(paths: list[str]) -> list[str]:
        return [p for p in paths if (dest / p).exists()]

    watcher = [f"watcher/segment-{n}/{f}" for n in segs for f in ("events.jsonl", "metrics.jsonl")]
    return {
        "trajectory": present(["trajectory/trajectory.jsonl", "trajectory/manifest.json"]),
        "identifier_registry": present(["bundle/identifiers.json"]),
        "user_wait": present(["manifest.json"]),
        "file_diffs": present(["bundle/changes.jsonl", *watcher]),
        "probe_answers": present(["manifest.json"]),
        "driver_tool_calls": present(["trajectory/trajectory.jsonl", *[f"segment-{n}/threads.json" for n in segs]]),
        "harness": present(["manifest.json"]),
        "driver_version": present(["manifest.json"]),
        "scoring_bundle": present([f"bundle/{f}" for f in ("scorer.jsonl", "identifiers.json", "changes.jsonl", "observations.jsonl", "episode.json")]),
        "runner_events": present(["manifest.json"]),
        "raw_record": present(["manifest.json", "environment.json", "gateway/requests.jsonl", *[f"segment-{n}/{f}" for n in segs for f in ("t3-state.sqlite", "threads.json", "server-log.txt")]]),
    }
