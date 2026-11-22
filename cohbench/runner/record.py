"""What the runner writes from a finished `Runner`: the episode record (the
runner's part of `episode.json`), the export handoff, and the run manifest.

The episode record has the fields of `cohbench.trajectory.episode` except
each probe's `about` and `boundary`: `bundle.make(episode_record=...,
probe_targets=...)` resolves those from `ExportHandoff.probe_targets` and the
scorer view. It holds no T3 id and no harness, model, provider or driver
name. Consumed tokens are in the run manifest only (D-022).
"""

from __future__ import annotations

import copy
from typing import Any

from .port import Description, ExportHandoff, Interval
from .runner import RunConfig, Runner
from .task import Task

EPISODE_FORMAT = "cohbench-episode"
RUN_FORMAT = "cohbench-run"
RUNNER_VERSION = "3"


def runner_sources() -> str:
    """A short hash of the runner's own sources, so a saved run says which revision made it."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    here = Path(__file__).parent
    for f in sorted(here.glob("*.py")) + sorted(here.glob("*.json")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def _episode_fields() -> tuple[str, ...]:
    from cohbench.trajectory import episode as tep

    return tuple(getattr(tep, "FIELDS", ()))


def episode_record(task: Task, cfg: RunConfig, r: Runner) -> dict[str, Any]:
    g = task.grouping
    end = r.end or {"reason": "runner_abort", "elapsed_ms": r.elapsed_ms()}
    return {
        "format": EPISODE_FORMAT,
        "version": 1,
        "episode": r.scorer_id,  # generated and opaque; the caller's label stays in the run manifest (m6)
        "grouping": {"family": g.family, "seed": g.seed, "pair": g.pair, "sibling": g.sibling, "control": g.control,
                     "horizon": g.horizon, "mode": cfg.mode},
        "budget": {"limits": dict(task.budgets), "consumed": {"elapsed_ms": end["elapsed_ms"], "user_wait_ms": r.user_wait_ms}},
        "planted": [{"name": e.name, "scheduled": e.scheduled()} for e in task.events],
        "reach": {"status": "not_reached" if r.not_reached else "reached", "not_reached": sorted(r.not_reached)},
        "answer_key": {"family": g.family, "payload": copy.deepcopy(task.answer_key)},
        "control": None if task.control is None else {"expected": copy.deepcopy(task.control["expected"]), "passed": None},
        "probes": [dict(p) for p in r.probes],
        # Explanation's two scored answers are view seqs, which only the bundle maker knows: it fills them.
        "answers": [] if g.family == "explanation" else None,
        "awareness": {"asked": r.awareness["asked"], "outcome": r.awareness["outcome"]},  # the reply is in the run manifest
        "end": {"reason": end["reason"], "duration_ms": end["elapsed_ms"]},
        # The polling interval is a fixed parameter of the benchmark (README): in the scorer
        # record once the trajectory unit's episode table has the field.
        **({"poll_s": cfg.poll_s} if "poll_s" in _episode_fields() else {}),
    }


TARGET_KEYS = ("about", "ref", "deferred", "event")  # D-028: `coincides` is no longer sent


def bundle_targets(targets: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The runner's probe targets in the trajectory unit's shape: what the probe
    is about, the event it belongs to, and whether its copy was deferred."""
    return {pid: {k: t[k] for k in TARGET_KEYS if k in t} for pid, t in targets.items()}


def handback_events(r: Runner) -> list[dict[str, Any]]:
    """D-028's hand-back records as runner events, once the trajectory unit's
    table has the type; until then they are in the run manifest only."""
    from cohbench.trajectory import format as F

    spec = F.RUNNER_FIELDS.get("handback")
    if spec is None:
        return []
    out = []
    for hb in r.handback_log:
        ref = {"item": hb["closing"]} if "target" in spec and hb["closing"] else None
        fields = {k: hb[k] for k in spec if k in hb and k not in ("target", "t_end")}
        out.append({"type": "handback", "utc": hb["utc"], "ref": ref, "fields": fields, "end_utc": None})
    return out


def handoff(task: Task, cfg: RunConfig, r: Runner, record: dict[str, Any], excluded: list[Interval], run_info: dict[str, Any]) -> ExportHandoff:
    return ExportHandoff(
        runner_events=[dict(e, fields=dict(e["fields"])) for e in r.runner_events] + handback_events(r),
        user_kinds=dict(r.user_kinds),
        answers=dict(r.answers),
        identifiers=copy.deepcopy(task.identifiers),
        observations=copy.deepcopy(task.observations),
        episode_record=record,
        probe_targets=bundle_targets(r.probe_targets),
        marks=list(r.marks),
        end_mark=r.end_mark,
        end_utc=(r.end or {}).get("utc"),
        end_reason=(r.end or {}).get("reason", "runner_abort"),
        post_run=dict(r.post_run) if r.post_run else None,
        excluded=[{"start_utc": i.start_utc, "end_utc": i.end_utc, "reason": i.reason} for i in excluded],
        run_info=run_info,
        submitted={k: dict(v) for k, v in r.submitted.items()},
    )


