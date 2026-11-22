"""Strict acceptance checks. An upstream fault remains a test failure."""

import json
import time
import tempfile
from pathlib import Path

import pytest

from cohbench.tasks.suite import write_suite
from cohbench.trajectory import bundle

from .checks import compare_artifacts
from .run import read, run_suite, write

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def completed():
    runs = Path(__file__).resolve().parents[2] / "runs"
    runs.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="self-check-", dir=runs))
    suite = root / "suite"
    assert write_suite(suite) == 88
    outputs = []
    for name in ("first", "second"):
        started = time.perf_counter()
        out = root / name
        rows, reports = run_suite(suite, out)
        print(f"{name}: {time.perf_counter() - started:.3f} seconds; {out}")
        outputs.append((out, rows, reports))
    write(root / "determinism.json", compare_artifacts(outputs[0][0], outputs[1][0]))
    return outputs


def test_every_episode_exports_a_valid_scored_bundle(completed):
    output, rows, _ = completed[0]
    assert len(rows) == 220
    problems = []
    for row in rows:
        label = (row["instance"], row["agent"])
        if row["status"] != "usable" or row["end_reason"] != "finished" or row["errors"]:
            problems.append((label, row["status"], row["detail"], row["errors"]))
            continue
        path = output / "runs" / row["instance"] / row["agent"] / "episodes"
        episode = next(path.iterdir())
        check = bundle.validate(bundle.read(episode / "export" / "bundle"))
        if not check["ok"] or not row["scored"]:
            problems.append((label, check, row["scored"]))
    assert not problems, json.dumps(problems, indent=2)
    assert sum(r["scored"] for r in rows) == 220


@pytest.mark.parametrize("family", ["routing", "explanation", "steering"])
@pytest.mark.parametrize("horizon", ["h20", "h80"])
def test_exact_script_subscores(completed, family, horizon):
    checks = read(completed[0][0] / "checks.json")
    selected = [c for c in checks if c["family"] == family and c["horizon"] == horizon and not c["control"]]
    assert selected
    failed = [c for c in selected if not c["matches"]]
    assert not failed, json.dumps(failed, indent=2)


def test_every_control_has_the_scripted_outcome(completed):
    checks = read(completed[0][0] / "checks.json")
    controls = [c for c in checks if c["control"]]
    assert len(controls) == 90
    failed = [c for c in controls if not c["matches"]]
    assert not failed, json.dumps(failed, indent=2)


def test_report_prints_both_models_and_endpoint_headlines(completed):
    output, _, reports = completed[0]
    for label, expected_models, expected_tables in (
        ("results", {"scripted-ideal", "scripted-worst"}, {"h20", "h80"}),
        ("grace", {"scripted-ideal"}, {"h80"}),
    ):
        assert reports[label]["exit"] == 0
        text = reports[label]["text"]
        assert "model \\ harness" in text
        assert all(model in text for model in expected_models)
        report = read(output / f"{label}-report" / "report.json")
        assert len(report["sections"]) == 1
        tables = report["sections"][0]["tables"]
        assert {table["horizon"] for table in tables} == expected_tables
        for table in tables:
            assert table["mode"] == "default"
            assert {cell["model"] for cell in table["rows"]} == expected_models
            for cell in table["rows"]:
                ideal = cell["model"] == "scripted-ideal"
                assert cell["headline"]["value"] == pytest.approx(100 if ideal else 5), cell
                for family in ("R", "E", "S"):
                    assert cell[family]["value"] == int(ideal), cell
        ideal_lines = [line.lower() for line in text.splitlines() if "scripted-ideal" in line]
        assert not any(
            word in line for line in ideal_lines
            for word in ("partial", "withheld", "not comparable")
        ), ideal_lines


def test_reset_outcomes_and_handoff_diagnostics(completed):
    output, rows, _ = completed[0]
    checks = read(output / "checks.json")
    long = [check for check in checks if check["horizon"] == "h80"]
    assert len(long) == 132
    assert all(check["observed"]["reset"] == check["expected"]["reset"] for check in long)
    by_run = {(row["instance"], row["agent"]): row for row in rows}
    for check in long:
        diagnostic = check["handoff"]
        assert diagnostic is not None, check
        row = by_run[(check["instance"], check["agent"])]
        info = read(output.parent / "suite" / row["instance"] / "scripted" / "info.json")
        work = output / "runs" / row["instance"] / row["agent"] / "work"
        results = info["filler_results"]
        right = sum(
            (work / path).is_file() and (work / path).read_text() == contents
            for path, contents in results.items()
        )
        assert diagnostic["chores"] == len(results), check
        assert diagnostic["chores_right"] == right, check
        if check["agent"] != "worst":
            assert right == len(results), check


def test_two_runs_have_identical_score_and_report_bytes(completed):
    first, second = completed[0][0], completed[1][0]

    comparison = compare_artifacts(first, second)
    assert comparison["score_files"] == 220
    assert comparison["report_files"] == 6
    assert comparison["identical"], comparison
