"""Recompute retained call and token totals without endpoint access."""

import collections
import json
from pathlib import Path

D = Path(__file__).resolve().parent.parent
R = D / "hosted-run-2026-11-23"
MODELS = ["glm-5.3-flash-uncensored", "deepseek-v4.1-flash-uncensored", "kimi-k3.1-pro"]
rows = [json.loads(line) for line in (R / "calls.jsonl").read_text().splitlines()]
starts = {r["call"]: r for r in rows if r["event"] == "start"}
ends = {r["call"]: r for r in rows if r["event"] == "finish"}
counts = {}
for model in MODELS:
    ids = [k for k, v in starts.items() if v["model"] == model]
    usage = collections.Counter()
    for k in ids:
        usage.update(ends.get(k, {}).get("usage", {}))
    counts[model] = {
        "attempts": len(ids),
        "usage": dict(usage),
        "missing_usage": sum(not ends.get(k, {}).get("usage") for k in ids),
        "request_ms": sum(ends.get(k, {}).get("ms", 0) for k in ids),
        "invalid_responses": sum(
            ends.get(k, {}).get("outcome") == "invalid_response" for k in ids
        ),
        "transport_errors": sum(
            ends.get(k, {}).get("outcome") == "transport_error" for k in ids
        ),
    }
(R / "usage-summary.json").write_text(json.dumps(counts, indent=2) + "\n")
print(json.dumps(counts, indent=2))
for model in MODELS:
    for stage in ["subset", "full"]:
        p = R / f"{model}-{stage}-agreement.json"
        if p.exists():
            rep = json.loads(p.read_text())
            print(model, stage, rep["n"], rep["kappa"], len(rep["disagreements"]))
