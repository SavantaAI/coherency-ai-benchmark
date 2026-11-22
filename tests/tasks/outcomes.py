"""Test doubles for the scoring unit's readers, and outcome helpers shared by
the run tests (`test_runs.py`, the real runner) and the long-horizon tests
(`test_long.py`, the D-046 stand-in)."""

from __future__ import annotations

import hashlib
from typing import Any

from cohbench.scoring import checks
from cohbench.scoring import routing as scoring_routing


class Tree:
    """The scoring unit's check functions read a tree through these three
    calls; this serves a plain {path: text} tree the same way."""

    def __init__(self, files: dict[str, str]):
        self.blobs = {hashlib.sha256(t.encode()).hexdigest(): t.encode() for t in files.values()}
        self.files = {p: ("file", "0644", hashlib.sha256(t.encode()).hexdigest()) for p, t in files.items()}

    def tree(self, batch: Any) -> dict[str, tuple[Any, ...]]:
        return self.files

    def unknown(self, batch: Any) -> set[str]:
        return set()

    def content(self, sha: str | None) -> bytes | None:
        return self.blobs.get(sha or "")


def check(files: dict[str, str], spec: Any) -> bool | None:
    return checks.run(Tree(files), 0, spec)["held"]


class RoutingEpisode(Tree):
    """What `cohbench.scoring.routing.score` reads from an episode: the
    answer key, the grouping, the reach, the outputs with their verdicts, the
    conflict's fired row and the final tree. Positions in the view follow the
    instants: outputs at their delivery, the conflict at its firing."""

    def __init__(self, files: dict[str, str], task: dict[str, Any], outputs: list[tuple[str, str, str | None]],
                 conflict_at: str | float | None):
        super().__init__(files)
        self.payload = task["scoring"]["answer_key"]
        self.grouping = task["grouping"]
        self.doc = {"reach": {"status": "reached", "not_reached": []}}
        rows = [(t, 1, label, reply) for t, label, reply in outputs]
        if conflict_at is not None:
            rows.append((conflict_at, 0, None, None))
        rows.sort(key=lambda r: (r[0], r[1]))
        self._outputs, self._verdicts, self._conflict = [], {}, None
        for seq, (_t, kind, label, reply) in enumerate(rows):
            if kind == 0:
                self._conflict = seq
            else:
                self._outputs.append({"seq": seq})
                self._verdicts[seq] = {"label": label, "reply": reply}

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return []

    def fired(self, name: str) -> dict[str, Any] | None:
        return {"seq": self._conflict} if name == "conflict" and self._conflict is not None else None

    def verdicts(self) -> dict[int, dict[str, Any]]:
        return self._verdicts

    def outputs(self) -> list[dict[str, Any]]:
        return self._outputs

    def final_batch(self) -> int:
        return 0


def routing_passed(files: dict[str, str], task: dict[str, Any], outputs: list[tuple[Any, str, str | None]], conflict_at: Any) -> bool | None:
    return scoring_routing.score(RoutingEpisode(files, task, outputs, conflict_at))["passed"]


def moves(values: list[int | None]) -> list[str]:
    """The objective's moves between measured states, as D-045 counts them
    (each changing step here is its own batch, seconds apart)."""
    seen = [v for v in values if v is not None]
    return ["toward" if b < a else "away" if b > a else "level" for a, b in zip(seen, seen[1:])]
