"""Eligibility controls (spec section 7, D-044): explicit-instruction episodes
judged by their own expected values, `episode.json` `control.expected`.

`expected` is an object whose keys this unit defines; every key present must
hold:

    "final":       a check or list of checks on the final workspace (checks.py)
    "requests":    "none" (no output judged a request), "no_unanswerable"
                   (none judged unanswerable), or "answerable" (at least one
                   judged answerable and none unanswerable)
    "final_metric": {"name": metric, "op": "<" | "<=" | "==" | ">=" | ">", "value": number},
                   on the reading of the final batch
    "answer":      {"which": "first" | "second" | "last", "regex": pattern}: the
                   output that closed that hand-back matches (case ignored)
    "reader_true_cause": {"which": 1 | 2}: the reader picks the true cause in
                   more than half of the option orders for that Explanation answer

A key this list does not name, a malformed value, or a part that cannot be
decided leaves the control not judged (`passed: null`), never guessed. A
part that fails makes the control fail even when another is undecided.
"""

from __future__ import annotations

import re
from typing import Any

from cohbench.trajectory import episode as TE

from . import checks, explanation, routing
from . import reader as R
from .load import REQUEST_LABELS, Episode

KEYS = ("final", "requests", "final_metric", "answer", "reader_true_cause")
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, ">=": lambda a, b: a >= b, ">": lambda a, b: a > b}


def closing_outputs(ep: Episode) -> list[int | None]:
    return [h.get("target") if h.get("type") == "handback" else h.get("closed_by") for h in TE.handbacks(ep.view)]


def _part(ep: Episode, key: str, v: Any, rd: R.Reader | None, cfg: R.Config | None) -> tuple[bool | None, str]:
    if key == "final":
        try:
            r = checks.run(ep, ep.final_batch(), v)
        except checks.CheckError as exc:
            return None, f"malformed: {exc}"
        return r["held"], "; ".join(x["why"] for x in r["results"])
    if key == "requests":
        labels = [e.get("label") for e in ep.of_type("verdict")]
        missing = routing.missing_verdicts(ep)
        known_failure = any(x in REQUEST_LABELS for x in labels) if v == "none" else "unanswerable" in labels
        if v in ("none", "no_unanswerable", "answerable") and missing and not known_failure:
            return None, f"delivered outputs have no verdict: {missing}"
        if v == "none":
            n = sum(1 for x in labels if x in REQUEST_LABELS)
            return n == 0, f"{n} request(s)"
        if v == "no_unanswerable":
            n = labels.count("unanswerable")
            return n == 0, f"{n} unanswerable"
        if v == "answerable":
            return (labels.count("answerable") > 0 and labels.count("unanswerable") == 0), f"{labels.count('answerable')} answerable, {labels.count('unanswerable')} unanswerable"
        return None, f"unknown requests value {v!r}"
    if key == "final_metric":
        if not (isinstance(v, dict) and set(v) == {"name", "op", "value"} and v["op"] in OPS and isinstance(v["value"], (int, float))):
            return None, "malformed final_metric"
        fb = ep.final_batch()
        m = ep.reading(fb) if fb is not None else None
        x = m.get(v["name"]) if isinstance(m, dict) else None
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None, "the final batch has no reading of that metric"
        return OPS[v["op"]](x, v["value"]), f"{v['name']} = {x}"
    if key == "answer":
        if not (isinstance(v, dict) and set(v) == {"which", "regex"} and v["which"] in ("first", "second", "last") and isinstance(v["regex"], str)):
            return None, "malformed answer"
        outs = closing_outputs(ep)
        idx = {"first": 0, "second": 1, "last": len(outs) - 1}[v["which"]]
        seq = outs[idx] if 0 <= idx < len(outs) else None
        ev = ep.by_seq(seq) if isinstance(seq, int) else None
        if ev is None:
            return False, f"no {v['which']} answer was given"
        try:
            hit = re.search(v["regex"], ev.get("text", ""), re.IGNORECASE | re.MULTILINE)
        except re.error:
            return None, "the regex does not compile"
        return bool(hit), "matches" if hit else "does not match"
    if key == "reader_true_cause":
        if not (isinstance(v, dict) and set(v) == {"which"} and v["which"] in (1, 2)):
            return None, "malformed reader_true_cause"
        if ep.family != "explanation":
            return None, "reader_true_cause needs an Explanation answer key"
        if rd is None or cfg is None:
            return None, "no reader was given"
        acc = explanation.answer_accuracy(ep, v["which"], rd, cfg)
        if acc is None:
            return False, "that answer was never given"
        return acc > 0.5, f"reader accuracy {acc:.3f} over the orders"
    return None, f"unknown expectation {key!r}"


def score(ep: Episode, rd: R.Reader | None = None, cfg: R.Config | None = None) -> dict[str, Any]:
    ctl = ep.doc.get("control") or {}
    exp = ctl.get("expected")
    if not isinstance(exp, dict) or not exp:
        return {"passed": None, "results": {}, "why": "the control has no expected values"}
    results = {}
    for key in sorted(exp):
        held, why = _part(ep, key, exp[key], rd, cfg)
        results[key] = {"held": held, "why": why}
    vals = [r["held"] for r in results.values()]
    passed = False if False in vals else (None if None in vals else True)
    return {"passed": passed, "results": results}
