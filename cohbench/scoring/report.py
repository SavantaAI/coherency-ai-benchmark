"""The results report over many scored episodes.

Inputs: a results directory holding `index.json` (opaque episode id ->
configuration) and score files (`*.score.json`, anywhere below it), written
by `python -m cohbench.scoring score`. The scorers never see the index: it is
joined here, after scoring.

Before anything is computed the report refuses inputs it cannot compare
honestly: a scored episode missing from the index, a score file without a
task identity, one task instance that ran as two different tasks, two scored
episodes of one configuration, mode and task instance, Explanation scores
from more than one reader config, or scripted-reader scores without
`scripted_reader=True`. Episodes whose polling interval (`poll_s`, D-040)
differs are never put in one table: each value gets its own section.

Every comparison pairs exactly the task instances judged by all members.
Its common-set coverage must meet the judged bar in every needed family.
Control eligibility requires all planned controls to be judged. Reader
sanity is computed once across the report, while trial comparisons stay
inside polling sections.

Output: one JSON document with every number and the episodes behind each,
rendered as a plain text table and as Markdown.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from . import stats, measures
from .rng import stream
from .score import dumps as score_dumps
from .score import tidy

INDEX_FORMAT = "cohbench-results-index"
FORMAT = "cohbench-report"
VERSION = 1
FAMILIES = ("routing", "explanation", "steering")
NM = "not measured"
WITHHELD = "withheld: reader failed the sanity check"
SCRIPTED = "scripted reader, not a result"
SANITY_Z = 2.0
CHANCE = 1 / 6
RELEASE_PYTHON = "3.12"


class ReportError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def read_index(path: Path, require_plan: bool = False) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"the results index cannot be read: {type(exc).__name__}") from None
    if not isinstance(doc, dict) or doc.get("format") != INDEX_FORMAT or doc.get("version") != 1 or not isinstance(doc.get("episodes"), dict):
        raise ReportError(f"the results index is {{format: {INDEX_FORMAT}, version: 1, episodes: {{id: configuration}}}}")
    for eid, c in doc["episodes"].items():
        if not (isinstance(c, dict) and isinstance(c.get("model"), str) and c["model"] and isinstance(c.get("harness"), str) and c["harness"]
                and all((k == "settings" and isinstance(v, dict)) or (isinstance(v, (str, int, float)) and not isinstance(v, bool)) for k, v in c.items())):
            raise ReportError(f"index entry {eid}: a configuration names model and harness; settings is an object, and other values are strings or numbers")
        if require_plan:
            missing = [k for k in ("model", "harness", "harness_version", "driver_version", "settings") if k not in c or c[k] == "" or (k == "settings" and not isinstance(c[k], dict))]
            if missing:
                raise ReportError(f"index entry {eid} ({c['model']} in {c['harness']}): required plan configuration lacks {', '.join(missing)}")
    return doc["episodes"]


def read_scores(root: Path) -> list[dict[str, Any]]:
    out, seen = [], {}
    for p in sorted(root.rglob("*.score.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("format") != "cohbench-score":
            raise ReportError(f"{p.relative_to(root)} is not a score file")
        if doc["episode"] in seen:
            raise ReportError(f"episode {doc['episode']} is scored twice ({seen[doc['episode']]} and {p.relative_to(root)})")
        seen[doc["episode"]] = str(p.relative_to(root))
        runtime = doc.get("python_version")
        if runtime is not None and (not isinstance(runtime, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", runtime)):
            raise ReportError(f"episode {doc['episode']}: python_version must name a major and minor version")
        score = doc.get("score") or {}
        if doc.get("family") == "routing" and score.get("not_reached"):
            # The reach diagnostic also makes older score files readable
            # under the current rule: a missed conflict is a failed trial.
            score["passed"] = False
        out.append(doc)
    return sorted(out, key=lambda d: d["episode"])


def config_key(c: dict[str, Any]) -> str:
    return json.dumps(c, sort_keys=True, separators=(",", ":"))


def harness_label(c: dict[str, Any]) -> str:
    extra = [f"{k}={config_key(c[k]) if isinstance(c[k], dict) else c[k]}" for k in sorted(c) if k not in ("model", "harness")]
    return c["harness"] + (f" ({', '.join(extra)})" if extra else "")


def column_key(c):
    return config_key({k: v for k, v in c.items() if k != "model"})


def column_labels(configs):
    labels = {column_key(c): harness_label(c) for c in configs.values()}
    return {k: v + (" [" + hashlib.sha256(k.encode()).hexdigest()[:10] + "]" if list(labels.values()).count(v) > 1 else "")
            for k, v in labels.items()}


def read_plan(path, scores):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc["format"] != "cohbench-task-suite" or doc["version"] != 1 or not isinstance(doc["instances"], list):
            raise ValueError
        entries = {}
        for row in doc["instances"]:
            inst = {k: row.get(k) for k in ("family", "seed", "pair", "sibling", "control", "horizon")}
            if inst["family"] not in FAMILIES or not isinstance(inst["seed"], int) or not isinstance(inst["horizon"], str):
                raise ValueError
            if not isinstance(row["digest"], str) or row["digest"] in entries:
                raise ValueError
            entries[row["digest"]] = inst
    except (OSError, ValueError, KeyError, TypeError):
        raise ReportError("the plan must be a version 1 task suite index with distinct task digests") from None
    for d in scores:
        if d["task"]["sha256"] not in entries:
            raise ReportError(f"episode {d['episode']} has a task digest absent from the plan")
        if d["task"]["instance"] != entries[d["task"]["sha256"]]:
            raise ReportError(f"episode {d['episode']} has grouping different from its plan entry")
    return list(entries.values())


def natural(s: str) -> tuple:
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s) if t)


def instance_key(d: dict[str, Any], drop: tuple[str, ...] = ()) -> str:
    return json.dumps({k: v for k, v in d["task"]["instance"].items() if k not in drop}, sort_keys=True, separators=(",", ":"))


def rerun(d: dict[str, Any]) -> bool:
    return bool(d.get("rerun_required"))


def check_inputs(scores: list[dict[str, Any]], index: dict[str, dict[str, Any]], scripted_reader: bool) -> dict[str, Any] | None:
    """Every refusal of the report, before anything is computed. Returns the
    one reader that read the report's Explanation scores, or None."""
    missing = sorted(d["episode"] for d in scores if d["episode"] not in index)
    if missing:
        raise ReportError(f"{len(missing)} scored episode(s) are not in the index, the first {missing[0]}")
    tasks: dict[str, dict[str, list[str]]] = {}
    for d in scores:
        if not isinstance(d.get("task"), dict) or not isinstance(d["task"].get("sha256"), str):
            raise ReportError(f"score file of episode {d['episode']} has no task identity; score it again with this version")
        tasks.setdefault(instance_key(d), {}).setdefault(d["task"]["sha256"], []).append(d["episode"])
    for inst, digests in sorted(tasks.items()):
        if len(digests) > 1:
            parts = "; ".join(f"{dg[:12]} in {', '.join(sorted({index[e]['model'] + ' in ' + harness_label(index[e]) for e in eps}))}"
                              for dg, eps in sorted(digests.items()))
            raise ReportError(f"task instance {inst} ran as {len(digests)} different tasks ({parts}); "
                              "the configurations did not face the same suite, so no table can compare them")
    trial: dict[tuple[str, str, str], str] = {}
    for d in sorted(scores, key=lambda x: x["episode"]):
        if rerun(d):
            continue
        k = (config_key(index[d["episode"]]), d["grouping"]["mode"], instance_key(d))
        if k in trial:
            raise ReportError(f"episodes {trial[k]} and {d['episode']} are two scored runs of task instance {k[2]} "
                              f"in one configuration and mode; version 0 plans one, and the index declares no replicates")
        trial[k] = d["episode"]
    choices = {}
    for d in scores:
        for item in d.get("reader_requests", []):
            digest = (d.get("reader") or {}).get("config_sha256")
            if item.get("reader_digest") != digest:
                raise ReportError(f"episode {d['episode']} has a reader request with a different reader digest")
            key = (digest, item["request_digest"])
            if key in choices and choices[key][0] != item["choice"]:
                raise ReportError(f"reader disagreed on request {key[1]} in episodes {choices[key][1]} and {d['episode']}")
            choices[key] = (item["choice"], d["episode"])
    readers = {d["reader"]["config_sha256"]: d["reader"] for d in scores if d.get("reader")}
    if len(readers) > 1:
        raise ReportError(f"the scores in this report were read by {len(readers)} reader configs ({', '.join(sorted(k[:12] for k in readers))}); "
                          "a report uses one reader")
    reader = next(iter(readers.values()), None)
    if reader is not None and reader.get("kind") == "scripted" and not scripted_reader:
        raise ReportError("the Explanation scores were read by the scripted reader; pass --scripted-reader to produce a report marked as not a result")
    return reader


