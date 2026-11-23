"""Fresh reader calibration, separate from the report's small-sample smoke check.

The driver chose these thresholds and the 200/60 sample allocation on
2026-11-23. The command uses only the generated main question and 80 words.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from cohbench.ledger import Ledger
from cohbench.tasks.common import Options
from cohbench.tasks.explanation import generate
from . import reader as R
from .explanation import read_items

FORMAT = "cohbench-reader-calibration"
THRESHOLDS = {
    "no_answer_tolerance": 0.05,
    "buried_max": 0.25,
    "ideal_min": 0.95,
    "worst_max": 0.10,
    "sanity_instances": 200,
    "control_instances": 60,
}
QUESTION_SOURCE = "answer_key"
DEVELOPMENT_SEEDS = set(range(1, 7)) | set(range(1001, 1007))


def template_digest():
    return hashlib.sha256(
        R.canonical(
            {
                "system": R.SYSTEM,
                "user": R.user_message("QUESTION", "ANSWER", list("ABCDEF")),
            }
        ).encode()
    ).hexdigest()


def assess(groups):
    checks = {}
    for name in ("no_answer", "buried", "ideal", "worst"):
        row = groups.get(name, {}) if isinstance(groups, dict) else {}
        if not isinstance(row, dict):
            row = {}
        count = row.get("instances", 0)
        n = row.get("n", 0)
        correct = row.get("correct", 0)
        positions = row.get("positions", [])
        valid = (
            type(count) is int
            and type(n) is int
            and type(correct) is int
            and count >= (200 if name in ("no_answer", "buried") else 60)
            and n == count * 6
            and 0 <= correct <= n
            and isinstance(positions, list)
            and len(positions) == 6
            and all(type(v) is int and v >= 0 for v in positions)
            and sum(positions) == n
        )
        rate = Fraction(correct, n) if valid else None
        passed = bool(
            valid
            and (
                abs(rate - Fraction(1, 6)) <= Fraction(1, 20)
                if name == "no_answer"
                else rate <= Fraction(1, 4)
                if name == "buried"
                else rate >= Fraction(19, 20)
                if name == "ideal"
                else rate <= Fraction(1, 10)
            )
        )
        checks[name] = {
            "passed": passed,
            "accuracy": float(rate) if rate is not None else None,
        }
    return {"passed": all(v["passed"] for v in checks.values()), "checks": checks}


def sample(start=5000, count=200, controls=60):
    if (
        type(start) is not int
        or start < 0
        or count < 200
        or controls < 60
        or controls > count
    ):
        raise ValueError("calibration needs at least 200 instances and 60 controls")
    seeds = list(range(start, start + count))
    if DEVELOPMENT_SEEDS.intersection(seeds):
        raise ValueError("calibration seeds overlap the development suite")
    rows = []
    for offset, seed in enumerate(seeds):
        inst = generate(Options("explanation", seed, "h20"))
        key = inst.task["scoring"]["answer_key"]
        bc = key["buried_control"]
        inputs = {"no_answer": "", "buried": bc["filler"] + " " + bc["sentence"]}
        if offset < controls:
            for name in ("ideal", "worst"):
                inputs[name] = inst.scripts[name]["run"]["script"]["handlers"][0][
                    "final"
                ]
        for name, text in inputs.items():
            rows.append(
                {
                    "seed": seed,
                    "input": name,
                    "question": key["question"],
                    "excerpt": R.window(text)[0],
                    "options": key["options"],
                    "true_cause": key["true_cause"],
                }
            )
    return rows


def record(cfg, rows):
    groups = {}
    for name in sorted({r["input"] for r in rows}):
        selected = [r for r in rows if r["input"] == name]
        votes = [v for r in selected for v in r["items"]]
        groups[name] = {
            "instances": len(selected),
            "seeds": sorted(r["seed"] for r in selected),
            "correct": sum(v["correct"] for v in votes),
            "n": len(votes),
            "positions": [sum(v["picked"] == i for v in votes) for i in range(6)],
            "per_order": [
                sum(v["correct"] for v in votes if v["order"] == i) for i in range(6)
            ],
        }
    return {
        "format": FORMAT,
        "version": 1,
        "date": date.today().isoformat(),
        "reader": cfg.digest,
        "reader_identity": R.make(cfg).describe(),
        "template_sha256": template_digest(),
        "question_source": QUESTION_SOURCE,
        "orders": 6,
        "threshold_authority": "driver, 2026-11-23",
        "thresholds": THRESHOLDS,
        "groups": groups,
        **assess(groups),
    }


def validate_record(doc, digest):
    if (
        not isinstance(doc, dict)
        or doc.get("format") != FORMAT
        or type(doc.get("version")) is not int
        or doc.get("version") != 1
    ):
        return False
    if (
        doc.get("reader") != digest
        or doc.get("template_sha256") != template_digest()
        or doc.get("question_source") != QUESTION_SOURCE
        or doc.get("orders") != 6
    ):
        return False
    groups = doc.get("groups", {})
    if (
        not isinstance(groups, dict)
        or set(groups) != {"no_answer", "buried", "ideal", "worst"}
        or not assess(groups)["passed"]
    ):
        return False
    for name, row in groups.items():
        seeds = row.get("seeds", [])
        per_order = row.get("per_order")
        # assess() already ties the position counts to the number of votes.
        if (
            not isinstance(per_order, list)
            or len(per_order) != 6
            or any(type(v) is not int or not 0 <= v <= row["instances"] for v in per_order)
            or sum(per_order) != row["correct"]
        ):
            return False
        if (
            not isinstance(seeds, list)
            or len(seeds) != row["instances"]
            or any(type(seed) is not int or seed < 0 for seed in seeds)
            or len(set(seeds)) != len(seeds)
            or DEVELOPMENT_SEEDS.intersection(seeds)
        ):
            return False
    return (
        groups["no_answer"]["seeds"] == groups["buried"]["seeds"]
        and groups["ideal"]["seeds"]
        == groups["worst"]["seeds"]
        == groups["no_answer"]["seeds"][: groups["ideal"]["instances"]]
    )


def output_name(cfg, kind):
    """The file holding one config's `kind` output ("items" or "calibration").

    Named by the config digest, so two configs that report the same model
    keep separate records; the model name is only a reading aid."""
    return f"{cfg.raw['expect']['model']}.{cfg.digest}.{kind}.json"


def run(
    configs,
    output,
    *,
    start=5000,
    count=200,
    controls=60,
    workers=8,
    max_calls=4500,
    no_answer_configs=(),
    overwrite=False,
):
    output = Path(output)
    inputs = sample(start, count, controls)
    partial = [R.load_config(p) for p in no_answer_configs]
    configs = [R.load_config(p) for p in configs] + partial
    partial_digests = {cfg.digest for cfg in partial}
    if any(cfg.orders != (6, 0) for cfg in configs):
        raise ValueError("calibration uses six orders and seed zero")
    if len({cfg.digest for cfg in configs}) != len(configs):
        raise ValueError("a reader config is given more than once")
    existing = sorted(
        name
        for cfg in configs
        for name in (output_name(cfg, "items"), output_name(cfg, "calibration"))
        if (output / name).exists()
    )
    if existing and not overwrite:
        raise ValueError(
            f"calibration output already holds {', '.join(existing)}; "
            "pass --overwrite to replace it"
        )
    output.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(output / "calls.jsonl", max_calls)
    jobs = [
        (cfg, row)
        for row in inputs
        for cfg in configs
        if cfg.digest not in partial_digests or row["input"] == "no_answer"
    ]
    # Persist the exact input plan before any request. Paths and credentials are absent.
    plan = {"inputs": inputs, "configs": [R.canonical(cfg.raw) for cfg in configs]}
    plan_path = output / "plan.json"
    encoded = json.dumps(plan, sort_keys=True, indent=2) + "\n"
    if plan_path.exists() and plan_path.read_text() != encoded:
        raise ValueError("calibration output holds another sample or config")
    plan_path.write_text(encoded)
    results = {cfg.digest: [] for cfg in configs}

    def execute(job):
        cfg, row = job
        rd = R.RecordedReader(
            R.make(cfg, ledger=ledger),
            output / (cfg.raw["expect"]["model"] + ".cache.jsonl"),
        )
        items = read_items(
            rd, cfg, row["question"], row["excerpt"], row["options"], row["true_cause"]
        )
        return cfg, {"seed": row["seed"], "input": row["input"], "items": items}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(execute, job) for job in jobs]
        try:
            for future in as_completed(pending):
                cfg, row = future.result()
                results[cfg.digest].append(row)
                ordered = sorted(
                    results[cfg.digest], key=lambda r: (r["seed"], r["input"])
                )
                (output / output_name(cfg, "items")).write_text(
                    json.dumps(ordered, sort_keys=True) + "\n"
                )
                (output / output_name(cfg, "calibration")).write_text(
                    json.dumps(record(cfg, ordered), sort_keys=True, indent=2) + "\n"
                )
                if sum(map(len, results.values())) % 20 == 0:
                    print(
                        sum(map(len, results.values())),
                        "/",
                        len(jobs),
                        "inputs complete",
                        flush=True,
                    )
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return {cfg.digest: record(cfg, results[cfg.digest]) for cfg in configs}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", action="append", required=True)
    p.add_argument("--no-answer-config", action="append", default=[])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--start-seed", type=int, default=5000)
    p.add_argument("--instances", type=int, default=200)
    p.add_argument("--controls", type=int, default=60)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-calls", type=int, default=4500)
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing records, as when resuming an interrupted run",
    )
    a = p.parse_args()
    run(
        a.config,
        a.out,
        start=a.start_seed,
        count=a.instances,
        controls=a.controls,
        workers=a.workers,
        max_calls=a.max_calls,
        no_answer_configs=a.no_answer_config,
        overwrite=a.overwrite,
    )


if __name__ == "__main__":
    main()
