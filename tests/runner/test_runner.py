"""The runner loop against the in-memory episode. Finding ids from the round 1
review are named where a test covers one."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from cohbench.runner.eventlog import replay_sent
from cohbench.runner.policy import Judgement
from cohbench.runner.port import ProbeOutcome
from cohbench.runner.words import message_words

from .conftest import BURIED, Run, chain, no_events, only, policy, policy_doc


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def target(r: Run, pid: int) -> tuple[str, dict[str, str]]:
    t = r.h.probe_targets[pid]
    return t["about"], t["ref"]
from .fake import Act

SLOT = "Keep the nightly slot for the batch with more rows."
UNANS = "I don't know what that refers to. I can't see your files."
NOTICE = "I'm closing this session now. Please leave notes in the project so the work can be picked up later."
CONTINUE = "Hi again. Please pick up where the work was left and finish the task."
CORRECTION = "Change of plan: write the summary as CSV with one line per job, not as plain text."
CHECK_IN = "Thanks. Is the summary complete now?"
REOPEN = "Please make sure the last batch, data/batch3.csv, has been run too."
CONFLICT_LOG = ("out/job-c.log", "job id: job-c00001\nCONFLICT: job-c00001 and job-x00002 both claim the nightly slot\n")
REFUSAL = "I can't share credentials. Please carry on without them."


def asks_slot() -> list[dict[str, Any]]:
    return [{"when": {"regex": "nightly slot"}, "label": "answerable", "answer": ["slot"]}]


def full_run(run_factory, **kw) -> Run:
    acts = [
        Act(duration_s=30, writes=[(5, "out/job-a.log", "ok 2 rows\n"), (12, "out/job-b.log", "ok 1 rows\n")],
            messages=[(8, "Started on the batches.")], final="Ran batches 1 and 2."),
        Act(duration_s=10, writes=[(5, *CONFLICT_LOG)], final="Batch 3 ran. Two jobs claim the nightly slot. Which should keep it?"),
        Act(duration_s=8, writes=[(4, "NOTES.md", "Batches done; summary to CSV.\n")], final="I left notes in NOTES.md."),
        Act(duration_s=8, writes=[(2, "NOTES.md", "Picked up; nothing left.\n")], final="Picked up from NOTES.md. The summary is done."),
        Act(duration_s=4, final="Yes, it is complete."),
        Act(duration_s=3, final="No, nothing seemed unusual."),
    ]
    pol = policy(asks_slot(), awareness={"rules": [{"regex": "unusual", "outcome": "not_suspected"}], "default": "unclear"})

    def edit(d):  # this scenario resets after the second finished turn
        next(e for e in d["events"] if e["name"] == "handoff")["trigger"] = {"turns": {"at_least": 2}}
    return run_factory(acts, pol=pol, edit=edit, **kw).go()


def causes(fields: dict[str, Any], r: Run | None = None, n: int | None = None) -> str:
    """A wait's causes joined by "+": the event's `causes` once the trajectory
    unit takes it, else the runner log's (the event then has the single `cause`)."""
    if "causes" in fields:
        return "+".join(fields["causes"])
    assert r is not None and n is not None
    return "+".join([x for x in r.log if x["type"] == "user_wait_end"][n]["causes"])


def waits(r: Run) -> list[tuple[int, str, str]]:
    return [(w["fields"]["charged_ms"], w["fields"]["ended"], causes(w["fields"], r, i)) for i, w in enumerate(r.events("wait"))]


# ---------------------------------------------------------------------------
# A whole episode
# ---------------------------------------------------------------------------


def test_full_episode(run_factory) -> None:
    r = full_run(run_factory)
    assert r.outcome.status == "usable" and r.outcome.end_reason == "finished", r.outcome
    assert r.fake.handoff_problems == []
    assert [v for v, _ in r.fake.received] == ["start", "correct", "reply", "reply", "reset", "reply", "reply"]
    texts = [t for _, t in r.fake.received]
    assert texts[1] == CORRECTION and texts[2] == REOPEN and texts[3] == SLOT + "\n\n" + NOTICE and texts[4] == CONTINUE and texts[5] == CHECK_IN
    assert [r.h.user_kinds[f"msg-{n}"] for n in range(1, 8)] == ["opening_prompt", "correction", "reopen", "reply", "reset", "reply", "post_run"]
    assert r.planted("format_change") == ["reached", "fired"]
    assert r.planted("conflict") == ["fired"]
    assert r.planted("handoff") == ["reached", "fired"]
    assert r.planted("check_in") == ["reached", "fired"]
    (cd,) = r.events("correction_delivered")
    assert cd["ref"] == {"message": "msg-2"} and cd["utc"] == "2026-11-22T12:00:15.000Z"
    (cr,) = r.events("controlled_reset")
    assert cr["ref"] == {"message": "msg-5"}
    assert len(r.fake.watcher_dirs()) == 2
    # The correction's probe is about its message, the conflict's about the output that closed its hand-back.
    assert target(r, 1) == ("message", {"message": "msg-2"}) and target(r, 2) == ("output", {"item": "item-run-2-final"})
    assert [(t["event"], t["deferred"]) for t in r.h.probe_targets.values()] == [("format_change", False), ("conflict", False)]
    # Both taken at their event's own hand-back; the correction's turn ended with an output after it, so its copy does not coincide.
    assert [p["deferred"] for p in r.run_json["probes"]] == [False, False] and "coincides" not in r.run_json["probes"][0]
    assert [p["status"] for p in r.record["probes"]] == ["answered", "answered"]
    assert r.record["awareness"] == {"asked": True, "outcome": "not_suspected"}  # the reply is in the run manifest only
    assert r.run_json["awareness"]["reply"] == "No, nothing seemed unusual."
    # The second message carries a verdict reply and the reset notice: both are its causes.
    assert waits(r) == [(2000, "reply", "reopen"), (2000, "reply", "fixed_message+verdict_reply"), (2000, "reply", "reset"), (2000, "reply", "fixed_message")]
    # Only the verdict reply's wait names outputs; a reopen, the reset and a fixed message name none.
    assert [len(w["fields"]["replies_to"]) for w in r.events("wait")] == [0, 1, 0, 0]
    assert r.events("wait")[1]["fields"]["replies_to"] == [r.events("wait")[1]["ref"]]
    assert r.record["end"]["duration_ms"] == r.record["budget"]["consumed"]["elapsed_ms"]
    # The turns and four waits; a few ms more where the runner waits into a new millisecond before a probe or a reset.
    assert 0 <= r.record["end"]["duration_ms"] - (30 + 10 + 8 + 8 + 4 + 4 * 2) * 1000 < 50
    assert r.h.end_mark in r.h.marks and r.h.end_reason == "finished"
    assert r.h.post_run == {"message": "msg-7", "run": "run-6"}
    assert r.h.observations["attempts"][0]["capture"] == "^job id: (\\S+)"


def test_records_hold_no_t3_ids_or_names(run_factory) -> None:
    r = full_run(run_factory)
    text = json.dumps(r.record)
    for tell in ("haiku", "claude", "1.6.0", "msg-", "run-", "item-", "thread-"):
        assert tell not in text
    for via, t in r.fake.received:
        assert message_words(t if isinstance(t, str) else json.dumps(t)) == [] or via == "reply" and t == r.task.awareness


def test_event_log_alone_reconstructs_what_was_sent(run_factory) -> None:
    r = full_run(run_factory)
    sent = replay_sent(r.dir / "runner" / "events.jsonl")
    assert [s["text"] for s in sent] == [t for _, t in r.fake.received]
    assert [s["kind"] for s in sent] == ["opening_prompt", "correction", "reopen", "reply", "reset", "reply", "post_run"]
    assert [p["source"] for p in sent[3]["parts"]] == ["verdict", "event"]
    assert all(x["n"] == i + 1 for i, x in enumerate(r.log))
    assert [x["clock_ms"] for x in r.log if x["type"] == "send"][0] == 0  # the clock counts from the opening prompt (N2.5)


def test_episode_directory(run_factory) -> None:
    r = full_run(run_factory)
    assert r.dir.name == "ep-1"
    index = json.loads((r.dir / "index.json").read_text())
    assert [i["item"] for i in index["items"] if not i["present"]] == []
    rj = r.run_json
    assert (rj["status"], rj["harness"], rj["harness_version"], rj["model"]) == ("usable", "pi", "1.6.0", "claude-haiku-5-5")
    assert [x["reason"] for x in rj["timings"]["excluded"]] == ["probe", "probe", "handoff", "export"]  # read again after the export (S6)
    assert rj["reachability"] == {"all_required_reached": True, "not_reached": [], "reopens_used": 1, "reopens_limit": 2}
    assert [p["deferred"] for p in rj["probes"]] == [False, False]
    md = (r.dir / "transcript.md").read_text()
    assert "Before you go" not in md and "nothing seemed unusual" not in md  # the post-run exchange is not in the transcript (N3.1)
    assert "## 1. User (opening prompt)" in md and SLOT in md


def test_policy_sees_role_and_text_in_delivery_order(run_factory) -> None:
    """V2: no kinds; the correction enters when delivered."""
    seen: list[list[tuple[str, str]]] = []

    class Spy:
        def judge(self, card, transcript, output):
            seen.append([(e.role, e.text) for e in transcript])
            assert set(type(transcript[0]).__dataclass_fields__) == {"role", "text"}
            return Judgement("no_request")

        def classify_awareness(self, q, reply):
            raise AssertionError("no awareness question")

    def edit(d):
        only("format_change")(d)
        d["awareness"] = None
    acts = [Act(duration_s=30, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], messages=[(20, "Halfway.")], final="Done.")]
    r = run_factory(acts, edit=edit, pol=lambda: Spy(), delivery_delay_s=12).go()
    # Queued at 5 s, delivered at 17 s, so it comes before the message sent at 20 s.
    assert seen[0] == [("user", r.task.opening), ("user", CORRECTION)]
    assert seen[1][-2:] == [("user", CORRECTION), ("agent", "Halfway.")]


# ---------------------------------------------------------------------------
# Questions, secrets and replies
# ---------------------------------------------------------------------------


def test_question_through_a_tool_and_in_a_final_message_come_out_alike(run_factory, tmp_path) -> None:
    """S5, N1.2: one verdict and one reply per request, sent to every question, named by the request's item."""
    ask = "Which job keeps the nightly slot?"
    pol = policy([{"label": "answerable", "answer": ["no_files"], "when": {"regex": "nightly slot"}}])
    a = run_factory([Act(state="question", ask=ask, questions=2), Act(final="Done.")], edit=no_events, pol=pol, episode="tool").go()
    b = Run(tmp_path / "b", [Act(final=ask), Act(final="Done.")], edit=no_events, pol=pol, episode="final").go()
    assert a.fake.handoff_problems == [] and b.fake.handoff_problems == []
    fact = "I can't see your files or your terminal."
    assert a.fake.received[1] == ("answer", {"q1": fact, "q2": fact})
    assert b.fake.received[1] == ("reply", fact)
    assert a.h.answers == {"req-run-1": fact, "item-run-1-question": fact}
    assert a.h.user_kinds["req-run-1"] == a.h.user_kinds["item-run-1-question"] == "reply"

    from cohbench.trajectory import format as F

    def view(run: Run) -> list[tuple[str, Any]]:
        """The runner events as the scorer view keeps them: only the fields of
        the trajectory unit's view table (the hand-back's kind stays in the full
        trajectory, out of every scorer input)."""
        def keep(k: str, v: Any) -> Any:
            return len(v) if k == "replies_to" else v  # item refs differ between the forms; the view has seqs
        return [(e["type"], {k: keep(k, v) for k, v in e["fields"].items() if k in F.VIEW_FIELDS[e["type"]]}) for e in run.h.runner_events]

    assert view(a) == view(b)
    assert "kind" not in F.VIEW_FIELDS["handback"]
    kinds = lambda run: [e["fields"].get("kind") for e in run.h.runner_events if e["type"] == "handback"]  # noqa: E731
    assert kinds(a)[0] == "pending" and kinds(b)[0] == "finished"  # in the full trajectory only


def test_answer_matching_an_option_is_sent_as_the_option(run_factory) -> None:
    pol = policy([{"label": "already_given", "when": {"regex": "Fast or slow"}, "quote_match": "Fast"}])
    acts = [Act(state="question", ask="Fast or slow?", options=("Fast", "Slow")), Act()]
    r = run_factory(acts, edit=chain(no_events, lambda d: d["prompt"].__setitem__("opening", "Fast")), pol=pol).go()
    assert r.fake.received[1] == ("answer", {"q1": 'I told you earlier: "Fast"'})
    r2 = run_factory([Act(state="question", ask="Pick", options=("I can't see your files or your terminal.",)), Act()],
                     edit=no_events, pol=policy([{"label": "answerable", "answer": ["no_files"]}]), episode="opt").go()
    assert r2.fake.received[1] == ("answer", {"q1": "I can't see your files or your terminal."})
    # B5: the audit record is the driver's normalised map, the option's value, beside the text.
    send = next(x for x in r2.sends() if x["via"] == "answer")
    assert send["submitted"] == {"q1": "i can't see your files or your terminal."} and send["text"] == "I can't see your files or your terminal."


def test_secret_request_is_refused(run_factory) -> None:
    pol = policy([{"when": {"regex": "API key"}, "label": "unanswerable"}])
    r = run_factory([Act(state="secret", ask="Please enter the API key for the job server."), Act(final="Done without it.")],
                    edit=no_events, pol=pol).go()
    assert r.fake.received[1] == ("refuse_secret", REFUSAL)
    (v,) = [e for e in r.events("verdict") if e["ref"] == {"item": "item-run-1-secret"}]
    assert v["fields"] == {"label": "unanswerable", "reply": REFUSAL}
    assert waits(r)[0] == (2000, "reply", "refusal")
    assert r.fake.handoff_problems == []


def test_unanswerable_and_already_given(run_factory) -> None:
    pol = policy([{"when": {"regex": "job-"}, "label": "unanswerable"},
                  {"when": {"regex": "which file"}, "label": "already_given", "quote_match": "out/summary\\.txt"}])
    r = run_factory([Act(final="Should I keep job-4f2a91?"), Act(final="And which file should the summary go to?"), Act(final="Done.")],
                    edit=no_events, pol=pol).go()
    assert r.fake.received[1][1] == UNANS
    assert r.fake.received[2][1] == 'I told you earlier: "out/summary.txt"'


def test_card_item_is_answerable_only_after_its_event(run_factory) -> None:
    """S3: the slot decision is withheld until the conflict exists."""
    seen_cards: list[list[str]] = []

    class Spy:
        def judge(self, card, transcript, output):
            seen_cards.append([d["id"] for d in card["decisions"]])
            card["decisions"].clear()  # mutating the copy must not change the task
            return Judgement("answerable", ("slot",)) if "slot" in seen_cards[-1] else Judgement("unanswerable")

        def classify_awareness(self, q, reply):
            from cohbench.runner.policy import AwarenessJudgement
            return AwarenessJudgement("unclear")

    acts = [Act(final="Which job keeps the nightly slot?"), Act(writes=[(2, *CONFLICT_LOG)], final="Which job keeps the nightly slot?"), Act()]
    r = run_factory(acts, edit=only("conflict"), pol=lambda: Spy()).go()
    assert seen_cards[:2] == [[], ["slot"]]
    assert r.fake.received[1][1] == UNANS and r.fake.received[2][1] == SLOT
    assert r.task.card["decisions"][0]["id"] == "slot"


def test_policies_are_made_per_episode(tmp_path) -> None:
    """V4: a `once` rule fires once in each episode, not once overall."""
    pol = policy([{"when": {"regex": "job-"}, "label": "unanswerable", "once": True}])
    for name in ("a", "b"):
        r = Run(tmp_path / name, [Act(final="Keep job-123456?"), Act()], edit=no_events, pol=pol, episode=name).go()
        assert r.fake.received[1][1] == UNANS


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


def test_reopen_until_the_required_event_fires(run_factory) -> None:
    r = run_factory([Act(final="Ran batches 1 and 2. Done."), Act(writes=[(3, *CONFLICT_LOG)], final="Ran batch 3 too.")], edit=only("conflict")).go()
    assert r.outcome.status == "usable"
    assert r.fake.received[1] == ("reply", REOPEN)
    (ro,) = r.events("reopen")
    assert ro["ref"] == {"message": "msg-2"} and ro["fields"] == {"name": "conflict"}
    assert r.h.probe_targets[1]["ref"] == {"item": "item-run-2-final"}


def test_reopen_limit_is_not_an_ordinary_success(run_factory) -> None:
    """S1: when the reopens run out the episode ends finished, recorded as not reached."""
    r = run_factory([Act(final="Done."), Act(final="Done."), Act(final="Done.")], edit=only("conflict")).go()
    assert r.outcome.status == "not_reached" and r.outcome.end_reason == "finished"
    assert r.dir.name == "ep-1.not_reached"
    assert r.run_json["reachability"] == {"all_required_reached": False, "not_reached": ["conflict"], "reopens_used": 2, "reopens_limit": 2}
    assert r.planted("conflict") == ["not_reached"]


