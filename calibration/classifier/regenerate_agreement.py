"""Recompute the hosted reports from retained verdicts without inference.

Without private paths only development files are regenerated. With both
private paths, full held-out reports stay private; only aggregate counts
and requested item-id differences are updated in the public results file.
"""

import json
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cohbench.user.agree import compare, render
from cohbench.user.awareness import report as awareness_report
from cohbench.user.items import checked_entries, items_digest, load_items, read_answers


def agreement(items_dir, labels_path, verdicts_path, json_out, text_out):
    items = load_items([items_dir])
    digest = items_digest(items)
    sides = []
    for path in [labels_path, verdicts_path]:
        _, head, entries = read_answers(path)
        assert head["items"] == digest
        sides.append(checked_entries(entries, {i.id: i for i in items}, str(path)))
    rep = compare(items, *sides)
    json_out.write_text(json.dumps(rep, indent=2) + "\n")
    text_out.write_text(render(rep, items, ("gold", "classifier")) + "\n")
    return rep


parser = argparse.ArgumentParser(
    description="Regenerate agreement files from saved verdicts, with no endpoint calls"
)
parser.add_argument("--heldout-root", type=Path)
parser.add_argument("--private-results", type=Path)
args = parser.parse_args()
if bool(args.heldout_root) != bool(args.private_results):
    parser.error("--heldout-root and --private-results must be supplied together")

dev = ROOT / "calibration/classifier/dev"
r = dev / "hosted-run-2026-11-23"
heads = {}
for verdicts in sorted(r.glob("*-verdicts.json")):
    name = verdicts.name.replace("-verdicts.json", "")
    json_out = r / f"{name}-agreement.json"
    text_out = r / f"{name}-agreement.txt"
    if not json_out.exists() and not text_out.exists():
        continue
    rep = agreement(dev / "items", dev / "labels.json", verdicts, json_out, text_out)
    summary_path = r / f"{name}-summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        summary.update(
            kappa=rep["kappa"],
            ratios=rep["ratios"],
            exact_verdicts=rep["exact_verdicts"],
            label_misses=rep["n"] - rep["label_agreements"],
            misses=len(rep["disagreements"]),
        )
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    heads[name] = {
        k: rep[k]
        for k in ("n", "exact_verdicts", "kappa", "ratios", "extra_card_items")
    }

# Raw held-out records and full reports stay outside the worktree.
if not args.heldout_root:
    for name, v in heads.items():
        print(
            name,
            v["exact_verdicts"]["identical"],
            "/",
            v["n"],
            "label kappa",
            v["kappa"],
        )
    sys.exit(0)
h = args.heldout_root
private = args.private_results
public = ROOT / "calibration/classifier/heldout-run-2026-11-23"
result = json.loads((public / "results.json").read_text())
for model, stats in result["models"].items():
    name = f"{model}-turn"
    rep = agreement(
        h / "items",
        h / "labels-author.json",
        private / f"{name}-verdicts.json",
        private / f"{name}-agreement.json",
        private / f"{name}-agreement.txt",
    )
    stats["turn"].update(
        {
            k: rep[k]
            for k in (
                "n",
                "observed",
                "chance",
                "kappa",
                "ratios",
                "exact_verdicts",
                "extra_card_items",
                "label_agreements",
                "label_kappa_at_least_0_75",
            )
        }
    )
    stats["turn"]["misses"] = [
        {
            "item": d["item"],
            "kinds": d["kinds"],
            "given": d["second"]["label"],
            "expected": d["first"]["label"],
        }
        for d in rep["disagreements"]
    ]
    stats["turn"]["answer_items"] = rep["answer_items"]
    stats["turn"]["quotes"] = rep["quotes"]
    stats["turn"]["served_metadata"] = rep["served_metadata"]["second"]
    labels = json.loads((h / "awareness-labels-author.json").read_text())
    verdicts = json.loads((private / f"{model}-awareness-verdicts.json").read_text())
    assert labels["items"] == verdicts["items"]
    aware = awareness_report(labels["labels"], verdicts["verdicts"])
    (private / f"{model}-awareness-agreement.json").write_text(
        json.dumps(aware, indent=2) + "\n"
    )
    stats["awareness"].update(
        {k: aware[k] for k in ("n", "observed", "chance", "kappa", "ratios")}
    )
    stats["awareness"]["exact_verdicts"] = {
        "identical": aware["n"] - len(aware["disagreements"]),
        "of": aware["n"],
    }
    stats["awareness"]["exact_verdicts"] = {
        "identical": aware["n"] - len(aware["disagreements"]),
        "of": aware["n"],
    }
(public / "results.json").write_text(json.dumps(result, indent=2) + "\n")
for name, v in heads.items():
    print(name, v["exact_verdicts"]["identical"], "/", v["n"], "kappa", v["kappa"])
for name, v in result["models"].items():
    print(name, "held-out", v["turn"]["exact_verdicts"], "kappa", v["turn"]["kappa"])
