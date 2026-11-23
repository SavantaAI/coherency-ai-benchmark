"""One episode from task instance to published directory.

    outcome = run_episode(task, cfg, factory, ("scripted", rules_file), out)

`factory(spec)` opens the episode and returns an `EpisodePort`; stage 2's
adapter provides the real one. The policy is a kind and a file; the run
builds the policy's factory from that file itself (`policy.POLICY_KINDS`),
so the file admission checks is the file the policy uses. Whatever happens, the episode is closed and the directory
is published:

| Status | Directory | When |
|---|---|---|
| `usable` | `<out>/<episode>` | finished or cut off by a budget, every required event fired, export accepted, closed cleanly |
| `not_reached` | `<out>/<episode>.not_reached` | as usable, except a required event never fired (`planted: not_reached`) |
| `failed` | `<out>/<episode>.failed-<reason>` | anything else |
"""

from __future__ import annotations

import copy
import hashlib
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import record
from .episode_dir import EpisodeDir, _write_json, _write_jsonl
from .eventlog import EventLog, utc_iso
from .policy import policy_factory as make_policy_factory
from .port import Description, EpisodePort, EpisodeSpec, ExportRejected, Interval
from .runner import RunConfig, Runner
from .task import Task

USABLE_ENDS = ("finished", "budget_cutoff")
Factory = Callable[[EpisodeSpec], EpisodePort]


@dataclass(frozen=True)
class Outcome:
    path: Path
    status: str  # usable, not_reached, failed
    end_reason: str | None
    failure: str | None
    detail: str | None

    @property
    def ok(self) -> bool:
        return self.status == "usable"


@dataclass
class _State:
    ep: EpisodePort | None = None
    runner: Runner | None = None
    desc: Description | None = None
    excluded: list[Interval] = field(default_factory=list)
    failure: str | None = None
    detail: str | None = None
    export_info: dict[str, Any] = field(default_factory=lambda: {"ok": False, "error": "not exported"})
    export_files: dict[str, list[str]] = field(default_factory=dict)
    close_info: dict[str, Any] = field(default_factory=lambda: {"ok": None})

    def fail(self, failure: str, detail: str | None) -> None:
        if self.failure is None:
            self.failure, self.detail = failure, detail


def stage_metric(task: Task, dest: Path) -> Path:
    """A private copy of the metric directory built from its validated
    manifest alone: each directory and file with the manifest's mode, each
    link with its target, every entry timed at the manifest's one mtime (the
    workspace's when the metric manifest has none). A file whose content no
    longer matches the manifest stops the copy. This copy is what the watcher
    mounts, so nothing about the host directory reaches the metric."""
    m = task.watcher.metric
    assert m is not None
    doc = m.manifest_doc
    when = doc.get("mtime", task.workspace.manifest_doc.get("mtime", 0))
    dest.mkdir(parents=True, exist_ok=False)
    entries = sorted(doc["entries"], key=lambda e: e["path"])
    for e in entries:
        p = dest / e["path"]
        kind = e.get("type", "file")
        if kind == "dir":
            p.mkdir()
        elif kind == "symlink":
            os.symlink(e["target"], p)
        else:
            data = (m.dir / e["path"]).read_bytes()
            if len(data) != e["size"] or hashlib.sha256(data).hexdigest() != e["sha256"]:
                raise RuntimeError(f"metric/{e['path']} changed after the task was loaded")
            p.write_bytes(data)
    for e in reversed(entries):  # modes and times last, deepest first, so a read-only directory can still be filled
        p = dest / e["path"]
        if e.get("type", "file") != "symlink":
            os.chmod(p, int(e["mode"], 8))
            os.utime(p, (when, when))
        elif os.utime in os.supports_follow_symlinks:
            os.utime(p, (when, when), follow_symlinks=False)
    os.chmod(dest, 0o755)
    os.utime(dest, (when, when))
    return dest


