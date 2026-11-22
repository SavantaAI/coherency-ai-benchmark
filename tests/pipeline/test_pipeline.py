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
    assert reports["results"]["exit"] == 0
    text = reports["results"]["text"]
    assert "model \\ harness" in text
    assert "scripted-ideal" in text and "scripted-worst" in text
    report = read(output / "results-report" / "report.json")
    failures = []
    for section in report["sections"]:
        for table in section["tables"]:
            for cell in table["rows"]:
                model = cell["model"]
                want = 100 if model == "scripted-ideal" else 5
                if cell["headline"]["value"] != pytest.approx(want):
                    failures.append((table["horizon"], model, want, cell["headline"]))
    assert not failures, failures
    assert "partial" not in text.lower()
    ideal_lines = [line for line in text.splitlines() if "scripted-ideal" in line]
    assert not any("not comparable" in line for line in ideal_lines)
    assert reports["grace"]["exit"] == 0


def test_two_runs_have_identical_score_and_report_bytes(completed):
    first, second = completed[0][0], completed[1][0]

    comparison = compare_artifacts(first, second)
    assert comparison["score_files"] > 0
    assert comparison["identical"], comparison
