"""Statistics for the report: the headline, the seed bootstrap, rank bands,
Spearman's rank correlation and a quantile rule. Arithmetic only, in a fixed
order, so the results are the same in every process."""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Callable

from .rng import Rng

FLOOR = 0.05
DIGITS = 6


def published(value):
    """The numeric precision of score and report files, also used for ranking."""
    if not math.isfinite(value):
        raise ValueError("a score is never infinite or NaN")
    result = round(value, DIGITS)
    return 0.0 if result == 0 else result


def headline(r: float | None, e: float | None, s: float | None) -> float | None:
    """100 * (max(R, 0.05) * max(E, 0.05) * max(S, 0.05)) ^ (1/3); None when a sub-score is not measured."""
    if r is None or e is None or s is None:
        return None
    product = math.prod(max(Fraction(v), Fraction(1, 20)) for v in (r, e, s))
    return 100 * float(product) ** (1 / 3)


def quantile(sorted_xs: list[float], q: float) -> float:
    """Linear interpolation between order statistics (the common type 7 rule)."""
    if not sorted_xs:
        raise ValueError("no values")
    h = (len(sorted_xs) - 1) * Fraction(str(q))
    lo = math.floor(h)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (h - lo) * (sorted_xs[hi] - sorted_xs[lo])


def interval(draws: list[float], level: float) -> list[float] | None:
    xs = sorted(d for d in draws if d is not None)
    if not xs:
        return None
    a = (1 - Fraction(str(level))) / 2
    return [quantile(xs, a), quantile(xs, 1 - a)]


def resample(rng: Rng, units: list[Any]) -> list[Any]:
    """Draw len(units) units with replacement."""
    n = len(units)
    return [units[rng.below(n)] for _ in range(n)]


def bootstrap(units: dict[str, list[Any]], stat: Callable[[dict[str, list[Any]]], dict[str, float | None]], rng: Rng | dict[str, Rng], resamples: int) -> list[dict[str, float | None]]:
    """`units` maps each family to its resampling units (one per seed, holding
    everything of that seed, sibling pairs whole). Each draw resamples every
    family's seeds independently and applies `stat`. A family with no units
    is passed through empty."""
    out = []
    for _ in range(resamples):
        draw = {f: (resample(rng[f] if isinstance(rng, dict) else rng, us) if us else []) for f, us in sorted(units.items())}
        out.append(stat(draw))
    return out


def overlaps(a: list[float], b: list[float]) -> bool:
    return published(a[0]) <= published(b[1]) and published(b[0]) <= published(a[1])


def rank_bands(rows: list[tuple[str, float, list[float]]]) -> dict[str, dict[str, Any]]:
    """Rank bands from (key, point, [lo, hi]).

    Configurations are linked when their intervals overlap; a band is a
    connected group of linked configurations. Bands are ordered by the
    highest upper bound in them, and a band covers the ranks its members would
    take in order of their point values: a band of three after a band of one
    is ranks 2 to 4."""
    order = sorted(((key, published(point), [published(v) for v in iv]) for key, point, iv in rows),
                   key=lambda r: (-r[1], r[0]))
    parent = {r[0]: r[0] for r in order}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(order):
        for b in order[i + 1:]:
            if overlaps(a[2], b[2]):
                ra, rb = find(a[0]), find(b[0])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    comps: dict[str, list[tuple[str, float, list[float]]]] = {}
    for r in order:
        comps.setdefault(find(r[0]), []).append(r)
    bands = sorted(comps.values(), key=lambda c: (-max(x[2][1] for x in c), -max(x[1] for x in c), min(x[0] for x in c)))
    out: dict[str, dict[str, Any]] = {}
    start = 1
    for n, members in enumerate(bands, 1):
        end = start + len(members) - 1
        label = str(start) if start == end else f"{start}-{end}"
        for m in members:
            out[m[0]] = {"band": n, "ranks": label, "members": len(members)}
        start = end + 1
    return out


def ranks(xs: list[float | Fraction]) -> list[float]:
    """Ranks from exact counts or published values, ties sharing their mean rank."""
    xs = [published(x) if isinstance(x, float) else x for x in xs]
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs: list[float | Fraction], ys: list[float | Fraction]) -> float | None:
    """Spearman's rho, Pearson's correlation of the ranks; None for fewer than
    three pairs or a constant side."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def near_chance(correct: int, n: int, chance: float, z: float, two_sided: bool, clusters: int | None = None) -> dict[str, Any]:
    """A sanity check on a reader: is the observed accuracy within z standard
    errors of chance (two-sided), or not above it (one-sided)? The `n`
    observations come in `clusters` items whose answers need not be
    independent (one item's option orders), so the standard error counts
    items: se = sqrt(chance (1 - chance) / clusters). Without `clusters`,
    every observation is its own item."""
    m = n if clusters is None else clusters
    if n == 0 or m == 0:
        return {"accuracy": None, "n": n, "clusters": m, "passed": None}
    acc = correct / n
    se = math.sqrt(chance * (1 - chance) / m)
    hi = min(1.0, chance + z * se)
    lo = max(0.0, chance - z * se)
    ok = (lo <= acc <= hi) if two_sided else (acc <= hi)
    return {"accuracy": acc, "n": n, "clusters": m, "chance": chance, "bound": [lo, hi] if two_sided else [0.0, hi], "passed": ok}
