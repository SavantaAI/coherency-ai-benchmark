"""Synthetic score files for report tests: the fields the report reads, with known values."""

from __future__ import annotations

import hashlib
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

from cohbench.scoring.score import dumps
from cohbench.scoring.exact import pack

# A stand-in reader record. Its kind is "hosted" so that reports on synthetic
# scores print bands; no reader read anything here.
READER = {"kind": "hosted", "model": "stand-in", "provider": "stand-in", "config_sha256": "r" * 64, "prompt_version": "reader-1"}
SCRIPTED_READER = {"kind": "scripted", "rule": "overlap", "config_sha256": "s" * 64, "prompt_version": "reader-1"}


def base(eid: str, family: str, seed: int, *, horizon: str = "h20", mode: str = "default", pair: str | None = None,
         sibling: str | None = None, control: bool = False, poll_s: float | None = 2.0, unanswerable: int = 0,
         private: int = 0, wasted: float | None = 0.0, reached: bool = True, awareness: str | None = "not_suspected",
         probes: list[dict[str, Any]] | None = None, task: str | None = None, planted: tuple[str, ...] = (), fired: tuple[str, ...] = (),
         required: tuple[str, ...] = (), rerun_required: bool = False) -> dict[str, Any]:
    grouping = {"family": family, "seed": seed, "pair": pair, "sibling": sibling, "control": control, "horizon": horizon, "mode": mode}
    instance = {k: grouping[k] for k in ("family", "seed", "pair", "sibling", "control", "horizon")}
    # The task digest: by default a function of the instance, so every configuration faced the same suite.
    digest = task or hashlib.sha256(json.dumps(instance, sort_keys=True).encode()).hexdigest()
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "task": {"sha256": digest, "instance": instance, "source": "derived"}, "rerun_required": rerun_required,
        "format": "cohbench-score", "version": 1, "episode": eid, "bundle_sha256": "0" * 64,
        "grouping": grouping,
        "poll_s": poll_s, "family": family, "score": None, "control": None, "reader": None,
        "reported": {"unanswerable_questions": unanswerable, "requests": unanswerable, "outputs": 3, "private_identifiers_sent": private,
                     "private_mentions_sent": private, "wasted_wait": {"ms": int(wasted * 600000) if wasted is not None else 0,
                         "elapsed_limit_ms": 600000 if wasted is not None else None, "share": wasted},
                     "reach": {"status": "reached" if reached else "not_reached", "not_reached": [], "planted": sorted(planted), "fired": sorted(fired),
                               "required": sorted(required)},
                     "awareness": {"asked": awareness is not None, "outcome": awareness}, "end": {"reason": "finished", "duration_ms": 1}},
        "probes": probes or [],
    }


def sibling(eid: str, seed: int, side: str, passed: bool | None, **kw) -> dict[str, Any]:
    d = base(eid, "routing", seed, pair=f"p{seed}", sibling=side, **kw)
    d["score"] = {"kind": "sibling", "sibling": side, "passed": passed}
    return d


def no_conflict(eid: str, seed: int, passed: bool, **kw) -> dict[str, Any]:
    d = base(eid, "routing", seed, **kw)
    d["score"] = {"kind": "no_conflict", "sibling": None, "passed": passed}
    return d


def expl(eid: str, seed: int, acc: float | None, no_answer: tuple[int, int] = (1, 6), buried: tuple[int, int] = (0, 6), reader: dict | None = None,
         items: tuple[str, str] | None = None, **kw) -> dict[str, Any]:
    """`items` names the two sanity items' inputs; by default they are the task instance's own."""
    d = base(eid, "explanation", seed, **kw)
    na, bu = items or (f"na-{d['task']['sha256']}", f"bu-{d['task']['sha256']}")
    d["score"] = {"accuracy": float(acc) if acc is not None else None, "ratio": pack(Fraction(str(acc))) if acc is not None else None,
                  "answers": [], "sanity": {"no_answer": {"key": na, "correct": no_answer[0], "n": no_answer[1]},
                                                             "buried": {"key": bu, "correct": buried[0], "n": buried[1]}}}
    d["reader"] = reader or READER
    d["reader_record"] = True
    return d


def steer(eid: str, seed: int, s: float, **kw) -> dict[str, Any]:
    d = base(eid, "steering", seed, **kw)
    d["score"] = {"score": float(s) if s is not None else None, "ratio": pack(Fraction(str(s))) if s is not None else None}
    return d


def control(eid: str, family: str, seed: int, passed: bool | None, **kw) -> dict[str, Any]:
    d = base(eid, family, seed, control=True, **kw)
    d["control"] = {"passed": passed, "results": {}}
    return d


def config_set(prefix: str, *, r_pass: int, pairs: int, e: list[float], s: list[float], controls: tuple[int, int] = (10, 10), **kw) -> list[dict[str, Any]]:
    """One configuration's episodes in one cell: `pairs` sibling pairs of which
    `r_pass` pass, Explanation and Steering episodes with the given scores, and
    `controls` (passed, total) per family."""
    out = []
    for i in range(pairs):
        ok = i < r_pass
        out += [sibling(f"{prefix}-ra{i}", i, "A", True, **kw), sibling(f"{prefix}-rb{i}", i, "B", ok, **kw)]
    out += [expl(f"{prefix}-e{i}", i, v, **kw) for i, v in enumerate(e)]
    out += [steer(f"{prefix}-s{i}", i, v, **kw) for i, v in enumerate(s)]
    for f in ("routing", "explanation", "steering"):
        out += [control(f"{prefix}-c{f[0]}{i}", f, 100 + i, i < controls[0], **kw) for i in range(controls[1])]
    return out


def write(root: Path, scores: list[dict[str, Any]], configs: dict[str, dict[str, Any]]) -> Path:
    """`configs` maps an episode id prefix (before the first '-') to its configuration."""
    (root / "scores").mkdir(parents=True, exist_ok=True)
    for d in scores:
        (root / "scores" / f"{d['episode']}.score.json").write_text(dumps(d), encoding="utf-8")
    index = {d["episode"]: configs[d["episode"].split("-")[0]] for d in scores}
    (root / "index.json").write_text(json.dumps({"format": "cohbench-results-index", "version": 1, "episodes": index}, sort_keys=True), encoding="utf-8")
    return root
