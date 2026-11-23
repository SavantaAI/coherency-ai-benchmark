"""The documented audit contract, opener attribution and process launch paths."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from cohbench.tasks import Options, generate
from .test_round3 import history, profile, logged_score
from cohbench.trajectory.observations import TaskObservations
from .test_round6 import READERS


EXTRA_READERS = '''
import _io
import multiprocessing
import shlex
import child_helper
_previous_run = run

def run(client):
    mode = os.environ['READ_METHOD']
    if mode in ('fileio', 'fileio_opener', 'raw_fileio_opener', 'open_opener', 'io_opener'):
        directory = os.open('resource_data', os.O_RDONLY)
        try:
            opener = lambda p, f: os.open(p, f, dir_fd=directory)
            if mode == 'fileio':
                stream = io.FileIO('resource_data/value.txt', 'r')
            else:
                factory = {'fileio_opener': io.FileIO, 'raw_fileio_opener': _io.FileIO,
                           'open_opener': open, 'io_opener': io.open}[mode]
                stream = factory('value.txt', 'r', opener=opener)
            with stream:
                assert stream.read()
        finally:
            os.close(directory)
    elif mode == 'external':
        assert Path(os.environ['OUTSIDE_INPUT']).read_bytes()
    elif mode == 'anonymous_mmap':
        with mmap.mmap(-1, 1) as data:
            data[0] = 1
    elif mode == 'directory':
        fd = os.open('resource_data', os.O_RDONLY)
        os.close(fd)
    elif mode == 'runtime':
        exec(compile('value = 1', '<inline>', 'exec'), {})
        sys._getframe()
    elif mode == 'unknown':
        sys.audit('unlisted.read', 'resource_data/value.txt')
    elif mode == 'enumeration':
        assert os.listdir('resource_data')
    elif mode == 'native':
        import ctypes
        ctypes.CDLL(None)
    elif mode.startswith('launch:'):
        api = mode.split(':', 1)[1]
        args = [sys.executable, '-c', "from pathlib import Path; assert Path('resource_data/value.txt').read_bytes()"]
        if api == 'system':
            assert os.system(shlex.join(args)) == 0
        elif api == 'subprocess':
            subprocess.run(args, check=True)
        elif api in ('posix_spawn', 'posix_spawnp'):
            pid = getattr(os, api)(args[0], args, os.environ)
            assert os.waitpid(pid, 0)[1] == 0
        elif api in ('fork', 'forkpty'):
            if api == 'forkpty':
                pid, master = os.forkpty()
            else:
                pid = os.fork()
            if pid == 0:
                try:
                    child_helper.read()
                finally:
                    os._exit(0)
            assert os.waitpid(pid, 0)[1] == 0
            if api == 'forkpty':
                os.close(master)
        elif api.startswith('multiprocessing_'):
            context = multiprocessing.get_context(api.removeprefix('multiprocessing_'))
            child = context.Process(target=child_helper.read)
            child.start()
            child.join(10)
            assert child.exitcode == 0
        elif api.startswith('spawn'):
            function = getattr(os, api)
            rest = [os.environ] if api.endswith('e') else []
            if api.startswith('spawnl'):
                status = function(os.P_WAIT, args[0], *args, *rest)
            else:
                status = function(os.P_WAIT, args[0], args, *rest)
            assert status == 0
        elif api.startswith('exec'):
            function = getattr(os, api)
            rest = [os.environ] if api.endswith('e') else []
            if api.startswith('execl'):
                function(args[0], *args, *rest)
            else:
                function(args[0], args, *rest)
        elif api == 'failed_exec':
            try:
                os.execv('/no-such-profile-program', ['no-such-profile-program'])
            except FileNotFoundError:
                pass
    else:
        return _previous_run(client)
    return _saved_run(client)
'''


def workspace(tmp_path):
    inst = generate(Options('steering', 8205, 'h20'))
    work = tmp_path / 'work'
    inst.workspace.write(work)
    init = work / inst.info['pkg'] / '__init__.py'
    init.write_text(init.read_text() + READERS + EXTRA_READERS)
    (work / 'resource_data').mkdir()
    (work / 'resource_data/__init__.py').write_text('')
    (work / 'resource_data/value.txt').write_text('consumed\n')
    (work / 'value.txt').write_text('unread nominal path\n')
    (work / 'child_helper.py').write_text("from pathlib import Path\ndef read():\n    assert Path('resource_data/value.txt').read_bytes()\n")
    import zipfile
    with zipfile.ZipFile(work / 'helper.zip', 'w') as archive:
        archive.writestr('zipped_helper.py', 'VALUE = "value"\n')
    outside = tmp_path / 'outside.txt'
    outside.write_text('external input')
    return inst, work, outside


def test_b62_fileio_opener_ignores_nominal_path_and_tracks_resolved_file(tmp_path, tool_home, monkeypatch):
    inst, work, _ = workspace(tmp_path)
    for mode in ('fileio_opener', 'raw_fileio_opener', 'open_opener', 'io_opener'):
        monkeypatch.setenv('READ_METHOD', mode)
        for n in range(3):
            (work / 'value.txt').write_text(str(n))
            result = profile(work, tool_home)
            assert result.returncode == 0, result.stderr
    rows = history(tool_home)
    assert rows[0]['state'] is not None
    assert len({row['state'] for row in rows}) == 1
    assert logged_score(rows, TaskObservations.from_dict(inst.task['scoring']['observations']))['value'] == 0
    (work / 'resource_data/value.txt').write_text('actual edit')
    assert profile(work, tool_home).returncode == 0
    assert history(tool_home)[-1]['state'] not in (None, rows[0]['state'])


# Each README contract row has executable cases. The table's result column
# drives the assertion, and its row set must match this catalogue exactly.
CONTRACT_CASES = {
    'paths': ('builtin', 'pathlib', 'io', 'fileio'),
    'descriptors': ('os_open', 'dir_fd', 'fdopen', 'mmap'),
    'openers': ('opener', 'fileio_opener', 'raw_fileio_opener', 'open_opener', 'io_opener'),
    'imports': ('resources', 'zipimport', 'zip_bytes'),
    'absence': ('missing_absolute', 'missing_relative'),
    'directories': ('directory',),
    'memory': ('anonymous_mmap',),
    'runtime': ('runtime',),
    'opaque': ('anonymous_fd', 'external'),
    'processes': tuple('launch:' + api for api in (
        'system', 'subprocess', 'posix_spawn', 'posix_spawnp', 'fork', 'forkpty',
        'multiprocessing_fork', 'multiprocessing_spawn', 'multiprocessing_forkserver',
        'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe',
        'execl', 'execle', 'execlp', 'execlpe', 'execv', 'execve', 'execvp', 'execvpe', 'failed_exec')),
    'native': ('native',),
    'unlisted': ('unknown', 'enumeration'),
}


def test_audit_contract_table(tmp_path, tool_home, monkeypatch):
    readme = (Path(__file__).parents[2] / 'cohbench/tasks/README.md').read_text()
    rows = dict(re.findall(r'^\| `([a-z]+)` \| .* \| (tracked|null) \|$', readme, re.M))
    assert rows.keys() == CONTRACT_CASES.keys()
    _, work, outside = workspace(tmp_path)
    monkeypatch.setenv('OUTSIDE_INPUT', str(outside))
    states = {}
    for row, modes in CONTRACT_CASES.items():
        for mode in modes:
            monkeypatch.setenv('READ_METHOD', mode)
            before = len(history(tool_home))
            result = profile(work, tool_home)
            assert result.returncode == 0, (mode, result.stderr)
            lines = history(tool_home)
            assert len(lines) == before + 1, (mode, lines[before:])
            state = lines[-1]['state']
            assert (isinstance(state, str) if rows[row] == 'tracked' else state is None), (row, mode, state)
            states[mode] = state
    same_file = sum((CONTRACT_CASES[row] for row in ('paths', 'descriptors', 'openers')), ()) + ('resources',)
    assert len({states[mode] for mode in same_file}) == 1
    assert states['zipimport'] == states['zip_bytes']
    assert states['missing_absolute'] == states['missing_relative']


def test_b63_system_reads_changing_child_inputs_always_have_null_state(tmp_path, tool_home, monkeypatch):
    _, work, _ = workspace(tmp_path)
    monkeypatch.setenv('READ_METHOD', 'launch:system')
    for n in range(3):
        (work / 'resource_data/value.txt').write_text(str(n))
        result = profile(work, tool_home)
        assert result.returncode == 0, result.stderr
    rows = history(tool_home)
    assert len(rows) == 3 and all(row['state'] is None for row in rows)
    assert len({row['result'] for row in rows}) == 1
