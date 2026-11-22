"""Release conditions, harness versions and unknown logged states."""
import json

import pytest

from cohbench.scoring import report, steering
from cohbench.scoring.__main__ import main
from cohbench.scoring.score import score_episode
from . import synth as S
from .test_review4 import full_config
from .test_review5 import log_episode
from .test_round2 import plan_file
from .test_round4 import full_scores


def release_inputs(path):
    scores = full_scores() + full_scores("ac", mode="coached")
    for score in scores:
        score["python_version"] = "3.12"
    root = S.write(path / "results", scores, {"a": full_config(), "ac": full_config()})
    return root, plan_file(path, scores), scores


@pytest.mark.parametrize("option, value", [
    ("bar", 0.5), ("bar", 1), ("judged_bar", 0.5), ("judged_bar", 1),
    ("level", 0.5), ("level", 0.99), ("z", 1), ("z", 10),
    ("resamples", 0), ("resamples", 20), ("resamples", 2001), ("seed", 1),
])
def test_diagnostic_options_cannot_publish_release_bands(tmp_path, option, value):
    root, plan, _ = release_inputs(tmp_path)
    rep = report.build(root, plan=plan, require_plan=True, **{option: value})
    assert rep["not_release"]
    assert option.replace("_", "-") in rep["parameters"]["nonrelease_note"]
    for table in rep["sections"][0]["tables"]:
        row = table["rows"][0]
        assert row["headline"]["band"] is None and row["headline"]["value"] == 100
        assert row["headline"]["ranked"]
        assert "not a release" in row["headline"]["band_note"]


@pytest.mark.parametrize("flags", [{}, {"scripted_reader": True}, {"allow_mixed_runtimes": True}])
def test_default_release_and_unused_allowance_flags_publish_identical_bands(tmp_path, flags):
    root, plan, _ = release_inputs(tmp_path)
    rep = report.build(root, plan=plan, require_plan=True, **flags)
    assert not rep["not_release"]
    assert all(t["rows"][0]["headline"]["band"] == {"band": 1, "ranks": "1", "members": 1} for t in rep["sections"][0]["tables"])


@pytest.mark.parametrize("condition", ["plan", "runtime", "mixed", "scripted", "reader_record"])
def test_release_input_conditions_withhold_all_bands(tmp_path, condition):
    root, plan, scores = release_inputs(tmp_path)
    kw = {"require_plan": condition != "plan", "plan": plan}
    if condition in ("runtime", "mixed"):
        for score in scores if condition == "runtime" else scores[:1]:
            score["python_version"] = "3.15"
        kw["allow_mixed_runtimes"] = True
    if condition == "scripted":
        for score in scores:
            if score["reader"]:
                score["reader"] = S.SCRIPTED_READER
        kw["scripted_reader"] = True
    if condition == "reader_record":
        next(s for s in scores if s["reader"])["reader_record"] = False
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    rep = report.build(root, **kw)
    assert rep["not_release"]
    assert all(t["rows"][0]["headline"]["band"] is None for t in rep["sections"][0]["tables"])


@pytest.mark.parametrize("option", ["bar", "judged-bar"])
def test_lowered_threshold_cli_remains_diagnostic(tmp_path, option):
    root, plan, scores = release_inputs(tmp_path)
    if option == "bar":
        next(s for s in scores if s["family"] == "routing" and s["control"])["control"]["passed"] = False
    else:
        scores.extend([S.steer("a-s1", 1, 1), S.steer("ac-s1", 1, 1, mode="coached")])
        for score in scores:
            score["python_version"] = "3.12"
        next(s for s in scores if s["family"] == "steering" and s["score"])["score"]["score"] = None
        plan = plan_file(tmp_path, scores)
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    out = tmp_path / "out"
    assert main(["report", str(root), "--out", str(out), "--plan", str(plan), "--require-plan", "--" + option, "0.5"]) == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["not_release"] and all(row["headline"]["band"] is None for t in rep["sections"][0]["tables"] for row in t["rows"])
    assert rep["sections"][0]["tables"][0]["rows"][0]["headline"]["ranked"]