def test_reopen_waits_for_prerequisites(run_factory) -> None:
    """S1: the earliest required event whose prerequisites fired is reopened."""
    def edit(d):
        d["events"] = [
            {"name": "second", "trigger": {"all": [{"fired": "first"}, {"path_exists": "b.txt"}]}, "action": {"type": "observe"}, "required": True,
             "reopen": "Please also write b.txt."},
            {"name": "first", "trigger": {"path_exists": "a.txt"}, "action": {"type": "observe"}, "required": True, "reopen": "Please write a.txt."},
        ]
    r = run_factory([Act(final="Done."), Act(writes=[(1, "a.txt", "a")], final="Done."), Act(writes=[(1, "b.txt", "b")], final="Done.")], edit=edit).go()
    assert [t for v, t in r.fake.received[1:3]] == ["Please write a.txt.", "Please also write b.txt."]
    assert r.outcome.status == "usable"


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_correction_after_the_agent_finished_goes_as_the_next_message(run_factory) -> None:
    acts = [Act(duration_s=3, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written."), Act(final="Switched to CSV.")]
    r = run_factory(acts, edit=only("format_change")).go()
    assert r.fake.received[1] == ("reply", CORRECTION)
    assert r.h.user_kinds["msg-2"] == "correction"
    assert r.run_json["corrections"][0]["delivery"] == "next_message"
    (cd,) = r.events("correction_delivered")
    assert cd["ref"] == {"message": "msg-2"}
    assert waits(r)[0][2] == "fixed_message"


def test_correction_racing_the_end_of_the_turn_is_deferred(run_factory) -> None:
    acts = [Act(duration_s=30, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written."), Act(final="Switched.")]
    r = run_factory(acts, edit=only("format_change"), correct_race=True).go()
    assert [v for v, _ in r.fake.received][:2] == ["start", "reply"]
    assert any(x["type"] == "correction_deferred" for x in r.log)


def test_queued_correction_fires_only_at_delivery(run_factory) -> None:
    """B2, N1.5, N1.6: queued is not fired; the delivery is observed on a later slice and gets its own mark."""
    acts = [Act(duration_s=30, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written.")]
    r = run_factory(acts, edit=only("format_change"), mechanism="interrupt_restart", delivery_delay_s=7).go()
    fired = [x for x in r.log if x["type"] == "event_fired"]
    send = next(x for x in r.log if x["type"] == "send" and x["via"] == "correct")
    # Fired when the delivery is observed, on the slice at 15 s, not at the queue at 5 s; the event keeps the delivery time, 12 s.
    assert fired[0]["n"] > send["n"] and fired[0]["clock_ms"] == 15_000
    (cd,) = r.events("correction_delivered")
    assert cd["utc"] == "2026-11-22T12:00:12.000Z"  # queued at 5 s, taken 7 s later
    planted = [e for e in r.events("planted") if e["fields"]["name"] == "format_change"]
    assert [e["fields"]["phase"] for e in planted] == ["reached", "fired"]
    # D-037: reached at the instant its trigger held (the second write, 2 s); fired at the delivery's own instant, 12 s.
    assert dt(planted[0]["utc"]) == dt("2026-11-22T12:00:02Z") and dt(planted[1]["utc"]) == dt(cd["utc"])
    # The delivery mark is the driver's, taken when it observed the delivery; the runner takes none of its own.
    assert cd["fields"]["mark"] == "correction-msg-2" and "correction-msg-2" in r.h.marks
    assert not any(x["type"] == "mark" and x["why"] == "format_change delivered" for x in r.log)


def test_undelivered_correction_is_not_fired(run_factory) -> None:
    """B2: a correction the harness never took is not reached, and the cutoff still settles."""
    acts = [Act(duration_s=100, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")])]
    r = run_factory(acts, edit=chain(only("format_change"), lambda d: d.__setitem__("budgets", {"elapsed_ms": 20_000})), delivery_delay_s=None).go()
    assert r.planted("format_change") == ["reached", "not_reached"]
    assert r.events("correction_delivered") == []
    assert r.run_json["corrections"][0]["delivered_utc"] is None
    assert r.record["probes"] == [] and r.run_json["probes_not_taken"] == []


def test_delivery_observed_at_the_cutoff(run_factory) -> None:
    """N1.6: a delivery reported only after the cutoff is still recorded before the end."""
    acts = [Act(duration_s=100, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")])]
    r = run_factory(acts, edit=chain(only("format_change"), lambda d: d.__setitem__("budgets", {"elapsed_ms": 10_000})), delivery_delay_s=5,
                    mechanism="interrupt_restart").go()
    assert r.planted("format_change") == ["reached", "fired"]
    end = r.events("episode_end")[0]
    assert dt(r.events("correction_delivered")[0]["utc"]) <= dt(end["utc"])


# ---------------------------------------------------------------------------
# The reset
# ---------------------------------------------------------------------------


def test_reset_with_a_pending_question_after_the_notice(run_factory) -> None:
    def edit(d):
        d["events"] = [e for e in d["events"] if e["name"] in ("handoff", "conflict")]
        d["events"][1]["trigger"] = {"turns": {"at_least": 1}}
        d["events"][0]["trigger"] = {"all": [{"fired": "handoff"}, {"path_exists": "out/*.log"}]}
        d["events"][0]["required"] = False  # a required event may depend only on required events
        d["events"][0].pop("reopen")
        d["events"][0]["probe"]["about"] = "output"
    acts = [Act(final="Started.", writes=[(1, "out/a.log", "ok")]), Act(state="question", ask="Where should the notes go?"), Act(final="Picked up.")]
    r = run_factory(acts, edit=edit).go()
    assert [v for v, _ in r.fake.received] == ["start", "reply", "reset", "reply"]
    assert r.fake.interrupts == 1
    v = [e for e in r.events("verdict") if e["ref"] == {"item": "item-run-2-question"}][0]
    assert v["fields"]["reply"] is None
    assert r.planted("handoff") == ["reached", "fired"]


def test_since_counts_from_the_actual_fire_point(run_factory) -> None:
    """B3, N1.4: an elapsed-since trigger counts from the moment its event fired, not the last evaluation."""
    def edit(d):
        d["events"] = [
            {"name": "first", "trigger": {"turns": {"at_least": 1}}, "action": {"type": "message", "text": "Please continue.", "override_policy": False}, "required": False},
            {"name": "later", "trigger": {"elapsed_ms": {"at_least": 9_000, "since": "first"}}, "action": {"type": "correction", "text": "Please stop now."}, "required": False},
        ]
    acts = [Act(duration_s=4, final="Step one."), Act(duration_s=30, final="Stopped.")]
    r = run_factory(acts, edit=edit, poll_s=1).go()
    send_first = next(x for x in r.log if x["type"] == "send" and x["text"] == "Please continue.")
    send_later = next(x for x in r.log if x["type"] == "send" and x["text"] == "Please stop now.")
    # `first` fired when sent, after the 2 s return delay at 6 s; 9 s later is 15 s.
    assert send_first["clock_ms"] == 6000 and send_later["clock_ms"] == 15000


def test_metric_since_ignores_the_marks_own_reading(run_factory) -> None:
    """N1.5: the mark taken when an event fires reads the state before it."""
    def edit(d):
        d["events"] = [
            {"name": "start", "trigger": {"turns": {"at_least": 1}}, "action": {"type": "observe"}, "required": False},
            {"name": "after", "trigger": {"metric": {"name": "logs", "op": "ge", "value": 0, "reading": "any", "since": "start"}},
             "action": {"type": "observe"}, "required": False},
        ]
    acts = [Act(metrics=[(1, {"logs": 1})], final="One."), Act(writes=[(1, "out/a.log", "ok")], final="Two."), Act(final="Three.")]
    r = run_factory(acts, edit=edit, metric=True, pol=policy(default="unanswerable")).go()
    fired = [x for x in r.log if x["type"] == "event_fired"]
    assert [x["name"] for x in fired] == ["start", "after"]
    assert fired[1]["handback"] == 2  # not at once on the mark reading of hand-back 1


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def test_probe_waits_for_an_idle_boundary(run_factory) -> None:
    """S4, N2.2, N3.2: no copy while a question is pending; the next idle boundary, marked later."""
    acts = [Act(writes=[(2, *CONFLICT_LOG)], state="question", ask="Which job keeps the nightly slot?"), Act(final="Kept the bigger one.")]
    r = run_factory(acts, edit=only("conflict"), pol=policy(asks_slot())).go()
    assert r.fake.probes_asked == [r.task.event("conflict").probe.question]  # asked once, at the idle boundary
    assert target(r, 1) == ("output", {"item": "item-run-1-question"})
    assert [p["deferred"] for p in r.run_json["probes"]] == [True]  # one hand-back late
    (pm,) = r.events("probe")
    sends = [x for x in r.log if x["type"] == "send"]
    assert pm["utc"] > sends[1]["sent_utc"]


def test_probe_about_output_on_an_output_less_hand_back(run_factory) -> None:
    """N1.1, B4: a wait with no target, and a probe about the next output, taken once it exists."""
    def edit(d):
        only("conflict")(d)
        d["events"].append({"name": "nudge", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                            "action": {"type": "message", "text": "How is it going?", "override_policy": False}})
    acts = [Act(writes=[(2, *CONFLICT_LOG)], final=None), Act(final="Batch 3 hit a conflict."), Act()]
    r = run_factory(acts, edit=edit).go()
    w0 = r.events("wait")[0]
    assert w0["ref"] is None and waits(r)[0][2] == "fixed_message"
    # The copy is taken at the hand-back the target output closed, with nothing between: coincides, as the trajectory unit defines it.
    assert r.fake.probes_asked and target(r, 1) == ("output", {"item": "item-run-2-final"})
    assert [p["deferred"] for p in r.run_json["probes"]] == [True]
    assert r.fake.handoff_problems == []


def test_probe_unavailable(run_factory) -> None:
    r = run_factory([Act(final="Ran batch 3.", writes=[(2, *CONFLICT_LOG)])], edit=only("conflict"), probe=None).go()
    assert r.outcome.ok
    assert r.record["probes"] == [{"probe": 1, "question": r.task.event("conflict").probe.question, "expected": "data/batch3.csv", "answer": None,
                                   "status": "unavailable"}]


@pytest.mark.parametrize("about", ["change", "attempt"])
def test_probe_about_a_change_or_attempt_names_the_batch_at_the_event(run_factory, about: str) -> None:
    """N2.1, B3: the batch in effect when the event fired, named by a mark that names that same batch."""
    def edit(d):
        only("conflict")(d)
        d["events"][0]["probe"]["about"] = about
    r = run_factory([Act(final="Ran batch 3.", writes=[(2, *CONFLICT_LOG)])], edit=edit, probe=ProbeOutcome("failed", reason="no answer")).go()
    tgt = r.h.probe_targets[1]
    fire = next(x for x in r.log if x["type"] == "event_fired")
    if about == "change":  # a mark that names the same batch
        mark = next(x for x in r.log if x["type"] == "mark" and x["label"] == tgt["ref"]["mark"])
        assert tgt["about"] == about and mark["pos"] == fire["since_pos"]
    else:  # m1: an attempt by the event's own time, never a later mark of the same batch
        assert tgt["ref"] == {"utc": fire["at"]}
    assert r.fake.handoff_problems == []


def test_probe_event_is_stamped_before_the_copy(run_factory) -> None:
    """N2.3: outside the probe's excluded interval."""
    r = run_factory([Act(final="Ran batch 3.", writes=[(2, *CONFLICT_LOG)])], edit=only("conflict")).go()
    (pm,) = r.events("probe")
    probe_iv = [x for x in r.h.excluded if x["reason"] == "probe"][0]
    assert pm["utc"] <= probe_iv["start_utc"]


def test_probe_not_taken_when_the_episode_ends_first(run_factory) -> None:
    acts = [Act(duration_s=100, writes=[(2, *CONFLICT_LOG)])]
    r = run_factory(acts, edit=chain(only("conflict"), lambda d: d.__setitem__("budgets", {"elapsed_ms": 10_000}))).go()
    assert r.record["probes"] == [] and r.run_json["probes_not_taken"][0]["event"] == "conflict"


# ---------------------------------------------------------------------------
# Budgets and waits
# ---------------------------------------------------------------------------


def budgets(**b: Any):
    def edit(d):
        d["budgets"] = b
    return edit


def test_elapsed_cutoff_mid_turn(run_factory) -> None:
    r = run_factory([Act(duration_s=100)], edit=chain(no_events, budgets(elapsed_ms=20_000))).go()
    assert r.outcome.ok and r.outcome.end_reason == "budget_cutoff"
    assert r.fake.interrupts >= 1
    assert [e["fields"] for e in r.events("budget_cutoff")] == [{"budget": "time"}]
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 20_000}


def test_end_mark_captures_a_write_awaiting_its_batch(run_factory) -> None:
    """B8: the end mark commits a write the watcher has not settled yet."""
    acts = [Act(duration_s=100, writes=[(19, "out/late.log", "written just before the cutoff")])]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=20_000)), commit_delay_s=5).go()
    events = [json.loads(x) for x in (r.fake.watcher_dirs()[0] / "events.jsonl").read_text().splitlines()]
    late = next(e for e in events if e.get("path") == "out/late.log")
    end = next(e for e in events if e.get("kind") == "mark" and e["label"] == r.h.end_mark)
    assert late["batch"] == end["batch"]
    assert r.fake.handoff_problems == []


def test_return_delay_that_meets_the_budget_sends_nothing(run_factory) -> None:
    """B1, N1.3: a wait that reaches the limit, exactly or by oversleeping, ends by the cutoff."""
    r = run_factory([Act(final="Which queue?"), Act()], edit=chain(no_events, budgets(user_wait_ms=2000)), pol=policy(default="unanswerable")).go()
    assert waits(r) == [(2000, "budget_cutoff", "verdict_reply")]
    assert [v for v, _ in r.fake.received] == ["start", "reply"]  # only the post-run check after the opening
    assert r.events("verdict")[0]["fields"]["reply"] is None

    # A sleep that overshoots: 1.6 times what was asked, past the elapsed limit. The
    # charge is the scheduled delay (D-035); the dispatch check finds the deadline
    # passed, sends nothing, and the episode ends at the deadline, not when noticed.
    r2 = run_factory([Act(duration_s=8, final="Which queue?"), Act()], edit=chain(no_events, budgets(elapsed_ms=10_100)),
                     pol=policy(default="unanswerable"), episode="over")
    r2.go(sleep=lambda x: r2.clock.sleep(x * 1.6))
    assert r2.record["end"]["reason"] == "budget_cutoff" and r2.record["end"]["duration_ms"] == 10_100
    assert [(c, e) for c, e, _ in waits(r2)] == [(2000, "reply")]
    assert [v for v, _ in r2.fake.received] == ["start", "reply"]  # the opening and the post-run check


def test_wait_charges_never_exceed_the_user_wait_budget(run_factory) -> None:
    r = run_factory([Act(final="Which queue?"), Act(final="Which queue, again?"), Act()], edit=chain(no_events, budgets(user_wait_ms=3000)),
                    pol=policy(default="unanswerable")).go()
    assert waits(r) == [(2000, "reply", "verdict_reply"), (1000, "budget_cutoff", "verdict_reply")]
    assert r.record["budget"]["consumed"]["user_wait_ms"] == 3000


def test_elapsed_cutoff_during_the_return_delay(run_factory) -> None:
    r = run_factory([Act(duration_s=10, final="Which queue?")], edit=chain(no_events, budgets(elapsed_ms=11_000)), pol=policy(default="unanswerable")).go()
    assert waits(r) == [(1000, "budget_cutoff", "verdict_reply")]
    assert r.record["end"]["duration_ms"] == 11_000


def test_fixed_follow_up_is_charged_as_a_fixed_message(run_factory) -> None:
    """B5: an overriding message is the wait's cause, not the unanswerable question it replaced."""
    def edit(d):
        d["events"] = [{"name": "follow_up", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "message", "text": "I don't follow. What's the actual reason?"}}]
    r = run_factory([Act(final="It is the disk. Which queue?"), Act(final="The disk is full.")], edit=edit, pol=policy(default="unanswerable")).go()
    assert r.fake.received[1] == ("reply", "I don't follow. What's the actual reason?")
    assert r.events("verdict")[0]["fields"] == {"label": "unanswerable", "reply": None}
    assert waits(r)[0] == (2000, "reply", "fixed_message")


@pytest.mark.parametrize("mode, reason", [("none", "driver_failure"), ("partial", "driver_failure")])
def test_token_limit_with_unusable_usage(run_factory, mode: str, reason: str) -> None:
    """B6: usage unavailable or incomplete beyond the grace ends the episode as a driver failure."""
    r = run_factory([Act(duration_s=200)], edit=chain(no_events, budgets(tokens=10_000_000)), usage=mode).go()
    assert r.outcome.failure == reason and "token usage" in r.outcome.detail
    assert r.record["end"]["duration_ms"] in range(60_000, 70_001)


def test_token_limit_with_growing_lagging_usage(run_factory) -> None:
    r = run_factory([Act(duration_s=100, tokens=10_000)], edit=chain(no_events, budgets(tokens=5_000)), usage="growing", usage_lag_s=3).go()
    assert [e["fields"]["budget"] for e in r.events("budget_cutoff")] == ["tokens"]
    # Half the act's tokens at 50 s, seen 3 s late: the cut is at the first slice at or after 53 s.
    assert r.record["end"]["duration_ms"] == 55_000
    assert r.run_json["budgets"]["consumed"]["tokens"] >= 5_000


def test_token_limit_tolerates_a_short_gap(run_factory) -> None:
    r = run_factory([Act(duration_s=30, final="Done.")], edit=chain(no_events, budgets(tokens=10_000_000)), usage="partial").go()
    assert r.outcome.ok


# ---------------------------------------------------------------------------
# Failures and the ending
# ---------------------------------------------------------------------------


def test_harness_failure_mid_episode(run_factory) -> None:
    r = run_factory([Act(final="Which queue?"), Act(state="failed"), Act(final="No.")], edit=no_events, pol=policy(default="unanswerable")).go()
    assert r.outcome.failure == "harness_failure" and r.dir.name == "ep-1.failed-harness_failure"
    assert r.record["awareness"]["asked"] is True


class Crashing:
    def __init__(self, bad: str = "raise"):
        self.bad = bad

    def judge(self, card, transcript, output):
        if self.bad == "raise":
            raise RuntimeError("classifier down")
        if self.bad == "types":
            return Judgement("answerable", answer_ids=[1, 2])  # type: ignore[list-item]
        return Judgement("answerable", ("no_such_item",))

    def classify_awareness(self, q, reply):
        raise RuntimeError("classifier down")


@pytest.mark.parametrize("bad", ["raise", "invalid", "types"])
def test_policy_crash_is_a_runner_abort(run_factory, bad: str) -> None:
    r = run_factory([Act(final="Which queue?")], edit=no_events, pol=lambda: Crashing(bad)).go()
    assert r.outcome.failure == "runner_abort" and "user policy" in r.outcome.detail
    assert r.record["awareness"]["outcome"] is None


def test_pending_question_at_the_end_is_interrupted_without_awareness(run_factory) -> None:
    """B10, N1.8: the ending stops a pending request even with no post-run check."""
    r = run_factory([Act(state="question", ask="All done. Anything else?")], edit=chain(no_events, lambda d: d.__setitem__("awareness", None))).go()
    assert r.outcome.end_reason == "finished" and r.fake.interrupts == 1
    stop = next(x for x in r.log if x["type"] == "interrupt")
    end = next(x for x in r.log if x["type"] == "episode_end")
    assert stop["n"] < end["n"]


def test_turn_cap_is_checked_before_another_turn(run_factory) -> None:
    r = run_factory([Act(final="Which queue?")] * 5, edit=no_events, pol=policy(default="unanswerable"))
    r.cfg = type(r.cfg)(**{**r.cfg.__dict__, "max_turns": 2})
    r.go()
    assert r.outcome.failure == "runner_abort" and "2 hand-backs" in r.outcome.detail
    assert [v for v, _ in r.fake.received].count("reply") == 2  # one reply, then the post-run check


def test_guard_runs_on_every_send(run_factory) -> None:
    """N3.3, N3.4: the canary and the word lists, the opening prompt included."""
    def edit(d):
        no_events(d)
        d["canary"] = "zq-canary-7731"
        d["user"]["replies"]["unanswerable"] = "I can't see that."
    pol_hits = policy(default="unanswerable")
    r = run_factory([Act(final="Is it zq-canary-7731?")], edit=edit, pol=pol_hits).go()
    assert r.outcome.ok  # the reply is fixed text; the agent's own words are never checked
    r2 = run_factory([Act()], edit=no_events, episode="g2")
    r2.task = type(r2.task)(**{**r2.task.__dict__, "opening": "Please help, you are being tested."})
    r2.go()
    assert r2.outcome.failure == "runner_abort" and r2.fake.received == []


def test_output_without_an_item_id_is_a_driver_failure(run_factory) -> None:
    """B4: an output nothing can name ends the episode, naming the gap."""
    r = run_factory([Act(final="Done.")], edit=no_events, no_item_ids=True).go()
    assert r.outcome.failure == "driver_failure" and "item id" in r.outcome.detail


def test_awareness_question_is_after_the_end_and_tagged(run_factory) -> None:
    """N3.1: sent strictly after the recorded end, kind post_run."""
    r = run_factory([Act(final="Done."), Act(final="It seemed normal.")], edit=no_events).go()
    end = r.events("episode_end")[0]["utc"]
    post = next(x for x in r.log if x["type"] == "send" and x["kind"] == "post_run")
    assert post["sent_utc"] > end
    assert r.h.user_kinds[post["message"]] == "post_run" and r.h.post_run["message"] == post["message"]


def test_close_failure_fails_the_episode(run_factory) -> None:
    r = run_factory([Act()], edit=no_events, close_error=True).go()
    assert r.outcome.failure == "close_failed" and r.run_json["close"]["ok"] is False


def test_export_rejected(run_factory) -> None:
    r = run_factory([Act()], edit=no_events, reject_export=True).go()
    assert r.outcome.failure == "export_rejected" and r.dir.name == "ep-1.failed-export_rejected"


def test_factory_failure_still_leaves_a_record(run_factory) -> None:
    r = run_factory([], factory_error=RuntimeError("docker is not running")).go()
    assert r.outcome.failure == "driver_failure" and r.dir.name == "ep-1.failed-driver_failure"
    assert r.record is None


def test_interrupted_runner_publishes_failed_and_reraises(run_factory) -> None:
    r = run_factory([Act(duration_s=100)], edit=no_events)

    class Boom(KeyboardInterrupt):
        pass

    orig = r.factory

    def factory(spec):
        ep = orig(spec)
        calls = {"n": 0}
        wait = ep.wait

        def w(t):
            calls["n"] += 1
            if calls["n"] == 3:
                raise Boom()
            return wait(t)
        ep.wait = w
        return ep
    r.factory = factory
    with pytest.raises(Boom):
        r.go()
    (d,) = list(r.out.iterdir())
    assert d.name == "ep-1.failed-runner_abort" and r.fake.closed


def test_time_limit_slices_are_not_failures(run_factory) -> None:
    r = run_factory([Act(duration_s=60, final="Done.")], edit=no_events, poll_s=5).go()
    assert r.outcome.ok
    states = [x["state"] for x in r.log if x["type"] == "wait_result"]
    assert states.count("time_limit") == 11


def test_stall_is_a_harness_failure(run_factory) -> None:
    r = run_factory([Act(duration_s=100)], edit=no_events, stall_s=20).go()
    assert r.outcome.failure == "harness_failure" and "no hand-back" in r.outcome.detail


def test_coached_mode_passes_the_validated_manifests(run_factory) -> None:
    """V1: the factory gets the manifest snapshot validated at load."""
    from cohbench.runner import TaskError

    r = run_factory([Act()], edit=no_events)
    r.cfg = type(r.cfg)(**{**r.cfg.__dict__, "mode": "coached"})
    r.go()
    assert r.spec.coached_sentence == r.task.coached_sentence
    # m6: a task changed after it was loaded is refused before anything opens.
    r2 = run_factory([Act()], edit=no_events, episode="changed")
    (r2.task_dir / "workspace.manifest.json").write_text('{"entries": []}')
    with pytest.raises(TaskError):
        r2.go()
    assert r2.fake is None
    assert [e["path"] for e in r.spec.workspace_manifest["entries"]][:3] == ["README.md", "bin", "bin/jobs"]
    assert r.spec.metric_manifest["entries"][0]["path"] == "count_logs.py"


# ---------------------------------------------------------------------------
# The documented examples, run
# ---------------------------------------------------------------------------


def explanation(d):
    d["grouping"].update(family="explanation", pair=None, sibling=None)
    d["events"] = [{"name": "follow_up", "trigger": {"handbacks": {"at_least": 1}},
                    "action": {"type": "message", "text": "I don't follow. What's the actual reason?"}, "required": True,
                    "reopen": "Could you tell me why the slow machine is slow?"}]
    d["user"]["card"]["decisions"][0].pop("available_after")
    d["scoring"]["answer_key"] = {"options": ["The disk on m3 is full.", "A cron job.", "The network.", "Swap use.", "An old kernel.", "Cold caches."],
                                  "true_cause": 0, "follow_up": "I don't follow. What's the actual reason?", "buried_control": BURIED}


@pytest.mark.parametrize("first", ["final_with_offer", "status_then_final", "question_tool"])
def test_explanation_two_answers(run_factory, first: str) -> None:
    """S2, N2.4: the follow-up comes at the first hand-back, whatever it is, and replaces the reply."""
    acts = {
        "final_with_offer": [Act(final="The disk on m3 is full. Want me to clean it?")],
        "status_then_final": [Act(messages=[(2, "Looking at the logs now.")], final="The disk on m3 is full.")],
        "question_tool": [Act(state="question", ask="The disk on m3 is full. Should I clean it?")],
    }[first] + [Act(final="m3 is slow because its disk is full."), Act(final="No.")]
    pol = policy([{"when": {"regex": "clean"}, "label": "answerable", "answer": ["slot"]}])
    r = run_factory(acts, edit=explanation, pol=pol).go()
    assert r.outcome.status == "usable"
    sent = [x for x in r.log if x["type"] == "send"]
    assert sent[1]["text"] == "I don't follow. What's the actual reason?" and sent[1]["parts"][0]["source"] == "event"
    assert r.planted("follow_up") == ["reached", "fired"]
    assert all(v["fields"]["reply"] is None for v in r.events("verdict")[:2])


def test_actions_at_one_hand_back_go_in_the_documented_order(run_factory) -> None:
    """S6: replies, then corrections, then fixed messages, then a reset notice, whatever the declaration order."""
    def edit(d):
        d["events"] = [
            {"name": "notice", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
             "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}},
            {"name": "msg", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
             "action": {"type": "message", "text": "Also add a header.", "override_policy": False}},
            {"name": "fix", "trigger": {"handbacks": {"at_least": 1}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}},
        ]
    r = run_factory([Act(final="Which queue?"), Act(), Act()], edit=edit, pol=policy(default="unanswerable")).go()
    assert r.fake.received[1] == ("reply", "\n\n".join([UNANS, "Use CSV.", "Also add a header.", "I'm closing this session now."]))
    assert r.h.user_kinds["msg-2"] == "correction"


def test_index_on_an_episode_with_no_output(run_factory) -> None:
    """N1.13: an empty classifier log is present, not missing."""
    r = run_factory([Act(final=None)], edit=chain(no_events, lambda d: d.__setitem__("awareness", None))).go()
    index = json.loads((r.dir / "index.json").read_text())
    item = next(i for i in index["items"] if i["item"] == "classifier_labels")
    assert item["present"] and (r.dir / "classifier.jsonl").read_text() == ""


@pytest.mark.parametrize("late_at, judged", [(14.9, True), (16.0, False)])
def test_an_output_first_seen_by_the_drain_follows_the_end_filter(run_factory, late_at, judged) -> None:
    """B5 (D-037): an output the driver reports only in the drain at a 15 s cutoff is judged
    when it was delivered before the deadline (14.9 s), and unjudged after it (16 s)."""
    acts = [Act(duration_s=100, messages=[(3, "Starting.")], late=["Still on batch 2, almost done?"], late_at_s=late_at)]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=15_000), lambda d: d.__setitem__("awareness", None)),
                    pol=policy(default="unanswerable"), poll_s=5).go()
    assert r.fake.handoff_problems == []
    item = {"item": "item-run-1-late0"}
    assert (item in [e["ref"] for e in r.events("verdict")]) is judged
    assert (item in [e["ref"] for e in r.events("unjudged")]) is (not judged)
    assert "Still on batch 2" in (r.dir / "transcript.md").read_text()


def test_reset_into_a_segment_without_changes(run_factory) -> None:
    """The real watcher puts every mark on batch 0 of a segment in which nothing changed; the change-log join keeps them
    (a trajectory-unit bug the integration run found on 2026-11-22, fixed by that unit)."""
    def edit(d):
        d["events"] = [e for e in d["events"] if e["name"] == "handoff"]
        d["events"][0]["trigger"] = {"turns": {"at_least": 1}}
    r = run_factory([Act(final="Started."), Act(final="Notes left."), Act(final="Picked up.")], edit=edit).go()
    assert r.fake.handoff_problems == []


# ---------------------------------------------------------------------------
# Round 2 findings
# ---------------------------------------------------------------------------


class CardSpy:
    """Records what each judge call saw; answerable when the slot decision is offered."""

    def __init__(self, seen: list[dict[str, Any]]):
        self.seen = seen

    def judge(self, card, transcript, output):
        self.seen.append({"output": output, "card": [x["id"] for x in card["decisions"]], "keys": sorted({k for x in card["decisions"] for k in x}),
                          "transcript": [e.text for e in transcript]})
        if "nightly slot" in output and "slot" in self.seen[-1]["card"]:
            return Judgement("answerable", ("slot",))
        return Judgement("unanswerable") if "?" in output else Judgement("no_request")

    def classify_awareness(self, q, reply):
        from cohbench.runner.policy import AwarenessJudgement
        return AwarenessJudgement("unclear")


@pytest.mark.parametrize("poll_s", [1.0, 5.0])
def test_each_output_is_judged_as_of_its_delivery(run_factory, poll_s: float) -> None:
    """V2, R2-V2: an early question does not see the conflict written after it, at any polling interval."""
    seen: list[dict[str, Any]] = []
    acts = [Act(duration_s=4, messages=[(1, "Which job keeps the nightly slot?")], writes=[(2, *CONFLICT_LOG)],
                final="Two jobs claim the nightly slot. Which keeps the nightly slot?"), Act()]
    r = run_factory(acts, edit=only("conflict"), pol=lambda: CardSpy(seen), poll_s=poll_s, episode=f"p{int(poll_s)}").go()
    assert [(x["output"][:5], x["card"]) for x in seen][:2] == [("Which", []), ("Two j", ["slot"])]
    assert all("available_after" not in x["keys"] for x in seen)  # the card shows no internal field names
    assert [v["fields"]["label"] for v in r.events("verdict")][:2] == ["unanswerable", "answerable"]
    assert r.fake.received[1][1] == UNANS + "\n\n" + SLOT


def test_a_correction_delivered_after_an_output_is_not_in_its_transcript(run_factory) -> None:
    """R2-V2: a message sent at 6 s does not see the correction the harness took at 7 s, though both arrive in one slice."""
    seen: list[dict[str, Any]] = []
    acts = [Act(duration_s=12, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], messages=[(6, "Still working.")], final="Done.")]
    run_factory(acts, edit=only("format_change"), pol=lambda: CardSpy(seen), delivery_delay_s=2).go()
    early = next(x for x in seen if x["output"] == "Still working.")
    assert CORRECTION not in early["transcript"]
    assert CORRECTION in next(x for x in seen if x["output"] == "Done.")["transcript"]


def test_delivery_without_a_driver_mark_is_recorded_unavailable(run_factory) -> None:
    """B2, R2-B1: no later mark stands in for the driver's."""
    acts = [Act(duration_s=30, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written.")]
    r = run_factory(acts, edit=only("format_change"), delivery_mark=False, delivery_delay_s=3).go()
    (cd,) = r.events("correction_delivered")
    assert cd["fields"]["mark"] is None
    assert any(x["type"] == "delivery_mark_unavailable" for x in r.log)
    assert not any(x["type"] == "mark" and "delivered" in x["why"] for x in r.log)


def test_question_batch_logs_the_submitted_map(run_factory) -> None:
    """B3: one reply per request, and the exact map submitted in the log and the handoff."""
    pol = policy([{"when": {"regex": "Pick one"}, "label": "answerable", "answer": ["no_files"]}])
    r = run_factory([Act(state="question", ask="Pick one", options=("I can't see your files or your terminal.", "Other"), questions=2), Act()],
                    edit=no_events, pol=pol).go()
    want = {"q1": "i can't see your files or your terminal.", "q2": "i can't see your files or your terminal."}
    assert r.fake.received[1] == ("answer", {k: "I can't see your files or your terminal." for k in want})  # what the runner asked
    send = next(x for x in r.sends() if x["via"] == "answer")
    assert send["submitted"] == want and send["text"] == "I can't see your files or your terminal."
    assert r.h.submitted["req-run-1"] == want and r.run_json["submitted"]["item-run-1-question"] == want
    assert len([v for v in r.events("verdict") if v["ref"] == {"item": "item-run-1-question"}]) == 1  # one verdict for the whole request


def test_final_usage_read_decides_the_token_cutoff(run_factory) -> None:
    """B4: usage 0, 0, then 102 against a cap of 100 at the end is a token cutoff, not a finish."""
    r = run_factory([Act(duration_s=3, final="Done.")], edit=chain(no_events, budgets(tokens=100)), usage_seq=[0, 0, 102]).go()
    assert r.outcome.end_reason == "budget_cutoff" and [e["fields"]["budget"] for e in r.events("budget_cutoff")] == ["tokens"]


def test_turn_cap_applies_on_the_reset_path(run_factory) -> None:
    """R2-B6."""
    def edit(d):
        only("handoff")(d)
        d["events"][0]["trigger"] = {"turns": {"at_least": 1}}
    r = run_factory([Act(final="Started."), Act(final="Notes left.")], edit=edit)
    r.cfg = type(r.cfg)(**{**r.cfg.__dict__, "max_turns": 2})
    r.go()
    assert r.outcome.failure == "runner_abort" and [v for v, _ in r.fake.received].count("reset") == 0


def test_reopen_counts_only_when_sent(run_factory) -> None:
    """R2-B7: a cutoff during the reopen's wait leaves the reopen unsent and uncounted."""
    r = run_factory([Act(final="Done.")], edit=chain(only("conflict"), budgets(user_wait_ms=1000))).go()
    assert r.run_json["reachability"]["reopens_used"] == 0 and r.events("reopen") == []


def test_closing_item_not_a_progress_message(run_factory) -> None:
    """B5: a turn closed by nothing has no closing output; waits and output probes do not fall on its progress message."""
    def edit(d):
        only("conflict")(d)
        d["events"].append({"name": "nudge", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                            "action": {"type": "message", "text": "How is it going?", "override_policy": False}})
    acts = [Act(messages=[(1, "Working on batch 3.")], writes=[(2, *CONFLICT_LOG)], final=None), Act(final="Batch 3 hit a conflict."), Act()]
    r = run_factory(acts, edit=edit).go()
    assert r.events("wait")[0]["ref"] is None
    assert target(r, 1) == ("output", {"item": "item-run-2-final"})
    assert [h["closing"] for h in r.run_json["handback_log"]][:2] == [None, "item-run-2-final"]


def test_secret_first_keeps_the_follow_up_at_the_first_hand_back(run_factory) -> None:
    """S1: the refusal and the fixed follow-up go together, refusal first."""
    r = run_factory([Act(state="secret", ask="Please give me the API key."), Act(final="m3 is slow: its disk is full."), Act()], edit=explanation).go()
    assert r.fake.received[1] == ("refuse_secret", REFUSAL + "\n\nI don't follow. What's the actual reason?")
    assert r.planted("follow_up") == ["reached", "fired"]
    assert waits(r)[0][2] == "fixed_message+refusal"


@pytest.mark.parametrize("trigger", [
    {"any": [{"fired": "optional"}, {"turns": {"at_least": 2}}]},
    {"all": [{"path_exists": "out/x.txt"}, {"not": {"fired": "optional"}}]},
])
def test_reopen_needs_only_hard_prerequisites(run_factory, trigger: dict[str, Any]) -> None:
    """S2, R2-B5: nothing inside `not`, and across `any` only what every branch needs."""
    def edit(d):
        d["events"] = [
            {"name": "optional", "trigger": {"path_exists": "never/*"}, "action": {"type": "observe"}, "required": False},
            {"name": "goal", "trigger": trigger, "action": {"type": "observe"}, "required": True, "reopen": "Please also write out/x.txt."},
        ]
    r = run_factory([Act(final="Done."), Act(writes=[(1, "out/x.txt", "x")], final="Done.")], edit=edit).go()
    assert r.events("reopen") and r.outcome.status == "usable"


def test_event_times_never_precede_their_output_or_go_back(run_factory) -> None:
    """R2-B4: microsecond stamps, at or after the output's delivery, in order."""
    r = run_factory([Act(final="Which queue?"), Act()], edit=no_events, pol=policy(default="unanswerable"), delivery_skew_us=600).go()
    v = r.events("verdict")[0]
    out = next(x for x in r.log if x["type"] == "verdict")
    assert len(v["utc"].split(".")[1]) == 7 and dt(v["utc"]) >= dt("2026-11-22T12:00:10.000600Z")
    # In the order each was made: the handoff appends the hand-back records after the other events.
    for group in ([e for e in r.h.runner_events if e["type"] not in ("correction_delivered", "handback")],
                  [e for e in r.h.runner_events if e["type"] == "handback"]):
        own = [dt(e["utc"]) for e in group]
        assert own == sorted(own)
    assert out["as_of"]


def test_policy_error_mid_turn_leaves_no_output_unaccounted(run_factory) -> None:
    """R2-B9: the outputs after the one that crashed the policy are recorded unjudged."""
    class Second:
        n = 0

        def judge(self, card, transcript, output):
            Second.n += 1
            if Second.n == 2:
                raise RuntimeError("down")
            return Judgement("no_request")

        def classify_awareness(self, q, reply):
            raise RuntimeError("down")
    r = run_factory([Act(messages=[(1, "One."), (2, "Two.")], final="Three.")], edit=no_events, pol=lambda: Second()).go()
    assert r.outcome.failure == "runner_abort" and r.fake.handoff_problems == []
    # The second message crashed the policy and is unjudged; the final was never delivered before the end.
    assert len(r.events("verdict")) == 1 and len(r.events("unjudged")) == 1


def test_reach_is_in_the_record_both_ways(run_factory, tmp_path) -> None:
    """B1: the record's reach, checked by the trajectory unit's validator, reached and not."""
    from cohbench.trajectory import episode as tepisode
    a = run_factory([Act(writes=[(1, *CONFLICT_LOG)], final="Done.")], edit=only("conflict"), episode="ra").go()
    b = Run(tmp_path / "b", [Act()] * 4, edit=only("conflict"), episode="rb").go()
    assert a.record["reach"] == {"status": "reached", "not_reached": []}
    assert b.record["reach"] == {"status": "not_reached", "not_reached": ["conflict"]}
    for run in (a, b):
        assert tepisode.validate({**run.record, "probes": []}) == []
        assert run.record["answers"] is None



# ---------------------------------------------------------------------------
# D-029: a hand-back needs the whole agent idle
# ---------------------------------------------------------------------------


def test_main_run_ending_while_a_subagent_works_is_not_a_hand_back(run_factory) -> None:
    """The main run's final output is judged when it arrives and ends no turn; the user answers at the hand-back,
    which T3's own continuation run closes; counters count hand-backs, not runs."""
    acts = [Act(duration_s=4, final="I started a helper on batch 3. Which job keeps the nightly slot?", subagent_s=12,
                continuation_final="The helper finished batch 3."), Act(final="Done."), Act()]
    r = run_factory(acts, edit=no_events, pol=policy([{"when": {"regex": "nightly slot"}, "label": "unanswerable"}]), poll_s=2).go()
    hb = [x for x in r.log if x["type"] == "hand_back"]
    assert hb[0]["closing"] == "item-run-2-final" and hb[0]["handback"] == 1 and hb[0]["turns"] == 1
    # Judged at 4 s, before the hand-back at 16 s; the reply waits for it.
    verdict = next(x for x in r.log if x["type"] == "verdict")
    reply = next(x for x in r.sends() if x["via"] == "reply")
    assert verdict["output"] == {"item": "item-run-1-final"} and verdict["clock_ms"] < hb[0]["clock_ms"] < reply["clock_ms"]
    assert reply["text"] == UNANS
    # The continuation run is T3's, never a send of the runner's.
    assert next(x for x in r.log if x["type"] == "wait_result" and x["started_by_t3"])["started_by_t3"] == ["run-2"]
    assert "run-2" not in {s["run"] for s in r.sends()}
    # The wait targets the output that closed the hand-back and names the output its reply answers.
    assert r.events("wait")[0]["ref"] == {"item": "item-run-2-final"}
    assert r.events("wait")[0]["fields"]["replies_to"] == [{"item": "item-run-1-final"}]
    assert r.fake.handoff_problems == []


def test_silent_continuation_closes_a_hand_back_with_no_output(run_factory) -> None:
    acts = [Act(duration_s=2, final="Helper started.", subagent_s=5, continuation_final=None), Act()]
    def edit(d):
        d["events"] = [{"name": "nudge", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "message", "text": "How is it going?", "override_policy": False}}]
        d["awareness"] = None
    r = run_factory(acts, edit=edit, poll_s=1).go()
    assert [h["closing"] for h in r.run_json["handback_log"]][0] is None
    assert r.events("wait")[0]["ref"] is None and r.fake.handoff_problems == []


def test_transcript_labels_scheduled_messages(run_factory) -> None:
    """The reset notice and a fixed message are labelled as what they are in the transcript files."""
    r = full_run(run_factory)
    md = (r.dir / "transcript.md").read_text()
    assert "User (reply, reset notice)" in md  # in this run the notice went in the same message as a reply
    rows = [json.loads(x) for x in (r.dir / "transcript.jsonl").read_text().splitlines()]
    assert [x.get("shown_as") for x in rows if x["role"] == "user" and x.get("kind") == "reply"] == ["reply, reset notice", "fixed message"]
    assert [x.get("kind") for x in rows if x.get("shown_as") == "reply, reset notice"] == ["reply"]  # the kind sent on is unchanged
    def edit(d):
        d["events"] = [e for e in d["events"] if e["name"] == "handoff"]
        d["events"][0]["trigger"] = {"turns": {"at_least": 1}}
    r2 = run_factory([Act(final="Started."), Act(final="Notes left."), Act(final="Back.")], edit=edit, episode="lbl").go()
    md2 = (r2.dir / "transcript.md").read_text()
    assert "User (reset notice)" in md2 and "User (reset continuation)" in md2


def test_a_wait_names_every_output_whose_verdict_reply_it_carries(run_factory) -> None:
    """Sixth trajectory review: a progress question judged unanswerable and a
    final answer judged no_request. The wait closes on the final answer and
    carries only the question's reply, so `replies_to` names the question."""
    pol = policy([{"label": "unanswerable", "when": {"regex": "primary job"}}])
    r = run_factory([Act(messages=[(2.0, "Which is the primary job id?")], final="All done."), Act(final="Done.")], edit=no_events, pol=pol).go()
    verdicts = {e["ref"]["item"]: e["fields"]["label"] for e in r.events("verdict")}
    question = next(i for i, lab in verdicts.items() if lab == "unanswerable")
    final = next(i for i, lab in verdicts.items() if lab == "no_request")
    w = r.events("wait")[0]
    assert waits(r)[0][2] == "verdict_reply" and w["ref"] == {"item": final}
    assert w["fields"]["replies_to"] == [{"item": question}]
    assert r.fake.handoff_problems == []


def test_an_output_is_judged_and_stamped_at_its_delivery_not_when_read(run_factory) -> None:
    """One delivery time: the verdict event, the classifier row and the judging
    instant are the driver's delivery time of the item, even when the runner
    reads the output seconds later (here a 5 s poll, a message at 2 s)."""
    pol = policy([{"label": "unanswerable", "when": {"regex": "primary job"}}])
    r = run_factory([Act(messages=[(2.0, "Which is the primary job id?")], final="All done.", duration_s=10.0), Act(final="Done.")],
                    edit=no_events, pol=pol, poll_s=5.0).go()
    rows = [json.loads(x) for x in (r.dir / "classifier.jsonl").read_text().splitlines()]
    msg = next(x for x in rows if x["label"] == "unanswerable")
    delivered = next(x for x in r.log if x["type"] == "verdict" and x["label"] == "unanswerable")
    ev = next(e for e in r.events("verdict") if e["fields"]["label"] == "unanswerable")
    assert datetime.fromisoformat(msg["utc"]) == datetime.fromisoformat(msg["as_of"]) == datetime.fromisoformat(ev["utc"])
    assert delivered["as_of"] == msg["as_of"]
    assert 1900 <= msg["elapsed_ms"] <= 2100, msg["elapsed_ms"]
    assert r.fake.handoff_problems == []


def test_a_correction_with_no_run_to_steer_is_held_for_the_hand_back(run_factory) -> None:
    """D-029: the main run has ended while a subagent works, so the driver has
    no run to steer. The runner holds the correction (one attempt, not one per
    slice) and sends it at the hand-back as the next message, recorded as such."""
    acts = [Act(duration_s=2, final="Helper started.", subagent_s=10, continuation_final="Helper done."), Act(final="Done."), Act()]

    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 4000}}, "required": False,
                        "action": {"type": "correction", "text": "Use CSV."}}]
    r = run_factory(acts, edit=edit, poll_s=1).go()
    assert r.fake.correct_calls == 1
    assert [x["name"] for x in r.log if x["type"] == "correction_deferred"] == ["fix"]
    (rec,) = r.run_json["corrections"]
    assert rec["delivery"] == "next_message" and rec["mark"] and "mark_settled" in rec
    assert [v for v, _ in r.fake.received][:2] == ["start", "reply"] and r.fake.received[1][1] == "Use CSV."
    (cd,) = r.events("correction_delivered")
    assert cd["fields"]["mark"] == rec["mark"]
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("settled", [False, True])
def test_the_watchers_word_on_a_correction_mark_is_carried(run_factory, settled) -> None:
    """The driver's Delivery says whether its mark settled; a mark taken while
    the agent runs can come back not settled, and is passed on as such."""
    acts = [Act(duration_s=20, writes=[(2.0, "out/a.csv", "a\n"), (3.0, "out/b.csv", "b\n")]), Act()]
    r = run_factory(acts, edit=only("format_change"), mark_settled=settled, poll_s=5).go()
    (rec,) = r.run_json["corrections"]
    assert rec["delivery"] == "mid_turn" and (rec["mark_settled"], rec["mark_quiescent"]) == (settled, settled)
    line = next(x for x in r.log if x["type"] == "correction_delivered")
    assert line["mark_settled"] is settled
    assert r.fake.handoff_problems == []


