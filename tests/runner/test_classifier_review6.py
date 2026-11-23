"""Round 6 end, reset and receipt regressions. No hosted calls."""

from dataclasses import replace
from datetime import timedelta
import copy

import pytest

from cohbench.runner.policy import Judgement
from cohbench.runner.port import Output, Turn
from .conftest import no_events, policy
from .fake import Act, T0, iso_us
from .test_classifier_review4 import runners
from .test_classifier_review5 import Serial
from .test_runner import _reset_mid_turn, _slow_judge


@pytest.mark.parametrize("boundary", ["grace", "elapsed"])
def test_r6_e3_catch_up_finds_handback_before_timer(run_factory, boundary):
    def edit(d):
        if boundary == "grace":
            _reset_mid_turn(d, grace_ms=5000)
        else:
            no_events(d)
            d["budgets"]["elapsed_ms"] = 10_000
            d["events"] = [
                {
                    "name": "inspect",
                    "trigger": {"outputs": {"at_least": 1}},
                    "action": {"type": "observe"},
                    "required": False,
                    "probe": {
                        "about": "output",
                        "question": "Was it completed?",
                        "expected": "yes",
                    },
                }
            ]
        d["user"]["return_delay_ms"] = 30_000

    run = run_factory(
        [
            Act(
                duration_s=5,
                writes=[(1, "out/step1.txt", "one")],
                messages=[(3, "Progress.")],
                final="Which day?",
            ),
            Act(final="Done."),
            Act(),
        ],
        edit=edit,
        poll_s=1,
        pol=policy(
            [
                {
                    "when": {"regex": "Which day"},
                    "label": "answerable",
                    "answer": ["slot"],
                }
            ]
        ),
    )
    original = run.factory

    def factory(spec):
        ep = original(spec)
        wait = ep.wait

        def observed(timeout_s):
            turn = wait(timeout_s)
            if turn.run_id == "run-1" and turn.state == "finished":
                stamp = iso_us(T0 + timedelta(seconds=5))
                return replace(
                    turn,
                    at=stamp,
                    outputs=tuple(
                        replace(o, delivered_utc=stamp) if o.kind == "final" else o
                        for o in turn.outputs
                    ),
                )
            return turn

        ep.wait = observed
        return ep

    run.factory = factory
    _slow_judge(run, 25)
    run.go()
    assert run.outcome.status == "usable", run.outcome
    if boundary == "grace":
        reset = run.run_json["events"]["handoff"]["reset"]
        assert reset["at"] == "handback" and reset["utc"] == iso_us(
            T0 + timedelta(seconds=5)
        )
    else:
        assert run.events("handback")[0]["ref"] == {"item": "item-run-1-final"}
        assert run.run_json["probes"][0]["target"] == {"item": "item-run-1-final"}
        wait = run.events("wait")[0]
        assert wait["utc"] == iso_us(T0 + timedelta(seconds=5))
        assert wait["end_utc"] == iso_us(T0 + timedelta(seconds=10))
        assert wait["fields"]["ended"] == "budget_cutoff"


@pytest.mark.parametrize("expires", [False, True])
def test_r6_e4_final_drain_question_gets_own_limit_without_reply(run_factory, expires):
    calls = []

    class EndJudge(Serial):
        def judge(self, card, transcript, output, *, deadline):
            calls.append((deadline, self.clock()))
            return super().judge(card, transcript, output, deadline=deadline)

    def edit(d):
        no_events(d)
        d["budgets"]["elapsed_ms"] = 10_000

    run = run_factory(
        [Act(duration_s=40), Act()],
        edit=edit,
        poll_s=1,
        pol=lambda: EndJudge(run, cost=30 if expires else 1),
    )
    original = run.factory

    def factory(spec):
        ep = original(spec)
        wait = ep.wait

        def drained(timeout_s):
            turn = wait(timeout_s)
            if turn.state == "interrupted":
                return replace(
                    turn,
                    outputs=(
                        Output(
                            "question",
                            "Which day?",
                            ep._item("drained-question"),
                            "run-1",
                            iso_us(T0 + timedelta(seconds=9)),
                        ),
                    ),
                )
            return turn

        ep.wait = drained
        return ep

    run.factory = factory
    run.go()
    assert calls and calls[0][0] - calls[0][1] == 25
    assert run.outcome.status == "usable", run.outcome
    assert run.record["end"]["reason"] == "budget_cutoff"
    assert not any(s["via"] == "answer" for s in run.sends())
    if expires:
        assert run.events("unjudged")[0]["fields"]["reason"] == "deadline"
    else:
        assert run.events("verdict")[0]["ref"] == {"item": "drained-question"}


