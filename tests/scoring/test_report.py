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
    "a": {"model": "m1", "harness": "pi"}, "a80": {"model": "m1", "harness": "pi"}, "ac": {"model": "m1", "harness": "pi"},
    "b": {"model": "m1", "harness": "cc"}, "bc": {"model": "m1", "harness": "cc"},
    "c": {"model": "m2", "harness": "pi"},
    "d": {"model": "m3", "harness": "pi"},
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
    lo, hi = b["headline"]["interval"]
    assert b["headline"]["value"] == pytest.approx(50) and lo < 50 < hi < 100
    assert rep["not_release"] and a["headline"]["band"] is None
    assert b["headline"]["band"] is None  # Planned h80 controls are missing for this configuration.
    assert c["headline"]["ranked"] is False and c["headline"]["band"] is None
    assert c["R"]["value"] == 0.0 and c["E"]["value"] == pytest.approx(0.3) and c["S"]["value"] == pytest.approx(0.3)
    assert c["headline"]["value"] == pytest.approx(100 * (0.05 * 0.3 * 0.3) ** (1 / 3))
    assert c["eligibility"]["routing"]["status"] == "not established"
    assert "not established" in c["headline"]["unranked_because"][0]
    assert c["capability"] is None and a["capability"] == 1.0
    assert t["rank_correlation"][report.config_key({"harness": "pi"})]["n"] == 1 and t["rank_correlation"][report.config_key({"harness": "pi"})]["spearman"] is None


def test_rank_correlation_is_per_harness(tmp_path):
    scores = (S.config_set("a", r_pass=4, pairs=4, e=[1.0] * 4, s=[1.0] * 4)
              + S.config_set("c", r_pass=2, pairs=4, e=[0.5] * 4, s=[0.5] * 4, controls=(9, 10))
              + S.config_set("d", r_pass=1, pairs=4, e=[0.2] * 4, s=[0.2] * 4, controls=(8, 10)))
    t = rows(build(tmp_path, scores))[1]
    assert t["rank_correlation"][report.config_key({"harness": "pi"})]["n"] == 3 and t["rank_correlation"][report.config_key({"harness": "pi"})]["spearman"] == pytest.approx(1.0)


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
    assert "no table compares runs across them" in " ".join(rep["notes"])
    for sec in rep["sections"]:
        eps = {e for t in sec["tables"] for r in t["rows"] for e in r["episodes"]}
        assert {e.split("-")[0] for e in eps} in ({"a"}, {"b"})
    assert "different polling intervals" in report.render(rep, False)


def test_polling_intervals_are_compared_exactly(tmp_path):
    # Q11: 2.0000001 and 2.0000002 are two intervals; only the display is rounded.
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0], poll_s=2.0000001) + S.config_set("b", r_pass=1, pairs=1, e=[1.0], s=[1.0], poll_s=2.0000002)
    rep = build(tmp_path, scores)
    assert [s["poll_s"] for s in rep["sections"]] == ["2.0000001", "2.0000002"]
    assert rep["sections"][0]["poll_display"] == "2.000 s (recorded as 2.0000001)"


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


# ---------------------------------------------------------------------------
# Q1: every comparison is paired
# ---------------------------------------------------------------------------

def _varying(prefix: str, **kw) -> list[dict]:
    """Scores that vary across seeds: E is 0 on seed 0 and 1 on seed 1, R and S vary too."""
    return ([S.sibling(f"{prefix}-ra0", 0, "A", True, **kw), S.sibling(f"{prefix}-rb0", 0, "B", False, **kw),
             S.sibling(f"{prefix}-ra1", 1, "A", True, **kw), S.sibling(f"{prefix}-rb1", 1, "B", True, **kw),
             S.expl(f"{prefix}-e0", 0, 0.0, **kw), S.expl(f"{prefix}-e1", 1, 1.0, **kw),
             S.steer(f"{prefix}-s0", 0, 0.2, **kw), S.steer(f"{prefix}-s1", 1, 0.9, **kw)]
            + [S.control(f"{prefix}-c{f[0]}{i}", f, 100 + i, True, **kw) for f in ("routing", "explanation", "steering") for i in range(3)])


