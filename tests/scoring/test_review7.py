"""Published ties, population-based draws and positive release evidence."""
import copy
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess

import pytest

from cohbench.scoring import report, stats
from cohbench.scoring.score import dumps, tidy
from . import synth as S
from .runtimes import runtime_interpreters
from .test_review4 import full_config
from .test_review6 import release_inputs
from .test_round2 import plan_file


def write_release(path, scores, configs):
    for score in scores:
        score["python_version"] = "3.12"
        if score.get("reader"):
            score["reader_record"] = True
    root = S.write(path / "results", scores, configs)
    return root, plan_file(path, scores)


def test_equal_published_headlines_share_a_band(tmp_path):
    scores, configs = [], {}
    for model, e, s in (("a", Fraction(1, 3), Fraction(7, 12)), ("b", Fraction(7, 12), Fraction(1, 3))):
        for suffix, mode in (("", "default"), ("c", "coached")):
            prefix = model + suffix
            configs[prefix] = full_config(model=model)
            scores += S.config_set(prefix, r_pass=0, pairs=6, e=[e] * 6, s=[s] * 6, controls=(1, 1), mode=mode)
    root, plan = write_release(tmp_path, scores, configs)
    rep = json.loads(dumps(S.calibrated_report(root, plan=plan, require_plan=True)))
    assert not rep["not_release"]
    for table in rep["sections"][0]["tables"]:
        for row in table["rows"]:
            assert row["headline"]["interval"] == [21.342986, 21.342986]
            assert row["headline"]["band"]["ranks"] == "1-2"


def test_band_overlap_and_order_use_published_endpoints():
    value = 21.342975
    up = math.nextafter(value, math.inf)
    down = math.nextafter(value, -math.inf)
    assert stats.overlaps([down, down], [up, up])
    bands = stats.rank_bands([("b", up, [up, up]), ("a", down, [down, down]), ("c", 10, [9, 11])])
    assert [bands[k]["ranks"] for k in ("a", "b", "c")] == ["1-2", "1-2", "3"]


def test_spearman_uses_published_headline_ties():
    assert stats.spearman([10, 20, math.nextafter(20.0, math.inf)], [1, 2, 3]) == pytest.approx(math.sqrt(3) / 2)


def test_capability_count_tie_has_shared_correlation_ranks(tmp_path):
    scores, configs = [], {}
    for model, passed, s in (("a", (22, 22, 22), 0.125), ("b", (22, 24, 24), 0.5), ("c", (23, 23, 24), 1)):
        for suffix, mode in (("", "default"), ("c", "coached")):
            prefix = model + suffix
            configs[prefix] = full_config(model=model)
            scores += S.config_set(prefix, r_pass=1, pairs=1, e=[1], s=[s], controls=(0, 0), mode=mode)
            for family, count in zip(report.FAMILIES, passed):
                scores += [S.control(f"{prefix}-{family}{i}", family, 100 + i, i < count, mode=mode) for i in range(24)]
    root, plan = write_release(tmp_path, scores, configs)
    rep = S.calibrated_report(root, plan=plan, require_plan=True)
    for table in rep["sections"][0]["tables"]:
        assert table["rows"][1]["capability"] == table["rows"][2]["capability"] == float(Fraction(70, 72))
        correlation, = table["rank_correlation"].values()
        assert tidy(correlation)["spearman"] == 0.866025


def test_exact_count_composites_are_not_rounded_into_a_tie():
    counts = {f: {"official": True, "passed": 9000000, "judged": 10000000} for f in report.FAMILIES}
    a = report.capability_fraction(counts)
    counts["routing"]["passed"] += 1
    b = report.capability_fraction(counts)
    assert tidy(float(a)) == tidy(float(b))
    assert stats.ranks([a, b, Fraction(1)]) == [1, 2, 3]


def varying_inputs(path):
    scores, configs = [], {}
    for model in ("amber", "blue", "cyan"):
        for harness in ("oak", "pine"):
            for horizon in ("h20", "h40"):
                for mode in ("default", "coached"):
                    prefix = f"{model}{harness}{horizon}{mode}"
                    configs[prefix] = full_config(harness=harness, model=model)
                    offset = (0 if model == "amber" else 0.1 if model == "blue" else 0.2) + (0.03 if harness == "pine" else 0)
                    offset += (0.02 if mode == "coached" else 0) - (0.01 if horizon == "h40" else 0)
                    scores += S.config_set(prefix, r_pass=4, pairs=6, e=[0.2 + offset, 0.7, 0.3, 0.9, 0.5, 0.1],
                                           s=[0.1, 0.2 + offset, 0.6, 0.8, 0.3, 0.9], controls=(1, 1), horizon=horizon, mode=mode)
    return write_release(path, scores, configs)


