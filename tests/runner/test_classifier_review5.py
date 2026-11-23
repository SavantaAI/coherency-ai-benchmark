"""Round 5 serial-judgement and delivery regressions. No hosted calls."""
from dataclasses import replace

import pytest

from cohbench.runner.port import Usage
from .conftest import chain, no_events
from .fake import Act
from .test_runner import _reset_mid_turn, _slow_judge, budgets


@pytest.mark.parametrize('recovers', [False, True])
def test_r5_full_limit_respects_usage_grace(run_factory, recovers):
    run = run_factory([Act(duration_s=80, messages=[(1, 'Progress.')]), Act()],
                      edit=chain(no_events, budgets(tokens=1000)), poll_s=1)
    run.cfg = replace(run.cfg, usage_grace_s=20)
    original = run.factory
    def factory(spec):
        ep = original(spec)
        ep.usage = lambda: Usage(0 if recovers and ep.c.clock >= 25 else None,
                                recovers and ep.c.clock >= 25, {})
        return ep
    run.factory = factory
    _slow_judge(run, 25)
    run.go()
    if recovers:
        assert run.outcome.status == 'usable', run.outcome
    else:
        assert run.record['end']['reason'] == 'driver_failure'
        assert 'usage unavailable' in run.run_json['end']['detail']
    assert run.fake.handoff_problems == []


def test_r5_full_limit_preserves_elapsed_end_and_interrupt(run_factory):
    run = run_factory([Act(duration_s=80, messages=[(1, 'Progress.')], late=['Drained.']), Act()],
                      edit=chain(no_events, budgets(elapsed_ms=10_000)), poll_s=1)
    _slow_judge(run, 25)
    run.go()
    assert run.record['end'] == {'reason':'budget_cutoff', 'duration_ms':10_000}
    assert run.fake.interrupts == 1
    assert run.run_json['end']['stopped_utc'] >= '2026-11-22T12:00:26'
    assert [x['type'] for x in run.log].count('drain') == 1
    assert run.fake.handoff_problems == []


def test_r5_full_limit_preserves_reset_grace_instant(run_factory):
    run = run_factory([
        Act(duration_s=80, writes=[(1, 'out/step1.txt', 'one')], messages=[(3, 'Progress.')]),
        Act(final='Done.'), Act()], edit=lambda d: _reset_mid_turn(d, grace_ms=5000), poll_s=1)
    _slow_judge(run, 25)
    run.go()
    rec = run.run_json['events']['handoff']['reset']
    assert rec['at'] == 'grace'
    assert rec['utc'] == '2026-11-22T12:00:06.000000Z'
    assert rec['stopped_utc'] >= '2026-11-22T12:00:28'
    assert rec['continuation'] == 'launched'
    assert run.outcome.status == 'usable', run.outcome
    assert run.fake.handoff_problems == []


class Serial:
    verdict_deadline_s = 25

    def __init__(self, run, *, cost=0, error=None):
        self.run, self.cost, self.error = run, cost, error
        self.calls = []
        self.clock = lambda: run.clock.clock

    def judge(self, card, transcript, output, *, deadline):
        from cohbench.runner.policy import Judgement
        from cohbench.user.api import DeadlineExceeded
        self.calls.append((output, deadline, card, transcript))
        remaining = max(0, deadline-self.clock())
        self.run.clock.advance(min(self.cost, remaining))
        if self.error:
            raise self.error
        if self.cost >= remaining:
            raise DeadlineExceeded('judgement deadline')
        return Judgement('no_request')

    def classify_awareness(self, *args):
        from cohbench.runner.policy import AwarenessJudgement
        return AwarenessJudgement('unclear')


def test_r5_serial_outputs_share_deadline_and_keep_last_closing(run_factory):
    def edit(d):
        no_events(d)
        d['user']['return_delay_ms'] = 30_000
    instances = []
    def make():
        p = Serial(run, cost=10)
        instances.append(p)
        return p
    run = run_factory([Act(duration_s=1, messages=[(1,'First.'),(1,'Second.')], final='Last.'), Act()],
                      edit=edit, pol=make)
    run.go()
    assert run.outcome.status == 'usable', run.outcome
    calls = instances[0].calls
    assert [x[0] for x in calls] == ['First.', 'Second.', 'Last.']
    assert [x[1] for x in calls] == [26, 28, 28]
    assert [x['fields']['reason'] for x in run.events('unjudged')] == ['deadline']
    assert run.events('handback')[0]['ref'] == {'item':'item-run-1-final'}
    assert len(run.events('verdict')) == 2
    assert not any('completed_at' in x for x in run.log)


