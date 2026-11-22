"""Statistics on inputs whose answer is known."""

from __future__ import annotations

import pytest

from cohbench.scoring import stats
from cohbench.scoring.report import subscores
from cohbench.scoring.rng import Rng, stream


def test_headline_formula_and_floor():
    assert stats.headline(1.0, 1.0, 1.0) == pytest.approx(100.0)
    assert stats.headline(0.0, 1.0, 1.0) == pytest.approx(100 * 0.05 ** (1 / 3))
    assert stats.headline(0.5, 0.4, 0.2) == pytest.approx(100 * (0.5 * 0.4 * 0.2) ** (1 / 3))
    assert stats.headline(None, 1.0, 1.0) is None


def test_quantile_type_7():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert stats.quantile(xs, 0.0) == 1.0 and stats.quantile(xs, 1.0) == 4.0
    assert stats.quantile(xs, 0.5) == 2.5 and stats.quantile(xs, 0.25) == 1.75


def test_rng_is_fixed_by_its_definition():
    # SplitMix64 from seed 0: the first outputs of the reference generator.
    r = Rng(0)
    assert [r.next64() for _ in range(2)] == [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4]
    assert stream("x", 1).next64() == 9726617011091689414


def test_identical_units_give_a_point_interval():
    units = {"routing": [], "explanation": [[0.5], [0.5], [0.5]], "steering": []}
    draws = stats.bootstrap(units, subscores, stream("t"), 200)
    assert stats.interval([d["E"] for d in draws], 0.95) == [0.5, 0.5]


def test_two_seeds_zero_and_one():
    # Resampling two seeds gives means 0, 0.5 and 1 with chances 1/4, 1/2, 1/4:
    # with 2000 draws the 95% interval is [0, 1] and the draws' mean is near 0.5.
    units = {"routing": [], "explanation": [], "steering": [[0.0], [1.0]]}
    draws = [d["S"] for d in stats.bootstrap(units, subscores, stream("t2"), 2000)]
    assert set(draws) == {0.0, 0.5, 1.0}
    assert stats.interval(draws, 0.95) == [0.0, 1.0]
    assert abs(sum(draws) / len(draws) - 0.5) < 0.03
    assert abs(draws.count(0.5) / 2000 - 0.5) < 0.04


def test_sibling_pairs_stay_together():
    # Three seeds, one pair each: R in a draw is a multiple of 1/3, never a sibling's half.
    units = {"routing": [[True], [False], [False]], "explanation": [], "steering": []}
    draws = [d["R"] for d in stats.bootstrap(units, subscores, stream("t3"), 500)]
    assert set(round(x * 3, 9) for x in draws) <= {0.0, 1.0, 2.0, 3.0}


def test_bootstrap_is_deterministic():
    units = {"routing": [[True], [False]], "explanation": [[0.2], [0.9]], "steering": [[0.4], [0.6]]}
    a = stats.bootstrap(units, subscores, stream("same"), 100)
    b = stats.bootstrap(units, subscores, stream("same"), 100)
    assert a == b and a != stats.bootstrap(units, subscores, stream("other"), 100)


def test_rank_bands_from_overlap():
    rows = [("a", 80, [75, 85]), ("b", 70, [60, 78]), ("c", 40, [30, 50]), ("d", 10, [5, 12])]
    b = stats.rank_bands(rows)
    assert {k: v["ranks"] for k, v in b.items()} == {"a": "1-2", "b": "1-2", "c": "3", "d": "4"}


def test_rank_bands_chain():
    # a overlaps b, b overlaps c, a and c do not: one connected band.
    b = stats.rank_bands([("a", 80, [70, 90]), ("b", 65, [55, 72]), ("c", 50, [40, 56])])
    assert {v["ranks"] for v in b.values()} == {"1-3"} and {v["band"] for v in b.values()} == {1}


def test_rank_bands_disjoint():
    b = stats.rank_bands([("x", 10, [9, 11]), ("y", 30, [29, 31]), ("z", 20, [19, 21])])
    assert {k: v["ranks"] for k, v in b.items()} == {"y": "1", "z": "2", "x": "3"}


def test_spearman():
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert stats.spearman([1, 2, 3], [1, 1, 1]) is None
    assert stats.spearman([1, 2], [1, 2]) is None
    assert stats.ranks([3, 1, 3, 2]) == [3.5, 1.0, 3.5, 2.0]


def test_near_chance():
    assert stats.near_chance(1, 6, 1 / 6, 2, True)["passed"] is True
    assert stats.near_chance(60, 60, 1 / 6, 2, True)["passed"] is False
    assert stats.near_chance(0, 60, 1 / 6, 2, False)["passed"] is True     # low is fine for the buried cause
    assert stats.near_chance(0, 60, 1 / 6, 2, True)["passed"] is False     # but far below chance with no answer is a bias
    assert stats.near_chance(0, 0, 1 / 6, 2, True)["passed"] is None
