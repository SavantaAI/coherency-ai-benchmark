"""The runner's own append-only event log.

One JSON object per line, written and flushed to disk as each decision is
made, so an episode that crashes still leaves what was sent and why. Every
line has `n` (from 1), `utc`, `clock_ms` (the episode clock in ms from the
opening prompt, D-020; null before it was sent) and `type`. `replay_sent` rebuilds every user message
from the log alone.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


def utc_iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    def __init__(self, path: Path, utcnow: Callable[[], datetime], clock: Callable[[], float | None] = lambda: None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._utcnow = utcnow
        self._clock = clock
        self.n = 0

    def set_clock(self, clock: Callable[[], float | None]) -> None:
        """The episode clock relative to the opening prompt (D-020); None before it."""
        self._clock = clock

    def write(self, type: str, **fields: Any) -> dict[str, Any]:
        self.n += 1
        c = self._clock()
        rec = {"n": self.n, "utc": utc_iso(self._utcnow()), "clock_ms": None if c is None else round(c * 1000), "type": type, **fields}
        self._fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        return rec

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def read(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.endswith("\n") and line.strip():
                yield json.loads(line)


def replay_sent(path: Path) -> list[dict[str, Any]]:
    """Every user message the runner sent, from the log alone: kind, how it
    was sent, its text, the parts it was made of, and for an answer the map
    the driver submitted (m2)."""
    return [{"kind": r["kind"], "via": r["via"], "text": r["text"], "parts": r["parts"], "submitted": r.get("submitted")}
            for r in read(path) if r["type"] == "send"]
