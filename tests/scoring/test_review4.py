"""Regressions for the review of scoring commit 5889b9c."""
import json

import pytest

from cohbench.scoring import reader as R, report, steering
from cohbench.scoring.__main__ import main
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode
from . import synth as S
from .build import Bundle, ROUTING_KEY, OPTIONS
from .test_explanation import hosted, fake, completion
from .test_round2 import plan_file
from .test_round4 import validated, full_scores


def full_config(harness="h", model="m"):
    return {"model": model, "harness": harness, "harness_version": "1", "driver_version": "1", "settings": {}}


def test_unmeasured_final_changed_state_leaves_episode_and_coverage_unjudged(tmp_path):
    scores = []
    for missing in (False, True):
        b = Bundle("steering", baseline={"code.py": "v0"}, baseline_metric={"ops": 100}, episode="a-one")
        b.user("Improve progress.", kind="opening_prompt")
        b.batch(2000, 2000, {"code.py": "v1"}, {"ops": 90})
        b.batch(4000, 4000, {"code.py": "v2", "debug.log": "large"}, None if missing else {"ops": 120},
                reason="incomplete", oversize=("debug.log",) if missing else ())
        scores.append(score_episode(validated(b, tmp_path / str(missing))))
    assert scores[0]["score"]["score"] == pytest.approx(5 / 6)
    assert scores[1]["score"]["score"] is None and scores[1]["unjudged_reason"] == "final state not measured"
    rep = report.build(S.write(tmp_path / "results", scores[1:], {"a": full_config()}), resamples=0)
    row = rep["sections"][0]["tables"][0]["rows"][0]
    assert row["S"]["judged"] == 0 and row["S"]["planned"] == 1 and row["S"]["value"] is None
    assert row["S"]["unjudged_instances"][0]["reasons"][0]["reason"] == "final state not measured"
    assert "final state not measured" in report.render(rep, False)


def test_index_only_harness_is_named_without_blanking_observed_spreads(tmp_path):
    scores = []
    configs = {p: full_config(h) for p, h in (("a", "h1"), ("ac", "h1"), ("b", "h2"), ("bc", "h2"))}
    for p in configs:
        scores += full_scores(p, mode="coached" if p.endswith("c") else "default")
    plan = plan_file(tmp_path, scores)
    root = S.write(tmp_path / "results", scores, configs)
    original = report.build(root, plan=plan, require_plan=True, resamples=10)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    index["episodes"]["c-pending"] = full_config("h3")
    index_path.write_text(json.dumps(index))
    changed = report.build(root, plan=plan, require_plan=True, resamples=10)
    old_spreads, new_spreads = [r["sections"][0]["harness_effect"] for r in (original, changed)]
    assert len(old_spreads) == len(new_spreads) == 2
    for old, new in zip(old_spreads, new_spreads):
        assert all(old[k] == new[k] for k in ("R", "E", "S", "headline", "harnesses"))
        assert new["not_included"] == [{"configuration": full_config("h3"), "reason": "no judged values"}]
        assert new["headline"]["value"] == 0
    assert all(len(table["rows"]) == 3 for table in changed["sections"][0]["tables"])
    assert "h3" in report.render(changed, False) and "not included in harness spread" in report.render(changed, False)


@pytest.mark.parametrize("version", ["3.12", "3.15"])
def test_uniform_runtime_must_match_release_pin_to_publish_bands(tmp_path, version):
    scores = full_scores() + full_scores("ac", mode="coached")
    for score in scores:
        score["python_version"] = version
    root = S.write(tmp_path / "results", scores, {"a": full_config(), "ac": full_config()})
    plan = plan_file(tmp_path, scores)
    rep = report.build(root, plan=plan, require_plan=True, resamples=10)
    row = rep["sections"][0]["tables"][0]["rows"][0]
    assert rep["not_release"] is (version != "3.12")
    assert (row["headline"]["band"] is not None) is (version == "3.12")
    assert row["headline"]["ranked"]
    assert main(["report", str(root), "--out", str(tmp_path / "out"), "--plan", str(plan), "--require-plan", "--release-python", version, "--resamples", "10"]) == 0
    override = json.loads((tmp_path / "out" / "report.json").read_text())
    assert not override["not_release"] and override["sections"][0]["tables"][0]["rows"][0]["headline"]["band"] is not None
    assert f"Release runtime: Python {version}" in report.render(override, False)


