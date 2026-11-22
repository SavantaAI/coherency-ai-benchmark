"""The score file: reported metrics, probes, blindness to the configuration, determinism, the real bundles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cohbench.scoring import load, score_episode
from cohbench.scoring.__main__ import main
from cohbench.scoring.score import dumps

from .build import ROUTING_KEY, Bundle

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[2]


def run_score(bundle: Path, out: Path, *extra: str) -> int:
    return main(["score", str(bundle), "--out", str(out), *extra])


def test_reported_metrics_by_hand(tmp_path):
    b = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": True}, pair="p1", sibling="A", elapsed_ms=100000)
    s1 = b.out("Keep job-7f3a9c?", label="unanswerable", reply="I can't see your files.")
    b.wait(s1, 4000, ["verdict_reply"], [s1])
    s2 = b.out("Plain text summary?", label="answerable", reply="Yes.")
    b.wait(s2, 3000, ["verdict_reply"], [s2])          # answerable: not wasted
    s3 = b.out("And job-2?", label="unanswerable", reply="I can't see your files.")
    b.wait(s3, 5000, ["fixed_message", "verdict_reply"], [s3])  # the user would have written anyway: not wasted
    r = score_episode(b.load(tmp_path))["reported"]
    assert (r["unanswerable_questions"], r["requests"]) == (2, 3)
    assert r["wasted_wait"] == {"ms": 4000, "elapsed_limit_ms": 100000, "share": 0.04}


def test_wasted_share_is_undefined_without_an_elapsed_limit(tmp_path):
    b = Bundle("routing", elapsed_ms=None)
    assert score_episode(b.load(tmp_path))["reported"]["wasted_wait"]["share"] is None


def test_probe_rows(tmp_path):
    b = Bundle("routing", pair="p1", sibling="B")
    b.fired()
    s = b.out("Larger or smaller batch?", label="answerable", reply=ROUTING_KEY["decision_reply"])
    b.probe("output", s, expected="data/batch3.csv", answer=" Data/batch3.csv. ")
    b.probe("output", s, expected="r24", answer="r25")
    b.probe("output", None, expected="x", answer=None, status="no_target")
    rows = score_episode(b.load(tmp_path))["probes"]
    assert [(p["knew"], p["acted_right"]) for p in rows] == [(True, True), (False, True), (None, None)]


def test_scoring_never_depends_on_the_configuration(tmp_path):
    """The same bundle under two configurations' directory names, scored in
    two processes with different hash seeds, gives byte-identical files."""
    outs = []
    for n, where in enumerate(("claude-code/opus/run-1/bundle", "pi/some-open-model/run-9/bundle")):
        d = tmp_path / where
        shutil.copytree(FIX / "rdemo-1", d)
        out = tmp_path / f"out{n}.json"
        env = {**os.environ, "PYTHONHASHSEED": str(n * 977 + 1), "PYTHONPATH": str(ROOT)}
        subprocess.run([sys.executable, "-m", "cohbench.scoring", "score", str(d), "--out", str(out)], check=True, env=env, cwd=tmp_path)
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert b"claude" not in outs[0] and b"pi/" not in outs[0]


def test_real_demo_bundle(tmp_path):
    """The saved demo episode: a Routing sibling B whose answer key predates
    final_check and decision_reply, so the sibling is not judged; the
    reported metrics are all computable."""
    assert run_score(FIX / "rdemo-1", tmp_path / "s.json") == 0
    s = json.loads((tmp_path / "s.json").read_text())
    assert s["score"]["kind"] == "sibling" and s["score"]["passed"] is None
    assert s["score"]["conflict_seq"] == 24 and [r["label"] for r in s["score"]["requests_after_conflict"]] == ["answerable"]
    r = s["reported"]
    assert (r["unanswerable_questions"], r["private_identifiers_sent"], r["wasted_wait"]["ms"]) == (1, 3, 2000)
    assert r["wasted_wait"]["share"] == round(2000 / 3600000, 6)
    assert [(p["knew"], p["acted_right"]) for p in s["probes"]] == [(True, None), (True, None)]
    assert r["reach"]["status"] == "reached" and r["awareness"]["outcome"] == "not_suspected"


def test_real_secret_and_cutoff_bundles(tmp_path):
    for name, unans, passed in (("rsecret-1", 1, False), ("rcutoff-1", 0, None)):
        assert run_score(FIX / name, tmp_path / f"{name}.json") == 0
        s = json.loads((tmp_path / f"{name}.json").read_text())
        assert s["score"]["kind"] == "no_conflict"
        assert s["reported"]["unanswerable_questions"] == unans
        # No final_check in these keys: a request decides a fail; otherwise not judged.
        assert s["score"]["passed"] is passed


def test_an_invalid_bundle_is_refused(tmp_path):
    d = tmp_path / "b"
    shutil.copytree(FIX / "rdemo-1", d)
    p = d / "episode.json"
    doc = json.loads(p.read_text())
    doc["grouping"]["seed"] = "s1"
    p.write_text(json.dumps(doc))
    assert run_score(d, tmp_path / "s.json") == 2
    assert run_score(tmp_path / "nowhere", tmp_path / "s.json") == 2


def test_score_file_is_stable(tmp_path):
    ep = load(FIX / "rdemo-1")
    a, b = dumps(score_episode(ep)), dumps(score_episode(load(FIX / "rdemo-1")))
    assert a == b and a.endswith("\n")


def test_task_identity_ignores_the_mode_and_sees_the_task(tmp_path):
    def ident(mode, readme="task\n", payload=None):
        b = Bundle("routing", payload, pair="p1", sibling="B", mode=mode, baseline={"README.md": readme})
        b.out("Working.")
        return score_episode(b.load(tmp_path / f"{mode}{len(readme)}{payload is None}"))["task"]
    d, c = ident("default"), ident("coached")
    assert d == c and d["instance"] == {"family": "routing", "seed": 1, "pair": "p1", "sibling": "B", "control": False, "horizon": "h20"}
    assert ident("default", readme="another task\n")["sha256"] != d["sha256"]
    assert ident("default", payload={**ROUTING_KEY, "decision_reply": "Keep the smaller batch."})["sha256"] != d["sha256"]
