"""Compare exported scores with the generator's exact script expectations."""

import json
from pathlib import Path


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
        })
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