@pytest.mark.parametrize('cause', ['deadline', 'endpoint_failure'])
def test_r5_missing_verdict_audit_names_cause(run_factory, cause):
    from cohbench.user.api import DeadlineExceeded, EndpointError
    error = DeadlineExceeded('expired') if cause == 'deadline' else EndpointError('connection refused')
    def edit(d):
        no_events(d)
        d['user']['return_delay_ms'] = 30_000
    run = run_factory([Act(final='Done.'), Act()], edit=edit,
                      pol=lambda: Serial(run, error=error))
    run.go()
    assert [x['fields']['reason'] for x in run.events('unjudged')] == [cause]
    assert (run.outcome.status == 'usable') == (cause == 'deadline')


def test_r5_pending_question_gets_full_limit_and_no_invented_reopen(run_factory):
    def edit(d):
        no_events(d)
        d['events'] = [{'name':'later','trigger':{'outputs':{'at_least':3}},'required':True,
                        'action':{'type':'observe'},'reopen':'Please continue.'}]
    instances=[]
    def make():
        p=Serial(run, cost=30)
        instances.append(p)
        return p
    run=run_factory([Act(duration_s=1,state='question',ask='Which day?'), Act()], edit=edit,pol=make)
    run.go()
    assert instances[0].calls[0][1] == 26
    assert run.outcome.status == 'failed', run.outcome
    assert not any(x['via']=='answer' for x in run.sends())
    assert run.events('unjudged')[0]['fields']['reason']=='endpoint_failure'


def test_r5_refusal_delivery_through_real_adapter_and_driver():
    from cohbench.driver import turns
    from cohbench.runner.adapter import DriverEpisode
    from cohbench.runner.port import Pending
    from tests.driver.test_units import _episode, proj, RUN
    tree={'t':{**proj('running',[{'id':'item-s','runId':RUN,'type':'secret_request','secretStatus':'pending','label':'Access code'}]),
               'providerSessions':[{'capabilities':{'turns':{'supportsActiveSteering':True}}}]}}
    ep=_episode(tree)
    req=turns.secret_of(tree['t']['turnItems'][0],'t')
    adapter=object.__new__(DriverEpisode)
    adapter.ep,adapter._sent,adapter._pending,adapter.absorbed=ep,{}, {'item-s':req}, []
    sent=adapter.refuse_secret(Pending('secret','item-s','t',None), 'Continue without it.')
    assert adapter.correction_delivery(sent) is None
    tree['t']['turnItems'].append({'id':'user-'+sent.message_id,'messageId':sent.message_id,'type':'user_message',
                                 'inputIntent':'steer','startedAt':'2026-11-23T00:00:01Z','completedAt':'2026-11-23T00:00:03Z'})
    delivery=adapter.correction_delivery(sent)
    assert delivery.utc=='2026-11-23T00:00:01Z'
    assert ep.corrections==[]


