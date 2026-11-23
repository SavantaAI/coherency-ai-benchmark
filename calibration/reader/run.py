"""Calibrate recorded readers on saved pipeline bundles and private authored items.

Run from the repository root with Python 3.12. No credentials or answer text
are written to the aggregate record. Detailed inputs and choices stay in runs/.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from cohbench.ledger import Ledger
from cohbench.scoring import explanation, reader
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def calibrate(config, pipeline, authored, output, ledger, key_file):
    cfg = reader.load_config(config)
    label = cfg.raw["expect"]["model"]
    target = output / label
    target.mkdir(parents=True, exist_ok=True)
    hosted = reader.make(cfg, key=reader.read_key(key_file), ledger=ledger)
    rd = reader.RecordedReader(hosted, target / "cache.jsonl")
    rows = []
    for run in read(pipeline / "runs.json"):
        if not run["instance"].startswith("explanation-") or run["agent"] not in (
            "ideal",
            "worst",
        ):
            continue
        rd.records.clear()
        ep = load(Path(run["path"]) / "export" / "bundle", validate=True)
        result = score_episode(ep, rd, cfg)
        (target / f"{run['episode']}.score.json").write_text(dumps(result))
        # Replay through the production scorer. Timing metadata is cache-only.
        assert dumps(score_episode(ep, rd, cfg)) == dumps(result)
        diagnostic = explanation.score(ep, rd, cfg)
        rows.append(
            {
                "instance": run["instance"],
                "agent": run["agent"],
                "score": result,
                "explanation": diagnostic,
                "requests": [r["request_digest"] for r in rd.used()],
            }
        )
        write(target / "pipeline.json", rows)
        print(label, run["instance"], run["agent"], flush=True)
    harder = []
    for item in read(authored):
        rd.records.clear()
        excerpt, words = reader.window(item["answer"])
        votes = explanation.read_items(
            rd, cfg, item["question"], excerpt, item["options"], item["true_cause"]
        )
        harder.append(
            {
                "id": item["id"],
                "group": item["group"],
                "expected": item["expected"],
                "words": words,
                "votes": votes,
                "requests": [r["request_digest"] for r in rd.used()],
            }
        )
        write(target / "harder.json", harder)
        print(label, item["id"], flush=True)
    return label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pipeline", type=Path)
    parser.add_argument("authored", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args()
    ledger = Ledger(args.output / "calls.jsonl", 2500)
    configs = sorted(Path("cohbench/scoring/configs").glob("explabs-*.json"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                calibrate,
                config,
                args.pipeline,
                args.authored,
                args.output,
                ledger,
                args.key_file,
            )
            for config in configs
        ]
        for future in futures:
            print("Completed", future.result(), flush=True)


if __name__ == "__main__":
    main()