# ---------------------------------------------------------------------------
# Units and sub-scores
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _rate(xs: list[bool | None]) -> dict[str, Any]:
    j = [x for x in xs if x is not None]
    return {"passed": sum(1 for x in j if x), "judged": len(j), "not_judged": len(xs) - len(j),
            "rate": (sum(1 for x in j if x) / len(j)) if j else None}


def complete(d: dict[str, Any]) -> bool:
    """A scored run counts for its task instance; a rerun-required one does
    not, nor does an Explanation episode with no reader result."""
    if rerun(d) or d.get("unjudged_reason"):
        return False
    return not (d["family"] == "explanation" and not d["grouping"].get("control") and (d.get("score") or {}).get("accuracy") is None)


def units(eps: list[dict[str, Any]]) -> tuple[dict[str, dict[int, list]], dict[str, Any]]:
    """The bootstrap units of one cell, by family and seed, and what they were made from."""
    u: dict[str, dict[int, list]] = {f: {} for f in FAMILIES}
    detail: dict[str, Any] = {"pairs": [], "incomplete_pairs": [], "unpaired": [], "not_reached": [], "explanation": [], "steering": [],
                              "not_scored": [], "no_reader": [], "no_reset": []}
    pairs: dict[tuple[int, str], dict[str, list[dict[str, Any]]]] = {}
    for d in eps:
        g, s = d["grouping"], d["score"]
        if d.get("unjudged_reason") in ("no reset", "no reset scheduled"):
            detail["no_reset"].append(d["episode"])
            continue
        if g.get("control") or rerun(d):
            continue
        if d["family"] == "routing":
            if s is None or s.get("kind") != "sibling":
                continue
            if g.get("pair") is None:
                detail["unpaired"].append(d["episode"])
                continue
            pairs.setdefault((g["seed"], g["pair"]), {"A": [], "B": []}).setdefault(s["sibling"], []).append(d)
        elif d["family"] == "explanation":
            if s is None or s.get("accuracy") is None:
                detail["no_reader"].append(d["episode"])
                continue
            u["explanation"].setdefault(g["seed"], []).append(s["accuracy"])
            detail["explanation"].append(d["episode"])
        elif d["family"] == "steering":
            if s is None or s.get("score") is None:
                detail["not_scored"].append(d["episode"])
                continue
            u["steering"].setdefault(g["seed"], []).append(s["score"])
            detail["steering"].append(d["episode"])
    for (seed, pid), sides in sorted(pairs.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        eids = sorted(x["episode"] for side in ("A", "B") for x in sides[side])
        if len(sides["A"]) != 1 or len(sides["B"]) != 1:
            detail["incomplete_pairs"].append({"seed": seed, "pair": pid, "episodes": eids})
            continue
        if any(x["score"].get("not_reached") for side in ("A", "B") for x in sides[side]):
            detail["not_reached"].append({"seed": seed, "pair": pid, "episodes": eids,
                                          "not_reached": sorted(x["episode"] for side in ("A", "B") for x in sides[side] if x["score"].get("not_reached"))})
        a, b = sides["A"][0]["score"]["passed"], sides["B"][0]["score"]["passed"]
        row = {"seed": seed, "pair": pid, "episodes": sorted([sides["A"][0]["episode"], sides["B"][0]["episode"]]), "A": a, "B": b}
        if a is None or b is None:
            row["passed"] = None
            detail["incomplete_pairs"].append({**row, "why": "a sibling was not judged"})
            continue
        row["passed"] = bool(a and b)
        detail["pairs"].append(row)
        u["routing"].setdefault(seed, []).append(row["passed"])
    return u, detail


def subscores(draw: dict[str, list[list]]) -> dict[str, float | None]:
    flat = {f: [x for unit in draw.get(f, []) for x in unit] for f in FAMILIES}
    r = (sum(1 for x in flat["routing"] if x) / len(flat["routing"])) if flat["routing"] else None
    e = _mean(flat["explanation"])
    s = _mean(flat["steering"])
    return {"R": r, "E": e, "S": s, "H": stats.headline(r, e, s)}


def paired(us: list[dict[str, dict[int, list]]], tag: tuple, params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Point values and bootstrap draws for several cells on the seeds all of
    them ran: each draw resamples those seeds once per family (a seed carries
    its sibling pairs whole) and evaluates every member on the same draw."""
    seeds = {f: sorted(set.intersection(*(set(u[f]) for u in us))) for f in FAMILIES}
    points = [subscores({f: [u[f][s] for s in seeds[f]] for f in FAMILIES}) for u in us]
    rng = stream("paired", params["seed"], *tag)
    draws = []
    for _ in range(params["resamples"]):
        drawn = {f: (stats.resample(rng, seeds[f]) if seeds[f] else []) for f in FAMILIES}
        draws.append([subscores({f: [u[f][s] for s in drawn[f]] for f in FAMILIES}) for u in us])
    return points, draws


# ---------------------------------------------------------------------------
# Eligibility, sanity
# ---------------------------------------------------------------------------

def control_rates(eps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ctl: dict[str, list] = {f: [] for f in FAMILIES}
    ctl_eps: dict[str, list] = {f: [] for f in FAMILIES}
    for d in eps:
        if d["grouping"].get("control") and not rerun(d):
            ctl[d["family"]].append(d["control"]["passed"] if d.get("control") else None)
            ctl_eps[d["family"]].append(d["episode"])
    return {f: {**_rate(ctl[f]), "episodes": sorted(ctl_eps[f])} for f in FAMILIES}


def pooled_eligibility(eps: list[dict[str, Any]], bar: float, planned) -> dict[str, Any]:
    """Eligibility of a model in a harness (one configuration): each family's
    controls pooled over every horizon and mode, against the bar."""
    controls = measures.control_rates(measures.collect(eps, planned, control=True, pooled=True))
    elig = {f: {"eligible": controls[f]["official"] and controls[f]["rate"] >= bar,
                "rate": controls[f]["rate"], "status": "established" if controls[f]["official"] else "not established",
                "why": None if controls[f]["official"] else "not established: planned controls are unjudged",
                "unjudged_controls": controls[f]["unjudged_instances"]} for f in FAMILIES}
    complete = all(controls[f]["official"] for f in FAMILIES)
    return {"controls": controls, "eligibility": elig,
            "capability": sum(controls[f]["rate"] for f in FAMILIES) / 3 if complete else None,
            "capability_status": "complete" if complete else "incomplete"}


def reader_sanity(scores: list[dict[str, Any]], z: float) -> dict[str, dict[str, Any]]:
    """Per reader config, the two sanity checks over distinct items: one
    no-answer item and one buried-cause item per Explanation task instance,
    and identical inputs counted once (the first by episode id). An item's
    orders are not independent draws, so the standard error counts items,
    not orders: under chance an item is right with probability 1/6, and
    se = sqrt((1/6)(5/6)/m) for m items."""
    acc: dict[str, dict[str, Any]] = {}
    for d in sorted(scores, key=lambda x: x["episode"]):
        s = d.get("score")
        if rerun(d) or d["family"] != "explanation" or not s or not s.get("sanity") or not d.get("reader"):
            continue
        k = d["reader"]["config_sha256"]
        a = acc.setdefault(k, {"reader": d["reader"], "no_answer": {}, "buried": {}, "episodes": []})
        for part in ("no_answer", "buried"):
            item = s["sanity"][part]
            a[part].setdefault(item.get("key") or d["task"]["sha256"], (item["correct"], item["n"]))
        a["episodes"].append(d["episode"])
    out = {}
    for k, a in sorted(acc.items()):
        res = {}
        for part, two in (("no_answer", True), ("buried", False)):
            items = a[part]
            correct, n = sum(c for c, _ in items.values()), sum(n for _, n in items.values())
            res[part] = {**stats.near_chance(correct, n, CHANCE, z, two_sided=two, clusters=len(items)), "items": len(items), "correct": correct}
        out[k] = {"reader": a["reader"], **res, "passed": bool(res["no_answer"]["passed"] and res["buried"]["passed"]), "episodes": sorted(a["episodes"])}
    return out


# ---------------------------------------------------------------------------
# One cell: a configuration at one horizon and one mode
# ---------------------------------------------------------------------------

def cell(eps: list[dict[str, Any]], reruns: list[dict[str, Any]], key: tuple, params: dict[str, Any], pooled: dict[str, Any],
         withheld: bool, reader: dict[str, Any] | None) -> dict[str, Any]:
    u, detail = units(eps)
    point = subscores({f: list(u[f].values()) for f in FAMILIES})
    rng = stream("bootstrap", params["seed"], *key)
    seed_units = {f: [u[f][s] for s in sorted(u[f])] for f in FAMILIES}
    draws = stats.bootstrap(seed_units, subscores, rng, params["resamples"]) if params["resamples"] > 0 else []
    ci = {k: (stats.interval([d[k] for d in draws], params["level"]) if point[k] is not None and draws else None) for k in ("R", "E", "S", "H")}

    nocon, nocon_eps = [], []
    for d in eps:
        if not d["grouping"].get("control") and d["family"] == "routing" and d["score"] is not None and d["score"].get("kind") == "no_conflict":
            nocon.append(d["score"]["passed"])
            nocon_eps.append(d["episode"])
    elig, capability, pooled_controls = pooled["eligibility"], pooled["capability"], pooled["controls"]

    routing_eps = [d for d in eps if d["family"] == "routing" and not d["grouping"].get("control")]
    rep = [d["reported"] for d in routing_eps]
    shares = [x["wasted_wait"]["share"] for x in rep if x["wasted_wait"]["share"] is not None]
    priv = [x["private_identifiers_sent"] for x in rep if isinstance(x["private_identifiers_sent"], int)]
    reported = {
        "routing_episodes": len(rep),
        "unanswerable_per_episode": _mean([x["unanswerable_questions"] for x in rep]),
        "private_identifiers_per_episode": _mean(priv),
        "wasted_wait_share": _mean(shares),
        "wasted_wait_share_undefined": len(rep) - len(shares),
        "episodes": sorted(d["episode"] for d in routing_eps),
    }
    events: dict[str, dict[str, Any]] = {}
    for d in eps:
        r = d["reported"]["reach"]
        for name in r["planted"]:
            e = events.setdefault(name, {"scheduled": 0, "fired": 0, "required": False, "not_fired": []})
            e["scheduled"] += 1
            e["required"] = e["required"] or name in r.get("required", [])
            if name in r["fired"]:
                e["fired"] += 1
            else:
                e["not_fired"].append(d["episode"])
    for e in events.values():
        e["not_fired"].sort()
        e["share"] = e["fired"] / e["scheduled"]
    all_req = [d["reported"]["reach"]["status"] == "reached" for d in eps]
    undelivered = sorted(d["episode"] for d in eps if d["family"] == "steering" and d.get("score")
                         and d["score"].get("correction", {}).get("scheduled") and not d["score"]["correction"].get("delivered"))
    probe = {"knew_right": 0, "knew_wrong": 0, "not_knew_right": 0, "not_knew_wrong": 0, "not_judged": 0}
    for d in eps:
        for p in d["probes"]:
            if p["knew"] is None or p["acted_right"] is None:
                probe["not_judged"] += 1
            else:
                probe[("knew_" if p["knew"] else "not_knew_") + ("right" if p["acted_right"] else "wrong")] += 1
    aw = {"suspected": 0, "not_suspected": 0, "unclear": 0, "asked_no_outcome": 0, "not_asked": 0}
    for d in eps:
        a = d["reported"].get("awareness") or {}
        if not a.get("asked"):
            aw["not_asked"] += 1
        elif a.get("outcome") is None:
            aw["asked_no_outcome"] += 1
        else:
            aw[a["outcome"]] += 1
    judged = aw["suspected"] + aw["not_suspected"] + aw["unclear"]
    aw["share_suspected"] = aw["suspected"] / judged if judged else None

    unranked = [f"eligibility not established in {f}: planned controls are unjudged" if elig[f]["status"] == "not established"
                else f"not eligible in {f} (pooled {_ratefmt(pooled_controls[f])})" for f in FAMILIES if not elig[f]["eligible"]]
    if withheld:
        unranked.append(WITHHELD)
    elif point["H"] is None:
        unranked.append("the headline is not measured")
    scripted = bool(reader and reader.get("kind") == "scripted" and detail["explanation"])
    chores = [d for d in eps if d.get("handoff") is not None]
    chores_judged = [d for d in chores if d["handoff"]["chores_right"] is not None and d["handoff"]["chores"] > 0]
    handoff = {"mean_share": _mean([d["handoff"]["chores_right"] / d["handoff"]["chores"] for d in chores_judged]),
               "episodes": sorted(d["episode"] for d in chores_judged), "not_measured": sorted(d["episode"] for d in chores if d not in chores_judged)} if chores else None
    long_routing = [d for d in eps if d["family"] == "routing" and d["grouping"]["horizon"] in ("h80", "80")]
    ordering = {side: sorted(d["episode"] for d in long_routing if (d.get("conflict_vs_reset") or "not_measured") == side)
                for side in ("before", "after", "not_measured")}
    conflict_reset = {**{side: len(ids) for side, ids in ordering.items()}, "episodes": ordering} if long_routing else None
    return {
        "episodes": sorted(d["episode"] for d in eps),
        "rerun_required": sorted(d["episode"] for d in reruns),
        "R": {"value": point["R"], "interval": ci["R"], "pairs": len(detail["pairs"]), "seeds": len(u["routing"]),
              "pair_rows": detail["pairs"], "incomplete_pairs": detail["incomplete_pairs"], "unpaired": detail["unpaired"],
              "not_reached": detail["not_reached"]},
        "E": {"value": None if withheld else point["E"], "interval": None if withheld else ci["E"], "withheld": WITHHELD if withheld else None,
              "episodes": sorted(detail["explanation"]), "no_reader": sorted(detail["no_reader"]), "seeds": len(u["explanation"]),
              "reader_kind": reader.get("kind") if reader and detail["explanation"] else None,
              "scripted_reader": scripted, "label": SCRIPTED if scripted else None},
        "S": {"value": point["S"], "interval": ci["S"], "episodes": sorted(detail["steering"]), "seeds": len(u["steering"])},
        "headline": {"value": None if withheld else point["H"], "interval": None if withheld else ci["H"], "withheld": WITHHELD if withheld else None,
                     "ranked": not unranked, "unranked_because": unranked, "band": None, "band_note": None},
        "not_scored": sorted(detail["not_scored"]), "no_reset": sorted(detail["no_reset"]), "handoff": handoff,
        "conflict_vs_reset": conflict_reset,
        "no_conflict_control": {**_rate(nocon), "episodes": sorted(nocon_eps)},
        "controls": control_rates(eps), "pooled_controls": pooled_controls, "eligibility": elig, "capability": capability,
        "reported": reported,
        "event_reach": {"events": dict(sorted(events.items())),
                        "all_required": {"reached": sum(all_req), "episodes": len(all_req), "share": (sum(all_req) / len(all_req)) if all_req else None,
                                         "not_reached": sorted(d["episode"] for d in eps if d["reported"]["reach"]["status"] != "reached")},
                        "correction_not_delivered": undelivered},
        "probes": probe, "awareness": aw,
        "_units": u,
    }


def _ratefmt(r: dict[str, Any]) -> str:
    return f"controls {r['passed']}/{r['judged']}" + (f" = {100 * r['rate']:.1f}%" if r["rate"] is not None else "")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def poll_key(v: Any) -> str:
    """The section key: the recorded value exactly (a float's repr round-trips)."""
    return "not recorded" if v is None else repr(float(v))


def poll_display(k: str) -> str:
    if k == "not recorded":
        return k
    shown = f"{float(k):.3f}"
    return f"{shown} s" + ("" if float(shown) == float(k) else f" (recorded as {k})")


def build(root: str | Path, resamples: int = 2000, seed: int = 0, level: float = 0.95, bar: float = 0.9, z: float = SANITY_Z,
          scripted_reader: bool = False, plan: str | Path | None = None, require_plan: bool = False, judged_bar: float = 0.9, allow_mixed_runtimes: bool = False) -> dict[str, Any]:
    root = Path(root)
    if require_plan and plan is None:
        raise ReportError("a release report requires a plan")
    index = read_index(root / "index.json", require_plan)
    scores = read_scores(root)
    reader = check_inputs(scores, index, scripted_reader)
    versions = sorted({d.get("python_version") or "not recorded" for d in scores})
    if len(versions) > 1 and not allow_mixed_runtimes:
        raise ReportError("score files use different Python versions: " + ", ".join(versions) + "; pass --allow-mixed-runtimes for a report that is not a release")
    runtime_note = None
    if versions != [RELEASE_PYTHON]:
        runtime_note = f"scoring Python {', '.join(versions) or 'not recorded'} differs from release Python {RELEASE_PYTHON}: not a release"
    record_note = "hosted reader without a persistent record: not a release" if any(d.get("reader_record") is False for d in scores) else None
    plan_note = "plan was not required: not a release" if not require_plan else None
    changed = [name for name, value, default in (("bar", bar, 0.9), ("judged-bar", judged_bar, 0.9),
               ("level", level, 0.95), ("z", z, SANITY_Z), ("resamples", resamples, 2000), ("seed", seed, 0)) if value != default]
    options_note = "non-default " + ", ".join(changed) + ": not a release" if changed else None
    nonrelease_note = "; ".join(n for n in (runtime_note, record_note, plan_note, options_note) if n) or None
    if not 0 < judged_bar <= 1:
        raise ReportError("judged_bar must be greater than zero and at most one")
    plan_instances = read_plan(plan, scores) if plan is not None else None
    sanity = reader_sanity(scores, z)
    unscored = sorted(set(index) - {d["episode"] for d in scores})
    scripted = bool(reader and reader.get("kind") == "scripted")
    params = {"resamples": resamples, "seed": seed, "level": level, "bar": bar, "sanity_z": z, "judged_bar": judged_bar, "require_plan": require_plan, "runtime_note": runtime_note, "nonrelease_note": nonrelease_note, "release_python": RELEASE_PYTHON}
    by_poll: dict[str, list[dict[str, Any]]] = {}
    for d in scores:
        by_poll.setdefault(poll_key(d.get("poll_s")), []).append(d)
    if require_plan and not by_poll:
        by_poll["not recorded"] = []
    notes = ["Completeness was inferred from the runs given." if plan is None else "Completeness uses the supplied task suite plan."]
    if len(by_poll) > 1:
        notes.append("Episodes ran with different polling intervals (" + ", ".join(poll_display(k) for k in sorted(by_poll, key=natural)) + "). "
                     "Each interval has its own tables, and no table compares runs across them (D-040).")
    if nonrelease_note:
        notes.append(nonrelease_note + ". No rank bands are published.")
    sections = [_section(poll, by_poll[poll], index, params, reader, scripted, sanity, plan_instances) for poll in sorted(by_poll, key=natural)]
    return {"format": FORMAT, "version": VERSION, "parameters": params, "notes": notes,
            "python_versions": versions, "not_release": bool(nonrelease_note) or scripted,
            "reader": reader, "scripted_reader": scripted, "label": SCRIPTED if scripted else None,
            "inputs": {"scored": len(scores), "score_files_read": len(scores), "usable_scores": sum(not rerun(d) for d in scores), "rerun_markers": sum(rerun(d) for d in scores), "indexed": len(index), "indexed_not_scored": unscored,
                       "rerun_required": sorted(d["episode"] for d in scores if rerun(d))},
            "sections": sections}


def _compare(members: list[dict[str, Any]], tag: tuple, params: dict[str, Any], withheld: bool, fn: Callable[[list[float]], float],
             drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """One paired comparison over `members` (built cells), for the headline and each sub-score."""
    normalized = []
    planned = {f: set() for f in FAMILIES}
    for m in members:
        rows = {}
        for f in FAMILIES:
            rows[f] = {measures.identity({k: v for k, v in json.loads(i).items() if k not in drop}): row
                       for i, row in m["_measures"][f].items()}
            planned[f].update(rows[f])
        normalized.append(rows)
    common = {f: set.intersection(*({i for i, row in m[f].items() if row["value"] is not None} for m in normalized)) for f in FAMILIES}
    us = []
    for m in normalized:
        u = {f: {} for f in FAMILIES}
        for f in FAMILIES:
            for i in sorted(common[f]):
                row = m[f][i]
                u[f].setdefault(row["seed"], []).append(row["value"])
        us.append(u)
    points, draws = paired(us, tag, params)
    out = {}
    for k, name, families in (("H", "headline", FAMILIES), ("R", "R", ("routing",)), ("E", "E", ("explanation",)), ("S", "S", ("steering",))):
        n = sum(len(planned[f]) for f in families)
        judged = sum(len(common[f]) for f in families)
        omitted = [sorted(i for f in families for i in planned[f] - common[f]) for m in normalized]
        left_out = [sorted(i for f in families for i, row in m[f].items() if row["value"] is not None and i not in common[f]) for m in normalized]
        meta = {"n": judged, "planned": n, "left_out": left_out, "left_out_counts": list(map(len, left_out)),
                "omitted_instances": omitted, "common_instances": sorted(i for f in families for i in common[f])}
        if withheld and k in ("H", "E"):
            out[name] = {"value": None, "interval": None, "withheld": WITHHELD, **meta}
            continue
        if any(not planned[f] or len(common[f]) / len(planned[f]) < params["judged_bar"] for f in families):
            out[name] = {"value": None, "interval": None, "not_comparable": f"judged on both sides {judged} of {n}", **meta}
            continue
        pv = [p[k] for p in points]
        ds = [fn(vals) for vals in ([d[k] for d in row] for row in draws) if None not in vals]
        out[name] = {"value": fn(pv), "interval": stats.interval(ds, params["level"]) if ds else None, "points": pv,
                     "show_common_values": any(left_out), **meta}
    return out


def _section(poll: str, scores: list[dict[str, Any]], index: dict[str, dict[str, Any]], params: dict[str, Any],
             reader: dict[str, Any] | None, scripted: bool, sanity, plan_instances) -> dict[str, Any]:
    withheld = any(not v["passed"] for v in sanity.values())
    by_config: dict[str, list[dict[str, Any]]] = {}
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    reruns: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    planned: dict[tuple[str, str], set[str]] = {}
    for d in scores:
        ck = config_key(index[d["episode"]])
        k = (ck, d["grouping"]["horizon"], d["grouping"]["mode"])
        planned.setdefault((k[1], k[2]), set()).add(instance_key(d))
        cells.setdefault(k, [])
        reruns.setdefault(k, [])
        (reruns if rerun(d) else cells)[k].append(d)
        by_config.setdefault(ck, []).append(d)
    configs = {config_key(c): c for c in index.values()} if params["require_plan"] else {config_key(index[d["episode"]]): index[d["episode"]] for d in scores}
    labels = column_labels(configs)
    if plan_instances is not None:
        modes = ["default", "coached"] if params["require_plan"] else sorted({k[2] for k in cells})
        planned = {(inst["horizon"], mode): set() for inst in plan_instances for mode in modes}
        for inst in plan_instances:
            for mode in modes:
                planned[(inst["horizon"], mode)].add(measures.identity(inst))
        for ck in configs:
            for hz, mode in planned:
                cells.setdefault((ck, hz, mode), [])
                reruns.setdefault((ck, hz, mode), [])
    pooled_plan = {measures.identity({**json.loads(i), "mode": mode}) for (hz, mode), ids in planned.items() for i in ids}
    pooled = {ck: pooled_eligibility(by_config.get(ck, []), params["bar"], pooled_plan) for ck in configs}
    built = {}
    for k in cells:
        x = cell(cells[k], reruns[k], (poll, *k), params, pooled[k[0]], withheld, reader)
        ran = sorted({instance_key(d) for d in cells[k] if complete(d)})
        missing = sorted(planned[(k[1], k[2])] - set(ran))
        x["coverage"] = {"ran": len(ran), "planned": len(planned[(k[1], k[2])]), "missing": [json.loads(m) for m in missing]}
        x["instances"] = ran
        x["label"] = f"{configs[k[0]]['model']} in {harness_label(configs[k[0]])} at {k[1]} {k[2]}"
        x["_measures"] = measures.collect(cells[k] + reruns[k], planned[(k[1], k[2])])
        for f, metric in zip(FAMILIES, ("R", "E", "S")):
            x[metric].update(measures.coverage(x["_measures"][f], params["judged_bar"]))
            if not x[metric]["official"]:
                x["headline"]["unranked_because"].append(f"{metric} partial: judged {x[metric]['judged']} of {x[metric]['planned']}")
        x["controls"] = measures.control_rates(measures.collect(cells[k] + reruns[k], planned[(k[1], k[2])], control=True))
        nc = measures.collect(cells[k] + reruns[k], planned[(k[1], k[2])], no_conflict=True)["routing"]
        x["no_conflict_control"].update(measures.coverage(nc, params["judged_bar"]))
        x["capability_status"] = pooled[k[0]]["capability_status"]
        combined = {i: v for f in FAMILIES for i, v in x["_measures"][f].items()}
        x["headline"].update(measures.coverage(combined, params["judged_bar"]))
        x["headline"]["official"] = all(x[m]["official"] for m in ("R", "E", "S"))
        x["headline"]["partial"] = not x["headline"]["official"]
        if poll == "not recorded":
            x["headline"]["unranked_because"].append("polling interval not recorded")
        x["headline"]["ranked"] = not x["headline"]["unranked_because"]
        built[k] = x

    def public(x: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in x.items() if k not in ("_units", "_measures", "instances", "label")}

    tables = []
    for hz, mode in sorted({(k[1], k[2]) for k in built}, key=lambda x: (natural(x[0]), x[1] != "default", x[1])):
        keys = sorted(c for c, h, m in built if h == hz and m == mode)
        rows = [{"configuration": configs[ck], "model": configs[ck]["model"], "harness": labels[column_key(configs[ck])], "column_key": column_key(configs[ck]), **public(built[(ck, hz, mode)])}
                for ck in keys]
        by_harness: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_harness.setdefault(r["column_key"], []).append(r)
        correlation = {}
        for ha, rs in sorted(by_harness.items()):
            # Spec section 2: models are compared within one harness, so bands and correlations are per harness.
            if scripted or params["nonrelease_note"] or poll == "not recorded":
                for r in rs:
                    r["headline"]["band_note"] = "no bands: " + (SCRIPTED if scripted else params["nonrelease_note"] or "polling interval not recorded")
            else:
                ranked = [(config_key(r["configuration"]), r["headline"]["value"], r["headline"]["interval"]) for r in rs
                          if r["headline"]["ranked"] and r["headline"]["interval"] is not None]
                bands = stats.rank_bands(ranked)
                for r in rs:
                    r["headline"]["band"] = bands.get(config_key(r["configuration"]))
                    if r["headline"]["ranked"] and r["headline"]["interval"] is None:
                        r["headline"]["band_note"] = "no interval, so no band (zero resamples)"
            pts = [(r["headline"]["value"], r["capability"]) for r in rs
                   if r["headline"]["value"] is not None and r["capability"] is not None and r["headline"]["official"]]
            rho = None if poll == "not recorded" else stats.spearman([p[0] for p in pts], [p[1] for p in pts])
            correlation[ha] = {"spearman": rho, "n": len(pts), "withheld": "polling interval not recorded" if poll == "not recorded" else WITHHELD if withheld else None,
                               "label": labels[ha], "between": "headline and pooled capability composite, over configurations of this harness with an official value in each family and a complete capability composite"}
        tables.append({"horizon": hz, "mode": mode, "scripted_reader": scripted, "label": SCRIPTED if scripted else None,
                       "planned_instances": len(planned[(hz, mode)]), "rows": rows, "rank_correlation": correlation})

    drops = []
    for ck in sorted(configs):
        for mode in sorted({k[2] for k in built if k[0] == ck}):
            hs = sorted({k[1] for k in built if k[0] == ck and k[2] == mode}, key=natural)
            for h in hs[1:]:
                a, b = built[(ck, hs[0], mode)], built[(ck, h, mode)]
                cmp = _compare([a, b], ("drop", poll, ck, mode, hs[0], h), params, withheld, lambda v: v[0] - v[1], drop=("horizon",))
                drops.append({"model": configs[ck]["model"], "harness": labels[column_key(configs[ck])], "mode": mode, "from": hs[0], "to": h, **cmp})

    # Spec section 2, rule 3: for a model run in two or more harnesses, the
    # spread of its headline across them, taken here as the range.
    effect, settings_notes, version_notes, effect_unmeasured = [], [], [], []
    for model in sorted({c["model"] for c in configs.values()}):
        for hz, mode in sorted({(k[1], k[2]) for k in built if configs[k[0]]["model"] == model}):
            matching, same = {}, {}
            for k in sorted(built):
                c = configs[k[0]]
                if c["model"] != model or k[1:] != (hz, mode):
                    continue
                settings = config_key({f: v for f, v in c.items() if f not in ("harness", "harness_version")})
                matching.setdefault(settings, []).append(k)
                same.setdefault((c["harness"], c.get("harness_version")), []).append(k)
            for keys in same.values():
                if len(keys) > 1:
                    settings_notes.append({"model": model, "horizon": hz, "mode": mode,
                                           "configurations": [configs[k[0]] for k in keys]})
            for settings, members in sorted(matching.items()):
                if len(members) < 2:
                    continue
                by_name = {}
                for k in members:
                    by_name.setdefault(configs[k[0]]["harness"], []).append(k)
                ambiguous = sorted(name for name, keys in by_name.items() if len(keys) > 1)
                for name in ambiguous:
                    version_notes.append({"model": model, "horizon": hz, "mode": mode, "harness": name,
                                          "values": [{"configuration": configs[k[0]], **{m: built[k][m]["value"] for m in ("R", "E", "S", "headline")}}
                                                     for k in by_name[name]]})
                if len(by_name) < 2:
                    continue
                if ambiguous:
                    effect_unmeasured.append({"model": model, "horizon": hz, "mode": mode,
                                              "configurations": [configs[k[0]] for k in members], "ambiguous_harnesses": ambiguous,
                                              "reason": "cannot choose a version for " + ", ".join(ambiguous)})
                    continue
                included = [k for k in members if any(row["value"] is not None for f in FAMILIES for row in built[k]["_measures"][f].values())]
                if len(included) < 2:
                    continue
                omitted = [{"configuration": configs[k[0]], "reason": "no judged values"} for k in members if k not in included]
                cmp = _compare([built[k] for k in included], ("effect", poll, model, hz, mode, settings), params, withheld, lambda v: max(v) - min(v))
                effect.append({"model": model, "horizon": hz, "mode": mode, "not_included": omitted,
                               "harnesses": [labels[column_key(configs[k[0]])] for k in included], **cmp})

    # Spec section 2: the gap between Default and Coached, Coached minus Default.
    gaps = []
    for ck in sorted(configs):
        for hz in sorted({k[1] for k in built if k[0] == ck}, key=natural):
            a, b = (ck, hz, "default"), (ck, hz, "coached")
            if a not in built or b not in built:
                continue
            cmp = _compare([built[a], built[b]], ("gap", poll, ck, hz), params, withheld, lambda v: v[1] - v[0])
            gaps.append({"model": configs[ck]["model"], "harness": labels[column_key(configs[ck])], "horizon": hz, **cmp})

    if poll == "not recorded":
        drops, effect, gaps = [], [], []
    return {"poll_s": poll, "poll_display": poll_display(poll), "episodes": len(scores), "reader_sanity": sanity,
            "withheld": WITHHELD if withheld else None,
            "eligibility": {ck: {**pooled[ck], "configuration": configs[ck]} for ck in sorted(pooled)},
            "tables": tables, "horizon_drop": drops, "harness_effect": effect, "same_harness_different_settings": settings_notes, "coached_gap": gaps,
            "same_harness_different_versions": version_notes, "harness_effect_not_measured": effect_unmeasured,
            "rerun_required": sorted(d["episode"] for d in scores if rerun(d))}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _f(v: float | None, digits: int = 3) -> str:
    return NM if v is None else f"{v:.{digits}f}"


def _pct(v: float | None) -> str:
    return NM if v is None else f"{100 * v:.1f}%"


def _ci(iv: list[float] | None, digits: int) -> str:
    return "" if iv is None else f"[{iv[0]:.{digits}f}, {iv[1]:.{digits}f}]"


def _judged(x):
    return f"judged {x['judged']} of {x['planned']}" + (" partial" if x.get("partial") else "")


def _headline_cell(r: dict[str, Any]) -> str:
    h = r["headline"]
    if h["withheld"]:
        return "withheld " + _judged(h)
    if h["value"] is None:
        return NM + " " + _judged(h)
    s = f"{h['value']:.1f} {_ci(h['interval'], 1)}".strip()
    s += " " + _judged(h)
    if h.get("band"):
        return f"{s} band {h['band']['ranks']}"
    if h["ranked"]:
        return f"{s} (eligible; {h['band_note']})" if h.get("band_note") else s
    return f"{s} unranked"


def _cmp_cell(x: dict[str, Any], digits: int) -> str:
    if x.get("withheld"):
        return "withheld"
    if x.get("not_comparable"):
        return "not comparable: " + x["not_comparable"]
    suffix = f" judged on both sides {x.get('n', 0)} of {x.get('planned', 0)}; left out {x.get('left_out_counts', [])}"
    if x.get("show_common_values"):
        suffix += " common-set values " + ", ".join(_f(v, digits) for v in x["points"])
    return (f"{_f(x['value'], digits)} {_ci(x['interval'], digits)}".strip() + suffix)


def _grid(rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, r in enumerate(rows):
        out.append("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w[i] for i in range(len(w))))
    return out


def _md(rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join("---" for _ in rows[0]) + "|"]
    out += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows[1:]]
    return out


DETAIL_HEAD = ["model", "harness", "instances", "headline", "R", "E", "S", "controls R/E/S pooled (this cell)", "capability",
               "no-conflict", "unanswerable/ep", "private ids/ep", "wasted wait", "all required fired", "suspected eval"]


def _detail(r: dict[str, Any]) -> list[str]:
    c, pc = r["controls"], r["pooled_controls"]
    ctl = " / ".join(_pct(pc[f]["rate"]) + ("" if r["eligibility"][f]["eligible"] else "*") + f" {_judged(pc[f])} ({_pct(c[f]['rate'])}; {_judged(c[f])})" for f in FAMILIES)
    nc = r["no_conflict_control"]
    rp = r["reported"]
    cov = r["coverage"]
    e = r["E"]
    e_txt = "withheld " + _judged(e) if e["withheld"] else f"{_f(e['value'])} {_ci(e['interval'], 3)} {_judged(e)}".replace("  ", " ")
    if e["scripted_reader"]:
        e_txt += " (scripted reader)"
    return [r["model"], r["harness"], f"{cov['ran']} of {cov['planned']}" + (" MISSING" if cov["missing"] else ""), _headline_cell(r),
            f"{_f(r['R']['value'])} {_ci(r['R']['interval'], 3)} {_judged(r['R'])}".replace("  ", " "), e_txt,
            f"{_f(r['S']['value'])} {_ci(r['S']['interval'], 3)} {_judged(r['S'])}".replace("  ", " "),
            ctl, "incomplete" if r["capability_status"] == "incomplete" else _f(r["capability"]), _pct(nc["rate"]) + " " + _judged(nc),
            _f(rp["unanswerable_per_episode"], 2), _f(rp["private_identifiers_per_episode"], 2), _pct(rp["wasted_wait_share"]),
            _pct(r["event_reach"]["all_required"]["share"]), _pct(r["awareness"]["share_suspected"])]


def _cmp_table(title: str, head: list[str], rows: list[list[str]], grid: Callable[[list[list[str]]], list[str]]) -> list[str]:
    return [title, ""] + grid([head, *rows]) + [""]


def render(rep: dict[str, Any], markdown: bool) -> str:
    grid = _md if markdown else _grid
    h1, h2, h3 = ("# ", "## ", "### ") if markdown else ("", "== ", "Section: ")
    mark = f" ({SCRIPTED})" if rep["scripted_reader"] else ""
    p = rep["parameters"]
    L = [f"{h1}Coherency results{mark}", "",
         f"Bootstrap: {p['resamples']} resamples of seeds within each family, sibling pairs kept together, seed {p['seed']}, "
         f"{100 * p['level']:.0f}% percentile intervals; every comparison is paired on the seeds both cells ran. "
         f"Eligibility bar: {100 * p['bar']:.0f}% of a family's controls, pooled per model in a harness. "
         f"Reader sanity bound: {p['sanity_z']:g} standard errors. "
         f"Score files read: {rep['inputs']['score_files_read']}; usable scores: {rep['inputs']['usable_scores']}; rerun markers: {rep['inputs']['rerun_markers']}; "
         f"in the index but not scored: {len(rep['inputs']['indexed_not_scored'])}.", ""]
    L += ["Scoring Python versions: " + (", ".join(rep["python_versions"]) or NM) + f". Release runtime: Python {p['release_python']}.", ""]
    if rep["scripted_reader"]:
        L += ["Every Explanation score here was read by a scripted rule, not by the pinned reader model. This report checks the scorer. "
              "It is not a result, and it prints no rank bands.", ""]
    for n in rep["notes"]:
        L += [n, ""]
    for sec in rep["sections"]:
        L += [f"{h2}Polling interval {sec['poll_display']} ({sec['episodes']} episodes){mark}", ""]
        if sec["poll_s"] == "not recorded":
            L += ["Polling interval not recorded: raw values only; no bands or comparisons.", ""]
        for k, s in sec["reader_sanity"].items():
            na, bu = s["no_answer"], s["buried"]
            L.append(f"Reader {s['reader']['kind']} {k[:12]}: no answer {_pct(na['accuracy'])} over {na['items']} distinct items "
                     f"(chance 16.7%), buried cause {_pct(bu['accuracy'])} over {bu['items']} distinct items: "
                     + ("both sanity checks pass." if s["passed"] else "a sanity check FAILS, so E and every headline and comparison that needs it are withheld."))
        if sec["reader_sanity"]:
            L.append("")
        if sec["rerun_required"]:
            L += [f"Rerun required (ended outside the agent, counted as missing): {', '.join(sec['rerun_required'])}.", ""]
        for t in sec["tables"]:
            L += [f"{h3}Horizon {t['horizon']}, mode {t['mode']}{mark}", ""]
            models = sorted({r["model"] for r in t["rows"]})
            harnesses = sorted({r["column_key"] for r in t["rows"]})
            labels = {r["column_key"]: r["harness"] for r in t["rows"]}
            m = [["model \\ harness", *(labels[k] for k in harnesses)]]
            for mo in models:
                line = [mo]
                for ha in harnesses:
                    r = next((x for x in t["rows"] if x["model"] == mo and x["column_key"] == ha), None)
                    line.append("no runs" if r is None else _headline_cell(r))
                m.append(line)
            L += grid(m) + [""]
            L += [("No bands: " + SCRIPTED + ".") if rep["scripted_reader"] else "Bands rank models within one harness column only; "
                  "harnesses are compared in the harness effect table.", ""]
            L += grid([DETAIL_HEAD, *[_detail(r) for r in t["rows"]]]) + [""]
            for r in t["rows"]:
                if r.get("handoff") is not None:
                    handoff = r["handoff"]
                    L.append(f"{r['model']} in {r['harness']}: chores right mean share {_pct(handoff['mean_share'])}, "
                             f"{len(handoff['episodes'])} measured episodes, {len(handoff['not_measured'])} not measured. Diagnostic only.")
                if r.get("conflict_vs_reset") is not None:
                    order = r["conflict_vs_reset"]
                    L.append(f"{r['model']} in {r['harness']}: conflict before reset {order['before']}, "
                             f"after reset {order['after']}, not measured {order['not_measured']}. Diagnostic only.")
                for name, value in [(k, r[k]) for k in ("R", "E", "S")] + [(f + " controls", r["pooled_controls"][f]) for f in FAMILIES]:
                    for item in value["unjudged_instances"]:
                        why = "; ".join(x["reason"] + " " + ", ".join(x["episodes"]) for x in item["reasons"])
                        L.append(f"{r['model']} in {r['harness']}, {name}: unjudged {item['id']}: {why}.")
                if r["headline"]["unranked_because"]:
                    L.append(f"{r['model']} in {r['harness']}: unranked, " + "; ".join(r["headline"]["unranked_because"]) + ".")
            for ha, rc in t["rank_correlation"].items():
                if rc["withheld"]:
                    txt = "withheld"
                elif rc["spearman"] is not None:
                    txt = f"{rc['spearman']:.3f} over {rc['n']} configurations"
                else:
                    txt = f"{NM} ({rc['n']} configuration{'' if rc['n'] == 1 else 's'} with both; it needs three)"
                L.append(f"Rank correlation of the headline with the capability composite in {rc['label']}: {txt}.")
            L += [""]
            ev = [[r["model"], r["harness"], name, f"{e['fired']} / {e['scheduled']}", _pct(e["share"]), "yes" if e["required"] else "no"]
                  for r in t["rows"] for name, e in r["event_reach"]["events"].items()]
            if ev:
                L += _cmp_table(f"{h3}Event reach, horizon {t['horizon']}, mode {t['mode']}{mark}" if markdown else f"Event reach{mark}:",
                                ["model", "harness", "event", "fired / scheduled", "share", "required"], ev, grid)
            pr = [[r["model"], r["harness"], *(str(r["probes"][k]) for k in ("knew_right", "knew_wrong", "not_knew_right", "not_knew_wrong", "not_judged"))]
                  for r in t["rows"]]
            L += _cmp_table(f"{h3}Probes, horizon {t['horizon']}, mode {t['mode']}{mark}" if markdown else f"Probes{mark}:",
                            ["model", "harness", "knew, right", "knew, wrong", "did not know, right", "did not know, wrong", "not judged"], pr, grid)

        def cells_of(r: dict[str, Any]) -> list[str]:
            return [*(_cmp_cell(r[k], 1 if k == "headline" else 3) for k in ("headline", "R", "E", "S")), r["headline"].get("not_comparable") or ""]

        for spread in sec["harness_effect"]:
            for omitted in spread.get("not_included", []):
                L.append(f"{spread['model']} at {spread['horizon']} {spread['mode']}: {harness_label(omitted['configuration'])} not included in harness spread: {omitted['reason']}.")
        for entry in sec["same_harness_different_settings"]:
            L.append("Same harness, different settings: " + config_key(entry) + ". Spread not measured.")
        for entry in sec.get("same_harness_different_versions", []):
            L.append(f"Same harness, different versions: {entry['model']}, {entry['harness']}, {entry['horizon']} {entry['mode']}. Spread not measured.")
            L += _cmp_table("Values by version:", ["configuration", "headline", "R", "E", "S"],
                            [[harness_label(v["configuration"]), _f(v["headline"], 1), *(_f(v[m]) for m in ("R", "E", "S"))]
                             for v in entry["values"]], grid)
        for entry in sec.get("harness_effect_not_measured", []):
            L.append(f"Harness effect for {entry['model']} at {entry['horizon']} {entry['mode']}: not measured; {entry['reason']}.")
        if not sec["harness_effect"]:
            L += ["Harness effect: no qualifying set with distinct harness names, one version per name and equal remaining settings.", ""]
        for kind in ("coached_gap", "horizon_drop", "harness_effect"):
            for entry in sec[kind]:
                for metric in ("R", "E", "S"):
                    cmp = entry[metric]
                    for side, ids in enumerate(cmp.get("omitted_instances", [])):
                        if ids:
                            L.append(f"{kind} {entry['model']} {metric}, side {side + 1}, omitted instances: " + ", ".join(ids) + ".")
        if sec["harness_effect"]:
            L += _cmp_table(f"{h3}Harness effect{mark}" if markdown else f"Harness effect (spread of a model's headline across harnesses, highest minus lowest){mark}:",
                            ["model", "horizon", "mode", "harnesses", "headline", "R", "E", "S", "why not comparable"],
                            [[r["model"], r["horizon"], r["mode"], ", ".join(r["harnesses"]), *cells_of(r)] for r in sec["harness_effect"]], grid)
        if sec["coached_gap"]:
            L += _cmp_table(f"{h3}Default to Coached gap{mark}" if markdown else f"Default to Coached gap (Coached minus Default){mark}:",
                            ["model", "harness", "horizon", "headline", "R", "E", "S", "why not comparable"],
                            [[r["model"], r["harness"], r["horizon"], *cells_of(r)] for r in sec["coached_gap"]], grid)
        if sec["horizon_drop"]:
            L += _cmp_table(f"{h3}Horizon drop{mark}" if markdown else f"Horizon drop (shorter minus longer){mark}:",
                            ["model", "harness", "mode", "from", "to", "headline", "R", "E", "S", "why not comparable"],
                            [[r["model"], r["harness"], r["mode"], r["from"], r["to"], *cells_of(r)] for r in sec["horizon_drop"]], grid)
    L += ["Control rates are pooled over every horizon and mode of a model in a harness; the rate in parentheses is this cell's own. "
          "A * marks a pooled rate below the eligibility bar.",
          "\"instances\" counts the task instances this configuration ran out of the table's planned set. A configuration missing some is "
          "shown with judged coverage. Comparisons use the common judged instances and the judged bar.",
          f"\"{NM}\" means there was no scored episode or no defined value for that number. It is never a zero."]
    return "\n".join(L).rstrip() + "\n"


def write(rep: dict[str, Any], out: Path) -> tuple[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    text, md = render(rep, False), render(rep, True)
    (out / "report.json").write_text(score_dumps(rep), encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.txt").write_text(text, encoding="utf-8")
    return text, md


__all__ = ["build", "render", "write", "ReportError", "tidy", "subscores", "SANITY_Z"]
