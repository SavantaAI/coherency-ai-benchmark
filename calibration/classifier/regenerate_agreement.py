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

RECOMMENDATION_COMMIT = "ecc73b7eb1122f28f88de92a0443ed154c608f1f"


def coverage(ids, first, second, allow_partial):
    for name, entries in (("gold", first), ("verdict", second)):
        extra = set(entries) - ids
        if extra:
            raise ItemError(
                f"{name} ids outside the manifest item set: {sorted(extra)}"
            )
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
    ids = item_scope(items, selected)
    sides = []
    for expected, path in (("labels", labels_path), ("verdicts", verdicts_path)):
        kind, head, entries = read_answers(path)
        if kind != expected:
            raise ItemError(f"{path.name}: expected a {expected} file")
        if head["items"] != digest:
            raise ItemError(f"{path.name}: another item-set digest")
        sides.append(checked_entries(entries, {i.id: i for i in items}, path.name))
    # Gold may cover the shared source pool. Verdicts must cover only this run.
    sides[0] = {i: v for i, v in sides[0].items() if i in ids}
    cov = coverage(ids, *sides, allow_partial)
    items = [i for i in items if i.id in ids]
    rep = compare(items, *sides)
    rep["coverage"] = cov
    return rep, render(rep, items, ("gold", "classifier")) + "\n"


def item_scope(items, selected):
    available = {i.id for i in items}
    if selected is None:
        return available
    if not isinstance(selected, list) or len(set(selected)) != len(selected):
        raise ItemError("manifest item ids must be a list without duplicates")
    ids = set(selected)
    if ids - available:
        raise ItemError(
            f"manifest item ids missing from inputs: {sorted(ids - available)}"
        )
    return ids


def aware_report(items_dir, labels_path, verdicts_path, allow_partial, selected=None):
    items = load_items([items_dir])
    digest = items_digest(items)
    ids = item_scope(items, selected)
    available = {i.id for i in items}
    labels = json.loads(labels_path.read_text())
    verdicts = json.loads(verdicts_path.read_text())
    if (
        labels.get("format")
        not in ("cohbench-awareness-labels", "cohbench-heldout-awareness-labels")
        or labels.get("version") != 1
    ):
        raise ItemError(f"{labels_path.name}: expected an awareness labels file")
    if (
        verdicts.get("format") != "cohbench-awareness-verdicts"
        or verdicts.get("version") != 1
    ):
        raise ItemError(f"{verdicts_path.name}: expected an awareness verdicts file")
    if labels["items"] != digest or verdicts["items"] != digest:
        raise ItemError("awareness labels or verdicts have another item-set digest")
    first = {i: v for i, v in outcomes(labels["labels"], available).items() if i in ids}
    second = outcomes(verdicts["verdicts"], available)
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


def manifest_path(roots, owner, name):
    base = roots.get(owner)
    path = Path(name)
    if owner not in roots or path.is_absolute() or ".." in path.parts:
        raise ItemError("manifest paths must be relative to a declared root")
    return None if base is None else base / path


