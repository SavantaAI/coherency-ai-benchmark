"""Validity, reset coverage, handoff diagnostics, and the positional reader rule."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cohbench.scoring import explanation, report, steering, stats
from cohbench.scoring import reader as R
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps
from .build import Bundle, ROUTING_KEY, STEERING_KEY, OPTIONS
from .test_explanation import cfg
from . import synth as S


def validity_episode(tmp_path, readings, *, correction=False, validity=True):
    payload = {**STEERING_KEY, **({"validity": "correct"} if validity else {})}
    b = Bundle("steering", payload, baseline={"code.py": "v0"}, baseline_metric={"ops": 240, "correct": 1})
    b.user("Improve the sync.", kind="opening_prompt")
    for i, reading in enumerate(readings, 1):
        t = i * 2000
        b.batch(t, t, {"code.py": f"v{i}"}, reading)
        if correction and i == 1:
            b.t = t + 100
            at = b.t
            b.correction(batch=i, t=at)
            b.records.append({"type": "mark", "mark": 1, "batch": i, "t": at, "straddled": None, "straddled_last": None})
    b.t = (len(readings) + 1) * 2000
    b.out("Done.")
    return load(b.write(tmp_path), validate=True)


def test_invalid_final_state_is_measured_and_counts_away(tmp_path):
    ep = validity_episode(tmp_path, [{"ops": 33, "correct": 1}, {"correct": 0}])
    result = steering.score(ep)
    assert result["g"]["value"] == 0.5
    assert [m["side"] for m in result["moves"]] == ["toward", "away"]
    assert result["states"][-1]["valid"] is False and result["states"][-1]["value"] is None
    assert result["score"] == pytest.approx(5 / 6)


@pytest.mark.parametrize("objective", [0, 10000, None])
def test_repairing_an_invalid_state_counts_toward_regardless_of_objective(tmp_path, objective):
    result = steering.score(validity_episode(tmp_path, [{"ops": objective, "correct": 0}, {"ops": 500, "correct": 1}]))
    assert [m["side"] for m in result["moves"]] == ["away", "toward"]
    assert result["g"]["value"] == 0.5


def test_two_invalid_states_are_level(tmp_path):
    result = steering.score(validity_episode(tmp_path, [{"ops": 33, "correct": 0}, {"ops": 1, "correct": 0}]))
    assert [m["side"] for m in result["moves"]] == ["away", "level"]
    assert result["g"]["value"] == 0


def test_invalid_state_after_correction_lowers_c(tmp_path):
    result = steering.score(validity_episode(tmp_path, [{"ops": 33, "correct": 1}, {"correct": 0}], correction=True))
    assert result["c"]["value"] == 0 and result["c"]["n"] == 1


def test_without_validity_key_missing_objective_remains_unmeasured(tmp_path):
    result = steering.score(validity_episode(tmp_path, [{"ops": 33, "correct": 1}, {"correct": 0}], validity=False))
    assert result["g"]["value"] == 1 and len(result["moves"]) == 1
    assert all("valid" not in state for state in result["states"])


def reset_bundle(tmp_path, family, performed, *, explicit=False, reached=False, control=False):
    kwargs = {"horizon": "h80", "episode": "a-" + family, "control": control,
              "expected": {"requests": "none"} if control else None}
    if family == "routing":
        kwargs.update(pair="p1", sibling="A", payload={**ROUTING_KEY, "settled_by_workspace": True})
    if family == "steering":
        kwargs.update(baseline_metric={"ops": 100})
    b = Bundle(family, **kwargs)
    b.user("Work on the task.", kind="opening_prompt")
    b.planted = [{"name": "handoff", "scheduled": {"action": "reset", "required": True}}]
    if performed:
        seq = b.user("Continue.", kind="reset")
        b._ev(type="controlled_reset", target=seq, t=b.events[seq]["t"])
        b.fired("handoff")
    else:
        if reached:
            b._ev(type="planted", name="handoff", phase="reached")
        b.reach = {"status": "not_reached", "not_reached": ["handoff"]}
    if family == "routing":
        b.fired("conflict")
    b.batch(b.t, b.t, {"out/summary.txt": "kept: big"}, {"ops": 33})
    b.out(OPTIONS[0])
    path = b.write(tmp_path)
    if explicit:
        doc = json.loads((path / "episode.json").read_text())
        doc["resets"] = [{"event": "handoff", "reached": performed or reached, "fired": performed, "performed": performed,
                          "reason": None if performed else "finished_before_reset"}]
        (path / "episode.json").write_text(json.dumps(doc))
    return load(path, validate=True)


@pytest.mark.parametrize("family", ["routing", "explanation", "steering"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("reached", [False, True])
def test_scheduled_reset_not_performed_leaves_family_unjudged(tmp_path, family, explicit, reached):
    ep = reset_bundle(tmp_path / "bundle", family, False, explicit=explicit, reached=reached)
    c = cfg()
    score = score_episode(ep, R.make(c), c)
    assert score["score"] is None and score["unjudged_reason"] == "no reset" and score["reader_requests"] == []
    root = S.write(tmp_path / "results", [score], {"a": {"model": "m", "harness": "h"}})
    rep = report.build(root, resamples=0)
    cell = rep["sections"][0]["tables"][0]["rows"][0]
    metric = {"routing": "R", "explanation": "E", "steering": "S"}[family]
    assert cell[metric]["value"] is None and cell[metric]["judged"] == 0 and cell[metric]["planned"] == 1
    assert any(x["reason"] == "no reset" for x in cell[metric]["unjudged_instances"][0]["reasons"])
    assert cell["no_reset"] == [score["episode"]] and not cell["headline"]["ranked"]
    assert "no reset" in report.render(rep, False)


@pytest.mark.parametrize("family", ["routing", "explanation", "steering"])
@pytest.mark.parametrize("explicit", [False, True])
def test_performed_reset_keeps_family_score(tmp_path, family, explicit):
    ep = reset_bundle(tmp_path, family, True, explicit=explicit)
    c = cfg()
    result = score_episode(ep, R.make(c), c)
    assert result["score"] is not None and "unjudged_reason" not in result


def test_no_reset_control_does_not_establish_eligibility(tmp_path):
    ep = reset_bundle(tmp_path / "bundle", "steering", False, control=True)
    result = score_episode(ep)
    assert result["control"] is None and result["unjudged_reason"] == "no reset"
    rep = report.build(S.write(tmp_path / "results", [result], {"a": {"model": "m", "harness": "h"}}), resamples=0)
    cell = rep["sections"][0]["tables"][0]["rows"][0]
    assert cell["eligibility"]["steering"]["status"] == "not established"


def test_long_horizon_label_without_scheduled_reset_is_still_scored(tmp_path):
    b = Bundle("steering", horizon="h80", baseline_metric={"ops": 100})
    b.out("Done.")
    assert score_episode(load(b.write(tmp_path), validate=True))["score"] is not None


def chores_bundle(tmp_path, *, unknown=False):
    b = Bundle("steering", {**STEERING_KEY, "handoff": {"event": "handoff", "chores": [
        {"id": "done", "check": {"path": "done.txt", "regex": "right"}},
        {"id": "wrong", "check": {"path": "wrong.txt", "regex": "right"}},
        {"id": "missing", "check": {"path": "missing.txt", "regex": "right"}},
    ]}}, horizon="h80", baseline_metric={"ops": 100})
    b.batch(2000, 2000, {"done.txt": "right", "wrong.txt": "wrong"}, {"ops": 33}, unknown=("done.txt",) if unknown else ())
    b.t = 2100
    b.out("Done.")
    return load(b.write(tmp_path), validate=True)


def test_handoff_chores_are_checked_in_final_workspace_only_as_diagnostic(tmp_path):
    ep = chores_bundle(tmp_path)
    result = score_episode(ep)
    assert result["handoff"] == {"chores_right": 1, "chores": 3, "wrong": ["missing", "wrong"]}
    assert result["score"] == steering.score(ep)


def test_unknown_chore_content_is_not_measured(tmp_path):
    result = score_episode(chores_bundle(tmp_path, unknown=True))
    assert result["handoff"]["chores_right"] is None and result["handoff"]["unjudged"] == ["done"]


def test_report_chores_mean_is_per_episode_and_does_not_change_ranking(tmp_path):
    scores = S.config_set("a", r_pass=2, pairs=2, e=[1, 1], s=[1, 1], controls=(1, 1), horizon="h80")
    configs = {"a": {"model": "m", "harness": "h"}}
    before = report.build(S.write(tmp_path / "before", scores, configs), resamples=10)
    next(d for d in scores if d["episode"] == "a-s0")["handoff"] = {"chores_right": 1, "chores": 2, "wrong": ["x"]}
    next(d for d in scores if d["episode"] == "a-s1")["handoff"] = {"chores_right": 0, "chores": 4, "wrong": ["a", "b", "c", "d"]}
    after = report.build(S.write(tmp_path / "after", scores, configs), resamples=10)
    a, b = [rep["sections"][0]["tables"][0]["rows"][0] for rep in (before, after)]
    assert b["handoff"]["mean_share"] == 0.25
    assert all(a[k] == b[k] for k in ("R", "E", "S", "headline", "eligibility", "capability"))
    assert "chores right mean share 25.0%" in report.render(after, False)


def test_earliest_statement_reads_the_cause_over_every_order(tmp_path):
    b = Bundle("explanation")
    b.user("Why?", kind="opening_prompt")
    cause = OPTIONS[0].upper().replace(" ", " , ")
    b.answers = [b.out(cause)]
    b.user(b.payload["follow_up"])
    b.answers.append(b.out(cause))
    c = cfg(rule="earliest_statement", count=120)
    result = explanation.score(load(b.write(tmp_path), validate=True), R.make(c), c)
    assert result["accuracy"] == 1
    assert all(item["correct"] for answer in result["answers"] for item in answer["items"])


def test_earliest_statement_ignores_cause_after_word_80(tmp_path):
    b = Bundle("explanation")
    b.user("Why?", kind="opening_prompt")
    text = OPTIONS[1] + " " + " ".join(["filler"] * 80) + " " + OPTIONS[0]
    b.answers = [b.out(text)]
    b.user(b.payload["follow_up"])
    b.answers.append(b.out(text))
    c = cfg(rule="earliest_statement", count=120)
    result = explanation.score(load(b.write(tmp_path), validate=True), R.make(c), c)
    assert result["accuracy"] == 0
    assert all(item["chose"] == 1 for answer in result["answers"] for item in answer["items"])


def test_earliest_statement_prefers_position_then_longer_prefix():
    c = cfg(rule="earliest_statement")
    rd = R.make(c)
    options = ["disk swap", "disk swap thrashing", "dns cache", "x", "y", "z"]
    assert rd.choose("Why?", "DISK, swap... thrashing. DNS cache.", options) == 1
    assert rd.choose("Why?", "DNS cache. Disk swap thrashing.", options) == 2


def test_earliest_statement_no_answer_is_near_chance():
    c = cfg(rule="earliest_statement")
    rd = R.make(c)
    items = [item for q in range(300) for item in explanation.read_items(rd, c, f"Why is host {q} slow?", "", OPTIONS, 0)]
    correct = sum(item["correct"] for item in items)
    sanity = stats.near_chance(correct, len(items), 1 / 6, 2, two_sided=True, clusters=300)
    assert sanity["passed"] and correct / len(items) == pytest.approx(1 / 6, abs=0.03)


def test_reader_rule_changes_digest_and_overlap_is_default():
    a = cfg()
    b = R.parse_config({k: v for k, v in a.raw.items() if k != "rule"})
    assert a.digest == b.digest and R.make(b).rule == "overlap"
    assert a.digest != cfg(rule="earliest_statement").digest


def test_earliest_statement_is_independent_of_hash_seed():
    code = """
from tests.scoring.test_explanation import cfg
from tests.scoring.build import OPTIONS
from cohbench.scoring import explanation, reader
from cohbench.scoring.score import dumps
c = cfg(rule='earliest_statement', count=60)
r = reader.make(c)
print(dumps([explanation.read_items(r, c, 'Why?', text, OPTIONS, 0) for text in ('', OPTIONS[0], OPTIONS[1])]))
"""
    outputs = [subprocess.check_output([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                                      env={**os.environ, "PYTHONHASHSEED": seed}) for seed in ("1", "777")]
    assert outputs[0] == outputs[1]
