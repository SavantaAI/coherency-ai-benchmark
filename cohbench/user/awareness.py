"""Evaluate awareness replies without exposing labels to the classifier.

python -m cohbench.user.awareness ITEMS --labels LABELS --config CONFIG --out VERDICTS

Items use the turn-item format, with an empty card and exactly one user
message containing the post-run question. The output is the reply to judge.
The command resumes matching records and prints JSON agreement and every miss.
"""

import argparse
import json
import sys
from pathlib import Path

from cohbench.runner.policy import AWARENESS, USER, PolicyError

from .agree import confusion, kappa
from .api import Client, read_key
from .classifier import Classifier
from .config import ConfigError, load
from .items import ItemError, items_digest, load_items, write_json
from .revision import code_revision

FORMAT = "cohbench-awareness-verdicts"


def read_object(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ItemError("cannot read awareness JSON file") from None
    if not isinstance(doc, dict):
        raise ItemError("awareness file must be an object")
    return doc


def outcomes(entries, allowed, *, complete=False):
    if not isinstance(entries, dict) or set(entries) - allowed:
        raise ItemError("awareness entries contain unknown ids or are not an object")
    if complete and set(entries) != allowed:
        raise ItemError("awareness labels must cover every item exactly")
    for value in entries.values():
        if not isinstance(value, dict) or value.get("outcome") not in AWARENESS:
            raise ItemError("invalid awareness outcome")
    return entries


def report(gold, verdicts):
    ids = sorted(set(gold) & set(verdicts))
    pairs = [(gold[i]["outcome"], verdicts[i]["outcome"]) for i in ids]
    result = kappa(pairs, AWARENESS)
    return {
        "n": result.n,
        "expected": len(gold),
        "observed": result.observed,
        "chance": result.chance,
        "kappa": result.kappa,
        "confusion": confusion(pairs, AWARENESS),
        "missing": sorted(set(gold) - set(verdicts)),
        "disagreements": [
            {"item": i, "given": verdicts[i]["outcome"], "expected": gold[i]["outcome"]}
            for i in ids
            if gold[i]["outcome"] != verdicts[i]["outcome"]
        ],
    }


def evaluate(args):
    items = load_items(args.items)
    for item in items:
        if (
            any(item.card.values())
            or len(item.transcript) != 1
            or item.transcript[0].role != USER
        ):
            raise ItemError(
                "awareness items need an empty card and exactly one user question"
            )
    digest = items_digest(items)
    ids = {item.id for item in items}
    labels = read_object(args.labels)
    if (
        labels.get("format")
        not in ("cohbench-awareness-labels", "cohbench-heldout-awareness-labels")
        or labels.get("version") != 1
        or labels.get("items") != digest
    ):
        raise ItemError("awareness labels have another version or item-set digest")
    gold = outcomes(labels.get("labels"), ids, complete=True)
    cfg = load(args.config)
    header = {
        "format": FORMAT,
        "version": 1,
        "items": digest,
        "config": cfg.digest,
        "code": code_revision(),
    }
    out = Path(args.out)
    verdicts = {}
    if out.exists():
        old = read_object(out)
        if any(old.get(k) != v for k, v in header.items()):
            raise ItemError(
                "awareness output has another config, item set or code revision"
            )
        verdicts = outcomes(old.get("verdicts"), ids)
    client = None
    try:
        for item in sorted(items, key=lambda i: i.id):
            if item.id in verdicts:
                continue
            if client is None:
                ledger = None
                if args.ledger:
                    from .ledger import Ledger

                    ledger = Ledger(args.ledger, args.max_calls)
                client = Client(cfg, read_key(args.key_file), ledger=ledger)
            judged = Classifier(cfg, client).classify_awareness(
                item.transcript[0].text, item.output
            )
            verdicts[item.id] = {
                "outcome": judged.outcome,
                "record": dict(judged.record),
            }
            write_json(out, {**header, "verdicts": verdicts})
    except PolicyError:
        if verdicts:
            print(json.dumps(report(gold, verdicts), indent=2))
        raise
    print(json.dumps(report(gold, verdicts), indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="+")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--key-file")
    parser.add_argument("--ledger")
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args(argv)
    if bool(args.ledger) != (args.max_calls is not None) or (
        args.max_calls is not None and args.max_calls <= 0
    ):
        parser.error("--ledger and a positive --max-calls must be supplied together")
    try:
        evaluate(args)
        return 0
    except (ConfigError, ItemError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
