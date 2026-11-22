"""Client parity, source-scoped events and effective file-read paths."""

import json
import os
from pathlib import Path
import re
import sys
import zipfile

import pytest

from cohbench.tasks import Options, generate, generate_to, steering, fillers
from cohbench.tasks.rng import Rng
from cohbench.runner.triggers import Context, holds
from tests.runner.fake import FakeClock, FakeWatcher
from tests.runner.test_triggers import fire, refresh
from .test_round3 import profile, history, logged_score
from .minipi import run_metric
from cohbench.trajectory.observations import TaskObservations


MATRIX = '''
class Sized:
    def __init__(self, values, size=None):
        self.values, self.size = values, size
    def __len__(self):
        return len(self.values) if self.size is None else self.size
    def __iter__(self):
        return iter(self.values)


def run(client):
    global OUTCOMES
    ids = client.list_ids()
    rates = client.get_rates()
    records = [client.get(i) for i in ids]
    index = dict(zip(ids, records))
    mirror = Mirror('', read_json=lambda path: records if path.endswith('records.json') else rates) if Mirror else None
    def check_mirror(method, arg, outcome):
        if mirror is None:
            return
        try:
            got = ('ok', getattr(mirror, method)(arg))
        except Exception as error:
            got = ('error', [type(error).__name__, str(error)])
        assert got == outcome, (method, got, outcome)
    forms = [ids[:2], tuple(ids[:2]), set(ids[:2]), dict.fromkeys(ids[:2]), dict.fromkeys(ids[:2]).keys(),
             (i for i in ids[:2]), Sized(ids[:2]), ids[0], [], (), set(), {}, '',
             [ids[0], ids[0]], ids[:51], Sized(ids[:51], 1), None, 3, [None], [[ids[0]]], ['missing-id']]
    OUTCOMES = []
    for arg in forms:
        try:
            value = client.get_many(arg)
        except Exception as error:
            OUTCOMES.append([type(error).__name__, str(error)])
            check_mirror('get_many', arg, ('error', OUTCOMES[-1]))
        else:
            check_mirror('get_many', arg, ('ok', value))
            assert value == [index[i] for i in arg]
            OUTCOMES.append(['ok', len(value)])
    for arg in [ids[0], 'missing-id', None, [], 3]:
        try:
            value = client.get(arg)
        except Exception as error:
            OUTCOMES.append([type(error).__name__, str(error)])
            check_mirror('get', arg, ('error', OUTCOMES[-1]))
        else:
            check_mirror('get', arg, ('ok', value))
            assert value == index[arg]
            OUTCOMES.append(['ok', 1])
    if EXPECTED is not None:
        assert OUTCOMES == EXPECTED, (OUTCOMES, EXPECTED)
    out = {}
    for rec in records:
        out[rec['category']] = out.get(rec['category'], 0) + rec['amount'] * rates['rates'][rec['currency']] // 10000
    return out
'''


@pytest.mark.parametrize('layout', range(3))
@pytest.mark.parametrize('horizon', ['h20', 'h80'])
@pytest.mark.parametrize('easy', [False, True])
def test_b51_public_and_hidden_clients_agree_on_argument_matrix(tmp_path, layout, horizon, easy):
    seed = next(s for s in range(300) if steering.plan(Options('steering', s, horizon)).theme.layout == layout
                and steering.has_easy_wins(s) == easy)
    d = tmp_path / 'instance'
    inst = generate_to(Options('steering', seed, horizon), d)
    theme = steering.plan(inst.options).theme
    vendor = {}
    exec(inst.workspace.text(f'vendor/{theme.module}.py'), vendor)
    client = vendor[theme.cls](str(d / 'workspace/sample'))
    public = {'EXPECTED': None, 'Mirror': None}
    exec(MATRIX, public)
    assert public['run'](client) == json.loads(inst.workspace.text('sample/expected.json'))
    tree = inst.workspace.texts()
    tree[theme.pkg + '/__init__.py'] = f'from vendor.{theme.module} import {theme.cls} as Mirror\nEXPECTED = ' + repr(public['OUTCOMES']) + '\n' + MATRIX
    metric = run_metric(inst.task['watcher']['metric']['command'], d / 'metric', tree)
    assert metric['correct'] == 1
    assert metric['round_trips'] == client.round_trips + inst.info['n_metric'] - inst.info['n_sample']


