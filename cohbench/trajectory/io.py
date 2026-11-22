"""Reading and writing normalised trajectories and run manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .format import CHANGELOG_FORMAT, FORMAT, MANIFEST_FORMAT, VERSION, VIEW_FORMAT, canonical


@dataclass
class Trajectory:
    header: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    # For a scorer view: view seq -> the full trajectory seq it came from.
    # Kept in memory only, never written.
    sources: dict[int, int] = field(default_factory=dict, compare=False, repr=False)
    # For a scorer view: view seq -> wall microseconds from the origin, the
    # precision every decision uses. In memory only, never written.
    wall: dict[int, int] = field(default_factory=dict, compare=False, repr=False)

    def dumps(self) -> str:
        """The file's exact text. Byte-identical for equal content."""
        return "".join(canonical(line) + "\n" for line in [self.header, *self.events])

    def sha256(self) -> str:
        return hashlib.sha256(self.dumps().encode()).hexdigest()


class FormatError(ValueError):
    pass


def loads(text: str) -> Trajectory:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise FormatError("empty file")
    parsed = []
    for n, line in enumerate(lines, 1):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise FormatError(f"line {n}: {exc}") from None
        if not isinstance(parsed[-1], dict):
            raise FormatError(f"line {n}: not an object")
    header = parsed[0]
    if header.get("type") != "header" or header.get("format") not in (FORMAT, VIEW_FORMAT, CHANGELOG_FORMAT):
        raise FormatError("first line is not a cohbench trajectory, scorer view or change log header")
    if header.get("version") != VERSION:
        raise FormatError(f"version {header.get('version')!r}; this reader knows version {VERSION}")
    return Trajectory(header=header, events=parsed[1:])


def read(path: str | Path) -> Trajectory:
    return loads(Path(path).read_text(encoding="utf-8"))


def write(traj: Trajectory, path: str | Path) -> None:
    Path(path).write_text(traj.dumps(), encoding="utf-8")


def write_manifest(manifest: dict[str, Any], path: str | Path) -> None:
    if manifest.get("format") != MANIFEST_FORMAT:
        raise FormatError("not a run manifest")
    Path(path).write_text(json.dumps(manifest, sort_keys=True, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")


def read_manifest(path: str | Path) -> dict[str, Any]:
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    if m.get("format") != MANIFEST_FORMAT:
        raise FormatError("not a run manifest")
    return m
