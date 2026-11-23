"""Round 7 cutoff, catch-up, admission and carried-correction regressions."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cohbench.runner.policy import AwarenessJudgement, Judgement
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from tests.user.conftest import config_doc
from .conftest import no_events, policy
from .fake import Act, T0, iso_us
from .test_classifier_review5 import Serial
from .test_runner import _reset_mid_turn, _slow_judge


@pytest.mark.parametrize("arrival", ["cutoff_poll", "handback"])
def test_r7_02_cutoff_poll_gives_pre_end_output_own_limit(run_factory, arrival):
    calls = []

    class Judge(Serial):
        def judge(self, card, transcript, output, *, deadline):
            calls.append((output, deadline - self.clock()))
            return super().judge(card, transcript, output, deadline=deadline)

    def edit(d):
        no_events(d)
        d["budgets"]["elapsed_ms"] = 10_000
        d["user"]["return_delay_ms"] = 30_000

    act = (
        Act(duration_s=40, messages=[(9, "Progress.")])
        if arrival == "cutoff_poll"
        else Act(duration_s=9, final="Progress.")
    )
    run = run_factory(
        [act, Act()], edit=edit, poll_s=10, pol=lambda: Judge(run, cost=7)
    )
    run.go()
    assert calls == [("Progress.", 25)]
    assert [x["fields"]["label"] for x in run.events("verdict")] == ["no_request"]
    assert run.outcome.status == "usable"
    assert run.record["end"]["duration_ms"] == (
        10_000 if arrival == "cutoff_poll" else 9000
    )


def test_r7_03_catch_up_handles_grace_before_later_output(run_factory):
    starts = []

    class Judge(Serial):
        def judge(self, card, transcript, output, *, deadline):
            starts.append((output, self.clock()))
            return super().judge(card, transcript, output, deadline=deadline)

    def edit(d):
        _reset_mid_turn(d, grace_ms=5000)
        d["user"]["return_delay_ms"] = 30_000

    run = run_factory(
        [
            Act(
                duration_s=80,
                writes=[(1, "out/step1.txt", "one")],
                messages=[(3, "First."), (8, "Second.")],
            ),
            Act(final="Done."),
            Act(),
        ],
        edit=edit,
        poll_s=1,
        pol=lambda: Judge(run, cost=7),
    )
    interrupts = []
    original = run.factory

    def factory(spec):
        ep = original(spec)
        interrupt = ep.interrupt

        def stop():
            interrupts.append(run.clock.clock)
            return interrupt()

        ep.interrupt = stop
        return ep

    run.factory = factory
    run.go()
    assert interrupts[0] == 10
    assert dict(starts)["Second."] >= interrupts[0]
    assert run.run_json["events"]["handoff"]["reset"]["utc"] == iso_us(
        T0 + timedelta(seconds=6)
    )


def test_r7_03_due_correction_precedes_next_catch_up_judgement(run_factory):
    starts = []

    class Judge(Serial):
        def judge(self, card, transcript, output, *, deadline):
            starts.append((output, self.clock()))
            return super().judge(card, transcript, output, deadline=deadline)

    def edit(d):
        no_events(d)
        d["user"]["return_delay_ms"] = 30_000
        d["events"] = [
            {
                "name": "change",
                "trigger": {"elapsed_ms": {"at_least": 4000}},
                "action": {"type": "correction", "text": "Use the revised order."},
                "required": False,
            }
        ]

    run = run_factory(
        [
            Act(duration_s=25, messages=[(3, "First."), (5, "Second.")], final="Done."),
            Act(),
        ],
        edit=edit,
        poll_s=1,
        pol=lambda: Judge(run, cost=7),
    )
    sent = []
    original = run.factory

    def factory(spec):
        ep = original(spec)
        correct = ep.correct

        def correction(*args, **kwargs):
            sent.append(run.clock.clock)
            return correct(*args, **kwargs)

        ep.correct = correction
        return ep

    run.factory = factory
    run.go()
    assert sent == [10]
    assert sent[0] <= dict(starts)["Second."]
    order = [x["type"] for x in run.log]
    second = next(
        i
        for i, x in enumerate(run.log)
        if x["type"] == "verdict" and x["text"] == "Second."
    )
    assert order.index("send", order.index("verdict")) < second


@pytest.mark.parametrize("cost", [0.01, 1.0])
def test_r7_03_short_judgement_does_not_add_catch_up_poll(run_factory, cost):
    run = run_factory(
        [Act(duration_s=10, messages=[(1, "Progress.")], final="Done."), Act()],
        edit=no_events,
        poll_s=1,
    )
    waits = []
    original = run.factory

    def factory(spec):
        ep = original(spec)
        wait = ep.wait

        def observed(timeout_s):
            waits.append(timeout_s)
            return wait(timeout_s)

        ep.wait = observed
        return ep

    run.factory = factory
    _slow_judge(run, cost)
    run.go()
    assert 0 not in waits


@pytest.mark.parametrize("delay", [2000, 27999])
def test_r7_04_classifier_rejected_before_open_when_delay_too_short(run_factory, delay):
    def edit(d):
        no_events(d)
        d["user"]["return_delay_ms"] = delay

    def make():
        client = SimpleNamespace(
            clock=lambda: run.clock.clock, flush=lambda **kwargs: {}
        )
        p = Classifier(parse(config_doc("http://127.0.0.1:1")), client)
        p.judge = lambda *args, **kwargs: Judgement("no_request")
        p.classify_awareness = lambda *args: AwarenessJudgement("unclear")
        return p

    run = run_factory([Act(final="Done."), Act()], edit=edit, pol=make)
    run.go()
    assert run.fake is None
    assert run.outcome.status == "failed"
    assert f"{delay / 1000:g}" in run.outcome.detail
    assert "28" in run.outcome.detail


@pytest.mark.parametrize("queued", [False, True])
def test_r7_06_carried_correction_uses_send_time_and_mark(run_factory, queued):
    def edit(d):
        no_events(d)
        d["events"] = [
            {
                "name": "change",
                "trigger": {"handbacks": {"at_least": 1}},
                "action": {"type": "correction", "text": "Use the revised order."},
                "required": False,
            }
        ]

    run = run_factory(
        [
            Act(duration_s=1, final="Which day?"),
            Act(duration_s=5, final="Done."),
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
    )
    original = run.factory

    def factory(spec):
        ep = original(spec)
        send = ep.reply

        def later_receipt(*args, **kwargs):
            result = send(*args, **kwargs)
            return replace(
                result,
                delivered_utc=None if queued else iso_us(T0 + timedelta(seconds=4)),
                delivery_pending=queued,
            )

        ep.reply = later_receipt
        if queued:
            from cohbench.runner.port import Delivery

            ep.correction_delivery = lambda sent: Delivery(
                iso_us(T0 + timedelta(seconds=4))
            )
        return ep

    run.factory = factory
    run.go()
    carried = run.run_json["corrections"][0]
    assert datetime.fromisoformat(
        carried["queued_utc"].replace("Z", "+00:00")
    ) == T0 + timedelta(seconds=3)
    assert carried["delivered_utc"] == carried["queued_utc"]
    mark = next(
        row
        for row in run.log
        if row["type"] == "mark" and row["label"] == carried["mark"]
    )
    assert datetime.fromisoformat(mark["at"].replace("Z", "+00:00")) == T0 + timedelta(
        seconds=3
    )


def test_r7_04_boundary_and_generated_delays_are_admitted():
    from cohbench.runner import ScriptedPolicy
    from cohbench.runner.run import admit_policy_delay
    from cohbench.tasks import generate
    from cohbench.tasks.suite import options

    slow = SimpleNamespace(max_judgement_s=25)
    admit_policy_delay(SimpleNamespace(return_delay_ms=28_000), slow)
    scripted = ScriptedPolicy({"format": "cohbench-scripted-policy", "version": 1})
    assert scripted.max_judgement_s == 0
    admit_policy_delay(SimpleNamespace(return_delay_ms=2000), scripted)
    delays = []
    for option in options():
        delay = generate(option).task["user"]["return_delay_ms"]
        delays.append(delay)
        admit_policy_delay(SimpleNamespace(return_delay_ms=delay), slow)
    assert len(delays) == 88 and min(delays) >= 30_000


def test_r7_03_catch_up_does_not_chain_before_due_grace(run_factory):
    def edit(d):
        _reset_mid_turn(d, grace_ms=12_000)
        d["user"]["return_delay_ms"] = 30_000

    run = run_factory(
        [
            Act(
                duration_s=80,
                writes=[(1, "out/step1.txt", "one")],
                messages=[(3, "First."), (5, "Second.")],
            ),
            Act(final="Done."),
            Act(),
        ],
        edit=edit,
        poll_s=1,
        pol=lambda: Serial(run, cost=7),
    )
    waits = []
    original = run.factory

    def factory(spec):
        ep = original(spec)
        wait = ep.wait

        def observed(timeout_s):
            waits.append(timeout_s)
            return wait(timeout_s)

        ep.wait = observed
        return ep

    run.factory = factory
    run.go()
    assert waits.count(0) == 1
    reset = run.run_json["events"]["handoff"]["reset"]
    assert reset["utc"] == iso_us(T0 + timedelta(seconds=13))
    assert reset["stopped_utc"] == iso_us(T0 + timedelta(seconds=17))
