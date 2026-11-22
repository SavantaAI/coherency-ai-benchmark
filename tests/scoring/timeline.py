"""Seeded Steering timelines, rendered to change logs at two grains.

A timeline is a list of events at integer milliseconds: writes (a path and
its new content, or None for a removal), attempts (a fingerprint) and at
most one correction. `render(tl, merge)` turns it into a test bundle the way
a watcher would at two grains: with `merge`, writes less than `MERGE_MS`
after the previous write share its batch; without, every write is its own
batch. A batch records its net change, its first and last write, and the
reading of its tree. The correction's batch and straddled run are worked
out from the batches' write spans as the watcher's mark would be (D-037).
"""

from __future__ import annotations

from typing import Any

from cohbench.scoring.rng import Rng, stream

from .build import STEERING_KEY, Bundle

MERGE_MS = 300
OPS = {"v0": 100, "v1": 90, "v2": 110, "v3": 80}


def generate(seed: int) -> list[dict[str, Any]]:
    r: Rng = stream("timeline", seed)
    t, out, tmp = 500, [], False
    corr = r.below(3) != 0
    n = 3 + r.below(9)
    for _ in range(n):
        gap = [lambda: 10 + r.below(280), lambda: 300 + r.below(700), lambda: 1000 + r.below(1500)][r.below(3)]()
        t += gap
        k = r.below(10)
        if k < 4:
            out.append({"t": t, "kind": "write", "path": "pkg/core.py", "content": f"v{r.below(4)}"})
        elif k < 6:
            out.append({"t": t, "kind": "write", "path": "pkg/tmp.txt", "content": None if tmp else "x"})
            tmp = not tmp
        elif k < 7:
            out.append({"t": t, "kind": "write", "path": "NOTES.md", "content": f"note {r.below(3)}"})
        else:
            out.append({"t": t, "kind": "attempt", "fp": f"f{r.below(2)}"})
    if corr:
        out.append({"t": out[r.below(len(out))]["t"] + 1 + r.below(200), "kind": "correction"})
    return sorted(out, key=lambda e: (e["t"], e["kind"] != "write"))


def _tree_metric(tree: dict[str, str]) -> dict[str, Any]:
    return {"ops": OPS[tree.get("pkg/core.py", "v0")]}


def batches(tl: list[dict[str, Any]], merge: bool) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for e in (x for x in tl if x["kind"] == "write"):
        if merge and out and e["t"] - out[-1][-1]["t"] < MERGE_MS:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def render(tl: list[dict[str, Any]], merge: bool) -> Bundle:
    b = Bundle("steering", {**STEERING_KEY, "variant": "easy_wins"}, baseline={"pkg/core.py": "v0"}, baseline_metric={"ops": 100})
    tree = {"pkg/core.py": "v0"}
    spans = []
    for bt in batches(tl, merge):
        before = dict(tree)
        for e in bt:
            if e["content"] is None:
                tree.pop(e["path"], None)
            else:
                tree[e["path"]] = e["content"]
        net = {p: tree.get(p) for p in set(before) | set(tree) if before.get(p) != tree.get(p)}
        n = b.batch(bt[0]["t"], bt[-1]["t"], net, _tree_metric(tree))
        spans.append((n, bt[0]["t"], bt[-1]["t"]))
    for e in tl:
        if e["kind"] == "attempt":
            b.attempt("measure", None, e["fp"], t=e["t"])
        elif e["kind"] == "correction":
            at = e["t"]
            strad = [n for n, lo, hi in spans if lo < at < hi]
            done = [n for n, lo, hi in spans if hi <= at and (not strad or n < strad[0])]
            b.correction(batch=done[-1] if done else 0, straddled=strad[0] if strad else None,
                         straddled_last=strad[-1] if strad else None, t=at)
    return b