def run_manifest(task: Task, cfg: RunConfig, r: Runner | None, desc: Description | None, *, started_utc: str, ended_utc: str,
                 wall_s: float, excluded: list[Interval], status: str, failure: str | None, detail: str | None,
                 export: dict[str, Any], close: dict[str, Any]) -> dict[str, Any]:
    end = (r.end if r else None) or {}
    return {
        "format": RUN_FORMAT,
        "version": 1,
        "runner_version": f"{RUNNER_VERSION}+{runner_sources()}",
        "episode": cfg.episode_id,
        "scorer_episode": r.scorer_id if r else None,
        "status": status,  # usable, not_reached, failed
        "harness": desc.harness if desc else cfg.harness,
        "harness_version": desc.harness_version if desc else None,
        "t3_version": desc.t3_version if desc else None,
        "driver_version": desc.driver_version if desc else None,
        "model": desc.model if desc else cfg.model,
        "mode": cfg.mode,
        "upstream": cfg.upstream[0],
        "task": {"instance": task.instance, "digest": task.digest, "dir": str(task.root.resolve()), "family": task.grouping.family},
        "after_deadline": list(r.after_deadline) if r else [],
        "overruns": {"count": len(r.overruns) if r else 0, "total_ms": sum(x["overrun_ms"] for x in r.overruns) if r else 0,
                     "waits": list(r.overruns) if r else []},
        "timings": {"started_utc": started_utc, "ended_utc": ended_utc, "wall_s": round(wall_s, 3),
                    "episode_end_utc": end.get("utc"), "duration_ms": end.get("elapsed_ms"),
                    "excluded": [{"start_utc": i.start_utc, "end_utc": i.end_utc, "reason": i.reason} for i in excluded]},
        "budgets": {
            "limits": dict(task.budgets),
            "consumed": {"elapsed_ms": end.get("elapsed_ms"), "user_wait_ms": r.user_wait_ms if r else 0,
                         "tokens": r.tokens if r else None, "tokens_complete": r.tokens_complete if r else None},
            "usage": r.usage_raw if r else {},
        },
        # `utc` and `duration_ms`: the episode's end (an elapsed cutoff's deadline, D-035); `stopped_utc`: when the
        # agent was actually stopped; `noticed_ms`: the episode clock when the runner noticed the end.
        "end": {"reason": end.get("reason"), "detail": end.get("detail"), "budget": end.get("budget"), "end_mark": r.end_mark if r else None,
                "utc": end.get("utc"), "duration_ms": end.get("elapsed_ms"), "stopped_utc": end.get("stopped_utc"), "noticed_ms": end.get("noticed_ms")},
        "failure": failure,
        "failure_detail": detail,
        "reachability": {"all_required_reached": not (r.not_reached if r else []), "not_reached": list(r.not_reached) if r else [],
                         "reopens_used": r.reopens if r else 0, "reopens_limit": task.max_reopens},
        "events": {e.name: {"status": r.ev[e.name].status, "reached_utc": r.ev[e.name].reached_utc, "fired_utc": r.ev[e.name].fired_utc,
                            "delivery": r.ev[e.name].delivery} for e in task.events} if r else {},
        "corrections": list(r.corrections) if r else [],
        "probes": list(r.probe_log) if r else [],
        "handback_log": list(r.handback_log) if r else [],
        "probes_not_taken": list(r.probes_not_taken) if r else [],
        "post_run": r.post_run if r else None,
        "awareness": dict(r.awareness) if r else None,
        "submitted": dict(r.submitted) if r else {},
        "handbacks": r.ctx.handbacks if r else 0,
        "turns": r.ctx.turns if r else 0,
        "outputs": r.ctx.outputs if r else 0,
        "messages_sent": len(r.sends) if r else 0,
        "export": export,
        "close": close,
        "config": {"poll_s": cfg.poll_s, "stall_s": cfg.stall_s, "max_turns": cfg.max_turns, "awareness_timeout_s": cfg.awareness_timeout_s,
                   "usage_grace_s": cfg.usage_grace_s},
    }
