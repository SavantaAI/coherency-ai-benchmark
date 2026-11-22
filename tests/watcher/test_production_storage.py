"""Dedicated-user cleanup under Docker's built-in seccomp profile."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.isolate import drop, exec_server
from cohbench.watcher.manifest import Manifest
from cohbench.watcher.metrics import IsolatedExecutor

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.getuid() != 0, reason='dedicated-user container phase of run_linux.sh')

PROBE = r'''
import json, os, stat
from pathlib import Path
from cohbench.watcher.storage import writable_roots
roots = writable_roots()
seen, written, shared = [], [], []
for directory, children, files in os.walk('/', followlinks=False):
    children[:] = [n for n in children if os.path.join(directory,n) not in ('/proc','/sys','/src')]
    for name in files:
        existing = os.path.join(directory, name)
        try:
            info = os.lstat(existing)
            if stat.S_ISREG(info.st_mode) and info.st_uid != os.getuid() and os.access(existing, os.W_OK):
                shared.append(existing)
        except OSError:
            pass
    p = os.path.join(directory, 'storage_probe_cache')
    if os.path.lexists(p):
        seen.append(p)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        continue
    try:
        os.write(fd, b'cached totals')
    except OSError:
        pass
    os.close(fd)
    written.append(p)
print(json.dumps({'seen': seen, 'written': written, 'roots': roots, 'home': os.environ['HOME'], 'calls': 0 if seen else 6, 'shared_writable_files': shared}))
'''


def test_dedicated_user_cleans_every_writable_location():
    root = Path(tempfile.mkdtemp(prefix='metric-storage-'))
    root.chmod(0o777)
    to_r, to_w = os.pipe()
    fr_r, fr_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(to_w)
        os.close(fr_r)
        drop(1500, 1500)
        exec_server(to_r, fr_w, str(root / 'workspace'), str(root / 'work'), kill_strays=True)
        os._exit(1)
    os.close(to_r)
    os.close(fr_w)
    ex = IsolatedExecutor(to_w, fr_r, BlobStore(str(root / 'out')))
    try:
        homes = []
        for _ in range(2):
            # The import path is an ordinary read-only test image path.
            code = 'import sys; sys.path.insert(0,"/src");\n' + PROBE
            rec = ex.run(Manifest.of({}), [sys.executable, '-c', code], 30, 1 << 20, 4096)
            assert rec['ok'], json.dumps(rec)
            result = rec['metrics']
            assert result['calls'] == 6 and result['seen'] == [], result
            assert {'/tmp', '/dev/shm', '/dev/mqueue'} <= set(result['roots'])
            assert result['written']
            assert result['shared_writable_files'] == []
            for path in result['written']:
                assert any(path.startswith(r.rstrip('/') + '/') for r in result['roots']), path
                assert not Path(path).exists(), path
            homes.append(result['home'])
            assert not Path(result['home']).exists()
        assert homes[0] != homes[1]
    finally:
        ex.close()
        os.waitpid(pid, 0)
        shutil.rmtree(root, ignore_errors=True)
