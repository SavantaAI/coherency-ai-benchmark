"""Command line for the simulated user's classifier.

    python -m cohbench.user label ITEMS... --out LABELS [--seed N]
    python -m cohbench.user run ITEMS... --config CONFIG --out VERDICTS [--key-file FILE] [--order SEED]
    python -m cohbench.user agree FIRST SECOND --items ITEMS... [--json]
    python -m cohbench.user check ITEMS... [--labels LABELS]

ITEMS is item files or directories of them. `run` calls the endpoint the
config names, with the API key from `--key-file` or the file named by
COHBENCH_CLASSIFIER_KEY_FILE. It skips items already in VERDICTS made with
the same config, so an interrupted run continues.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from .agree import compare, render
from .items import VERDICTS_FORMAT, ItemError, check_label, checked_entries, items_digest, load_items, read_answers, write_json


def _run(a: argparse.Namespace) -> int:
    from cohbench.runner.policy import PolicyError

    from .api import Client, read_key
    from .classifier import Classifier
    from .config import ConfigError, load
    from .revision import code_revision

    items = load_items(a.items)
    try:
        cfg = load(a.config)
        key = read_key(a.key_file)
    except (ConfigError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    digest = items_digest(items)
    out = Path(a.out)
    done: dict = {}
    if out.exists():
        kind, head, entries = read_answers(out)
        if (kind != "verdicts" or head.get("config") != cfg.digest or head.get("items") != digest or head.get("order") != a.order
                or head.get("code") != code_revision()):
            print(f"error: {out} holds results for another config, item set, --order or code revision; choose a new --out", file=sys.stderr)
            return 2
        done = checked_entries(entries, {i.id: i for i in items}, str(out))
    # The whole set in id order, shuffled when asked, then the finished ones
    # skipped: a resumed run goes on in the order it started with.
    order = sorted(items, key=lambda i: i.id)
    if a.order is not None:
        random.Random(a.order).shuffle(order)
    order = [i for i in order if i.id not in done]
    client = Client(cfg, key)
    try:
        for n, it in enumerate(order, 1):
            t0 = time.monotonic()
            # A fresh policy for every item: an item is a moment of some episode, not a
            # continuation of the previous item, so no verdict's memory may carry over.
            j = Classifier(cfg, client).judge(it.card, it.transcript, it.output)
            entry = {"label": j.label, "answer_ids": list(j.answer_ids), "quote_of": j.quote_of, "quote": j.quote, "record": dict(j.record)}
            problems = check_label(entry, it)
            if problems:  # never write a verdict the runner could not use
                print("error: the classifier returned an invalid verdict: " + "; ".join(problems), file=sys.stderr)
                return 1
            done[it.id] = entry
            write_json(out, {"format": VERDICTS_FORMAT, "version": 1, "items": digest, "config": cfg.digest, "order": a.order,
                             "code": code_revision(),
                             "verdicts": {k: done[k] for k in sorted(done)}})
            extra = f" {','.join(j.answer_ids)}" if j.answer_ids else (f" [{j.quote_of + 1}]" if j.quote_of is not None else "")
            print(f"{n:>3}/{len(order)} {it.id}: {j.label}{extra}  {time.monotonic() - t0:.1f} s", flush=True)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _agree(a: argparse.Namespace) -> int:
    items = load_items(a.items)
    digest = items_digest(items)
    sides = []
    for name in (a.first, a.second):
        kind, head, entries = read_answers(Path(name))
        if head.get("items") != digest:
            print(f"error: {name} was made for a different item set than the items given "
                  f"({str(head.get('items'))[:12]} against {digest[:12]})", file=sys.stderr)
            return 2
        sides.append((kind, entries))
    (ka, first), (kb, second) = sides
    by_id = {i.id: i for i in items}
    first = checked_entries(first, by_id, a.first)
    second = checked_entries(second, by_id, a.second)
    if not set(first) & set(second):
        print(f"error: no item is labelled on both sides ({len(first)} and {len(second)} entries)", file=sys.stderr)
        return 2
    rep = compare(items, first, second, whole=(ka == kb == "verdicts"))
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print(render(rep, items, (Path(a.first).stem + f" ({ka})", Path(a.second).stem + f" ({kb})")))
    return 0


def _check(a: argparse.Namespace) -> int:
    items = load_items(a.items)
    print(f"{len(items)} items, digest {items_digest(items)[:12]}")
    if a.labels:
        _, head, entries = read_answers(Path(a.labels))
        by_id = {i.id: i for i in items}
        problems = [f"{k}: not an item" for k in entries if k not in by_id]
        problems += [p for k, v in entries.items() if k in by_id for p in check_label(v, by_id[k])]
        if head.get("items") not in (None, items_digest(items)):
            problems.append("the labels file names a different item set")
        for p in problems:
            print(p)
        print(f"{len(entries)} labels, {len(problems)} problems")
        return 1 if problems else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cohbench.user", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lab = sub.add_parser("label", help="label items by hand")
    lab.add_argument("items", nargs="+")
    lab.add_argument("--out", required=True)
    lab.add_argument("--seed", type=int, default=None, help="shuffle seed for a new labels file (default 1)")
    run = sub.add_parser("run", help="run the classifier over items")
    run.add_argument("items", nargs="+")
    run.add_argument("--config", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--key-file", default=None, help="file holding the API key (default: $COHBENCH_CLASSIFIER_KEY_FILE)")
    run.add_argument("--order", type=int, default=None, help="run the items in an order shuffled by this seed")
    ag = sub.add_parser("agree", help="agreement between two labels or verdicts files")
    ag.add_argument("first")
    ag.add_argument("second")
    ag.add_argument("--items", nargs="+", required=True)
    ag.add_argument("--json", action="store_true")
    ck = sub.add_parser("check", help="validate items and a labels file")
    ck.add_argument("items", nargs="+")
    ck.add_argument("--labels", default=None)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "label":
            from .label import main as label_main

            return label_main(load_items(a.items), Path(a.out), a.seed)
        if a.cmd == "run":
            return _run(a)
        if a.cmd == "agree":
            return _agree(a)
        return _check(a)
    except ItemError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
