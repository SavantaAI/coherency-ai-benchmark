"""Run five suite scripts on Docker after a successful offline self check."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from cohbench.runner import RunConfig, load, run_episode
from cohbench.runner.adapter import open_episode
from cohbench.scoring.__main__ import main as scoring_main
from cohbench.trajectory import bundle

from .checks import log_evidence, observed, reset_observed
from .run import read, write
from .variants import script_runs

INSTANCES = (
    ("routing-h20-s1-B", "ideal"),
    ("explanation-h20-s1-single", "ideal"),
    ("steering-h20-s1-single", "ideal"),
    ("routing-h80-s1-A", "ideal"),
    ("steering-h80-s1-single", "worst"),
)


def check_reference(reference):
    assert read(reference.parent / "determinism.json")["identical"], "offline reruns differ"
    checks = read(reference / "checks.json")
    assert len(checks) == 268 and all(row["matches"] for row in checks), "offline expectations failed"
    steering = [row for row in checks if row["family"] == "steering"]
    assert len(steering) == 108
    assert all(row["log"]["observations"]["log"] == row["log"]["expected_runs"] for row in steering)
    runs = read(reference / "runs.json")
    assert all(row["status"] == "usable" and row["scored"] and not row["errors"] for row in runs)
    reports = read(reference / "reports.json")
    assert all(report["exit"] == 0 for report in reports.values())
    for label in reports:
        report = read(reference / f"{label}-report" / "report.json")
        for section in report["sections"]:
            for table in section["tables"]:
                for cell in table["rows"]:
                    expected = 100 if cell["model"] == "scripted-ideal" else 5
                    assert abs(cell["headline"]["value"] - expected) < 0.000001
    return {(row["instance"], row["agent"]): row for row in runs if not row["script_variant"]}


def run_live(suite, reference, output):
    suite, reference, output = (Path(path).resolve() for path in (suite, reference, output))
    references = check_reference(reference)
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for name, agent in INSTANCES:
        instance = suite / name
        script = instance / "scripted" / f"{agent}-run.json"
        policy = instance / "scripted" / f"{agent}-policy.json"
        task = load(instance, {"user policy": policy, "model script": script})
        baseline = read(reference / references[(name, agent)]["score_file"])
        expected = read(instance / "scripted" / "info.json")["expected"][agent]
        assert baseline["task"]["sha256"] == task.digest, "live task differs from the offline instance"
        identity = {
            "task": task.digest, "script": read(script), "policy": read(policy),
            "harness": "pi", "execution": "docker", "mode": "default", "poll_s": 5.0,
            "model": "claude-haiku-5-5",
        }
        opaque = "e" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        config = RunConfig(
            episode_id=opaque, scorer_id=opaque, harness="pi", model="claude-haiku-5-5",
            upstream=("scripted", script), mode="default", poll_s=5.0,
            stall_s=3600.0, awareness_timeout_s=180.0,
        )
        print(f"Starting {name}", flush=True)
        started = time.perf_counter()
        outcome = run_episode(task, config, open_episode, ("scripted", policy), output / name)
        row = {
            "instance": name, "agent": agent, "episode": str(outcome.path),
            "wall_seconds": round(time.perf_counter() - started, 3),
            "status": outcome.status, "end_reason": outcome.end_reason,
            "failure": outcome.failure, "detail": outcome.detail,
            "expected": observed(baseline), "observed": None,
            "validated": False, "rebuild_exit": None, "matches": False,
        }
        if outcome.status == "usable":
            script_state = read(outcome.path / "export" / "scripted" / "state.json")
            row["script_errors"] = script_state["errors"]
            check = bundle.validate(bundle.read(outcome.path / "export" / "bundle"))
            row["validated"] = check["ok"]
            row["validation_errors"] = check["errors"]
            verify_started = time.perf_counter()
            verify = subprocess.run(
                ["uv", "run", "--isolated", "--python", "3.12", "python", "-m",
                 "cohbench.trajectory", "verify-episode", str(outcome.path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=300,
            )
            row["rebuild_exit"] = verify.returncode
            row["rebuild_seconds"] = round(time.perf_counter() - verify_started, 3)
            (outcome.path / "verify.json").write_text(verify.stdout)
            score_file = outcome.path / "result.score.json"
            code = scoring_main([
                "score", str(outcome.path / "export" / "bundle"),
                "--reader", str(reference / "reader.json"),
                "--reader-cache", str(output / "reader-cache.jsonl"),
                "--out", str(score_file),
            ])
            row["score_exit"] = code
            if code == 0:
                score = read(score_file)
                row["observed"] = observed(score)
                row["handoff"] = score.get("handoff")
                if task.grouping.family == "steering":
                    row["log"] = log_evidence(score, outcome.path / "export" / "bundle", script_runs(read(script), script_state))
                    row["authored_expected"] = {key: value for key, value in expected.items() if key != "reset"}
                    assert row["observed"] == row["authored_expected"], row
                if task.grouping.horizon == "h80":
                    row["reset"] = reset_observed(outcome.path)
                    row["reset_expected"] = expected["reset"]
                row["matches"] = (
                    row["observed"] == row["expected"]
                    and row.get("reset") == row.get("reset_expected")
                    and row["validated"] and verify.returncode == 0 and not row["script_errors"]
                    and outcome.end_reason == "finished"
                )
        results.append(row)
        write(output / "results.json", results)
        print(json.dumps(row, sort_keys=True), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = run_live(args.suite, args.reference, args.output)
    return int(not all(row["matches"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
