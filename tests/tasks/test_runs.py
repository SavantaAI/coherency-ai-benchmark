"""Every generated instance's ideal and worst script, run through the runner
with its fake episode and the real scripted model engine (harness.py,
minipi.py), and what each run must show."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from cohbench.scoring import checks

from .conftest import info, instances, task


class Tree:
    """The scoring unit's check functions read a tree through these three
    calls; this serves a plain {path: text} tree the same way."""

    def __init__(self, files: dict[str, str]):
        self.blobs = {hashlib.sha256(t.encode()).hexdigest(): t.encode() for t in files.values()}
        self.files = {p: ("file", "0644", hashlib.sha256(t.encode()).hexdigest()) for p, t in files.items()}

    def tree(self, batch: Any) -> dict[str, tuple[Any, ...]]:
        return self.files

    def unknown(self, batch: Any) -> set[str]:
        return set()

    def content(self, sha: str | None) -> bytes | None:
        return self.blobs.get(sha or "")


def check(files: dict[str, str], spec: Any) -> bool | None:
    return checks.run(Tree(files), 0, spec)["held"]


def test_every_script_runs_to_a_usable_episode_without_leaving_its_script(suite, runs):
    bad = []
    for (name, who), r in sorted(runs.items()):
        if r.status != "usable" or r.end_reason != "finished" or r.errors() or (r.record and r.record["reach"]["status"] != "reached"):
            bad.append((name, who, r.status, r.end_reason, r.detail, r.errors()[:2]))
    assert not bad, bad
    assert len(runs) == 2 * len(instances(suite))


def _after_conflict(r) -> list[str]:
    ev = r.run["events"].get("conflict")
    assert ev and ev["status"] == "fired", "the conflict never fired"
    return [v["label"] for v in r.verdicts if v["utc"] >= ev["fired_utc"]]


@pytest.mark.parametrize("who", ["ideal", "worst"])
def test_routing_final_check_holds_for_the_ideal_agent_and_fails_for_the_worst(suite, runs, who):
    for d in instances(suite, "routing"):
        k = task(d)["scoring"]["answer_key"]
        got = check(runs[(d.name, who)].tree, k["final_check"])
        assert got is (who == "ideal"), (d.name, who, runs[(d.name, who)].tree.get(info(d)["deliverable"]))
        if task(d)["scoring"]["control"]:
            assert check(runs[(d.name, who)].tree, task(d)["scoring"]["control"]["expected"]["final"]) is (who == "ideal")


def test_routing_final_check_fails_an_unfinished_job(suite):
    for d in instances(suite, "routing"):
        k = task(d)["scoring"]["answer_key"]
        full = info(d)["final_list"]
        lines = full.splitlines(keepends=True)
        assert check({info(d)["deliverable"]: full}, k["final_check"]) is True, d.name
        assert check({info(d)["deliverable"]: "".join(lines[1:])}, k["final_check"]) is False, d.name
        assert check({}, k["final_check"]) is False, d.name


def test_routing_requests_are_what_each_script_is_meant_to_make(suite, runs):
    for d in instances(suite, "routing"):
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
            assert set(worst.labels()) <= {"no_request"}, (d.name, worst.labels())


def _answers(r) -> list[str]:
    return [x["text"] for x in r.transcript if x.get("role") == "agent"]


def test_explanation_ideal_leads_with_the_cause_and_worst_buries_it(suite, runs):
    for d in instances(suite, "explanation"):
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
                    assert sentence not in a, (d.name, a)
        users = [x["text"] for x in runs[(d.name, "ideal")].transcript if x.get("role") == "user"]
        assert users[1] == k["follow_up"]


def _moves(values: list[int | None]) -> list[str]:
    """The objective's moves between measured states, as D-045 counts them
    (each changing step here is its own batch, seconds apart)."""
    seen = [v for v in values if v is not None]
    return ["toward" if b < a else "away" if b > a else "level" for a, b in zip(seen, seen[1:])]


def test_steering_ideal_only_improves_and_worst_only_regresses(suite, runs):
    for d in instances(suite, "steering"):
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
            assert all(m.get("correct") == 1 for _at, m in r.fake.readings), d.name
            c = task(d)["scoring"]["control"]
            if c:
                last = r.fake.readings[-1][1]["checked_round_trips"]
                assert (last <= c["expected"]["final_metric"]["value"]) is (who == "ideal"), (d.name, who, last)


def test_steering_correction_is_delivered_and_the_worst_agent_keeps_regressing_after_it(suite, runs):
    for d in instances(suite, "steering"):
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
    for d in instances(suite, "steering"):
        for who in ("ideal", "worst"):
            log = runs[(d.name, who)].pi.log
            repeats, prev, run_len = 0, None, 0
            for c in log:
                if c["tool"] == "bash" and "bin/profile" in c["input"]["command"]:
                    key = re.search(r"^summary: (.+)$", c["result"], re.M).group(1)
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
        for who in ("ideal", "worst"):
            doc = json.loads((d / "scripted" / f"{who}-run.json").read_text())
            assert set(doc["probes"]) == qs, (d.name, who)


def test_horizon_80_runs_have_their_reset_before_the_scored_events(suite, runs):
    for d in instances(suite):
        t = task(d)
        if t["grouping"]["horizon"] != "h80" or t["grouping"]["family"] == "explanation":
            continue
        for who in ("ideal", "worst"):
            ev = runs[(d.name, who)].run["events"]
            assert ev["handoff"]["status"] == "fired", (d.name, who)
            for name, e in ev.items():
                if name != "handoff" and e["status"] == "fired":
                    assert e["fired_utc"] > ev["handoff"]["fired_utc"], (d.name, who, name)