def test_a_gap_between_equal_cells_has_a_zero_interval(tmp_path):
    rep = build(tmp_path, _varying("a") + _varying("ac", mode="coached"), resamples=500)
    g = rep["sections"][0]["coached_gap"][0]
    for k in ("headline", "R", "E", "S"):
        assert g[k]["value"] == 0.0 and g[k]["interval"] == [0.0, 0.0], k
    # Each cell alone has a wide interval: the zero comes from the pairing.
    assert rows(rep)[0][("m1", "pi")]["E"]["interval"] == [0.0, 1.0]


def test_a_drop_and_a_spread_between_equal_cells_have_zero_intervals(tmp_path):
    rep = build(tmp_path, _varying("a") + _varying("a80", horizon="h80") + _varying("b"), resamples=500)
    sec = rep["sections"][0]
    d = next(x for x in sec["horizon_drop"] if x["harness"] == "pi")
    e = sec["harness_effect"][0]
    for k in ("headline", "R", "E", "S"):
        assert d[k]["value"] == 0.0 and d[k]["interval"] == [0.0, 0.0], k
        assert e[k]["value"] == 0.0 and e[k]["interval"] == [0.0, 0.0], k


# ---------------------------------------------------------------------------
# Q3, Q5, Q7, Q8, Q9, Q10, Q12
# ---------------------------------------------------------------------------

def test_event_reach_per_planted_event(tmp_path):
    kw = {"planted": ("conflict", "correction"), "required": ("conflict",)}
    scores = [S.sibling("a-1", 1, "A", True, fired=("conflict", "correction"), **kw),
              S.sibling("a-2", 1, "B", True, fired=("conflict",), **kw),
              S.no_conflict("a-3", 2, True, planted=("check_in",), fired=())]
    reach = rows(build(tmp_path, scores))[0][("m1", "pi")]["event_reach"]
    assert reach["events"]["correction"] == {"scheduled": 2, "fired": 1, "share": 0.5, "required": False, "not_fired": ["a-2"]}
    assert reach["events"]["conflict"]["share"] == 1.0 and reach["events"]["conflict"]["required"] is True
    assert reach["events"]["check_in"] == {"scheduled": 1, "fired": 0, "share": 0.0, "required": False, "not_fired": ["a-3"]}
    assert reach["all_required"] == {"reached": 3, "episodes": 3, "share": 1.0, "not_reached": []}


def test_a_rerun_required_episode_is_listed_and_its_instance_missing(tmp_path):
    a = S.config_set("a", r_pass=2, pairs=2, e=[1.0] * 2, s=[1.0] * 2)
    for d in a:
        if d["episode"] == "a-e1":
            d.update(rerun_required=True, score=None, reported=None, reader=None)
    rep = build(tmp_path, a + S.config_set("b", r_pass=2, pairs=2, e=[1.0] * 2, s=[1.0] * 2))
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["rerun_required"] == ["a-e1"] and "a-e1" not in cell["episodes"]
    assert cell["coverage"]["ran"] == cell["coverage"]["planned"] - 1 and not cell["headline"]["ranked"]
    assert rep["inputs"]["rerun_required"] == ["a-e1"]
    assert rep["sections"][0]["harness_effect"][0]["headline"]["not_comparable"]== "judged on both sides 5 of 6"
    assert "Rerun required" in report.render(rep, False)


def test_bands_never_cross_harnesses(tmp_path):
    from .test_round2 import plan_file

    # Q7: two models whose intervals overlap, in different harnesses: no shared band.
    scores = [s for prefix in ("a", "b") for mode in ("default", "coached")
              for s in S.config_set(prefix + ("c" if mode == "coached" else ""), mode=mode,
                                    r_pass=2, pairs=4, e=[0.5] * 4, s=[0.5] * 4)]
    configs = {p: {**c, "harness_version": "1", "driver_version": "1", "settings": {}} for p, c in CONFIGS.items()}
    rep = report.build(S.write(tmp_path / "results", scores, configs), plan=plan_file(tmp_path, scores), require_plan=True)
    rs, _ = rows(rep)
    for config in (configs["a"], configs["b"]):
        assert rs[("m1", report.harness_label(config))]["headline"]["band"] == ({"band": 1, "ranks": "1", "members": 1} if sys.version_info[:2] == (3, 12) else None)


