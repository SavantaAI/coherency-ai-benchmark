"""Recompute agreement solely from saved inputs and verdicts, without inference.

All reports are computed and validated before any output is replaced. Partial
coverage requires --allow-partial and is marked in every affected output.
Full held-out records stay private. Public fields are explicitly selected.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cohbench.user.agree import compare, render
from cohbench.user.awareness import outcomes, report as awareness_report
from cohbench.user.items import (
    ItemError,
    checked_entries,
    items_digest,
    load_items,
    read_answers,
)

MODELS = ("kimi-k3.1-pro", "deepseek-v4.1-flash-uncensored")
RECOMMENDATION_COMMIT = "ecc73b7eb1122f28f88de92a0443ed154c608f1f"


def coverage(ids, first, second, allow_partial):
    info = {
        "complete": set(first) == set(second) == ids,
        "compared": len(ids & set(first) & set(second)),
        "expected": len(ids),
        "missing_first": sorted(ids - set(first)),
        "missing_second": sorted(ids - set(second)),
    }
    if not info["complete"] and not allow_partial:
        raise ItemError(
            f"incomplete report: missing gold ids {info['missing_first']}; missing verdict ids {info['missing_second']}; use --allow-partial"
        )
    return info


def turn_report(items_dir, labels_path, verdicts_path, allow_partial, selected=None):
    items = load_items([items_dir])
    digest = items_digest(items)
    sides = []
    for path in (labels_path, verdicts_path):
        _, head, entries = read_answers(path)
        if head["items"] != digest:
            raise ItemError(f"{path.name}: another item-set digest")
        sides.append(checked_entries(entries, {i.id: i for i in items}, path.name))
    if selected is not None:
        items = [i for i in items if i.id in selected]
        sides = [
            {i: v for i, v in entries.items() if i in selected} for entries in sides
        ]
    cov = coverage({i.id for i in items}, *sides, allow_partial)
    rep = compare(items, *sides)
    rep["coverage"] = cov
    return rep, render(rep, items, ("gold", "classifier")) + "\n"


def aware_report(items_dir, labels_path, verdicts_path, allow_partial):
    items = load_items([items_dir])
    digest = items_digest(items)
    ids = {i.id for i in items}
    labels = json.loads(labels_path.read_text())
    verdicts = json.loads(verdicts_path.read_text())
    if labels["items"] != digest or verdicts["items"] != digest:
        raise ItemError("awareness labels or verdicts have another item-set digest")
    first = outcomes(labels["labels"], ids)
    second = outcomes(verdicts["verdicts"], ids)
    cov = coverage(ids, first, second, allow_partial)
    rep = awareness_report(first, second)
    rep["coverage"] = cov
    rep["exact_verdicts"] = {
        "identical": rep["n"] - len(rep["disagreements"]),
        "of": rep["n"],
    }
    text = [
        f"Exact verdicts: {rep['exact_verdicts']['identical']} of {rep['n']}",
        f"Label kappa: {rep['kappa']}",
        "Disclosing verdicts: not applicable, awareness has no card selection",
    ]
    if not cov["complete"]:
        text.insert(
            0,
            f"INCOMPLETE: {cov['compared']} of {cov['expected']} items; missing gold {cov['missing_first']}; missing verdicts {cov['missing_second']}",
        )
    text += [
        "Confusion, expected rows and given columns:",
        json.dumps(rep["confusion"], indent=2),
    ]
    text += [
        f"{d['item']}: given {d['given']}, expected {d['expected']}"
        for d in rep["disagreements"]
    ]
    return rep, "\n".join(text) + "\n"


def public_report(rep, awareness=False):
    """No text or raw records from private items may enter public output."""
    result = {
        key: rep[key]
        for key in (
            "n",
            "coverage",
            "observed",
            "chance",
            "kappa",
            "ratios",
            "confusion",
            "exact_verdicts",
        )
    }
    result["misses"] = [{"item": miss["item"]} for miss in rep["disagreements"]]
    if not awareness:
        for key in ("label_agreements", "label_kappa_at_least_0_75"):
            result[key] = rep[key]
        for key in ("disclosures", "extra_card_items"):
            result[key] = {k: v for k, v in rep[key].items() if k != "differences"}
            result[key]["differences"] = [
                {"item": d["item"]} for d in rep[key]["differences"]
            ]
        result["answer_items"] = {
            k: v for k, v in rep["answer_items"].items() if k != "differences"
        }
        result["answer_items"]["differences"] = [
            {"item": d["item"]} for d in rep["answer_items"]["differences"]
        ]
        result["quotes"] = {
            k: v for k, v in rep["quotes"].items() if k != "differences"
        }
        result["quotes"]["differences"] = [
            {"item": d["item"]} for d in rep["quotes"]["differences"]
        ]
    return result


def regenerate(root, heldout_root=None, private_results=None, allow_partial=False):
    writes, heads = {}, {}
    dev = root / "calibration/classifier/dev"
    archive = dev / "hosted-run-2026-11-23"
    for verdicts in sorted(archive.glob("*-verdicts.json")):
        name = verdicts.name.removesuffix("-verdicts.json")
        if name == "glm-compatibility":
            continue
        selected = None
        if name.endswith("-subset"):
            selected = set(json.loads((archive / "subset.json").read_text())["ids"])
        rep, text = turn_report(
            dev / "items", dev / "labels.json", verdicts, allow_partial, selected
        )
        writes[archive / f"{name}-agreement.json"] = rep
        writes[archive / f"{name}-agreement.txt"] = text
        # Every statistical summary is rebuilt; original timing stays in run logs.
        writes[archive / f"{name}-summary.json"] = public_report(rep)
        heads[name] = rep
    if heldout_root is not None:
        public = root / "calibration/classifier/heldout-run-2026-11-23"
        result = {
            "recommendation_commit": RECOMMENDATION_COMMIT,
            "complete": True,
            "models": {},
        }
        for model in MODELS:
            stats = {}
            for kind in ("turn", "awareness"):
                name = f"{model}-{kind}"
                if kind == "turn":
                    rep, text = turn_report(
                        heldout_root / "items",
                        heldout_root / "labels-author.json",
                        private_results / f"{name}-verdicts.json",
                        allow_partial,
                    )
                else:
                    rep, text = aware_report(
                        heldout_root / "awareness",
                        heldout_root / "awareness-labels-author.json",
                        private_results / f"{name}-verdicts.json",
                        allow_partial,
                    )
                writes[private_results / f"{name}-agreement.json"] = rep
                writes[private_results / f"{name}-agreement.txt"] = text
                stats[kind] = public_report(rep, awareness=kind == "awareness")
                result["complete"] &= rep["coverage"]["complete"]
                heads[f"{model}-heldout-{kind}"] = rep
            result["models"][model] = stats
        writes[public / "results.json"] = result
    for path, value in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
        )
    for name, rep in heads.items():
        cov = rep["coverage"]
        print(
            name
            + (
                f" INCOMPLETE: {cov['compared']}/{cov['expected']}; missing {cov['missing_first'] + cov['missing_second']}"
                if not cov["complete"]
                else f" COMPLETE: {cov['compared']}/{cov['expected']}"
            )
        )
        print(f"Exact verdicts: {rep['exact_verdicts']['identical']}/{rep['n']}")
        print(f"Label kappa: {rep['kappa']}")
        if "disclosures" in rep:
            dis = rep["disclosures"]
            print(
                f"Disclosing verdicts: {dis['count']}/{rep['n']} ({dis['rate']}); ids {dis['differences']}"
            )
        else:
            print(
                "Disclosing verdicts: not applicable, awareness has no card selection"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heldout-root", type=Path)
    parser.add_argument("--private-results", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.heldout_root) != bool(args.private_results):
        parser.error("--heldout-root and --private-results must be supplied together")
    try:
        regenerate(ROOT, args.heldout_root, args.private_results, args.allow_partial)
    except (ItemError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
