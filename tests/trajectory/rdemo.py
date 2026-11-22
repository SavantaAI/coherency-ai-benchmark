"""Rebuild the kept end-to-end episode `rdemo-1` from its raw record: the
two T3 databases with their harness sessions, the watcher logs, and what
the runner recorded (its sends, runner events, probe targets and episode
record), the way the driver's export assembles them
(cohbench/driver/export.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cohbench.trajectory import bundle, replay

ROOT = Path(__file__).resolve().parents[2]
RDEMO = ROOT / "runs" / "runner-integration" / "2026-11-22T022707" / "rdemo-1"
# An earlier kept run of the same task: one segment, no reset (R1 of round 9).
RDEMO_021448 = ROOT / "runs" / "runner-integration" / "2026-11-22T021448" / "rdemo-1"
# Two runs under the current contract (D-035): a finished one, and an elapsed cutoff.
RDEMO_042933 = ROOT / "runs" / "runner-integration" / "2026-11-22T042933" / "rdemo-1"
RCUTOFF = ROOT / "runs" / "runner-integration" / "2026-11-22T043203-cutoff" / "episodes" / "rcutoff-1"
TASK = ROOT / "tests" / "runner" / "fixtures" / "demo" / "task.json"


def available(run: Path = RDEMO) -> bool:
    return (run / "export" / "manifest.json").exists()


def _lines(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def rebuild(verdicts_at_as_of: bool = False, run_dir: Path = RDEMO) -> tuple[Any, bundle.Bundle]:
    """The package's replay (cohbench.trajectory.replay), with the demo task
    for runs whose own task directory was not kept."""
    return replay.rebuild(run_dir, replay.task_of(run_dir) or TASK, verdicts_at_as_of=verdicts_at_as_of)
