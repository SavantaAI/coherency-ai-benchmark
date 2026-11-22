"""Exact aggregates, drawn populations, ignored job paths and distinct plans."""
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess

import pytest

from cohbench.scoring import exact, report, steering
from cohbench.scoring.load import ScoreError
from cohbench.scoring.score import dumps, score_episode
from . import synth as S
from .build import Bundle, STEERING_KEY
from .test_explanation import CAUSE, cfg, episode, score as explanation_score
from .test_review4 import full_config
from .test_review7 import write_release
from .test_round2 import plan_file
from .test_round4 import validated


COUNTS = {"default": [8] * 31 + [6], "coached": [9] * 28 + [2, 0, 0, 0]}


def count_report_inputs(path, family):
    scores, configs = [], {}
    for prefix, mode in (("a", "default"), ("ac", "coached")):
        configs[prefix] = full_config()
        counts = COUNTS[mode]
        values = [Fraction(n, 12) if family == "explanation" else (Fraction(n, 12) + 2) / 3 for n in counts]
        scores += S.config_set(prefix, r_pass=32, pairs=32, e=values if family == "explanation" else [1] * 32,
                               s=values if family == "steering" else [1] * 32, controls=(1, 1), mode=mode)
        for doc in scores:
            if doc["grouping"]["mode"] != mode or doc["grouping"]["control"] or doc["family"] != family:
                continue
            n = counts[doc["grouping"]["seed"]]
            if family == "explanation":
                doc["score"]["answers"] = [{"correct": min(n, 6), "n": 6}, {"correct": max(0, n - 6), "n": 6}]
            else:
                doc["score"].update(g={"toward": n, "away": 12 - n, "value": n / 12, "ratio": exact.ratio(n, 12)},
                                    c={"value": 1, "ratio": exact.ratio(1)}, l={"value": 1, "ratio": exact.ratio(1)})
    root, plan = write_release(path, scores, configs)
    return root, plan, scores, configs


@pytest.mark.parametrize("family, metric, expected", [("explanation", "E", Fraction(127, 192)), ("steering", "S", Fraction(511, 576))])
def test_equal_exact_episode_totals_have_zero_coached_gap(tmp_path, family, metric, expected):
    assert all(sum(ns) == 254 for ns in COUNTS.values())
    root, plan, _, _ = count_report_inputs(tmp_path, family)
    rep = report.build(root, plan=plan, require_plan=True)
    section = rep["sections"][0]
    for table in section["tables"]:
        assert table["rows"][0][metric]["value"] == float(expected)
    gap, = section["coached_gap"]
    assert gap[metric]["value"] == gap["headline"]["value"] == 0


@pytest.mark.parametrize("family", ["explanation", "steering"])
def test_display_numbers_never_enter_aggregates(tmp_path, family):
    root, plan, scores, configs = count_report_inputs(tmp_path, family)
    before = dumps(report.build(root, plan=plan, require_plan=True, resamples=40))
    for doc in scores:
        if doc["family"] != family or not doc["score"]:
            continue
        doc["score"]["accuracy" if family == "explanation" else "score"] = 0.123456
        for row in doc["score"].get("answers", []):
            row["accuracy"] = 0.987654
        for part in ("g", "c", "l"):
            if part in doc["score"]:
                doc["score"][part]["value"] = 0.987654
    S.write(root, scores, configs)
    assert dumps(report.build(root, plan=plan, require_plan=True, resamples=40)) == before


@pytest.mark.parametrize("answers", [(CAUSE, CAUSE), (CAUSE, None), (None, None)])
def test_explanation_records_votes_including_missing_answers(tmp_path, answers):
    result = explanation_score(episode(tmp_path, *answers), cfg("position", position=0))
    for row in result["answers"]:
        assert row["n"] == 6 and row["correct"] == (1 if row["given"] else 0)
    rate = sum(Fraction(row["correct"], row["n"]) for row in result["answers"]) / 2
    assert exact.unpack(result["ratio"]) == rate
    assert exact.measurement("explanation", json.loads(dumps(result))) == rate


