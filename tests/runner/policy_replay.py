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


@pytest.fixture(autouse=True)
def replay_scripted(request, monkeypatch):
    destination = os.environ.get('COHBENCH_POLICY_REPLAY')
    if not destination:
        return
    original = Runner.run
    index = 0

    def wrapped(r):
        nonlocal index
        if not isinstance(r.policy, ScriptedPolicy) or not isinstance(r.ep, FakeEpisode):
            return original(r)
        index += 1
        scenario = f'{request.node.nodeid}#{index}'
        root = Path(tempfile.mkdtemp(prefix='cohbench-policy-replay-'))
        calls = []
        judge = r.policy.judge
        def capture(card, tr, output):
            before = r.ep.c.clock
            try:
                result = judge(card,tr,output)
            except Exception as exc:
                calls.append((output, exc, r.ep.c.clock-before))
                raise
            calls.append((output,result,r.ep.c.clock-before))
            return result
        children = []
        for mode in ('same','missing'):
            read_fd, write_fd = os.pipe()
            ready_read, ready_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(write_fd)
                    os.close(ready_read)
                    # Copy fixture files before the parent can mutate them.
                    child_dir = root / mode
                    shutil.copytree(r.ep.out, child_dir / 'driver')
                    old_out = r.ep.out
                    r.ep.out = child_dir / 'driver'
                    for watcher in r.ep.watchers:
                        watcher.root = r.ep.out / watcher.root.relative_to(old_out)
                    old_log = r.log
                    log_path = child_dir / 'events.jsonl'
                    shutil.copyfile(old_log.path, log_path)
                    old_log.close()
                    r.log = EventLog(log_path, r.utcnow, r._log_clock)
                    r.log.n = old_log.n
                    os.write(ready_write,b"1"); os.close(ready_write)
                    os.read(read_fd,1)
                    oracle = pickle.loads((root/'oracle').read_bytes())
                    remaining = list(oracle)
                    class BatchOracle:
                        def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
                            from .test_classifier_review4 import ScheduledJob
                            from types import SimpleNamespace
                            pos = next((i for i,c in enumerate(remaining) if c[0] == output), None)
                            if pos is None:
                                value, cost = AssertionError(f'output absent from oracle: {output}'), 0
                            else:
                                _, value, cost = remaining.pop(pos)
                            # The oracle returns saved verdicts immediately. Cost is
                            # carried only by fixtures that explicitly simulate it.
                            if mode == 'missing' and not isinstance(value, Exception):
                                value = None
                            r.ep.c.advance(cost)
                            return ScheduledJob(SimpleNamespace(clock=r.ep.c), value, 0)
                        def classify_awareness(self, *args):
                            return r_original_policy.classify_awareness(*args)
                    r_original_policy = r.policy
                    r.policy = BatchOracle()
                    original(r)
                    value = snapshot(r)
                    (root/f'{mode}.json').write_text(json.dumps(value))
                except BaseException:
                    (root/f'{mode}.error').write_text(traceback.format_exc())
                finally:
                    os._exit(0)
            os.close(read_fd)
            os.close(ready_write)
            os.read(ready_read,1); os.close(ready_read)
            children.append((pid,write_fd,mode))
        r.policy.judge = capture
        try:
            result = original(r)
            baseline = snapshot(r)
        finally:
            (root/'oracle').write_bytes(pickle.dumps(calls))
            for pid,fd,mode in children:
                os.write(fd,b'1'); os.close(fd)
            for pid,fd,mode in children:
                os.waitpid(pid,0)
        (root/'baseline.json').write_text(json.dumps(baseline))
        result_row = {'scenario':scenario,'artifacts':str(root),'verdicts':len(calls)}
        for mode in ('same','missing'):
            error = root/f'{mode}.error'
            if error.exists():
                result_row[mode] = {'error':error.read_text()}
            else:
                other = json.loads((root/f'{mode}.json').read_text())
                result_row[mode] = compare_same(baseline,other) if mode == 'same' else compare_missing(baseline,other)
        REPORT.append(result_row)
        Path(destination).parent.mkdir(parents=True,exist_ok=True)
        Path(destination).write_text(json.dumps(REPORT,indent=2)+'\n')
        for mode in ("same", "missing"):
            assert not result_row[mode].get("error"), result_row
            assert not result_row[mode]["differences"], result_row
        return result
    monkeypatch.setattr(Runner,'run',wrapped)


# These are verdict input/timing fields, or append-only journal write fields.
VERDICT_METADATA = ('as_of', 'card_items', 'batch_outputs', 'completed_at', 'effective_at')
JOURNAL_METADATA = ('n', 'clock_ms')