@pytest.mark.parametrize("queued", [False, True])
def test_r6_v1_receipt_after_end_is_not_a_delivered_reply(run_factory, runners, queued):
    def edit(d):
        no_events(d)
        d["budgets"]["elapsed_ms"] = 10_000
        d["user"]["return_delay_ms"] = 2000

    run = run_factory(
        [
            Act(duration_s=1, state="question", ask="Which day?"),
            Act(duration_s=30),
            Act(),
        ],
        edit=edit,
        pol=policy(
            [
                {
                    "when": {"regex": "Which day"},
                    "label": "answerable",
                    "answer": ["slot"],
                }
            ]
        ),
        poll_s=1,
    )
    original = run.factory

    def factory(spec):
        ep = original(spec)
        answer = ep.answer

        def late(*args, **kwargs):
            sent = answer(*args, **kwargs)
            return replace(
                sent,
                delivered_utc=None if queued else iso_us(T0 + timedelta(seconds=11)),
                delivery_pending=queued,
            )

        ep.answer = late
        if queued:
            from cohbench.runner.port import Delivery

            ep.correction_delivery = lambda sent: Delivery(
                iso_us(T0 + timedelta(seconds=11))
            )
        return ep

    run.factory = factory
    run.go()
    assert run.outcome.status == "usable", run.outcome
    assert run.events("wait")[0]["fields"]["ended"] == "budget_cutoff"
    assert run.events("verdict")[0]["fields"]["reply"] is None
    assert not any(x.meta.get("delivered_ids") for x in runners[0].ordered())
    assert (
        next(x for x in run.log if x["type"] == "user_wait_end")["ended"]
        == "budget_cutoff"
    )


def test_r6_e2_message_carried_notice_sets_shared_deadline(run_factory, runners):
    from cohbench.runner.runner import _Message
    from cohbench.runner.port import Delivery, Sent

    def edit(d):
        _reset_mid_turn(d, grace_ms=2000)
        d["user"]["return_delay_ms"] = 10_000

    run = run_factory(
        [Act(duration_s=1, final="Done."), Act()], edit=edit, pol=lambda: Serial(run)
    )
    # Inject the real carrier at observation, with receipt at 1 and grace at 3.
    from cohbench.runner.runner import Runner

    original = Runner._observe_deliveries
    used = False

    def observed(r):
        nonlocal used
        if not used:
            used = True
            e = r.task.events[0]
            s = Sent(iso_us(T0), 0, "run-1", "t", "notice")
            q = _Message(s, {"message": "notice"}, "Notice.", {}, actions=[e])
            return [
                (
                    T0 + timedelta(seconds=1),
                    q,
                    Delivery(iso_us(T0 + timedelta(seconds=1))),
                )
            ]
        return original(r)

    # A delayed reported hand-back has an earlier notice grace.
    original_factory = run.factory

    def factory(spec):
        ep = original_factory(spec)
        wait = ep.wait

        def delayed(timeout_s):
            turn = wait(timeout_s)
            if turn.run_id == "run-1":
                turn = replace(turn, at=iso_us(T0 + timedelta(seconds=5)))
            return turn

        ep.wait = delayed
        return ep

    run.factory = factory
    import unittest.mock

    with unittest.mock.patch.object(Runner, "_observe_deliveries", observed):
        run.go()
    assert runners[0].policy.calls[0][1] == 10


def test_r6_t2_replay_compares_handoff_row_order(run_factory, monkeypatch):
    from cohbench.runner.runner import Runner
    from .policy_replay import snapshot, compare_same

    captured = []
    original = Runner.run

    def capture(r):
        result = original(r)
        captured.append(snapshot(r))
        return result

    monkeypatch.setattr(Runner, "run", capture)
    run_factory(
        [Act(messages=[(0, "Progress.")], final="Done."), Act()], edit=no_events
    ).go()
    baseline = captured[0]
    changed = copy.deepcopy(baseline)
    rows = changed["handoff"]["runner_events"]
    indices = [i for i, r in enumerate(rows) if r["type"] == "verdict"]
    assert len(indices) == 2
    a, b = indices
    rows[a], rows[b] = rows[b], rows[a]
    assert compare_same(baseline, changed)["differences"]
