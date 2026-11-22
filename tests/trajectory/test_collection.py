"""Collected attempt logs, their clocks, blobs, and legacy readback."""

import hashlib
import json
from datetime import datetime

import pytest

from cohbench.trajectory import bundle, observations, replay
from cohbench.trajectory.collection import Collected, Collector, LIMIT, captured
from cohbench.trajectory.io import Trajectory
from . import mini
from .test_replay import raw_record

ORIGIN = round(datetime.fromisoformat(mini.T0.replace('Z', '+00:00')).timestamp() * 1000)


def line(start=1000, end=2000, state='ab' * 32, result='ops=12'):
    return json.dumps({'start_ms': ORIGIN + start, 'end_ms': ORIGIN + end, 'state': state, 'result': result}).encode() + b'\n'


def collect(data):
    c = Collector([{'name': 'attempts', 'path': '~/.local/state/profile/runs.jsonl'}])
    c.capture(1, lambda p: captured(data))
    return c.result()


def test_log_observation_order_end_exclusion_and_malformed_lines():
    data = line(7000, 8000) + line(1000, 2000) + line(10000, 11000) + line(19000, 21000) + b'bad\n{}\n\xff\n'
    traj = Trajectory({'excluded': [{'us': 9000000, 'us_end': 12000000, 'reason': 'handoff'}]}, [{'seq': 0, 'type': 'episode_end', 'us': 20000000}])
    spec = observations.TaskObservations.from_dict({'attempts': [{'name': 'profile', 'regex': 'profile', 'log': 'attempts'}]})
    obs = observations.build(traj, None, spec, collected=collect(data), origin=mini.T0)
    assert [(r['t'], r['t_end']) for r in obs.records] == [(1000, 2000), (7000, 8000), (9000, 9000)]
    assert obs.header['malformed_logs'] == {'attempts': 3}
    assert all(r['source'] == 'log' and r['state'] is None and r['log_state'] == 'ab' * 32 for r in obs.records)
    assert all(r['result'] == {'status': 'complete', 'fingerprint': hashlib.sha256(b'ops=12').hexdigest()} for r in obs.records)
    assert observations.validate(obs) == []


@pytest.mark.parametrize('data,status', [(None, 'absent'), (b'', 'collected'), (b'x' * (LIMIT + 1), 'oversize')])
def test_collection_status_and_cap(data, status):
    result = collect(data)
    assert result.records['attempts']['status'] == status
    assert result.records['attempts']['bytes'] == len(data or b'')
    assert (result.records['attempts']['sha256'] is not None) == (status == 'collected')


def test_collection_combines_reset_segments_once_and_caps_the_total():
    c = Collector([{'name': 'attempts', 'path': '~/runs.jsonl'}])
    c.capture(1, lambda p: captured(b'first\n'))
    c.capture(1, lambda p: captured(b'first\n'))
    c.capture(2, lambda p: captured(b'second\n'))
    assert c.result().content('attempts') == b'first\nsecond\n'
    c.capture(2, lambda p: captured(b'x' * LIMIT))
    assert c.result().records['attempts']['status'] == 'oversize'


def test_collected_blob_round_trip_and_raw_rebuild(tmp_path):
    root = raw_record(tmp_path)
    ex = root / 'export'
    data = collect(line())
    data.write(ex / 'collected' / 'blobs')
    m = json.loads((ex / 'manifest.json').read_text())
    m.update(collected=data.records, observation_sources=True)
    m['runner_events'][0]['fields']['answer_ids'] = ['mode']
    (ex / 'manifest.json').write_text(json.dumps(m))
    task = tmp_path / 'task' / 'task.json'
    d = json.loads(task.read_text())
    d['scoring']['observations']['attempts'] = [{'name': 'profile', 'regex': 'profile', 'log': 'attempts'}]
    task.write_text(json.dumps(d))
    _, b = replay.rebuild(root)
    assert b.episode['collected'] == data.records
    assert b.episode['attempt_logs'] == {'profile': 'attempts'}
    assert next(e for e in b.view.events if e['type'] == 'verdict')['answer_ids'] == ['mode']
    bundle.write(b, ex / 'bundle')
    back = bundle.read(ex / 'bundle')
    assert bundle.validate(back)["ok"]
    assert replay.verify(root) == {'ok': True, 'differs': [], 'validation': {}}
    sha = data.records['attempts']['sha256']
    assert (ex / 'bundle' / 'blobs' / sha[:2] / sha).read_bytes() == line()


def test_legacy_observations_without_source_read_without_changing_bytes(tmp_path):
    from .test_attempt_output import extract
    obs = extract('./bin/profile', 'PROFILE ops=12\n', by=False)
    for r in obs.records:
        r.pop('source')
    path = tmp_path / 'observations.jsonl'
    path.write_text(obs.dumps())
    back = observations.read(path)
    assert observations.validate(back) == []
    assert back.dumps() == path.read_text()


@pytest.mark.parametrize('value', [None, '', 'bad-name', '../attempts', 1])
def test_bad_attempt_log_name(value):
    with pytest.raises(ValueError):
        observations.TaskObservations.from_dict({'attempts': [{'name': 'profile', 'regex': 'profile', 'log': value}]})


def test_malformed_log_values_are_counted_without_aborting():
    bad = [line(state='no'), line(start=3000, end=2000), line(result='\ud800'), b'{"start_ms": 1e9999}\n', b'\n']
    data = collect(b''.join(bad) + line(result='one\u2028two'))
    traj = Trajectory({'excluded': []}, [{'seq': 0, 'type': 'episode_end', 'us': 20000000}])
    spec = observations.TaskObservations.from_dict({'attempts': [{'name': 'profile', 'regex': 'profile', 'log': 'attempts'}]})
    obs = observations.build(traj, None, spec, collected=data, origin=mini.T0)
    assert len(obs.records) == 1 and obs.header['malformed_logs'] == {'attempts': 5}


def test_old_raw_export_rebuild_keeps_shell_source_absent(tmp_path):
    root = raw_record(tmp_path)
    task = tmp_path / 'task' / 'task.json'
    d = json.loads(task.read_text())
    d['scoring']['observations']['attempts'] = [{'name': 'run', 'regex': './run'}]
    task.write_text(json.dumps(d))
    _, b = replay.rebuild(root)
    assert len(b.observations.records) == 1 and 'source' not in b.observations.records[0]
    assert all('answer_ids' not in e for e in b.view.events if e['type'] == 'verdict')
    bundle.write(b, root / 'export' / 'bundle')
    assert replay.verify(root) == {'ok': True, 'differs': [], 'validation': {}}