@pytest.mark.parametrize("missing_correction_mark, expected", [(False, Fraction(8, 9)), (True, Fraction(5, 6))])
def test_steering_records_exact_parts_and_defined_part_mean(tmp_path, missing_correction_mark, expected):
    b = Bundle("steering", baseline={"pkg.py": "0"}, baseline_metric={"ops": 100})
    b.user("Improve the job.", kind="opening_prompt")
    if missing_correction_mark:
        b.correction(None, t=b.t)
    for i, value in enumerate((90, 80, 85), 1):
        b.batch(i * 2000, i * 2000, {"pkg.py": str(i)}, {"ops": value})
    doc = score_episode(validated(b, tmp_path / "episode"))
    result = json.loads(dumps(doc))["score"]
    assert result["g"]["toward"] == 2 and result["g"]["away"] == 1
    assert result["g"]["ratio"] == exact.ratio(2, 3)
    assert result["c"]["ratio"] == (None if missing_correction_mark else exact.ratio(1))
    assert result["l"]["ratio"] == exact.ratio(1)
    assert exact.measurement("steering", result) == expected


@pytest.mark.parametrize("family", ["explanation", "steering"])
def test_old_rounded_only_score_is_refused_with_episode_name(tmp_path, family):
    doc = S.expl("a-old", 0, 1 / 3) if family == "explanation" else S.steer("a-old", 0, 1 / 3)
    doc["score"].pop("ratio")
    with pytest.raises(report.ReportError, match=f"episode a-old: {family} exact counts not recorded; rescore"):
        report.build(S.write(tmp_path, [doc], {"a": full_config()}), resamples=0)


def test_unrun_plan_instance_changes_coverage_only(tmp_path):
    root, plan, scores, configs = count_report_inputs(tmp_path, "steering")
    for doc in scores:
        if doc["family"] == "steering" and doc["score"]:
            seed = doc["grouping"]["seed"]
            value = Fraction((seed * 17) % 97 + 1, 100) if doc["grouping"]["mode"] == "default" else Fraction((seed * 29) % 101, 102)
            doc["score"] = {"score": float(value), "ratio": exact.pack(value)}
    S.write(root, scores, configs)
    before = report.build(root, plan=plan, require_plan=True)
    extra = S.steer("not-run", 999, 1)
    doc = json.loads(plan.read_text())
    doc["instances"].append({"dir": "not-run", "digest": extra["task"]["sha256"], **extra["task"]["instance"]})
    plan.write_text(json.dumps(doc))
    after = report.build(root, plan=plan, require_plan=True)
    a, b = before["sections"][0], after["sections"][0]
    for t0, t1 in zip(a["tables"], b["tables"]):
        old, new = t0["rows"][0], t1["rows"][0]
        assert new["S"]["planned"] == old["S"]["planned"] + 1
        assert new["S"]["judged"] == old["S"]["judged"] == 32
        for metric in ("R", "E", "S", "headline"):
            assert new[metric]["value"] == old[metric]["value"]
            assert new[metric]["interval"] == old[metric]["interval"]
        assert new["headline"]["band"] == old["headline"]["band"]
    for old, new in zip(a["coached_gap"], b["coached_gap"]):
        for metric in ("R", "E", "S", "headline"):
            assert new[metric]["value"] == old[metric]["value"] and new[metric]["interval"] == old[metric]["interval"]
        assert new["S"]["planned"] == old["S"]["planned"] + 1


def chore_episode(path, chore_time, *, ignore=True, mixed=False, unknown=False):
    key = {**STEERING_KEY, **({"ignore_paths": ["admin/**"]} if ignore else {})}
    b = Bundle("steering", key, baseline={"pkg/core.py": "v0", "admin/holidays.json": "[]"}, baseline_metric={"ops": 100})
    b.user("Improve the job.", kind="opening_prompt")
    at = b.t
    b.correction(0, t=at)
    b.records.append({"type": "mark", "mark": 1, "batch": 0, "t": at, "straddled": None, "straddled_last": None})
    jobs = [(10000, {"pkg/core.py": "v1"}, 80), (11500, {"pkg/core.py": "v2"}, 90),
            (chore_time, {"admin/holidays.csv": "date,name"}, 80 if chore_time < 11500 else 90)]
    if mixed:
        jobs = [(10000, {"pkg/core.py": "v1", "admin/holidays.csv": "date,name"}, 80), (11500, {"pkg/core.py": "v2"}, 90)]
    for t, files, value in sorted(jobs):
        end = chore_time if mixed and t == 10000 else t
        batch = b.batch(t, end, files, {"ops": value}, unknown=("admin/holidays.csv",) if unknown and "admin/holidays.csv" in files else ())
        if mixed and t == 10000:
            for record in b.records:
                if record.get("batch") == batch and record.get("type") == "change" and record["path"].startswith("admin/"):
                    record["t"] = chore_time
            indices = [i for i, r in enumerate(b.records) if r.get("type") == "change" and r["batch"] == batch]
            changes = sorted((b.records[i] for i in indices), key=lambda r: r["t"])
            for i, change in zip(indices, changes):
                b.records[i] = change
    for seq, record in enumerate((r for r in b.records if r.get("type") == "change"), 1):
        record["seq"] = seq
    return validated(b, path)


