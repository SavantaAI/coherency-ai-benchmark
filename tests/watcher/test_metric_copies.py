"""The metric copy, through both executors: the local one and the isolated
one with its server (run here in a thread, as the same user). Each case
checks that the copy the command sees is exactly the batch's tree."""

from __future__ import annotations

import hashlib
import os
import sys
import threading

import pytest

from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.manifest import Entry, Manifest
from cohbench.watcher.metrics import IsolatedExecutor, LocalExecutor, MetricRunner, serve
from cohbench.watcher.records import MetricsLog

PY = sys.executable
SNAP = r"""
import hashlib, json, os, stat
out = {}
for d, ds, fs in os.walk("."):
    for n in ds + fs:
        p = os.path.relpath(os.path.join(d, n), ".")
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            out[p] = ["symlink", os.readlink(p)]
        elif stat.S_ISDIR(st.st_mode):
            out[p] = ["dir", "%04o" % stat.S_IMODE(st.st_mode)]
        else:
            out[p] = ["file", "%04o" % stat.S_IMODE(st.st_mode), hashlib.sha256(open(p, "rb").read()).hexdigest()]
print(json.dumps({"tree": out}))
"""


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Rig:
    def __init__(self, tmp_path, kind):
        self.out = tmp_path / "out"
        self.out.mkdir()
        self.blobs = BlobStore(str(self.out))
        self.ino = 100
        if kind == "local":
            self.ws = "/work/project"
            ex = LocalExecutor(str(tmp_path / "work"), self.blobs, self.ws)
            self.thread = None
        else:
            to_r, to_w = os.pipe()
            fr_r, fr_w = os.pipe()
            ws = tmp_path / "ws" / "project"
            self.ws = str(ws)
            self.thread = threading.Thread(target=serve, args=(to_r, fr_w, str(ws), str(tmp_path / "srvwork")), daemon=True)
            self.thread.start()
            ex = IsolatedExecutor(to_w, fr_r, self.blobs)
        self.ex = ex
        self.runner = MetricRunner([PY, "-c", SNAP], 10, 1 << 20, 4096, MetricsLog(str(self.out)), ex)

    def F(self, data, mode=0o644, ino=None):
        if ino is None:
            self.ino += 1
            ino = self.ino
        self.blobs.put(sha(data), data)
        return Entry(type="file", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=len(data), sha256=sha(data), content="stored")

    def D(self, mode=0o755, ino=None):
        if ino is None:
            self.ino += 1
            ino = self.ino
        return Entry(type="dir", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=None, sha256=None, content="none")

    def L(self, target):
        self.ino += 1
        raw = target.encode()
        self.blobs.put(sha(raw), raw)
        return Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=self.ino, size=len(raw), sha256=sha(raw), content="stored", target=target)

    def read(self, m: Manifest, batch=0):
        rec = self.runner.run_once(batch, m, "change")
        assert rec["ok"], rec
        return rec["metrics"]["tree"]

    def close(self):
        self.ex.close()
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture(params=["local", "isolated"])
def rig(tmp_path, request):
    r = Rig(tmp_path, request.param)
    yield r
    r.close()


def expect(m: Manifest) -> dict:
    out = {}
    for p, e in m.entries.items():
        if e.type == "dir":
            out[p] = ["dir", f"{e.mode:04o}"]
        elif e.type == "file":
            out[p] = ["file", f"{e.mode:04o}", e.sha256]
        else:
            out[p] = ["symlink", e.target]
    return out


def test_directory_mode_change_keeps_its_files(rig):
    d, f = rig.D(), rig.F(b"f\n")
    m0 = Manifest.of({"d": d, "d/f": f})
    assert rig.read(m0) == expect(m0)
    m1 = Manifest.of({"d": Entry(**{**d.__dict__, "mode": 0o700}), "d/f": f})
    assert rig.read(m1, 1) == expect(m1)


def test_directory_renamed(rig):
    d, f = rig.D(), rig.F(b"f\n")
    rig.read(Manifest.of({"d": d, "d/f": f}))
    m1 = Manifest.of({"e": d, "e/f": f})
    assert rig.read(m1, 1) == expect(m1)


def test_file_changes_type(rig):
    m0 = Manifest.of({"x": rig.F(b"1"), "y": rig.F(b"2")})
    rig.read(m0)
    m1 = Manifest.of({"x": rig.L("y"), "y": rig.D(), "y/z": rig.F(b"3")})
    assert rig.read(m1, 1) == expect(m1)


def test_unchanged_file_under_a_changed_directory(rig):
    keep = rig.F(b"keep\n")
    d = rig.D()
    rig.read(Manifest.of({"d": d, "d/keep": keep, "d/other": rig.F(b"o")}))
    m1 = Manifest.of({"d": Entry(**{**d.__dict__, "mode": 0o750}), "d/keep": keep, "d/new": rig.F(b"n")})
    assert rig.read(m1, 1) == expect(m1)


def test_two_batches_read_out_of_order(rig):
    a1, a3 = rig.F(b"one"), rig.F(b"three")
    m1 = Manifest.of({"a": a1, "d": rig.D(), "d/x": rig.F(b"x")})
    m3 = Manifest.of({"a": a3, "e": rig.D(), "e/y": rig.F(b"y")})
    assert rig.read(m3, 3) == expect(m3)
    assert rig.read(m1, 1) == expect(m1)
    assert rig.read(m3, 3) == expect(m3)


