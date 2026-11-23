"""Aggregate private run outputs without publishing answer text."""

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
from cohbench.scoring.stats import near_chance


def smoke_bound(correct, n, items, name):
    return near_chance(correct, n, 1 / 6, 2, name == "no_answer", clusters=items)


def read(path):
    return json.loads(path.read_text())


def ratio(votes, field="correct"):
    return {"correct": sum(v[field] for v in votes), "n": len(votes)}


def orders(votes, field="correct"):
    return [ratio([v for v in votes if v["order"] == i], field) for i in range(6)]


def summarize(root):
    ledger = [
        json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    starts = {r["call"]: r for r in ledger if r["event"] == "start"}
    finishes = {r["call"]: r for r in ledger if r["event"] == "finish"}
    output = {
        "date": "2026-11-23",
        "cap": 2500,
        "reserved_calls": len(starts),
        "models": {},
    }
    assert len(starts) == len(finishes), "unfinished call accounting"
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder / "harder.json").exists():
            continue
        model = folder.name
        pipeline = read(folder / "pipeline.json")
        assert len(pipeline) == 48, (
            "expected both agents on all 24 Explanation instances"
        )
        result = {"pipeline": {}, "harder": {}}
        for subset in ["all", "non_control", "control"]:
            selected = [
                r
                for r in pipeline
                if subset == "all"
                or r["score"]["grouping"]["control"] == (subset == "control")
            ]
            section = {}
            for agent in ["ideal", "worst"]:
                answers = [
                    (r, a)
                    for r in selected
                    if r["agent"] == agent
                    for a in r["explanation"]["answers"]
                ]
                assert all(a["given"] for r, a in answers), "missing scripted answer"
                votes = [v for r, a in answers for v in a["items"]]
                section[agent] = {
                    **ratio(votes),
                    "answers": len(answers),
                    "orders": orders(votes),
                    "unexpected": [
                        {
                            "instance": r["instance"],
                            "answer": a["which"],
                            "orders": [
                                v["order"]
                                for v in a["items"]
                                if v["correct"] != (agent == "ideal")
                            ],
                        }
                        for r, a in answers
                        if any(v["correct"] != (agent == "ideal") for v in a["items"])
                    ],
                }
            for name in ["no_answer", "buried"]:
                unique = {
                    r["explanation"]["sanity"][name]["key"]: r["explanation"]["sanity"][
                        name
                    ]
                    for r in selected
                }
                votes = [v for item in unique.values() for v in item["items"]]
                section[name] = {
                    **ratio(votes),
                    "items": len(unique),
                    "orders": orders(votes),
                }
                result_bound = smoke_bound(
                    section[name]["correct"], section[name]["n"], len(unique), name
                )
                section[name]["bound"] = result_bound["bound"]
                section[name]["passed"] = result_bound["passed"]
            result["pipeline"][subset] = section
        harder = read(folder / "harder.json")
        assert len(harder) == 40 and len({r["id"] for r in harder}) == 40
        for group in sorted({r["group"] for r in harder}):
            selected = [r for r in harder if r["group"] == group]
            assert len(selected) == 10
            votes = [
                {**v, "interpreted": v["chose"] == r["expected"]}
                for r in selected
                for v in r["votes"]
            ]
            result["harder"][group] = {
                "attribution": ratio(votes, "interpreted"),
                "true_cause": ratio(votes),
                "orders": orders(votes, "interpreted"),
                "misses": [
                    {
                        "id": r["id"],
                        "expected_option": r["expected"],
                        "votes": [
                            {"order": v["order"], "chosen_option": v["chose"]}
                            for v in r["votes"]
                            if v["chose"] != r["expected"]
                        ],
                    }
                    for r in selected
                    if any(v["chose"] != r["expected"] for v in r["votes"])
                ],
            }
        tickets = [k for k, r in starts.items() if r["model"] == model]
        measured = [finishes[k] for k in tickets if k in finishes]
        durations = sorted(r["ms"] for r in measured)
        cache = [
            json.loads(line)
            for line in (folder / "cache.jsonl").read_text().splitlines()
        ]
        assert len({r["reader_digest"] for r in cache}) == 1, "mixed reader configs"
        assert len({r["request_digest"] for r in cache}) == len(cache), (
            "duplicate paid verdicts"
        )
        latencies = sorted(r["response"]["ms"] for r in cache)
        result["accounting"] = {
            "attempts": len(tickets),
            "completed": len(measured),
            "outcomes": dict(Counter(r["outcome"] for r in measured)),
            "tokens": dict(sum((Counter(r["usage"]) for r in measured), Counter())),
            "unmeasured_usage_calls": [
                k for k in tickets if not finishes.get(k, {}).get("usage")
            ],
            "accepted_choices": len(cache),
            "choice_latency_ms": {
                "mean": statistics.mean(latencies),
                "median": statistics.median(latencies),
                "p95": latencies[(len(latencies) * 95 + 99) // 100 - 1],
                "max": max(latencies),
            },
            "attempt_latency_ms": {
                "mean": statistics.mean(durations),
                "max": max(durations),
            },
        }
        result["phases"] = {}
        for phase, rows in [("generated", pipeline), ("authored", harder)]:
            keys = {key for row in rows for key in row["requests"]}
            responses = [r["response"] for r in cache if r["request_digest"] in keys]
            times = sorted(r["ms"] for r in responses)
            result["phases"][phase] = {
                "accepted_choices": len(responses),
                "attempts": sum(r["attempts"] for r in responses),
                "tokens": dict(
                    sum((Counter(r["usage"]) for r in responses), Counter())
                ),
                "latency_ms": {
                    "mean": statistics.mean(times),
                    "median": statistics.median(times),
                    "p95": times[(len(times) * 95 + 99) // 100 - 1],
                    "max": max(times),
                },
            }
        assert len(cache) == result["accounting"]["outcomes"]["accepted"]
        result["config_sha256"] = cache[0]["reader_digest"]
        output["models"][model] = result
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.write_text(
        json.dumps(summarize(args.root), indent=2, sort_keys=True) + "\n"
    )