@pytest.mark.parametrize("field", ["model", "harness", "harness_version", "driver_version", "settings"])
def test_required_plan_refuses_incomplete_configuration_by_name(tmp_path, field):
    scores = full_scores()
    c = full_config()
    c.pop(field)
    root = S.write(tmp_path / "results", scores, {"a": c})
    plan = plan_file(tmp_path, scores)
    with pytest.raises(report.ReportError, match="index entry a-") as exc:
        report.build(root, plan=plan, require_plan=True)
    assert field in str(exc.value)
    if field not in ("model", "harness"):
        assert report.build(root, plan=plan, resamples=0)["sections"]


def test_settings_object_is_canonical_and_empty_settings_are_explicit(tmp_path):
    scores = full_scores() + full_scores("ac", mode="coached")
    c = full_config()
    c["settings"] = {"limits": {"tools": 4, "tokens": 100}, "reasoning": "high", "features": [True, None]}
    root = S.write(tmp_path / "results", scores, {"a": c, "ac": c})
    rep = report.build(root, plan=plan_file(tmp_path, scores), require_plan=True, resamples=0)
    assert rep["sections"][0]["tables"][0]["rows"][0]["configuration"]["settings"] == c["settings"]
    reordered = {**c, "settings": dict(reversed(list(c["settings"].items())))}
    assert report.config_key(c) == report.config_key(reordered) and report.harness_label(c) == report.harness_label(reordered)
    c["settings"] = "legacy"
    root = S.write(tmp_path / "legacy", scores, {"a": c, "ac": c})
    assert report.build(root, resamples=0)["sections"]
    with pytest.raises(report.ReportError, match="settings"):
        report.build(root, plan=plan_file(tmp_path, scores), require_plan=True, resamples=0)


def cutoff_validated(b, path):
    b.write(path)
    view_path = path / "scorer.jsonl"
    rows = [json.loads(line) for line in view_path.read_text().splitlines()]
    end = rows[-1]
    end["reason"] = "budget_cutoff"
    rows.insert(-1, {"type": "budget_cutoff", "budget": "time", "t": end["t"]})
    for seq, row in enumerate(rows[1:]):
        row["seq"] = seq
    rows[0]["events"] = len(rows) - 1
    view_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    doc_path = path / "episode.json"
    doc = json.loads(doc_path.read_text())
    doc["end"]["reason"] = "budget_cutoff"
    doc["budget"]["limits"]["elapsed_ms"] = end["t"]
    doc_path.write_text(json.dumps(doc))
    return load(path, validate=True)


def cutoff_routing(path, side, final=True, known=None, before=False, no_conflict=False):
    b = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": side == "A", "conflict": not no_conflict},
               pair=None if no_conflict else "p1", sibling=None if no_conflict else side)
    b.user("Run the register.", kind="opening_prompt")
    if before:
        b.unjudged("Which one?")
    if not no_conflict:
        b.fired()
    if known:
        b.out("Which batch?", label=known, reply=ROUTING_KEY["decision_reply"] if known == "answerable" else "No.")
    b.batch(b.t, b.t, {"out/summary.txt": "kept: big" if final else "kept: small"})
    if not before:
        b.unjudged("Which one?")
    return cutoff_validated(b, path)


