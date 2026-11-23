"""Opt-in replay of every ScriptedPolicy episode, using forked fixture state.

Run with COHBENCH_POLICY_REPLAY=out/property.json pytest tests/runner
-p tests.runner.policy_replay. No endpoint or model is involved.
"""
import copy
import dataclasses
import hashlib
import json
import os
import pickle
import shutil
import tempfile
import traceback
from pathlib import Path

import pytest

from cohbench.runner import ScriptedPolicy
from cohbench.runner.eventlog import EventLog, read
from cohbench.runner.runner import Runner
from cohbench.runner import record
from .fake import FakeEpisode

REPORT = []


def snapshot(r):
    ep = record.episode_record(r.task, r.cfg, r)
    excluded = r.ep.excluded()
    handoff = record.handoff(r.task, r.cfg, r, ep, excluded, {})
    return json.loads(json.dumps({
        'episode': ep, 'handoff': dataclasses.asdict(handoff),
        'transcript': [dict(role=x.entry.role, text=x.entry.text, **x.meta) for x in r.ordered()],
        'classifier': r.classifier, 'awareness_record': r.awareness_record,
        'manifest': record.run_manifest(r.task, r.cfg, r, None, started_utc='', ended_utc='', wall_s=0,
            excluded=excluded, status='', failure=None, detail=None, export={}, close={}),
        'log': list(read(r.log.path)),
        'observed': [dict(item=s.output.item_id, text=s.output.text, utc=s.line.meta['utc']) for s in r._observed.values()],
        'baselines': {k: dataclasses.asdict(v) for k,v in r.ctx.fired.items()},
    }, default=str))


def differences(a, b, path=''):
    if type(a) is not type(b):
        return [path]
    if isinstance(a, dict):
        out = []
        for k in sorted(a.keys() | b.keys()):
            if k not in a or k not in b:
                out.append(f'{path}/{k}')
            else:
                out.extend(differences(a[k], b[k], f'{path}/{k}'))
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f'{path}/length']
        return [p for i,(x,y) in enumerate(zip(a,b)) for p in differences(x,y,f'{path}/{i}')]
    return [] if a == b else [path]


class PrefixEnd(BaseException):
    pass


def call_input(card, transcript):
    return {'card': copy.deepcopy(card),
            'transcript': [dataclasses.asdict(e) for e in transcript],
            'delivered_ids': [list(x) if x is not None else None
                              for x in getattr(transcript, 'delivered_ids', ())]}


def replay_answer(oracle, item, card, transcript, output):
    identity, expected_text, inputs, value, cost = oracle[item]
    assert identity == item and output == expected_text
    assert call_input(card, transcript) == inputs, f'judge input differs for {item}'
    return value, cost


def canonical(value):
    """Only measured classifier latency is masked. Identity is never text."""
    value = copy.deepcopy(value)
    def latency(row):
        rec = row.get('record', {})
        rec.pop('ms', None)
        for q in rec.get('questions', []):
            q.pop('ms', None)
    for row in value['classifier']:
        latency(row)
    if value.get('awareness_record'):
        latency(value['awareness_record'])
    for row in value['log']:
        if row['type'] == 'verdict':
            latency(row)
    for name, key in [('log','output'), ('handoff','ref')]:
        rows = value[name] if name == 'log' else value[name]['runner_events']
        verdicts = [r for r in rows if r['type'] in ('verdict','unjudged')]
        indexed = {r[key]['item']: r for r in verdicts}
        assert len(indexed) == len(verdicts), 'duplicate output identity'
        grouped = {'judgements': indexed, 'other': [r for r in rows if r['type'] not in ('verdict','unjudged')]}
        if name == 'log':
            value[name] = grouped
        else:
            value[name]['runner_events'] = grouped
    rows = value['classifier']
    value['classifier'] = {r['output']['item']:r for r in rows}
    assert len(rows) == len(value['classifier']), 'duplicate classifier output identity'
    return value


