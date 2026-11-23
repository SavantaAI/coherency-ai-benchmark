"""Round 4 episode-path regressions. Synthetic verdicts only."""

from dataclasses import replace
import json

import pytest

from cohbench.runner.policy import AwarenessJudgement, Judgement
from cohbench.runner.runner import Runner
from .conftest import no_events, policy
from .fake import Act
from .test_runner import explanation, _reset_mid_turn


class SerialPolicy:
    verdict_deadline_s = 25

    def __init__(self, run, *, missing=False):
        self.run, self.missing = run, missing
        self.clock = lambda: run.clock.clock

    def judge(self, card, transcript, output, *, deadline):
        if self.missing:
            from cohbench.user.api import DeadlineExceeded
            raise DeadlineExceeded("judgement deadline")
        return Judgement("no_request")

    def classify_awareness(self, *args):
        return AwarenessJudgement("unclear")


@pytest.fixture
def runners(monkeypatch):
    seen = []
    original = Runner.run
    def capture(self):
        seen.append(self)
        return original(self)
    monkeypatch.setattr(Runner, "run", capture)
    return seen


def test_r4_e1_missing_verdict_preserves_closing_answer_and_probe(run_factory, runners):
    def edit(doc):
        explanation(doc)
        doc["user"]["return_delay_ms"] = 30_000
        doc["events"][0]["probe"] = {"question": "What caused it?", "expected": "A full disk.", "about": "output"}
    run = run_factory([Act(final="The disk on m3 is full."), Act(final="The disk is full."), Act()], edit=edit,
                      pol=lambda: SerialPolicy(run, missing=True))
    run.go()
    assert run.outcome.status == "usable", run.outcome
    assert run.events("handback")[0]["ref"] == {"item": "item-run-1-final"}
    assert run.run_json["probes"][0]["target"] == {"item": "item-run-1-final"}
    assert runners[0].ctx.outputs == 2


@pytest.mark.parametrize("missing", [False, True])
def test_r4_e3_observation_and_correction_baseline_follow_stream(run_factory, runners, missing):
    def edit(doc):
        no_events(doc)
        doc["user"]["return_delay_ms"] = 30_000
        doc["events"] = [
            {"name": "fix", "trigger": {"elapsed_ms": {"at_least": 2000}}, "required": False,
             "action": {"type": "correction", "text": "Keep going."}},
            {"name": "next", "trigger": {"outputs": {"at_least": 2, "since": "fix"}}, "required": False,
             "action": {"type": "observe"}},
        ]
    run = run_factory([Act(duration_s=5, messages=[(1, "First."), (3, "Second.")], final="Done."), Act()],
                      edit=edit, poll_s=1, pol=lambda: SerialPolicy(run, missing=missing))
    run.go()
    runner = runners[0]
    assert runner.ctx.fired["fix"].outputs == 1
    assert runner.ctx.outputs == 3
    assert runner.ev["next"].fired_utc == "2026-11-22T12:00:05.000000Z"
    assert [x.entry.text for x in runner.ordered() if x.entry.role == "agent"] == ["First.", "Second.", "Done."]




def test_r4_v1_native_option_keeps_user_words_and_wire_value(run_factory, runners):
    def edit(doc):
        no_events(doc)
        doc["user"]["card"]["decisions"][0]["answer"] = "Tuesday"
    run = run_factory([Act(state="question", ask="Which day?", options=("Tuesday", "Friday")), Act(), Act()],
                      edit=edit, pol=policy([{"when": {"regex": "Which day"}, "label": "answerable", "answer": ["slot"]}]))
    original = run.factory
    def factory(spec):
        ep = original(spec)
        wait = ep.wait
        def mapped(timeout_s):
            turn = wait(timeout_s)
            if turn.pending and turn.pending.kind == "question":
                prompts = tuple(replace(q, options=tuple(replace(o, value="schedule_2" if o.label == "Tuesday" else "schedule_5") for o in q.options)) for q in turn.pending.prompts)
                turn = replace(turn, pending=replace(turn.pending, prompts=prompts))
            return turn
        ep.wait = mapped
        return ep
    run.factory = factory
    run.go()
    answer = next(x for x in runners[0].ordered() if x.entry.role == "user" and x.entry.text == "Tuesday")
    assert answer.meta["wire_text"] == "schedule_2"
    assert answer.meta["delivered_ids"] == ("slot",)


@pytest.mark.parametrize("delay", [2, None])
def test_r4_v1_refusal_steer_waits_for_observed_delivery(run_factory, runners, delay):
    run = run_factory([Act(state="secret", ask="Synthetic credential?"), Act(duration_s=5), Act()], edit=no_events,
                      delivery_delay_s=delay)
    original = run.factory
    def factory(spec):
        ep = original(spec)
        refuse = ep.refuse_secret
        def steer(pending, text, **kwargs):
            sent = replace(refuse(pending, text, **kwargs), mechanism="native_steer")
            ep.corrections.append((sent, ep.c.clock))
            return sent
        ep.refuse_secret = steer
        return ep
    run.factory = factory
    run.go()
    refusal = next(s for s in run.sends() if s["via"] == "refuse_secret")
    lines = [line for line in runners[0].ordered() if line.meta.get("ref") == refusal["ref"]]
    verdict = next(v for v in run.events("verdict") if v["ref"] == {"item": "item-run-1-secret"})
    if delay is None:
        assert lines == []
        assert verdict["fields"]["reply"] is None
    else:
        from datetime import timedelta
        from .test_runner import dt
        assert len(lines) == 1
        assert verdict["fields"]["reply"] == lines[0].entry.text
        assert lines[0].utc == dt(refusal["sent_utc"]) + timedelta(seconds=delay)


def test_r4_s2_rebuild_uses_output_instant_not_snapshot(tmp_path):
    from cohbench.trajectory.replay import rebuild
    from tests.trajectory.test_replay import raw_record
    from tests.trajectory import mini
    root = raw_record(tmp_path)
    manifest = json.loads((root / "export/manifest.json").read_text())
    verdict = next(e for e in manifest["runner_events"] if e["type"] == "verdict")
    (root / "classifier.jsonl").write_text(json.dumps({"output": verdict["ref"], "output_at": verdict["utc"], "as_of": mini.ts(10)}) + "\n")
    _, baseline = rebuild(root)
    _, rebuilt = rebuild(root, verdicts_at_as_of=True)
    assert rebuilt == baseline