def spec_for(task: Task, cfg: RunConfig, out_dir: Path, metric_dir: Path | None = None) -> EpisodeSpec:
    """`metric_dir`: the staged copy of the metric directory (`stage_metric`); the task's own when not given."""
    m = task.watcher.metric
    return EpisodeSpec(
        harness=cfg.harness, model=cfg.model, upstream=cfg.upstream, mode=cfg.mode,
        coached_sentence=task.coached_sentence if cfg.mode == "coached" else None,
        workspace_dir=task.workspace.dir, workspace_manifest=task.workspace.manifest(),
        watcher_ignore=task.watcher.ignore, metric_command=m.command if m else None,
        metric_dir=(metric_dir or m.dir) if m else None,
        metric_manifest=copy.deepcopy(m.manifest_doc) if m else None, metric_timeout_s=m.timeout_s if m else None, out_dir=out_dir, collect=task.collect,
    )


def host_files(cfg: RunConfig, policy: tuple[str, Path]) -> dict[str, Path]:
    """Host-only files opened by the policy or a scripted upstream."""
    out = {"user policy": Path(policy[1])}
    if cfg.upstream[0] == "scripted":
        out["model script"] = Path(cfg.upstream[1])
    if policy[0] == "classifier" and (key := os.environ.get("COHBENCH_CLASSIFIER_KEY_FILE")):
        out["classifier credential"] = Path(key)
    return out


def admit_host_files(task: Task, cfg: RunConfig, policy: tuple[str, Path]) -> None:
    """The task checked again with the host-only documents this run opens
    (m6), and it must be the task that was loaded."""
    from .task import TaskError, check

    again, problems, _ = check(task.root, host_files(cfg, policy))
    if problems:
        raise TaskError(problems)
    assert again is not None
    if again.digest != task.digest:
        raise ValueError("run_episode: the task directory changed after it was loaded")