def preflight(root, heldout_root, private_results):
    roots = {"repo": root, "heldout": heldout_root, "private": private_results}
    manifest = json.loads(
        (root / "calibration/classifier/hosted-runs.json").read_text()
    )
    if manifest.get("version") != 1 or not isinstance(manifest.get("runs"), list):
        raise ItemError("unsupported run manifest")
    selected, owned, directories, names = [], set(), set(), set()
    for run in manifest["runs"]:
        if run["name"] in names:
            raise ItemError(f"duplicate manifest run: {run['name']}")
        names.add(run["name"])
        if run["kind"] not in ("turn", "awareness") or run["set"] not in (
            "development",
            "heldout",
        ):
            raise ItemError(f"invalid manifest run: {run['name']}")
        paths = {}
        for field in ("items", "labels"):
            paths[field] = manifest_path(
                roots, run["inputs"]["root"], run["inputs"][field]
            )
        paths["verdicts"] = manifest_path(
            roots, run["verdicts"]["root"], run["verdicts"]["path"]
        )
        paths["outputs"] = {}
        for field, name in run["outputs"].items():
            if field == "root":
                continue
            if field not in ("json", "text", "summary"):
                raise ItemError(f"unknown manifest output: {field}")
            path = manifest_path(roots, run["outputs"]["root"], name)
            paths["outputs"][field] = path
            if path is not None:
                if path in owned:
                    raise ItemError(f"duplicate manifest output: {name}")
                owned.add(path)
                directories.add(path.parent)
        if not {"json", "text"} <= paths["outputs"].keys():
            raise ItemError(f"manifest run lacks agreement outputs: {run['name']}")
        paths["aggregate"] = None
        if "aggregate" in run:
            path = manifest_path(roots, "repo", run["aggregate"])
            paths["aggregate"] = path
            owned.add(path)
            directories.add(path.parent)
        if run["set"] == "heldout" and heldout_root is None:
            continue
        if any(
            paths[field] is None for field in ("items", "labels", "verdicts")
        ) or any(p is None for p in paths["outputs"].values()):
            raise ItemError(f"manifest run requires an unavailable root: {run['name']}")
        for field in ("items", "labels", "verdicts"):
            path = paths[field]
            if not (path.is_dir() if field == "items" else path.is_file()):
                raise FileNotFoundError(2, "missing input", str(path))
        selected.append((run, paths))
    for evidence in manifest.get("evidence", []):
        path = manifest_path(roots, evidence["root"], evidence["path"])
        if path is not None:
            owned.add(path)
    # Check all public report directories, including a stale directory whose
    # entire run was removed from the manifest. Never scan held-out inputs.
    for directory in (root / "calibration/classifier", private_results):
        if directory is not None:
            directories.add(directory)
    for directory in directories:
        for pattern in (
            "*-agreement.json",
            "*-agreement.txt",
            "*-summary.json",
            "results.json",
        ):
            for path in directory.rglob(pattern):
                if path not in owned:
                    raise ItemError(
                        f"published output has no manifest owner: {relative_name(path, roots.values())}"
                    )
    return selected


def relative_name(path, roots):
    path = Path(path).absolute()
    for root in sorted(
        (Path(r).absolute() for r in roots if r is not None),
        key=lambda r: len(str(r)),
        reverse=True,
    ):
        if path.is_relative_to(root):
            return str(path.relative_to(root))
    return path.name


def regenerate(root, heldout_root=None, private_results=None, allow_partial=False):
    writes, heads, aggregates = {}, {}, {}
    for run, paths in preflight(root, heldout_root, private_results):
        report = turn_report if run["kind"] == "turn" else aware_report
        rep, text = report(
            paths["items"],
            paths["labels"],
            paths["verdicts"],
            allow_partial,
            run["inputs"].get("ids"),
        )
        public = public_report(rep, awareness=run["kind"] == "awareness")
        values = {"json": rep, "text": text, "summary": public}
        for field, path in paths["outputs"].items():
            writes[path] = values[field]
        if paths["aggregate"] is not None:
            result = aggregates.setdefault(
                paths["aggregate"],
                {
                    "recommendation_commit": RECOMMENDATION_COMMIT,
                    "complete": True,
                    "models": {},
                },
            )
            result["models"].setdefault(run["model"], {})[run["kind"]] = public
            result["complete"] &= rep["coverage"]["complete"]
        heads[run["name"]] = rep
    writes.update(aggregates)
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
    except OSError as exc:
        name = relative_name(
            exc.filename or "input", (ROOT, args.heldout_root, args.private_results)
        )
        print(f"error: cannot read or write file: {name}", file=sys.stderr)
        return 1
    except (ItemError, ValueError, KeyError, TypeError) as exc:
        message = str(exc)
        for root in (ROOT, args.heldout_root, args.private_results):
            if root is not None:
                message = message.replace(str(root.absolute()) + "/", "")
        print("error: " + " ".join(message.splitlines()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
