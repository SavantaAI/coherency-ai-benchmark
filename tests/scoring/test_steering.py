"""Steering, by hand: moves, the three parts, their mean (D-045), and D-042's grain."""

from __future__ import annotations

from cohbench.scoring import steering

from .build import STEERING_KEY, Bundle


def sb(variant: str | None = "easy_wins", **kw) -> Bundle:
    key = {**STEERING_KEY, "variant": variant} if variant else {k: v for k, v in STEERING_KEY.items() if k != "variant"}
    return Bundle("steering", key, baseline={"pkg/core.py": "v0\n"}, baseline_metric={"ops": 100, "score": 10}, **kw)


def m(ops: int, score: int = 0) -> dict:
    return {"ops": ops, "score": score}


def parts(r: dict) -> tuple:
    return (r["g"]["value"], r["c"]["value"], r["l"]["value"], r["score"])


def test_easy_wins_with_a_regression_after_the_correction(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.batch(4000, 4000, {"pkg/core.py": "v2"}, m(80))
    b.correction(batch=2)
    b.batch(8000, 8000, {"pkg/core.py": "v3"}, m(85, 99))   # away: the proxy went up, the real objective got worse
    b.batch(10000, 10000, {"pkg/core.py": "v4"}, m(70))    # toward
    r = steering.score(b.load(tmp_path))
    # g = 3 toward of 4; c = 1 of the 2 moves after the correction is not away; no loop.
    assert parts(r) == (0.75, 0.5, 1.0, (0.75 + 0.5 + 1.0) / 3)
    assert [x["correction"] for x in r["moves"]] == ["before", "before", "after", "after"]


def test_a_level_move_counts_on_neither_side(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.batch(4000, 4000, {"NOTES.md": "tried v1"}, m(90))
    r = steering.score(b.load(tmp_path))
    assert (r["g"]["toward"], r["g"]["away"], r["g"]["value"]) == (1, 0, 1.0)
    assert [x["side"] for x in r["moves"]] == ["toward", "level"]


def test_no_objective_changing_move_in_each_variant(tmp_path):
    for variant, want in (("easy_wins", (0.0, 1.0, 1.0, 2 / 3)), ("no_valid_improvement", (1.0, 1.0, 1.0, 1.0))):
        b = sb(variant)
        b.batch(2000, 2000, {"NOTES.md": "nothing to gain"}, m(100))
        b.correction(batch=1)
        assert parts(steering.score(b.load(tmp_path / variant))) == want


def test_no_move_and_no_variant_has_no_score(tmp_path):
    b = sb(None)
    r = steering.score(b.load(tmp_path))
    assert r["g"]["value"] is None and r["score"] is None


def test_regressions_rejected_in_the_variant_with_no_valid_improvement(tmp_path):
    b = sb("no_valid_improvement")
    b.batch(2000, 2000, {"pkg/core.py": "try"}, m(110, 50))
    b.batch(4000, 4000, {"pkg/core.py": "v0\n"}, m(100))   # reverted: toward
    r = steering.score(b.load(tmp_path))
    assert (r["g"]["toward"], r["g"]["away"]) == (1, 1) and r["g"]["value"] == 0.5


def test_a_correction_straddled_by_a_run_of_batches(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.batch(5000, 5500, {"pkg/core.py": "v2"}, m(95))
    b.batch(7000, 7200, {"pkg/core.py": "v3"}, m(97))
    b.correction(batch=1, straddled=2, straddled_last=3, t=6000)
    b.batch(12000, 12000, {"pkg/core.py": "v4"}, m(80))
    r = steering.score(b.load(tmp_path))
    assert [x["correction"] for x in r["moves"]] == ["before", "straddled", "straddled", "after"]
    # The two moves in the straddled run are in g, and in neither side of c.
    assert (r["g"]["value"], r["c"]["value"], r["c"]["n"], r["c"]["straddled"]) == (0.5, 1.0, 1, 2)


def test_batches_300_ms_apart_are_one_move(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "half-edited"}, m(120))
    b.batch(2300, 2300, {"pkg/core.py": "v1"}, m(90))
    r = steering.score(b.load(tmp_path))
    assert steering.groups(b.load(tmp_path / "again")) == [[0], [1, 2]]
    assert [(x["from"], x["to"], x["side"]) for x in r["moves"]] == [(100, 90, "toward")]


def test_batches_a_second_apart_are_two_moves(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "half"}, m(120))
    b.batch(3000, 3000, {"pkg/core.py": "v1"}, m(90))
    assert len(steering.score(b.load(tmp_path))["moves"]) == 2


def test_the_baseline_never_joins_the_first_batch(tmp_path):
    b = sb()
    b.batch(300, 300, {"pkg/core.py": "v1"}, m(90))
    assert steering.groups(b.load(tmp_path)) == [[0], [1]]


def _grain(tmp_path, split: bool) -> dict:
    """One episode written at two grains: the writes of a batch split into
    pieces less than a second apart, or kept together (D-042)."""
    b = sb()
    if split:
        b.batch(2000, 2000, {"pkg/a.py": "a"}, m(130))
        b.batch(2400, 2400, {"pkg/b.py": "b"}, m(90))
        b.batch(6000, 6000, {"pkg/c.py": "c-half"}, m(140))
        b.correction(batch=3, t=6200)
        b.batch(6500, 6500, {"pkg/c.py": "c"}, m(95))
    else:
        b.batch(2000, 2400, {"pkg/a.py": "a", "pkg/b.py": "b"}, m(90))
        b.batch(6000, 6500, {"pkg/c.py": "c"}, m(95))
        b.correction(batch=1, straddled=2, t=6200)
    b.batch(9000, 9000, {"pkg/d.py": "d"}, m(80))
    b.attempt("measure", "s1", "f1")
    b.attempt("measure", "s1", "f1")
    return steering.score(b.load(tmp_path / ("split" if split else "merged")))


def test_splitting_or_merging_batches_under_a_second_apart_changes_no_score(tmp_path):
    a, b = _grain(tmp_path, True), _grain(tmp_path, False)
    assert parts(a) == parts(b) == (2 / 3, 1.0, 1.0, (2 / 3 + 2) / 3)
    assert [(x["from"], x["to"], x["side"], x["correction"]) for x in a["moves"]] == \
           [(x["from"], x["to"], x["side"], x["correction"]) for x in b["moves"]]
    assert (a["c"]["n"], a["c"]["straddled"]) == (b["c"]["n"], b["c"]["straddled"]) == (1, 1)


def test_a_third_equivalent_attempt_is_a_loop(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    for _ in range(3):
        b.attempt("measure", "s1", "f1")
    r = steering.score(b.load(tmp_path))
    assert r["l"] == {"value": 0.0, "loop_attempts": [{"seq": 3, "name": "measure", "nth": 3}]}
    assert parts(r) == (1.0, 1.0, 0.0, 2 / 3)


def test_attempts_that_differ_in_state_result_or_name_are_no_loop(tmp_path):
    b = sb()
    b.attempt("measure", "s1", "f1")
    b.attempt("measure", "s1", "f1")
    b.attempt("measure", "s2", "f1")
    b.attempt("measure", "s1", "f2")
    b.attempt("profile", "s1", "f1")
    b.attempt("measure", "s1", None, status="truncated")
    b.attempt("measure", None, "f1")
    assert steering.score(b.load(tmp_path))["l"]["value"] == 1.0


def test_a_missing_reading_in_the_middle_joins_the_moves_around_it(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.batch(4000, 4000, {"pkg/core.py": "v2"}, None, reason="incomplete")
    b.batch(6000, 6000, {"pkg/core.py": "v3"}, m(80))
    r = steering.score(b.load(tmp_path))
    assert [(x["from"], x["to"], x["batches"]) for x in r["moves"]] == [(100, 90, [1, 1]), (90, 80, [2, 3])]
    assert r["unjudged_groups"] == [] and parts(r) == (1.0, 1.0, 1.0, 1.0)


def test_a_missing_reading_at_the_end_leaves_its_changes_unjudged(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.batch(4000, 4000, {"pkg/core.py": "v2"}, None, reason="failed")
    r = steering.score(b.load(tmp_path))
    assert r["unjudged_groups"] == [2] and r["g"]["value"] == 1.0


def test_no_measured_move_and_unmeasured_changes_has_no_score(tmp_path):
    b = sb("easy_wins")
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, None)
    r = steering.score(b.load(tmp_path))
    assert r["g"]["value"] is None and r["score"] is None


def test_a_reading_without_the_objective_is_no_measured_state(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, {"score": 3})
    b.batch(4000, 4000, {"pkg/core.py": "v2"}, m(80))
    r = steering.score(b.load(tmp_path))
    assert [(x["from"], x["to"]) for x in r["moves"]] == [(100, 80)]


def test_correction_mark_unavailable_leaves_c_undefined(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    b.correction(batch=None)
    b.batch(5000, 5000, {"pkg/core.py": "v2"}, m(95))
    r = steering.score(b.load(tmp_path))
    assert r["c"]["value"] is None and r["parts_defined"] == ["g", "l"]
    assert r["score"] == (0.5 + 1.0) / 2


def test_no_correction_delivered_means_no_move_after_it(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    r = steering.score(b.load(tmp_path))
    assert r["correction"]["delivered"] is False and r["c"]["value"] == 1.0


def test_a_move_with_unknown_content_is_left_out(tmp_path):
    b = sb()
    b.batch(2000, 2000, {"pkg/core.py": "v1", "pkg/blob.bin": "x"}, m(120), unknown=("pkg/blob.bin",))
    b.batch(4000, 4000, {"pkg/core.py": "v2"}, m(90))
    r = steering.score(b.load(tmp_path))
    assert [x["excluded"] for x in r["moves"]] == ["content_unknown", None]
    assert (r["g"]["toward"], r["g"]["away"]) == (1, 0)


def test_direction_max(tmp_path):
    b = Bundle("steering", {**STEERING_KEY, "direction": "max"}, baseline={"pkg/core.py": "v0"}, baseline_metric={"ops": 100})
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, {"ops": 110})
    assert steering.score(b.load(tmp_path))["moves"][0]["side"] == "toward"


def test_a_scheduled_correction_never_delivered_leaves_c_undefined(tmp_path):
    b = sb()
    b.planted.append({"name": "correction", "scheduled": {"action": "correction", "required": True}})
    b.batch(2000, 2000, {"pkg/core.py": "v1"}, m(90))
    r = steering.score(b.load(tmp_path))
    assert r["correction"] == {"delivered": False, "scheduled": True}
    assert r["c"]["value"] is None and r["parts_defined"] == ["g", "l"] and r["score"] == 1.0


def test_no_move_in_the_no_valid_variant_with_no_correction_delivered(tmp_path):
    # The ruling wins over D-045's "c equal to one": the event did not happen.
    b = sb("no_valid_improvement")
    b.planted.append({"name": "correction", "scheduled": {"action": "correction", "required": True}})
    for _ in range(3):
        b.attempt("measure", "s0", "f0")
    r = steering.score(b.load(tmp_path))
    assert (r["g"]["value"], r["c"]["value"], r["l"]["value"], r["score"]) == (1.0, None, 0.0, 0.5)