def run_episode(task: Task, cfg: RunConfig, factory: Factory, policy: tuple[str, str | Path], out: Path, *,
                sleep: Callable[[float], None] = time.sleep, utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Outcome:
    """`policy` is (kind, file): the run builds the policy's factory from that
    file itself, after admission checked the task against it and, for a
    scripted upstream, against the model script, before anything is opened."""
    if cfg.mode not in ("default", "coached"):
        raise ValueError("mode: default or coached")
    kind, ppath = policy[0], Path(policy[1])
    admit_host_files(task, cfg, (kind, ppath))
    policy_factory = make_policy_factory(kind, ppath)
    d = EpisodeDir(out, cfg.episode_id)
    s = _State()
    log = EventLog(d.path("runner/events.jsonl"), utcnow)
    started_utc, t0 = utc_iso(utcnow()), time.monotonic()
    log.write("run_start", episode=cfg.episode_id, harness=cfg.harness, model=cfg.model, mode=cfg.mode, upstream=cfg.upstream[0],
              task=task.instance, task_digest=task.digest, budgets=task.budgets, return_delay_ms=task.return_delay_ms,
              events=[e.name for e in task.events])
    status = "failed"
    try:
        policy = None
        try:
            policy = policy_factory()
        except Exception as exc:  # noqa: BLE001
            log.write("error", where="policy", error=repr(exc), traceback=traceback.format_exc())
            s.fail("runner_abort", f"the user policy could not be made: {exc!r}")
        staged = None
        if policy is not None and task.watcher.metric is not None:
            try:
                staged = stage_metric(task, d.path("metric"))
                log.write("metric_staged", dir=str(staged), entries=len(task.watcher.metric.manifest_doc["entries"]))
            except Exception as exc:  # noqa: BLE001
                log.write("error", where="metric staging", error=repr(exc), traceback=traceback.format_exc())
                s.fail("runner_abort", f"the metric directory could not be staged: {exc!r}")
                policy = None
        if policy is not None:
            try:
                s.ep = factory(spec_for(task, cfg, d.path("driver"), staged))
            except Exception as exc:  # noqa: BLE001
                log.write("error", where="open", error=repr(exc), traceback=traceback.format_exc())
                s.fail("driver_failure", f"the episode could not be opened: {exc!r}")
        if s.ep is not None:
            _drive(task, cfg, s, policy, log, d, sleep, utcnow)
    except BaseException as exc:
        log.write("error", where="run", error=repr(exc), traceback=traceback.format_exc())
        s.fail("runner_abort", f"interrupted: {exc!r}")
        raise
    finally:
        if s.ep is not None:
            try:
                s.ep.close()
                s.close_info = {"ok": True}
                log.write("closed")
            except Exception as exc:  # noqa: BLE001
                s.close_info = {"ok": False, "error": repr(exc)}
                log.write("error", where="close", error=repr(exc))
                s.fail("close_failed", f"the episode could not be closed: {exc!r}")
        r = s.runner
        if s.failure is None:
            status = "not_reached" if r is not None and r.not_reached else "usable"
        _write_json(d.path("run.json"), record.run_manifest(task, cfg, r, s.desc, started_utc=started_utc, ended_utc=utc_iso(utcnow()),
                                                              wall_s=time.monotonic() - t0, excluded=s.excluded, status=status, failure=s.failure,
                                                              detail=s.detail, export=s.export_info, close=s.close_info))
        index = d.write_index(s.export_files)
        log.write("published", status=status, failure=s.failure, detail=s.detail, missing=[i["item"] for i in index["items"] if not i["present"]])
        log.close()
        path = d.publish(status, s.failure, s.detail)
    return Outcome(path, status, r.end["reason"] if r is not None and r.end else None, s.failure, s.detail)


def _drive(task: Task, cfg: RunConfig, s: _State, policy: Any, log: EventLog, d: EpisodeDir,
           sleep: Callable[[float], None], utcnow: Callable[[], datetime]) -> None:
    ep = s.ep
    assert ep is not None
    r = s.runner = Runner(task, cfg, ep, policy, log, sleep=sleep, utcnow=utcnow)
    try:
        r.run()
    finally:
        flush = getattr(policy, "flush", None)
        if callable(flush):
            try:
                r.classifier_cleanup = flush()
                log.write("classifier_cleanup", **(r.classifier_cleanup or {}))
            except Exception as exc:
                log.write("error", where="classifier cleanup", error=repr(exc))
                s.fail("runner_abort", "classifier cleanup failed")
    assert r.end is not None
    if r.end["reason"] not in USABLE_ENDS:
        s.fail(r.end["reason"], r.end["detail"])
    try:
        s.desc = ep.describe()
    except Exception as exc:  # noqa: BLE001
        log.write("error", where="describe", error=repr(exc))
    try:
        s.excluded = list(ep.excluded())
    except Exception as exc:  # noqa: BLE001
        log.write("error", where="excluded", error=repr(exc))
    rec = record.episode_record(task, cfg, r)
    _write_json(d.path("episode-record.json"), rec)
    d.write_transcript(r.ordered())
    _write_jsonl(d.path("classifier.jsonl"), r.classifier + ([{"awareness": r.awareness_record}] if r.awareness_record else []))
    run_info = {"episode": cfg.episode_id, "harness": s.desc.harness if s.desc else cfg.harness,
                "model": s.desc.model if s.desc else cfg.model, "mode": cfg.mode, "corrections": r.corrections}
    h = record.handoff(task, cfg, r, rec, s.excluded, run_info)
    log.write("export_start", runner_events=len(h.runner_events), user_kinds=len(h.user_kinds), answers=len(h.answers), marks=len(h.marks),
              end_mark=h.end_mark)
    r._clear_ms()  # the export is left out of the clock; no runner event may fall inside it
    try:
        res = ep.export(d.path("export"), h)
        s.export_files = {k: list(v) for k, v in res.files.items()}
        s.export_info = {"ok": True, "error": None, "report": res.report}
        log.write("export_done", files=s.export_files)
    except ExportRejected as exc:
        s.export_info = {"ok": False, "error": str(exc), "stage": exc.stage, "reasons": exc.reasons, "report": exc.report}
        log.write("export_rejected", stage=exc.stage, reasons=exc.reasons, report=exc.report)
        s.fail("export_rejected", str(exc))
    except Exception as exc:  # noqa: BLE001
        s.export_info = {"ok": False, "error": repr(exc)}
        log.write("error", where="export", error=repr(exc), traceback=traceback.format_exc())
        s.fail("export_failed", repr(exc))
    _write_json(d.path("episode-record.json"), rec)
    try:
        s.excluded = list(ep.excluded())  # again, so the manifest has the export's own interval
    except Exception as exc:  # noqa: BLE001
        log.write("error", where="excluded after export", error=repr(exc))
