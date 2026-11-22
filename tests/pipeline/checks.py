"""Explicit script expectations, independent of scorer implementations."""

import json


def expected(task, who):
    ideal = who != "worst"
    family = task["grouping"]["family"]
    if task["grouping"]["control"]:
        return {"control": ideal}
    if family == "routing":
        return {"passed": ideal}
    if family == "explanation":
        # Both ideal answers lead with the cause. Both worst answers leave it
        # outside the window, including the h80 question about the lost job.
        return {"accuracy": int(ideal), "answer_1": int(ideal), "answer_2": int(ideal)}
    return {"g": int(ideal), "c": int(ideal), "l": int(ideal), "score": int(ideal)}


def observed(score):
    if score["control"] is not None:
        return {"control": score["control"]["passed"]}
    parts = score["score"]
    if score["family"] == "routing":
        return {"passed": parts["passed"]}
    if score["family"] == "explanation":
        return {
            "accuracy": parts["accuracy"],
            "answer_1": parts["answers"][0]["accuracy"],
            "answer_2": parts["answers"][1]["accuracy"],
        }
    return {**{key: parts[key]["value"] for key in ("g", "c", "l")}, "score": parts["score"]}


def audit(suite, output, rows):
    checks = []
    for row in rows:
        task = json.loads((suite / row["instance"] / "task.json").read_text())
        facts = json.loads((suite / row["instance"] / "scripted" / "info.json").read_text())
        want = expected(task, row["agent"])
        got = (
            observed(json.loads((output / row["score_file"]).read_text()))
            if row["scored"] else {key: None for key in want}
        )
        checks.append({
            "instance": row["instance"], "agent": row["agent"],
            "family": task["grouping"]["family"],
            "horizon": task["grouping"]["horizon"],
            "control": task["grouping"]["control"],
            "variant": task["scoring"]["answer_key"].get("variant"),
            "expected": want, "observed": got, "matches": want == got,
            "script_facts_family": facts["family"],
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
