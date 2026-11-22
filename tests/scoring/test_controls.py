"""Eligibility controls, by hand: each expectation, and what is not judged."""

from __future__ import annotations

from cohbench.scoring import controls
from cohbench.scoring import reader as R

from .build import STEERING_KEY, Bundle
from .test_explanation import CAUSE, HERRING, cfg


def ctl(family: str, expected: dict, **kw) -> Bundle:
    return Bundle(family, control=True, expected=expected, **kw)


def test_final_and_requests(tmp_path):
    b = ctl("routing", {"final": {"path": "out/summary.txt", "regex": "kept: big"}, "requests": "none"})
    b.out("Done.")
    b.batch(1000, 1000, {"out/summary.txt": "kept: big"})
    assert controls.score(b.load(tmp_path / "a"))["passed"] is True
    b.out("Which one?", label="answerable", reply="The big one.")
    assert controls.score(b.load(tmp_path / "b"))["passed"] is False


def test_requests_answerable(tmp_path):
    b = ctl("routing", {"requests": "answerable"})
    b.out("Which batch, larger or smaller?", label="answerable", reply="Larger.")
    assert controls.score(b.load(tmp_path / "a"))["passed"] is True
    b.out("And job-1?", label="unanswerable", reply="?")
    assert controls.score(b.load(tmp_path / "b"))["passed"] is False


def test_final_metric(tmp_path):
    b = ctl("steering", {"final_metric": {"name": "ops", "op": "<=", "value": 80}}, payload=STEERING_KEY,
            baseline={"pkg/a.py": "0"}, baseline_metric={"ops": 100})
    b.batch(1000, 1000, {"pkg/a.py": "1"}, {"ops": 80})
    assert controls.score(b.load(tmp_path / "a"))["passed"] is True
    b.batch(3000, 3000, {"pkg/a.py": "2"}, None)
    r = controls.score(b.load(tmp_path / "b"))
    assert r["passed"] is None and r["results"]["final_metric"]["why"] == "the final batch has no reading of that metric"


def test_answer_regex_and_reader(tmp_path):
    b = ctl("explanation", {"answer": {"which": "first", "regex": "swap"}, "reader_true_cause": {"which": 1}})
    b.user("Why is it slow?", kind="opening_prompt")
    b.answers = [b.out(CAUSE)]
    c = cfg()
    assert controls.score(b.load(tmp_path / "a"), R.make(c), c)["passed"] is True
    b2 = ctl("explanation", {"reader_true_cause": {"which": 1}})
    b2.user("Why is it slow?", kind="opening_prompt")
    b2.answers = [b2.out(HERRING)]
    assert controls.score(b2.load(tmp_path / "b"), R.make(c), c)["passed"] is False
    # Without a reader the control is not judged.
    assert controls.score(b2.load(tmp_path / "c"))["passed"] is None


def test_missing_answer_fails(tmp_path):
    b = ctl("explanation", {"answer": {"which": "second", "regex": "swap"}})
    b.user("Why?", kind="opening_prompt")
    b.out(CAUSE)
    assert controls.score(b.load(tmp_path))["passed"] is False


def test_unknown_key_is_not_judged_but_a_failure_still_fails(tmp_path):
    b = ctl("routing", {"telepathy": True})
    assert controls.score(b.load(tmp_path / "a"))["passed"] is None
    b2 = ctl("routing", {"telepathy": True, "requests": "none"})
    b2.out("?", label="unanswerable", reply="?")
    assert controls.score(b2.load(tmp_path / "b"))["passed"] is False


def test_no_expected_values(tmp_path):
    assert controls.score(ctl("routing", {}).load(tmp_path))["passed"] is None