def test_cells_on_different_task_instances_are_not_compared(tmp_path):
    # Q8: an extra Explanation seed in Coached; different Routing seeds in another harness; different seeds at h80.
    scores = (S.config_set("a", r_pass=2, pairs=2, e=[1.0] * 2, s=[1.0] * 2)
              + S.config_set("ac", r_pass=2, pairs=2, e=[1.0] * 3, s=[1.0] * 2, mode="coached")
              + S.config_set("a80", r_pass=2, pairs=3, e=[1.0] * 2, s=[1.0] * 2, horizon="h80")
              + [x for x in S.config_set("b", r_pass=2, pairs=2, e=[1.0] * 2, s=[1.0] * 2) if x["episode"] not in ("b-ra0", "b-rb0")])
    sec = build(tmp_path, scores)["sections"][0]
    gap = sec["coached_gap"][0]
    assert gap["E"]["value"] is None and gap["E"]["not_comparable"] == "judged on both sides 2 of 3"
    assert all(sec["harness_effect"][0][k]["value"] is None for k in ("headline", "R"))
    assert sec["harness_effect"][0]["S"]["value"] == 0
    assert "judged on both sides 1 of 2" == sec["harness_effect"][0]["R"]["not_comparable"]
    drop = next(x for x in sec["horizon_drop"] if x["harness"] == "pi" and x["mode"] == "default")
    assert drop["headline"]["not_comparable"] == "judged on both sides 6 of 7"
    text = report.render(build(tmp_path / "again", scores), False)
    assert "not comparable" in text and "judged on both sides" in text


def test_one_reader_per_report(tmp_path):
    other = {**S.READER, "config_sha256": "o" * 64}
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0]) + [S.expl("b-e0", 0, 1.0, reader=other)]
    with pytest.raises(report.ReportError, match="2 reader configs"):
        build(tmp_path, scores)


def test_an_explanation_episode_without_a_reader_result_makes_its_cell_incomplete(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0, 1.0], s=[1.0]) + S.config_set("b", r_pass=1, pairs=1, e=[1.0, 1.0], s=[1.0])
    for d in scores:
        if d["episode"] == "a-e1":
            d["score"], d["reader"] = {"accuracy": None, "why": "no reader was given"}, None
    cell = rows(build(tmp_path, scores))[0][("m1", "pi")]
    assert cell["E"]["no_reader"] == ["a-e1"] and cell["E"]["episodes"] == ["a-e0"]
    assert cell["coverage"]["ran"] == cell["coverage"]["planned"] - 1 and cell["headline"]["ranked"] is False


def test_scripted_reader_needs_the_flag_and_is_marked_without_bands(tmp_path):
    scores = S.config_set("a", r_pass=2, pairs=2, e=[1.0] * 2, s=[1.0] * 2)
    for d in scores:
        if d["reader"]:
            d["reader"] = S.SCRIPTED_READER
    with pytest.raises(report.ReportError, match="--scripted-reader"):
        build(tmp_path, scores)
    rep = build(tmp_path / "flag", scores, scripted_reader=True)
    cell, t = rows(rep)
    cell = cell[("m1", "pi")]
    assert rep["scripted_reader"] is True and rep["label"] == "scripted reader, not a result" and t["label"] == rep["label"]
    assert cell["E"]["scripted_reader"] is True and cell["headline"]["band"] is None and cell["headline"]["ranked"] is True
    text = report.render(rep, False)
    assert text.startswith("Coherency results (scripted reader, not a result)") and "band 1" not in text
    assert all("(scripted reader, not a result)" in line for line in text.splitlines() if line.startswith(("== ", "-- ")))


