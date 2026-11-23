"""Second-round reader review regressions. No model endpoint is called."""

import copy
import json
from pathlib import Path

import pytest

from cohbench.scoring import calibration as C, reader as R, report
from . import synth as S
from .test_review6 import release_inputs

ROOT = Path(__file__).resolve().parents[2]
KIMI = ROOT / "cohbench" / "scoring" / "configs" / "explabs-kimi-k3.1-pro.json"


def build(root, plan, path):
    return report.build(root, plan=plan, require_plan=True, calibration_manifest=path)


# R2-1: a release needs every reader-bearing score to record the calibrated question source.


@pytest.mark.parametrize("source", ["opening_prompt", None, "absent"])
def test_r2_1_reader_question_source_must_match_calibration(tmp_path, source):
    root, plan, scores = release_inputs(tmp_path)
    assert not build(root, plan, S.manifest(root))["not_release"]
    target = next(s for s in scores if s["reader"])
    if source == "absent":
        del target["reader_question_source"]
    else:
        target["reader_question_source"] = source
    S.write(root, scores, _configs())
    rep = build(root, plan, S.manifest(root))
    assert rep["not_release"]
    reasons = rep["parameters"]["calibration"]["reasons"]
    assert any("question source" in r and "answer_key" in r for r in reasons), reasons
    assert all(row["headline"]["band"] is None for t in rep["sections"][0]["tables"] for row in t["rows"])


def _configs():
    from .test_review4 import full_config

    return {"a": full_config(), "ac": full_config()}


# R2-2: calibration outputs are named by config digest and never silently replaced.


def _hosted(path, seed):
    raw = json.loads(KIMI.read_text())
    raw["sampling"]["seed"] = seed
    path.write_text(json.dumps(raw))
    return path


class _Fake:
    last = None

    def __init__(self, cfg):
        self.cfg = cfg

    def choose(self, question, excerpt, shown):
        return 0

    def describe(self):
        return {"kind": "hosted", "config_sha256": self.cfg.digest}


def test_r2_2_two_configs_of_one_model_keep_separate_records(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "make", lambda cfg, *a, **k: _Fake(cfg))
    paths = [_hosted(tmp_path / "a.json", 1), _hosted(tmp_path / "b.json", 2)]
    cfgs = [R.load_config(p) for p in paths]
    assert cfgs[0].raw["expect"] == cfgs[1].raw["expect"] and cfgs[0].digest != cfgs[1].digest
    out = tmp_path / "out"
    result = C.run([], out, workers=2, no_answer_configs=paths)
    assert set(result) == {cfg.digest for cfg in cfgs}
    for cfg in cfgs:
        assert cfg.digest in C.output_name(cfg, "calibration")
        assert json.loads((out / C.output_name(cfg, "calibration")).read_text())["reader"] == cfg.digest
        assert (out / C.output_name(cfg, "items")).exists()
    assert len(list(out.glob("*.calibration.json"))) == 2


def test_r2_2_existing_record_is_refused_without_overwrite(tmp_path, monkeypatch):
    def no_reader(*a, **k):
        raise AssertionError("no reader may be built before the overwrite check")

    monkeypatch.setattr(R, "make", no_reader)
    a = _hosted(tmp_path / "a.json", 1)
    out = tmp_path / "out"
    out.mkdir()
    existing = out / C.output_name(R.load_config(a), "calibration")
    existing.write_text("{}\n")
    with pytest.raises(ValueError, match="overwrite"):
        C.run([], out, no_answer_configs=[a])
    assert existing.read_text() == "{}\n"
    with pytest.raises(ValueError, match="more than once"):
        C.run([], tmp_path / "other", no_answer_configs=[a, a])
    monkeypatch.setattr(R, "make", lambda cfg, *a, **k: _Fake(cfg))
    C.run([], out, workers=2, no_answer_configs=[a], overwrite=True)
    assert json.loads(existing.read_text())["reader"] == R.load_config(a).digest


# R2-4: a reader config with a repeated field is refused.


def test_r2_4_config_with_duplicate_field_is_refused(tmp_path):
    text = KIMI.read_text()
    path = tmp_path / "dup.json"
    path.write_text(text.replace('"kind": "hosted",', '"kind": "hosted",\n  "kind": "hosted",', 1))
    assert path.read_text() != text
    R.load_config(KIMI)
    with pytest.raises(R.ReaderError, match="repeats a field"):
        R.load_config(path)


# R2-5: a calibration record must be internally consistent.


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g["no_answer"]["per_order"].__setitem__(0, g["no_answer"]["per_order"][0] + 1),
        lambda g: g["ideal"].pop("per_order"),
        lambda g: g["worst"].update(per_order=[0] * 5),
        lambda g: g["buried"].update(per_order=[1, -1, 0, 0, 0, 0]),
        lambda g: g["ideal"].update(per_order=[61, 59, 60, 60, 60, 60]),
        lambda g: g["no_answer"]["positions"].__setitem__(0, g["no_answer"]["positions"][0] + 1),
    ],
)
def test_r2_5_inconsistent_calibration_record_is_refused(tmp_path, mutation):
    root, plan, _ = release_inputs(tmp_path)
    path = S.manifest(root)
    doc = json.loads(path.read_text())
    large = doc["reader"]["large_sample"]
    assert C.validate_record(large, large["reader"])
    bad = copy.deepcopy(large)
    mutation(bad["groups"])
    assert not C.validate_record(bad, bad["reader"])
    doc["reader"]["large_sample"] = bad
    path.write_text(json.dumps(doc))
    rep = build(root, plan, path)
    assert rep["not_release"] and any("large-sample" in r for r in rep["parameters"]["calibration"]["reasons"])


def test_r2_5_committed_kimi_record_is_consistent():
    doc = json.loads((ROOT / "calibration" / "reader" / "round1" / "kimi-k3.1-pro.calibration.json").read_text())
    assert C.validate_record(doc, R.load_config(KIMI).digest)


# R2-6: an Explanation answer key whose question is present must hold text.


@pytest.mark.parametrize("question, refused", [(None, True), ("", True), ("   ", True), (3, True), (["q"], True), ("Why?", False), ("absent", False)])
def test_r2_6_present_question_must_be_nonempty_text(question, refused):
    from cohbench.runner.task import _answer_key_errors
    from cohbench.tasks.common import Options
    from cohbench.tasks.explanation import generate

    payload = copy.deepcopy(generate(Options("explanation", 1, "h20")).task["scoring"]["answer_key"])
    if question == "absent":
        del payload["question"]
    else:
        payload["question"] = question
    errors = _answer_key_errors("explanation", payload)
    if refused:
        assert any("answer_key.payload.question" in e for e in errors), errors
    else:
        assert errors == [], errors


# Limit recorded for the owner.


def test_failed_component_headline_limit_is_documented():
    text = " ".join((ROOT / "cohbench" / "scoring" / "README.md").read_text().split())
    assert "still includes that component" in text and '"excluded from the headline"' in text
    assert "three-part geometric mean" in text and "open question for the owner" in text
