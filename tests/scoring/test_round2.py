"""Regression cases from the second scoring review."""
import json
import sys

import pytest

from cohbench.scoring import report, routing, reader as R
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps
from cohbench.scoring.__main__ import main
from . import synth as S
from .test_report import CONFIGS, rows
from .test_explanation import cfg, hosted, fake, completion, episode, CAUSE
from .build import Bundle, ROUTING_KEY
from .test_routing import right_summary


def build(tmp_path, scores, configs=None, **kw):
    return S.calibrated_report(S.write(tmp_path, scores, configs or CONFIGS), resamples=30, **kw)


@pytest.mark.parametrize("pairs", [2, 20])
def test_judged_pairs_control_official_values_and_common_set(tmp_path, pairs):
    scores = S.config_set("a", r_pass=pairs - 1, pairs=pairs, e=[1], s=[1], controls=(1, 1))
    scores += S.config_set("ac", r_pass=pairs - 1, pairs=pairs, e=[1], s=[1], controls=(1, 1), mode="coached")
    d = next(d for d in scores if d["episode"] == f"ac-rb{pairs - 1}")
    d["score"].update(passed=None)
    rep = build(tmp_path, scores)
    c = rows(rep, mode="coached")[0][("m1", "pi")]
    gap = rep["sections"][0]["coached_gap"][0]["R"]
    assert c["coverage"]["ran"] == c["coverage"]["planned"]
    assert (c["R"]["judged"], c["R"]["planned"]) == (pairs - 1, pairs)
    assert c["R"]["official"] is (pairs == 20)
    assert c["headline"]["ranked"] is (pairs == 20)
    assert c["R"]["unjudged_instances"][0]["reasons"][0]["reason"] == "unjudged reply"
    assert gap["n"] == pairs - 1 and gap["left_out_counts"] == [1, 0]
    assert json.loads(gap["left_out"][0][0])["pair"] == f"p{pairs - 1}"
    text = report.render(rep, False)
    if pairs == 2:
        assert gap["value"] is None and gap["not_comparable"] == "judged on both sides 1 of 2"
        assert "judged 1 of 2 partial" in text
    else:
        assert gap["value"] == 0 and gap["points"] == [1, 1] and gap["show_common_values"]
        assert "common-set values 1.000, 1.000" in text and '"pair":"p19"' in text


def test_comparisons_pair_instances_within_a_seed(tmp_path):
    scores = []
    for prefix, mode in (("a", "default"), ("ac", "coached")):
        for pair in ("p", "q"):
            for side in ("A", "B"):
                d = S.sibling(f"{prefix}-{pair}-{side}", 0, side, pair == "p", mode=mode)
                d["grouping"]["pair"] = d["task"]["instance"]["pair"] = pair
                d["task"]["sha256"] = pair + side
                if prefix == "ac" and pair == "q":
                    d["score"].update(passed=None)
                scores.append(d)
    rep = build(tmp_path, scores, judged_bar=0.5)
    gap = rep["sections"][0]["coached_gap"][0]["R"]
    assert gap["n"] == 1 and gap["value"] == 0 and gap["points"] == [1, 1]


def test_undefined_steering_score_is_partial(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1], s=[1, None], controls=(1, 1))
    c = rows(build(tmp_path, scores))[0][("m1", "pi")]
    assert c["S"]["partial"] and not c["headline"]["ranked"]
    assert c["S"]["unjudged_instances"][0]["reasons"][0]["reason"] == "undefined score"


@pytest.mark.parametrize("cause", ["no reader", "missing", "rerun required"])
def test_all_planned_controls_must_be_judged(tmp_path, cause):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1], s=[1], controls=(2, 2))
    scores += S.config_set("b", r_pass=1, pairs=1, e=[1], s=[1], controls=(2, 2))
    d = next(d for d in scores if d["episode"] == "a-ce1")
    if cause == "missing":
        scores.remove(d)
    elif cause == "rerun required":
        d.update(rerun_required=True, control=None, reported=None)
    else:
        d["control"].update(passed=None, results={"reader_true_cause": {"held": None, "why": "no reader was given"}})
    rep = build(tmp_path, scores)
    c = rows(rep)[0][("m1", "pi")]
    assert c["eligibility"]["explanation"]["status"] == "not established"
    assert not c["headline"]["ranked"] and c["capability"] is None and c["capability_status"] == "incomplete"
    missing = c["pooled_controls"]["explanation"]["unjudged_instances"]
    assert len(missing) == 1 and missing[0]["reasons"][0]["reason"] == cause
    assert "incomplete" in report.render(rep, False)


