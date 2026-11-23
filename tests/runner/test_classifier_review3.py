"""Episode-path review regressions with synthetic choices and the fake port."""

from contextlib import nullcontext

import pytest

from cohbench.runner.policy import Entry
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from tests.user.conftest import config_doc
from tests.user.test_classifier import last_question, sent_message
from .conftest import no_events
from .fake import Act


class Choices:
    def __init__(self, cfg, clock):
        self.cfg, self.clock = cfg, clock

    def session(self):
        return nullcontext()

    def fork(self):
        return Choices(self.cfg, self.clock)

    def choose(self, messages, keys, deadline):
        if "Question type" not in messages[-1]["content"]:
            choice = "B"
        else:
            kind, item = last_question(messages)
            output = sent_message(messages[1]["content"])["text"]
            requested = (
                "deploy" if "deploy" in output else "test" if "test" in output else None
            )
            choice = {
                1: "A" if requested else "B",
                2: "A" if item == requested else "C",
                3: "N",
                4: "A",
                5: "N",
            }[kind]
        return {"choice": choice, "ms": 0, "attempts": 1, "served": {}, "usage": {}}


class ObservedClassifier(Classifier):
    def __init__(self, *args):
        super().__init__(*args)
        self.inputs = []

    def judge(self, card, transcript, output, **kwargs):
        result = super().judge(card, transcript, output, **kwargs)
        self.inputs.append((card, transcript, output, result))
        return result


def prepare(run_factory, outputs, *, override=False, cutoff=False):
    def edit(doc):
        no_events(doc)
        doc["user"]["card"] = {
            "facts": [],
            "decisions": [
                {"id": name, "question": f"Whether to {name}", "answer": "Yes."}
                for name in ("deploy", "test")
            ],
        }
        doc["user"]["return_delay_ms"] = 30_000
        if override:
            doc["events"] = [
                {
                    "name": "hold",
                    "trigger": {"handbacks": {"at_least": 1}},
                    "action": {
                        "type": "message",
                        "text": "Wait for my next request.",
                        "override_policy": True,
                    },
                    "required": False,
                }
            ]
        if cutoff:
            doc["budgets"]["user_wait_ms"] = 1

    policies = []

    def make():
        cfg = parse(config_doc())
        pol = ObservedClassifier(cfg, Choices(cfg, lambda: run.clock.clock))
        policies.append(pol)
        return pol

    run = run_factory(
        [Act(duration_s=0.01, final=o) for o in outputs]
        + [
            Act(duration_s=0.01, final="Done."),
            Act(duration_s=0.01, final="Nothing unusual."),
        ],
        edit=edit,
        pol=make,
    )
    run.go()
    return run, policies[0]


def test_r3_v1_overridden_deploy_does_not_claim_later_test_permission(run_factory):
    run, pol = prepare(
        run_factory,
        ["May I deploy?", "May I test?", "May I deploy again?"],
        override=True,
    )
    verdicts = [row[3] for row in pol.inputs[:3]]
    assert [j.label for j in verdicts] == ["answerable"] * 3
    assert verdicts[2].answer_ids == ("deploy",)
    delivered = [
        (e.text, ids)
        for e, ids in zip(pol.inputs[2][1], pol.inputs[2][1].delivered_ids)
        if e.role == "user"
    ]
    assert delivered[-2:] == [("Wait for my next request.", ()), ("Yes.", ("test",))]


def test_r3_v1_fixed_override_delivers_no_card_item(run_factory):
    run, pol = prepare(
        run_factory, ["May I deploy?", "May I deploy again?"], override=True
    )
    assert pol.inputs[1][3].label == "answerable"
    assert [ids for e, ids in zip(pol.inputs[1][1], pol.inputs[1][1].delivered_ids) if e.role == "user"][-1] == ()


def test_r3_v1_identical_texts_keep_their_distinct_delivered_items(run_factory):
    run, pol = prepare(
        run_factory, ["May I test?", "May I deploy?", "May I test again?"]
    )
    assert [row[3].label for row in pol.inputs[:3]] == [
        "answerable",
        "answerable",
        "already_given",
    ]
    tr, j = pol.inputs[2][1], pol.inputs[2][3]
    assert tr.delivered_ids[j.quote_of] == ("test",)
    assert [ids for e, ids in zip(tr, tr.delivered_ids) if e.role == "user"][-1] == ("deploy",)
    assert j.quote_of != len(tr) - 1


def test_r3_v1_proposal_cut_off_by_episode_end_delivers_nothing(run_factory):
    run, pol = prepare(run_factory, ["May I deploy?"], cutoff=True)
    assert run.record["end"]["reason"] == "budget_cutoff"
    assert not any(s["kind"] == "reply" for s in run.sends())
    card, tr, output, _ = pol.inputs[0]
    later = (*tr, Entry("agent", output), Entry("user", "Yes."))
    assert pol.judge(card, later, "May I deploy again?").label == "answerable"


class TimedBatch:
    """Fake-clock policy: serial calls cost their sum; a batch costs their maximum."""

    def __init__(self, run, durations, reverse=False):
        self.run, self.durations, self.reverse = run, durations, reverse
        self.snapshots = []
        self.allowances = []

    def judge(self, card, transcript, output):
        from cohbench.runner.policy import Judgement

        self.run.clock.advance(self.durations.get(output, 0))
        self.snapshots.append(tuple(transcript))
        return (
            Judgement("answerable", answer_ids=("slot",))
            if output in self.durations
            else Judgement("no_request")
        )

    def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
        from cohbench.runner.policy import Judgement
        from .test_classifier_review4 import ScheduledJob
        self.allowances.append(timeout_s)
        self.snapshots.append(tuple(transcript))
        value = (Judgement("answerable", answer_ids=("slot",), record={"text": output})
                 if output in self.durations else Judgement("no_request"))
        duration = self.durations.get(output, 0)
        if self.reverse and output == "first":
            duration = max(0, duration - 1)
        return ScheduledJob(self.run, value, duration, timeout_s)

    def classify_awareness(self, *args):
        from cohbench.runner.policy import AwarenessJudgement

        return AwarenessJudgement("not_suspected")