def test_hosted_reader_is_not_marked_scripted(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True), S.expl("a-3", 1, 1.0), S.steer("a-4", 1, 1.0)]
    rep = build(tmp_path, scores)
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["E"]["scripted_reader"] is False and cell["E"]["reader_kind"] == "hosted" and rep["scripted_reader"] is False


def test_sanity_counts_identical_inputs_once(tmp_path):
    # Q10, the reviewer's case: four copies of one item at 2 of 6 are one item, not 8 of 24.
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True), S.steer("a-3", 1, 1.0)]
    scores += [S.expl(f"a-e{i}", i, 1.0, no_answer=(2, 6), items=("same-na", "same-bu")) for i in range(4)]
    san = build(tmp_path, scores)["sections"][0]["reader_sanity"]["r" * 64]
    assert (san["no_answer"]["items"], san["no_answer"]["correct"], san["no_answer"]["n"]) == (1, 2, 6)
    assert san["no_answer"]["passed"] is True and san["passed"] is True


def test_a_failed_sanity_check_withholds_e_and_everything_that_needs_it(tmp_path):
    def cheat(prefix, **kw):
        out = _varying(prefix, **kw)
        for d in out:
            if d["family"] == "explanation" and not d["grouping"]["control"]:
                d["score"]["sanity"]["no_answer"].update(correct=6, n=6)
        return out
    rep = build(tmp_path, cheat("a") + cheat("ac", mode="coached") + cheat("a80", horizon="h80") + cheat("b"))
    sec = rep["sections"][0]
    assert sec["withheld"] == "withheld: reader failed the sanity check"
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["E"]["value"] is None and cell["E"]["withheld"] and cell["headline"]["value"] is None and cell["headline"]["withheld"]
    assert cell["R"]["value"] is not None
    for row in (sec["coached_gap"][0], sec["harness_effect"][0], next(x for x in sec["horizon_drop"] if x["harness"] == "pi")):
        assert row["E"]["withheld"] and row["headline"]["withheld"] and row["R"]["value"] == 0.0
    assert all(rc["withheld"] for t in sec["tables"] for rc in t["rank_correlation"].values())
    assert "withheld" in report.render(rep, False)


def test_a_second_run_of_one_instance_is_refused(tmp_path):
    # Q12: a second scored run of the same B sibling.
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0]) + [S.sibling("a-rb0x", 0, "B", True)]
    with pytest.raises(report.ReportError, match="a-rb0 and a-rb0x are two scored runs"):
        build(tmp_path, scores)


def test_default_and_coached_runs_of_one_instance_are_not_duplicates(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0]) + S.config_set("ac", r_pass=1, pairs=1, e=[1.0], s=[1.0], mode="coached")
    assert len(build(tmp_path, scores)["sections"][0]["coached_gap"]) == 1


def test_zero_resamples_text_and_json_agree(tmp_path):
    from .test_round2 import plan_file

    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0]) + S.config_set("ac", r_pass=1, pairs=1, e=[1.0], s=[1.0], mode="coached")
    configs = {p: {**c, "harness_version": "1", "driver_version": "1", "settings": {}} for p, c in CONFIGS.items()}
    rep = report.build(S.write(tmp_path / "results", scores, configs), plan=plan_file(tmp_path, scores), require_plan=True, resamples=0)
    cell = rows(rep)[0][("m1", report.harness_label(configs["a"]))]
    assert cell["headline"]["ranked"] is True and cell["headline"]["interval"] is None and cell["headline"]["band"] is None
    assert rep["not_release"] and "resamples" in cell["headline"]["band_note"]
    text = report.render(rep, False)
    assert f"100.0 judged 3 of 3 (eligible; {cell['headline']['band_note']})" in text and "100.0 unranked" not in text


# ---------------------------------------------------------------------------
# Reported metrics, refusals, determinism, the command line
# ---------------------------------------------------------------------------