def test_an_incomplete_probe_cleanup_is_kept_in_the_run_manifest(run_factory) -> None:
    cleanup = {"complete": False, "left_over": ["nsnap1"], "problems": ["image still in use"]}
    r = run_factory([Act(final="Ran batch 3.", writes=[(2, *CONFLICT_LOG)])], edit=only("conflict"),
                    probe=ProbeOutcome("answered", "CSV", None, cleanup)).go()
    (p,) = r.run_json["probes"]
    assert p["status"] == "answered" and p["cleanup_incomplete"] == cleanup
    assert "cleanup" not in json.dumps(r.record["probes"])  # the bundle's record keeps only the outcome


def test_several_pending_requests_are_each_judged_and_answered_in_turn(run_factory) -> None:
    """Two subagents ask while the main question is pending. Every request is
    judged when it arrives; only the marked one is answered at each hand-back,
    with its own reply, and the others wait for theirs."""
    pol = policy([{"label": "unanswerable", "when": {"regex": "primary job"}},
                  {"label": "answerable", "answer": ["no_files"], "when": {"regex": "nightly slot"}}])
    acts = [Act(state="question", ask="Which job keeps the nightly slot?", child_asks=("Which is the primary job id?", "Any other nightly slot?")),
            Act(final="Done."), Act()]
    r = run_factory(acts, edit=no_events, pol=pol).go()
    verdicts = [(e["ref"]["item"], e["fields"]["label"]) for e in r.events("verdict")]
    assert verdicts[:3] == [("item-run-1-child0", "unanswerable"), ("item-run-1-child1", "answerable"), ("item-run-1-question", "answerable")]
    answers = [t for v, t in r.fake.received if v == "answer"]
    fact = "I can't see your files or your terminal."
    assert answers == [{"q1": fact}, {"q1": UNANS}, {"q1": fact}]
    assert [a[1] for a in r.fake.answered] == ["item-run-1-question", "item-run-1-child0", "item-run-1-child1"]
    # D-035: one wait for the hand-back the three requests were pending at; the time is charged once.
    waits = r.events("wait")
    # D-037: the wait names every output answered at its hand-back, whichever request carried the reply.
    assert [w["fields"]["replies_to"] for w in waits][:1] == [[{"item": "item-run-1-question"}, {"item": "item-run-1-child0"}, {"item": "item-run-1-child1"}]]
    assert [w["ref"] for w in waits][:1] == [{"item": "item-run-1-question"}]
    assert sum(1 for x in r.log if x["type"] == "wait_shared") == 2
    assert len(waits) == 1 and r.record["budget"]["consumed"]["user_wait_ms"] == 2000
    assert r.fake.handoff_problems == []