def numeric_report(value):
    """Ignore labels and list order, retaining every numeric field and band label."""
    if isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value if value.replace("-", "").isdigit() else None
    if isinstance(value, list):
        return sorted((numeric_report(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        # Configuration keys in these two maps contain names.
        return {k: numeric_report(list(v.values()) if k in ("eligibility", "rank_correlation") and isinstance(v, dict) else v)
                for k, v in value.items() if k not in ("configuration", "configurations")}
    raise AssertionError(type(value))


@pytest.mark.parametrize("change", ["model", "harness", "index_order", "score_files"])
def test_names_and_input_order_do_not_change_any_report_number(tmp_path, change):
    root, plan = varying_inputs(tmp_path)
    before = S.calibrated_report(root, plan=plan, require_plan=True)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    if change in ("model", "harness"):
        old, new = ("blue", "violet") if change == "model" else ("oak", "willow")
        for config in index["episodes"].values():
            if config[change] == old:
                config[change] = new
    if change == "index_order":
        index["episodes"] = dict(reversed(list(index["episodes"].items())))
    if change == "score_files":
        for i, file in enumerate(sorted((root / "scores").glob("*.score.json"))):
            file.rename(file.with_name(f"renamed-{9999 - i}.score.json"))
    index_path.write_text(json.dumps(index))
    after = S.calibrated_report(root, plan=plan, require_plan=True)
    assert numeric_report(tidy(before)) == numeric_report(tidy(after))
    def cells(rep):
        return {tuple(row["episodes"]): numeric_report(tidy(row)) for section in rep["sections"]
                for table in section["tables"] for row in table["rows"]}
    assert cells(before) == cells(after)


def test_matching_populations_share_cell_and_paired_draws(tmp_path):
    root, plan = varying_inputs(tmp_path)
    rep = S.calibrated_report(root, plan=plan, require_plan=True)
    section = rep["sections"][0]
    # All Routing inputs agree. Sharing draws gives identical cell intervals
    # and exactly zero spread, drop and coached gap in every resample.
    intervals = [tuple(row["R"]["interval"]) for t in section["tables"] for row in t["rows"]]
    assert len(set(intervals)) == 1
    for field in ("coached_gap", "horizon_drop", "harness_effect"):
        assert all(cmp["R"]["interval"] == [0, 0] for cmp in section[field])


def test_family_stream_depends_on_its_population_only():
    population = {f: [f"{f}-1", f"{f}-2"] for f in report.FAMILIES}
    original = report.sampling_streams(0, population)
    changed = copy.deepcopy(population)
    changed["steering"].append("steering-3")
    other = report.sampling_streams(0, changed)
    a = {f: [original[f].next64() for _ in range(5)] for f in report.FAMILIES}
    b = {f: [other[f].next64() for _ in range(5)] for f in report.FAMILIES}
    assert a["routing"] == b["routing"] and a["explanation"] == b["explanation"] and a["steering"] != b["steering"]
    assert report.sampling_streams(1, population)["routing"].next64() != a["routing"][0]


@pytest.mark.parametrize("evidence", ["absent", "null", "false", "one", "text"])
def test_release_requires_explicit_reader_record(tmp_path, evidence):
    root, plan, scores = release_inputs(tmp_path)
    for score in scores:
        if score["reader"]:
            score.pop("reader_requests", None)
            score.pop("reader_record", None)
            if evidence != "absent":
                score["reader_record"] = {"null": None, "false": False, "one": 1, "text": "true"}[evidence]
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    rep = S.calibrated_report(root, plan=plan, require_plan=True)
    assert rep["not_release"]
    assert "without a persistent record" in rep["parameters"]["nonrelease_note"]
    assert all(row["headline"]["band"] is None for t in rep["sections"][0]["tables"] for row in t["rows"])


@pytest.mark.parametrize("field", ["reader", "reader.kind", "reader.model", "reader.provider", "reader.prompt_version",
    "sanity", "sanity.no_answer", "sanity.buried", "sanity.no_answer.n", "sanity.buried.correct", "python_version", "poll_s", "control", "score"])
def test_absent_release_evidence_cannot_publish_a_band(tmp_path, field):
    root, plan, scores = release_inputs(tmp_path)
    for score in scores:
        if field in ("python_version", "poll_s"):
            score.pop(field)
        elif field == "control" and score["control"]:
            score.pop("control")
        elif field == "score" and score["score"]:
            score["score"] = None
        elif score["reader"] and field not in ("control", "score"):
            score["reader"] = dict(score["reader"])
            target = score["score"] if field.startswith("sanity") else score
            parts = field.split(".")
            for part in parts[:-1]:
                target = target[part]
            target.pop(parts[-1])
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    rep = S.calibrated_report(root, plan=plan, require_plan=True)
    assert all(row["headline"]["band"] is None for t in rep["sections"][0]["tables"] for row in t["rows"])
    for markdown in (False, True):
        assert "Coherency results" in report.render(rep, markdown)


def test_absent_reader_digest_is_refused(tmp_path):
    root, plan, scores = release_inputs(tmp_path)
    for score in scores:
        if score["reader"]:
            score["reader"] = {k: v for k, v in score["reader"].items() if k != "config_sha256"}
    S.write(root, scores, {"a": full_config(), "ac": full_config()})
    with pytest.raises(report.ReportError, match="reader config_sha256 not recorded"):
        S.calibrated_report(root, plan=plan, require_plan=True)


def test_report_numbers_match_across_runtimes_and_hash_seeds(tmp_path):
    executables = runtime_interpreters()
    root, plan = varying_inputs(tmp_path)
    code = """import sys
from tests.scoring import synth as S
from cohbench.scoring import report
from cohbench.scoring.score import dumps
print(dumps(S.calibrated_report(sys.argv[1], plan=sys.argv[2], require_plan=True)), end='')
"""
    outputs = []
    for executable in executables:
        for seed in ("1", "777"):
            outputs.append(subprocess.check_output([executable, "-S", "-c", code, str(root), str(plan)],
                           cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PYTHONHASHSEED": seed}))
    assert all(output == outputs[0] for output in outputs)