def test_sanity_verdict_is_shared_across_polling_sections(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[0.5] * 3, s=[1], controls=(1, 1))
    scores += S.config_set("b", r_pass=1, pairs=1, e=[0.5], s=[1], controls=(1, 1), poll_s=2.1)
    for d in scores:
        if d["reader"]:
            n = 4 if d["episode"].startswith("a-") else 2
            for part in ("no_answer", "buried"):
                d["score"]["sanity"][part]["key"] = d["episode"] + part
            d["score"]["sanity"]["no_answer"]["correct"] = n
    rep = build(tmp_path, scores)
    for sec in rep["sections"]:
        sanity = sec["reader_sanity"][S.READER["config_sha256"]]
        assert sanity["no_answer"]["correct"] == 14 and sanity["no_answer"]["n"] == 24
        assert not sanity["passed"]
        for table in sec["tables"]:
            for row in table["rows"]:
                assert row["E"]["value"] is None and row["headline"]["band"] is None


def test_same_harness_settings_are_not_a_harness_effect(tmp_path):
    configs = {"a": {"model": "m", "harness": "pi", "harness_version": "1", "reasoning": "low"},
               "b": {"model": "m", "harness": "pi", "harness_version": "1", "reasoning": "high"},
               "c": {"model": "m", "harness": "other", "harness_version": "2", "reasoning": "low"}}
    scores = [d for p in configs for d in S.config_set(p, r_pass=1, pairs=1, e=[1], s=[1], controls=(1, 1))]
    rep = build(tmp_path, scores[:2 * len(scores) // 3], configs)
    assert rep["sections"][0]["harness_effect"] == []
    assert len(rep["sections"][0]["same_harness_different_settings"]) == 1
    assert "no qualifying set" in report.render(rep, False)
    rep = build(tmp_path / "all", scores, configs)
    effect = rep["sections"][0]["harness_effect"]
    assert len(effect) == 1 and len(effect[0]["harnesses"]) == 2
    assert all("reasoning=low" in x for x in effect[0]["harnesses"])


def test_column_keys_keep_json_types_distinct(tmp_path):
    configs = {"a": {"model": "m", "harness": "pi", "threads": 1},
               "b": {"model": "m", "harness": "pi", "threads": "1"}}
    configs = {q: {**c, "harness_version": "1", "driver_version": "1", "settings": {}} for p, c in configs.items() for q in (p, p + "c")}
    scores = [d for p in configs for d in S.config_set(p, r_pass=1, pairs=1, e=[1], s=[1], controls=(1, 1), mode="coached" if p.endswith("c") else "default")]
    rep = S.calibrated_report(S.write(tmp_path, scores, configs), plan=plan_file(tmp_path, scores), require_plan=True)
    rs = rep["sections"][0]["tables"][0]["rows"]
    assert len({r["column_key"] for r in rs}) == len({r["harness"] for r in rs}) == 2
    assert all((r["headline"]["band"]["ranks"] == "1" if sys.version_info[:2] == (3, 12) else r["headline"]["band"] is None) for r in rs)
    assert len(rep["sections"][0]["eligibility"]) == 2
    assert all(r["harness"] in report.render(rep, False) for r in rs)


@pytest.mark.parametrize("cards", [["Take larger batch", "Take larger batch"], ["Take larger batch", "Take larger", "batch"]])
def test_ambiguous_routing_replies_are_unjudged(tmp_path, cards):
    b = Bundle("routing", {**ROUTING_KEY, "decision_reply": "Take larger batch", "card_texts": cards}, pair="p1", sibling="B")
    b.fired()
    seq = b.out("Which batch?", label="answerable", reply="Take larger batch")
    right_summary(b)
    b.t = 5100
    b.out("Done.")
    ep = load(b.write(tmp_path), validate=True)
    result = routing.score(ep)
    assert result["passed"] is None and "ambiguous" in result["reasons"][0]
    assert routing.output_right(ep, seq) is None


def plan_file(tmp_path, scores, extra=None):
    entries = {d["task"]["sha256"]: {"dir": d["episode"], **d["task"]["instance"], "digest": d["task"]["sha256"]} for d in scores}
    if extra:
        entries[extra["digest"]] = extra
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"format": "cohbench-task-suite", "version": 1, "instances": list(entries.values())}))
    return path


