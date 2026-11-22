"""One episode's scoring bundle, read for the scorers.

This is the only module of the unit that opens files of a bundle, and it
opens nothing else: the five files and `blobs/` (D-018). It goes through
`cohbench.trajectory.bundle.read`, and `load(..., validate=True)` refuses a
bundle that `bundle.validate` rejects.

Nothing here, and nothing a scorer computes from it, depends on the
directory's name or place: the score file names the episode by its id and
the bundle by a digest of its five files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cohbench.trajectory import bundle as B
from cohbench.trajectory.changelog import ChangeLog

REQUEST_LABELS = ("answerable", "already_given", "unanswerable")


class ScoreError(ValueError):
    """The bundle cannot be scored (missing, malformed, or rejected by the validator)."""


@dataclass
class Episode:
    view: Any  # cohbench.trajectory.io.Trajectory
    changes: ChangeLog
    observations: list[dict[str, Any]]
    identifiers: dict[str, Any]
    doc: dict[str, Any]
    blobs: Path | None
    digest: str
    _trees: dict[int, Any] = field(default_factory=dict, repr=False)

    # The episode record -------------------------------------------------
    @property
    def grouping(self) -> dict[str, Any]:
        return self.doc["grouping"]

    @property
    def family(self) -> str:
        return self.grouping["family"]

    @property
    def payload(self) -> dict[str, Any]:
        return self.doc["answer_key"]["payload"]

    # The scorer view -----------------------------------------------------
    @property
    def events(self) -> list[dict[str, Any]]:
        return self.view.events

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.view.events if e.get("type") in types]

    def by_seq(self, seq: int) -> dict[str, Any] | None:
        ev = self.view.events
        if 0 <= seq < len(ev) and ev[seq].get("seq") == seq:
            return ev[seq]
        return next((e for e in ev if e.get("seq") == seq), None)

    def verdicts(self) -> dict[int, dict[str, Any]]:
        """Output seq -> its verdict (one per output in a bundle with a runner record)."""
        return {e["target"]: e for e in self.of_type("verdict")}

    def outputs(self) -> list[dict[str, Any]]:
        return self.of_type("to_user")

    def opening_question(self) -> str:
        m = next((e for e in self.of_type("user_message") if e.get("kind") == "opening_prompt"), None)
        return m["text"] if m else ""

    def fired(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self.of_type("planted") if e.get("name") == name and e.get("phase") == "fired"), None)

    # The change log ------------------------------------------------------
    def batch_records(self) -> list[dict[str, Any]]:
        return [r for r in self.changes.records if r.get("type") == "batch"]

    def final_batch(self) -> int | None:
        b = self.batch_records()
        return b[-1]["batch"] if b else None

    def tree(self, batch: int | None) -> dict[str, tuple[Any, ...]]:
        if batch is None:
            return {}
        if batch not in self._trees:
            self._trees[batch] = (self.changes.tree(batch), self.changes.unknown(batch))
        return self._trees[batch][0]

    def unknown(self, batch: int | None) -> set[str]:
        if batch is None:
            return set()
        self.tree(batch)
        return self._trees[batch][1]

    def reading(self, batch: int) -> dict[str, Any] | None:
        """The batch's kept metric reading's values, or None (a `no_metric` row)."""
        for r in self.changes.records:
            if r.get("type") == "metric" and r.get("batch") == batch:
                return r.get("metrics")
        return None

    def content(self, sha: str | None) -> bytes | None:
        """A stored file content by its digest, from the bundle's own `blobs/`."""
        if not sha or self.blobs is None:
            return None
        p = self.blobs / sha[:2] / sha
        if not p.is_file():
            return None
        data = p.read_bytes()
        return data if hashlib.sha256(data).hexdigest() == sha else None


def bundle_digest(directory: Path) -> str:
    """sha256 over the five files, by name and bytes, in the bundle's own order."""
    h = hashlib.sha256()
    for name in B.FILES:
        p = directory / name
        data = p.read_bytes() if p.is_file() else b""
        h.update(name.encode("ascii") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return h.hexdigest()


def load(directory: str | Path, validate: bool = True) -> Episode:
    d = Path(directory)
    if not d.is_dir():
        raise ScoreError(f"not a bundle directory: {d.name}")
    try:
        b = B.read(d)
    except Exception as exc:  # noqa: BLE001 - every read failure is a refusal, never a partial score
        raise ScoreError(f"the bundle cannot be read ({type(exc).__name__}: {exc})"[:300]) from None
    if b.episode is None:
        raise ScoreError("the bundle has no episode.json; a scorer needs the episode record")
    if validate:
        rep = B.validate(b)
        if not rep["ok"]:
            first = next(iter(rep["errors"].items()))
            raise ScoreError(f"the bundle does not validate: {first[0]}: {first[1][0]}"[:400])
    obs = [r for r in b.observations.records if r.get("type") == "attempt"]
    return Episode(view=b.view, changes=b.changes, observations=obs, identifiers=b.identifiers, doc=b.episode,
                   blobs=d / "blobs", digest=bundle_digest(d))