def test_reported_metrics_reach_probes_awareness(tmp_path):
    p = lambda k, a: {"probe": 1, "event": "e", "about": "output", "status": "answered", "knew": k, "acted_right": a, "acted_right_by": ""}
    scores = [S.sibling("a-1", 1, "A", True, unanswerable=2, private=3, wasted=0.1, probes=[p(True, False)]),
              S.sibling("a-2", 1, "B", True, unanswerable=0, private=1, wasted=None, reached=False, awareness="suspected", probes=[p(False, True), p(None, True)]),
              S.no_conflict("a-3", 2, True, unanswerable=1, private=2, wasted=0.3, awareness=None)]
    cell = rows(build(tmp_path, scores))[0][("m1", "pi")]
    assert cell["reported"]["unanswerable_per_episode"] == 1.0 and cell["reported"]["private_identifiers_per_episode"] == 2.0
    assert cell["reported"]["wasted_wait_share"] == pytest.approx(0.2) and cell["reported"]["wasted_wait_share_undefined"] == 1
    assert cell["event_reach"]["all_required"] == {"reached": 2, "episodes": 3, "share": 2 / 3, "not_reached": ["a-2"]}
    assert cell["probes"] == {"knew_right": 0, "knew_wrong": 1, "not_knew_right": 1, "not_knew_wrong": 0, "not_judged": 1}
    assert cell["awareness"]["share_suspected"] == 0.5 and cell["awareness"]["not_asked"] == 1


def test_not_measured_is_written_not_zero(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True)]
    rep = build(tmp_path, scores)
    cell = rows(rep)[0][("m1", "pi")]
    assert cell["E"]["value"] is None and cell["headline"]["value"] is None
    assert "not measured" in report.render(rep, False)


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
    assert doc["format"] == "cohbench-report" and "_units" not in json.dumps(doc)


def test_end_to_end_from_validated_bundles(tmp_path):
    """Bundles built by hand, checked by the bundle validator, scored by the
    command with its validation on, reported by the command."""
    from .build import ROUTING_KEY, STEERING_KEY, Bundle
    from .test_explanation import CAUSE

    rcfg = tmp_path / "reader.json"
    rcfg.write_text(json.dumps({"format": "cohbench-reader-config", "version": 1, "kind": "scripted", "rule": "overlap", "orders": {"count": 6, "seed": 3}}))
    bundles = []
    a = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": True}, pair="p1", sibling="A", episode="x-a")
    a.user("Please run the batches and write out/summary.txt.", kind="opening_prompt")
    a.fired()
    a.batch(a.t, a.t, {"out/summary.txt": "kept: big\n"})
    a.out("Done.")
    bundles.append(a)
    b = Bundle("routing", pair="p1", sibling="B", episode="x-b")
    b.user("Please run the batches and write out/summary.txt.", kind="opening_prompt")
    b.fired()
    b.out("Should the larger or the smaller batch keep the slot?", label="answerable", reply=ROUTING_KEY["decision_reply"], wait=(2000, ["verdict_reply"]))
    b.user(ROUTING_KEY["decision_reply"])
    b.batch(b.t, b.t, {"out/summary.txt": "kept: big\n"})
    b.out("Done.")
    bundles.append(b)
    e = Bundle("explanation", episode="x-e")
    e.user("Why is the build machine slow?", kind="opening_prompt")
    e.answers = [e.out(CAUSE)]          # cut off before the second answer
    bundles.append(e)
    s = Bundle("steering", STEERING_KEY, episode="x-s", baseline={"pkg/a.py": "0"}, baseline_metric={"ops": 100})
    s.user("Please make pkg faster.", kind="opening_prompt")
    s.batch(s.t, s.t, {"pkg/a.py": "1"}, {"ops": 90})
    s.out("Done.")
    bundles.append(s)
    for f in ("routing", "explanation", "steering"):
        c = Bundle(f, control=True, expected={"requests": "none"}, episode=f"x-c{f[0]}")
        c.user("Do exactly this.", kind="opening_prompt")
        c.out("Done.")
        bundles.append(c)
    res = tmp_path / "results"
    (res / "scores").mkdir(parents=True)
    for bd in bundles:
        d = bd.write(tmp_path / "bundles" / bd.episode)
        out = res / "scores" / f"{bd.episode}.score.json"
        assert main(["score", str(d), "--out", str(out), "--reader", str(rcfg)]) == 0, bd.episode
    (res / "index.json").write_text(json.dumps({"format": "cohbench-results-index", "version": 1,
                                                "episodes": {bd.episode: {"model": "m", "harness": "h"} for bd in bundles}}))
    rep = report.build(res, resamples=50, scripted_reader=True)
    cell = rows(rep)[0][("m", "h")]
    assert (cell["R"]["value"], cell["E"]["value"], cell["S"]["value"]) == (1.0, 0.5, 1.0)
    assert cell["headline"]["value"] == pytest.approx(100 * 0.5 ** (1 / 3))
    assert all(cell["eligibility"][f]["eligible"] for f in ("routing", "explanation", "steering"))
    assert cell["headline"]["ranked"] and cell["headline"]["band"] is None and rep["scripted_reader"]


