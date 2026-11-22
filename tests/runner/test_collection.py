"""Task collection admission and the episode port on the fake."""

import pytest

from cohbench.runner import load
from cohbench.runner.task import TaskError
from cohbench.trajectory.collection import LIMIT
from .conftest import no_events, write_task
from .fake import Act
from .test_runner import _reset_mid_turn, SLOT_Q, SLOT_POLICY, policy

PATH = '~/.local/state/profile/runs.jsonl'


def edit(d):
    no_events(d)
    d['collect'] = [{'name': 'attempts', 'path': PATH}]
    d['scoring']['observations'] = {'attempts': [{'name': 'profile', 'regex': 'profile', 'log': 'attempts'}]}


@pytest.mark.parametrize('row', [
    {'name': 'bad-name', 'path': PATH}, {'name': '', 'path': PATH}, {'name': 'UPPER', 'path': PATH},
    {'name': 'attempts', 'path': '/work/project/runs.jsonl'}, {'name': 'attempts', 'path': '//work/project/runs.jsonl'}, {'name': 'attempts', 'path': '/work/other/../project/x'},
    {'name': 'attempts', 'path': '~/../../work/project/x'}, {'name': 'attempts', 'path': 'relative/runs.jsonl'},
])
def test_collection_admission_rejects_bad_names_and_paths(tmp_path, row):
    def change(d):
        edit(d)
        d['collect'] = [row]
    with pytest.raises(TaskError, match='collect'):
        load(write_task(tmp_path, change))


@pytest.mark.parametrize('data,status', [(b'one\n', 'collected'), (None, 'absent'), (b'x' * (LIMIT + 1), 'oversize')])
def test_fake_collection_is_in_the_episode_record(run_factory, data, status):
    r = run_factory([Act(), Act()], edit=edit, collected_files={PATH: data}).go()
    assert r.outcome.status == 'usable'
    assert r.record['collected']['attempts']['status'] == status
    assert r.record['attempt_logs'] == {'profile': 'attempts'}


def test_failing_collection_read_is_an_export_failure(run_factory):
    r = run_factory([Act(), Act()], edit=edit, collected_files={PATH: OSError('read failed')}).go()
    assert r.outcome.failure == 'export_rejected'
    assert r.run_json['export']['stage'] == 'collection'


def test_fake_collection_keeps_both_sides_of_reset(run_factory):
    def change(d):
        edit(d)
        _reset_mid_turn(d)
    r = run_factory([Act(duration_s=10, writes=[(2, 'out/step1.txt', 'one')]), Act(), Act()], edit=change, collected_files={PATH: b'first\n'})
    factory = r.factory

    def make(spec):
        ep = factory(spec)
        reset = ep.reset
        def reset_and_write(*args, **kwargs):
            sent = reset(*args, **kwargs)
            assert ep.collected_files == {}
            ep.collected_files[PATH] = b'second\n'
            return sent
        ep.reset = reset_and_write
        return ep
    r.factory = make
    r.go()
    assert r.fake.collect().content('attempts') == b'first\nsecond\n'
    assert r.record['collected']['attempts']['bytes'] == 13


def test_verdict_keeps_selected_ids_when_the_reply_is_cut_off(run_factory):
    def change(d):
        no_events(d)
        d['budgets']['user_wait_ms'] = 500
        d['user']['return_delay_ms'] = 30000
    r = run_factory([Act(final=SLOT_Q), Act()], edit=change, pol=policy(SLOT_POLICY)).go()
    fields = r.events('verdict')[0]['fields']
    assert fields['answer_ids'] == ['slot'] and fields['reply'] is None


def test_reset_collection_failure_remains_an_export_failure(run_factory):
    def change(d):
        edit(d)
        _reset_mid_turn(d)
    r = run_factory([Act(duration_s=10, writes=[(2, 'out/step1.txt', 'one')]), Act(), Act()],
                    edit=change, collected_files={PATH: OSError('failed before reset')}).go()
    assert r.outcome.failure == 'export_rejected'
    assert r.run_json['export']['stage'] == 'collection'
    assert r.fake.segment == 2
