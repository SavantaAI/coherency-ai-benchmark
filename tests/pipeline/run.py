"""Run and score the generated scripts with the simulated episode clock."""

import argparse
import contextlib
import hashlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cohbench.runner import RunConfig, load, run_episode
from cohbench.scoring.__main__ import main as scoring_main
from tests.runner.fake import FakeClock
from tests.tasks.minipi import copy_workspace

from .episode import ExportingEpisode, RecordingPi


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def run_one(instance, who, output):
    started = time.perf_counter()
    script = instance / "scripted" / f"{who}-run.json"
    policy = instance / "scripted" / f"{who}-policy.json"
    task = load(instance, {"user policy": policy, "model script": script})
    doc = read(script)
    pi = RecordingPi(doc["script"], copy_workspace(instance, output / "work"))
    clock = FakeClock()
    metric = (
        (list(task.watcher.metric.command), instance / "metric")
        if task.watcher.metric else None
    )

    def factory(spec):
        return ExportingEpisode(
            pi, output / "driver", clock, metric=metric,
            commit_delay_s=0.1, probes=doc["probes"],
        )

    opaque = "e" + hashlib.sha256(f"{instance.name}/{who}".encode()).hexdigest()[:20]
    config = RunConfig(
        episode_id=opaque, harness="pi", model="claude-haiku-5-5",
        upstream=("scripted", script), mode="default", poll_s=5.0, stall_s=3600.0,
    )
    outcome = run_episode(
        task, config, factory, ("scripted", policy), output / "episodes",
        sleep=clock.sleep, utcnow=clock.utcnow,
    )
    return {
        "instance": instance.name, "agent": who, "episode": opaque,
        "status": outcome.status, "end_reason": outcome.end_reason,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "detail": outcome.detail, "errors": pi.ep.state()["errors"],
        "path": str(outcome.path),
    }


def run_suite(suite, output, workers=8):
    started = time.perf_counter()
    suite, output = Path(suite).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = read(suite / "suite.json")
    jobs = [
        (suite / item["dir"], who)
        for item in plan["instances"]
        for who in ("ideal", "worst", "ideal-grace")
        if (suite / item["dir"] / "scripted" / f"{who}-run.json").exists()
    ]
    reader = output / "reader.json"
    write(reader, {
        "format": "cohbench-reader-config", "version": 1,
        "kind": "scripted", "rule": "overlap", "orders": {"count": 6, "seed": 0},
    })

    def execute(job):
        instance, who = job
        row = run_one(instance, who, output / "runs" / instance.name / who)
        if row["status"] == "usable":
            scores = output / ("grace" if who == "ideal-grace" else "results")
            scores.mkdir(exist_ok=True)
            score_path = scores / f"{row['episode']}.score.json"
            code = scoring_main([
                "score", str(Path(row["path"]) / "export" / "bundle"),
                "--reader", str(reader), "--out", str(score_path),
            ])
            row["scored"] = code == 0
            if code == 0:
                row["score_file"] = str(score_path.relative_to(output))
                row["episode"] = read(score_path)["episode"]
        else:
            row["scored"] = False
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(execute, jobs))
    write(output / "runs.json", rows)
    reports = {}
    for label in ("results", "grace"):
        folder = output / label
        selected = [r for r in rows if (r["agent"] == "ideal-grace") == (label == "grace")]
        write(folder / "index.json", {
            "format": "cohbench-results-index", "version": 1,
            "episodes": {
                r["episode"]: {
                    "model": "scripted-worst" if r["agent"] == "worst" else "scripted-ideal",
                    "harness": "simulated-pi",
                }
                for r in selected
            },
        })
        # Grace replaces the ordinary ideal run at h80. It is not a replicate
        # in the main table, which permits one run per instance and config.
        plan_path = suite / "suite.json"
        if label == "grace":
            plan_path = output / "grace-plan.json"
            write(plan_path, {**plan, "instances": [
                item for item in plan["instances"] if item["horizon"] == "h80"
            ]})
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = scoring_main([
                "report", str(folder), "--out", str(output / f"{label}-report"),
                "--plan", str(plan_path), "--require-plan", "--scripted-reader",
                "--resamples", "0",
            ])
        reports[label] = {"exit": code, "text": printed.getvalue()}
    write(output / "reports.json", reports)
    write(output / "timing.json", {"wall_seconds": round(time.perf_counter() - started, 3)})
    from .checks import audit

    write(output / "checks.json", audit(suite, output, rows))
    return rows, reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    rows, reports = run_suite(args.suite, args.output, args.workers)
    print(reports["results"]["text"])
    failed = [r for r in rows if not r["scored"] or r["errors"]]
    checks = read(args.output / "checks.json")
    mismatches = [check for check in checks if not check["matches"]]
    print(f"{len(rows)} episodes, {len(failed)} failed, {len(mismatches)} expectation mismatches")
    return int(bool(failed or mismatches) or any(r["exit"] for r in reports.values()))


if __name__ == "__main__":
    raise SystemExit(main())