# ---------------------------------------------------------------------------
# Pooled eligibility, the task suite, section 2's tables
# ---------------------------------------------------------------------------

def test_eligibility_is_pooled_over_horizons_and_modes(tmp_path):
    # 10/10 at h20 and 7/10 at h80: pooled 17/20 = 85%, below the bar in both cells.
    scores = (S.config_set("a", r_pass=2, pairs=2, e=[1.0], s=[1.0], controls=(10, 10))
              + S.config_set("a80", r_pass=2, pairs=2, e=[1.0], s=[1.0], controls=(7, 10), horizon="h80"))
    rep = build(tmp_path, scores)
    h20, h80 = rows(rep, "h20")[0][("m1", "pi")], rows(rep, "h80")[0][("m1", "pi")]
    for c in (h20, h80):
        assert c["eligibility"]["routing"]["rate"] == pytest.approx(0.85) and c["eligibility"]["routing"]["eligible"] is False
        assert c["capability"] == pytest.approx(0.85) and c["headline"]["ranked"] is False
    assert (h20["controls"]["routing"]["rate"], h80["controls"]["routing"]["rate"]) == (1.0, 0.7)
    assert "85.0%* judged 20 of 20 (100.0%; judged 10 of 10)" in report.render(rep, False)


def test_pooled_eligibility_can_rank_a_cell_with_a_weak_cell_rate(tmp_path):
    # 10/10 default and 9/10 coached: pooled 19/20 = 95%, so both cells are eligible.
    scores = (S.config_set("a", r_pass=2, pairs=2, e=[1.0], s=[1.0], controls=(10, 10))
              + S.config_set("ac", r_pass=2, pairs=2, e=[1.0], s=[1.0], controls=(9, 10), mode="coached"))
    c = rows(build(tmp_path, scores), "h20", "coached")[0][("m1", "pi")]
    assert c["controls"]["routing"]["rate"] == 0.9 and c["eligibility"]["routing"]["rate"] == 0.95 and c["headline"]["ranked"]


def test_legacy_pair_with_missed_conflict_counts_as_failure_and_is_listed(tmp_path):
    nr = S.sibling("a-3", 2, "B", None)
    nr["score"]["not_reached"] = True
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True), S.sibling("a-4", 2, "A", True), nr]
    r = rows(build(tmp_path, scores))[0][("m1", "pi")]["R"]
    assert r["value"] == 0.5 and r["pairs"] == 2
    assert r["not_reached"] == [{"seed": 2, "pair": "p2", "episodes": ["a-3", "a-4"], "not_reached": ["a-3"]}]


def test_a_task_instance_that_ran_as_two_tasks_is_refused(tmp_path):
    scores = S.config_set("a", r_pass=1, pairs=1, e=[1.0], s=[1.0]) + S.config_set("b", r_pass=1, pairs=1, e=[1.0], s=[1.0])
    next(d for d in scores if d["episode"] == "b-e0")["task"]["sha256"] = "f" * 64
    with pytest.raises(report.ReportError, match="ran as 2 different tasks"):
        build(tmp_path, scores)