@pytest.mark.parametrize('layout', range(3))
@pytest.mark.parametrize('form', ['package', 'single_module', 'back_to_package', 'root_helper'])
@pytest.mark.parametrize('horizon', ['h20', 'h80'])
def test_b52_source_edits_fire_at_the_same_edit_after_refactors(tmp_path, layout, form, horizon):
    seed = next(s for s in range(100) if steering.plan(Options('steering', s, 'h80')).theme.layout == layout)
    d = tmp_path / 'instance'
    inst = generate_to(Options('steering', seed, horizon), d)
    theme = steering.plan(inst.options).theme
    key = inst.task['scoring']['answer_key']
    events = {e['name']: e for e in inst.task['events']}
    reset = events['handoff']['trigger']['any'][1] if horizon == 'h80' else None
    correction = events['correction']['trigger']
    if reset:
        assert reset['changes'] == {**steering.SOURCE_SCOPE, 'at_least': 1}
    assert correction['any'][0]['changes'] == {**steering.SOURCE_SCOPE, 'at_least': inst.info['k'], 'unit': 'batch', **({'since': 'handoff'} if reset else {})}
    tree = inst.workspace.texts()
    flat = '\n'.join(tree[steering.module_path(theme, name)] for name in ('config', 'fetch', 'totals', 'sync'))
    flat = '\n'.join(line for line in flat.splitlines() if not line.startswith('from .')) + '\n'
    if form == 'single_module':
        tree = {p: v for p, v in tree.items() if not p.startswith(theme.pkg + '/')}
        target = theme.pkg + '.py'
        tree[target] = flat
    elif form == 'back_to_package':
        tree = {p: v for p, v in tree.items() if not p.startswith(theme.pkg + '/')}
        tree[theme.pkg + '.py'] = flat
        target = theme.pkg + '/__init__.py'
        tree[target] = tree.pop(theme.pkg + '.py')
    elif form == 'root_helper':
        target = 'helper.py'
        tree[target] = flat
        tree[theme.pkg + '/__init__.py'] = 'from helper import run\n'
    else:
        target = steering.module_path(theme, 'sync')
    reading = run_metric(inst.task['watcher']['metric']['command'], d / 'metric', tree)
    assert reading['correct'] == 1 and reading['round_trips'] == key['baseline']
    clock = FakeClock()
    fw = FakeWatcher(tmp_path / 'watch', tree, clock)
    ctx = Context()
    refresh(fw, ctx)
    fw.write('admin/helper.py', 'chore = 1\n'); refresh(fw, ctx)
    if reset:
        assert not holds(reset, ctx)
        fw.write(target, tree.get(target, '') + '\n# First source edit.\n'); refresh(fw, ctx)
        assert holds(reset, ctx)
        fire(ctx, 'handoff', fw)
    for n in range(1, inst.info['k'] + 1):
        fw.write('admin/helper.py', f'chore = {n + 1}\n'); refresh(fw, ctx)
        assert not holds(correction, ctx)
        fw.write(target, tree[target] + f'\n# source edit {n}\n'); refresh(fw, ctx)
        assert holds(correction, ctx) == (n == inst.info['k'])
    assert key['ignore_paths'] == ['admin/**']


READERS = '''
import importlib.resources
import io
import mmap
import os
from pathlib import Path
import subprocess
import sys
import resource_data
_saved_run = run

def run(client):
    path = Path('resource_data/value.txt')
    mode = os.environ['READ_METHOD']
    if mode == 'builtin':
        with open(path, 'rb') as f: data = f.read()
    elif mode == 'pathlib':
        data = path.read_bytes()
    elif mode == 'io':
        with io.open(path, 'rb') as f: data = f.read()
    elif mode in ('os_open', 'dir_fd', 'fdopen', 'mmap'):
        directory = os.open('resource_data', os.O_RDONLY)
        try:
            fd = os.open('value.txt', os.O_RDONLY, dir_fd=directory) if mode == 'dir_fd' else os.open(path, os.O_RDONLY)
            try:
                if mode == 'mmap':
                    with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as m: data = m[:]
                elif mode == 'fdopen':
                    with io.open(fd, 'rb', closefd=False) as f: data = f.read()
                else:
                    data = os.read(fd, 100)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    elif mode == 'opener':
        directory = os.open('resource_data', os.O_RDONLY)
        try:
            with open('value.txt', 'rb', opener=lambda p, f: os.open(p, f, dir_fd=directory)) as stream:
                data = stream.read()
        finally:
            os.close(directory)
    elif mode.startswith('missing_'):
        directory = os.open('resource_data', os.O_RDONLY)
        try:
            try:
                fd = os.open('missing.txt', os.O_RDONLY, dir_fd=directory) if mode == 'missing_relative' else os.open('resource_data/missing.txt', os.O_RDONLY)
            except FileNotFoundError:
                data = b'absent'
            else:
                try: data = os.read(fd, 100)
                finally: os.close(fd)
        finally:
            os.close(directory)
    elif mode == 'resources':
        data = importlib.resources.files(resource_data).joinpath('value.txt').read_bytes()
    elif mode == 'zip_bytes':
        data = Path('helper.zip').read_bytes()
    elif mode == 'zipimport':
        sys.path.insert(0, str(Path('helper.zip').resolve()))
        import zipped_helper
        data = zipped_helper.VALUE
    elif mode == 'child':
        data = subprocess.check_output([sys.executable, '-c', "print(open('resource_data/value.txt').read())"])
    elif mode == 'anonymous_fd':
        r, w = os.pipe()
        os.write(w, b'ok')
        os.close(w)
        with io.open(r, 'rb') as f: data = f.read()
    assert data
    return _saved_run(client)
'''


