"""Planned and judged task instances, shared by cells and paired comparisons."""
from __future__ import annotations

import json

FAMILIES = ("routing", "explanation", "steering")


def identity(instance):
    return json.dumps(instance, sort_keys=True, separators=(",", ":"))


def reason(d, control=False):
    if d is None:
        return "missing"
    if d.get("rerun_required"):
        return "rerun required"
    if d.get("unjudged_reason") == "no reset":
        return "no reset"
    score = (d.get("control") if control else d.get("score")) or {}
    if score.get("not_reached"):
        return "not reached"
    field = "passed" if control or d["family"] == "routing" else "accuracy" if d["family"] == "explanation" else "score"
    if score.get(field) is not None:
        return None
    if d["family"] == "explanation" and not d.get("reader") and (not control or
            any(v.get("why") == "no reader was given" for v in score.get("results", {}).values())):
        return "no reader"
    return "undefined score" if d["family"] == "steering" and not control else "unjudged reply"


def collect(eps, planned, *, control=False, pooled=False, no_conflict=False):
    actual = {}
    for d in eps:
        inst = dict(d["task"]["instance"])
        if pooled:
            inst["mode"] = d["grouping"]["mode"]
        key = identity(inst)
        if key not in actual or actual[key].get("rerun_required"):
            actual[key] = d
    result = {f: {} for f in FAMILIES}
    grouped = {}
    for key in sorted(planned):
        inst = json.loads(key)
        if bool(inst.get("control")) != control:
            continue
        f = inst["family"]
        if no_conflict:
            if f != "routing" or inst.get("pair") is not None:
                continue
        elif not control and f == "routing" and inst.get("pair") is None:
            continue
        unit = dict(inst)
        if f == "routing" and not control and not no_conflict:
            unit.pop("sibling", None)
        grouped.setdefault(identity(unit), []).append(key)
    for key, keys in sorted(grouped.items()):
        inst = json.loads(key)
        docs = [actual.get(k) for k in keys]
        reasons = [{"instance": json.loads(k), "reason": why, "episodes": [d["episode"]] if d else []}
                   for k, d in zip(keys, docs) if (why := reason(d, control)) is not None]
        if inst["family"] == "routing" and not control and not no_conflict:
            if {json.loads(k).get("sibling") for k in keys} != {"A", "B"}:
                reasons.append({"instance": inst, "reason": "missing", "episodes": []})
            value = None if reasons else float(all(d["score"]["passed"] for d in docs))
        else:
            field = "passed" if control or no_conflict else "accuracy" if inst["family"] == "explanation" else "score"
            value = None if reasons else float(docs[0]["control" if control else "score"][field])
        result[inst["family"]][key] = {"value": value, "seed": inst["seed"], "reasons": reasons,
                                      "episodes": sorted(d["episode"] for d in docs if d)}
    return result


def coverage(items, bar):
    judged = sorted(k for k, v in items.items() if v["value"] is not None)
    missing = [{"id": k, **items[k]} for k in sorted(items) if items[k]["value"] is None]
    official = bool(items) and len(judged) / len(items) >= bar
    return {"judged": len(judged), "planned": len(items), "judged_instances": judged,
            "unjudged_instances": missing, "official": official, "partial": not official}


def control_rates(items):
    out = {}
    for f, rows in items.items():
        cov = coverage(rows, 1)
        vals = [v["value"] for v in rows.values() if v["value"] is not None]
        out[f] = {**cov, "passed": sum(vals), "not_judged": len(rows) - len(vals),
                  "rate": sum(vals) / len(vals) if vals else None,
                  "episodes": sorted(e for v in rows.values() for e in v["episodes"])}
    return out
