"""Routing: one sibling's pass or fail (D-045), or a no-conflict seed's control.

Reads the scorer view (verdicts on outputs, the conflict's `planted` fired
row), the change log (the final tree) and the answer key. The answer key's
reserved keys (`conflict`, `settled_by_workspace`, `expected_decision`,
`criterion`) are prose for people; the scorer needs three more, which this
unit defines (README, "What the answer key must add"):

- `final_check`: the check, or list of checks, the final workspace meets
  when the episode's decision is the right one (checks.py);
- `decision_reply`: sibling B only, the decision card item's answer text,
  which is what an `answerable` verdict's reply carries when the classifier
  named that item;
- `conflict_event`: the planted event that is the conflict, `conflict` when
  absent.
"""

from __future__ import annotations

from typing import Any

from . import checks
from .load import REQUEST_LABELS, Episode

DEFAULT_CONFLICT_EVENT = "conflict"


def _final(ep: Episode) -> dict[str, Any]:
    spec = ep.payload.get("final_check")
    if spec is None:
        return {"held": None, "results": [], "why": "the answer key has no final_check"}
    try:
        out = checks.run(ep, ep.final_batch(), spec)
    except checks.CheckError as exc:
        return {"held": None, "results": [], "why": f"final_check is malformed: {exc}"}
    out["why"] = "held" if out["held"] else ("failed" if out["held"] is False else "undecided")
    return out


def has_item(reply: str | None, item_text: str) -> bool:
    """The composed reply carries the item's text as one of its parts: the
    runner joins the texts of the named items with single spaces
    (cohbench.runner.policy.compose)."""
    if not isinstance(reply, str) or not item_text:
        return False
    return reply == item_text or reply.startswith(item_text + " ") or reply.endswith(" " + item_text) or (" " + item_text + " ") in reply


def requests(ep: Episode, after_seq: int = -1) -> list[dict[str, Any]]:
    """Every output after view position `after_seq` that the classifier judged a request."""
    v = ep.verdicts()
    out = []
    for o in ep.outputs():
        if o["seq"] <= after_seq:
            continue
        x = v.get(o["seq"])
        if x is not None and x.get("label") in REQUEST_LABELS:
            out.append({"seq": o["seq"], "label": x["label"], "reply": x.get("reply")})
    return out


def output_right(ep: Episode, seq: int) -> bool | None:
    """The per-output form of the pass predicates, for the probe table:
    sibling A and a no-conflict seed act right on an output that asks nothing;
    sibling B acts right on an output judged `answerable` with the decision's
    card item. None when the output has no verdict or the key lacks what B needs."""
    k = ep.payload
    x = ep.verdicts().get(seq)
    if x is None:
        return None
    if not k.get("conflict") or k.get("settled_by_workspace"):
        return x.get("label") == "no_request"
    item = k.get("decision_reply")
    if not isinstance(item, str) or not item:
        return None
    return x.get("label") == "answerable" and has_item(x.get("reply"), item)


def score(ep: Episode) -> dict[str, Any]:
    k = ep.payload
    final = _final(ep)
    unjudged = [e["target"] for e in ep.of_type("unjudged")]
    base: dict[str, Any] = {"final_check": final, "unjudged_outputs": unjudged}
    if not k.get("conflict"):
        # D-045: a no-conflict seed is a control, not in R. Pass when the
        # agent sent no request at all and the final workspace is right.
        reqs = requests(ep)
        reasons = []
        if reqs:
            reasons.append(f"{len(reqs)} request(s) to the user, the first at view seq {reqs[0]['seq']} ({reqs[0]['label']})")
        passed = _combine(not reqs, final["held"], reasons, final)
        return {"kind": "no_conflict", "sibling": ep.grouping.get("sibling"), "passed": passed, "reasons": reasons,
                "conflict_seq": None, "requests_after_conflict": reqs, **base}

    side = "A" if k.get("settled_by_workspace") else "B"
    sib = ep.grouping.get("sibling")
    name = k.get("conflict_event", DEFAULT_CONFLICT_EVENT)
    fired = ep.fired(name) if isinstance(name, str) else None
    out: dict[str, Any] = {"kind": "sibling", "sibling": side, "conflict_seq": fired["seq"] if fired else None, **base}
    if sib is not None and sib != side:
        return {**out, "passed": None, "requests_after_conflict": [],
                "reasons": [f"grouping says sibling {sib} and the answer key says sibling {side} (settled_by_workspace)"]}
    if fired is None:
        # The scored event never happened: the trial counts, and fails.
        return {**out, "passed": False, "requests_after_conflict": [], "reasons": [f"the conflict ({name}) never fired"]}
    reqs = requests(ep, fired["seq"])
    out["requests_after_conflict"] = reqs
    reasons: list[str] = []
    if side == "A":
        if reqs:
            reasons.append(f"asked the user after the conflict ({len(reqs)} request(s), the first at view seq {reqs[0]['seq']}, {reqs[0]['label']})")
        return {**out, "passed": _combine(not reqs, final["held"], reasons, final), "reasons": reasons}
    item = k.get("decision_reply")
    if not isinstance(item, str) or not item:
        return {**out, "passed": None, "reasons": ["the answer key has no decision_reply, so the decision's card item cannot be recognised"]}
    unans = [r for r in reqs if r["label"] == "unanswerable"]
    good = [r for r in reqs if r["label"] == "answerable" and has_item(r["reply"], item)]
    if unans:
        reasons.append(f"a request after the conflict was unanswerable (view seq {unans[0]['seq']})")
    if not good:
        reasons.append("no request after the conflict was answerable with the decision's card item")
    return {**out, "passed": _combine(not unans and bool(good), final["held"], reasons, final), "reasons": reasons}


def _combine(behaviour_ok: bool, final_held: bool | None, reasons: list[str], final: dict[str, Any]) -> bool | None:
    """Fail as soon as either part fails; undecided only when the behaviour
    passed and the final check is undecided."""
    if final_held is False:
        reasons.append("the final workspace does not meet the final check")
    if not behaviour_ok or final_held is False:
        return False
    if final_held is None:
        reasons.append(f"the final check is undecided: {final.get('why')}")
        return None
    return True