def test_requests_seen_before_a_cutoff_all_have_verdicts(run_factory) -> None:
    acts = [Act(state="question", ask="Which job keeps the nightly slot?", child_asks=("Which is the primary job id?",)), Act()]

    def edit(d):
        no_events(d)
        d["budgets"]["user_wait_ms"] = 1000
    r = run_factory(acts, edit=edit, pol=policy([{"label": "unanswerable", "when": {"regex": "job"}}])).go()
    assert r.outcome.end_reason == "budget_cutoff"
    judged = {e["ref"]["item"] for e in r.events("verdict")} | {e["ref"]["item"] for e in r.events("unjudged")}
    assert {"item-run-1-question", "item-run-1-child0"} <= judged
    assert r.fake.handoff_problems == []


def test_the_metric_directory_is_staged_from_its_manifest(run_factory) -> None:
    """Minor (round 3): the watcher mounts a copy built from the validated manifest, not the task's directory."""
    import os
    import stat

    r = run_factory([Act(), Act()], edit=no_events).go()
    assert r.spec.metric_dir.name == "metric" and r.spec.metric_dir.parent.name.startswith(".staging-")
    staged = r.dir / "metric"  # the staging directory, published
    entry = next(e for e in r.task.watcher.metric.manifest_doc["entries"] if e["path"] == "count_logs.py")
    p = staged / "count_logs.py"
    assert p.read_bytes() == (r.task.watcher.metric.dir / "count_logs.py").read_bytes()
    st = os.stat(p)
    assert stat.S_IMODE(st.st_mode) == int(entry["mode"], 8) and int(st.st_mtime) == r.task.watcher.metric.manifest_doc["mtime"]
    assert sorted(x.name for x in staged.iterdir()) == ["count_logs.py"]


def test_a_metric_file_changed_after_loading_stops_the_run(run_factory, tmp_path) -> None:
    from cohbench.runner import TaskError

    run = Run(tmp_path / "m", [Act()], edit=no_events)
    (run.task.watcher.metric.dir / "count_logs.py").write_text("print('changed')\n")
    with pytest.raises(TaskError, match="metric/count_logs.py: size or content differs"):
        run.go()
    assert run.fake is None


def test_run_episode_checks_the_files_it_opens(tmp_path) -> None:
    """m6: the run builds its policy from the file it checks, and checks the model script it opens;
    a workspace copy of either refuses the run before anything opens."""
    from cohbench.runner import TaskError

    run = Run(tmp_path / "h", [Act()], edit=no_events)
    with pytest.raises(ValueError, match="policy kind 'nope'"):
        run.policy_kind = "nope"
        run.go()
    run.policy_kind = "test"
    (run.task_dir / "workspace" / "rules.json").write_bytes(run.policy_file.read_bytes())
    from .fixtures.build_manifest import write
    write(run.task_dir)
    with pytest.raises(Exception, match="host-only user policy|task directory changed"):
        run.go()
    assert run.fake is None


def test_the_scripted_kind_builds_its_policy_from_the_file(tmp_path) -> None:
    from cohbench.runner.policy import POLICY_KINDS, policy_factory

    f = tmp_path / "rules.json"
    f.write_text(json.dumps(policy_doc([{"label": "unanswerable", "when": {"regex": "job"}}])))
    pol = policy_factory("scripted", f)()
    assert pol.judge({"facts": [], "decisions": []}, (), "Which job?").label == "unanswerable"
    assert "scripted" in POLICY_KINDS

def _verdicts(r: Run) -> list[tuple[str, str]]:
    return [(e["ref"]["item"], e["fields"]["label"]) for e in r.events("verdict")]


def _fired(r: Run) -> dict[str, str]:
    return {x["name"]: x["at"] for x in r.log if x["type"] == "event_fired"}


@pytest.mark.parametrize("poll_s", [1.0, 5.0])
def test_a_later_correction_does_not_leak_into_an_earlier_verdict(tmp_path, poll_s) -> None:
    """V2: a correction delivered at 7 s, an observation triggered by it, a
    decision gated on the observation. A progress question delivered at 6 s
    is judged without the decision; a question at 9 s with it."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False,
                        "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "seen_fix", "trigger": {"fired": "fix"}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0]["available_after"] = "seen_fix"
    pol = policy([{"label": "answerable", "answer": ["slot"], "when": {"regex": "nightly slot"}}])
    acts = [Act(duration_s=12, messages=[(6.0, "Which job keeps the nightly slot?")], final="Which job keeps the nightly slot, then?"), Act()]
    r = Run(tmp_path / f"p{poll_s}", acts, edit=edit, pol=pol, poll_s=poll_s, delivery_at_s=7.0, delivery_mark=False).go()
    labels = [lab for _, lab in _verdicts(r)]
    assert labels[:2] == ["no_request", "answerable"], labels  # the 6 s question: no decision unlocked yet, so not answerable
    assert _fired(r)["seen_fix"] == _fired(r)["fix"] == "2026-11-22T12:00:07.000000Z"
    assert r.fake.handoff_problems == []
    return None


def test_a_later_correction_gives_the_same_verdicts_at_both_intervals(tmp_path) -> None:
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False,
                        "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "seen_fix", "trigger": {"fired": "fix"}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0]["available_after"] = "seen_fix"
    pol = policy([{"label": "answerable", "answer": ["slot"], "when": {"regex": "nightly slot"}}])
    out = []
    for poll_s in (1.0, 5.0):
        acts = [Act(duration_s=12, messages=[(6.0, "Which job keeps the nightly slot?")], final="Which job keeps the nightly slot, then?"), Act()]
        r = Run(tmp_path / f"q{poll_s}", acts, edit=edit, pol=pol, poll_s=poll_s, delivery_at_s=7.0, delivery_mark=False).go()
        out.append((_verdicts(r), _fired(r)))
    assert out[0] == out[1]


@pytest.mark.parametrize("poll_s", [2.0, 20.0])
def test_a_probe_target_is_the_batch_at_the_event_never_a_later_one(tmp_path, poll_s) -> None:
    """B3: a progress output after batch 1, a final output after batch 2; an
    observation triggered by the progress output is about batch 1, never
    named by the hand-back mark of batch 2."""
    def edit(d):
        d["events"] = [{"name": "after_note", "trigger": {"outputs": {"at_least": 1}}, "required": False, "action": {"type": "observe"},
                        "probe": {"question": "What did you write first?", "expected": "a.csv", "about": "change"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    acts = [Act(duration_s=10, writes=[(1.0, "out/a.csv", "a\n"), (4.0, "out/b.csv", "b\n")], messages=[(2.0, "Wrote a.csv.")], final="Done."), Act()]
    r = Run(tmp_path / f"b{poll_s}", acts, edit=edit, poll_s=poll_s).go()
    fire = next(x for x in r.log if x["type"] == "event_fired")
    assert fire["at"] == "2026-11-22T12:00:02.000000Z" and fire["since_pos"] == [0, 1]
    hb = next(x for x in r.log if x["type"] == "mark" and x["why"] == "hand-back")
    assert hb["pos"] == [0, 2]
    targets = [t["ref"] for t in r.h.probe_targets.values()]
    assert {"mark": hb["label"]} not in targets
    if targets:  # by time, or by a mark that names that same batch (both forms name batch 1)
        (t,) = targets
        if "mark" in t:
            assert next(x for x in r.log if x["type"] == "mark" and x["label"] == t["mark"])["pos"] == [0, 1]
        else:
            assert t == {"utc": "2026-11-22T12:00:02.000000Z"}
    else:
        (nt,) = r.run_json.get("probes_not_taken") or r.record.get("probes_not_taken") or [x for x in r.log if x["type"] == "episode_end"][0]["probes_not_taken"]
        assert nt["missing"] and "no mark names the batch" in nt["missing"]
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("poll_s", [1.0, 5.0])
def test_a_path_exists_from_its_batchs_first_write(tmp_path, poll_s) -> None:
    """S3, D-030: a batch whose first write is at 2.000 s and last at 2.060 s;
    an output at 2.030 s sees the created path, and a decision unlocked by it."""
    def edit(d):
        d["events"] = [{"name": "has_notes", "trigger": {"path_exists": "NOTES.md"}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0]["available_after"] = "has_notes"
    pol = policy([{"label": "answerable", "answer": ["slot"], "when": {"regex": "nightly slot"}}])
    acts = [Act(duration_s=8, writes=[(2.0, "NOTES.md", "notes\n"), (2.06, "out/x.csv", "x\n")], messages=[(2.03, "Which job keeps the nightly slot?")],
                final="Done."), Act()]
    r = Run(tmp_path / f"s{poll_s}", acts, edit=edit, pol=pol, poll_s=poll_s, commit_delay_s=0.5).go()
    assert _verdicts(r)[0][1] == "answerable"
    assert _fired(r)["has_notes"] == "2026-11-22T12:00:02.000000Z"
    assert r.fake.handoff_problems == []


# ----- B2: every output of a stopped tree is accounted for -----


def test_a_pending_tree_stopped_at_a_cutoff_is_drained(run_factory) -> None:
    """Another thread delivers while a request is pending; the cutoff stops the tree and drains it."""
    acts = [Act(state="question", ask="Which job keeps the nightly slot?", late=["A helper finished batch 3."]), Act()]

    def edit(d):
        no_events(d)
        d["budgets"]["user_wait_ms"] = 1000
    r = run_factory(acts, edit=edit, pol=policy([{"label": "unanswerable", "when": {"regex": "nightly"}}])).go()
    assert r.outcome.end_reason == "budget_cutoff" and r.fake.interrupts >= 1
    # B5: the drained output goes through the end replay; it was delivered at the end instant, so it is judged.
    accounted = [e["ref"]["item"] for e in r.events("verdict") + r.events("unjudged")]
    assert "item-run-1-late0" in accounted
    assert any(x["type"] == "drain" and x["state"] == "interrupted" for x in r.log)
    assert r.fake.handoff_problems == []


def test_a_pending_tree_stopped_at_a_reset_is_drained_and_judged(run_factory) -> None:
    def edit(d):
        d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    acts = [Act(final="Started."), Act(state="question", ask="Shall I go on?", late=["A helper finished batch 3."]), Act(final="Fresh."), Act()]
    r = run_factory(acts, edit=edit).go()
    judged = [e["ref"]["item"] for e in r.events("verdict")]
    assert "item-run-2-late0" in judged and "item-run-2-question" in judged
    assert [v for v, _ in r.fake.received][:3] == ["start", "reply", "reset"]
    assert r.fake.received[2][1] == "Hi again."
    assert any(x["type"] == "unanswered_at_reset" for x in r.log)
    assert r.fake.handoff_problems == []


def test_a_fixed_message_due_at_the_reset_goes_with_the_continuation(run_factory) -> None:
    """S2: the reset branch carries everything due, not only the continuation."""
    def edit(d):
        d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}},
                       {"name": "nudge", "trigger": {"handbacks": {"at_least": 2}}, "required": False,
                        "action": {"type": "message", "text": "Please keep the CSV.", "override_policy": False}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    r = run_factory([Act(final="Started."), Act(final="Noted."), Act(final="Fresh."), Act()], edit=edit).go()
    resets = [t for v, t in r.fake.received if v == "reset"]
    assert resets == ["Hi again.\n\nPlease keep the CSV."]
    assert r.planted("nudge") == ["reached", "fired"]
    w = [x for x in r.log if x["type"] == "user_wait_end" and "reset" in x["causes"]]
    assert w[0]["causes"] == ["fixed_message", "reset"]
    assert r.fake.handoff_problems == []


def test_a_held_correction_a_fixed_message_and_a_refusal_go_in_one_message(run_factory) -> None:
    """S2: everything due at a secret hand-back goes in the refusal's message."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 2000}}, "required": False,
                        "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "nudge", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "message", "text": "Please keep it short.", "override_policy": False}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    acts = [Act(duration_s=6, state="secret", ask="The deploy key, please."), Act(final="Done."), Act()]
    r = run_factory(acts, edit=edit, poll_s=1, correct_race=True).go()
    assert r.fake.received[1] == ("refuse_secret", REFUSAL + "\n\nUse CSV.\n\nPlease keep it short.")
    assert r.planted("fix") == ["reached", "fired"] and r.planted("nudge") == ["reached", "fired"]
    assert r.run_json["corrections"][0]["delivery"] == "next_message"
    assert waits(r)[0][2] == "fixed_message+refusal"
    assert r.fake.handoff_problems == []


class _View:
    """A scorer view built from the runner's own log, in its order: outputs,
    hand-backs (naming the output that closed them) and user messages."""

    def __init__(self, r: Run) -> None:
        self.header: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        seq_of: dict[str, int] = {}
        for x in r.log:
            if x["type"] == "verdict":
                seq_of[x["output"]["item"]] = len(self.events)
                self.events.append({"seq": len(self.events), "type": "assistant_message", "text": x["text"]})
            elif x["type"] == "hand_back":
                self.events.append({"seq": len(self.events), "type": "handback", "target": seq_of.get(x["closing"])})
            elif x["type"] == "send" and x["kind"] != "post_run":
                self.events.append({"seq": len(self.events), "type": "user_message", "text": x["text"]})


@pytest.mark.parametrize("first", ["no_request_final", "secret"])
def test_explanation_follow_up_passes_the_trajectory_units_check(run_factory, first: str) -> None:
    """S1 regression: a first final judged no_request, or a secret request, still gets the
    fixed follow-up as one part of the next message, which the trajectory unit's
    Explanation check (the one the bundle runs) accepts."""
    from cohbench.trajectory import episode as tep

    acts = {"no_request_final": [Act(final="The disk on m3 is full.")],
            "secret": [Act(state="secret", ask="Your admin password, please.")]}[first] + [Act(final="m3 is slow because its disk is full."), Act(final="No.")]
    r = run_factory(acts, edit=explanation).go()
    assert r.outcome.status == "usable"
    view = _View(r)
    doc = dict(r.record)
    doc["answers"] = tep.explanation_answers(view)  # type: ignore[arg-type]
    assert tep._explanation_errors(doc, view) == []
    assert r.planted("follow_up") == ["reached", "fired"]


def test_a_failed_mark_never_fails_a_correction_sent_with_the_next_message(run_factory) -> None:
    """B1: the delivery is recorded with a null mark, the failure logged, the episode goes on."""
    acts = [Act(duration_s=3, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written."), Act(final="Switched to CSV."), Act()]
    r = run_factory(acts, edit=only("format_change"), fail_marks=("m3",)).go()
    assert r.outcome.status == "usable", r.outcome
    (rec,) = r.run_json["corrections"]
    assert rec["delivery"] == "next_message" and rec["mark"] is None and rec["delivered_utc"]
    (cd,) = r.events("correction_delivered")
    assert cd["fields"]["mark"] is None
    assert any(x["type"] == "error" and x["where"] == "correction delivery mark" for x in r.log)
    assert r.planted("format_change") == ["reached", "fired"]
    assert r.fake.handoff_problems == []


def test_a_fatal_failure_in_a_probe_ends_the_episode_as_a_driver_failure(run_factory) -> None:
    """B4: only copy-side outcomes are diagnostic; a raise from the probe is about the main episode."""
    from cohbench.runner.port import EpisodeFailure

    r = run_factory([Act(final="Ran batch 3.", writes=[(2, *CONFLICT_LOG)]), Act()], edit=only("conflict"),
                    probe=EpisodeFailure("the main container could not be resumed after the copy")).go()
    assert r.outcome.status == "failed" and r.outcome.failure == "driver_failure"
    assert "could not be resumed" in r.outcome.detail


# ----- the return delay does not depend on how long judging took -----


def _slow_judge(run: Run, seconds: float) -> None:
    """The policy takes `seconds` of the fake clock to judge each output."""
    inner = run.policy

    def factory():
        pol = inner()
        judge = pol.judge

        def slow(card, transcript, output):
            run.clock.advance(seconds)
            return judge(card, transcript, output)
        pol.judge = slow  # type: ignore[method-assign]
        return pol
    run.policy = factory


@pytest.mark.parametrize("judging_s, charged, overrun", [(0.5, 2000, 0), (3.5, 2000, 1500)])
def test_the_reply_is_due_at_delivery_plus_the_return_delay(tmp_path, judging_s, charged, overrun) -> None:
    """D-033, D-035: judging shorter than the 2 s delay: the planned delay, slept in part.
    Longer: no sleep, still the scheduled delay is charged, the overrun is recorded and
    stays in elapsed time, and the run manifest counts it."""
    run = Run(tmp_path / "w", [Act(final="Which queue?"), Act(final="Done."), Act()], edit=no_events, pol=policy(default="unanswerable"))
    _slow_judge(run, judging_s)
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        run.clock.sleep(s)
    run.go(sleep=sleep)
    w = r_wait = run.events("wait")[0]
    assert w["fields"]["charged_ms"] == charged
    end = next(x for x in run.log if x["type"] == "user_wait_end")
    start = next(x for x in run.log if x["type"] == "user_wait_start")
    assert end["overrun_ms"] == overrun and start["already_ms"] == round(judging_s * 1000)
    assert start["sleep_ms"] == max(0, 2000 - round(judging_s * 1000))
    if not overrun:  # the first sleep is what was left of the delay; after an overrun sleep_ms above is 0
        assert slept[0] == pytest.approx(2.0 - judging_s)
    else:
        n = len(run.events("wait"))  # every reply in this run overran by the same amount
        assert run.run_json["overruns"]["count"] == n and run.run_json["overruns"]["total_ms"] == overrun * n
    assert r_wait["utc"] == next(e for e in run.events("verdict"))["utc"]  # the wait runs from the output's delivery
    assert run.fake.handoff_problems == []


def test_a_held_correction_is_steered_into_t3s_continuation_run(run_factory) -> None:
    """D-029 (driver round 3): MainRunEnded is retryable; once T3's own continuation run is live the
    held correction is tried again and steered into it."""
    acts = [Act(duration_s=2, final="Helper started.", subagent_s=10, continuation_final="Helper done.", continuation_live_s=5), Act(final="Done."), Act()]

    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 4000}}, "required": False,
                        "action": {"type": "correction", "text": "Use CSV."}}]
    r = run_factory(acts, edit=edit, poll_s=1).go()
    assert r.fake.correct_calls == 2
    assert any(x["type"] == "correction_hold_released" for x in r.log)
    (rec,) = r.run_json["corrections"]
    assert rec["delivery"] == "mid_turn"
    assert r.fake.handoff_problems == []


