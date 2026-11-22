"""The results report over many scored episodes.

Inputs: a results directory holding `index.json` (opaque episode id ->
configuration) and score files (`*.score.json`, anywhere below it), written
by `python -m cohbench.scoring score`. The scorers never see the index: it is
joined here, after scoring.

Output: one JSON document with every number and the episodes behind each,
rendered as a plain text table and as Markdown. Episodes whose polling
interval (`poll_s`, D-040) differs are never put in one table: each value
gets its own section, and the report says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import stats
from .rng import stream
from .score import dumps as score_dumps
from .score import tidy

INDEX_FORMAT = "cohbench-results-index"
FORMAT = "cohbench-report"
VERSION = 1
FAMILIES = ("routing", "explanation", "steering")
NM = "not measured"


class ReportError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def read_index(path: Path) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"the results index cannot be read: {type(exc).__name__}") from None
    if not isinstance(doc, dict) or doc.get("format") != INDEX_FORMAT or doc.get("version") != 1 or not isinstance(doc.get("episodes"), dict):
        raise ReportError(f"the results index is {{format: {INDEX_FORMAT}, version: 1, episodes: {{id: configuration}}}}")
    for eid, c in doc["episodes"].items():
        if not (isinstance(c, dict) and isinstance(c.get("model"), str) and c["model"] and isinstance(c.get("harness"), str) and c["harness"]
                and all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in c.values())):
            raise ReportError(f"index entry {eid}: a configuration names model and harness, and every value is a string or number")
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
        out.append(doc)
    return out


def config_key(c: dict[str, Any]) -> str:
    return json.dumps(c, sort_keys=True, separators=(",", ":"))


def harness_label(c: dict[str, Any]) -> str:
    extra = [f"{k}={c[k]}" for k in sorted(c) if k not in ("model", "harness")]
    return c["harness"] + (f" ({', '.join(extra)})" if extra else "")


def natural(s: str) -> tuple:
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s) if t)


# ---------------------------------------------------------------------------
# One cell: a configuration at one horizon and one mode
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _rate(xs: list[bool | None]) -> dict[str, Any]:
    j = [x for x in xs if x is not None]
    return {"passed": sum(1 for x in j if x), "judged": len(j), "not_judged": len(xs) - len(j),
            "rate": (sum(1 for x in j if x) / len(j)) if j else None}


def units(eps: list[dict[str, Any]]) -> tuple[dict[str, dict[int, list]], dict[str, Any]]:
    """The bootstrap units of one cell, by family and seed, and what they were made from."""
    u: dict[str, dict[int, list]] = {f: {} for f in FAMILIES}
    detail: dict[str, Any] = {"pairs": [], "incomplete_pairs": [], "unpaired": [], "explanation": [], "steering": [], "not_scored": []}
    pairs: dict[tuple[int, str], dict[str, list[dict[str, Any]]]] = {}
    for d in eps:
        g, s = d["grouping"], d["score"]
        if g.get("control"):
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
                detail["not_scored"].append(d["episode"])
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
        a, b = sides["A"][0]["score"]["passed"], sides["B"][0]["score"]["passed"]
        row = {"seed": seed, "pair": pid, "episodes": [sides["A"][0]["episode"], sides["B"][0]["episode"]], "A": a, "B": b}
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


def cell(eps: list[dict[str, Any]], key: tuple, params: dict[str, Any], reader_ok: dict[str, bool | None]) -> dict[str, Any]:
    u, detail = units(eps)
    point = subscores({f: list(u[f].values()) for f in FAMILIES})
    rng = stream("bootstrap", params["seed"], *key)
    seed_units = {f: [u[f][s] for s in sorted(u[f])] for f in FAMILIES}
    draws = stats.bootstrap(seed_units, subscores, rng, params["resamples"]) if params["resamples"] > 0 else []
    ci = {k: (stats.interval([d[k] for d in draws], params["level"]) if point[k] is not None else None) for k in ("R", "E", "S", "H")}

    ctl: dict[str, list] = {f: [] for f in FAMILIES}
    ctl_eps: dict[str, list] = {f: [] for f in FAMILIES}
    nocon, nocon_eps = [], []
    for d in eps:
        if d["grouping"].get("control"):
            ctl[d["family"]].append(d["control"]["passed"] if d.get("control") else None)
            ctl_eps[d["family"]].append(d["episode"])
        elif d["family"] == "routing" and d["score"] is not None and d["score"].get("kind") == "no_conflict":
            nocon.append(d["score"]["passed"])
            nocon_eps.append(d["episode"])
    controls = {f: {**_rate(ctl[f]), "episodes": sorted(ctl_eps[f])} for f in FAMILIES}
    elig = {}
    for f in FAMILIES:
        r = controls[f]["rate"]
        elig[f] = {"eligible": (r is not None and r >= params["bar"]), "rate": r, "why": None if r is not None else "no judged controls"}
    rates = [controls[f]["rate"] for f in FAMILIES]
    capability = None if None in rates else sum(rates) / 3

    readers = sorted({d["reader"]["config_sha256"] for d in eps if d["family"] == "explanation" and d.get("reader") and not d["grouping"].get("control")})
    e_flags = []
    if len(readers) > 1:
        e_flags.append("explanation episodes were read by more than one reader config")
    if any(reader_ok.get(r) is False for r in readers):
        e_flags.append("the reader failed a sanity check")

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
    reach = [d["reported"]["reach"]["status"] == "reached" for d in eps]
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

    unranked = [f"not eligible in {f} ({_ratefmt(controls[f])})" if elig[f]["rate"] is not None else f"no judged controls in {f}"
                for f in FAMILIES if not elig[f]["eligible"]]
    if point["H"] is None:
        unranked.append("the headline is not measured")
    if e_flags:
        unranked.extend(e_flags)
    return {
        "episodes": sorted(d["episode"] for d in eps),
        "R": {"value": point["R"], "interval": ci["R"], "pairs": len(detail["pairs"]), "seeds": len(u["routing"]),
              "pair_rows": detail["pairs"], "incomplete_pairs": detail["incomplete_pairs"], "unpaired": detail["unpaired"]},
        "E": {"value": point["E"], "interval": ci["E"], "episodes": sorted(detail["explanation"]), "seeds": len(u["explanation"]), "flags": e_flags, "readers": readers},
        "S": {"value": point["S"], "interval": ci["S"], "episodes": sorted(detail["steering"]), "seeds": len(u["steering"])},
        "headline": {"value": point["H"] if not e_flags else None, "interval": ci["H"] if not e_flags else None,
                     "computed": point["H"], "ranked": not unranked, "unranked_because": unranked},
        "not_scored": sorted(detail["not_scored"]),
        "no_conflict_control": {**_rate(nocon), "episodes": sorted(nocon_eps)},
        "controls": controls, "eligibility": elig, "capability": capability,
        "reported": reported,
        "event_reach": {"reached": sum(reach), "episodes": len(reach), "share": (sum(reach) / len(reach)) if reach else None},
        "probes": probe, "awareness": aw,
        "_draws": draws,
    }


def _ratefmt(r: dict[str, Any]) -> str:
    return f"controls {r['passed']}/{r['judged']}" + (f" = {100 * r['rate']:.1f}%" if r["rate"] is not None else "")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def reader_sanity(scores: list[dict[str, Any]], z: float) -> dict[str, dict[str, Any]]:
    """Per reader config: the two sanity checks over every Explanation episode it read."""
    acc: dict[str, dict[str, Any]] = {}
    for d in scores:
        s = d.get("score")
        if d["family"] != "explanation" or not s or not s.get("sanity") or not d.get("reader"):
            continue
        k = d["reader"]["config_sha256"]
        a = acc.setdefault(k, {"reader": d["reader"], "no_answer": [0, 0], "buried": [0, 0], "episodes": []})
        for part in ("no_answer", "buried"):
            a[part][0] += s["sanity"][part]["correct"]
            a[part][1] += s["sanity"][part]["n"]
        a["episodes"].append(d["episode"])
    out = {}
    for k, a in sorted(acc.items()):
        na = stats.near_chance(a["no_answer"][0], a["no_answer"][1], 1 / 6, z, two_sided=True)
        bu = stats.near_chance(a["buried"][0], a["buried"][1], 1 / 6, z, two_sided=False)
        out[k] = {"reader": a["reader"], "no_answer": na, "buried": bu, "passed": bool(na["passed"] and bu["passed"]), "episodes": sorted(a["episodes"])}
    return out


def build(root: str | Path, resamples: int = 2000, seed: int = 0, level: float = 0.95, bar: float = 0.9, z: float = 2.0) -> dict[str, Any]:
    root = Path(root)
    index = read_index(root / "index.json")
    scores = read_scores(root)
    missing = sorted(d["episode"] for d in scores if d["episode"] not in index)
    if missing:
        raise ReportError(f"{len(missing)} scored episode(s) are not in the index, the first {missing[0]}")
    unscored = sorted(set(index) - {d["episode"] for d in scores})
    params = {"resamples": resamples, "seed": seed, "level": level, "bar": bar, "sanity_z": z}
    by_poll: dict[str, list[dict[str, Any]]] = {}
    for d in scores:
        by_poll.setdefault("not recorded" if d.get("poll_s") is None else repr(float(d["poll_s"])), []).append(d)
    notes = []
    if len(by_poll) > 1:
        notes.append("Episodes ran with different polling intervals (" + ", ".join(sorted(by_poll)) + " s). "
                     "Each interval has its own tables, and no table compares runs across them (D-040).")
    sections = []
    for poll in sorted(by_poll, key=natural):
        sections.append(_section(poll, by_poll[poll], index, params))
    return {"format": FORMAT, "version": VERSION, "parameters": params, "notes": notes,
            "inputs": {"scored": len(scores), "indexed": len(index), "indexed_not_scored": unscored},
            "sections": sections}


def _section(poll: str, scores: list[dict[str, Any]], index: dict[str, dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    sanity = reader_sanity(scores, params["sanity_z"])
    ok = {k: v["passed"] for k, v in sanity.items()}
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for d in scores:
        c = index[d["episode"]]
        cells.setdefault((config_key(c), d["grouping"]["horizon"], d["grouping"]["mode"]), []).append(d)
    built = {k: cell(v, (poll, *k), params, ok) for k, v in cells.items()}
    configs = {config_key(index[d["episode"]]): index[d["episode"]] for d in scores}
    tables = []
    for hz, mode in sorted({(k[1], k[2]) for k in built}, key=lambda x: (natural(x[0]), x[1])):
        rows = []
        for ck in sorted(c for c, h, m in built if h == hz and m == mode):
            x = built[(ck, hz, mode)]
            rows.append({"configuration": configs[ck], "model": configs[ck]["model"], "harness": harness_label(configs[ck]), **{k: v for k, v in x.items() if k != "_draws"}})
        ranked = [(config_key(r["configuration"]), r["headline"]["value"], r["headline"]["interval"]) for r in rows
                  if r["headline"]["ranked"] and r["headline"]["interval"] is not None]
        bands = stats.rank_bands(ranked)
        for r in rows:
            b = bands.get(config_key(r["configuration"]))
            r["headline"]["band"] = b
        pts = [(r["headline"]["computed"], r["capability"]) for r in rows if r["headline"]["computed"] is not None and r["capability"] is not None]
        rho = stats.spearman([p[0] for p in pts], [p[1] for p in pts])
        tables.append({"horizon": hz, "mode": mode, "rows": rows,
                       "rank_correlation": {"spearman": rho, "n": len(pts), "between": "headline (computed) and capability composite"}})
    drops = []
    for ck in sorted(configs):
        for mode in sorted({k[2] for k in built if k[0] == ck}):
            hs = sorted({k[1] for k in built if k[0] == ck and k[2] == mode}, key=natural)
            for h in hs[1:]:
                a, b = built[(ck, hs[0], mode)], built[(ck, h, mode)]
                row = {"model": configs[ck]["model"], "harness": harness_label(configs[ck]), "mode": mode, "from": hs[0], "to": h}
                for k, name in (("H", "headline"), ("R", "R"), ("E", "E"), ("S", "S")):
                    pa = a["headline"]["computed"] if k == "H" else a[k]["value"]
                    pb = b["headline"]["computed"] if k == "H" else b[k]["value"]
                    if pa is None or pb is None:
                        row[name] = {"value": None, "interval": None}
                        continue
                    diffs = [da[k] - db[k] for da, db in zip(a["_draws"], b["_draws"]) if da[k] is not None and db[k] is not None]
                    row[name] = {"value": pa - pb, "interval": stats.interval(diffs, params["level"]) if diffs else None}
                drops.append(row)
    return {"poll_s": poll, "episodes": len(scores), "reader_sanity": sanity, "tables": tables, "horizon_drop": drops}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _f(v: float | None, digits: int = 3) -> str:
    return NM if v is None else f"{v:.{digits}f}"


def _pct(v: float | None) -> str:
    return NM if v is None else f"{100 * v:.1f}%"


def _ci(iv: list[float] | None, digits: int) -> str:
    return "" if iv is None else f"[{iv[0]:.{digits}f}, {iv[1]:.{digits}f}]"


def _headline_cell(r: dict[str, Any]) -> str:
    h = r["headline"]
    if h["computed"] is None:
        return NM
    if r["E"]["flags"]:
        return f"{NM} (reader check)"
    s = f"{h['computed']:.1f} {_ci(h['interval'] if h['interval'] is not None else None, 1)}".strip()
    if h["ranked"] and h.get("band"):
        return f"{s} band {h['band']['ranks']}"
    return f"{s} unranked"


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


DETAIL_HEAD = ["model", "harness", "headline", "R", "E", "S", "controls R/E/S", "capability", "no-conflict", "unanswerable/ep",
               "private ids/ep", "wasted wait", "reach", "suspected eval"]


def _detail(r: dict[str, Any]) -> list[str]:
    c = r["controls"]
    ctl = " / ".join(_pct(c[f]["rate"]) + ("" if r["eligibility"][f]["eligible"] else "*") for f in FAMILIES)
    nc = r["no_conflict_control"]
    rp = r["reported"]
    return [r["model"], r["harness"], _headline_cell(r),
            f"{_f(r['R']['value'])} {_ci(r['R']['interval'], 3)} n={r['R']['pairs']}".replace("  ", " "),
            f"{_f(r['E']['value'])} {_ci(r['E']['interval'], 3)} n={len(r['E']['episodes'])}".replace("  ", " ") + (" (flagged)" if r["E"]["flags"] else ""),
            f"{_f(r['S']['value'])} {_ci(r['S']['interval'], 3)} n={len(r['S']['episodes'])}".replace("  ", " "),
            ctl, _f(r["capability"]), _pct(nc["rate"]) + f" n={nc['judged']}",
            _f(rp["unanswerable_per_episode"], 2), _f(rp["private_identifiers_per_episode"], 2), _pct(rp["wasted_wait_share"]),
            _pct(r["event_reach"]["share"]), _pct(r["awareness"]["share_suspected"])]


def render(rep: dict[str, Any], markdown: bool) -> str:
    grid = _md if markdown else _grid
    h1, h2, h3 = ("# ", "## ", "### ") if markdown else ("", "== ", "-- ")
    p = rep["parameters"]
    L = [f"{h1}Coherency results", "",
         f"Bootstrap: {p['resamples']} resamples of seeds within each family, sibling pairs kept together, seed {p['seed']}, "
         f"{100 * p['level']:.0f}% percentile intervals. Eligibility bar: {100 * p['bar']:.0f}% of a family's controls. "
         f"Episodes scored: {rep['inputs']['scored']}; in the index but not scored: {len(rep['inputs']['indexed_not_scored'])}.", ""]
    for n in rep["notes"]:
        L += [n, ""]
    for sec in rep["sections"]:
        L += [f"{h2}Polling interval {sec['poll_s']} s ({sec['episodes']} episodes)", ""]
        for k, s in sec["reader_sanity"].items():
            L.append(f"Reader {s['reader']['kind']} {k[:12]}: no answer {_pct(s['no_answer']['accuracy'])} of {s['no_answer']['n']} "
                     f"(chance 16.7%), buried cause {_pct(s['buried']['accuracy'])} of {s['buried']['n']}: "
                     + ("both sanity checks pass." if s["passed"] else "a sanity check FAILS; E from this reader is not used in the headline."))
        if sec["reader_sanity"]:
            L.append("")
        for t in sec["tables"]:
            L += [f"{h3}Horizon {t['horizon']}, mode {t['mode']}", ""]
            models = sorted({r["model"] for r in t["rows"]})
            harnesses = sorted({r["harness"] for r in t["rows"]})
            m = [["model \\ harness", *harnesses]]
            for mo in models:
                line = [mo]
                for ha in harnesses:
                    r = next((x for x in t["rows"] if x["model"] == mo and x["harness"] == ha), None)
                    line.append("no runs" if r is None else _headline_cell(r))
                m.append(line)
            L += grid(m) + [""]
            L += grid([DETAIL_HEAD, *[_detail(r) for r in t["rows"]]]) + [""]
            for r in t["rows"]:
                if r["headline"]["unranked_because"]:
                    L.append(f"{r['model']} in {r['harness']}: unranked, " + "; ".join(r["headline"]["unranked_because"]) + ".")
            rc = t["rank_correlation"]
            L.append(f"Rank correlation of the headline with the capability composite: "
                     + (f"{rc['spearman']:.3f} over {rc['n']} configurations." if rc["spearman"] is not None
                        else f"{NM} ({rc['n']} configuration{'' if rc['n'] == 1 else 's'} with both; it needs three)."))
            L += [""]
            pr = [["model", "harness", "knew, right", "knew, wrong", "did not know, right", "did not know, wrong", "not judged"]]
            for r in t["rows"]:
                q = r["probes"]
                pr.append([r["model"], r["harness"], *(str(q[k]) for k in ("knew_right", "knew_wrong", "not_knew_right", "not_knew_wrong", "not_judged"))])
            L += [f"{h3}Probes, horizon {t['horizon']}, mode {t['mode']}" if markdown else "Probes:", ""] + grid(pr) + [""]
        if sec["horizon_drop"]:
            L += [f"{h3}Horizon drop" if markdown else "Horizon drop (shorter minus longer):", ""]
            d = [["model", "harness", "mode", "from", "to", "headline", "R", "E", "S"]]
            for r in sec["horizon_drop"]:
                d.append([r["model"], r["harness"], r["mode"], r["from"], r["to"],
                          *(f"{_f(r[k]['value'], 1 if k == 'headline' else 3)} {_ci(r[k]['interval'], 1 if k == 'headline' else 3)}".strip() for k in ("headline", "R", "E", "S"))])
            L += grid(d) + [""]
    L += ["A * after a control rate marks a family in which the configuration is below the eligibility bar.",
          f"\"{NM}\" means there was no scored episode or no defined value for that number; it is never a zero."]
    return "\n".join(L).rstrip() + "\n"


def write(rep: dict[str, Any], out: Path) -> tuple[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    text, md = render(rep, False), render(rep, True)
    (out / "report.json").write_text(score_dumps(rep), encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.txt").write_text(text, encoding="utf-8")
    return text, md


__all__ = ["build", "render", "write", "ReportError", "tidy"]