def test_r5_native_answer_uses_resolution_time_in_driver_and_trajectory(monkeypatch):
    from cohbench.driver import turns
    from cohbench.runner.adapter import DriverEpisode
    from cohbench.runner.port import Pending
    from cohbench.trajectory import Hints, Segment, t3
    from tests.driver.test_units import _episode, proj
    from tests.trajectory import mini
    from tests.trajectory.conftest import run as normalise
    th=mini.thread('t')
    raw_run=mini.run_rec('t',1,0,10)
    opening=mini.user(raw_run,0,'Choose a day.')
    item=mini.item(raw_run,'user_input_request',1,5,requestId='req',questions=[{'id':'q','question':'Which day?','options':[]}])
    req={'id':'req','status':'pending'}
    tree={'t':proj('running',[opening,item],[req],runs=[raw_run])}
    ep=_episode(tree)
    ep.t3.on_respond=lambda tid,rid,answers:req.update(status='resolved',answers=answers,resolvedAt=mini.ts(4))
    monkeypatch.setattr('cohbench.driver.episode.utc_now',lambda:mini.ts(2))
    q=turns.question_of(item,'t')
    adapter=object.__new__(DriverEpisode)
    adapter.ep,adapter._sent,adapter._pending,adapter.absorbed=ep,{}, {item['id']:q}, []
    sent=adapter.answer(Pending('question',item['id'],'t','req'), {'q':'Tuesday.'})
    assert sent.delivered_utc==mini.ts(4).replace('.000Z','Z')
    assert sent.utc==mini.ts(2)
    export=mini.export([(th,[raw_run],[opening,item,mini.assistant(raw_run,7,'Done.')])])
    export['threads'][0]['runtimeRequests']=[req]
    result=normalise([Segment(t3.from_export(export))],hints=Hints(answers={'req':'Tuesday.'}))
    answer=next(x for x in result.trajectory.events if x['type']=='answer')
    assert answer['t']==4000


def test_r5_cleanup_count_is_in_episode_run_record(run_factory):
    class Cleanup(Serial):
        def flush(self):
            return {'blocked_transport_threads':1,'pending_accounting_threads':0}
    def edit(d):
        no_events(d)
        d['user']['return_delay_ms']=30_000
    run=run_factory([Act(final='Done.'),Act()],edit=edit,pol=lambda:Cleanup(run))
    run.go()
    assert run.run_json['classifier_cleanup']=={'blocked_transport_threads':1,'pending_accounting_threads':0}


@pytest.mark.parametrize('handback,kind,deadline', [(5,'handback',12),(7,'grace',13)])
def test_r5_reset_chooses_dispatch_instant_before_judging(run_factory, handback, kind, deadline):
    def edit(d):
        _reset_mid_turn(d,grace_ms=2000)
        d['user']['return_delay_ms']=10_000
    instances=[]
    def make():
        p=Serial(run,cost=.25)
        instances.append(p)
        return p
    run=run_factory([Act(duration_s=handback,writes=[(1,'out/step1.txt','one')],
                         messages=[(handback,'First.')],final='Last.'),Act(final='Done.'),Act()],
                    edit=edit,pol=make,poll_s=1,finish_s=3)
    original=run.factory
    def factory(spec):
        ep=original(spec)
        interrupt=ep.interrupt
        def stop():
            interrupt()
            # The real driver can read an already-idle tree after a no-op stop.
            if ep.drain is None:
                ep.drain=(ep.cur_run,[])
        ep.interrupt=stop
        return ep
    run.factory=factory
    run.go()
    assert run.run_json['events']['handoff']['reset'] is not None, (run.outcome, run.run_json['events']['handoff'])
    assert run.run_json['events']['handoff']['reset']['at']==kind
    assert [c[1] for c in instances[0].calls[:2]]==[deadline,deadline]
    assert not run.run_json['overruns']['count']
    assert run.outcome.status=='usable',run.outcome


def test_r5_strict_replay_keeps_snapshot_context_and_missing_audits(run_factory, monkeypatch):
    from cohbench.runner.runner import Runner
    from .policy_replay import snapshot, compare_same, without_verdicts, differences
    import copy
    captured=[]
    original=Runner.run
    def capture(r):
        result=original(r)
        captured.append(snapshot(r))
        return result
    monkeypatch.setattr(Runner,'run',capture)
    run_factory([Act(final='Done.'),Act()],edit=no_events).go()
    base=captured[0]
    changed=copy.deepcopy(base)
    changed['classifier'][0]['as_of']='2026-11-22T13:00:00Z'
    changed['classifier'][0]['record']['context']={'wrong':True}
    assert compare_same(base,changed)['differences']
    expected=without_verdicts(base,expected=True)
    changed=copy.deepcopy(expected)
    changed['log']['judgements'].clear()
    assert differences(expected,changed)
    changed=copy.deepcopy(expected)
    changed['transcript'][1]['text']='A different observed input.'
    assert differences(expected,changed)


