"""Round 3 batch and calibration-tooling regressions, without hosted calls."""

import http.client
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cohbench.runner.policy import Judgement
from cohbench.user.api import Client, EndpointError
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from cohbench.user.ledger import Ledger
from .conftest import KEY, ROOT, config_doc
from .test_hosted_review2 import report_fixture, write_json


def fixture_manifest(repo, models):
    runs = []
    for model in models:
        for kind in ("turn", "awareness"):
            name = f"{model}-{kind}"
            runs.append(
                {
                    "name": name,
                    "model": model,
                    "set": "heldout",
                    "kind": kind,
                    "inputs": {
                        "root": "heldout",
                        "items": "items" if kind == "turn" else "awareness",
                        "labels": "labels-author.json"
                        if kind == "turn"
                        else "awareness-labels-author.json",
                    },
                    "verdicts": {"root": "private", "path": name + "-verdicts.json"},
                    "outputs": {
                        "root": "private",
                        "json": name + "-agreement.json",
                        "text": name + "-agreement.txt",
                    },
                    "aggregate": "calibration/classifier/heldout-run-2026-11-23/results.json",
                }
            )
    write_json(
        repo / "calibration/classifier/hosted-runs.json",
        {"version": 1, "runs": runs, "evidence": []},
    )


def development(tmp_path):
    script, held, private, public, models = report_fixture(tmp_path)
    public.unlink()  # This fixture declares development only.
    repo = script.parents[2]
    archive = repo / "calibration/classifier/dev/hosted-run-2026-11-23"
    dev = archive.parent
    shutil.copytree(held / "items", dev / "items")
    shutil.copy(held / "labels-author.json", dev / "labels.json")
    source = archive / "kimi-k3.1-pro-full-verdicts.json"
    shutil.copy(private / f"{models[0]}-turn-verdicts.json", source)
    entry = {
        "name": "kimi-k3.1-pro-full",
        "model": models[0],
        "set": "development",
        "kind": "turn",
        "inputs": {
            "root": "repo",
            "items": "calibration/classifier/dev/items",
            "labels": "calibration/classifier/dev/labels.json",
        },
        "verdicts": {"root": "repo", "path": str(source.relative_to(repo))},
        "outputs": {
            "root": "repo",
            **{
                kind: str((archive / f"kimi-k3.1-pro-full-{suffix}").relative_to(repo))
                for kind, suffix in [
                    ("json", "agreement.json"),
                    ("text", "agreement.txt"),
                    ("summary", "summary.json"),
                ]
            },
        },
    }
    manifest = repo / "calibration/classifier/hosted-runs.json"
    write_json(manifest, {"version": 1, "runs": [entry], "evidence": []})
    for path in entry["outputs"].values():
        if path != "repo":
            (repo / path).write_text('{"stale": true}\n')
    return script, manifest, source, entry, archive


def call(script, *args):
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True
    )


@pytest.mark.parametrize("fault", ["missing_verdict", "orphan_report"])
def test_r3_b4_manifest_preflight_owns_every_report(tmp_path, fault):
    script, manifest, source, entry, archive = development(tmp_path)
    if fault == "missing_verdict":
        source.unlink()
        required = source.name
    else:
        required = "unowned-agreement.json"
        (archive / required).write_text('{"old": true}\n')
    before = {p.name: p.read_bytes() for p in archive.iterdir() if p != source}
    proc = call(script)
    assert proc.returncode != 0 and required in proc.stderr
    assert {p.name: p.read_bytes() for p in archive.iterdir() if p != source} == before


def test_r3_s1_extra_id_is_not_filtered_out_of_manifest_scope(tmp_path):
    script, manifest, source, entry, archive = development(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["runs"][0]["inputs"]["ids"] = ["a"]
    write_json(manifest, doc)
    proc = call(script, "--allow-partial")
    assert proc.returncode != 0 and "b" in proc.stderr and "outside" in proc.stderr


@pytest.mark.parametrize("kind", ["turn", "awareness"])
def test_r3_s2_verdict_slot_refuses_labels_by_kind(tmp_path, kind):
    script, held, private, public, models = report_fixture(tmp_path)
    fixture_manifest(script.parents[2], models)
    shutil.copy(
        held
        / ("labels-author.json" if kind == "turn" else "awareness-labels-author.json"),
        private / f"{models[0]}-{kind}-verdicts.json",
    )
    proc = call(script, "--heldout-root", str(held), "--private-results", str(private))
    assert proc.returncode != 0 and "verdicts file" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_r3_v3_missing_input_names_relative_file_without_traceback(tmp_path):
    script, held, private, public, models = report_fixture(tmp_path)
    fixture_manifest(script.parents[2], models)
    missing = private / f"{models[0]}-turn-verdicts.json"
    missing.unlink()
    proc = call(script, "--heldout-root", str(held), "--private-results", str(private))
    assert proc.returncode != 0
    assert missing.name in proc.stderr and str(tmp_path) not in proc.stderr
    assert proc.stderr.count("\n") == 1 and "Traceback" not in proc.stderr


def test_r3_b3_submission_failure_before_transmission_is_not_sent(
    tmp_path, fake, monkeypatch
):
    cfg = parse(config_doc(fake.base_url, limits={"retries": 0}))

    def fail(*args, **kwargs):
        raise OSError("synthetic submission failure")

    monkeypatch.setattr(http.client.HTTPConnection, "request", fail)
    path = tmp_path / "calls"
    client = Client(cfg, KEY, ledger=Ledger(path, 3))
    with pytest.raises(EndpointError):
        client.choose([], ["A"], client.clock() + 1)
    client.flush()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(r["event"] == "not_sent" and r["call"] == 1 for r in rows)
    assert fake.requests == []


def test_r3_b2_limit_documents_reservation_write_crossing_cutoff():
    text = " ".join((ROOT / "cohbench/user/README.md").read_text().split())
    assert "reservation write crosses the cutoff" in text
    assert "still consumes one slot" in text


@pytest.mark.parametrize("reverse", [False, True])
def test_r3_b1_classifier_workers_share_cutoff_and_discard_late_result(reverse):
    barrier, release = threading.Barrier(3), threading.Event()
    cutoffs, snapshots = [], []

    class Controlled(Classifier):
        def judge(self, card, transcript, output, *, deadline, client):
            cutoffs.append(deadline)
            snapshots.append(transcript)
            barrier.wait(timeout=0.5)
            if output == "slow":
                release.wait(0.5)
            elif (output == "first") == reverse:
                time.sleep(0.01)
            return Judgement(
                "answerable", answer_ids=(output,), record={"output": output}
            )

    cfg = parse(config_doc())
    pol = Controlled(cfg, Client(cfg, KEY))
    try:
        results = pol.judge_batch({}, (), ["first", "slow", "last"], timeout_s=0.1)
        assert [j.answer_ids if j else None for j in results] == [
            ("first",),
            None,
            ("last",),
        ]
        assert len(set(cutoffs)) == 1 and snapshots == [()] * 3
    finally:
        release.set()
    time.sleep(0.02)
    assert results[1] is None