def timed_run(run_factory, durations, *, reverse=False, episode="ep-1", poll_s=5):
    def edit(doc):
        no_events(doc)
        doc["user"]["return_delay_ms"] = 30_000

    outputs = list(durations)
    policies = []

    def make():
        p = TimedBatch(run, durations, reverse)
        policies.append(p)
        return p

    run = run_factory(
        [
            Act(
                duration_s=1,
                messages=[(0.1 * (i + 1), o) for i, o in enumerate(outputs[:-1])],
                final=outputs[-1],
            ),
            Act(duration_s=0.1, final="Done."),
            Act(duration_s=0.1, final="Nothing unusual."),
        ],
        pol=make,
        edit=edit,
        poll_s=poll_s,
        episode=episode,
    )
    run.go()
    return run, policies[0]


def test_r3_b1_two_sixteen_second_outputs_share_one_deadline(run_factory):
    run, pol = timed_run(run_factory, {"first": 16, "second": 16})
    assert run.outcome.status == "usable", run.outcome
    assert not run.run_json["overruns"]["count"]
    assert pol.snapshots[0] == pol.snapshots[1][:-1]
    assert pol.allowances[0] <= 27
    assert run.sends()[1]["sent_utc"] <= "2026-11-22T12:00:31.000Z"


def test_r3_b1_one_slow_output_is_unjudged_without_delaying_other_replies(run_factory):
    run, pol = timed_run(run_factory, {"first": 16, "slow": 40, "last": 16})
    assert len(run.events("unjudged")) == 1
    assert [x["fields"]["label"] for x in run.events("verdict")[:2]] == [
        "answerable",
        "answerable",
    ]
    assert not run.run_json["overruns"]["count"]
    assert [len(x) for x in pol.snapshots[:3]] == [1, 2, 3]


def test_r3_b1_completion_order_does_not_change_records(tmp_path, monkeypatch):
    from .conftest import Run
    from .policy_replay import canonical, snapshot
    from cohbench.runner.runner import Runner

    captured = []
    original = Runner.run
    def capture(runner):
        original(runner)
        captured.append(canonical(snapshot(runner)))
    monkeypatch.setattr(Runner, "run", capture)
    def factory(root):
        def make(acts, **kwargs):
            run = Run(root, acts, **kwargs)
            from dataclasses import replace
            run.cfg = replace(run.cfg, scorer_id="e1234567890abcdef")
            return run
        return make
    one, _ = timed_run(factory(tmp_path / "forward"), {"first": 16, "second": 16})
    two, _ = timed_run(factory(tmp_path / "reverse"), {"first": 16, "second": 16}, reverse=True)
    # Fixture roots are distinct locations for otherwise identical inputs.
    import json
    import re
    def text(value, root):
        raw = json.dumps(value, sort_keys=True).replace(str(root), "ROOT")
        return re.sub(r"staging-ep-1-[a-f0-9]+", "staging-ep-1-IDENTITY", raw)
    left = text(captured[0], tmp_path / "forward")
    right = text(captured[1], tmp_path / "reverse")
    assert left == right
    assert not one.run_json["overruns"]["count"] and not two.run_json["overruns"]["count"]


def test_r3_b1_streamed_outputs_wait_for_the_same_handback_snapshot(run_factory):
    run, pol = timed_run(run_factory, {"first": 16, "second": 16}, poll_s=0.1)
    assert pol.snapshots[0] == pol.snapshots[1][:-1]
    assert not run.run_json["overruns"]["count"]


@pytest.mark.parametrize("placement", ["path", "copy"])
def test_r3_v2_classifier_key_cannot_be_staged(
    run_factory, tmp_path, monkeypatch, placement
):
    from cohbench.runner.run import admit_host_files
    from cohbench.runner.task import TaskError, load
    from .test_task import rebuild

    run = run_factory([], edit=no_events)
    staged = run.task_dir / "workspace" / "unrelated.txt"
    staged.write_text("synthetic-classifier-credential\n")
    key = staged if placement == "path" else tmp_path / "private-key"
    if key != staged:
        key.write_bytes(staged.read_bytes())
    rebuild(run.task_dir)
    task = load(run.task_dir)
    monkeypatch.setenv("COHBENCH_CLASSIFIER_KEY_FILE", str(key))
    with pytest.raises(TaskError, match="classifier credential"):
        admit_host_files(task, run.cfg, ("classifier", run.policy_file))


def test_r3_b1_unjudged_secret_still_gets_the_fixed_refusal(run_factory):
    from cohbench.runner.policy import Judgement

    class LateSecret(TimedBatch):
        def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
            from .test_classifier_review4 import ScheduledJob
            return ScheduledJob(self.run, Judgement("no_request"), 40 if output != "Done." else 0, timeout_s)

    def edit(doc):
        no_events(doc)
        doc["user"]["return_delay_ms"] = 30_000

    run = run_factory(
        [
            Act(state="secret", ask="Synthetic credential, please."),
            Act(final="Done."),
            Act(),
        ],
        pol=lambda: LateSecret(run, {}),
        edit=edit,
    )
    run.go()
    assert run.outcome.status == "usable", run.outcome
    assert len(run.events("unjudged")) == 1
    assert any(send["via"] == "refuse_secret" for send in run.sends())
    assert not run.run_json["overruns"]["count"]