def test_explicit_plan_counts_instances_everyone_omitted(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1], s=[1], controls=(1, 1))
    extra = {"dir": "absent", **S.steer("a-absent", 99, 1)["task"]["instance"], "digest": "absent"}
    plan = plan_file(tmp_path, scores, extra)
    root = S.write(tmp_path / "scores", scores, CONFIGS)
    rep = S.calibrated_report(root, plan=plan, resamples=10)
    c = rows(rep)[0][("m1", "pi")]
    assert c["coverage"]["planned"] == len(scores) + 1 and c["S"]["partial"]
    assert c["S"]["unjudged_instances"][0]["reasons"][0]["reason"] == "missing"
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    for config in index["episodes"].values():
        config.update(harness_version="1", driver_version="1", settings={})
    index_path.write_text(json.dumps(index))
    assert main(["report", str(root), "--plan", str(plan), "--require-plan", "--judged-bar", "0.5", "--resamples", "0", "--out", str(tmp_path / "out")]) == 0
    assert main(["report", str(root), "--require-plan", "--out", str(tmp_path / "out")]) == 2
    plan = plan_file(tmp_path, scores[1:])
    with pytest.raises(report.ReportError, match="a-ra0.*absent from the plan"):
        S.calibrated_report(root, plan=plan)


def test_counts_distinguish_reruns_and_inferred_plan(tmp_path):
    scores = [S.steer("a-1", 1, 1), S.steer("a-2", 2, 1, rerun_required=True)]
    rep = build(tmp_path, scores)
    assert rep["inputs"]["score_files_read"] == 2 and rep["inputs"]["usable_scores"] == rep["inputs"]["rerun_markers"] == 1
    text = report.render(rep, False)
    assert "Score files read: 2; usable scores: 1; rerun markers: 1" in text
    assert "Completeness was inferred from the runs given." in text


def test_result_filenames_do_not_order_episode_lists(tmp_path):
    scores = [S.steer("a-z", 1, 1, planted=("correction",)), S.steer("a-a", 2, 1, planted=("correction",))]
    root = S.write(tmp_path, scores, CONFIGS)
    before = dumps(S.calibrated_report(root, resamples=10))
    (root / "scores" / "a-z.score.json").rename(root / "scores" / "0.score.json")
    assert dumps(S.calibrated_report(root, resamples=10)) == before
    c = rows(S.calibrated_report(root, resamples=10))[0][("m1", "pi")]
    assert c["event_reach"]["events"]["correction"]["not_fired"] == ["a-a", "a-z"]


def test_reader_config_rejects_fields_unused_by_its_kind():
    with pytest.raises(R.ReaderError, match="sampling"):
        cfg(sampling={"temperature": 0, "seed": 1, "max_tokens": 16})
    with pytest.raises(R.ReaderError, match="rule"):
        hosted(rule="position")


def test_reader_cache_replays_and_score_records_choices(tmp_path):
    ep = episode(tmp_path, CAUSE, CAUSE)
    c = hosted()
    first_transport, first_calls = fake(completion("A"))
    second_transport, second_calls = fake(completion("B"))
    cache = tmp_path / "choices.jsonl"
    a = score_episode(ep, R.RecordedReader(R.make(c, key="test", transport=first_transport), cache), c)
    b = score_episode(ep, R.RecordedReader(R.make(c, key="test", transport=second_transport), cache), c)
    assert first_calls and not second_calls and dumps(a) == dumps(b)
    assert a["reader_requests"] and all(x["choice"] == "A" for x in a["reader_requests"])
    cached = [json.loads(line) for line in cache.read_text().splitlines()]
    assert sorted([{k: row[k] for k in ("request_digest", "reader_digest", "choice")} for row in cached],
                  key=lambda x: x["request_digest"]) == a["reader_requests"]