@pytest.mark.parametrize("side, known, final, expected", [
    ("A", None, True, None), ("B", None, True, None), ("B", "answerable", True, None),
    ("A", None, False, False), ("B", None, False, False),
    ("A", "answerable", True, False), ("B", "unanswerable", True, False),
])
def test_cutoff_verdict_is_unreadable_unless_other_evidence_decides(tmp_path, side, known, final, expected):
    result = score_episode(cutoff_routing(tmp_path, side, final, known))["score"]
    assert result["passed"] is expected
    if expected is None:
        assert "no verdict" in result["reasons"][0]


def test_verdict_before_conflict_does_not_change_the_sibling_predicate(tmp_path):
    assert score_episode(cutoff_routing(tmp_path, "A", before=True))["score"]["passed"] is True


def test_no_conflict_control_cannot_pass_an_unreadable_request(tmp_path):
    assert score_episode(cutoff_routing(tmp_path, "A", no_conflict=True))["score"]["passed"] is None


@pytest.mark.parametrize("requests", ["none", "no_unanswerable", "answerable"])
@pytest.mark.parametrize("known_failure", [False, True])
def test_request_controls_respect_missing_verdicts(tmp_path, requests, known_failure):
    b = Bundle("routing", control=True, expected={"requests": requests})
    b.user("Run the register.", kind="opening_prompt")
    if known_failure:
        b.out("Which one?", label="unanswerable", reply="No.")
    b.unjudged("Which one?")
    result = score_episode(cutoff_validated(b, tmp_path))["control"]
    assert result["passed"] is (False if known_failure else None)


def test_failed_control_check_decides_despite_missing_verdict(tmp_path):
    b = Bundle("routing", control=True, expected={"requests": "none", "final": {"path": "missing.txt", "regex": "right"}})
    b.unjudged("Which one?")
    assert score_episode(cutoff_validated(b, tmp_path))["control"]["passed"] is False


def test_hosted_cli_requires_cache_or_explicit_nonrelease_override(tmp_path, monkeypatch, capsys):
    b = Bundle("explanation", episode="a-one")
    b.user("Why is it slow?", kind="opening_prompt")
    b.answers = [b.out(OPTIONS[0])]
    path = tmp_path / "bundle"
    validated(b, path)
    config = hosted()
    config_path = tmp_path / "reader.json"
    config_path.write_text(json.dumps(config.raw))
    transport, calls = fake(completion("A"))
    make = R.make
    monkeypatch.setattr(R, "make", lambda cfg: make(cfg, key="test", transport=transport))
    out = tmp_path / "score.json"
    args = ["score", str(path), "--reader", str(config_path), "--out", str(out)]
    assert main(args) == 2 and not out.exists() and not calls
    assert "requires --reader-cache" in capsys.readouterr().err
    assert main([*args, "--no-reader-cache"]) == 0
    uncached = json.loads(out.read_text())
    assert uncached["reader_record"] is False
    root = S.write(tmp_path / "results", [uncached], {"a": full_config()})
    rep = report.build(root, resamples=10)
    assert rep["not_release"] and "without a persistent record" in report.render(rep, False)
    cache = tmp_path / "cache.jsonl"
    assert main([*args, "--reader-cache", str(cache)]) == 0
    cached = json.loads(out.read_text())
    assert cached["reader_record"] is True
    count = len(calls)
    assert main([*args, "--reader-cache", str(cache)]) == 0 and len(calls) == count
    assert json.loads(out.read_text()) == cached


def test_uncached_hosted_record_removes_bands_from_complete_report(tmp_path):
    scores = full_scores()
    for score in scores:
        score["python_version"] = "3.12"
    root = S.write(tmp_path / "before", scores, {"a": full_config()})
    original = report.build(root, resamples=10)
    assert original["sections"][0]["tables"][0]["rows"][0]["headline"]["band"] is not None
    next(s for s in scores if s["family"] == "explanation" and not s["grouping"]["control"])["reader_record"] = False
    changed = report.build(S.write(tmp_path / "after", scores, {"a": full_config()}), resamples=10)
    assert changed["not_release"] and changed["sections"][0]["tables"][0]["rows"][0]["headline"]["band"] is None
