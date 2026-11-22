"""python -m cohbench.scoring score BUNDLE_DIR [--reader CONFIG] --out FILE
python -m cohbench.scoring report RESULTS_DIR --out DIR [--resamples N] [--seed N] [--level X] [--bar X] [--z X] [--scripted-reader]

Exit status 0 on success, 2 when the input cannot be scored or reported."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import reader as R
from . import report
from .load import ScoreError, load
from .score import dumps, score_episode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cohbench.scoring")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score", help="score one episode's bundle")
    s.add_argument("bundle")
    s.add_argument("--reader", help="a reader config (needed for Explanation episodes)")
    s.add_argument("--out", required=True)
    s.add_argument("--no-validate", action="store_true", help="skip the bundle validator (for hand-built test bundles)")
    r = sub.add_parser("report", help="the results report over a directory of score files and an index")
    r.add_argument("results")
    r.add_argument("--out", required=True)
    r.add_argument("--resamples", type=int, default=2000)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--level", type=float, default=0.95)
    r.add_argument("--bar", type=float, default=0.9)
    r.add_argument("--scripted-reader", action="store_true",
                   help="allow Explanation scores from the scripted reader; the report is then marked as not a result and has no rank bands")
    r.add_argument("--z", type=float, default=report.SANITY_Z,
                   help=f"the reader sanity checks' bound in standard errors of chance (default {report.SANITY_Z:g})")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "score":
            cfg = R.load_config(a.reader) if a.reader else None
            rd = R.make(cfg) if cfg is not None else None
            ep = load(a.bundle, validate=not a.no_validate)
            Path(a.out).write_text(dumps(score_episode(ep, rd, cfg)), encoding="utf-8")
            return 0
        if not (a.resamples >= 0 and 0 < a.level < 1 and 0 < a.bar <= 1 and a.z > 0):
            raise report.ReportError("resamples is at least 0, level and bar lie between 0 and 1, z is positive")
        rep = report.build(a.results, resamples=a.resamples, seed=a.seed, level=a.level, bar=a.bar, z=a.z, scripted_reader=a.scripted_reader)
        text, _ = report.write(rep, Path(a.out))
        sys.stdout.write(text)
        return 0
    except (ScoreError, R.ReaderError, report.ReportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
