"""Calculations over the retained local run, archived so the README's numbers
can be rechecked. Run from the repository root:

    python calibration/classifier/dev/local-run-2026-11-22/calculations.py

1. The p4 verdicts recomputed against the current development labels. The
   verdicts were made on the item texts in `items/` here; items reworded since
   are listed, and the agreement is given over all items and over the
   unchanged ones. A recomputation, not a run.
2. Tokens per verdict if the same questions were sent to a hosted endpoint
   that resends the whole conversation for every question. For each question
   the local server reported the prompt size it processed (`tokens.cached` +
   `tokens.processed`, Qwen3.6 tokenizer); a verdict's input is the sum over
   its questions. Output tokens are not in the records and are not counted
   here. An estimate for another tokenizer and endpoint, not a measurement.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from cohbench.user.agree import compare  # noqa: E402
from cohbench.user.items import load_items  # noqa: E402

HERE = Path(__file__).parent
DEV = HERE.parent


def recompute() -> None:
    items = load_items([DEV / "items"])
    labels = json.loads((DEV / "labels.json").read_text())["labels"]
    verdicts = json.loads((HERE / "verdicts-p4.json").read_text())["verdicts"]
    old = {d["id"]: d for d in (json.loads(p.read_text()) for p in sorted((HERE / "items").glob("*.json")))}
    changed = [i.id for i in items if old[i.id] != dict(i.doc)]
    print(f"items reworded since p4 ran: {len(changed)}: {', '.join(changed)}")
    for name, ids in (("all items", [i.id for i in items]), ("unchanged items", [i.id for i in items if i.id not in changed])):
        r = compare(items, {k: labels[k] for k in ids}, {k: verdicts[k] for k in ids})
        agreed = round(r["observed"] * r["n"])
        misses = ", ".join(d["item"] for d in r["disagreements"])
        print(f"p4 against current labels, {name}: {agreed} of {r['n']}, kappa {r['kappa']:.3f}; misses: {misses}")


def tokens() -> None:
    verdicts = json.loads((HERE / "verdicts-p4.json").read_text())["verdicts"]
    per, nq = [], []
    for v in verdicts.values():
        qs = v["record"]["questions"]
        per.append(sum((q["tokens"]["cached"] or 0) + (q["tokens"]["processed"] or 0) for q in qs))
        nq.append(len(qs))
    per.sort()
    print(f"input tokens per verdict over {len(per)} verdicts: median {statistics.median(per):.0f}, "
          f"mean {statistics.mean(per):.0f}, 90th percentile {per[int(0.9 * len(per))]}, maximum {per[-1]}, total {sum(per)}")
    print(f"questions per verdict: mean {statistics.mean(nq):.2f}, maximum {max(nq)}")


if __name__ == "__main__":
    recompute()
    tokens()
