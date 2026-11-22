"""The private startup command and the metric server's process ancestry."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.isolate import exec_server
from cohbench.watcher.manifest import Manifest
from cohbench.watcher.metrics import IsolatedExecutor

SCAN = r'''
import json, os
from pathlib import Path
needle = bytes([114, 51, 45, 112, 114, 105, 118, 97, 116, 101, 45])
pid, checked, leaks = os.getpid(), 0, 0
while pid:
    for name in ('cmdline', 'environ'):
        try:
            data = Path('/proc/' + str(pid) + '/' + name).read_bytes()
        except (PermissionError, FileNotFoundError):
            continue
        checked += 1
        leaks += needle in data
    stat = Path('/proc/' + str(pid) + '/stat').read_text()
    pid = int(stat[stat.rindex(')') + 2:].split()[1])
print(json.dumps({'leaks': leaks, 'checked': checked}))
'''


def metric_command():
    # Like the task's service: consume the seed and re-exec before importing
    # measured code. The package can inspect this process and its ancestors.
    wrapper = 'import os,sys; os.execv(sys.executable, [sys.executable, "-c", ' + repr(SCAN) + '])'
    return [sys.executable, '-c', wrapper, 'r3-private-seed-4267']


@pytest.mark.linux_inotify
def test_private_command_file_leaves_no_marker_in_any_ancestor(tmp_path):
    root, out = tmp_path / 'root', tmp_path / 'out'
    root.mkdir()
    out.mkdir()
    command = out / 'command.json'
    command.write_text(json.dumps(metric_command()))
    args = [sys.executable, '-m', 'cohbench.watcher', 'run', '--root', str(root), '--out', str(out),
            '--metric-cmd-file', str(command), '--metric-isolation', 'none', '--work-dir', str(tmp_path / 'work')]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        end = time.monotonic() + 30
        records = []
        while time.monotonic() < end:
            metric = out / 'metrics.jsonl'
            if metric.exists():
                records = [json.loads(s) for s in metric.read_text().splitlines() if s]
                if records:
                    break
            assert p.poll() is None, p.communicate()
            time.sleep(.02)
        assert records and records[0]['ok'], records
        assert records[0]['metrics']['leaks'] == 0
        assert records[0]['metrics']['checked'] >= 4
        assert not command.exists()
    finally:
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
    assert p.returncode == 0


@pytest.mark.linux_inotify
def test_reexecuted_metric_server_has_no_inherited_command_or_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PRIVATE_INPUT', 'r3-private-seed-4267')
    to_r, to_w = os.pipe()
    fr_r, fr_w = os.pipe()
    workspace = str(tmp_path / 'workspace')
    pid = os.fork()
    if pid == 0:
        os.close(to_w)
        os.close(fr_r)
        exec_server(to_r, fr_w, workspace, str(tmp_path / 'work'), kill_strays=False)
        os._exit(1)
    os.close(to_r)
    os.close(fr_w)
    ex = IsolatedExecutor(to_w, fr_r, BlobStore(str(tmp_path / 'out')), workspace=workspace)
    try:
        rec = ex.run(Manifest.of({}), metric_command(), 10, 4096, 4096)
        assert rec['ok'], rec
        assert rec['metrics']['leaks'] == 0 and rec['metrics']['checked'] >= 4
    finally:
        ex.close()
        _, status = os.waitpid(pid, 0)
    assert status == 0