def test_report_refuses_reader_disagreements(tmp_path):
    scores = [S.expl("a-1", 1, 1), S.expl("b-2", 2, 1)]
    for d, choice in zip(scores, ("A", "B")):
        d["reader_requests"] = [{"reader_digest": S.READER["config_sha256"], "request_digest": "same-request", "choice": choice}]
    with pytest.raises(report.ReportError, match="same-request.*a-1.*b-2"):
        build(tmp_path, scores)


def test_correlation_minimum_and_n_are_printed(tmp_path):
    configs = {"a": {"model": "a", "harness": "pi"}, "b": {"model": "b", "harness": "pi"}}
    scores = [d for p in configs for d in S.config_set(p, r_pass=1, pairs=1, e=[1], s=[1], controls=(1, 1))]
    rep = build(tmp_path, scores, configs)
    rc = next(iter(rep["sections"][0]["tables"][0]["rank_correlation"].values()))
    assert rc["n"] == 2 and rc["spearman"] is None
    assert "2 configurations with both; it needs three" in report.render(rep, False)


@pytest.mark.parametrize("pairs", [2, 20])
def test_missed_conflicts_from_validated_bundles_count_as_failed_pairs(tmp_path, pairs):
    scores = []
    for prefix, mode in (("a", "default"), ("ac", "coached")):
        for seed in range(pairs):
            for side in ("A", "B"):
                eid = f"{prefix}-r{side.lower()}{seed}"
                b = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": side == "A"},
                           pair=f"p{seed}", sibling=side, seed=seed, mode=mode, episode=eid)
                b.user("Run the batches.", kind="opening_prompt")
                b.planted = [{"name": "conflict", "scheduled": {"required": True}}]
                if prefix == "ac" and seed == pairs - 1:
                    b.reach = {"status": "not_reached", "not_reached": ["conflict"]}
                else:
                    b.fired()
                    if side == "B":
                        b.out("Which batch?", label="answerable", reply=ROUTING_KEY["decision_reply"], wait=(2000, ["verdict_reply"]))
                        b.user(ROUTING_KEY["decision_reply"])
                b.batch(b.t, b.t, {"out/summary.txt": "kept: small" if seed == pairs - 1 else "kept: big"})
                b.out("Done.")
                scores.append(score_episode(load(b.write(tmp_path / eid), validate=True)))
    rep = build(tmp_path / "results", scores)
    cell = rows(rep, mode="coached")[0][("m1", "pi")]
    gap = rep["sections"][0]["coached_gap"][0]["R"]
    assert cell["R"]["official"] and cell["R"]["judged"] == pairs
    assert cell["R"]["value"] == (pairs - 1) / pairs
    assert cell["R"]["not_reached"] and not cell["R"]["incomplete_pairs"]
    assert gap["value"] == 0 and gap["n"] == pairs


def test_unread_control_from_validated_bundle_stays_incomplete(tmp_path):
    b = Bundle("explanation", control=True, expected={"reader_true_cause": {"which": 1}}, episode="a-ce2", seed=200)
    b.user("Why is the machine slow?", kind="opening_prompt")
    b.answers = [b.out("Stale dns cache entries are the problem here.")]
    ep = load(b.write(tmp_path / "bundle"), validate=True)
    no_reader = score_episode(ep)
    c = cfg()
    judged = score_episode(ep, R.make(c), c)
    assert no_reader["control"]["passed"] is None and judged["control"]["passed"] is False
    base = S.config_set("a", r_pass=1, pairs=1, e=[], s=[1], controls=(1, 1))
    rep = build(tmp_path / "unread", base + [no_reader])
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["eligibility"]["explanation"]["status"] == "not established" and cell["capability_status"] == "incomplete"
    rep = build(tmp_path / "judged", base + [judged], scripted_reader=True)
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["eligibility"]["explanation"]["rate"] == 0.5 and not cell["eligibility"]["explanation"]["eligible"]