def test_r5_replay_checks_inputs_by_identity_even_with_repeated_text():
    from cohbench.runner.policy import DeliveredTranscript, Entry, Judgement
    from .policy_replay import call_input, replay_answer
    tr=DeliveredTranscript([Entry('user','Continue.')],[()])
    cards=[{'facts':[{'id':'a','text':'One.'}]},{'facts':[{'id':'b','text':'Two.'}]}]
    oracle={key:(key,'Same text.',call_input(card,tr),Judgement('no_request'),0)
            for key,card in zip(['first','second'],cards)}
    assert replay_answer(oracle,'second',cards[1],tr,'Same text.')[0].label=='no_request'
    with pytest.raises(AssertionError,match='second'):
        replay_answer(oracle,'second',cards[0],tr,'Same text.')


@pytest.mark.parametrize('route,want', [('plain',2),('steer',2),('restart',2)])
def test_r5_message_routes_use_same_driver_and_trajectory_instant(route,want,monkeypatch):
    from cohbench.runner.adapter import DriverEpisode
    from cohbench.trajectory import Hints, Segment, t3
    from tests.driver.test_units import _episode, proj
    from tests.trajectory import mini
    from tests.trajectory.conftest import run as normalise
    th=mini.thread('t')
    first=mini.run_rec('t',1,0,3 if route=='restart' else 1,
                       status='interrupted' if route=='restart' else 'completed')
    second=mini.run_rec('t',2,4 if route=='restart' else 2,10)
    opening=mini.user(first,0,'Please continue.')
    message=mini.user(second,2,'Use the next day.',inputIntent='steer' if route=='steer' else 'turn_start')
    message['completedAt']=mini.ts(4)
    items=[opening,mini.assistant(first,.5,'Noted.'),message,mini.assistant(second,8,'Done.')]
    ep=_episode({'t':proj('running',items,runs=[first,second])})
    monkeypatch.setattr('cohbench.driver.episode.utc_now',lambda:mini.ts(1))
    sent=ep._new_sent('refusal' if route!='plain' else 'reply','t',text=message['text'])
    sent.message_id=message['messageId']
    sent.mechanism={'plain':'queued','steer':'native_steer','restart':'interrupt_restart'}[route]
    adapter=object.__new__(DriverEpisode)
    adapter.ep,adapter._sent,adapter.absorbed=ep,{},[]
    port_sent=adapter._port_sent(sent)
    assert port_sent.delivery_pending
    observed=adapter.correction_delivery(port_sent)
    assert observed is not None and observed.utc==mini.ts(want).replace('.000Z','Z')
    result=normalise([Segment(t3.from_export(mini.export([(th,[first,second],items)])))],
                     hints=Hints(user_kinds={message['messageId']:'reply'}))
    event=next(x for x in result.trajectory.events if x['type']=='user_message' and x['text']==message['text'])
    assert event['t']==want*1000


def test_r5_unjudged_secret_keeps_fixed_refusal(run_factory):
    class LateSecret(Serial):
        def judge(self, card, transcript, output, *, deadline):
            self.cost = 0 if output == 'Done.' else 30
            return super().judge(card, transcript, output, deadline=deadline)
    def edit(d):
        no_events(d)
        d['user']['return_delay_ms'] = 30_000
    run=run_factory([Act(state='secret',ask='Synthetic credential, please.'),
                     Act(final='Done.'),Act()],edit=edit,pol=lambda:LateSecret(run))
    run.go()
    assert run.outcome.status=='usable',run.outcome
    assert len(run.events('unjudged'))==1
    assert any(s['via']=='refuse_secret' for s in run.sends())
    assert not run.run_json['overruns']['count']


def test_r5_correction_receipt_preserves_submillisecond_instant():
    from tests.driver.test_units import _episode, proj, RUN
    ep=_episode({'t':proj('running')})
    sent=ep._new_sent('correction','t',text='Use the next day.')
    ep.corrections.append({'message_id':sent.message_id,'requested':'native_steer',
                           'observed':None,'run_before':RUN,'run_after':None})
    instant='2026-11-22T00:00:03.123456Z'
    ep.t3.tree_['t']=proj('running',[{'id':'msg:'+sent.message_id,'type':'user_message',
                                   'runId':RUN,'completedAt':instant}])
    assert ep.correction_delivery(sent).delivered_utc==instant