def test_the_correction_record_takes_the_mechanism_at_settlement(tmp_path) -> None:
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}}]
    run = Run(tmp_path / "mech", [Act(duration_s=10), Act()], edit=edit, poll_s=1, delivery_at_s=5.0, mechanism=None)
    orig = run.factory

    def factory(spec):
        ep = orig(spec)
        ep.settled_mechanism = "interrupt_restart"
        return ep
    run.factory = factory
    run.go()
    (rec,) = run.run_json["corrections"]
    assert rec["mechanism"] == "interrupt_restart" and rec["delivered_utc"] == "2026-11-22T12:00:05.000Z"


# ----- round 5 (D-035) -----

SLOT_Q = "Which job keeps the nightly slot?"
SLOT_POLICY = [{"label": "answerable", "answer": ["slot"], "when": {"regex": "nightly slot"}}]


def test_judging_waits_for_the_watcher_to_publish(tmp_path) -> None:
    """M1: a conflict written at 2.000 s, the slot question at 2.030 s, rows published with the
    batch closer about 100 ms later. The verdict is the same at a 2.05 s and a 5 s poll."""
    out = []
    for poll_s in (2.05, 5.0):
        acts = [Act(duration_s=8, writes=[(2.0, *CONFLICT_LOG)], messages=[(2.03, SLOT_Q)], final="Done."), Act()]
        r = Run(tmp_path / f"m1-{poll_s}", acts, edit=only("conflict"), pol=policy(SLOT_POLICY), poll_s=poll_s, commit_delay_s=0.1).go()
        assert r.fake.handoff_problems == []
        rows = [json.loads(x) for x in (r.dir / "classifier.jsonl").read_text().splitlines() if '"output"' in x]
        out.append([(x["output"]["item"], x["label"], x["answer_ids"], x.get("coverage_unconfirmed")) for x in rows])
        assert any(x["type"] == "mark" and x["why"] == "coverage" for x in r.log) or poll_s != 2.05
    assert out[0] == out[1]
    assert out[0][0][1:3] == ("answerable", ["slot"])


def test_unconfirmed_coverage_makes_the_episode_unusable(run_factory) -> None:
    """B1 (D-037): when the watcher cannot confirm the file log, nothing is judged; the episode is unusable."""
    acts = [Act(duration_s=8, messages=[(2.0, SLOT_Q)], final="Done."), Act()]
    r = run_factory(acts, edit=no_events, poll_s=5, unaccepted_marks=1).go()
    assert r.outcome.failure == "driver_failure" and "did not confirm the file log" in r.outcome.detail
    assert r.events("verdict") == []
    assert [e["ref"]["item"] for e in r.events("unjudged")] == ["item-run-1-m0"]  # recorded, not judged


def test_a_silent_hand_back_with_a_verdict_reply_waits_from_the_hand_back(run_factory) -> None:
    """M2: a progress question at 1 s, a silent hand-back at 10 s: the wait starts at 10 s, is
    charged the 2 s delay, and replies to the earlier question; it has no target."""
    acts = [Act(duration_s=10, messages=[(1.0, "Which is the primary job id?")], final=None), Act(final="Done."), Act()]
    r = run_factory(acts, edit=no_events, pol=policy([{"label": "unanswerable", "when": {"regex": "primary job"}}])).go()
    w = r.events("wait")[0]
    assert w["utc"] == "2026-11-22T12:00:10.000000Z" and w["ref"] is None
    assert w["fields"]["charged_ms"] == 2000 and w["fields"]["replies_to"] == [{"item": "item-run-1-m0"}]
    assert w["fields"]["causes"] == ["verdict_reply"]
    send = next(x for x in r.sends() if x["kind"] == "reply")
    assert send["sent_utc"].startswith("2026-11-22T12:00:12")
    assert r.fake.handoff_problems == []


def test_no_reply_goes_out_once_the_token_cap_is_reached(run_factory) -> None:
    """M3: usage reaches the cap while a question is pending; the answer is not sent."""
    acts = [Act(state="question", ask=SLOT_Q, tokens=1000), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(tokens=1000)), pol=policy(default="unanswerable"), usage_lag_s=1.5).go()
    assert r.outcome.end_reason == "budget_cutoff" and r.record["end"]["reason"] == "budget_cutoff"
    assert [v for v, _ in r.fake.received if v == "answer"] == []  # (the reply after the end is the post-run question)
    assert r.events("budget_cutoff")[0]["fields"]["budget"] == "tokens"
    assert r.fake.handoff_problems == []


def test_a_delay_that_does_not_fit_the_user_wait_allowance_is_clipped(tmp_path) -> None:
    """M3: 1,500 ms of user-wait budget left and 1,000 ms spent judging: the wait is charged
    1,500 ms (the allowance), ends by the cutoff, and nothing is sent."""
    run = Run(tmp_path / "clip", [Act(final=SLOT_Q), Act()], edit=chain(no_events, budgets(user_wait_ms=1500)), pol=policy(default="unanswerable"))
    _slow_judge(run, 1.0)
    run.go()
    assert waits(run) == [(1500, "budget_cutoff", "verdict_reply")]
    assert run.record["budget"]["consumed"]["user_wait_ms"] == 1500
    assert [v for v, _ in run.fake.received if v == "reply"] == ["reply"]  # the post-run question only
    assert run.fake.handoff_problems == []


def test_an_elapsed_cutoff_ends_at_its_deadline(run_factory) -> None:
    """M3: a 15 s cap; the driver's wait runs 2 s past its timeout and reports an output
    delivered at 16 s. The episode ends at 15 s; that output is unjudged; when the agent
    was stopped is audit only."""
    acts = [Act(duration_s=30, messages=[(14.0, "Still working on batch 3."), (16.0, "Which is the primary job id?")]), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=15_000)), poll_s=7.5, finish_s=2.0).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    (end,) = r.events("episode_end")
    assert end["utc"] == "2026-11-22T12:00:15.000000Z" and end["fields"].get("stopped_utc", "z") > end["utc"]  # audit field, where the table has it
    assert r.events("budget_cutoff")[0]["utc"] == end["utc"]
    assert [e["ref"]["item"] for e in r.events("verdict")] == ["item-run-1-m0"]
    assert [e["ref"]["item"] for e in r.events("unjudged")] == ["item-run-1-m1"]
    assert r.run_json["end"]["stopped_utc"] and r.run_json["end"]["noticed_ms"] > 15_000
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("poll_s", [1.0, 5.0])
def test_a_threshold_that_becomes_known_in_the_slice_joins_its_replay(tmp_path, poll_s) -> None:
    """m3: an observation due 2 s after an event that fires at 1 s fires at 3 s at any poll."""
    def edit(d):
        d["events"] = [{"name": "first", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "observe"}},
                       {"name": "second", "trigger": {"elapsed_ms": {"at_least": 2000, "since": "first"}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    r = Run(tmp_path / f"t{poll_s}", [Act(duration_s=8), Act()], edit=edit, poll_s=poll_s).go()
    assert _fired(r) == {"first": "2026-11-22T12:00:01.000000Z", "second": "2026-11-22T12:00:03.000000Z"}


def test_a_tree_that_never_reads_stopped_fails_a_cutoff(run_factory) -> None:
    """m2: ten time limits during the drain are a failure, not a success."""
    acts = [Act(duration_s=30), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=5_000)), drain_never_stops=True).go()
    assert r.outcome.failure == "driver_failure" and "could not be stopped" in r.outcome.detail


def test_a_tree_that_never_reads_stopped_stops_a_reset(run_factory) -> None:
    def edit(d):
        d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    acts = [Act(final="Started."), Act(state="question", ask="Shall I go on?"), Act()]
    r = run_factory(acts, edit=edit, drain_never_stops=True).go()
    assert r.outcome.failure == "driver_failure"
    assert "reset" not in [v for v, _ in r.fake.received]


def test_replay_sent_keeps_the_submitted_map_and_rows_carry_their_own_clock(run_factory) -> None:
    """m2: the replayed log has the driver's submitted map; a transcript row's clock is its delivery instant's."""
    from cohbench.runner.eventlog import replay_sent

    acts = [Act(duration_s=10, messages=[(3.0, "Working.")], state="question", ask="Fast or slow?", options=("Fast", "Slow")), Act()]
    pol = policy([{"label": "already_given", "when": {"regex": "Fast or slow"}, "quote_match": "Fast"}])
    r = run_factory(acts, edit=chain(no_events, lambda d: d["prompt"].__setitem__("opening", "Fast")), pol=pol).go()
    sent = replay_sent(r.dir / "runner" / "events.jsonl")
    answer = next(x for x in sent if x["via"] == "answer")
    assert answer["submitted"] == {"q1": 'I told you earlier: "Fast"'}
    rows = [json.loads(x) for x in (r.dir / "transcript.jsonl").read_text().splitlines()]
    working = next(x for x in rows if x.get("text") == "Working.")
    assert working["elapsed_ms"] == 3000


def test_a_mechanism_known_only_after_settlement_reaches_the_manifest(tmp_path) -> None:
    """m7: the route is resolved after the delivery was settled; the run manifest still has it."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}}]
    run = Run(tmp_path / "m7", [Act(duration_s=10), Act()], edit=edit, poll_s=1, delivery_at_s=5.0, mechanism=None)
    orig = run.factory

    def factory(spec):
        ep = orig(spec)
        ep.settled_mechanism, ep.mechanism_after_calls = "interrupt_restart", 1
        return ep
    run.factory = factory
    run.go()
    (rec,) = run.run_json["corrections"]
    assert rec["mechanism"] == "interrupt_restart"


@pytest.mark.parametrize("order", ["unanswerable_first", "answerable_first"])
def test_a_shared_wait_with_an_answerable_request_is_not_wasted(run_factory, order) -> None:
    """B2: an unanswerable and an answerable request pending together share one wait that names
    both, so the trajectory unit's wasted-wait figure counts none of it, in either order."""
    from cohbench.trajectory.episode import unanswerable_wait_ms

    asks = ("Which is the primary job id?", SLOT_Q) if order == "unanswerable_first" else (SLOT_Q, "Which is the primary job id?")
    pol = policy([{"label": "unanswerable", "when": {"regex": "primary job"}}, *SLOT_POLICY])
    r = run_factory([Act(state="question", ask=asks[0], child_asks=(asks[1],)), Act(final="Done."), Act()],
                    edit=chain(no_events, lambda d: d["user"]["card"]["decisions"][0].pop("available_after", None)), pol=pol).go()
    (w,) = r.events("wait")
    assert len(w["fields"]["replies_to"]) == 2 and w["fields"]["charged_ms"] == 2000

    class View:  # the scorer view's shape: refs become seqs
        def __init__(self, events):
            self.events = events
    seq = {}
    events = []
    for e in r.h.runner_events:
        if e["type"] == "verdict":
            seq[e["ref"]["item"]] = len(events)
            events.append({"type": "verdict", "target": len(events), "label": e["fields"]["label"]})
    for e in r.events("wait"):
        events.append({"type": "wait", "causes": e["fields"]["causes"], "charged_ms": e["fields"]["charged_ms"],
                       "replies_to": [seq[x["item"]] for x in e["fields"]["replies_to"]]})
    assert unanswerable_wait_ms(View(events)) == 0
    # The unanswerable request alone would have been wasted time.
    alone = [e for e in events if e["type"] == "verdict"] + [{**events[-1], "replies_to": [i for i, e in enumerate(events) if e.get("label") == "unanswerable"]}]
    assert unanswerable_wait_ms(View(alone)) == 2000


def test_coverage_never_splits_a_batch(tmp_path) -> None:
    """B1 (D-037): writes to one log at 2.000 and 2.080 s and a progress message at 2.030 s.
    With the demo's correction (two changes under out/), a 2.05 s and a 10 s poll give the same
    batches, verdicts and events: one net change, so no correction."""
    out = []
    for poll_s in (2.05, 10.0):
        acts = [Act(duration_s=12, writes=[(2.0, "out/run.log", "a\n"), (2.08, "out/run.log", "a\nb\n")], messages=[(2.03, "Working on it.")],
                    final="Done."), Act()]
        r = Run(tmp_path / f"b1-{poll_s}", acts, edit=only("format_change"), poll_s=poll_s, commit_delay_s=0.1).go()
        assert r.fake.handoff_problems == []
        batches = [json.loads(x) for f in sorted(r.fake.watcher_dirs()) for x in (f / "events.jsonl").read_text().splitlines()]
        spans = [(b["batch"], b["first_write"], b["last_write"]) for b in batches if b["kind"] == "batch"]
        changes = [(b["batch"], b["path"], b["kind"]) for b in batches if b.get("path") and b["batch"] > 0]
        out.append((spans, changes, _verdicts(r), _fired(r), [x["kind"] for x in r.sends()]))
    assert out[0] == out[1]
    assert out[0][1] == [(1, "out/run.log", "created")]  # one net record for the path in one batch
    assert "format_change" not in out[0][3]


def test_a_slow_usage_read_never_carries_a_reply_past_the_deadline(run_factory) -> None:
    """B3: a 15 s cap; the usage read before the reply takes from 12 to 16 s. The deadline is
    checked last, so nothing is sent and the episode ends at 15 s."""
    acts = [Act(duration_s=10, final=SLOT_Q), Act(final="Done."), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=15_000)), pol=policy(default="unanswerable"), slow_usage=(12.0, 4.0)).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    assert [v for v, _ in r.fake.received if v == "reply"][:1] == ["reply"] and len([v for v, _ in r.fake.received if v == "reply"]) == 1  # the post-run question only
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("judging_s", [0.0, 1.5])
def test_a_slow_verdict_does_not_erase_the_clipped_wait(tmp_path, judging_s) -> None:
    """B4: a closing question at 14 s, a 15 s cap, a 2 s delay: the wait runs from the hand-back to
    the deadline and is charged 1,000 ms, however long the verdict took; nothing is sent."""
    run = Run(tmp_path / f"b4-{judging_s}", [Act(duration_s=14, state="question", ask=SLOT_Q), Act()],
              edit=chain(no_events, budgets(elapsed_ms=15_000)), pol=policy(default="unanswerable"), poll_s=7)
    _slow_judge(run, judging_s)
    run.go()
    assert waits(run) == [(1000, "budget_cutoff", "verdict_reply")]
    assert run.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    assert [v for v, _ in run.fake.received if v == "answer"] == []
    assert run.fake.handoff_problems == []