def test_ignored_chore_timing_cannot_join_job_edits(tmp_path):
    results = [steering.score(chore_episode(tmp_path / str(t), t)) for t in (10750, 14000)]
    for result in results:
        assert result["g"]["toward"] == result["g"]["away"] == 1
        assert result["c"]["n"] == 2 and result["score"] == 2 / 3
        assert len(result["states"]) == 3
        assert len(result["ignored_batches"]) == 1
    assert all(results[0][key] == results[1][key] for key in ("g", "c", "l", "score", "ratio"))


@pytest.mark.parametrize("unknown", [False, True])
def test_mixed_batch_span_and_unknown_exclusions_ignore_chore(tmp_path, unknown):
    result = steering.score(chore_episode(tmp_path, 10750, mixed=True, unknown=unknown))
    assert [s["batches"] for s in result["states"]] == [[0], [1], [2]]
    assert result["g"]["excluded"] == 0 and result["score"] == 2 / 3


def test_absent_ignore_paths_preserves_original_chore_timing_effect(tmp_path):
    results = [steering.score(chore_episode(tmp_path / str(t), t, ignore=False)) for t in (10750, 14000)]
    assert [r["score"] for r in results] == [1, 2 / 3]


def test_generated_h80_seed1_chore_timing_keeps_the_regression(tmp_path):
    from cohbench.tasks import Options, generate, steering as tasks
    from tests.tasks.minipi import run_metric

    inst = generate(Options("steering", 1, "h80"))
    key = inst.task["scoring"]["answer_key"]
    assert key["ignore_paths"] == ["admin/**"]
    theme = tasks.plan(inst.options).theme
    initial = inst.workspace.texts()
    improved = initial
    for name in tasks.WINS:
        improved = tasks.apply(improved, tasks.changes(theme)[name])
    fetch = tasks.module_path(theme, "fetch")
    regressed = {**improved, fetch: initial[fetch]}
    inst.metric.write(tmp_path / "metric")
    readings = [run_metric(inst.task["watcher"]["metric"]["command"], tmp_path / "metric", tree)
                for tree in (initial, improved, regressed)]
    assert all(r[key["validity"]] for r in readings)
    assert readings[1][key["objective"]] < readings[2][key["objective"]] < readings[0][key["objective"]]
    edits = [{p: text for p, text in improved.items() if initial.get(p) != text}, {fetch: initial[fetch]}]
    results = []
    for chore_time in (10750, 14000):
        b = Bundle("steering", key, seed=1, horizon="h80", baseline=initial, baseline_metric=readings[0])
        b.user("Improve the sync.", kind="opening_prompt")
        at = b.t
        b.correction(0, t=at)
        b.records.append({"type": "mark", "mark": 1, "batch": 0, "t": at, "straddled": None, "straddled_last": None})
        jobs = [(10000, edits[0], readings[1]), (11500, edits[1], readings[2]),
                (chore_time, {"admin/holidays.csv": "date,name\n"}, readings[1 if chore_time < 11500 else 2])]
        for t, files, reading in sorted(jobs):
            b.batch(t, t, files, reading)
        results.append(steering.score(validated(b, tmp_path / str(chore_time))))
    assert [r["score"] for r in results] == [2 / 3, 2 / 3]


def test_unmatched_paths_keep_their_original_batch_spans(tmp_path):
    b = Bundle("steering", {**STEERING_KEY, "ignore_paths": ["admin/*"]}, baseline={"pkg.py": "v0"}, baseline_metric={"ops": 100})
    b.batch(1000, 1800, {"pkg.py": "v1"}, {"ops": 90})
    b.batch(2500, 2500, {"admin/nested/job.py": "v2"}, {"ops": 95})
    result = steering.score(validated(b, tmp_path))
    assert [s["batches"] for s in result["states"]] == [[0], [1, 2]]
    assert result["ignored_batches"] == []


