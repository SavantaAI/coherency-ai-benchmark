"""Round 4 episode-path regressions. Synthetic verdicts only."""

from dataclasses import replace
import json

import pytest

from cohbench.runner.policy import AwarenessJudgement, Judgement
from cohbench.runner.runner import Runner
from .conftest import no_events, policy
from .fake import Act
from .test_runner import explanation, _reset_mid_turn


class Batch:
    def __init__(self, run, *, missing=False, cost=0):
        self.run, self.missing, self.cost = run, missing, cost
        self.windows = []

    def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
        self.windows.append(timeout_s)
        return ScheduledJob(self.run, None if self.missing else Judgement("no_request"), self.cost, timeout_s)

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
                      pol=lambda: Batch(run, missing=True))
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
                      edit=edit, poll_s=1, pol=lambda: Batch(run, missing=missing))
    run.go()
    runner = runners[0]
    assert runner.ctx.fired["fix"].outputs == 1
    assert runner.ctx.outputs == 3
    assert runner.ev["next"].fired_utc == "2026-11-22T12:00:05.000000Z"
    assert [x.entry.text for x in runner.ordered() if x.entry.role == "agent"] == ["First.", "Second.", "Done."]


@pytest.mark.parametrize("grace", [False, True])
def test_r4_e2_reset_drain_cannot_open_a_new_window(run_factory, grace):
    def edit(doc):
        _reset_mid_turn(doc, grace_ms=3000 if grace else 60_000)
        doc["user"]["return_delay_ms"] = 30_000
    policies = []
    def make():
        p = Batch(run, cost=20)
        policies.append(p)
        return p
    run = run_factory([
        Act(duration_s=60 if grace else 5, state="finished" if grace else "question", ask="Continue?",
            writes=[(1, "out/step1.txt", "one")], messages=[(2, "First.")], late=["Last."], final=None),
        Act(final="Done."), Act()], edit=edit, poll_s=1, finish_s=18 if grace else 0, pol=make)
    run.go()
    assert not run.run_json["overruns"]["count"], run.run_json["overruns"]
    if not grace:
        assert policies[0].windows[-2] <= 7
    assert any(e["type"] == "unjudged" and e["text"] == "Last." for e in run.log)


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
    if delay is None:
        assert lines == []
    else:
        from datetime import timedelta
        from .test_runner import dt
        assert len(lines) == 1
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


class ScheduledJob:
    """Deterministic asynchronous clock for episode scheduling tests."""
    def __init__(self, run, value, duration, timeout_s=None):
        from datetime import timedelta
        self.run, self.value = run, value
        self.started_at = run.clock.utcnow()
        self.completed_at = self.started_at + timedelta(seconds=duration)
        self.deadline = self.started_at + timedelta(seconds=min(25, timeout_s) if timeout_s is not None else 25)
        self.tighten(self.deadline)

    def tighten(self, deadline):
        self.deadline = min(deadline, self.deadline)
        if self.completed_at > self.deadline and not self.ready():
            self.completed_at, self.value = self.deadline, None

    def ready(self):
        return self.run.clock.utcnow() >= self.completed_at

    def wait(self):
        self.run.clock.advance(max(0, (self.completed_at-self.run.clock.utcnow()).total_seconds()))


class ScheduledPolicy:
    def __init__(self, run, durations, labels=None):
        self.run, self.durations, self.labels = run, durations, labels or {}
        self.inputs, self.jobs = [], []
        self.verdict_deadline_s = 25

    def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
        self.inputs.append((card, tuple(transcript), output, utcnow()))
        job = ScheduledJob(self.run, self.labels.get(output, Judgement('no_request')), self.durations.get(output, 0), timeout_s)
        self.jobs.append(job)
        return job

    def classify_awareness(self, *args):
        return AwarenessJudgement('unclear')