def canonical(value):
    value = copy.deepcopy(value)
    def verdict(row):
        for field in VERDICT_METADATA:
            row.pop(field, None)
        rec = row.get('record', {})
        rec.pop('ms', None)
        rec.pop('context', None)
        for question in rec.get('questions', []):
            question.pop('ms', None)
        return row
    value['classifier'] = [verdict(row) for row in value['classifier']]
    if value.get('awareness_record'):
        verdict(value['awareness_record'])
        value['awareness_record'].pop('utc', None)
    log = value['log']
    for row in log:
        for field in JOURNAL_METADATA:
            row.pop(field, None)
        if row['type'] == 'verdict':
            verdict(row)
        # For these rows utc is the logger's write time. Semantic instants
        # have their own at/start/sent_utc fields and remain compared.
        if row['type'] not in ('verdict', 'correction_delivered', 'notice_delivered',
                               'message_delivered', 'delivery_after_end', 'reset_notice_sent',
                               'probe', 'controlled_reset', 'delivery_mark_unavailable') and not (row['type']=='hand_back' and row.get('state')=='interrupted'):
            row.pop('utc', None)
    value['log'] = {
        'verdict': {row['output']['item']: row for row in log if row['type'] == 'verdict'},
        'other': [row for row in log if row['type'] != 'verdict'],
    }
    events = value['handoff']['runner_events']
    value['handoff']['runner_events'] = {
        'verdict': {row['ref']['item']: row for row in events if row['type'] == 'verdict'},
        'other': [row for row in events if row['type'] != 'verdict'],
    }
    return value


def compare_same(baseline, other):
    a, b = canonical(baseline), canonical(other)
    changed = differences(a, b)
    # Retained and reported separately, not masked: verdict counters become
    # available at completion, which can differ from serial policy timing.
    baselines = [p for p in changed if p.startswith('/baselines/') and '/verdicts/' in p]
    return {'differences': [p for p in changed if p not in baselines], 'verdict_baseline_differences': baselines}


def compare_missing(baseline, other):
    def inputs(v):
        return [{k: row[k] for k in ('role','text','wire_text','utc','item','ref') if k in row}
                for row in v['transcript']]
    a, b = inputs(baseline), inputs(other)
    n = 0
    while n < min(len(a),len(b)) and a[n] == b[n]:
        n += 1
    divergence = None
    if a != b:
        instants = [v['utc'] for seq in (a,b) for v in seq[n:n+1]]
        instants += [v['manifest']['end']['utc'] for v in (baseline,other) if v['manifest']['end']['utc']]
        cutoff = min(instants)
        divergence = {'input_index': n, 'at': cutoff,
                      'baseline_next': a[n] if n<len(a) else None,
                      'missing_next': b[n] if n<len(b) else None}
    else:
        cutoff = None
    # A policy reply can be due but cut off before dispatch. Its absence
    # still changes the hand-back decision and the following wait/end.
    missing_waits = {(r['utc'], json.dumps(r['ref'], sort_keys=True)) for r in other['handoff']['runner_events'] if r['type']=='wait'}
    absent_wait = next((r for r in baseline['handoff']['runner_events']
                       if r['type']=='wait' and r['fields'].get('causes')==['verdict_reply']
                       and (r['utc'], json.dumps(r['ref'],sort_keys=True)) not in missing_waits), None)
    if absent_wait is not None and (cutoff is None or absent_wait['utc']<cutoff):
        cutoff = absent_wait['utc']
        divergence = {'input_index': n, 'at': cutoff, 'reason': 'absent policy reply at hand-back decision',
                      'reply_target': absent_wait['ref'], 'scheduled_wait_end': absent_wait['end_utc']}
    def project(v):
        rows = []
        for row in v['handoff']['runner_events']:
            if row['type'] in ('verdict','unjudged'):
                continue
            if cutoff and row['utc'] >= cutoff and row['type'] in ('episode_end','budget_cutoff'):
                continue
            if cutoff and row['utc'] >= cutoff and row['type'] == 'planted' and row['fields']['phase'] == 'not_reached':
                continue
            if cutoff and row['utc'] > cutoff:
                continue
            if cutoff and row['type']=='wait' and row['utc']>=cutoff and (row['utc'],json.dumps(row['ref'],sort_keys=True)) not in missing_waits:
                continue
            if row['type'] == 'wait' and 'verdict_reply' in row['fields'].get('causes',[]):
                if row['fields']['causes'] == ['verdict_reply']:
                    continue
                row = copy.deepcopy(row)
                row['fields']['causes'].remove('verdict_reply')
                row['fields']['replies_to'] = []
            rows.append(row)
        observations = [row for row in v['observed'] if cutoff is None or row['utc']<=cutoff]
        baselines = {}
        for key,snap in v['baselines'].items():
            fired = v['manifest']['events'][key]['fired_utc']
            if cutoff and fired and fired>cutoff:
                continue
            snap = copy.deepcopy(snap); snap.pop('verdicts',None)
            baselines[key]=snap
        return {'inputs': inputs(v)[:n] if cutoff else inputs(v),
                'observations': observations, 'events': rows, 'baselines': baselines,
                'probes': [p for p in v['manifest']['probes'] if not cutoff or p.get('utc','') <= cutoff]}
    return {'divergence': divergence, 'common_inputs': n,
            'differences': differences(project(baseline), project(other))}
