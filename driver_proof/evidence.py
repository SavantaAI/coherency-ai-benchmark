"""Evidence recording: checks, raw files, and the per-operation JSON.

Verdicts:

* ``fail``: any deciding or hygiene check failed, or the op raised. This wins
  over everything else.
* ``blocked``: no deciding or hygiene check failed, and a stage's target
  behaviour was never exercised because a precondition outside the driver
  failed (today: the gateway log shows the model API refusing the harness's
  requests). The cause and its structured evidence are recorded.
* ``pass``: every deciding and hygiene check passed, there was at least one
  deciding check, and nothing was blocked.

Every file written through the recorder is checked for the key in memory
before it reaches disk. Files copied from a container are scanned as a tar
stream before extraction (container.copy_out_scanned).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .container import KeySource, text_has_key


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Blocked(Exception):
    """Raised when a precondition outside the driver fails. The op stops there."""

    def __init__(self, cause: str, detail: Any = None):
        super().__init__(cause)
        self.cause = cause
        self.detail = detail


@dataclass
class Check:
    name: str
    passed: bool
    detail: Any = None
    # "decides": part of the verdict. "info": recorded only. "hygiene": key,
    # naming and cleanup checks; a failure fails the op even when it is blocked.
    kind: str = "decides"


@dataclass
class Recorder:
    harness: str
    op: str
    out_dir: Path
    key: KeySource
    started_at: str = field(default_factory=now_iso)
    checks: list[Check] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_files: list[str] = field(default_factory=list)
    error: str | None = None
    blocked: dict[str, Any] | None = None
    fixtures: Any = None

    def __post_init__(self) -> None:
        # Each run has its own directory; an existing raw dir means a reused run id.
        if self.raw_dir.exists():
            raise RuntimeError(f"{self.raw_dir} exists; every run writes under its own run id")
        self.raw_dir.mkdir(parents=True)

    @property
    def raw_dir(self) -> Path:
        return self.out_dir / self.op

    def check(self, name: str, passed: bool, detail: Any = None, *, decides: bool = True, hygiene: bool = False) -> bool:
        kind = "hygiene" if hygiene else ("decides" if decides else "info")
        self.checks.append(Check(name, bool(passed), detail, kind))
        return bool(passed)

    def event(self, what: str, **data: Any) -> None:
        self.events.append({"at": now_iso(), "t": round(time.time(), 3), "what": what, **data})

    def block(self, cause: str, detail: Any = None) -> None:
        if self.blocked is None:
            self.blocked = {"cause": cause, "detail": detail}

    # ----- files -------------------------------------------------------------

    def _write(self, path: Path, text: str) -> Path:
        if text_has_key(text, self.key):
            self.check(f"evidence file {path.name} would hold the key; not written", False, hygiene=True)
            path.write_text("[not written: the content held the API key]\n")
        else:
            path.write_text(text)
        self.raw_files.append(str(path.relative_to(self.out_dir)))
        return path

    def save_json(self, name: str, data: Any) -> Path:
        path = self.raw_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._write(path, json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def save_text(self, name: str, text: str) -> Path:
        path = self.raw_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._write(path, text)

    def register(self, path: Path) -> None:
        """Record a file that was scanned before it was written."""
        self.raw_files.append(str(path.relative_to(self.out_dir)))

    # ----- verdict -----------------------------------------------------------

    def verdict(self) -> str:
        if self.error is not None or any(not c.passed for c in self.checks if c.kind in ("decides", "hygiene")):
            return "fail"
        if self.blocked is not None:
            return "blocked"
        return "pass" if any(c.kind == "decides" for c in self.checks) else "fail"

    def write(self, versions: dict[str, Any]) -> Path:
        result = {
            "harness": self.harness,
            "op": self.op,
            "verdict": self.verdict(),
            "blocked": self.blocked,
            "started_at": self.started_at,
            "finished_at": now_iso(),
            "error": self.error,
            "checks": [{"name": c.name, "passed": c.passed, "kind": c.kind, "detail": c.detail} for c in self.checks],
            "facts": self.facts,
            "events": self.events,
            "versions": versions,
            "raw_files": sorted(set(self.raw_files)),
        }
        path = self.out_dir / f"{self.op}.json"
        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        if text_has_key(text, self.key):
            raise RuntimeError(f"refusing to write {path}: the result held the API key")
        path.write_text(text)
        return path
