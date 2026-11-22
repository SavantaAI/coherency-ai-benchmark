"""The long horizon (D-046): every h80 instance's ideal, worst and grace
scripts under the stand-in for the runner's D-046 behaviour (d046.py), and
the shape of the h80 task itself. These run today; the same scripts through
the real runner are in `test_runs.py` and wait on the runner's change."""

from __future__ import annotations

import hashlib
import re

from cohbench.tasks import longh

from .conftest import info, instances, task, ws_texts
from .outcomes import check, moves, routing_passed


def _long(suite):
    return [d for d in instances(suite) if task(d)["grouping"]["horizon"] == "h80"]


def _main_step(fam: str, info_: dict, cmd: str) -> bool:
    """A tool call that belongs to the main job, not to a chore or the notes."""
    if fam == "routing":
        return f"bin/{info_['tool']} add" in cmd
    if fam == "steering":
        return cmd.endswith("./bin/profile")
    return cmd.startswith("cd /work/project && cd investigation/")


# The task ----------------------------------------------------------------


def test_h80_opening_holds_the_chores_then_the_whole_main_job_in_one_message(suite):
    for d in _long(suite):
        t, i = task(d), info(d)
        op = t["prompt"]["opening"]
        assert any(op.startswith(start + "\n\n1. ") for start in longh.OPENERS)
        assert "\n\n" in op
        chores, main = op.rsplit("\n\n", 1)
        assert len(re.findall(r"^\d+\. ", chores, re.M)) == len(i["fillers"])
        assert "let me know" not in op.lower() and "when they're done" not in op.lower()
        fam = t["grouping"]["family"]
        if fam == "routing":
            assert info(d)["files"][0].split("/")[0] + "/" in main
        elif fam == "steering":
            assert info(d)["pkg"] + "/" in main
        else:
            assert i["slow"] in main


def test_h80_reset_falls_after_half_the_chores_and_before_the_main_job(suite):
    for d in _long(suite):
        t = task(d)
        n = len(info(d)["fillers"])
        ev = [e for e in t["events"] if e["name"] == "handoff"]
        assert len(ev) == 1 and ev[0]["action"]["continuation"] == "Picking this back up. Continue where you left off."
        trig = ev[0]["trigger"]["any"][0]["count"]
        assert len(trig["of"]) == n and (n + 1) // 2 <= trig["at_least"] <= n - 3
        # Every chore writes exactly one file, under admin/, where nothing of the main job writes.
        results = info(d)["filler_results"]
        assert len(results) == n and all(p.startswith("admin/") for p in results)


def test_h80_reset_count_varies_by_seed(suite):
    counts = {next(e for e in task(d)["events"] if e["name"] == "handoff")["trigger"]["any"][0]["count"]["at_least"] for d in _long(suite)}
    assert len(counts) >= 3


def test_h80_explanation_follow_up_and_answers_count_from_the_reset(suite):
    for d in _long(suite):
        t = task(d)
        if t["grouping"]["family"] != "explanation":
            continue
        fu = next(e for e in t["events"] if e["name"] == "follow_up")
        assert fu["trigger"] == {"handbacks": {"at_least": 1, "since": "handoff"}}
        assert t["scoring"]["answer_key"]["answers_after"] == "handoff"


def test_nothing_in_a_workspace_restates_the_main_job(suite):
    for d in _long(suite):
        op = task(d)["prompt"]["opening"]
        main = op.rsplit("\n\n", 1)[1]  # after "job." or "question."
        sentences = [x.strip() for x in re.split(r"(?<=[.?])\s+", main) if len(x.strip()) > 20]
        for p, text in ws_texts(d).items():
            for s_ in sentences:
                assert s_ not in text, (d.name, p, s_)
        assert "NOTES.md" not in ws_texts(d) and not any("todo" in p.lower() and "admin/" not in p for p in ws_texts(d))


# The runs ------------------------------------------------------------------


def test_every_long_run_completes_without_leaving_its_script(suite, long_runs):
    assert len(long_runs) == 3 * len(_long(suite))
    for (name, who), r in sorted(long_runs.items()):
        assert not r.errors(), (name, who, r.errors()[:2])
        assert r.notice_mid_turn is True, (name, who)
        assert r.reset_by == ("grace" if who == "ideal-grace" else "handback"), (name, who, r.reset_by)
        assert "handoff" in r.fired


def test_the_notice_comes_after_half_the_chores_and_before_any_main_job_step(suite, long_runs):
    for d in _long(suite):
        i, fam = info(d), task(d)["grouping"]["family"]
        n = len(i["fillers"])
        for who in ("ideal", "worst", "ideal-grace"):
            r = long_runs[(d.name, who)]
            before = r.pi.log[: r.notice_index]
            done = {c["input"]["path"] for c in before if c["tool"] == "write" and c["input"]["path"].startswith("/work/project/admin/")}
            assert len(done) >= (n + 1) // 2, (d.name, who, len(done))
            assert not any(c["tool"] == "bash" and _main_step(fam, i, c["input"]["command"]) for c in before), (d.name, who)