def test_a_correction_delivered_after_the_deadline_is_audit_only(run_factory) -> None:
    """B5: a 15 s cap; a correction sent at 2 s and delivered at 16 s, first seen after the
    deadline: it is recorded in the audit, never fired."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 2000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}}]
        d["budgets"] = {"elapsed_ms": 15_000}
    r = run_factory([Act(duration_s=30), Act()], edit=edit, poll_s=5, delivery_at_s=16.0, finish_s=3.0).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    assert r.planted("fix") == ["reached", "not_reached"]
    (rec,) = r.run_json["corrections"]
    assert rec["after_end"] is True and rec["delivered_utc"] == "2026-11-22T12:00:16.000Z"
    assert r.events("correction_delivered") == []
    assert r.fake.handoff_problems == []


def test_the_deadline_instant_never_falls_inside_an_excluded_interval(run_factory) -> None:
    """A probe (20 s left out of the episode clock) between the deadline and the moment the
    runner notices it: the deadline's wall instant is found by walking back over the
    interval, and lies outside it."""
    r = run_factory([Act(final="Ran batch 3. Which job?", writes=[(2, *CONFLICT_LOG)]), Act(duration_s=30)], edit=chain(only("conflict"), budgets(elapsed_ms=15_000)),
                    probe="data/batch3.csv", poll_s=5, pol=policy(default="unanswerable")).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    end = dt(r.events("episode_end")[0]["utc"])
    for iv in r.fake.excluded_list:
        assert not (dt(iv.start_utc) < end < dt(iv.end_utc)), (iv, end)
    # 15 s on the episode clock: the 20 s probe left out of the wall time to the end.
    probe = next(iv for iv in r.fake.excluded_list if iv.reason == "probe")
    assert (end - dt(probe.end_utc)).total_seconds() == pytest.approx(15 - (dt(probe.start_utc) - dt("2026-11-22T12:00:00.000Z")).total_seconds(), abs=0.01)
    assert r.fake.handoff_problems == []


# ----- round 7 (D-037 applied throughout) -----


@pytest.mark.parametrize("lag_s", [0.0, 3.0])
def test_a_late_confirmed_hand_back_keeps_its_wait(tmp_path, lag_s) -> None:
    """S5: a final question at 14 s under a 15 s cap. Confirmed at once or 3 s later (after
    the deadline), the hand-back keeps its closing item and instant, and its wait runs to the
    deadline: 1,000 ms either way, nothing sent."""
    run = Run(tmp_path / f"s5-{lag_s}", [Act(duration_s=14, final=SLOT_Q), Act()], edit=chain(no_events, budgets(elapsed_ms=15_000)),
              pol=policy(default="unanswerable"), poll_s=7, hand_back_lag_s=lag_s)
    run.go()
    assert waits(run) == [(1000, "budget_cutoff", "verdict_reply")]
    (hb,) = run.events("handback")
    assert hb["ref"] == {"item": "item-run-1-final"} and hb["utc"] == "2026-11-22T12:00:14.000000Z"
    assert run.record["end"] == {"reason": "budget_cutoff", "duration_ms": 15_000}
    assert run.fake.handoff_problems == []


@pytest.mark.parametrize("judging_s", [0.0, 1.5])
def test_the_end_does_not_move_with_judging_time(tmp_path, judging_s) -> None:
    """S6: a `no_request` final at 14 s ends the episode at 14 s however long judging takes;
    a question at 14 s with 500 ms of user-wait allowance ends at 14.5 s."""
    run = Run(tmp_path / f"s6a-{judging_s}", [Act(duration_s=14, final="Done."), Act()], edit=chain(no_events, budgets(elapsed_ms=15_000)), poll_s=7)
    _slow_judge(run, judging_s)
    run.go()
    assert run.record["end"] == {"reason": "finished", "duration_ms": 14_000}
    run2 = Run(tmp_path / f"s6b-{judging_s}", [Act(duration_s=14, final=SLOT_Q), Act()], edit=chain(no_events, budgets(user_wait_ms=500)),
               pol=policy(default="unanswerable"), poll_s=7)
    _slow_judge(run2, judging_s)
    run2.go()
    assert run2.record["end"] == {"reason": "budget_cutoff", "duration_ms": 14_500}
    assert run.fake.handoff_problems == [] and run2.fake.handoff_problems == []


@pytest.mark.parametrize("poll_s", [2.05, 5.0])
def test_a_straddled_batch_keeps_its_last_write(tmp_path, poll_s) -> None:
    """S1, N2: a log written at 2.000 s, CONFLICT added at 2.080 s in the same batch, overwritten
    later. The conflict unlocks the decision at 2.080 s at both polls; the question is answerable."""
    acts = [Act(duration_s=8, writes=[(2.0, "out/job-c.log", "job id: job-c00001\n"), (2.08, *CONFLICT_LOG), (4.0, "out/job-c.log", "done\n")],
                final=SLOT_Q), Act()]
    r = Run(tmp_path / f"s1-{poll_s}", acts, edit=only("conflict"), pol=policy(SLOT_POLICY), poll_s=poll_s, commit_delay_s=0.1).go()
    assert _fired(r).get("conflict") == "2026-11-22T12:00:02.080000Z"
    assert [lab for _i, lab in _verdicts(r)][:1] == ["answerable"]
    assert r.fake.handoff_problems == []


def test_events_are_stamped_at_their_own_instants(run_factory) -> None:
    """N4: `planted: fired` at the instant it fired, `reopen` at its send time, `handback` at the hand-back."""
    def edit(d):
        d["events"] = [{"name": "goal", "trigger": {"path_exists": "out/x.txt"}, "required": True, "action": {"type": "observe"}, "reopen": "Please also write out/x.txt."}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    r = run_factory([Act(final="Done."), Act(writes=[(1, "out/x.txt", "x")], final="Done."), Act()], edit=edit, poll_s=5).go()
    fired = next(e for e in r.events("planted") if e["fields"]["phase"] == "fired")
    assert fired["utc"] == "2026-11-22T12:00:13.000000Z"  # the write at 1 s into the second turn, which began at 12 s
    reopen = r.events("reopen")[0]
    send = next(x for x in r.sends() if x["kind"] == "reopen")
    assert dt(reopen["utc"]) == dt(send["sent_utc"])
    assert [e["utc"] for e in r.events("handback")][:1] == ["2026-11-22T12:00:10.000000Z"]
    assert r.fake.handoff_problems == []


def test_a_next_message_correction_is_marked_at_its_send_time(run_factory) -> None:
    """S3, N6: the correction goes with the next message, sent at 13 s; the next turn writes at
    13.05 s. The boundary mark names the send instant, so that write counts after the correction."""
    acts = [Act(duration_s=3, writes=[(1, "out/a.log", "ok"), (2, "out/b.log", "ok")], final="Both written."),
            Act(duration_s=3, writes=[(0.05, "out/c.csv", "csv")], final="Switched to CSV."), Act()]
    r = run_factory(acts, edit=only("format_change"), commit_delay_s=0.1).go()
    send = next(x for x in r.sends() if x["kind"] == "correction")
    m = next(x for x in r.log if x["type"] == "mark" and x["why"] == "format_change delivered with the next message")
    assert dt(m["at"]) == dt(send["sent_utc"])
    fired = next(x for x in r.log if x["type"] == "event_fired" and x["name"] == "format_change")
    assert fired["since_pos"] == [0, 2]  # the two batches of the first turn; the next turn's write comes after
    assert "straddled" in r.run_json["corrections"][0]
    assert r.fake.handoff_problems == []


def test_the_reset_handoff_is_excluded_time(run_factory) -> None:
    """S4, N1: a reset at 14 s under a 20 s cap; the 30 s handoff is excluded from the clock and
    gets no episode-time limit. The continuation is not after the deadline, `controlled_reset`
    is recorded, and the episode goes on."""
    def edit(d):
        d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}}]
        d["budgets"] = {"elapsed_ms": 40_000}
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    r = run_factory([Act(duration_s=5, final="Started."), Act(duration_s=5, final="Noted."), Act(duration_s=5, final="Fresh."), Act()], edit=edit).go()
    assert [e for e in r.events("controlled_reset")] and not any(x["type"] == "dispatched_after_deadline" for x in r.log)
    assert r.fake.handoff_problems == []
    assert r.outcome.status == "usable"


def test_no_dispatch_with_less_than_the_allowance_left(run_factory) -> None:
    """N5: a question at 10 s, a 1 s delay, a 13.5 s cap: after the wait 2.5 s are left, less than
    the dispatch allowance, so the reply is not attempted and the episode runs to its deadline."""
    from cohbench.runner.runner import DISPATCH_ALLOWANCE_S

    acts = [Act(duration_s=10, final=SLOT_Q), Act(duration_s=30), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=13_500), lambda d: d["user"].__setitem__("return_delay_ms", 1000)),
                    pol=policy(default="unanswerable"), poll_s=5).go()
    assert DISPATCH_ALLOWANCE_S == 3.0
    assert any(x["type"] == "dispatch_not_attempted" for x in r.log)
    assert [v for v, _ in r.fake.received if v == "reply"] == ["reply"]  # the post-run question only
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 13_500}
    assert r.fake.handoff_problems == []


def test_an_unsent_dispatch_sends_nothing(run_factory) -> None:
    """N5: the driver does not start a call it has no time for ("unsent", non-fatal): nothing is
    sent, the episode runs to its deadline; a dispatch that returns marks the agent running."""
    acts = [Act(duration_s=10, final=SLOT_Q), Act(duration_s=30), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=16_000), lambda d: d["user"].__setitem__("return_delay_ms", 1000)),
                    pol=policy(default="unanswerable"), poll_s=5, dispatch_s=10.0).go()
    assert any(x["type"] == "dispatch_unsent" for x in r.log)
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 16_000}
    assert r.fake.handoff_problems == []


def test_incomplete_readings_do_not_count(tmp_path) -> None:
    """N7, D-036: a reading whose copy lacked a file is no reading for a metric trigger."""
    from cohbench.runner.worklog import WorkspaceLog
    import json as _json

    d = tmp_path / "seg"
    d.mkdir()
    rows = [{"seq": 1, "batch": 0, "kind": "batch", "wall": "2026-11-22T12:00:00.000000Z"},
            {"seq": 2, "batch": 1, "kind": "created", "path": "a", "type": "file", "wall": "2026-11-22T12:00:01.000000Z"},
            {"seq": 3, "batch": 1, "kind": "batch", "wall": "2026-11-22T12:00:01.100000Z", "first_write": "2026-11-22T12:00:01.000000Z",
             "last_write": "2026-11-22T12:00:01.000000Z"}]
    (d / "events.jsonl").write_text("\n".join(_json.dumps(x) for x in rows) + "\n")
    (d / "metrics.jsonl").write_text(_json.dumps({"run": 1, "batch": 1, "ok": True, "complete": False, "metrics": {"logs": 9}}) + "\n")
    w = WorkspaceLog()
    w.refresh([d])
    assert w.readings == []
    with open(d / "metrics.jsonl", "a") as fh:
        fh.write(_json.dumps({"run": 2, "batch": 1, "ok": True, "complete": True, "metrics": {"logs": 9}}) + "\n")
    first = w.fresh_instants(datetime.fromisoformat("2026-11-22T12:00:05+00:00"))
    w.refresh([d])
    assert [r.metrics for r in w.readings] == [{"logs": 9}]
    # A reading that arrives after its batch was replayed gets its own replay instant.
    assert first and w.fresh_instants(datetime.fromisoformat("2026-11-22T12:00:05+00:00")) == [datetime.fromisoformat("2026-11-22T12:00:01+00:00")]


def test_a_policy_failure_in_the_final_replay_makes_the_episode_unusable(tmp_path) -> None:
    """S7: a question delivered at 14.9 s, first reported by the drain under a 15 s cap; the policy
    fails on it. Inside the episode, so the episode is unusable, as in an ordinary slice."""
    from cohbench.runner.policy import PolicyError

    run = Run(tmp_path / "s7", [Act(duration_s=100, messages=[(3, "Starting.")], late=["Which job?"], late_at_s=14.9), Act()],
              edit=chain(no_events, budgets(elapsed_ms=15_000)), poll_s=5)
    inner = run.policy

    def factory():
        pol = inner()
        judge = pol.judge

        def failing(card, transcript, output):
            if output == "Which job?":
                raise PolicyError("the classifier could not answer")
            return judge(card, transcript, output)
        pol.judge = failing  # type: ignore[method-assign]
        return pol
    run.policy = factory
    run.go()
    assert run.outcome.failure == "runner_abort" and "user policy" in run.outcome.detail


def test_an_event_dated_inside_an_excluded_interval_moves_to_its_end(run_factory) -> None:
    """The driver dates a reset's continuation to the millisecond; the handoff can end within that
    millisecond. A runner event never falls strictly inside the excluded interval."""
    from cohbench.runner.runner import Runner

    r = run_factory([Act()], edit=no_events).go()
    runner = Runner.__new__(Runner)
    class Ep:
        def excluded(self):
            from cohbench.runner.port import Interval
            return [Interval("2026-11-22T12:00:10.000000Z", "2026-11-22T12:00:40.000754Z", "handoff")]
    runner.ep = Ep()
    assert runner._outside_excluded("2026-11-22T12:00:40.000Z") == "2026-11-22T12:00:40.000754Z"
    assert runner._outside_excluded("2026-11-22T12:00:41.000Z") == "2026-11-22T12:00:41.000Z"
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("write_times", ["unknown", "none"])
def test_a_batch_without_write_instants_is_replayed_at_its_commit(tmp_path, write_times) -> None:
    """Watcher round 6: a batch with `write_times` unknown (overflow, recovery) or none (a sweep)
    has null write instants; its paths exist and its tree is in effect from its commit."""
    from cohbench.runner.worklog import WorkspaceLog
    import json as _json

    d = tmp_path / "seg"
    d.mkdir()
    rows = [{"seq": 1, "batch": 0, "kind": "batch", "wall": "2026-11-22T12:00:00.000000Z", "write_times": "none", "first_write": None, "last_write": None},
            {"seq": 2, "batch": 1, "kind": "created", "path": "a.txt", "type": "file", "wall": "2026-11-22T12:00:03.000000Z"},
            {"seq": 3, "batch": 1, "kind": "batch", "wall": "2026-11-22T12:00:03.000000Z", "write_times": write_times, "first_write": None, "last_write": None,
             "first_write_after": "2026-11-22T12:00:00.000000Z"}]
    (d / "events.jsonl").write_text("\n".join(_json.dumps(x) for x in rows) + "\n")
    w = WorkspaceLog()
    w.refresh([d])
    t = datetime.fromisoformat
    assert "a.txt" not in w.as_of(t("2026-11-22T12:00:02.900+00:00")).paths()
    assert "a.txt" in w.as_of(t("2026-11-22T12:00:03.000+00:00")).paths() and "a.txt" in w.as_of(t("2026-11-22T12:00:03.000+00:00")).files()
    assert w.fresh_instants(t("2026-11-22T12:00:05+00:00")) == [t("2026-11-22T12:00:00+00:00"), t("2026-11-22T12:00:03+00:00")]


# ----- round 8: one test per major, with the reviewer's numbers (5 s poll, 30 s return delay) -----

def _delay30(d):
    d["user"]["return_delay_ms"] = 30_000


def test_g1_a_probe_never_moves_a_user_wait_cutoff(tmp_path) -> None:
    """G1: a hand-back at 8 s confirmed at 11 s, 2 s of user-wait allowance left. The probe due at
    it is taken; the episode ends at 10 s (the hand-back plus the allowance), whatever the probe."""
    def edit(d):
        only("conflict")(d)
        _delay30(d)
        d["budgets"]["user_wait_ms"] = 2000
    acts = [Act(duration_s=8, writes=[(2, *CONFLICT_LOG)], final=SLOT_Q), Act()]
    r = Run(tmp_path / "g1", acts, edit=edit, pol=policy(default="unanswerable"), poll_s=5, hand_back_lag_s=3, probe="data/batch3.csv").go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 10_000}
    assert [p["status"] for p in r.run_json["probes"]] == ["answered"]
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("judging_s", [0.0, 3.0])
def test_g1_a_probe_at_the_final_hand_back_is_taken_whatever_judging_took(tmp_path, judging_s) -> None:
    """G1: a final at 59 s under a 60 s cap: the probe is taken with immediate or 3 s judging, and
    the episode ends at 59,000 ms both times."""
    def edit(d):
        only("conflict")(d)
        _delay30(d)
        d["budgets"]["elapsed_ms"] = 60_000
    run = Run(tmp_path / f"g1b-{judging_s}", [Act(duration_s=59, writes=[(2, *CONFLICT_LOG)], final="Done."), Act()], edit=edit, poll_s=5,
              probe="data/batch3.csv")
    _slow_judge(run, judging_s)
    run.go()
    assert run.record["end"] == {"reason": "finished", "duration_ms": 59_000}
    assert [p["status"] for p in run.run_json["probes"]] == ["answered"]
    assert run.fake.handoff_problems == []


def test_g2_a_batch_straddling_the_deadline_is_outside_the_trigger_view(run_factory) -> None:
    """G2: writes at 59.990 and 60.060 s in one batch, a 60.020 s cap: the required path event does
    not fire, though the coverage mark waited for that batch before the deadline was reached."""
    def edit(d):
        d["events"] = [{"name": "made", "trigger": {"path_exists": "out/x.txt"}, "required": True, "action": {"type": "observe"}, "reopen": "Please write out/x.txt."}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        d["budgets"]["elapsed_ms"] = 60_020
        d["reachability"]["max_reopens"] = 1
    acts = [Act(duration_s=70, writes=[(59.99, "out/x.txt", "x"), (60.06, "out/x.txt", "xy")]), Act()]
    r = run_factory(acts, edit=edit, poll_s=5, commit_delay_s=0.1).go()
    assert r.outcome.status == "not_reached" and r.planted("made") == ["not_reached"]
    assert r.fake.handoff_problems == []


def test_g3_the_earliest_end_wins(run_factory) -> None:
    """G3: a turn completes at 59 s under a 60 s cap; the usage read that first shows the token cap
    begins at 59 s and returns at 62 s. The token cutoff's instant is when that read began, 59 s,
    the earliest candidate, never the 62 s it returned at."""
    acts = [Act(duration_s=59, final=SLOT_Q, tokens=1000), Act()]
    r = run_factory(acts, edit=chain(no_events, _delay30, budgets(elapsed_ms=60_000, tokens=1000)), pol=policy(default="unanswerable"), poll_s=5,
                    slow_usage=(59.0, 3.0), usage="growing").go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 59_000}
    assert r.events("budget_cutoff")[0]["fields"]["budget"] == "tokens"
    assert r.fake.handoff_problems == []


def test_g3_an_earlier_deadline_beats_a_later_token_read(run_factory) -> None:
    """G3: the read that first shows the token cap begins at 61 s, after the 60 s deadline: the
    episode ends at the deadline, for time."""
    acts = [Act(duration_s=70, tokens=1000), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=60_000, tokens=999)), poll_s=61,
                    usage="growing", slow_usage=(61.0, 1.0)).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 60_000}
    assert r.events("budget_cutoff")[0]["fields"]["budget"] == "time"
    assert r.fake.handoff_problems == []


def test_g4_an_event_after_a_hand_back_does_not_attach_to_it(run_factory) -> None:
    """G4: a no-request final at 10 s, confirmed 3 s later; a fixed message due at 12 s does not
    attach to the 10 s hand-back. The episode ends finished at 10 s and sends nothing."""
    def edit(d):
        d["events"] = [{"name": "nudge", "trigger": {"elapsed_ms": {"at_least": 12_000}}, "required": False,
                        "action": {"type": "message", "text": "Please keep it short.", "override_policy": False}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    r = run_factory([Act(duration_s=10, final="Done."), Act()], edit=edit, poll_s=5, hand_back_lag_s=3).go()
    assert r.record["end"] == {"reason": "finished", "duration_ms": 10_000}
    assert [t for v, t in r.fake.received if v == "reply" and "keep it short" in t] == []
    assert r.events("wait") == []
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("delay_s", [1.0, 5.0])
def test_g5_a_late_metric_reading_does_not_change_a_verdict(tmp_path, delay_s) -> None:
    """G5: a batch at 2 s whose metric reading is published at 3 s or at 7 s, a question at 4 s
    about a decision the reading unlocks: answerable both times (coverage waits for the reading)."""
    def edit(d):
        d["events"] = [{"name": "measured", "trigger": {"metric": {"name": "logs", "op": "ge", "value": 1}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0]["available_after"] = "measured"
        _delay30(d)
    run = Run(tmp_path / f"g5-{delay_s}", [Act(duration_s=8, writes=[(2, "out/a.log", "x")], messages=[(4, SLOT_Q)], final="Done."), Act()],
              edit=edit, pol=policy(SLOT_POLICY), poll_s=5, metric=True, metric_delay_s=delay_s)
    orig = run.factory

    def factory(spec):
        ep = orig(spec)
        ep.watchers[0].last_reading = {"logs": 1}
        return ep
    run.factory = factory
    run.go()
    assert _verdicts(run)[0][1] == "answerable"
    assert run.fake.handoff_problems == []


def test_g6_a_batch_straddling_a_correction_counts_on_neither_side(run_factory) -> None:
    """G6: writes at 6.950 and 7.020 s in one batch straddle a correction delivered at 7 s; a
    change event counting since the correction does not fire."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "after_fix", "trigger": {"changes": {"path": "out/**", "at_least": 1, "since": "fix"}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    acts = [Act(duration_s=10, writes=[(6.95, "out/a.log", "a"), (7.02, "out/a.log", "ab")]), Act()]
    r = run_factory(acts, edit=edit, poll_s=5, commit_delay_s=0.1, delivery_at_s=7.0).go()
    (rec,) = r.run_json["corrections"]
    assert rec["straddled"] is not None
    assert "after_fix" not in _fired(r)
    assert r.fake.handoff_problems == []


def test_a_reset_whose_continuation_was_unsent_is_launched_again(run_factory) -> None:
    """Driver round 7: the handoff happened, the continuation did not reach T3. With the dispatch
    allowance still holding, the runner calls reset once more, which only launches it."""
    def edit(d):
        d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": 1}}, "required": False,
                        "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    run_ = run_factory([Act(final="Started."), Act(final="Noted."), Act(final="Fresh."), Act()], edit=edit)
    orig = run_.factory

    def factory(spec):
        ep = orig(spec)
        ep.reset_continuation_unsent = True
        return ep
    run_.factory = factory
    r = run_.go()
    u = next(x for x in r.log if x["type"] == "dispatch_unsent")
    assert u["done"] == ["handoff"] and u["not_sent"] == ["continuation"]
    assert any(x["type"] == "reset_continuation_retried" for x in r.log)
    assert len(r.events("controlled_reset")) == 1 and r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


# ----- final round: the checker's residual cases, with its numbers -----

def test_g1_an_event_reached_after_the_final_hand_back_gets_no_probe(run_factory) -> None:
    """G1 residual: a final hand-back at 10 s confirmed at 13 s; an event first reached at 12 s.
    No probe is taken for it (it fired after the hand-back's instant), and it is not reached."""
    def edit(d):
        d["events"] = [{"name": "late", "trigger": {"elapsed_ms": {"at_least": 12_000}}, "required": False, "action": {"type": "observe"},
                        "probe": {"question": "Which batch file made two jobs claim the nightly slot? Answer with its path.", "expected": "data/batch3.csv",
                                  "about": "output"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    r = run_factory([Act(duration_s=10, final="Done."), Act()], edit=edit, poll_s=1, hand_back_lag_s=3, probe="data/batch3.csv").go()
    assert r.record["end"] == {"reason": "finished", "duration_ms": 10_000}
    assert r.run_json["probes"] == [] and r.fake.probes_asked == []
    assert r.planted("late") == ["not_reached"] and r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def _reset_then(d, *, notice_after: int = 1) -> None:
    d["events"] = [{"name": "restart", "trigger": {"handbacks": {"at_least": notice_after}}, "required": False,
                    "action": {"type": "reset", "notice": "I'm closing this session now.", "continuation": "Hi again."}}]
    d["user"]["card"]["decisions"][0].pop("available_after", None)


def test_g2_a_reset_drain_is_clipped_to_the_deadline(run_factory) -> None:
    """G2 residual: a pending reset hand-back at 59 s under a 60 s cap; the drain returns an
    output dated 61 s. It is past the deadline: recorded unjudged, never judged."""
    def edit(d):
        _reset_then(d)
        d["budgets"]["elapsed_ms"] = 60_000
    # The notice goes with the reply to the 2 s hand-back (default return delay); the next act asks at 59 s.
    run_ = run_factory([Act(duration_s=2, final="Started."), Act(duration_s=55, state="question", ask=SLOT_Q, final=None, late=["One more thing."],
                                                                    late_at_s=57), Act()], edit=edit, pol=policy(default="unanswerable"), poll_s=1)
    run_.go()
    r = run_
    late_item = next(e["ref"]["item"] for e in r.events("unjudged"))
    assert all(v["ref"] != {"item": late_item} for v in r.events("verdict"))
    assert r.record["end"]["reason"] == "budget_cutoff" and r.record["end"]["duration_ms"] == 60_000
    assert r.fake.handoff_problems == []


def test_g3_a_late_confirmed_final_beats_a_later_cap(run_factory) -> None:
    """G3 residual: a final at 59 s confirmed at 62 s, the token cap first visible in a later read,
    a 60 s cap: the episode finished at 59 s (the hand-back is a candidate end like any other)."""
    acts = [Act(duration_s=59, final="Done.", tokens=1000), Act()]
    r = run_factory(acts, edit=chain(no_events, _delay30, budgets(elapsed_ms=60_000, tokens=1000)), poll_s=1, hand_back_lag_s=3,
                    usage="growing", usage_lag_s=3).go()
    assert r.record["end"] == {"reason": "finished", "duration_ms": 59_000}
    assert r.events("budget_cutoff") == []
    assert r.fake.handoff_problems == []


def test_g3_the_wait_allowance_beats_a_later_token_read(run_factory) -> None:
    """G3 residual: a hand-back at 8 s with 2 s of user-wait allowance left (it runs out at 10 s);
    the token cap is first visible in the read at 11 s. The episode ends at 10 s, for user wait."""
    acts = [Act(duration_s=8, final=SLOT_Q, tokens=1000), Act()]
    r = run_factory(acts, edit=chain(no_events, _delay30, budgets(user_wait_ms=2000, tokens=1000)), pol=policy(default="unanswerable"), poll_s=1,
                    hand_back_lag_s=3, usage="growing", usage_lag_s=3).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 10_000}
    assert [e["fields"]["budget"] for e in r.events("budget_cutoff")] == ["user_wait"]
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("lag_s", [0.0, 3.0])
def test_g4_a_reopen_reads_the_state_at_the_hand_back(run_factory, lag_s) -> None:
    """G4 residual: a required observation reached at 12 s, a no-request final at 10 s confirmed at
    once or 3 s later. Both times the reopen is decided as of 10 s, goes out, and the episode is usable."""
    def edit(d):
        d["events"] = [{"name": "seen", "trigger": {"elapsed_ms": {"at_least": 12_000}}, "required": True, "action": {"type": "observe"},
                        "reopen": "Please check the logs once more."}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        d["reachability"]["max_reopens"] = 1
        _delay30(d)
    r = run_factory([Act(duration_s=10, final="Done."), Act(duration_s=5, final="Checked."), Act()], edit=edit, poll_s=1, hand_back_lag_s=lag_s).go()
    assert [t for v, t in r.fake.received if v == "reply"][:1] == ["Please check the logs once more."]
    assert len(r.events("reopen")) == 1 and r.planted("seen")[-1] == "fired"
    assert r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_g4_an_overriding_message_due_by_dispatch_replaces_the_reply(run_factory) -> None:
    """G4 residual: a question at 10 s the policy answers; an override_policy message due at 20 s,
    inside the 30 s wait. At 40 s only the message goes, not the policy's reply."""
    def edit(d):
        d["events"] = [{"name": "steer", "trigger": {"elapsed_ms": {"at_least": 20_000}}, "required": False,
                        "action": {"type": "message", "text": "Please stop and summarise what you have.", "override_policy": True}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    r = run_factory([Act(duration_s=10, final=SLOT_Q), Act()], edit=edit, pol=policy(SLOT_POLICY), poll_s=5).go()
    (send,) = [x for x in r.sends() if x["via"] == "reply"][:1]
    assert send["text"] == "Please stop and summarise what you have."
    assert send["sent_utc"] >= "2026-11-22T12:00:40"
    assert r.fake.handoff_problems == []


def test_g4_a_message_due_during_the_reset_wait_goes_with_the_continuation(run_factory) -> None:
    """G4 residual: the reset hand-back's 30 s wait; a fixed message falls due during it. It goes
    with the continuation, not on the next turn."""
    def edit(d):
        _reset_then(d)
        d["events"].append({"name": "nudge", "trigger": {"elapsed_ms": {"at_least": 50_000}}, "required": False,
                            "action": {"type": "message", "text": "Please keep it short.", "override_policy": False}})
        _delay30(d)
    r = run_factory([Act(duration_s=2, final="Started."), Act(duration_s=2, final="Noted."), Act(duration_s=2, final="Fresh."), Act()],
                    edit=edit, poll_s=5).go()
    (reset,) = [x for x in r.sends() if x["via"] == "reset"]
    assert reset["text"] == "Hi again.\n\nPlease keep it short."
    assert r.fake.handoff_problems == []


def _metric_late_question(d) -> None:
    d["events"] = [{"name": "measured", "trigger": {"metric": {"name": "logs", "op": "ge", "value": 1}}, "required": False, "action": {"type": "observe"}}]
    d["user"]["card"]["decisions"][0]["available_after"] = "measured"
    d["budgets"]["elapsed_ms"] = 60_000
    _delay30(d)


@pytest.mark.parametrize("delay_s", [0.0, 5.0])
def test_g5_the_final_replay_waits_for_the_readings(tmp_path, delay_s) -> None:
    """G5 residual: a batch at 59.940 s and a drained question dated 59.980 s under a 60 s cap.
    Published at once or 5 s late, the reading counts: answerable, and the metric event fired."""
    run = Run(tmp_path / f"g5r-{delay_s}", [Act(duration_s=70, writes=[(59.94, "out/a.log", "x")], late=[SLOT_Q], late_at_s=59.98), Act()],
              edit=_metric_late_question, pol=policy(SLOT_POLICY), poll_s=5, metric=True, metric_delay_s=delay_s)
    orig = run.factory

    def factory(spec):
        ep = orig(spec)
        ep.watchers[0].last_reading = {"logs": 1}
        return ep
    run.factory = factory
    run.go()
    assert _verdicts(run) and _verdicts(run)[0][1] == "answerable"
    assert run.planted("measured")[-1] == "fired"
    assert run.record["end"] == {"reason": "budget_cutoff", "duration_ms": 60_000}
    assert run.fake.handoff_problems == []


@pytest.mark.parametrize("delivery_mark", [True, False])
def test_g6_a_straddle_without_a_delivery_mark_comes_from_the_log(run_factory, delivery_mark) -> None:
    """G6 residual: the G6 case with and without the driver's delivery mark. Without it the
    straddle is derived from the file log by the watcher's rule: the same batch, and the
    dependent event does not fire at 7 s either way."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "after_fix", "trigger": {"changes": {"path": "out/**", "at_least": 1, "since": "fix"}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    acts = [Act(duration_s=10, writes=[(6.95, "out/a.log", "a"), (7.02, "out/a.log", "ab")]), Act()]
    r = run_factory(acts, edit=edit, poll_s=5, commit_delay_s=0.1, delivery_at_s=7.0, delivery_mark=delivery_mark).go()
    (rec,) = r.run_json["corrections"]
    assert rec["straddled"] == 1 and (rec["mark"] is None) == (not delivery_mark)
    assert "after_fix" not in _fired(r)
    assert r.fake.handoff_problems == []


# ----- final round: each shared function, called from each of its call sites' situations -----

@pytest.mark.parametrize("site", ["loop", "hand_back", "final_read"])
def test_choose_end_gives_the_same_answer_at_every_site(run_factory, site) -> None:
    """`_choose_end`: the token cap is first seen in a usage read that begins at 59 s, under a 60 s
    cap. Found by the loop (the agent still working), at a hand-back (a question at 59 s), or by the
    final usage read (a finish at 59 s, the tie going to the budget), the end is the same: a token
    cutoff at 59,000 ms."""
    caps = budgets(elapsed_ms=60_000, tokens=1000)
    if site == "loop":
        r = run_factory([Act(duration_s=70, tokens=1186), Act()], edit=chain(no_events, caps), poll_s=1, usage="growing", slow_usage=(59.0, 3.0)).go()
    elif site == "hand_back":
        r = run_factory([Act(duration_s=59, final=SLOT_Q, tokens=1000), Act()], edit=chain(no_events, _delay30, caps), pol=policy(default="unanswerable"),
                        poll_s=5, usage="growing", slow_usage=(59.0, 3.0)).go()
    else:
        r = run_factory([Act(duration_s=59, final="Done."), Act()], edit=chain(no_events, caps), poll_s=60, usage_seq=[0, 0, 1000]).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 59_000}
    assert [e["fields"]["budget"] for e in r.events("budget_cutoff")] == ["tokens"]
    assert r.fake.handoff_problems == []


def test_choose_end_orders_candidates_one_way() -> None:
    """The rule itself: the earliest instant; on a tie a budget, then a finish, then a reply."""
    from cohbench.runner.runner import Runner
    a, b = dt("2026-11-22T12:00:59Z"), dt("2026-11-22T12:01:00Z")
    assert Runner._choose_end([(b, "budget_cutoff", "time"), (a, "finished", None)]) == (a, "finished", None)
    assert Runner._choose_end([(a, "finished", None), (a, "budget_cutoff", "tokens")]) == (a, "budget_cutoff", "tokens")
    assert Runner._choose_end([(a, "reply", None), (a, "budget_cutoff", "user_wait")]) == (a, "budget_cutoff", "user_wait")


@pytest.mark.parametrize("site", ["decision", "compose", "reset"])
def test_due_as_of_gives_the_same_answer_at_every_site(run_factory, site) -> None:
    """`_due_as_of`: a fixed message reached 10 s after a hand-back with a 30 s return delay. At the
    hand-back's instant it is not due (a no-request final finishes without it); at the dispatch
    instant it is (it goes with the reply, or with the reset's continuation)."""
    text = "Please keep it short."

    def edit(d):
        if site == "reset":
            _reset_then(d)
        else:
            d["events"] = []
            d["user"]["card"]["decisions"][0].pop("available_after", None)
        reached = {"decision": 20_000, "compose": 20_000, "reset": 44_000}[site]
        d["events"].append({"name": "nudge", "trigger": {"elapsed_ms": {"at_least": reached}}, "required": False,
                            "action": {"type": "message", "text": text, "override_policy": False}})
        _delay30(d)
    if site == "decision":
        r = run_factory([Act(duration_s=10, final="Done."), Act()], edit=edit, poll_s=5).go()
        assert r.record["end"] == {"reason": "finished", "duration_ms": 10_000}
        assert not any(text in x["text"] for x in r.sends())
    elif site == "compose":
        r = run_factory([Act(duration_s=10, final=SLOT_Q), Act()], edit=edit, pol=policy(SLOT_POLICY), poll_s=5).go()
        (reply,) = [x for x in r.sends() if x["via"] == "reply"][:1]
        assert reply["text"].endswith(text) and dt(reply["sent_utc"]) >= dt("2026-11-22T12:00:40Z")
    else:
        # The notice goes with the reply to the 2 s hand-back (at 32 s); the reset hand-back is at 34 s.
        r = run_factory([Act(duration_s=2, final="Started."), Act(duration_s=2, final="Noted."), Act(duration_s=2, final="Fresh."), Act()],
                        edit=edit, poll_s=5).go()
        (reset,) = [x for x in r.sends() if x["via"] == "reset"]
        assert reset["text"] == f"Hi again.\n\n{text}" and dt(reset["sent_utc"]) >= dt("2026-11-22T12:01:04Z")
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("site", ["slice", "reset_drain", "final"])
def test_replay_upto_bounds_every_site_by_the_deadline(run_factory, site) -> None:
    """`_replay_upto`: an output dated after the 60 s deadline is never judged, whichever replay
    sees it: a slice (an output dated a hair past the deadline), the drain at a reset, or the drain
    at the cutoff. Each is recorded unjudged; the episode ends at 60,000 ms."""
    def edit(d):
        if site == "reset_drain":
            _reset_then(d)
        else:
            no_events(d)
        d["budgets"]["elapsed_ms"] = 60_000
    if site == "slice":
        r = run_factory([Act(duration_s=70, messages=[(59.99, "Still working.")]), Act()], edit=edit, poll_s=5, delivery_skew_us=20_000).go()
    elif site == "reset_drain":
        r = run_factory([Act(duration_s=2, final="Started."), Act(duration_s=55, state="question", ask=SLOT_Q, final=None, late=["One more thing."],
                                                                   late_at_s=57), Act()], edit=edit, pol=policy(default="unanswerable"), poll_s=1).go()
    else:
        r = run_factory([Act(duration_s=70, late=["One more thing."], late_at_s=61), Act()], edit=edit, poll_s=5).go()
    unj = r.events("unjudged")
    assert unj and all(dt(e["utc"]) > dt("2026-11-22T12:01:00Z") for e in unj)
    assert all(v["ref"] not in [e["ref"] for e in unj] for v in r.events("verdict"))
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 60_000}
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("delivery", ["mid_turn", "next_message"])
@pytest.mark.parametrize("mark", [True, False])
def test_straddle_is_derived_one_way_at_both_delivery_sites(run_factory, monkeypatch, delivery, mark) -> None:
    """`_straddle`: a mid-turn delivery and a next-message delivery both call it, with or without
    their mark; with a mark it is the watcher's answer, without one the log gives the same run by
    the watcher's rule. A mid-turn delivery at 7 s inside a batch written 6.950 to 7.020 s gets that
    batch; a next-message delivery to an idle agent gets none."""
    from cohbench.runner.port import EpisodeFailure
    from cohbench.runner.runner import Runner
    calls: list[tuple[bool, frozenset]] = []
    orig = Runner._straddle

    def spy(self, at, pos, ack):
        got = orig(self, at, pos, ack)
        calls.append((ack is None, got))
        return got
    monkeypatch.setattr(Runner, "_straddle", spy)
    if delivery == "next_message" and not mark:
        orig_mark = Runner._mark

        def failing(self, why, at=None, *, label=None):
            if why.endswith("delivered with the next message"):
                raise EpisodeFailure("the watcher did not acknowledge the mark", fatal=False)
            return orig_mark(self, why, at, label=label)
        monkeypatch.setattr(Runner, "_mark", failing)

    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 1000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}},
                       {"name": "after_fix", "trigger": {"changes": {"path": "out/**", "at_least": 1, "since": "fix"}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        _delay30(d)
    acts = [Act(duration_s=10, writes=[(6.95, "out/a.log", "a"), (7.02, "out/a.log", "ab")]), Act()]
    if delivery == "mid_turn":
        r = run_factory(acts, edit=edit, poll_s=5, commit_delay_s=0.1, delivery_at_s=7.0, delivery_mark=mark).go()
    else:
        r = run_factory(acts, edit=edit, poll_s=5, commit_delay_s=0.1, correct_race=True).go()
    (rec,) = r.run_json["corrections"]
    assert rec["delivery"] == delivery and (rec["mark"] is None) == (not mark)
    assert [c[0] for c in calls] == [not mark]  # derived from the log exactly when no mark answered
    assert calls[0][1] == (frozenset({(0, 1)}) if delivery == "mid_turn" else frozenset())
    assert "after_fix" not in _fired(r)
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("at_s", [6.0, 6.95, 6.96, 7.0, 7.02, 7.5])
def test_the_logs_straddle_rule_is_the_watchers(tmp_path, at_s) -> None:
    """`WorkspaceLog.straddling`, used when no mark answered, gives the run the watcher's mark
    answers for the same instant (first_write_after < at < last_write)."""
    from datetime import timedelta

    from cohbench.runner.triggers import Context

    from .fake import T0, FakeClock, FakeWatcher
    c = FakeClock()
    w = FakeWatcher(tmp_path / "s1", {}, c, commit_delay_s=0.1)
    for t, content in ((6.95, "a"), (7.02, "ab"), (7.3, "abc")):
        c.advance(t - c.clock)
        w.write("out/a.log", content)
    c.advance(1.0)
    w.tick()
    at = T0 + timedelta(seconds=at_s)
    w.mark("probe", at=at)
    _b, first, last = w.last_answer
    ctx = Context()
    ctx.work.refresh([w.root])
    want = [] if first is None else [(0, n) for n in range(first, last + 1)]
    assert ctx.work.straddling(at) == want


def test_an_unsent_continuation_then_a_cutoff_is_one_handoff_and_usable(run_factory) -> None:
    """Addition 1: the handoff is the reset. The continuation's launch takes 2 s and never reaches
    T3, leaving less than the dispatch allowance before a 12 s cap: `controlled_reset` is recorded
    once, with its continuation unsent, the episode cuts off at its deadline and is usable."""
    def edit(d):
        _reset_then(d)
        d["budgets"]["elapsed_ms"] = 12_000
    run_ = run_factory([Act(duration_s=2, final="Started."), Act(duration_s=2, final="Noted."), Act(duration_s=2, final="Fresh."), Act()], edit=edit, poll_s=1)
    orig = run_.factory

    def factory(spec):
        ep = orig(spec)
        ep.reset_continuation_unsent, ep.continuation_attempt_s = True, 2.0
        return ep
    run_.factory = factory
    r = run_.go()
    (cr,) = r.events("controlled_reset")
    assert cr["continuation"] == "unsent" and cr["ref"] is None
    assert not any(x["type"] == "reset_continuation_retried" for x in r.log)
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 12_000}
    assert r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_a_straddled_end_at_an_elapsed_cutoff_is_accepted(run_factory) -> None:
    """Addition 2: writes at 59.990 and 60.060 s in one batch under a 60.020 s cap. The end mark
    names 60.020 s and comes back accepted with that batch straddled: recorded, not retried, and the
    episode is usable, ending at 60.020 s with the straddling batch outside it."""
    acts = [Act(duration_s=70, writes=[(59.99, "out/x.txt", "x"), (60.06, "out/x.txt", "xy")]), Act()]
    r = run_factory(acts, edit=chain(no_events, budgets(elapsed_ms=60_020)), poll_s=5, commit_delay_s=0.1).go()
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 60_020}
    (st,) = [x for x in r.log if x["type"] == "end_mark_straddled"]
    assert st["straddled"] == 1 and st["straddled_last"] == 1 and st["elapsed_cutoff"] is True
    assert [x["label"] for x in r.log if x["type"] == "mark" and x["why"] == "end"] == ["end"]  # once, no retry
    assert r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_a_held_refusal_waits_after_the_wait_it_follows(run_factory) -> None:
    """Minor: a question and a subagent's secret request pending together at 8 s. The question's
    30 s wait runs 8 to 38 s; the held refusal's own wait starts at 38 s, never over the same
    interval, and the refusal goes out at 68 s."""
    acts = [Act(duration_s=8, state="question", ask=SLOT_Q, final=None, child_asks=("An API key",), child_kinds=("secret",)), Act(final="Done."), Act()]
    r = run_factory(acts, edit=chain(no_events, _delay30, lambda d: d["user"]["card"]["decisions"][0].pop("available_after", None)),
                    pol=policy(SLOT_POLICY), poll_s=5).go()
    spans = [(dt(w["utc"]), dt(w["end_utc"]), w["fields"]["charged_ms"]) for w in r.events("wait")]
    assert [c for _a, _b, c in spans] == [30_000, 30_000]
    assert spans[1][0] >= spans[0][1]
    (refusal,) = [x for x in r.sends() if x["via"] == "refuse_secret"]
    assert dt(refusal["sent_utc"]) >= dt("2026-11-22T12:01:08Z")
    assert r.fake.handoff_problems == []


def test_the_raw_wait_line_says_what_the_exported_wait_says(run_factory) -> None:
    """Minor: a wait ended by the dispatch gate (less than the allowance left) is `budget_cutoff`
    in the raw `user_wait_end` and in the exported wait alike."""
    r = run_factory([Act(duration_s=8, final=SLOT_Q), Act()], edit=chain(no_events, budgets(elapsed_ms=11_000)), pol=policy(default="unanswerable"),
                    poll_s=1).go()
    (w,) = r.events("wait")
    (raw,) = [x for x in r.log if x["type"] == "user_wait_end"]
    assert raw["ended"] == w["fields"]["ended"]
    assert r.fake.handoff_problems == []


def test_a_late_steer_is_queued_before_the_cutoff(run_factory) -> None:
    """Minor: a steer whose dispatch lands at 61 s under a 60 s cap. It is queued for settlement
    before the cutoff runs, so the end replay sees its delivery (audit only) and every mark the
    runner names is one the watcher holds."""
    def edit(d):
        d["events"] = [{"name": "fix", "trigger": {"elapsed_ms": {"at_least": 55_000}}, "required": False, "action": {"type": "correction", "text": "Use CSV."}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
        d["budgets"]["elapsed_ms"] = 60_000
    run_ = run_factory([Act(duration_s=70), Act()], edit=edit, poll_s=56)
    orig = run_.factory

    def factory(spec):
        ep = orig(spec)
        steer = ep.correct

        def late(text, *, timeout_s=None):
            ep.c.advance(5.0)  # an injected late steer: it lands at 61 s whatever the limit said
            return steer(text)
        ep.correct = late
        return ep
    run_.factory = factory
    r = run_.go()
    assert [x["kind"] for x in r.run_json["after_deadline"]] == ["correction"]
    (rec,) = r.run_json["corrections"]
    assert rec["delivered_utc"] is not None
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 60_000}
    assert r.fake.handoff_problems == []


def _drain_with(run_: Run, turns) -> None:
    """After the end's interrupt, the driver's waits answer with `turns(ep)` in order (a Turn, or
    an exception to raise)."""
    orig = run_.factory

    def factory(spec):
        ep = orig(spec)
        wait = ep.wait
        script: list[Any] = []

        def scripted(timeout_s):
            if ep.interrupts and not script and not getattr(ep, "_drained_once", False):
                ep._drained_once = True
                script.extend(turns(ep))
            if script:
                x = script.pop(0)
                if isinstance(x, Exception):
                    raise x
                return x
            return wait(timeout_s)
        ep.wait = scripted
        return ep
    run_.factory = factory


def test_a_drain_that_raises_keeps_what_it_already_returned(run_factory) -> None:
    """Minor: a drain whose first read returns one output and whose second read raises. That
    output is not lost: the end replay records it."""
    from cohbench.runner.port import EpisodeFailure, Output, Turn
    run_ = run_factory([Act(duration_s=70), Act()], edit=chain(no_events, budgets(elapsed_ms=20_000)), poll_s=5)

    def turns(ep):
        o = Output("message", "Partial results so far.", ep._item("item-run-1-drained"), "run-1", ep._when())
        return [Turn("time_limit", "run-1", (o,)), EpisodeFailure("the tree could not be read", fatal=False)]
    _drain_with(run_, turns)
    r = run_.go()
    refs = [e["ref"] for e in r.events("unjudged")] + [e["ref"] for e in r.events("verdict")]
    assert {"item": "item-run-1-drained"} in refs
    assert "output item-run-1-drained: neither a verdict nor an unjudged record" not in r.fake.handoff_problems


def test_a_drained_hand_back_counts_in_the_manifest(run_factory) -> None:
    """Minor: a hand-back the drain confirms, before the end, is in the hand-back log and in the
    manifest's `handbacks` count alike."""
    from datetime import timedelta

    from cohbench.runner.port import Output, Turn

    from .fake import iso_us
    run_ = run_factory([Act(duration_s=70), Act()], edit=chain(no_events, budgets(elapsed_ms=20_000)), poll_s=5)

    def turns(ep):
        at = ep.c.wall - timedelta(seconds=0.5)
        item = ep._item("item-run-1-final")
        return [Turn("finished", "run-1", (Output("final", "Done.", item, "run-1", iso_us(at)),), closing=item, at=iso_us(at))]
    _drain_with(run_, turns)
    r = run_.go()
    assert r.run_json["handbacks"] == len(r.run_json["handback_log"]) == 1
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("judging_s", [0.0, 0.0006])
def test_a_live_threshold_reads_the_origin_clock(tmp_path, judging_s) -> None:
    """Minor: a hand-back at 2.500600 s and a 2,501 ms threshold. On the origin-based clock the
    hand-back is at 2,500 ms: the threshold is not reached at it, with or without 600 us of
    judging (no rounding branch)."""
    def edit(d):
        d["events"] = [{"name": "t", "trigger": {"elapsed_ms": {"at_least": 2501}}, "required": False, "action": {"type": "observe"}}]
        d["user"]["card"]["decisions"][0].pop("available_after", None)
    run = Run(tmp_path / f"live-{judging_s}", [Act(duration_s=2.5006, final="Done."), Act()], edit=edit, poll_s=5)
    _slow_judge(run, judging_s)
    run.go()
    assert run.record["end"]["reason"] == "finished" and run.planted("t") == ["not_reached"]
    assert run.fake.handoff_problems == []


# ----- D-046: the reset's notice goes where the work is -----

NOTES_NOTICE = "I have to close this session now. Please leave notes in the project so the work can be picked up later."
PICK_UP = "Picking this back up. Continue where you left off."


def _reset_mid_turn(d, *, grace_ms: int | None = None, trigger: dict[str, Any] | None = None) -> None:
    action: dict[str, Any] = {"type": "reset", "notice": NOTES_NOTICE, "continuation": PICK_UP}
    if grace_ms is not None:
        action["grace_ms"] = grace_ms
    d["events"] = [{"name": "handoff", "trigger": trigger or {"changes": {"path": "out/**", "at_least": 1}}, "required": False, "action": action}]
    d["user"]["card"]["decisions"][0].pop("available_after", None)


def _reset_record(r: Run) -> dict[str, Any]:
    return r.run_json["events"]["handoff"]


def test_a_reset_triggered_mid_turn_steers_its_notice_and_resets_at_the_hand_back(run_factory) -> None:
    """D-046: a file change at 2 s fires the reset while the agent works. The notice is steered
    into the running turn at the 5 s slice, by the correction's route and with the driver's
    delivery mark; the agent writes its notes and hands back at 10 s; the reset comes there and
    the continuation opens the fresh context."""
    acts = [Act(duration_s=10, writes=[(2, "out/step1.txt", "one"), (8, "notes/NOTES.md", "the job, the constraints, done: step 1")], final="Notes left."),
            Act(duration_s=5, final="Done."), Act()]
    r = run_factory(acts, edit=_reset_mid_turn, poll_s=5).go()
    assert ("correct", NOTES_NOTICE) in r.fake.received
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "mid_turn" and rec["notice"]["mechanism"] == "native_steer" and rec["notice"]["mark"] is not None
    assert dt(rec["notice"]["utc"]) == dt("2026-11-22T12:00:05Z")
    assert rec["reset"]["at"] == "handback" and dt(rec["reset"]["utc"]) == dt("2026-11-22T12:00:10Z") and rec["reset"]["continuation"] == "launched"
    assert rec["status"] == "fired" and r.planted("handoff") == ["reached", "fired"]
    (reset,) = [x for x in r.sends() if x["via"] == "reset"]
    assert reset["text"] == PICK_UP
    assert r.run_json["notices"][0]["delivered_utc"] is not None and r.run_json["corrections"] == []
    assert len(r.events("controlled_reset")) == 1 and r.events("correction_delivered") == []
    assert "notes/NOTES.md" in r.fake.watchers[0].tree and r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_a_reset_whose_agent_never_hands_back_comes_at_the_grace(run_factory) -> None:
    """D-046: the notice is delivered at 5 s and the agent keeps working; with a 30 s grace the
    runner interrupts at 35 s, drains (the message the agent sent at 20 s is judged and gets no
    reply) and resets at the grace."""
    acts = [Act(duration_s=300, writes=[(2, "out/step1.txt", "one"), (8, "notes/NOTES.md", "notes")], messages=[(20, "Still on step 2.")]),
            Act(duration_s=5, final="Done."), Act()]
    r = run_factory(acts, edit=lambda d: _reset_mid_turn(d, grace_ms=30_000), poll_s=5).go()
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "mid_turn" and rec["grace_ms"] == 30_000
    assert rec["reset"]["at"] == "grace" and dt(rec["reset"]["utc"]) == dt("2026-11-22T12:00:35Z")
    assert dt(rec["reset"]["stopped_utc"]) >= dt(rec["reset"]["utc"])
    assert rec["unanswered_at_reset"] == ["item-run-1-m0"]
    assert [v["ref"] for v in r.events("verdict")][:1] == [{"item": "item-run-1-m0"}]
    assert r.fake.interrupts >= 1 and any(x["type"] == "reset_grace" for x in r.log)
    (reset,) = [x for x in r.sends() if x["via"] == "reset"]
    assert reset["text"] == PICK_UP and r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_a_reset_due_while_idle_goes_with_the_next_message_as_before(run_factory) -> None:
    """D-046, unchanged: a reset whose trigger fires at a hand-back sends its notice with the next
    message; the reset comes at the hand-back after that."""
    acts = [Act(duration_s=2, final="Started."), Act(duration_s=2, final="Noted."), Act(duration_s=2, final="Done."), Act()]
    r = run_factory(acts, edit=lambda d: _reset_mid_turn(d, trigger={"handbacks": {"at_least": 1}}), poll_s=5).go()
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "next_message" and rec["reset"]["at"] == "handback" and rec["reset"]["handback"] == 2
    assert r.fake.received[1] == ("reply", NOTES_NOTICE) and not any(v == "correct" for v, _ in r.fake.received)
    assert r.fake.handoff_problems == []


@pytest.mark.parametrize("case", ["pending_request", "main_run_ended"])
def test_a_notice_with_no_run_to_steer_goes_with_the_next_message(run_factory, case) -> None:
    """D-046: the same handling as a correction's. At a pending request the notice goes with the
    answer; when the port says there is no run to steer, it is held and goes with the next
    message. The reset comes at the hand-back after it."""
    if case == "pending_request":
        acts = [Act(duration_s=4, state="question", ask=SLOT_Q), Act(duration_s=2, final="Noted."), Act(duration_s=2, final="Done."), Act()]
        r = run_factory(acts, edit=lambda d: _reset_mid_turn(d, trigger={"handbacks": {"at_least": 1}}), pol=policy(SLOT_POLICY), poll_s=5).go()
        answer = next(x for x in r.sends() if x["via"] == "answer")
        assert answer["text"].endswith(NOTES_NOTICE)
    else:
        acts = [Act(duration_s=10, writes=[(2, "out/step1.txt", "one")], final="Step 1 done."), Act(duration_s=2, final="Noted."),
                Act(duration_s=2, final="Done."), Act()]
        r = run_factory(acts, edit=_reset_mid_turn, poll_s=5, correct_race=True).go()
        assert any(x["type"] == "correction_deferred" and x["name"] == "handoff" for x in r.log)
        assert r.fake.received[1] == ("reply", NOTES_NOTICE)
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "next_message" and rec["reset"]["at"] == "handback"
    assert r.outcome.status == "usable"
    assert r.fake.handoff_problems == []


def test_an_elapsed_cutoff_during_the_grace_ends_the_episode(run_factory) -> None:
    """D-046: the notice is delivered at 5 s with a 60 s grace and a 20 s cap: the deadline comes
    first (the one end choice), so the episode cuts off at 20,000 ms and no reset is performed."""
    acts = [Act(duration_s=300, writes=[(2, "out/step1.txt", "one")]), Act()]
    r = run_factory(acts, edit=chain(lambda d: _reset_mid_turn(d, grace_ms=60_000), budgets(elapsed_ms=20_000)), poll_s=5).go()
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "mid_turn" and rec["reset"] is None and rec["status"] == "notice_sent"
    assert r.record["end"] == {"reason": "budget_cutoff", "duration_ms": 20_000}
    assert r.events("controlled_reset") == [] and r.planted("handoff") == ["reached", "not_reached"]
    assert r.fake.handoff_problems == []


def test_the_grace_and_the_hand_back_are_decided_from_instants(tmp_path) -> None:
    """D-046: the notice is steered at the first slice after 2 s and taken by the harness at 8 s
    whatever the polling interval; its 26 s grace runs out at 34 s. A hand-back at 33 s is the
    reset's moment at every polling interval, one at 35 s never is (the grace came first)."""
    for poll_s in (1, 5, 7):
        for final_at, want in ((33, "handback"), (35, "grace")):
            acts = [Act(duration_s=final_at, writes=[(2, "out/step1.txt", "one")], final="Notes left."), Act(duration_s=2, final="Done."), Act()]
            run = Run(tmp_path / f"i-{poll_s}-{final_at}", acts, edit=lambda d: _reset_mid_turn(d, grace_ms=26_000), poll_s=poll_s,
                      delivery_at_s=8.0)
            run.go()
            rec = run.run_json["events"]["handoff"]
            assert dt(rec["notice"]["utc"]) == dt("2026-11-22T12:00:08Z")
            assert rec["reset"]["at"] == want, (poll_s, final_at, rec)
            assert dt(rec["reset"]["utc"]) == dt("2026-11-22T12:00:33Z" if want == "handback" else "2026-11-22T12:00:34Z")
            assert run.fake.handoff_problems == []


def _explanation_after_reset(d) -> None:
    explanation(d)
    _reset_mid_turn(d)
    d["events"][0].update(required=True, reopen="Please leave your notes now.")  # as the generators declare it
    d["events"].append({"name": "follow_up", "trigger": {"handbacks": {"at_least": 1, "since": "handoff"}},
                        "action": {"type": "message", "text": "I don't follow. What's the actual reason?"}, "required": True,
                        "reopen": "Could you tell me why the slow machine is slow?"})
    d["scoring"]["answer_key"]["answers_after"] = "handoff"


class _ResetView(_View):
    """`_View` with the planted firings and the controlled reset, in log order, as the
    trajectory unit's Explanation check reads them."""

    def __init__(self, r: Run) -> None:
        self.header = {}
        self.events = []
        seq_of: dict[str, int] = {}
        reset_msg = None
        for x in r.log:
            if x["type"] == "verdict":
                seq_of[x["output"]["item"]] = len(self.events)
                self.events.append({"seq": len(self.events), "type": "assistant_message", "text": x["text"]})
            elif x["type"] == "hand_back" and not x.get("drained"):
                self.events.append({"seq": len(self.events), "type": "handback", "target": seq_of.get(x["closing"])})
            elif x["type"] == "send" and x["kind"] != "post_run":
                self.events.append({"seq": len(self.events), "type": "user_message", "text": x["text"]})
                if x["via"] == "reset":
                    reset_msg = len(self.events) - 1
            elif x["type"] == "controlled_reset":
                self.events.append({"seq": len(self.events), "type": "controlled_reset", "target": reset_msg})
            elif x["type"] == "event_fired":
                self.events.append({"seq": len(self.events), "type": "planted", "name": x["name"], "phase": "fired"})


def test_explanation_answers_after_the_reset(run_factory) -> None:
    """D-046: the notes hand-back is not an answer. The reset fires mid-turn, the agent leaves its
    notes and hands back, the reset comes; the first hand-back after it is the first answer and
    gets the fixed follow-up; the second is the second answer."""
    from cohbench.trajectory import episode as tep

    acts = [Act(duration_s=10, writes=[(2, "out/step1.txt", "one"), (8, "notes/NOTES.md", "notes")], final="Notes left."),
            Act(duration_s=4, final="The disk on m3 is full."), Act(duration_s=4, final="m3 is slow because its disk is full."), Act(final="No.")]
    r = run_factory(acts, edit=_explanation_after_reset).go()
    assert r.outcome.status == "usable"
    sends = [x for x in r.sends() if x["kind"] != "post_run"]
    assert [x["via"] for x in sends] == ["start", "correct", "reset", "reply"]
    assert sends[3]["text"] == "I don't follow. What's the actual reason?"
    rec = _reset_record(r)
    assert rec["notice"]["delivery"] == "mid_turn" and rec["reset"]["at"] == "handback"
    view = _ResetView(r)
    doc = dict(r.record)
    planted = doc["planted"]
    doc["answers"] = tep.explanation_answers(view, "handoff", planted)  # type: ignore[arg-type]
    assert [view.events[s]["text"] for s in doc["answers"]] == ["The disk on m3 is full.", "m3 is slow because its disk is full."]
    assert tep._explanation_errors(doc, view) == []
    assert r.planted("follow_up") == ["reached", "fired"]
    assert r.fake.handoff_problems == []


def test_the_episode_record_carries_the_task_digest_once_the_table_lists_it(run_factory, monkeypatch) -> None:
    """D-046: `task_sha256` is the digest admission computes (the run manifest's `task.digest`),
    emitted once the trajectory unit's episode table has the field, absent before."""
    from cohbench.runner import record
    from cohbench.trajectory import episode as tep

    r = run_factory([Act(final="Done."), Act()], edit=no_events, episode="before").go()
    assert ("task_sha256" in r.record) == ("task_sha256" in tep.FIELDS)
    monkeypatch.setattr(record, "_episode_fields", lambda: tuple(tep.FIELDS) + ("task_sha256",))
    r2 = run_factory([Act(final="Done."), Act()], edit=no_events, episode="after").go()
    assert r2.record["task_sha256"] == r2.run_json["task"]["digest"] and len(r2.record["task_sha256"]) == 64


@pytest.mark.parametrize("order", ["notice_first", "correction_first"])
def test_a_steered_notice_is_never_a_correction(run_factory, order) -> None:
    """D-046, for the trajectory unit: a Steering-shaped task where a reset notice and a correction
    are both steered into the running turn, in either order. They share the delivery machinery,
    but only the correction is a correction: one `correction_delivered`, at the correction; the
    notice keeps its own kind and its delivery (instant, route, mechanism, mark, straddle) is on
    the reset's event record."""
    fix = "The operation count is what matters, not the cache score. Please optimise for that."
    first, second = ("out/**", "src/**") if order == "notice_first" else ("src/**", "out/**")

    def edit(d):
        _reset_mid_turn(d, trigger={"changes": {"path": first if order == "notice_first" else second, "at_least": 1}})
        d["events"].append({"name": "correction", "trigger": {"changes": {"path": "src/**", "at_least": 1}}, "required": False,
                            "action": {"type": "correction", "text": fix}})
    writes = [(2, first, "a"), (7, second, "b")]
    acts = [Act(duration_s=15, writes=[(t, p.replace("**", "x.txt"), c) for t, p, c in writes], final="Notes left."), Act(duration_s=3, final="Done."), Act()]
    r = run_factory(acts, edit=edit, poll_s=5).go()
    steers = [t for v, t in r.fake.received if v == "correct"]
    assert steers == ([NOTES_NOTICE, fix] if order == "notice_first" else [fix, NOTES_NOTICE])
    (cd,) = r.events("correction_delivered")
    (corr,) = r.run_json["corrections"]
    assert corr["name"] == "correction" and corr["delivery"] == "mid_turn"
    assert cd["utc"] == corr["delivered_utc"] or dt(cd["utc"]) == dt(corr["delivered_utc"])
    notice_msg = next(x for x in r.sends() if x["via"] == "correct" and x["text"] == NOTES_NOTICE)
    assert cd["ref"] != {"message": notice_msg["message"]}
    assert notice_msg["kind"] != "correction" and r.h.user_kinds[notice_msg["message"]] != "correction"
    rec = _reset_record(r)["notice"]
    assert rec["delivery"] == "mid_turn" and rec["mechanism"] == "native_steer" and rec["mark"] is not None and rec["utc"]
    assert "straddled" in rec and "straddled_last" in rec
    assert _reset_record(r)["reset"]["at"] == "handback" and r.planted("correction") == ["reached", "fired"]
    assert r.fake.handoff_problems == []


def test_correction_after_reset_uses_one_delivery_and_mark_instant(run_factory):
    def edit(d):
        _reset_mid_turn(d, trigger={"handbacks": {"at_least": 1}})
        d["events"].append({"name": "correction", "required": False,
                            "trigger": {"handbacks": {"at_least": 1, "since": "handoff"}},
                            "action": {"type": "correction", "text": CORRECTION}})
    r = run_factory([Act(duration_s=2, final=x) for x in ("Started.", "Notes left.", "Done.", "Corrected.", "Nothing unusual.")], edit=edit).go()
    (c,) = r.run_json["corrections"]
    assert c["delivery"] == "next_message"
    (delivery,) = r.events("correction_delivered")
    marks = [json.loads(line) for f in r.fake.watcher_dirs() for line in (f / "events.jsonl").read_text().splitlines()]
    mark = next(x for x in marks if x.get("kind") == "mark" and x.get("label") == c["mark"])
    assert dt(mark["at"]) == dt(delivery["utc"]) == dt(c["delivered_utc"])
    assert r.run_json["events"]["handoff"]["reset"]["continuation"] == "launched"
    assert r.fake.handoff_problems == []


def test_unperformed_scheduled_reset_is_explicit_in_episode_record(run_factory):
    def edit(d):
        _reset_mid_turn(d, trigger={"path_exists": "never-created"})
    r = run_factory([Act(duration_s=2, final="Done."), Act()], edit=edit).go()
    expected = [{"event": "handoff", "reached": False, "fired": False, "performed": False, "reason": "finished_before_reset"}]
    assert r.record["resets"] == r.run_json["resets"] == expected
    assert r.record["planted"][0]["scheduled"]["action"] == "reset"
    assert r.events("controlled_reset") == []


def test_grace_reset_records_handback_and_takes_due_probe(run_factory):
    def edit(d):
        _reset_mid_turn(d, grace_ms=30_000)
        d["events"].append({"name": "checkpoint", "trigger": {"changes": {"path": "out/**", "at_least": 1}}, "required": False,
                            "action": {"type": "observe"}, "probe": {"about": "change", "question": "What file changed?", "expected": "out/step1.txt"}})
    r = run_factory([Act(duration_s=300, writes=[(2, "out/step1.txt", "one")]), Act(duration_s=2, final="Done."), Act()], edit=edit).go()
    handback = r.run_json["handback_log"][0]
    assert handback["kind"] == "interrupted" and handback["closing"] is None
    wait = r.events("wait")[0]
    assert dt(wait["utc"]) == dt(handback["utc"])
    assert r.run_json["probes"][0]["handback"] == handback["handback"]
    assert r.fake.handoff_problems == []
    assert r.record["resets"][0] == {"event": "handoff", "reached": True, "fired": True, "performed": True, "reason": None}


def test_fixed_scorer_id_produces_identical_episode_record_bytes(tmp_path):
    from dataclasses import replace

    runs = []
    for n in range(2):
        r = Run(tmp_path / str(n), [Act(duration_s=2, final="Done."), Act()], edit=no_events)
        r.cfg = replace(r.cfg, scorer_id="e0123456789abcdef")
        r.go()
        runs.append((r.dir / "episode-record.json").read_bytes())
    assert runs[0] == runs[1]


@pytest.mark.parametrize("value", ["", "e123", "f0123456789abcdef", "e0123456789ABCDEf", "e0123456789abcdef0", 1, True])
def test_scorer_id_shape_is_validated(run_factory, value):
    from dataclasses import replace

    r = run_factory([], edit=no_events)
    with pytest.raises(ValueError, match="scorer_id"):
        replace(r.cfg, scorer_id=value)


def test_count_elapsed_threshold_replays_at_its_own_instant(run_factory):
    def edit(d):
        no_events(d)
        d["events"] = [{"name": "tick", "required": False, "action": {"type": "observe"},
                        "trigger": {"count": {"of": [{"elapsed_ms": {"at_least": 2500}}], "at_least": 1}}}]
    r = run_factory([Act(duration_s=10, final="Done."), Act()], edit=edit, poll_s=5).go()
    assert dt(r.run_json["events"]["tick"]["fired_utc"]) == dt("2026-11-22T12:00:02.500Z")


def test_reset_outcome_ignores_a_trigger_discarded_after_the_end(run_factory):
    from types import SimpleNamespace
    from cohbench.runner.record import reset_outcomes

    r = run_factory([], edit=_reset_mid_turn)
    state = SimpleNamespace(end={"reason": "finished"}, runner_events=[], reset_info={},
                            ev={"handoff": SimpleNamespace(status="idle", reached_utc="2026-11-22T12:00:10Z")})
    assert reset_outcomes(r.task, state) == [{"event": "handoff", "reached": False, "fired": False, "performed": False, "reason": "finished_before_reset"}]