def reader_workspace(tmp_path):
    inst = generate(Options('steering', 8205, 'h20'))
    work = tmp_path / 'work'
    inst.workspace.write(work)
    init = work / inst.info['pkg'] / '__init__.py'
    init.write_text(init.read_text() + READERS)
    (work / 'resource_data').mkdir()
    (work / 'resource_data/__init__.py').write_text('')
    (work / 'resource_data/value.txt').write_text('value\n')
    with zipfile.ZipFile(work / 'helper.zip', 'w') as archive:
        archive.writestr('zipped_helper.py', 'VALUE = "value"\n')
    return inst, work


def test_b53_file_read_apis_have_the_same_non_null_input_state(tmp_path, tool_home, monkeypatch):
    inst, work = reader_workspace(tmp_path)
    for mode in ('builtin', 'pathlib', 'io', 'os_open', 'dir_fd', 'fdopen', 'mmap', 'resources', 'opener'):
        monkeypatch.setenv('READ_METHOD', mode)
        result = profile(work, tool_home)
        assert result.returncode == 0, (mode, result.stderr)
    rows = history(tool_home)
    assert isinstance(rows[0]['state'], str)
    assert len({r['state'] for r in rows}) == 1
    assert logged_score(rows, TaskObservations.from_dict(inst.task['scoring']['observations']))['value'] == 0
    (work / 'resource_data/value.txt').write_text('changed\n')
    assert profile(work, tool_home).returncode == 0
    assert history(tool_home)[-1]['state'] not in (None, rows[0]['state'])


def test_b53_zipimport_tracks_the_archive_bytes(tmp_path, tool_home, monkeypatch):
    _, work = reader_workspace(tmp_path)
    for mode in ('zip_bytes', 'zipimport'):
        monkeypatch.setenv('READ_METHOD', mode)
        result = profile(work, tool_home)
        assert result.returncode == 0, result.stderr
    rows = history(tool_home)
    assert rows[0]['state'] is not None and rows[0]['state'] == rows[1]['state']


@pytest.mark.parametrize('mode', ['child', 'anonymous_fd'])
def test_b53_unattributable_reads_have_null_state(tmp_path, tool_home, monkeypatch, mode):
    _, work = reader_workspace(tmp_path)
    monkeypatch.setenv('READ_METHOD', mode)
    result = profile(work, tool_home)
    assert result.returncode == 0, result.stderr
    assert history(tool_home)[-1]['state'] is None


def test_m52_changelog_rejects_indented_content_but_accepts_blank_line_spaces():
    filler = fillers.changelog(Rng('changelog', 1))
    path, content = next(iter(filler.solution.items()))
    pattern = fillers.result_regex(filler, path, content)
    assert re.fullmatch(pattern, content.replace('\n\n', '\n    \n'))
    for prefix in (' ', '    ', '\t'):
        indented = '\n'.join(prefix + line if line.startswith(('##', '-')) else line for line in content.splitlines()) + '\n'
        assert not re.fullmatch(pattern, indented)


def test_m51_heredoc_probe_limit_does_not_create_a_run_log_attempt(tmp_path, tool_home):
    from types import SimpleNamespace
    from cohbench.scoring.steering import loop_part
    from cohbench.trajectory.io import Trajectory
    from cohbench.trajectory.observations import build

    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    command = "cat > NOTES.md <<'TEXT'\n./bin/profile\nTEXT\n"
    assert profile(work, tool_home, command).returncode == 0
    assert history(tool_home) == []
    event = {"seq": 1, "type": "tool_call", "class": "shell", "input": {"command": command}, "us": 1000, "us_end": 2000,
             "status": "complete", "output": {"available": True, "text": "", "clipped": "none", "last_line_whole": True, "truncated": False}}
    obs = build(Trajectory({}, [event]), None, TaskObservations.from_dict(inst.task["scoring"]["observations"]))
    assert len(obs.records) == 1  # The documented probe limitation.
    ep = SimpleNamespace(doc={"collected": {"attempts": {"status": "absent"}}, "attempt_logs": {"profile": "attempts"}}, observations=obs.records)
    result = loop_part(ep, [])
    assert result["value"] == 1 and result["scored_attempts"] == []


def test_b53_dir_fd_absence_uses_the_effective_path(tmp_path, tool_home, monkeypatch):
    _, work = reader_workspace(tmp_path)
    for mode in ('missing_absolute', 'missing_relative'):
        monkeypatch.setenv('READ_METHOD', mode)
        assert profile(work, tool_home).returncode == 0
    rows = history(tool_home)
    assert rows[0]['state'] is not None and rows[0]['state'] == rows[1]['state']
    (work / 'resource_data/missing.txt').write_text('present')
    assert profile(work, tool_home).returncode == 0
    assert history(tool_home)[-1]['state'] not in (None, rows[0]['state'])
