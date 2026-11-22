"""Compare exported scores with the generator's exact script expectations."""

import json
from pathlib import Path

from cohbench.trajectory import bundle
from cohbench.trajectory.collection import Collected


def read(path):
    return json.loads(path.read_text())


def observed(score):
    if score["control"] is not None:
        return {"pass": score["control"]["passed"]}
    parts = score["score"]
    if parts is None:
        return {}
    if score["family"] == "routing":
        return {"pass": parts["passed"]}
    if score["family"] == "explanation":
        return {
            "answers": [answer["accuracy"] for answer in parts["answers"]],
            "accuracy": parts["accuracy"],
        }
    return {
        **{key: parts[key]["value"] for key in ("g", "c", "l")},
        "score": parts["score"],
    }


def log_evidence(score, directory, expected_runs):
    """Reject a shell fallback even when its numeric score happens to agree."""
    saved = bundle.read(directory)
    records = saved.episode["collected"]
    assert saved.episode["attempt_logs"] == {"profile": "attempts"}
    assert records["attempts"]["status"] == "collected", records
    content = Collected.read(records, directory / "blobs").content("attempts")
    lines = [json.loads(line) for line in content.splitlines()]
    attempts = saved.observations.records
    logged = [a for a in attempts if a.get("source") == "log"]
    shell = [a for a in attempts if a.get("source", "shell") == "shell"]
    assert len(lines) == len(logged) == expected_runs, (len(lines), len(logged), expected_runs)
    assert saved.observations.header["malformed_logs"] == {"attempts": 0}
    counts = {"shell": len(shell), "log": len(logged)}
    assert all(a.get("log_state") is not None for a in logged)
    if "log_state_unknown" in score["attempt_observations"]:
        counts["log_state_unknown"] = 0
    assert score["attempt_observations"] == counts
    loop = score["score"]["l"] if score["score"] is not None else None
    if loop is not None:
        assert loop["log_collections"] == {"profile": "collected"}
        assert loop["observations"] == counts
        assert loop["scored_attempts"] == [a["seq"] for a in logged]
        assert loop["unplaced_attempts"] == 0
        if "unknown_log_states" in loop:
            assert loop["unknown_log_states"] == 0
    else:
        assert score["control"] is not None, "Steering was not judged"
    return {"collection": records["attempts"]["status"], "observations": counts,
            "expected_runs": expected_runs, "loop_source": "log" if loop else "not measured"}


def reset_observed(episode):
    record = read(episode / "export" / "bundle" / "episode.json")
    resets = record["resets"]
    if len(resets) != 1 or not all(
        resets[0][key] for key in ("reached", "fired", "performed")
    ) or resets[0]["reason"] is not None:
        return None
    run = read(episode / "run.json")
    at = run["events"][resets[0]["event"]]["reset"]["at"]
    return "hand_back" if at == "handback" else at


def pair_results(output):
    results = {}
    for label in ("results", "grace"):
        path = output / f"{label}-report" / "report.json"
        if not path.exists():
            continue
        report = read(path)
        for section in report["sections"]:
            for table in section["tables"]:
                for cell in table["rows"]:
                    agent = (
                        "ideal-grace" if label == "grace"
                        else "ideal" if cell["model"] == "scripted-ideal" else "worst"
                    )
                    for pair in cell["R"]["pair_rows"]:
                        results[(table["horizon"], agent, pair["seed"], pair["pair"])] = (
                            int(pair["passed"]) if pair["passed"] is not None else None
                        )
    return results


def audit(suite, output, rows):
    checks = []
    pairs = pair_results(output)
    for row in rows:
        task = read(suite / row["instance"] / "task.json")
        facts = read(suite / row["instance"] / "scripted" / "info.json")
        want = dict(facts["expected"][row["agent"]])
        if "answers" in want:
            want["accuracy"] = sum(want["answers"]) / len(want["answers"])
        grouping = task["grouping"]
        score = read(output / row["score_file"]) if row["scored"] else None
        got = observed(score) if score is not None else {}
        if row["scored"] and "reset" in want:
            got["reset"] = reset_observed(Path(row["path"]))
        if "R" in want:
            got["R"] = pairs.get((
                grouping["horizon"], row["agent"], grouping["seed"], grouping["pair"],
            ))
        got = {key: got.get(key) for key in want}
        checks.append({
            "instance": row["instance"], "agent": row["agent"],
            "family": grouping["family"], "horizon": grouping["horizon"],
            "control": grouping["control"],
            "variant": task["scoring"]["answer_key"].get("variant"),
            "expected": want, "observed": got, "matches": want == got,
            "handoff": score.get("handoff") if score is not None else None,
            "failure": row["detail"],
            "script_variant": row["script_variant"],
        })
        if grouping["family"] == "steering" and score is not None:
            checks[-1]["log"] = log_evidence(score, Path(row["path"]) / "export" / "bundle", row["profile_runs"])
    return checks


def compare_artifacts(first, second):
    def artifacts(root):
        paths = list(root.rglob("*.score.json"))
        paths += list(root.glob("*-report/report.*"))
        return {str(path.relative_to(root)): path.read_bytes() for path in paths}

    left, right = artifacts(first), artifacts(second)
    common = left.keys() & right.keys()
    different = sorted(name for name in common if left[name] != right[name])
    return {
        "score_files": sum(name.endswith(".score.json") for name in common),
        "report_files": sum(not name.endswith(".score.json") for name in common),
        "different": different,
        "missing": sorted(left.keys() ^ right.keys()),
        "identical": bool(left) and left == right,
    }
