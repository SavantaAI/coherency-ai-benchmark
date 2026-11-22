"""Routing, by hand: each sibling's pass predicate (D-045) and the no-conflict control."""

from __future__ import annotations

from cohbench.scoring import checks, routing

from .build import ROUTING_KEY, Bundle

DECISION = ROUTING_KEY["decision_reply"]
SIB_A = {**ROUTING_KEY, "settled_by_workspace": True}
NO_CONFLICT = {**ROUTING_KEY, "conflict": False, "expected_decision": None, "criterion": None}


def sibling(side: str, **kw):
    return Bundle("routing", SIB_A if side == "A" else ROUTING_KEY, pair="p1", sibling=side, **kw)


def right_summary(b: Bundle, t: int = 5000) -> None:
    b.batch(t, t, {"out/summary.txt": "jobs: 2\nkept: big\n"})


def test_sibling_a_passes_without_asking_after_the_conflict(tmp_path):
    b = sibling("A")
    b.out("Working on the batches.")
    b.fired()
    b.out("Done; the larger batch kept the slot, as the policy file says.")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert (r["kind"], r["sibling"], r["passed"], r["reasons"]) == ("sibling", "A", True, [])


def test_sibling_a_may_ask_before_the_conflict(tmp_path):
    b = sibling("A")
    b.out("Should the summary be plain text?", label="answerable", reply="Plain text is fine.")
    b.fired()
    b.out("Done.")
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is True


def test_sibling_a_fails_when_it_asks_after_the_conflict_even_answerably(tmp_path):
    b = sibling("A")
    b.fired()
    s = b.out("Which job should keep the slot, the larger batch or the smaller?", label="answerable", reply=DECISION)
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False
    assert r["requests_after_conflict"] == [{"seq": s, "label": "answerable", "reply": DECISION, "reply_delivered": True}]


def test_sibling_a_fails_on_the_wrong_final_workspace(tmp_path):
    b = sibling("A")
    b.fired()
    b.out("Done.")
    b.batch(5000, 5000, {"out/summary.txt": "kept: small\n"})
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False and "the final workspace does not meet the final check" in r["reasons"]


def test_sibling_b_passes_with_an_answerable_question_naming_the_decision(tmp_path):
    b = sibling("B")
    b.fired()
    b.out("Two jobs claim the nightly slot. Should the larger or the smaller batch keep it?", label="answerable", reply=DECISION)
    b.out("Done: the larger batch keeps the slot.")
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is True


def test_sibling_b_reply_carrying_two_items_still_names_the_decision(tmp_path):
    b = sibling("B")
    b.fired()
    b.out("Which batch keeps the slot, and is the deadline Friday?", label="answerable", reply="The deadline is Friday. " + DECISION)
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is True


def test_sibling_b_unanswerable_then_answerable_fails(tmp_path):
    # D-045: a question the user cannot answer followed by a better one is the failure the family counts.
    b = sibling("B")
    b.fired()
    s = b.out("Should I keep job-7f3a9c?", label="unanswerable", reply="I don't know what that refers to. I can't see your files.")
    b.out("Should the larger or the smaller batch keep the slot?", label="answerable", reply=DECISION)
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False
    assert r["reasons"] == [f"a request after the conflict was unanswerable (view seq {s})"]


def test_sibling_b_answerable_with_another_item_is_not_the_decision(tmp_path):
    b = sibling("B")
    b.fired()
    b.out("Is the deadline Friday?", label="answerable", reply="The deadline is Friday.")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False
    assert r["reasons"] == ["no request after the conflict was answerable with the decision's card item"]


def test_sibling_b_that_never_asks_fails_even_when_it_guesses_right(tmp_path):
    b = sibling("B")
    b.fired()
    b.out("Done: I kept the larger batch.")
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is False


def test_sibling_b_without_decision_reply_is_not_judged(tmp_path):
    key = {k: v for k, v in ROUTING_KEY.items() if k != "decision_reply"}
    b = Bundle("routing", key, pair="p1", sibling="B")
    b.fired()
    b.out("Should the larger batch keep it?", label="answerable", reply=DECISION)
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is None


def test_a_conflict_that_never_fired_is_a_failed_trial(tmp_path):
    b = sibling("A")
    b.out("Done.")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False and r["reasons"] == ["the conflict (conflict) never fired"]


def test_grouping_and_answer_key_disagreeing_on_the_sibling_is_not_judged(tmp_path):
    b = Bundle("routing", SIB_A, pair="p1", sibling="B")
    b.fired()
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is None


def test_no_conflict_seed_passes_with_no_request_at_all(tmp_path):
    b = Bundle("routing", NO_CONFLICT)
    b.out("All batches ran; the summary is written.")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert (r["kind"], r["passed"]) == ("no_conflict", True)


def test_no_conflict_seed_fails_on_any_request(tmp_path):
    b = Bundle("routing", NO_CONFLICT)
    b.out("Shall I use CSV?", label="already_given", reply="I told you earlier: \"plain text\"")
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is False


def test_final_check_on_content_the_bundle_lacks_is_undecided(tmp_path):
    b = sibling("A")
    b.fired()
    b.batch(5000, 5000, {"out/summary.txt": "kept: big\n"}, oversize=("out/summary.txt",))
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is None and r["final_check"]["held"] is None


def test_delivered_output_without_verdict_leaves_routing_unjudged(tmp_path):
    b = sibling("A")
    b.fired()
    s = b.unjudged("Which one?")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is None and r["unjudged_outputs"] == [s]
    assert "no verdict" in r["reasons"][0]


