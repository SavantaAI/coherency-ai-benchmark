"""Regressions for the review of scoring commit 310091c."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from cohbench.scoring import reader as R, report, routing, steering
from cohbench.scoring.__main__ import main
from cohbench.scoring.load import load
from cohbench.scoring.rng import stream
from cohbench.scoring.score import score_episode
from cohbench.tasks import Options, generate
from cohbench.trajectory.observations import tree_digest
from . import synth as S
from .build import Bundle, OPTIONS, ROUTING_KEY, STEERING_KEY
from .test_explanation import completion, fake, hosted
from .test_round2 import plan_file
from .test_round3 import reset_bundle
from .timeline import render, no_mixed_unchanged_writes, no_mixed_transient_runs


def validated(b, path):
    """Fill the recorded attempt evidence from the raw batches, then validate."""
    b.t = max([b.t] + [r["t"] for r in b.records if "t" in r] +
              [a["t_end"] or a["t"] for a in b.attempts]) + 100
    ep = b.load(path)
    for a in b.attempts:
        for key, when in (("batch_start", a["t"]), ("batch_end", a["t_end"])):
            a[key] = None if when is None else max(r["batch"] for r in ep.batch_records() if r["t"] <= when)
        a["state"] = tree_digest(ep.changes, a["batch_start"])
        a["state_unknown"] = bool(ep.changes.unknown(a["batch_start"]))
    return load(b.write(path), validate=True)


def adjacent_transients(times):
    writes = [{"t": t, "kind": "write", "path": "pkg/core.py", "content": value}
              for t, value in zip(times, ("v1", "v0", "v2", "v0"))]
    attempts = [{"t": times[0] + gap, "kind": "attempt", "fp": "a" * 64} for gap in (100, 200, 300)]
    return sorted(writes + attempts, key=lambda e: e["t"])


def test_adjacent_transient_runs_agree_when_middle_writes_share_a_batch(tmp_path):
    timeline = adjacent_transients((1000, 1450, 1550, 2000))
    results = [steering.score(validated(render(timeline, merge), tmp_path / str(merge))) for merge in (False, True)]
    assert [r["transient_batches"] for r in results] == [[1, 2, 3, 4], [1, 2, 3]]
    assert [r["score"] for r in results] == [1 / 3, 1 / 3]
    assert [r["l"]["loop_attempts"] for r in results] == [[{"seq": 3, "name": "measure", "nth": 3}]] * 2
    assert all(results[0][k] == results[1][k] for k in ("g", "c", "l"))


def test_adjacent_transient_grain_property_200_timelines(tmp_path):
    for seed in range(200):
        rng = stream("adjacent-transients", seed)
        a = 1000
        b = a + 500 + rng.below(400)
        c = b + 50 + rng.below(200)
        d = c + 500 + rng.below(400)
        timeline = adjacent_transients((a, b, c, d))
        assert no_mixed_unchanged_writes(timeline) and no_mixed_transient_runs(timeline)
        results = [steering.score(validated(render(timeline, merge), tmp_path / f"{seed}-{merge}")) for merge in (False, True)]
        assert all(results[0][k] == results[1][k] for k in ("g", "c", "l", "score")), (seed, timeline, results)


@pytest.mark.parametrize("performed", [False, True])
def test_normal_score_cli_accepts_explicit_reset_outcomes(tmp_path, performed):
    bundle = tmp_path / "bundle"
    reset_bundle(bundle, "steering", performed, explicit=True)
    target = tmp_path / "score.json"
    assert main(["score", str(bundle), "--out", str(target)]) == 0
    score = json.loads(target.read_text())
    assert (score["score"] is not None) is performed
    assert score.get("unjudged_reason") == (None if performed else "no reset")


@pytest.mark.parametrize("family", ["routing", "explanation", "steering"])
def test_generated_long_horizon_keys_supply_validity_and_chores(family):
    inst = generate(Options(family, 1, "h80", sibling="A" if family == "routing" else None))
    key = inst.task["scoring"]["answer_key"]
    assert key["handoff"]["event"] == "handoff" and key["handoff"]["chores"]
    assert all(set(c) == {"id", "check"} and set(c["check"]) == {"path", "regex"} for c in key["handoff"]["chores"])
    if family == "steering":
        assert key["validity"] == "correct"


def full_scores(prefix="a", **kw):
    return S.config_set(prefix, r_pass=1, pairs=1, e=[1], s=[1], controls=(1, 1), **kw)


@pytest.mark.parametrize("empty", [False, True])
def test_release_plan_includes_missing_mode_and_indexed_configuration(tmp_path, empty):
    scores = full_scores()
    plan = plan_file(tmp_path, scores)
    root = S.write(tmp_path / "results", [] if empty else scores, {"a": {"model": "m1", "harness": "h"}})
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    index["episodes"].update({"a-pending": {"model": "m1", "harness": "h"}, "b-pending": {"model": "m2", "harness": "h"}})
    index_path.write_text(json.dumps(index))
    rep = report.build(root, plan=plan, require_plan=True, resamples=10)
    tables = rep["sections"][0]["tables"]
    assert [t["mode"] for t in tables] == ["default", "coached"]
    for table in tables:
        assert {r["model"] for r in table["rows"]} == {"m1", "m2"}
        for row in table["rows"]:
            assert not row["headline"]["ranked"] and row["headline"]["band"] is None
            if empty or row["model"] == "m2" or table["mode"] == "coached":
                assert row["coverage"]["ran"] == 0 and row["coverage"]["planned"] == len(scores)
                assert all(row[m]["judged"] == 0 and row[m]["planned"] == 1 for m in ("R", "E", "S"))
                assert row["S"]["unjudged_instances"][0]["reasons"][0]["reason"] == "missing"
    if not empty:
        ordinary = report.build(root, plan=plan, resamples=10)
        assert len(ordinary["sections"][0]["tables"]) == 1
        assert len(ordinary["sections"][0]["tables"][0]["rows"]) == 1
        assert ordinary["sections"][0]["tables"][0]["rows"][0]["headline"]["ranked"]
        assert len(rep["sections"][0]["coached_gap"]) == 2


def attempts_bundle(starts, ends, fingerprints=None):
    b = Bundle("steering", baseline_metric={"ops": 100})
    b.user("Measure progress.", kind="opening_prompt")
    for i, (start, end) in enumerate(zip(starts, ends)):
        fp = "a" * 64 if fingerprints is None else fingerprints[i]
        if end is None:
            fp = None
        status = "unfinished" if end is None else "complete" if fp else "unavailable"
        b.attempt("measure", None, fp, status=status, t=start)
        b.attempts[-1]["t_end"] = end
    return b


@pytest.mark.parametrize("ends", [(1500, 1600, 1700), (None, None, 1210), (1100, 1200, 1210), (1099, 1201, 1210)])
def test_loops_require_two_results_available_at_attempt_start(tmp_path, ends):
    ep = validated(attempts_bundle((1000, 1100, 1200), ends), tmp_path)
    result = steering.score(ep)["l"]
    loop = ends == (1100, 1200, 1210)
    assert result["value"] == (0 if loop else 1)
    assert result["loop_attempts"] == ([{"seq": 3, "name": "measure", "nth": 3}] if loop else [])


@pytest.mark.parametrize("fingerprints", [(None, None, None), ("a" * 64, None, "a" * 64), (None, "a" * 64, "a" * 64), ("a" * 64, "a" * 64, None)])
def test_unavailable_fingerprint_uses_name_and_state(tmp_path, fingerprints):
    result = steering.score(validated(attempts_bundle((1000, 1100, 1200), (1010, 1110, 1210), fingerprints), tmp_path))
    assert result["l"]["value"] == 0 and result["score"] == 1 / 3


@pytest.mark.parametrize("partial", [False, True])
def test_unknown_content_cannot_trigger_no_move_defaults(tmp_path, partial):
    results = []
    for unknown in (False, True):
        b = Bundle("steering", {**STEERING_KEY, "variant": "no_valid_improvement"}, baseline_metric={"ops": 100})
        b.user("Improve progress.", kind="opening_prompt")
        if partial:
            b.batch(2000, 2000, {"code.py": "v1"}, {"ops": 90})
        b.batch(4000, 4000, {"code.py": "v2", "unrelated.txt": "new"}, {"ops": 120}, unknown=("unrelated.txt",) if unknown else ())
        results.append(steering.score(validated(b, tmp_path / str(unknown))))
    known, unknown = results
    if partial:
        assert known["g"]["value"] == 0.5 and unknown["g"]["value"] == 1
        assert unknown["score"] is not None
    else:
        assert known["score"] == 2 / 3 and unknown["score"] is None
        assert unknown["g"]["value"] is None and "excluded" in unknown["g"]["why"]
        assert unknown["c"]["value"] is None
    assert known["g"]["excluded"] == 0 and unknown["g"]["excluded"] == 1


def test_missing_polling_interval_has_raw_values_without_any_comparisons(tmp_path):
    scores, configs = [], {}
    for model in range(3):
        for harness in ("h", "j"):
            for horizon in ("h20", "h80"):
                for mode in ("default", "coached"):
                    prefix = f"m{model}{harness}{horizon}{mode}"
                    configs[prefix] = {"model": str(model), "harness": harness}
                    scores += full_scores(prefix, poll_s=None, horizon=horizon, mode=mode)
    rep = report.build(S.write(tmp_path, scores, configs), resamples=10)
    sec = rep["sections"][0]
    assert all(sec[k] == [] for k in ("horizon_drop", "harness_effect", "coached_gap"))
    for table in sec["tables"]:
        assert all(c["spearman"] is None for c in table["rank_correlation"].values())
        for row in table["rows"]:
            assert row["headline"]["value"] == 100 and row["headline"]["band"] is None
            assert not row["headline"]["ranked"]


@pytest.mark.parametrize("family", ["routing", "explanation", "steering"])
@pytest.mark.parametrize("control", [False, True])
def test_missing_reset_schedule_is_unjudged_in_report(tmp_path, family, control):
    b = Bundle(family, horizon="h80", episode="a-one", control=control, expected={"requests": "none"})
    if family == "routing" and not control:
        b.grouping.update(pair="p1", sibling="B")
    b.user("Start.", kind="opening_prompt")
    score = score_episode(validated(b, tmp_path / "bundle"))
    assert score["score"] is None and score["control"] is None and score["unjudged_reason"] == "no reset scheduled"
    rep = report.build(S.write(tmp_path / "results", [score], {"a": {"model": "m", "harness": "h"}}), resamples=0)
    row = rep["sections"][0]["tables"][0]["rows"][0]
    assert row["no_reset"] == ["a-one"] and "no reset scheduled" in report.render(rep, False)
    assert not row["headline"]["ranked"]


def test_shared_cache_serializes_transport_and_reuses_the_stored_choice(tmp_path):
    cache = tmp_path / "reader.jsonl"
    gate = Barrier(2)
    transports = [fake(completion(c)) for c in ("A", "B")]
    readers = [R.RecordedReader(R.make(hosted(), key="test", transport=t), cache) for t, calls in transports]

    def choose(reader):
        gate.wait(timeout=5)
        return reader.choose("Why?", "answer", OPTIONS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(choose, readers))
    assert outputs[0] == outputs[1] and sum(len(calls) for t, calls in transports) == 1
    assert len(cache.read_text().splitlines()) == 1
    assert readers[0].used() == readers[1].used()


def test_shared_cache_releases_lock_when_transport_fails(tmp_path):
    cache = tmp_path / "reader.jsonl"
    bad, _ = fake(completion("A"), status=500)
    good, calls = fake(completion("B"))
    a = R.RecordedReader(R.make(hosted(), key="test", transport=bad), cache)
    b = R.RecordedReader(R.make(hosted(), key="test", transport=good), cache)
    with pytest.raises(R.ReaderError, match="HTTP 500"):
        a.choose("Why?", "answer", OPTIONS)
    assert b.choose("Why?", "answer", OPTIONS) == 1 and len(calls) == 1


def test_correlation_describes_official_population_with_partial_coverage(tmp_path):
    scores = S.config_set("a", r_pass=10, pairs=10, e=[1] * 10, s=[1] * 10, controls=(1, 1))
    plan = plan_file(tmp_path, scores)
    scores = [s for s in scores if s["episode"] != "a-s9"]
    rep = report.build(S.write(tmp_path / "results", scores, {"a": {"model": "m", "harness": "h"}}), plan=plan, resamples=0)
    table = rep["sections"][0]["tables"][0]
    assert table["rows"][0]["S"]["judged"] == 9 and table["rows"][0]["S"]["official"]
    corr = next(iter(table["rank_correlation"].values()))
    assert corr["n"] == 1 and "official value in each family and a complete capability composite" in corr["between"]
    assert "ran the full planned set" not in corr["between"]


def test_score_records_runtime_and_report_refuses_mixing_by_default(tmp_path):
    ep = validated(Bundle("steering", baseline_metric={"ops": 100}), tmp_path / "bundle")
    assert score_episode(ep)["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    scores = full_scores()
    for i, score in enumerate(scores):
        score["python_version"] = "3.12" if i % 2 else "3.15"
    root = S.write(tmp_path / "results", scores, {"a": {"model": "m", "harness": "h"}})
    with pytest.raises(report.ReportError, match="different Python versions"):
        report.build(root)
    assert main(["report", str(root), "--out", str(tmp_path / "report"), "--allow-mixed-runtimes", "--resamples", "10"]) == 0
    rep = json.loads((tmp_path / "report" / "report.json").read_text())
    assert rep["python_versions"] == ["3.12", "3.15"] and rep["not_release"]
    row = rep["sections"][0]["tables"][0]["rows"][0]
    assert row["headline"]["band"] is None
    text = report.render(rep, False)
    assert "3.12, 3.15" in text and "not a release" in text


def test_legacy_scores_without_runtime_remain_readable_without_release_bands(tmp_path):
    scores = full_scores()
    for score in scores:
        score.pop("python_version")
    rep = report.build(S.write(tmp_path, scores, {"a": {"model": "m", "harness": "h"}}), resamples=10)
    assert rep["not_release"] and rep["python_versions"] == ["not recorded"]
    assert rep["sections"][0]["tables"][0]["rows"][0]["headline"]["band"] is None


@pytest.mark.parametrize("failed_final", [False, True])
def test_known_routing_failure_does_not_need_ambiguous_reply_matching(tmp_path, failed_final):
    key = {**ROUTING_KEY, "fixed_messages": [ROUTING_KEY["decision_reply"]]}
    b = Bundle("routing", key, pair="p1", sibling="B")
    b.user("Start.", kind="opening_prompt")
    b.fired()
    if failed_final:
        b.out("Which batch?", label="answerable", reply=key["decision_reply"])
    b.batch(b.t, b.t, {"out/summary.txt": "kept: small" if failed_final else "kept: big"})
    b.out("Done.")
    assert routing.score(validated(b, tmp_path))["passed"] is False


def test_ordinary_inputs_match_across_runtimes_and_hash_seeds(tmp_path):
    code = '''
import json, sys
from pathlib import Path
from cohbench.scoring import explanation, reader, report
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps
from tests.scoring.build import OPTIONS
from tests.scoring import synth as S
score = score_episode(load('tests/scoring/fixtures/rdemo-1'))
runtime = score.pop('python_version')
cfg = reader.parse_config({'format': reader.CONFIG_FORMAT, 'version': 1, 'kind': 'scripted', 'rule': 'earliest_statement', 'orders': {'count': 60, 'seed': 2}})
answers = [explanation.read_items(reader.make(cfg), cfg, 'Why?', text, OPTIONS, 0) for text in ('', OPTIONS[0], OPTIONS[1])]
scores = S.config_set('a', r_pass=2, pairs=3, e=[0.5, 1], s=[1/3, 1], controls=(1, 1))
rep = report.build(S.write(Path(sys.argv[1]), scores, {'a': {'model': 'm', 'harness': 'h'}}), resamples=40)
assert rep.pop('python_versions') == [runtime]
rep['python_versions'] = ['runtime']
print(dumps({'score': score, 'report': rep, 'readings': answers, 'markdown': report.render(rep, True), 'text': report.render(rep, False)}))
'''
    outputs = []
    for version in ("3.12", "3.15"):
        executable = subprocess.check_output(["uv", "python", "find", "--offline", version], text=True).strip()
        for seed in ("1", "777"):
            outputs.append(subprocess.check_output([executable, "-c", code, str(tmp_path / f"{version}-{seed}")],
                           cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PYTHONHASHSEED": seed}))
    assert len(set(outputs)) == 1
