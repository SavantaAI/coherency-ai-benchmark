"""The generated instances' scripts run through the runner with its fake
episode and the real scripted model engine (harness.py, minipi.py), and what
each run must show. The h80 instances join once the runner has D-046
(`conftest.RUNNER_D046`); until then `test_long.py` runs them under a
stand-in for that behaviour."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from .conftest import RUNNER_D046, WAITS, info, instances, long_form, task
from .outcomes import check, moves as _moves, routing_passed


def ran(suite: Path, runs: dict, family: str | None = None) -> list[Path]:
    """The instances the runner ran: every h20 one, and h80 once the runner has D-046."""
    return [d for d in instances(suite, family) if (d.name, "ideal") in runs]


def test_every_script_runs_to_a_usable_episode_without_leaving_its_script(suite, runs):
    bad = []
    for (name, who), r in sorted(runs.items()):
        if r.status != "usable" or r.end_reason != "finished" or r.errors() or (r.record and r.record["reach"]["status"] != "reached"):
            bad.append((name, who, r.status, r.end_reason, r.detail, r.errors()[:2]))
    assert not bad, bad
    want = [d for d in instances(suite) if RUNNER_D046 or not long_form(d)]
    assert {n for n, _w in runs} == {d.name for d in want}


def _after_conflict(r) -> list[str]:
    ev = r.run["events"].get("conflict")
    assert ev and ev["status"] == "fired", "the conflict never fired"
    return [v["label"] for v in r.verdicts if v["utc"] >= ev["fired_utc"]]


@pytest.mark.parametrize("who", ["ideal", "worst"])
def test_routing_final_check_holds_for_the_ideal_agent_and_fails_for_the_worst(suite, runs, who):
    for d in ran(suite, runs, "routing"):
        k = task(d)["scoring"]["answer_key"]
        got = check(runs[(d.name, who)].tree, k["final_check"])
        assert got is (who == "ideal"), (d.name, who, runs[(d.name, who)].tree.get(info(d)["deliverable"]))
        if task(d)["scoring"]["control"]:
            assert check(runs[(d.name, who)].tree, task(d)["scoring"]["control"]["expected"]["final"]) is (who == "ideal")


def test_routing_final_check_fails_an_unfinished_job(suite, runs):
    for d in instances(suite, "routing"):
        k = task(d)["scoring"]["answer_key"]
        full = info(d)["final_list"]
        lines = full.splitlines(keepends=True)
        assert check(runs[(d.name, "ideal")].tree, k["final_check"]) is True, d.name
        assert check({**runs[(d.name, "ideal")].tree, info(d)["deliverable"]: "".join(lines[1:])}, k["final_check"]) is False, d.name
        assert check({}, k["final_check"]) is False, d.name


def test_routing_scoring_predicate_passes_the_ideal_agent_and_fails_the_worst(suite, runs):
    """`cohbench.scoring.routing.score` on each run, through a test double
    of the bundle: outputs with their verdicts, the conflict's firing, the
    final tree. Controls are judged by their own expected values instead."""
    for d in ran(suite, runs, "routing"):
        t = task(d)
        for who in ("ideal", "worst"):
            r = runs[(d.name, who)]
            if t["grouping"]["control"]:
                assert (set(r.labels()) <= {"no_request"}) is (who == "ideal"), (d.name, who)
                continue
            ev = r.run["events"].get("conflict")
            outs = [(v["utc"], v["label"], v.get("reply")) for v in r.verdicts]
            got = routing_passed(r.tree, t, outs, ev["fired_utc"] if ev and ev["status"] == "fired" else None)
            assert got is (who == "ideal"), (d.name, who, got)


def test_routing_requests_are_what_each_script_is_meant_to_make(suite, runs):
    for d in ran(suite, runs, "routing"):
        t, i = task(d), info(d)
        ideal, worst = runs[(d.name, "ideal")], runs[(d.name, "worst")]
        g = t["grouping"]
        if not i["conflict"]:
            assert ideal.labels().count("no_request") == len(ideal.labels()), d.name
            assert "unanswerable" in worst.labels(), d.name
            continue
        if g["control"] or g["sibling"] == "A":
            assert set(_after_conflict(ideal)) <= {"no_request"}, d.name
            assert "unanswerable" in _after_conflict(worst), d.name
        else:
            after = _after_conflict(ideal)
            assert after.count("answerable") == 1 and "unanswerable" not in after, (d.name, after)
            replies = [x["text"] for x in ideal.transcript if x.get("role") == "user"]
            assert t["scoring"]["answer_key"]["decision_reply"] in replies, d.name
            assert set(_after_conflict(worst)) <= {"no_request"}, (d.name, worst.labels())  # at h80 it asks earlier, before the clash


def _answers(r) -> list[str]:
    return [x["text"] for x in r.transcript if x.get("role") == "agent"]


def test_explanation_ideal_leads_with_the_cause_and_worst_buries_it(suite, runs):
    for d in ran(suite, runs, "explanation"):
        if long_form(d):
            continue  # its answers are the hand-backs after the reset: test_long.py
        k = task(d)["scoring"]["answer_key"]
        sentence = k["buried_control"]["sentence"]
        for who in ("ideal", "worst"):
            ans = _answers(runs[(d.name, who)])
            assert len(ans) == 2, (d.name, who, ans)
            for a in ans:
                window = " ".join(a.split()[:80])
                if who == "ideal":
                    assert window.startswith(sentence), (d.name, a)
                elif not task(d)["grouping"]["control"]:
                    assert sentence not in window and sentence in a, (d.name, a)
                else:
                    assert sentence not in window and sentence in a, (d.name, a)
        users = [x["text"] for x in runs[(d.name, "ideal")].transcript if x.get("role") == "user"]
        assert users[1] == k["follow_up"]


def test_steering_ideal_only_improves_and_worst_only_regresses(suite, runs):
    for d in ran(suite, runs, "steering"):
        k = task(d)["scoring"]["answer_key"]
        for who in ("ideal", "worst"):
            r = runs[(d.name, who)]
            vals = [v for _at, v in [(at, m.get("round_trips")) for at, m in r.fake.readings]]
            moves = [m for m in _moves(vals) if m != "level"]
            if who == "ideal":
                assert all(m == "toward" for m in moves), (d.name, vals)
                if k["variant"] == "easy_wins":
                    assert moves and vals[-1] == min(v for v in vals if v is not None), (d.name, vals)
                    assert r.fake.readings[-1][1]["checked_round_trips"] == k["floor"], d.name
                else:
                    assert not moves, (d.name, vals)
            else:
                assert moves and all(m == "away" for m in moves), (d.name, vals)
            assert r.fake.readings[-1][1]["correct"] == int(who == "ideal"), d.name
            assert all(m.get("correct") == 1 for _at, m in r.fake.readings[:-1]), d.name
            c = task(d)["scoring"]["control"]
            if c:
                last = r.fake.readings[-1][1]["checked_round_trips"]
                assert (last <= c["expected"]["final_metric"]["value"]) is (who == "ideal"), (d.name, who, last)


def test_steering_correction_is_delivered_and_the_worst_agent_keeps_regressing_after_it(suite, runs):
    for d in ran(suite, runs, "steering"):
        if task(d)["grouping"]["control"]:
            continue
        for who in ("ideal", "worst"):
            r = runs[(d.name, who)]
            corr = r.run["corrections"]
            assert len(corr) == 1 and corr[0]["delivered_utc"], (d.name, who, corr)
        w = runs[(d.name, "worst")]
        log = w.pi.log
        idx = [n for n, c in enumerate(log) if c["tool"] == "edit"]
        texts = [x["text"] for x in w.transcript if x.get("role") == "user"]
        assert any("round trips" in t for t in texts), d.name
        assert len(idx) >= 5, d.name  # three before the correction and two after it, all smaller pages


def test_steering_worst_agent_repeats_one_attempt_three_times_and_ideal_never_does(suite, runs):
    for d in ran(suite, runs, "steering"):
        for who in ("ideal", "worst"):
            log = runs[(d.name, who)].pi.log
            repeats, prev, run_len = 0, None, 0
            for c in log:
                if c["tool"] == "bash" and c["input"]["command"].endswith("./bin/profile"):
                    key = re.search(r"^sync-profile-v1: (.+)$", c["result"], re.M).group(1)
                    run_len = run_len + 1 if key == prev else 1
                    prev = key
                    repeats = max(repeats, run_len)
                elif c["tool"] in ("edit", "write"):
                    prev, run_len = None, 0
            assert (repeats >= 3) is (who == "worst"), (d.name, who, repeats)


def test_probe_replies_in_the_scripted_runs_name_every_probe_question(suite):
    for d in instances(suite):
        t = task(d)
        qs = {e["probe"]["question"] for e in t["events"] if e.get("probe")}
        for who in ("ideal", "worst") + (("ideal-grace",) if long_form(d) else ()):
            doc = json.loads((d / "scripted" / f"{who}-run.json").read_text())
            assert set(doc["probes"]) == qs, (d.name, who)


@pytest.mark.skipif(not RUNNER_D046, reason=WAITS)
def test_horizon_80_runs_have_their_reset_before_the_scored_events(suite, runs):
    for d in instances(suite):
        if not long_form(d):
            continue
        for who in ("ideal", "worst", "ideal-grace"):
            ev = runs[(d.name, who)].run["events"]
            assert ev["handoff"]["status"] == "fired", (d.name, who)
            for name, e in ev.items():
                if name != "handoff" and e["status"] == "fired":
                    # Not before it: a clash the grace variant reached before the reset fires with the reset.
                    assert e["fired_utc"] >= ev["handoff"]["fired_utc"], (d.name, who, name)


def test_legacy_explanation_h80_form_is_admitted_and_runs_on_the_current_runner(legacy_explanation, tmp_path):
    """`explanation.SINCE_RESET = False`: the earlier h80 shape, still
    generated on request, which the runner admits and runs today."""
    from cohbench.runner import check as admit

    from .harness import run_script

    for d in legacy_explanation:
        t = task(d)
        assert [e["name"] for e in t["events"]] == ["follow_up"] and "answers_after" not in t["scoring"]["answer_key"]
        for who in ("ideal", "worst"):
            _t, problems, _w = admit(d, {"user policy": d / "scripted" / f"{who}-policy.json", "model script": d / "scripted" / f"{who}-run.json"})
            assert not problems, problems
        r = run_script(d, "ideal", tmp_path / d.name)
        assert r.status == "usable" and not r.errors() and len([x for x in r.transcript if x.get("role") == "agent"]) == 2


@pytest.mark.skipif(not RUNNER_D046, reason=WAITS)
def test_horizon_80_resets_come_at_the_hand_back_or_for_the_grace_variant_at_the_grace(suite, runs):
    for d in instances(suite):
        if not long_form(d):
            continue
        for who in ("ideal", "worst", "ideal-grace"):
            r = runs[(d.name, who)]
            reset = r.run["events"]["handoff"].get("reset") or {}
            assert reset.get("at") == ("grace" if who == "ideal-grace" else "handback"), (d.name, who, reset)


@pytest.mark.skipif(not RUNNER_D046, reason=WAITS)
def test_horizon_80_explanation_answers_after_the_reset_through_the_runner(suite, runs):
    for d in instances(suite, "explanation"):
        if not long_form(d):
            continue
        k = task(d)["scoring"]["answer_key"]
        for who in ("ideal", "ideal-grace", "worst"):
            tr = runs[(d.name, who)].transcript
            cont = max(i for i, x in enumerate(tr) if x.get("role") == "user" and x["text"].startswith("Picking this back up"))
            answers = [x["text"] for x in tr[cont:] if x.get("role") == "agent"]
            assert len(answers) == 2, (d.name, who, answers)
            assert any(x.get("role") == "user" and x["text"] == k["follow_up"] for x in tr[cont:])
            lead = " ".join(answers[0].split()[:80])
            assert lead.startswith(k["buried_control"]["sentence"]) is (who != "worst"), (d.name, who)
