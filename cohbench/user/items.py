"""Calibration files: items, labels and verdicts.

An item is one JSON document holding exactly what the classifier sees:

    {"format": "cohbench-classifier-item", "version": 1, "id": "dev-001",
     "card": {"facts": [...], "decisions": [...]},
     "transcript": [{"role": "user", "text": "..."}, {"role": "agent", "text": "..."}],
     "output": "the agent message to judge"}

`card` is the card as `judge` receives it: only the items available at that
moment, without `available_after`. A directory of items is every `*.json`
file in it.

Labels and verdicts live in separate files, so a labeller sees neither the
classifier's verdict nor another labeller's:

    {"format": "cohbench-classifier-labels", "version": 1, "items": "<digest of the item set>",
     "seed": 7, "labels": {"dev-001": {"label": "answerable", "answer_ids": ["slot"], "quote_of": null}}}

    {"format": "cohbench-classifier-verdicts", "version": 1, "items": "...", "config": "<config digest>",
     "verdicts": {"dev-001": {"label": ..., "answer_ids": [...], "quote_of": ..., "quote": ..., "record": {...}}}}

`quote_of` is the 0-based index of the quoted user message in the
transcript, as in `Judgement`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from cohbench.runner.policy import AGENT, LABELS, USER, Entry

ITEM_FORMAT = "cohbench-classifier-item"
LABELS_FORMAT = "cohbench-classifier-labels"
VERDICTS_FORMAT = "cohbench-classifier-verdicts"


class ItemError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    id: str
    card: Mapping[str, Any]
    transcript: tuple[Entry, ...]
    output: str
    doc: Mapping[str, Any]

    def card_ids(self) -> list[str]:
        return [x["id"] for k in ("facts", "decisions") for x in self.card.get(k, [])]

    def user_indices(self) -> list[int]:
        return [i for i, e in enumerate(self.transcript) if e.role == USER]


def check_card(card: Any, where: str) -> list[str]:
    e: list[str] = []
    if not isinstance(card, Mapping) or set(card) - {"facts", "decisions", "notes"}:
        return [f"{where}: an object with facts, decisions and optional notes"]
    ids: list[str] = []
    for k in ("facts", "decisions"):
        if k in card and not isinstance(card[k], list):
            e.append(f"{where}.{k}: a list")
    if e:
        return e
    for f in card.get("facts", []):
        if not isinstance(f, Mapping) or set(f) != {"id", "text"} or not all(isinstance(f[k], str) and f[k] for k in f):
            e.append(f"{where}.facts: each {{id, text}}")
        else:
            ids.append(f["id"])
    for d in card.get("decisions", []):
        if not isinstance(d, Mapping) or set(d) != {"id", "question", "answer"} or not all(isinstance(d[k], str) and d[k] for k in d):
            e.append(f"{where}.decisions: each {{id, question, answer}} (no available_after: the card as judge receives it)")
        else:
            ids.append(d["id"])
    if len(ids) != len(set(ids)):
        e.append(f"{where}: item ids repeat")
    if "notes" in card:
        n = card["notes"]
        if not isinstance(n, list) or not all(isinstance(x, str) and x.strip() for x in n):
            e.append(f"{where}.notes: a list of texts (the notes available at that moment, as judge receives them)")
    return e


def parse_item(doc: Any, where: str = "item") -> Item:
    if not isinstance(doc, Mapping):
        raise ItemError(f"{where}: an object")
    e: list[str] = []
    if doc.get("format") != ITEM_FORMAT or doc.get("version") != 1:
        e.append(f"{where}: format {ITEM_FORMAT!r}, version 1")
    if set(doc) - {"format", "version", "id", "card", "transcript", "output"}:
        e.append(f"{where}: unknown fields {sorted(set(doc) - {'format', 'version', 'id', 'card', 'transcript', 'output'})}")
    if not isinstance(doc.get("id"), str) or not doc["id"]:
        e.append(f"{where}.id: text")
    e += check_card(doc.get("card"), f"{where}.card")
    tr = doc.get("transcript")
    if not isinstance(tr, list) or not all(isinstance(x, Mapping) and set(x) == {"role", "text"} and x["role"] in (USER, AGENT)
                                           and isinstance(x["text"], str) for x in tr):
        e.append(f"{where}.transcript: a list of {{role: user|agent, text}}")
    if not isinstance(doc.get("output"), str) or not doc["output"]:
        e.append(f"{where}.output: text")
    if e:
        raise ItemError("\n".join(e))
    return Item(doc["id"], doc["card"], tuple(Entry(x["role"], x["text"]) for x in tr), doc["output"], doc)


def item_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return out


def load_items(paths: Iterable[str | os.PathLike[str]]) -> list[Item]:
    items = []
    for p in item_paths(paths):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ItemError(f"{p}: {exc}") from None
        items.append(parse_item(doc, p.name))
    ids = [i.id for i in items]
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        raise ItemError(f"item ids repeat: {dup}")
    if not items:
        raise ItemError("no items found")
    return items


def items_digest(items: Iterable[Item]) -> str:
    """sha256 over the item documents, in id order: labels and verdicts name the set they belong to."""
    docs = sorted((dict(i.doc) for i in items), key=lambda d: d["id"])
    return hashlib.sha256(json.dumps(docs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def check_label(lab: Any, item: Item) -> list[str]:
    """A label or verdict entry against its item: field types, card item ids
    (a list without repeats, only for answerable), the quoted message (an
    earlier user message, only for already_given) and the quoted span (None,
    or a non-empty part of that message)."""
    w = f"{item.id}"
    if not isinstance(lab, Mapping) or not isinstance(lab.get("label"), str) or lab["label"] not in LABELS:
        return [f"{w}: label one of {LABELS}"]
    e: list[str] = []
    ids = lab.get("answer_ids", [])
    if ids is None:
        ids = []
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        return [f"{w}: answer_ids is a list of card item ids"]
    if len(ids) != len(set(ids)):
        e.append(f"{w}: answer_ids repeats an item")
    if lab["label"] == "answerable":
        if not ids or not all(x in item.card_ids() for x in ids):
            e.append(f"{w}: answerable names card items of this item ({item.card_ids()})")
    elif ids:
        e.append(f"{w}: answer_ids only for answerable")
    q = lab.get("quote_of")
    span = lab.get("quote")
    if q is not None and (not isinstance(q, int) or isinstance(q, bool)):
        return e + [f"{w}: quote_of is a message index or null"]
    if lab["label"] == "already_given":
        if q not in item.user_indices():
            e.append(f"{w}: already_given names an earlier user message ({item.user_indices()})")
        elif span is None and not item.transcript[q].text:
            e.append(f"{w}: already_given quotes message {q + 1}, which is empty (the runner cannot quote it)")
        elif span is not None and (not isinstance(span, str) or not span or span not in item.transcript[q].text):
            e.append(f"{w}: quote is null or a non-empty part of message {q + 1}")
    else:
        if q is not None:
            e.append(f"{w}: quote_of only for already_given")
        if span is not None:
            e.append(f"{w}: quote only for already_given")
    return e


def normal(lab: Mapping[str, Any], item: Item) -> dict[str, Any]:
    """A checked entry in one form: card ids as a list, and a quoted span equal
    to the whole message written as None (the runner quotes the whole message
    either way)."""
    q = lab.get("quote_of")
    span = lab.get("quote")
    if q is not None and span is not None and span == item.transcript[q].text:
        span = None
    return {**lab, "answer_ids": list(lab.get("answer_ids") or []), "quote_of": q, "quote": span}


def checked_entries(entries: Mapping[str, Any], items: Mapping[str, Item], name: str) -> dict[str, dict[str, Any]]:
    """Every entry of a labels or verdicts file, checked and normalised; ItemError otherwise."""
    problems = [f"{k}: not an item" for k in entries if k not in items]
    problems += [p for k, v in entries.items() if k in items for p in check_label(v, items[k])]
    if problems:
        raise ItemError(f"{name}:\n  " + "\n  ".join(problems[:20]))
    return {k: normal(v, items[k]) for k, v in entries.items()}


def write_json(path: Path, doc: Any) -> None:
    """Atomically, so an interrupted write never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_answers(path: Path) -> tuple[str, Mapping[str, Any], Mapping[str, Mapping[str, Any]]]:
    """A labels or verdicts file: (kind, header, entries by item id)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ItemError(f"{path}: {exc}") from None
    if not isinstance(doc, Mapping) or doc.get("version") != 1:
        raise ItemError(f"{path}: not a labels or verdicts file")
    if doc.get("format") == LABELS_FORMAT and isinstance(doc.get("labels"), Mapping):
        return "labels", {k: v for k, v in doc.items() if k != "labels"}, doc["labels"]
    if doc.get("format") == VERDICTS_FORMAT and isinstance(doc.get("verdicts"), Mapping):
        return "verdicts", {k: v for k, v in doc.items() if k != "verdicts"}, doc["verdicts"]
    raise ItemError(f"{path}: not a labels or verdicts file")