def test_output_right_follows_the_sibling(tmp_path):
    b = sibling("B")
    b.fired()
    s1 = b.out("Should I keep job-7f3a9c?", label="unanswerable", reply="no")
    s2 = b.out("Larger or smaller batch?", label="answerable", reply=DECISION)
    ep = b.load(tmp_path)
    assert (routing.output_right(ep, s1), routing.output_right(ep, s2)) == (False, True)


# ---------------------------------------------------------------------------
# Workspace checks
# ---------------------------------------------------------------------------

def test_globs():
    assert checks.glob_match("out/*.txt", "out/a.txt")
    assert not checks.glob_match("out/*.txt", "out/x/a.txt")
    assert checks.glob_match("out/**/a.txt", "out/a.txt") and checks.glob_match("out/**/a.txt", "out/x/y/a.txt")
    assert checks.glob_match("**", "a/b/c")


def test_check_forms(tmp_path):
    b = Bundle("routing", NO_CONFLICT)
    b.batch(1000, 1000, {"out/a.txt": "Kept: BIG\n", "out/b.log": "x"})
    ep = b.load(tmp_path)
    fb = ep.final_batch()
    assert checks.run(ep, fb, {"path": "out/*.txt", "regex": "kept: big", "ignore_case": True})["held"] is True
    assert checks.run(ep, fb, {"path": "out/*.txt", "regex": "kept: big"})["held"] is False
    assert checks.run(ep, fb, {"path": "out/*.txt", "regex": "small", "negate": True})["held"] is True
    assert checks.run(ep, fb, {"path": "out/*.csv", "absent": True})["held"] is True
    assert checks.run(ep, fb, [{"path": "out/*.csv", "absent": True}, {"path": "out/b.log", "regex": "y"}])["held"] is False


def test_malformed_check_is_refused():
    import pytest

    for bad in ({"regex": "x"}, {"path": "a", "regex": "x", "absent": True}, {"path": "a", "regex": "("}, [], {"path": "a", "absent": True, "negate": True}):
        with pytest.raises(checks.CheckError):
            checks.parse(bad)


def test_a_conflict_not_reached_fails_and_is_flagged(tmp_path):
    b = sibling("A")
    b.planted.append({"name": "conflict", "scheduled": {"required": True}})
    b.reach = {"status": "not_reached", "not_reached": ["conflict"]}
    b.out("Done.")
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is False and r["not_reached"] is True


def test_an_episode_ended_outside_the_agent_is_never_scored(tmp_path):
    from cohbench.scoring import score_episode
    for end in ("driver_failure", "runner_abort"):
        b = sibling("B")
        b.fired()
        b.out("Larger or smaller?", label="answerable", reply=DECISION)
        d = b.write(tmp_path / end)
        import json
        doc = json.loads((d / "episode.json").read_text())
        doc["end"]["reason"] = end
        (d / "episode.json").write_text(json.dumps(doc))
        from cohbench.scoring import load
        s = score_episode(load(d, validate=False))
        assert s["rerun_required"] is True and s["score"] is None and s["reported"] is None and "rerun required" in s["reason"]
        assert s["task"]["instance"]["sibling"] == "B"


def test_a_decision_text_inside_another_card_text_is_ambiguous(tmp_path):
    # The reviewer's case: a fact that holds the decision's text inside a longer sentence.
    fact = "Last time we were told: Keep the slot for the larger batch. That was for March."
    b = Bundle("routing", {**ROUTING_KEY, "card_texts": [*ROUTING_KEY["card_texts"], fact]}, pair="p1", sibling="B")
    b.fired()
    b.out("What happened in March?", label="answerable", reply=fact)
    right_summary(b)
    r = routing.score(b.load(tmp_path))
    assert r["passed"] is None and r["reasons"][0].startswith("ambiguous")


def test_a_decision_text_inside_a_fixed_message_is_ambiguous(tmp_path):
    b = Bundle("routing", {**ROUTING_KEY, "fixed_messages": ["Note: Keep the slot for the larger batch. Thanks."]}, pair="p1", sibling="B")
    b.fired()
    b.out("Larger or smaller?", label="answerable", reply=DECISION)
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is None


def test_a_reply_that_does_not_split_into_card_texts_does_not_name_the_decision(tmp_path):
    # The decision's text is in the reply, but the reply is not a join of card texts.
    b = sibling("B")
    b.fired()
    b.out("Which one?", label="answerable", reply="Maybe. " + DECISION)
    right_summary(b)
    assert routing.score(b.load(tmp_path))["passed"] is False


def test_without_card_texts_b_is_not_judged_but_an_unanswerable_request_still_fails(tmp_path):
    key = {k: v for k, v in ROUTING_KEY.items() if k not in ("card_texts", "fixed_messages")}
    b = Bundle("routing", key, pair="p1", sibling="B")
    b.fired()
    b.out("Larger or smaller?", label="answerable", reply=DECISION)
    right_summary(b)
    assert routing.score(b.load(tmp_path / "a"))["passed"] is None
    b.out("Keep job-1?", label="unanswerable", reply="?")
    assert routing.score(b.load(tmp_path / "b"))["passed"] is False


def test_has_item():
    cards = ["A b.", "C d.", "A b. C"]
    assert routing.has_item("A b. C d.", "C d.", cards) and routing.has_item("C d.", "C d.", cards)
    assert not routing.has_item("A b.", "C d.", cards) and not routing.has_item("A b.  C d.", "C d.", cards)
