"""A small seeded generator whose output is fixed by its definition, not by
the Python version: SplitMix64, with streams derived from a sha256 of their
names. Option orders and bootstrap draws come from it, so the same seeds give
the same numbers in any process and any Python from 3.12 on."""

from __future__ import annotations

import hashlib

MASK = (1 << 64) - 1


class Rng:
    def __init__(self, seed: int):
        self.state = seed & MASK

    def next64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        """A uniform integer in [0, n), by rejection, so no value is favoured."""
        if n <= 0:
            raise ValueError("below() needs a positive bound")
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            x = self.next64()
            if x < limit:
                return x % n

    def shuffled(self, items: list) -> list:
        """A Fisher-Yates shuffle of a copy of `items`."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out


def stream(*parts: object) -> Rng:
    """A generator named by `parts`: the same names give the same stream."""
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return Rng(int.from_bytes(h[:8], "big"))


def option_orders(count: int, seed: int, n: int = 6) -> list[list[int]]:
    """`count` orders of `n` options, `count` a positive multiple of `n`.

    Each block of `n` orders is the `n` cyclic rotations of one seeded
    permutation, so within a block every option is shown once in every
    position. A reader that always picks one position is then right exactly
    one time in `n`, whatever the true cause's index."""
    if count <= 0 or count % n:
        raise ValueError(f"the number of option orders must be a positive multiple of {n}")
    out = []
    for block in range(count // n):
        base = stream("option-orders", seed, block).shuffled(list(range(n)))
        for r in range(n):
            out.append([base[(i + r) % n] for i in range(n)])
    return out
