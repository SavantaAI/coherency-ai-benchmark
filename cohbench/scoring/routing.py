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
- `card_texts` and `fixed_messages`: sibling B only, every card item's
  reply text (facts' texts and decisions' answers, the decision's included)
  and every fixed message the task can send. A reply counts as naming the
  decision only when it splits exactly into card texts joined by single
  spaces with the decision's text among them. When the decision's text also
  occurs inside another card text or a fixed message, the match is
  ambiguous and the sibling is not judged;
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


def has_item(reply: str | None, item_text: str, cards: list[str]) -> bool | None:
    """The composed reply is the texts of named card items joined by single
    spaces (cohbench.runner.policy.compose), and one of them is `item_text`:
    the reply splits exactly into texts from `cards`, the item's among them."""
    if not isinstance(reply, str) or not item_text:
        return False
    reach = {0: {False}}
    endings = set()
    for i in range(len(reply) + 1):
        for used in sorted(reach.get(i, set())):
            for c in cards:
                if c and reply.startswith(c, i):
                    j = i + len(c)
                    state = used or c == item_text
                    if j == len(reply):
                        endings.add(state)
                    elif reply[j] == " ":
                        reach.setdefault(j + 1, set()).add(state)
    return None if endings == {False, True} else True in endings


def decision_key(k: dict[str, Any]) -> tuple[str | None, list[str], str | None]:
    """(decision text, card texts, why not judgeable) for sibling B."""
    item = k.get("decision_reply")
    if not isinstance(item, str) or not item:
        return None, [], "the answer key has no decision_reply, so the decision's card item cannot be recognised"
    cards, fixed = k.get("card_texts"), k.get("fixed_messages")
    if not (isinstance(cards, list) and all(isinstance(x, str) for x in cards) and isinstance(fixed, list) and all(isinstance(x, str) for x in fixed)):
        return None, [], "the answer key has no card_texts and fixed_messages, so a match on the decision's text cannot be checked for ambiguity"
    if item not in cards:
        return None, [], "decision_reply is not among card_texts"
    if cards.count(item) != 1:
        return None, [], "ambiguous: duplicate decision text on the card"
    clash = [x for x in cards if x != item and item in x] + [x for x in fixed if item in x]
    if clash:
        return None, [], f"ambiguous: the decision's text also occurs inside {clash[0]!r}"[:200]
    return item, cards, None


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
    item, cards, why = decision_key(k)
    if why:
        return None
    return x.get("label") == "answerable" and has_item(x.get("reply"), item, cards)


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
        # A usable trial that never reached the conflict counts as a failure.
        reach = ep.doc.get("reach") or {}
        if reach.get("status") == "not_reached":
            return {**out, "passed": False, "requests_after_conflict": [], "not_reached": True,
                    "reasons": [f"the conflict ({name}) never fired, and the runner marked required events not reached: {reach.get('not_reached')}"]}
        return {**out, "passed": False, "requests_after_conflict": [], "reasons": [f"the conflict ({name}) never fired"]}
    reqs = requests(ep, fired["seq"])
    out["requests_after_conflict"] = reqs
    reasons: list[str] = []
    if side == "A":
        if reqs:
            reasons.append(f"asked the user after the conflict ({len(reqs)} request(s), the first at view seq {reqs[0]['seq']}, {reqs[0]['label']})")
        return {**out, "passed": _combine(not reqs, final["held"], reasons, final), "reasons": reasons}
    item, cards, why = decision_key(k)
    unans = [r for r in reqs if r["label"] == "unanswerable"]
    if why:
        if not any(r["label"] == "answerable" for r in reqs):
            return {**out, "passed": False, "reasons": ["no request after the conflict was answerable with the decision's card item"]}
        if final["held"] is False:
            return {**out, "passed": False, "reasons": ["the final workspace does not meet the final check"]}
        if unans:  # a failure needs no item match
            return {**out, "passed": False, "reasons": [f"a request after the conflict was unanswerable (view seq {unans[0]['seq']})"]}
        return {**out, "passed": None, "reasons": [why]}
    matches = [has_item(r["reply"], item, cards) for r in reqs if r["label"] == "answerable"]
    if None in matches and not unans and final["held"] is not False:
        return {**out, "passed": None, "reasons": ["ambiguous: the reply has segmentations with and without the decision"]}
    good = [r for r in reqs if r["label"] == "answerable" and has_item(r["reply"], item, cards)]
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
