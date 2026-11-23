"""The public development suite. `suite.json` lists every instance with its
grouping keys, its directory and its task digest, the plan a results report
checks coverage against.

For each family and horizon: the seeds for 6
sibling pairs (Routing) or 6 scored seeds, 6 controls, and for Routing the
seeds with no conflict, a quarter of all its scored seeds."""

from __future__ import annotations

from pathlib import Path

from .common import HORIZONS, GenerateError, Options, dumps, task_digest, write_instance

PAIRS = 6  # sibling pairs, or scored seeds, per family and horizon (the root README section 11)
CONTROLS = 6
CONTROL_SEEDS = 1001  # controls take seeds from here on, apart from the scored ones


def routing_seeds() -> tuple[list[int], list[int]]:
    from .routing import has_conflict

    conflict: list[int] = []
    none: list[int] = []
    want_none = PAIRS // 3  # 6 conflict seeds and 2 without: a quarter of 8
    seed = 1
    while len(conflict) < PAIRS or len(none) < want_none:
        (conflict if has_conflict(seed) else none).append(seed)
        seed += 1
    return conflict[:PAIRS], none[:want_none]


def steering_seeds() -> list[int]:
    """Half with easy wins, half with no valid improvement."""
    from .steering import has_easy_wins

    easy: list[int] = []
    flat: list[int] = []
    seed = 1
    while len(easy) < PAIRS // 2 or len(flat) < PAIRS - PAIRS // 2:
        (easy if has_easy_wins(seed) else flat).append(seed)
        seed += 1
    return sorted(easy[:PAIRS // 2] + flat[:PAIRS - PAIRS // 2])


def options() -> list[Options]:
    out: list[Options] = []
    conflict, none = routing_seeds()
    controls = list(range(CONTROL_SEEDS, CONTROL_SEEDS + CONTROLS))
    for h in HORIZONS:
        for s in conflict:
            out += [Options("routing", s, h, "A"), Options("routing", s, h, "B")]
        out += [Options("routing", s, h) for s in none]
        out += [Options("routing", s, h, control=True) for s in controls]
        out += [Options("explanation", s, h) for s in range(1, PAIRS + 1)]
        out += [Options("explanation", s, h, control=True) for s in controls]
        out += [Options("steering", s, h) for s in steering_seeds()]
        out += [Options("steering", s, h, control=True) for s in controls]
    return out


def write_suite(out: Path) -> int:
    from . import generate

    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise GenerateError(f"{out}: exists and is not an empty directory")
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for o in options():
        inst = generate(o)
        write_instance(inst, out / o.name())
        g = inst.task["grouping"]
        index.append({"dir": o.name(), **g, "digest": task_digest(out / o.name())})
    (out / "suite.json").write_text(dumps({"format": "cohbench-task-suite", "version": 1, "instances": index}), encoding="utf-8")
    return len(index)
