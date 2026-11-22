"""Command line:

    python -m cohbench.tasks generate --family routing|explanation|steering --seed N --horizon 20|80 \\
        [--sibling a|b] [--control] --out DIR
    python -m cohbench.tasks suite --out DIR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import GenerateError, Options, generate_to
from .suite import write_suite


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cohbench.tasks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="write one task instance")
    g.add_argument("--family", required=True, choices=["routing", "explanation", "steering"])
    g.add_argument("--seed", required=True, type=int)
    g.add_argument("--horizon", required=True, choices=["20", "80"])
    g.add_argument("--sibling", choices=["a", "b"], default=None)
    g.add_argument("--control", action="store_true")
    g.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("suite", help="write the public development suite")
    s.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "generate":
            if a.seed < 0:
                raise GenerateError("--seed: a non-negative integer")
            o = Options(a.family, a.seed, f"h{a.horizon}", a.sibling.upper() if a.sibling else None, a.control)
            generate_to(o, a.out)
            print(a.out)
        else:
            n = write_suite(a.out)
            print(f"{n} instances in {a.out}")
    except GenerateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