def harness_inputs(path, third=False, settings=False, pending=False):
    configs = {"a": full_config("pi"), "b": {**full_config("pi"), "harness_version": "2"}}
    if third:
        configs["c"] = full_config("other")
    if settings:
        configs["b"]["settings"] = {"reasoning": "high"}
    scores = [score for prefix in configs if not (pending and prefix == "b")
              for score in S.config_set(prefix, r_pass=1, pairs=1, e=[1], s=[1 if prefix == "a" else 0.125], controls=(1, 1))]
    root = S.write(path / "results", scores, configs)
    if pending:
        index = json.loads((root / "index.json").read_text())
        index["episodes"]["b-pending"] = configs["b"]
        (root / "index.json").write_text(json.dumps(index))
    return report.build(root, plan=plan_file(path, scores), require_plan=pending, resamples=0)


def test_versions_of_one_harness_are_values_without_a_spread(tmp_path):
    rep = harness_inputs(tmp_path)
    section = rep["sections"][0]
    assert section["harness_effect"] == []
    note, = section["same_harness_different_versions"]
    assert note["harness"] == "pi"
    assert [v["headline"] for v in note["values"]] == pytest.approx([100, 50])
    for markdown in (False, True):
        text = report.render(rep, markdown)
        assert "Same harness, different versions" in text and "100.0" in text and "50.0" in text
        assert "Spread not measured" in text


@pytest.mark.parametrize("pending", [False, True])
def test_ambiguous_version_withholds_cross_harness_effect(tmp_path, pending):
    rep = harness_inputs(tmp_path, third=True, pending=pending)
    section = rep["sections"][0]
    assert section["harness_effect"] == []
    note = section["harness_effect_not_measured"][0]
    assert "cannot choose a version" in note["reason"] and note["ambiguous_harnesses"] == ["pi"]
    assert "cannot choose a version" in report.render(rep, False)


def test_version_ambiguity_does_not_cross_settings_groups(tmp_path):
    rep = harness_inputs(tmp_path, third=True, settings=True)
    section = rep["sections"][0]
    effect, = section["harness_effect"]
    assert len(effect["harnesses"]) == 2 and effect["headline"]["value"] == pytest.approx(50)
    assert section["harness_effect_not_measured"] == []


@pytest.mark.parametrize("states, expected", [([None] * 3, []), (["a" * 64, "a" * 64, None], []),
    ([None, "a" * 64, "a" * 64], []), ([None, "a" * 64, "a" * 64, "a" * 64], [4])])
def test_unknown_log_states_count_in_no_loop(tmp_path, states, expected):
    records = [(1000 + i * 100, 1010 + i * 100, "measure", state, "same") for i, state in enumerate(states)]
    ep = log_episode(tmp_path, records=records, shell=False)
    result = score_episode(ep)
    part = result["score"]["l"]
    assert [a["seq"] for a in part["loop_attempts"]] == expected
    assert part["unknown_log_states"] == states.count(None)
    assert part["unplaced_attempts"] == 0
    assert result["attempt_observations"]["log_state_unknown"] == states.count(None)
    for seq, state in enumerate(states, 1):
        if state is None:
            assert steering.attempt_right(result["score"], seq) is None


@pytest.mark.parametrize("condition", ["controls", "coverage", "sanity", "poll"])
def test_release_row_conditions_withhold_bands(tmp_path, condition):
    root, plan, scores = release_inputs(tmp_path)
    for score in scores:
        if condition == "controls" and score["control"] and score["family"] == "routing":
            score["control"]["passed"] = False
        if condition == "coverage" and score["score"] and score["family"] == "steering":
            score["score"]["score"] = None
        if condition == "sanity" and score["score"] and score["family"] == "explanation":
            score["score"]["sanity"]["no_answer"]["correct"] = 6
        if condition == "poll":
            score["poll_s"] = None
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    rep = report.build(root, plan=plan, require_plan=True)
    assert not rep["not_release"]
    for table in rep["sections"][0]["tables"]:
        assert not table["rows"][0]["headline"]["ranked"] and table["rows"][0]["headline"]["band"] is None
