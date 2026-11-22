"""Run a generated instance's scripted agent through the runner, with the
runner's fake episode and `MiniPi` (minipi.py), and collect what the tests
look at."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cohbench.runner import RunConfig, load, run_episode

from tests.runner.fake import FakeClock

from .minipi import MiniPi, ScriptedEpisode, copy_workspace


@dataclass
class Result:
    status: str
    end_reason: str | None
    failure: str | None
    detail: str | None
    dir: Path
    verdicts: list[dict[str, Any]]  # classifier.jsonl rows with a label
    transcript: list[dict[str, Any]]
    record: dict[str, Any] | None
    run: dict[str, Any]
    events: list[dict[str, Any]]
    pi: MiniPi
    fake: ScriptedEpisode
    tree: dict[str, str]  # the final workspace

    def labels(self) -> list[str]:
        return [v["label"] for v in self.verdicts]

    def agent_texts(self) -> list[str]:
        return [r["text"] for r in self.transcript if r.get("role") == "agent"]

    def errors(self) -> list[Any]:
        return self.pi.ep.state()["errors"]


def run_script(instance: Path, who: str, tmp: Path, *, poll_s: float = 5.0) -> Result:
    task = load(instance)
    script_file = instance / "scripted" / f"{who}-run.json"
    policy_file = instance / "scripted" / f"{who}-policy.json"
    doc = json.loads(script_file.read_text())
    work = copy_workspace(instance, tmp / "work")
    pi = MiniPi(doc["script"], work)
    clock = FakeClock()
    metric = (list(task.watcher.metric.command), instance / "metric") if task.watcher.metric else None
    holder: dict[str, ScriptedEpisode] = {}

    def factory(spec: Any) -> ScriptedEpisode:
        holder["ep"] = ScriptedEpisode(pi, tmp / "driver", clock, metric=metric, commit_delay_s=0.1)
        return holder["ep"]

    cfg = RunConfig(episode_id="ep-1", harness="pi", model="claude-haiku-5-5", upstream=("scripted", script_file), mode="default",
                    poll_s=poll_s, stall_s=3600.0)
    out = run_episode(task, cfg, factory, ("scripted", policy_file), tmp / "out", sleep=clock.sleep, utcnow=clock.utcnow)
    d = out.path
    rows = [json.loads(x) for x in (d / "classifier.jsonl").read_text().splitlines() if x.strip()] if (d / "classifier.jsonl").exists() else []
    verdicts = [r for r in rows if "label" in r and r.get("kind") != "awareness" and "outcome" not in r]
    transcript = [json.loads(x) for x in (d / "transcript.jsonl").read_text().splitlines() if x.strip()] if (d / "transcript.jsonl").exists() else []
    rec = json.loads((d / "episode-record.json").read_text()) if (d / "episode-record.json").exists() else None
    run = json.loads((d / "run.json").read_text())
    events = [json.loads(x) for x in (d / "runner" / "events.jsonl").read_text().splitlines() if x.strip()]
    return Result(out.status, out.end_reason, out.failure, out.detail, d, verdicts, transcript, rec, run, events, pi, holder["ep"], dict(pi.tree))