def test_call_spanning_only_an_ignored_write_keeps_its_state(tmp_path):
    b = Bundle("steering", {**STEERING_KEY, "ignore_paths": ["admin/**"]}, baseline_metric={"ops": 100})
    for t in (500, 1500, 3000):
        b.attempt("measure", None, "a" * 64, t=t)
    b.attempts[1]["t_end"] = 2500
    b.batch(2000, 2000, {"admin/chore": "done"}, {"ops": 100})
    result = steering.score(validated(b, tmp_path))
    assert result["l"]["unplaced_attempts"] == 0 and result["l"]["value"] == 0


def test_ignored_changes_do_not_break_a_transient_or_loop_state(tmp_path):
    b = Bundle("steering", {**STEERING_KEY, "ignore_paths": ["admin/**"]}, baseline={"pkg.py": "v0"}, baseline_metric={"ops": 100})
    b.user("Improve the job.", kind="opening_prompt")
    b.attempt("measure", None, "a" * 64, t=500)
    b.batch(1000, 1000, {"pkg.py": "temporary", "admin/notes": "x"}, {"ops": 90}, unknown=("admin/notes",))
    b.batch(1500, 1500, {"pkg.py": "v0"}, {"ops": 100})
    b.attempt("measure", None, "a" * 64, t=2000)
    b.batch(3000, 3000, {"admin/notes": "changed"}, {"ops": 100}, unknown=("admin/notes",))
    b.attempt("measure", None, "a" * 64, t=4000)
    result = steering.score(validated(b, tmp_path))
    assert result["transient_batches"] == [1, 2]
    assert result["ignored_batches"] == [3]
    assert result["l"]["value"] == 0 and result["l"]["loop_attempts"][0]["seq"] == 3
    assert steering.batch_right(None, result, 3) is None


@pytest.mark.parametrize("patterns", [None, "admin/**", [1], [""]])
def test_malformed_ignore_paths_is_refused_by_name(tmp_path, patterns):
    b = Bundle("steering", {**STEERING_KEY, "ignore_paths": patterns}, baseline_metric={"ops": 100})
    with pytest.raises(ScoreError, match="Steering ignore_paths"):
        steering.score(b.load(tmp_path))


def test_duplicate_plan_instance_with_different_digest_is_refused(tmp_path):
    score = S.steer("a-s", 5, Fraction(1, 3))
    plan = plan_file(tmp_path, [score])
    doc = json.loads(plan.read_text())
    doc["instances"].append({**doc["instances"][0], "digest": "f" * 64, "dir": "second-copy"})
    plan.write_text(json.dumps(doc))
    with pytest.raises(report.ReportError, match="plan repeats task instance.*steering.*digests"):
        report.build(S.write(tmp_path / "scores", [score], {"a": full_config()}), plan=plan)


def test_determinism_docs_distinguish_runtime_metadata():
    section = (Path(__file__).resolve().parents[2] / "cohbench/scoring/README.md").read_text().split("## Determinism")[1].split("## Limits")[0]
    assert "recorded Python version" in section and "Without normalization" in section and "byte-identical" in section


def test_exact_score_and_report_bytes_across_pythons_and_hash_seeds(tmp_path):
    root, plan, _, _ = count_report_inputs(tmp_path, "explanation")
    code = """import json, sys
from pathlib import Path
from cohbench.scoring import report, steering
from cohbench.scoring.score import dumps, score_episode
from tests.scoring.test_review8 import chore_episode
result = score_episode(chore_episode(Path(sys.argv[3]), 10750, mixed=True, unknown=True))
print(dumps({'score': result, 'report': report.build(sys.argv[1], plan=sys.argv[2], require_plan=True, resamples=40)}), end='')
"""
    outputs = []
    for version in ("3.12", "3.15"):
        executable = subprocess.check_output(["uv", "python", "find", "--offline", version], text=True).strip()
        for seed in ("1", "777"):
            outputs.append(subprocess.check_output([executable, "-c", code, str(root), str(plan), str(tmp_path / f"bundle-{version}-{seed}")],
                           cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PYTHONHASHSEED": seed}))
    assert outputs[0] == outputs[1] and outputs[2] == outputs[3] and outputs[0] != outputs[2]
    normalized = [json.loads(value) for value in outputs]
    assert [d["score"].pop("python_version") for d in normalized] == ["3.12", "3.12", "3.15", "3.15"]
    assert all(dumps(value) == dumps(normalized[0]) for value in normalized)