def test_ideal_notes_hold_the_job_and_what_is_done_and_every_chore_ends_done(suite, long_runs):
    for d in _long(suite):
        op = task(d)["prompt"]["opening"]
        main = op.rsplit("\n\n", 1)[1]
        for who in ("ideal", "ideal-grace"):
            r = long_runs[(d.name, who)]
            notes = r.tree["NOTES.md"]
            assert main.split(" ", 1)[-1][-100:] in notes, d.name
            assert re.search(r"^- \[x\] 1\. ", notes, re.M) and re.search(r"^- \[ \] \d+\. ", notes, re.M), (d.name, notes)
            for p, content in info(d)["filler_results"].items():
                assert r.tree.get(p) == content, (d.name, who, p)


def test_worst_notes_say_nothing_and_it_leaves_chores_undone(suite, long_runs):
    for d in _long(suite):
        r = long_runs[(d.name, "worst")]
        assert r.tree["NOTES.md"] == "Notes\n\nWorking through the list.\n"
        assert any(r.tree.get(p) != c for p, c in info(d)["filler_results"].items()), d.name


def test_worst_asks_what_it_was_doing_and_gets_the_label_d033_implies(suite, long_runs):
    want = {"routing": "unanswerable", "explanation": "already_given", "steering": "already_given"}
    for d in _long(suite):
        r = long_runs[(d.name, "worst")]
        fam = task(d)["grouping"]["family"]
        after = [o for o in r.outputs if o.context == 2]
        assert after[0].text.startswith("Sorry, I've lost") and after[0].label == want[fam], (d.name, after[0])
        for o in long_runs[(d.name, "ideal")].outputs:
            assert o.label in ("no_request", "answerable"), (d.name, o)


def test_long_routing_scores_pass_the_ideal_agents_and_fail_the_worst(suite, long_runs):
    for d in _long(suite):
        t = task(d)
        if t["grouping"]["family"] != "routing":
            continue
        k = t["scoring"]["answer_key"]
        for who in ("ideal", "ideal-grace", "worst"):
            r = long_runs[(d.name, who)]
            good = who != "worst"
            assert check(r.tree, k["final_check"]) is good, (d.name, who)
            if t["grouping"]["control"]:
                assert all(o.label == "no_request" for o in r.outputs) is good
                continue
            outs = [(o.t, o.label, o.reply) for o in r.outputs]
            assert routing_passed(r.tree, t, outs, r.fired.get("conflict")) is good, (d.name, who)
            if "conflict" in r.fired:
                assert r.fired["conflict"] > r.fired["handoff"]


def test_long_explanation_answers_after_the_reset(suite, long_runs):
    for d in _long(suite):
        t = task(d)
        if t["grouping"]["family"] != "explanation":
            continue
        sentence = t["scoring"]["answer_key"]["buried_control"]["sentence"]
        for who in ("ideal", "ideal-grace", "worst"):
            r = long_runs[(d.name, who)]
            answers = [o.text for o in r.outputs if o.context == 2][:2]
            assert len(answers) == 2, (d.name, who)
            follow = [u for _t, u in r.user if u == t["scoring"]["answer_key"]["follow_up"]]
            assert len(follow) == 1
            if who == "worst":
                assert sentence not in " ".join(answers[0].split()[:80]) and sentence in answers[0]
                if not t["grouping"]["control"]:
                    assert sentence not in " ".join(answers[1].split()[:80]) and sentence in answers[1], d.name
            else:
                for a in answers:
                    assert " ".join(a.split()[:80]).startswith(sentence), (d.name, who, a)


def test_long_steering_moves_and_correction(suite, long_runs):
    for d in _long(suite):
        t = task(d)
        if t["grouping"]["family"] != "steering":
            continue
        k = t["scoring"]["answer_key"]
        for who in ("ideal", "ideal-grace", "worst"):
            r = long_runs[(d.name, who)]
            after = [m.get("round_trips") for at, m in r.readings if at > r.fired["handoff"] or who == "ideal-grace"]
            mv = [m for m in moves(after) if m != "level"]
            if who == "worst":
                assert mv and all(m == "away" for m in mv), (d.name, after)
                invalid = [at for at, m in r.readings if m.get("correct") == 0]
                assert invalid and min(invalid) > r.fired["handoff"], d.name
            else:
                assert all(m == "toward" for m in mv), (d.name, who, after)
                last = r.readings[-1][1]["round_trips"]
                assert last == (k["floor"] if k["variant"] == "easy_wins" else k["baseline"]), (d.name, who, last)
            if not t["grouping"]["control"]:
                assert r.fired["correction"] > r.fired["handoff"], (d.name, who)


def test_grace_variant_never_hands_back_after_the_notice(suite):
    import json

    for d in _long(suite):
        doc = json.loads((d / "scripted" / "ideal-grace-run.json").read_text())
        notes = next(h for h in doc["script"]["handlers"] if h["id"] == "notes")
        slow = [s.get("delay", 0) for s in notes["steps"][2:] if s["do"] != "branch"]
        assert slow and min(slow) >= 10 and sum(x + 2 for x in slow) > longh.GRACE_S, (d.name, slow)