def test_blob_with_the_old_marker_content_is_present(rig):
    m = Manifest.of({"odd": rig.F(b"\0MISSING")})
    assert rig.read(m) == expect(m)


def test_a_corrupt_blob_fails_the_reading(rig):
    e = rig.F(b"good")
    with open(rig.blobs.path(e.sha256), "wb") as f:
        f.write(b"evil")
    rec = rig.runner.run_once(0, Manifest.of({"a": e}), "change")
    assert not rec["ok"] and rec["error"].startswith("copy verification failed")


def test_ignored_names_never_reach_the_copy(tmp_path, rig):
    from cohbench.watcher.config import Config
    from cohbench.watcher.core import Core
    from cohbench.watcher.records import EventLog
    from tests.watcher.helpers import FakeBackend

    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("instructions")
    (root / "sub" / "AGENTS.md").write_text("more")
    (root / "x.txt").write_text("x")
    elog = tmp_path / "elog"
    elog.mkdir()
    core = Core(Config(root=str(root), out=str(elog)), FakeBackend(), EventLog(str(elog)), rig.blobs)
    core.baseline()
    tree = rig.read(core.manifest)
    assert set(tree) == {"sub", "x.txt"}


def test_link_through_a_symlinked_directory_then_dotdot(rig):
    # l -> a/b; x -> l/../../file. The kernel resolves l first, so x reaches
    # the top-level file; collapsing `..` first would wrongly leave the tree.
    m = Manifest.of({"a": rig.D(), "a/b": rig.D(), "file": rig.F(b"top\n"), "l": rig.L("a/b"), "x": rig.L("l/../../file")})
    tree = rig.read(m)
    assert tree["x"] == ["symlink", "l/../../file"]
    rec = rig.runner.run_once(1, m, "change")
    assert rec["ok"] and rec["links_outside"] == {}


def test_absolute_link_reached_through_another_link_opens_in_the_copy(rig):
    # l -> <workspace with a /./ in it>/a, x -> l/b: the same resolver
    # handles l's target on its own and when x reaches it, so x is inside
    # and opening x in the copy gives a/b.
    head, tail = rig.ws.rsplit("/", 1)
    m = Manifest.of({"a": rig.D(), "a/b": rig.F(b"inner\n"), "l": rig.L(f"{head}/./{tail}/a"), "x": rig.L("l/b")})
    rig.runner.cmd = [PY, "-c", "import json; print(json.dumps({'x': open('x').read(), 'abs': open('l/b').read()}))"]
    rec = rig.runner.run_once(0, m, "change")
    assert rec["ok"], rec
    assert rec["metrics"] == {"x": "inner\n", "abs": "inner\n"} and rec["links_outside"] == {}


def test_classify_link_cases():
    from cohbench.watcher.metrics import classify_link

    E = lambda t: Entry(type="symlink", mode=0o777, uid=0, gid=0, dev=1, ino=1, size=1, sha256="x", content="stored", target=t)
    D = Entry(type="dir", mode=0o755, uid=0, gid=0, dev=1, ino=2, size=None, sha256=None, content="none")
    m = Manifest.of({"a": D, "a/b": D, "l": E("a/b"), "up": E(".."), "abs": E("/work/project/a")})
    ws = "/work/project"
    assert classify_link("x", "l/../../f", ws, m) == "f"
    assert classify_link("x", "l/../../../f", ws, m) is None
    assert classify_link("x", "up/f", ws, m) is None
    assert classify_link("x", "abs/../f", ws, m) == "f"
    assert classify_link("a/x", "../l", ws, m) == "a/b"
    assert classify_link("x", "/etc/passwd", ws, m) is None
    m2 = Manifest.of({"a": D, "l": E("/work/./project/a"), "l2": E("/work/x/../project/a")})
    assert classify_link("l", "/work/./project/a", ws, m2) == "a"
    assert classify_link("x", "l/b", ws, m2) == "a/b"
    assert classify_link("x", "l2/b", ws, m2) == "a/b"
    assert classify_link("x", "/work/project/../project/a", ws, m2) == "a"
    assert classify_link("x", "/work/projectx/a", ws, m2) is None
    assert classify_link("x", "/../work/project", ws, m2) == ""


def test_each_invocation_has_fresh_writable_storage(rig, tmp_path):
    import json
    from pathlib import Path

    outside = tmp_path / 'persistent-cache'
    code = r'''
import json, os
from pathlib import Path
paths = [Path(os.environ[k]) / 'totals' for k in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME')]
paths += [Path.cwd() / 'totals', Path('/tmp/profile-totals'), Path('/var/tmp/profile-totals'), Path('/dev/shm/profile-totals'), Path(OUTSIDE)]
seen, written = 0, []
for p in set(paths):
    if p.exists():
        seen += 1
    try:
        p.write_text('cached totals')
        written.append(str(p))
    except (PermissionError, OSError):
        pass
print(json.dumps({'calls': 0 if seen else 6, 'written': written, 'home': os.environ['HOME']}))
'''.replace('OUTSIDE', repr(str(outside)))
    rig.runner.cmd = [PY, '-c', code]
    homes = []
    for batch, text in enumerate((b'first', b'comment changed')):
        rec = rig.runner.run_once(batch, Manifest.of({'package.py': rig.F(text)}), 'change')
        assert rec['ok'], rec
        assert rec['metrics']['calls'] == 6, rec
        assert rec['metrics']['written']
        home = rec['metrics']['home']
        homes.append(home)
        assert not Path(home).exists()
        assert not outside.exists()
    assert homes[0] != homes[1]