def test_r4_arrival_snapshot_does_not_wait_for_handback(run_factory, runners):
    def edit(doc):
        no_events(doc)
        doc['user']['return_delay_ms'] = 30_000
        doc['events'] = [{'name': 'fix', 'trigger': {'elapsed_ms': {'at_least': 2000}},
                          'required': False, 'action': {'type': 'correction', 'text': 'Use Friday.'}}]
    policies = []
    def make():
        p = ScheduledPolicy(run, {'Which day?': 8})
        policies.append(p)
        return p
    run = run_factory([Act(duration_s=12, messages=[(1, 'Which day?')], final='Done.'), Act()],
                      edit=edit, poll_s=1, pol=make)
    run.go()
    p = policies[0]
    assert p.jobs[0].started_at < p.inputs[1][3]
    assert p.inputs[0][3].isoformat().startswith('2026-11-22T12:00:01')
    assert 'Use Friday.' not in [e.text for e in p.inputs[0][1]]
    assert 'Use Friday.' in [e.text for e in p.inputs[1][1]]
    assert runners[0].ev['fix'].fired_utc == '2026-11-22T12:00:02.000000Z'
    assert runners[0].classifier[0]['completed_at'] == '2026-11-22T12:00:09.000000Z'


def test_r4_verdict_effects_wait_for_completion_and_output_order(run_factory, runners):
    def edit(doc):
        no_events(doc)
        doc['user']['return_delay_ms'] = 30_000
        doc['events'] = [{'name': 'after', 'trigger': {'verdicts': {'label': 'no_request', 'at_least': 1}},
                          'required': False, 'action': {'type': 'observe'}}]
    run = run_factory([Act(duration_s=12, messages=[(1, 'First.'), (2, 'Second.')], final='Done.'), Act()],
                      edit=edit, poll_s=1, pol=lambda: ScheduledPolicy(run, {'First.': 8, 'Second.': 1}))
    run.go()
    r = runners[0]
    assert r.ev['after'].fired_utc == '2026-11-22T12:00:09.000000Z'
    assert [x['text'] for x in r.classifier[:2]] == ['First.', 'Second.']
    assert [x['effective_at'] for x in r.classifier[:2]] == ['2026-11-22T12:00:09.000000Z'] * 2
    assert r.classifier[1]['completed_at'] == '2026-11-22T12:00:03.000000Z'


def test_r4_own_start_limit_and_handback_cutoff_both_apply(run_factory, runners):
    def edit(doc):
        no_events(doc)
        doc['user']['return_delay_ms'] = 30_000
    policies = []
    def make():
        p = ScheduledPolicy(run, {'First.': 40, 'Last.': 40})
        policies.append(p)
        return p
    run = run_factory([Act(duration_s=20, messages=[(1, 'First.')], final='Last.'), Act()],
                      edit=edit, poll_s=1, pol=make)
    run.go()
    assert [j.deadline.isoformat() for j in policies[0].jobs] == [
        '2026-11-22T12:00:26+00:00', '2026-11-22T12:00:45+00:00']
    assert len(run.events('unjudged')) == 2
    assert run.events('handback')[0]['ref'] == {'item': 'item-run-1-final'}


def test_r4_endpoint_failure_does_not_remove_following_observations(run_factory, runners):
    from cohbench.user.api import EndpointError
    run = run_factory([Act(duration_s=4, messages=[(1,'First.'),(2,'Second.'),(3,'Third.')], final='Done.'), Act()],
                      edit=no_events, poll_s=1,
                      pol=lambda: ScheduledPolicy(run, {'First.':2,'Second.':1},
                          {'First.':EndpointError('first failed'),'Second.':EndpointError('second failed')}))
    run.go()
    r = runners[0]
    assert r.end['reason']=='runner_abort'
    assert 'Third.' in [s.output.text for s in r._observed.values()]
    assert r.ctx.outputs==3


def test_r4_failed_end_coverage_still_closes_pending_jobs(run_factory, runners, monkeypatch):
    from cohbench.runner.port import EpisodeFailure
    original = Runner._coverage
    def unavailable(r, *args, **kwargs):
        if r.ep.clock() >= 2:
            raise EpisodeFailure('coverage unavailable')
        return original(r, *args, **kwargs)
    monkeypatch.setattr(Runner, '_coverage', unavailable)
    policies = []
    def make():
        p = ScheduledPolicy(run, {'Working.':20})
        policies.append(p)
        return p
    run = run_factory([Act(duration_s=10,messages=[(1,'Working.')]),Act()],edit=no_events,poll_s=1,pol=make)
    run.go()
    assert runners[0].end['reason']=='driver_failure'
    assert policies[0].jobs[0].ready()
    assert runners[0]._jobs==[]
    assert {e['ref']['item'] for e in run.events('unjudged')} == set(runners[0]._observed)
