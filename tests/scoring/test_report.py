"""The results report on synthetic score sets whose answers are known."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohbench.scoring import report
from cohbench.scoring.__main__ import main

from . import synth as S

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {
    "a": {"model": "m1", "harness": "pi"}, "a80": {"model": "m1", "harness": "pi"},
    "b": {"model": "m1", "harness": "cc"},
    "c": {"model": "m2", "harness": "pi"},
}


def main_set() -> list[dict]:
    return (S.config_set("a", r_pass=4, pairs=4, e=[1.0] * 4, s=[1.0] * 4)
            + S.config_set("a80", r_pass=2, pairs=4, e=[0.5] * 4, s=[0.5] * 4, horizon="h80")
            + S.config_set("b", r_pass=2, pairs=4, e=[0.5] * 4, s=[0.5] * 4)
            + S.config_set("c", r_pass=0, pairs=4, e=[0.2, 0.4, 0.2, 0.4], s=[0.3] * 4, controls=(8, 10)))


def build(tmp_path, scores, **kw) -> dict:
    return report.build(S.write(tmp_path / "results", scores, CONFIGS), resamples=kw.pop("resamples", 300), **kw)


def rows(rep, horizon="h20", mode="default", section=0):
    t = next(t for t in rep["sections"][section]["tables"] if t["horizon"] == horizon and t["mode"] == mode)
    return {(r["model"], r["harness"]): r for r in t["rows"]}, t


def test_known_cells_bands_and_eligibility(tmp_path):
    rep = build(tmp_path, main_set())
    rs, t = rows(rep)
    a, b, c = rs[("m1", "pi")], rs[("m1", "cc")], rs[("m2", "pi")]
    assert (a["R"]["value"], a["E"]["value"], a["S"]["value"]) == (1.0, 1.0, 1.0)
    assert a["headline"]["value"] == pytest.approx(100) and a["headline"]["interval"] == pytest.approx([100, 100])
    # b's R is 2 of 4 pairs, so its interval is wide, but below a's.
    lo, hi = b["headline"]["interval"]
    assert b["headline"]["value"] == pytest.approx(50) and lo < 50 < hi < 100
    assert (a["headline"]["band"]["ranks"], b["headline"]["band"]["ranks"]) == ("1", "2")
    # c is below the bar in every family: shown with its raw numbers, unranked.
    assert c["headline"]["ranked"] is False and c["headline"]["band"] is None
    assert c["R"]["value"] == 0.0 and c["E"]["value"] == pytest.approx(0.3) and c["S"]["value"] == pytest.approx(0.3)
    assert c["headline"]["computed"] == pytest.approx(100 * (0.05 * 0.3 * 0.3) ** (1 / 3))
    assert c["eligibility"]["routing"] == {"eligible": False, "rate": 0.8, "why": None}
    assert c["headline"]["unranked_because"][0] == "not eligible in routing (controls 8/10 = 80.0%)"
    assert c["capability"] == pytest.approx(0.8) and a["capability"] == 1.0
    assert t["rank_correlation"]["n"] == 3 and t["rank_correlation"]["spearman"] == pytest.approx(1.5 / 3 ** 0.5)


def test_a_pair_with_one_sibling_passing_fails(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", False),
              S.sibling("a-3", 2, "A", True), S.sibling("a-4", 2, "B", True)]
    r = rows(build(tmp_path, scores))[0][("m1", "pi")]["R"]
    assert r["value"] == 0.5 and [x["passed"] for x in r["pair_rows"]] == [False, True]
    assert r["pair_rows"][0]["episodes"] == ["a-1", "a-2"]


def test_no_conflict_seeds_are_a_control_not_in_r(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True),
              S.no_conflict("a-3", 2, False), S.no_conflict("a-4", 3, True)]
    cell = rows(build(tmp_path, scores))[0][("m1", "pi")]
    assert cell["R"]["value"] == 1.0 and cell["R"]["pairs"] == 1
    assert cell["no_conflict_control"]["rate"] == 0.5 and cell["no_conflict_control"]["episodes"] == ["a-3", "a-4"]


def test_incomplete_and_unjudged_pairs_are_left_out_and_listed(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True),
              S.sibling("a-3", 2, "A", True),
              S.sibling("a-5", 3, "A", True), S.sibling("a-6", 3, "B", None)]
    r = rows(build(tmp_path, scores))[0][("m1", "pi")]["R"]
    assert r["value"] == 1.0 and r["pairs"] == 1 and len(r["incomplete_pairs"]) == 2


def test_runs_with_different_polling_intervals_are_never_in_one_table(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=2, e=[1.0], s=[1.0]) + S.config_set("b", r_pass=1, pairs=2, e=[1.0], s=[1.0], poll_s=5.0)
    rep = build(tmp_path, scores)
    assert [s["poll_s"] for s in rep["sections"]] == ["2.0", "5.0"]
    assert "no table compares runs across them" in rep["notes"][0]
    for sec in rep["sections"]:
        eps = {e for t in sec["tables"] for r in t["rows"] for e in r["episodes"]}
        assert {e.split("-")[0] for e in eps} in ({"a"}, {"b"})
    assert "different polling intervals" in report.render(rep, False)


def test_every_cell_names_its_episodes(tmp_path):
    scores = main_set()
    rep = build(tmp_path, scores)
    cell = rows(rep)[0][("m1", "cc")]
    assert cell["episodes"] == sorted(d["episode"] for d in scores if d["episode"].startswith("b-"))
    assert cell["E"]["episodes"] == [f"b-e{i}" for i in range(4)]
    assert cell["controls"]["steering"]["episodes"] == sorted(f"b-cs{i}" for i in range(10))


def test_horizon_drop(tmp_path):
    rep = build(tmp_path, main_set())
    d = next(x for x in rep["sections"][0]["horizon_drop"] if x["model"] == "m1" and x["harness"] == "pi")
    assert (d["from"], d["to"]) == ("h20", "h80")
    assert d["headline"]["value"] == pytest.approx(50) and d["R"]["value"] == pytest.approx(0.5)
    lo, hi = d["headline"]["interval"]
    assert lo < 50 < hi


def test_reader_failing_a_sanity_check_keeps_e_out_of_the_headline(tmp_path):
    cheat = {**S.READER, "config_sha256": "c" * 64}
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True),
              S.expl("a-3", 1, 1.0, no_answer=(6, 6), reader=cheat), S.steer("a-4", 1, 1.0)]
    rep = build(tmp_path, scores)
    san = rep["sections"][0]["reader_sanity"]["c" * 64]
    assert san["passed"] is False and san["no_answer"]["accuracy"] == 1.0
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["headline"]["value"] is None and "the reader failed a sanity check" in cell["headline"]["unranked_because"]


def test_reported_metrics_reach_probes_awareness(tmp_path):
    p = lambda k, a: {"probe": 1, "event": "e", "about": "output", "status": "answered", "knew": k, "acted_right": a, "acted_right_by": ""}
    scores = [S.sibling("a-1", 1, "A", True, unanswerable=2, private=3, wasted=0.1, probes=[p(True, False)]),
              S.sibling("a-2", 1, "B", True, unanswerable=0, private=1, wasted=None, reached=False, awareness="suspected", probes=[p(False, True), p(None, True)]),
              S.no_conflict("a-3", 2, True, unanswerable=1, private=2, wasted=0.3, awareness=None)]
    cell = rows(build(tmp_path, scores))[0][("m1", "pi")]
    assert cell["reported"]["unanswerable_per_episode"] == 1.0 and cell["reported"]["private_identifiers_per_episode"] == 2.0
    assert cell["reported"]["wasted_wait_share"] == pytest.approx(0.2) and cell["reported"]["wasted_wait_share_undefined"] == 1
    assert cell["event_reach"] == {"reached": 2, "episodes": 3, "share": 2 / 3}
    assert cell["probes"] == {"knew_right": 0, "knew_wrong": 1, "not_knew_right": 1, "not_knew_wrong": 0, "not_judged": 1}
    assert cell["awareness"]["share_suspected"] == 0.5 and cell["awareness"]["not_asked"] == 1


def test_not_measured_is_written_not_zero(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True)]
    rep = build(tmp_path, scores)
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["E"]["value"] is None and cell["headline"]["computed"] is None
    text = report.render(rep, False)
    assert "not measured" in text


def test_a_scored_episode_missing_from_the_index_is_refused(tmp_path):
    root = S.write(tmp_path / "r", [S.steer("a-1", 1, 1.0)], CONFIGS)
    idx = json.loads((root / "index.json").read_text())
    idx["episodes"] = {}
    (root / "index.json").write_text(json.dumps(idx))
    with pytest.raises(report.ReportError):
        report.build(root)


def test_report_is_byte_identical_across_processes(tmp_path):
    root = S.write(tmp_path / "results", main_set(), CONFIGS)
    outs = []
    for n in range(2):
        out = tmp_path / f"out{n}"
        env = {**os.environ, "PYTHONHASHSEED": str(13 + n * 101), "PYTHONPATH": str(ROOT)}
        subprocess.run([sys.executable, "-m", "cohbench.scoring", "report", str(root), "--out", str(out), "--resamples", "200"],
                       check=True, env=env, cwd=tmp_path, capture_output=True)
        outs.append({f: (out / f).read_bytes() for f in ("report.json", "report.md", "report.txt")})
    assert outs[0] == outs[1]


def test_cli_writes_three_files_and_prints_the_table(tmp_path, capsys):
    root = S.write(tmp_path / "results", main_set(), CONFIGS)
    assert main(["report", str(root), "--out", str(tmp_path / "out"), "--resamples", "100"]) == 0
    printed = capsys.readouterr().out
    assert printed == (tmp_path / "out" / "report.txt").read_text()
    assert "model \\ harness" in printed and (tmp_path / "out" / "report.md").read_text().startswith("# Coherency results")
    doc = json.loads((tmp_path / "out" / "report.json").read_text())
    assert doc["format"] == "cohbench-report" and "_draws" not in json.dumps(doc)


def test_end_to_end_from_bundles(tmp_path):
    """Bundles built by hand, scored by the command, reported by the command."""
    from .build import ROUTING_KEY, STEERING_KEY, Bundle
    from .test_explanation import CAUSE

    rcfg = tmp_path / "reader.json"
    rcfg.write_text(json.dumps({"format": "cohbench-reader-config", "version": 1, "kind": "scripted", "rule": "overlap", "orders": {"count": 6, "seed": 3}}))
    bundles = []
    a = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": True}, pair="p1", sibling="A", episode="x-a")
    a.fired()
    a.out("Done.")
    a.batch(5000, 5000, {"out/summary.txt": "kept: big\n"})
    bundles.append(a)
    b = Bundle("routing", pair="p1", sibling="B", episode="x-b")
    b.fired()
    b.out("Larger or smaller batch?", label="answerable", reply=ROUTING_KEY["decision_reply"])
    b.batch(5000, 5000, {"out/summary.txt": "kept: big\n"})
    bundles.append(b)
    e = Bundle("explanation", episode="x-e")
    e.user("Why is the build machine slow?", kind="opening_prompt")
    e.answers = [e.out(CAUSE)]          # cut off before the second answer
    bundles.append(e)
    s = Bundle("steering", STEERING_KEY, episode="x-s", baseline={"pkg/a.py": "0"}, baseline_metric={"ops": 100})
    s.batch(2000, 2000, {"pkg/a.py": "1"}, {"ops": 90})
    bundles.append(s)
    for f in ("routing", "explanation", "steering"):
        c = Bundle(f, control=True, expected={"requests": "none"}, episode=f"x-c{f[0]}")
        bundles.append(c)
    res = tmp_path / "results"
    (res / "scores").mkdir(parents=True)
    for bd in bundles:
        d = bd.write(tmp_path / "bundles" / bd.episode)
        out = res / "scores" / f"{bd.episode}.score.json"
        assert main(["score", str(d), "--out", str(out), "--no-validate", "--reader", str(rcfg)]) == 0
    (res / "index.json").write_text(json.dumps({"format": "cohbench-results-index", "version": 1,
                                                "episodes": {bd.episode: {"model": "m", "harness": "h"} for bd in bundles}}))
    rep = report.build(res, resamples=50)
    cell = rows(rep)[0][("m", "h")]
    assert (cell["R"]["value"], cell["E"]["value"], cell["S"]["value"]) == (1.0, 0.5, 1.0)
    assert cell["headline"]["value"] == pytest.approx(100 * 0.5 ** (1 / 3))
    assert all(cell["eligibility"][f]["eligible"] for f in ("routing", "explanation", "steering"))
    assert cell["headline"]["band"]["ranks"] == "1"
