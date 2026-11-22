"""Command line: validate, blindness, normalise, changelog, registry.

    python -m cohbench.trajectory validate FILE [--allow-unknown]     trajectory or scorer view
    python -m cohbench.trajectory blindness FILE                      trajectory or scorer view
    python -m cohbench.trajectory normalise --profile pi --sqlite state.sqlite --main <thread id> \\
        --sessions <dir> --out <dir>                                  writes trajectory.jsonl, scorer.jsonl, manifest.json
    python -m cohbench.trajectory changelog WATCHER_OUT_DIR --manifest manifest.json --out changes.jsonl
    python -m cohbench.trajectory bundle --trajectory trajectory.jsonl --manifest manifest.json --watcher WATCHER_OUT_DIR \
        [--task task.json] [--episode episode.json] --out BUNDLE_DIR      the scoring bundle, validated
    python -m cohbench.trajectory validate-bundle BUNDLE_DIR
    python -m cohbench.trajectory verify-episode RUN_DIR [--task task.json]   rebuild a saved episode's bundle and compare digests
    python -m cohbench.trajectory registry trajectory.jsonl [--changes changes.jsonl] [--task task.json]   the audit registry

Exit status 0 when the check passes, 1 when it fails, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from . import bundle, changelog, episode, io, t3
from .observations import TaskObservations
from .adapters import profile as get_profile
from .blindness import scan, scan_view
from .format import VIEW_FORMAT
from .normalise import Hints, RunnerEvent, Segment, normalise
from .registry import TaskIdentifiers, build
from .validate import validate, validate_view
from .view import scorer_view


def _print(obj: object) -> None:
    json.dump(obj, sys.stdout, indent=1, sort_keys=True, ensure_ascii=True)
    sys.stdout.write("\n")


def _changes(watcher_out: list[str], manifest: dict, end_mark: str | None = None, excluded: list | None = None) -> changelog.ChangeLog:
    logs = [changelog.read_dir(d) for d in watcher_out]
    changelog.check_status([st for _, _, st in logs], end_mark)
    return changelog.join([(ev, ms) for ev, ms, _ in logs], manifest["episode_start_utc"], end_mark=end_mark, excluded=excluded or [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cohbench.trajectory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("file")
    v.add_argument("--allow-unknown", action="store_true")
    b = sub.add_parser("blindness")
    b.add_argument("file")
    n = sub.add_parser("normalise")
    n.add_argument("--profile", required=True)
    src = n.add_mutually_exclusive_group(required=True)
    src.add_argument("--sqlite", nargs="+", help="one T3 SQLite backup per source, in order")
    src.add_argument("--export", nargs="+", help="one export JSON per source")
    src.add_argument("--joined", help="a handoff's joined-trajectory.json")
    src.add_argument("--snapshot", help="an HTTP thread snapshot")
    n.add_argument("--transcript", help="with --snapshot: an HTTP transcript holding omitted outputs")
    n.add_argument("--main", nargs="+", help="the main thread id of each source (default: the one root thread)")
    n.add_argument("--sessions", nargs="+", default=[], help="one directory of harness session files per source")
    n.add_argument("--workspace", default="/work/project")
    n.add_argument("--hints", help="JSON: every Hints field: {user_kinds, answers, runner: [{type, utc, ref, fields, end_utc}], run_info, excluded, audit_only, instruction_files, driver_ref_namespaces}")
    n.add_argument("--out", required=True)
    c = sub.add_parser("changelog")
    c.add_argument("watcher_out", nargs="+", help="one watcher output directory per context segment, in order")
    c.add_argument("--manifest", required=True, help="for episode_start_utc")
    c.add_argument("--end-mark", help="the runner's watcher mark at the episode end")
    c.add_argument("--excluded", help="a JSON file of the episode clock's excluded intervals ({start_utc, end_utc, reason} or {t, t_end, reason})")
    c.add_argument("--out", required=True)
    bd = sub.add_parser("bundle")
    bd.add_argument("--trajectory", required=True)
    bd.add_argument("--manifest", required=True)
    bd.add_argument("--watcher", required=True, nargs="+", help="one watcher output directory per context segment, in order")
    bd.add_argument("--task", help="JSON: {identifiers: {...}, observations: {attempts: [{name, regex}]}}")
    bd.add_argument("--episode", help="JSON of an EpisodeInput (episode, grouping, limits, user_wait_ms, planted, answer_key, control, probes, awareness)")
    bd.add_argument("--record", help="JSON of the runner's episode record (cohbench/runner/record.py)")
    bd.add_argument("--collected-blobs", help="collected blob directory; defaults to export/collected/blobs beside --record")
    bd.add_argument("--probe-targets", help="JSON: {probe id: {about, ref, coincides?}}, beside --record")
    bd.add_argument("--end-mark", help="the runner's watcher mark at the episode end")
    bd.add_argument("--out", required=True)
    vb = sub.add_parser("validate-bundle")
    vb.add_argument("dir")
    ve = sub.add_parser("verify-episode", help="rebuild a saved episode's bundle from its raw record and compare it with the stored one")
    ve.add_argument("run_dir")
    ve.add_argument("--task", help="the task.json, when run.json names none that exists")
    r = sub.add_parser("registry")
    r.add_argument("file")
    r.add_argument("--changes", help="changes.jsonl")
    r.add_argument("--task", help="JSON: {patterns: [{kind, regex, case_sensitive}], minted: {text: kind}, min_token_length}")
    a = ap.parse_args(argv)

    if a.cmd in ("validate", "blindness"):
        f = io.read(a.file)
        is_view = f.header.get("format") == VIEW_FORMAT
        if a.cmd == "validate":
            rep = validate_view(f) if is_view else validate(f, allow_unknown=a.allow_unknown)
        else:
            rep = scan_view(f) if is_view else scan(f)
        _print(rep.as_dict())
        return 0 if rep.ok else 1
    if a.cmd == "normalise":
        prof = get_profile(a.profile)
        if a.sqlite:
            per_seg = [t3.from_sqlite(p) for p in a.sqlite]
        elif a.export:
            per_seg = [t3.from_export(p) for p in a.export]
        elif a.joined:
            per_seg = t3.from_joined(a.joined)
        else:
            per_seg = [t3.from_snapshot(a.snapshot, a.transcript)]
        mains = a.main or [None] * len(per_seg)
        if len(mains) != len(per_seg):
            ap.error("give one --main per source")
        segs = []
        for i, (threads, main) in enumerate(zip(per_seg, mains)):
            d = a.sessions[i] if i < len(a.sessions) else None
            files = sorted(glob.glob(str(Path(d) / "**" / "*.jsonl"), recursive=True)) if d else []
            segs.append(Segment(threads, main, [prof.read_session(f) for f in files]))
        h = json.loads(Path(a.hints).read_text()) if a.hints else {}
        res = normalise(segs, prof, Hints.from_dict(h, workspace=a.workspace))
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        view = scorer_view(res.trajectory)
        io.write(res.trajectory, out / "trajectory.jsonl")
        io.write(view, out / "scorer.jsonl")
        io.write_manifest(res.manifest, out / "manifest.json")
        rep, vrep = validate(res.trajectory), validate_view(view)
        ok = rep.ok and vrep.ok and res.manifest["blindness"]["ok"] and res.manifest["blindness_scorer_view"]["ok"]
        _print({"validate": rep.as_dict(), "validate_scorer_view": vrep.as_dict(), "blindness_ok": res.manifest["blindness"]["ok"],
                "blindness_scorer_view_ok": res.manifest["blindness_scorer_view"]["ok"], "out": str(out)})
        return 0 if ok else 1
    if a.cmd == "changelog":
        excluded = json.loads(Path(a.excluded).read_text()) if a.excluded else []
        log = _changes(a.watcher_out, io.read_manifest(a.manifest), a.end_mark, excluded)
        changelog.write(log, a.out)
        _print(log.header)
        return 0
    if a.cmd == "bundle":
        from .episode import EpisodeInput, ProbeInput
        from .normalise import Result

        res = Result(io.read(a.trajectory), io.read_manifest(a.manifest))
        t = json.loads(Path(a.task).read_text()) if a.task else {}
        ep = None
        if a.episode:
            d = json.loads(Path(a.episode).read_text())
            d["probes"] = [ProbeInput(**p) for p in d.get("probes", [])]
            ep = EpisodeInput(**d)
        record = json.loads(Path(a.record).read_text()) if a.record else None
        collected = None
        if record is not None and "collected" in record:
            from .collection import Collected

            directory = Path(a.collected_blobs) if a.collected_blobs else Path(a.record).parent / "export" / "collected" / "blobs"
            try:
                collected = Collected.read(record["collected"], directory)
            except (OSError, ValueError) as exc:
                print(f"bundle: cannot read collected blobs: {exc}", file=sys.stderr)
                return 2
        targets = json.loads(Path(a.probe_targets).read_text()) if a.probe_targets else None
        b_ = bundle.make(res, [bundle.WatcherInput.from_dir(d) for d in a.watcher], TaskIdentifiers.from_dict(t.get("identifiers", {})),
                         TaskObservations.from_dict(t.get("observations", {})), ep, episode_record=record, probe_targets=targets, end_mark=a.end_mark, collected=collected)
        # Validated before it is published: a bundle that fails is not written.
        pre = bundle.validate(b_, require_episode=bool(a.episode or a.record))
        if not pre["ok"]:
            _print(pre)
            return 1
        try:
            bundle.write(b_, a.out)
        except (OSError, bundle.BundleError) as exc:
            print(f"bundle: cannot publish {a.out}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        rep = bundle.validate(bundle.read(a.out), require_episode=bool(a.episode or a.record))
        _print(rep)
        return 0 if rep["ok"] else 1
    if a.cmd == "validate-bundle":
        try:
            b = bundle.read(a.dir)
        except Exception as exc:  # noqa: BLE001
            # Shape errors are reported, never raised (R6 of round 9).
            _print({"ok": False, "errors": {"bundle": [f"cannot be read: {type(exc).__name__}: {exc}"[:300]]}})
            return 1
        rep = bundle.validate(b)
        _print(rep)
        return 0 if rep["ok"] else 1
    if a.cmd == "verify-episode":
        from . import replay

        try:
            rep = replay.verify(a.run_dir, a.task)
        except replay.ReplayError as exc:
            print(f"verify-episode: {exc}", file=sys.stderr)
            return 2
        _print(rep)
        return 0 if rep["ok"] else 1
    if a.cmd == "registry":
        traj = io.read(a.file)
        changes = changelog.read(a.changes) if a.changes else None
        t = json.loads(Path(a.task).read_text()) if a.task else {}
        task = TaskIdentifiers.from_dict(t.get("identifiers", t)) if a.task else None
        _print(build(traj, changes, task))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
