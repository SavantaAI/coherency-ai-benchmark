"""Fixture values (codewords, markers, PINs) for one run.

Every value is derived from the run's seed and a name, so both harnesses in a
run receive the same values, and a rerun with the same seed repeats them.
"""

from __future__ import annotations

import hashlib
from typing import Any


class Fixtures:
    def __init__(self, seed: str):
        self.seed = seed
        self.used: dict[str, Any] = {}

    def _hex(self, name: str) -> str:
        return hashlib.sha256(f"{self.seed}:{name}".encode()).hexdigest()

    def word(self, prefix: str, name: str) -> str:
        value = f"{prefix}-{self._hex(name)[:6].upper()}"
        self.used[name] = value
        return value

    def number(self, name: str, low: int, high: int) -> int:
        value = low + int(self._hex(name)[:12], 16) % (high - low + 1)
        self.used[name] = value
        return value

    def tag(self, name: str) -> str:
        value = self._hex(name)[:4].upper()
        self.used[name] = value
        return value