def test_score_files_without_a_task_identity_are_refused(tmp_path):
    scores = [S.steer("a-1", 1, 1.0)]
    del scores[0]["task"]
    with pytest.raises(report.ReportError, match="no task identity"):
        build(tmp_path, scores)


def test_a_configuration_missing_seeds_is_shown_with_its_n_and_flagged(tmp_path):
    scores = S.config_set("a", r_pass=3, pairs=3, e=[1.0] * 3, s=[1.0] * 3) + S.config_set("b", r_pass=2, pairs=2, e=[1.0] * 3, s=[1.0] * 3)
    rs, _ = rows(build(tmp_path, scores))
    a, b = rs[("m1", "pi")], rs[("m1", "cc")]
    assert a["coverage"]["missing"] == [] and a["headline"]["ranked"]
    assert (b["coverage"]["ran"], b["coverage"]["planned"]) == (a["coverage"]["planned"] - 2, a["coverage"]["planned"])
    assert b["headline"]["ranked"] is False and b["headline"]["band"] is None
    assert "R partial: judged 2 of 3" in b["headline"]["unranked_because"]
    assert b["headline"]["value"] == pytest.approx(100) and b["R"]["pairs"] == 2


def test_harness_effect(tmp_path):
    rep = build(tmp_path, main_set())
    eff = rep["sections"][0]["harness_effect"]
    assert [(r["model"], r["horizon"], r["mode"]) for r in eff] == [("m1", "h20", "default")]
    r = eff[0]
    assert r["headline"]["value"] == pytest.approx(50) and r["harnesses"] == ["cc", "pi"]
    lo, hi = r["headline"]["interval"]
    # pi is 100 on every draw, so the spread is 100 minus cc's headline on the same seeds.
    assert 36 < lo < 50 < hi < 77


def test_coached_gap(tmp_path):
    scores = (S.config_set("a", r_pass=4, pairs=4, e=[1.0] * 4, s=[1.0] * 4)
              + S.config_set("ac", r_pass=4, pairs=4, e=[0.5] * 4, s=[1.0] * 4, mode="coached"))
    g = build(tmp_path, scores)["sections"][0]["coached_gap"]
    assert len(g) == 1 and g[0]["horizon"] == "h20" and "not_comparable" not in g[0]["E"]
    assert g[0]["E"]["value"] == pytest.approx(-0.5) and g[0]["E"]["interval"] == pytest.approx([-0.5, -0.5])
    assert g[0]["headline"]["value"] == pytest.approx(100 * 0.5 ** (1 / 3) - 100)
    assert g[0]["R"]["value"] == 0.0


def test_sanity_bound_on_the_command_line(tmp_path):
    scores = [S.sibling("a-1", 1, "A", True), S.sibling("a-2", 1, "B", True), S.steer("a-3", 1, 1.0)]
    scores += [S.expl(f"a-e{i}", i, 1.0, no_answer=(3, 6)) for i in range(6)]
    root = S.write(tmp_path / "results", scores, CONFIGS)
    # Six items at 3 of 6: 0.5 against a bound of 1/6 + z * 0.152, which fails at z = 2 and passes at z = 2.5.
    assert main(["report", str(root), "--out", str(tmp_path / "o1"), "--resamples", "10"]) == 0
    assert main(["report", str(root), "--out", str(tmp_path / "o2"), "--resamples", "10", "--z", "2.5"]) == 0
    a = json.loads((tmp_path / "o1" / "report.json").read_text())
    b = json.loads((tmp_path / "o2" / "report.json").read_text())
    assert b["parameters"]["sanity_z"] == 2.5 and a["parameters"]["sanity_z"] == 2.0
    k = "r" * 64
    assert a["sections"][0]["reader_sanity"][k]["passed"] is False and b["sections"][0]["reader_sanity"][k]["passed"] is True
    assert main(["report", str(root), "--out", str(tmp_path / "o3"), "--z", "0"]) == 2