def without_verdicts(value, *, expected=False, native_failure=None):
    """Remove verdict fields; synthesize and compare the required missing audits."""
    v=copy.deepcopy(value)
    ids={r['output']['item'] for r in v['classifier']}
    reason=lambda item:'endpoint_failure' if item==native_failure else 'deadline'
    if expected:
        for row in v['transcript']:
            if row.get('item') in ids:
                row['label']=None
                row['unjudged']=reason(row['item'])
        for row in v['handoff']['runner_events']:
            if row['type']=='verdict':
                row['type']='unjudged'
                row['fields']={'reason':reason(row['ref']['item'])}
        logs=[]
        for row in v['log']:
            if row['type']=='verdict':
                row={k:row[k] for k in ('n','utc','clock_ms','output','output_kind','text') if k in row}
                row.update(type='unjudged',reason=reason(row['output']['item']))
            logs.append(row)
        v['log']=logs
    if not expected:
        assert not v['classifier'], 'missing replay unexpectedly contains a verdict'
    v['classifier']=[]
    for baseline in v['baselines'].values():
        baseline.pop('verdicts',None)
    return canonical(v)


def compare_same(baseline, other):
    return {'differences':differences(canonical(baseline),canonical(other))}


@pytest.fixture(autouse=True)
def replay_scripted(request, monkeypatch):
    destination=os.environ.get('COHBENCH_POLICY_REPLAY')
    if not destination:
        return
    original=Runner.run
    index=0

    def wrapped(r):
        nonlocal index
        if not isinstance(r.policy,ScriptedPolicy) or not isinstance(r.ep,FakeEpisode):
            return original(r)
        index+=1
        scenario=f'{request.node.nodeid}#{index}'
        root=Path(tempfile.mkdtemp(prefix='cohbench-policy-replay-'))
        mode='baseline'
        calls=[]
        points=[]
        oracle={}
        expected_points=[]
        divergence=None
        point_index=0
        active_item=None
        policy=r.policy
        judge=policy.judge
        output=r._output
        due=r._due_as_of

        def checkpoint(kind, identity, reply=False, native_failure=None):
            nonlocal point_index, divergence
            snap=snapshot(r)
            point={'kind':kind,'identity':identity,'reply':reply,'snapshot':snap}
            if mode=='baseline':
                points.append(point)
            elif mode=='missing':
                expected=expected_points[point_index]
                assert (kind,identity)==(expected['kind'],expected['identity']), 'input changed without an absent reply'
                changed=differences(without_verdicts(expected['snapshot'],expected=True,native_failure=native_failure),
                                    without_verdicts(snap))
                assert not changed, changed
                point_index+=1
                if native_failure is not None or (expected['reply'] and not reply):
                    divergence={'checkpoint':point_index,'kind':kind,'identity':identity,
                                'reason':'absent native-question reply' if native_failure else 'absent verdict reply at hand-back decision'}
                    raise PrefixEnd()
            return snap

        def observe(o,t,**kwargs):
            nonlocal active_item
            active_item=o.item_id
            try:
                result=output(o,t,**kwargs)
            except Exception as exc:
                from cohbench.user.api import DeadlineExceeded, EndpointError
                if mode=='missing' and o.kind=='question' and isinstance(exc,EndpointError) and isinstance(exc.__cause__,DeadlineExceeded):
                    r._unjudged(o,'endpoint_failure')
                    checkpoint('output',o.item_id,native_failure=o.item_id)
                raise
            checkpoint('output',o.item_id)
            return result

        def decision(hb,t,**kwargs):
            result=due(hb,t,**kwargs)
            checkpoint('decision', [hb.h,t.isoformat()], reply='verdict_reply' in result.causes)
            return result

        def capture(card,tr,text):
            before=r.ep.c.clock
            inputs=call_input(card,tr)
            try:
                result=judge(card,tr,text)
            except Exception as exc:
                calls.append((active_item,text,inputs,exc,r.ep.c.clock-before))
                raise
            calls.append((active_item,text,inputs,result,r.ep.c.clock-before))
            return result

        r._output=observe
        r._due_as_of=decision
        children=[]
        for child_mode in ('same','missing'):
            read_fd,write_fd=os.pipe()
            ready_read,ready_write=os.pipe()
            pid=os.fork()
            if pid==0:
                mode=child_mode
                try:
                    os.close(write_fd);os.close(ready_read)
                    child_dir=root/mode
                    shutil.copytree(r.ep.out,child_dir/'driver')
                    old_out=r.ep.out
                    r.ep.out=child_dir/'driver'
                    for watcher in r.ep.watchers:
                        watcher.root=r.ep.out/watcher.root.relative_to(old_out)
                    old_log=r.log
                    log_path=child_dir/'events.jsonl'
                    shutil.copyfile(old_log.path,log_path)
                    old_log.close()
                    r.log=EventLog(log_path,r.utcnow,r._log_clock)
                    r.log.n=old_log.n
                    os.write(ready_write,b'1');os.close(ready_write)
                    os.read(read_fd,1)
                    saved=pickle.loads((root/'oracle').read_bytes())
                    oracle={c[0]:c for c in saved['calls']}
                    assert len(oracle)==len(saved['calls']), 'duplicate output identity in oracle'
                    expected_points=saved['points']
                    class SerialOracle:
                        verdict_deadline_s=25
                        def clock(self):
                            return r.ep.c.clock
                        def judge(self,card,tr,text,*,deadline):
                            value,cost=replay_answer(oracle,active_item,card,tr,text)
                            r.ep.c.advance(cost)
                            if isinstance(value,Exception):
                                raise value
                            if mode=='missing':
                                from cohbench.user.api import DeadlineExceeded
                                raise DeadlineExceeded('missing replay verdict')
                            return value
                        def classify_awareness(self,*args):
                            return policy.classify_awareness(*args)
                    r.policy=SerialOracle()
                    try:
                        original(r)
                    except PrefixEnd:
                        pass
                    value=snapshot(r)
                    (root/f'{mode}.json').write_text(json.dumps({'snapshot':value,'divergence':divergence,'checkpoints':point_index}))
                except BaseException:
                    (root/f'{mode}.error').write_text(traceback.format_exc())
                finally:
                    os._exit(0)
            os.close(read_fd);os.close(ready_write)
            os.read(ready_read,1);os.close(ready_read)
            children.append((pid,write_fd,child_mode))
        policy.judge=capture
        try:
            result=original(r)
            baseline=snapshot(r)
        finally:
            (root/'oracle').write_bytes(pickle.dumps({'calls':calls,'points':points}))
            for pid,fd,child_mode in children:
                os.write(fd,b'1');os.close(fd)
            for pid,fd,child_mode in children:
                os.waitpid(pid,0)
        (root/'baseline.json').write_text(json.dumps(baseline))
        row={'scenario':scenario,'artifacts':str(root),'verdicts':len(calls),'checkpoints':len(points)}
        for child_mode in ('same','missing'):
            error=root/f'{child_mode}.error'
            if error.exists():
                row[child_mode]={'error':error.read_text()}
            else:
                child=json.loads((root/f'{child_mode}.json').read_text())
                if child_mode=='same':
                    row[child_mode]=compare_same(baseline,child['snapshot'])
                else:
                    changed=[] if child['divergence'] else differences(without_verdicts(baseline,expected=True),without_verdicts(child['snapshot']))
                    row[child_mode]={'differences':changed,'divergence':child['divergence'],'checkpoints':child['checkpoints']}
        if all(not row[m].get('error') and not row[m].get('differences') for m in ('same','missing')):
            shutil.rmtree(root)
            row.pop('artifacts')
        REPORT.append(row)
        Path(destination).parent.mkdir(parents=True,exist_ok=True)
        Path(destination).write_text(json.dumps(REPORT,indent=2)+'\n')
        for child_mode in ('same','missing'):
            assert not row[child_mode].get('error'),row
            assert not row[child_mode]['differences'],row
        return result
    monkeypatch.setattr(Runner,'run',wrapped)
