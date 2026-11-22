"""Explanation: the reader's accuracy on the first 80 words of each of the two
answers, over a fixed, seeded set of option orders, and the two sanity checks
of the spec, both computed for every episode.

Reads `episode.json` (`answers`, the answer key's `options`, `true_cause` and
`buried_control`) and the scorer view (the opening question and the answers'
text). An answer never given is wrong and stays in the denominator (D-034).
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import reader as R
from .load import Episode
from .rng import option_orders

CHANCE = 1 / 6


def read_items(rd: R.Reader, cfg: R.Config, question: str, excerpt: str, options: list[str], true_cause: int) -> list[dict[str, Any]]:
    count, seed = cfg.orders
    out = []
    for n, order in enumerate(option_orders(count, seed)):
        shown = [options[j] for j in order]
        pos = rd.choose(question, excerpt, shown)
        if not (isinstance(pos, int) and 0 <= pos < len(shown)):
            raise R.ReaderError("the reader returned a position outside the options")
        out.append({"order": n, "shown": order, "picked": pos, "chose": order[pos], "correct": order[pos] == true_cause})
    return out


def input_key(question: str, excerpt: str, options: list[str], true_cause: int) -> str:
    return hashlib.sha256(R.canonical({"question": question, "excerpt": excerpt, "options": options, "true_cause": true_cause}).encode("utf-8")).hexdigest()


def _accuracy(items: list[dict[str, Any]]) -> float:
    return sum(1 for x in items if x["correct"]) / len(items) if items else 0.0


def answer_texts(ep: Episode) -> list[dict[str, Any]]:
    """The two scored answers, as far as the episode got: {which, seq, text}."""
    seqs = list(ep.doc.get("answers") or [])
    out = []
    for i in range(2):
        seq = seqs[i] if i < len(seqs) else None
        ev = ep.by_seq(seq) if isinstance(seq, int) else None
        text = ev.get("text") if ev is not None and ev.get("type") == "to_user" else None
        out.append({"which": i + 1, "seq": seq, "text": text})
    return out


def score(ep: Episode, rd: R.Reader | None, cfg: R.Config | None) -> dict[str, Any]:
    k = ep.payload
    options, true_cause = k["options"], k["true_cause"]
    question = ep.opening_question()
    answers = answer_texts(ep)
    if rd is None or cfg is None:
        return {"accuracy": None, "why": "no reader was given", "answers": [
            {"which": a["which"], "seq": a["seq"], "given": a["text"] is not None} for a in answers], "sanity": None, "reader": None}
    rows = []
    for a in answers:
        if a["text"] is None:
            rows.append({"which": a["which"], "seq": a["seq"], "given": False, "excerpt": None, "words": 0, "accuracy": 0.0, "items": [],
                         "why": "never given: counts as wrong (D-034)"})
            continue
        excerpt, words = R.window(a["text"])
        items = read_items(rd, cfg, question, excerpt, options, true_cause)
        rows.append({"which": a["which"], "seq": a["seq"], "given": True, "excerpt": excerpt, "words": words, "accuracy": _accuracy(items), "items": items})
    bc = k["buried_control"]
    buried_excerpt, _ = R.window(bc["filler"] + " " + bc["sentence"])
    none_items = read_items(rd, cfg, question, "", options, true_cause)
    buried_items = read_items(rd, cfg, question, buried_excerpt, options, true_cause)
    # Each sanity item is named by its inputs, so the report counts identical inputs once.
    sanity = {
        "no_answer": {"key": input_key(question, "", options, true_cause), "correct": sum(x["correct"] for x in none_items),
                      "n": len(none_items), "items": none_items},
        "buried": {"key": input_key(question, buried_excerpt, options, true_cause), "correct": sum(x["correct"] for x in buried_items),
                   "n": len(buried_items), "excerpt": buried_excerpt, "items": buried_items},
    }
    acc = (rows[0]["accuracy"] + rows[1]["accuracy"]) / 2
    return {"accuracy": acc, "answers": rows, "sanity": sanity, "reader": rd.describe()}


def answer_accuracy(ep: Episode, which: int, rd: R.Reader, cfg: R.Config) -> float | None:
    """One answer's accuracy over the orders, for controls; None when it was never given."""
    a = answer_texts(ep)[which - 1]
    if a["text"] is None:
        return None
    excerpt, _ = R.window(a["text"])
    return _accuracy(read_items(rd, cfg, ep.opening_question(), excerpt, ep.payload["options"], ep.payload["true_cause"]))
