"""A seeded random source that gives the same numbers on every Python.

`random.Random` is stable in practice, but its methods are not promised to
stay the same across releases. The generators need byte-identical output on
any Python from 3.12 on, so every draw comes from SHA-256 over a key and a
counter, and the few operations the generators use are written out here.
"""

from __future__ import annotations

import hashlib
from typing import Sequence, TypeVar

T = TypeVar("T")


class Rng:
    def __init__(self, *parts: object):
        self._key = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
        self._counter = 0

    def child(self, *parts: object) -> "Rng":
        """An independent source for one part of a task, so that adding a draw
        in one part never shifts the numbers of another."""
        return Rng(self._key.hex(), *parts)

    def _u64(self) -> int:
        self._counter += 1
        block = hashlib.sha256(self._key + self._counter.to_bytes(8, "big")).digest()
        return int.from_bytes(block[:8], "big")

    def below(self, n: int) -> int:
        """A whole number in [0, n), without bias."""
        if n <= 0:
            raise ValueError("below needs a positive bound")
        limit = (1 << 64) - (1 << 64) % n
        while True:
            x = self._u64()
            if x < limit:
                return x % n

    def between(self, lo: int, hi: int) -> int:
        """A whole number in [lo, hi], both included."""
        return lo + self.below(hi - lo + 1)

    def choice(self, seq: Sequence[T]) -> T:
        return seq[self.below(len(seq))]

    def shuffled(self, seq: Sequence[T]) -> list[T]:
        out = list(seq)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample(self, seq: Sequence[T], k: int) -> list[T]:
        return self.shuffled(seq)[:k]

    def chance(self, num: int, den: int) -> bool:
        return self.below(den) < num

    def hex(self, n: int) -> str:
        out = ""
        while len(out) < n:
            out += hashlib.sha256(self._key + b"hex" + self._u64().to_bytes(8, "big")).hexdigest()
        return out[:n]
