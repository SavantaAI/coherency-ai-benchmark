"""Second-review fixes, on any host: validation of live scans (B2), the
component-safe sweep and git reader (B3), write hints by identity and
unknown content (B5), root permission loss and recovery (B6), shutdown
reporting (B8), one read per inode (footprint), D-026 ignores."""

from __future__ import annotations

import hashlib
import os

import pytest

from cohbench.watcher import diff, inotify as I, replay, scan as scanmod
from cohbench.watcher import gitstate
from cohbench.watcher.config import Config
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.watcher import Watcher
from tests.watcher.helpers import FakeBackend, batch_records, kinds
from tests.watcher.test_core_host import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_b2_write_during_scan_is_not_a_mixed_state(rig, monkeypatch):
    rig.write("a", b"A1")
    rig.write("b", b"B1")
    rig.start()
    rig.write("a", b"A1x")
    rig.write("b", b"B1x")
    real = scanmod.Scanner._read
    fired = []

    def racing(self, st, name, dfd, res, rel, *a):
        e = real(self, st, name, dfd, res, rel, *a)
        if rel == "a" and not fired:
            fired.append(1)
            rig.write("a", b"A2-new")  # after a was read ...
            rig.write("b", b"B2-new")  # ... and before b is
        return e

    monkeypatch.setattr(scanmod.Scanner, "_read", racing)
    rig.dirty("a", "b", modify=True)
    out = rig.commit()
    st = replay.state_at(str(rig.out))
    assert st.entries["a"].sha256 == sha(b"A2-new") and st.entries["b"].sha256 == sha(b"B2-new")
    b = out[-1]
    assert b["settled"] and b["validation_rounds"] >= 2


def test_b2_tree_that_never_holds_still_is_flagged_unsettled(rig, monkeypatch):
    rig.write("a", b"0")
    rig.start()
    real = scanmod.Scanner._read
    n = [0]

    def churn(self, st, name, dfd, res, rel, *a):
        e = real(self, st, name, dfd, res, rel, *a)
        n[0] += 1
        rig.write("a", str(n[0]).encode() * (n[0] + 1))
        return e

    monkeypatch.setattr(scanmod.Scanner, "_read", churn)
    rig.write("a", b"start")
    rig.dirty("a", modify=True)
    rig.t += 1
    out = rig.core.tick(force=True)
    b = out[-1]
    assert b["kind"] == "batch" and b["settled"] is False and "a" in b["unsettled"]


def test_b3_sweep_hands_no_slashed_path_to_stat(rig, monkeypatch):
    rig.write("d/sub/deeper/f", b"x")
    rig.write("top", b"t")
    rig.start()
    seen = []
    real = os.stat

    def spy(path, *a, **k):
        if k.get("dir_fd") is not None:
            seen.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(scanmod.os, "stat", spy)
    rig.core.tick(force=True)  # a forced scan runs the sweep over every known path
    assert seen and not [p for p in seen if "/" in str(p)]


def test_b3_middle_component_swapped_for_a_symlink(rig):
    rig.write("d/sub/f", b"inside")
    rig.start()
    outside = rig.outside / "evil"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "f").write_bytes(b"OUTSIDE BYTES")
    os.rename(rig.p("d"), rig.outside / "d.moved")
    os.symlink(str(outside), rig.p("d"))
    rig.commit()  # no hint at all: only the sweep sees it
    st = replay.state_at(str(rig.out))
    assert st.entries["d"].type == "symlink" and "d/sub/f" not in st.entries
    assert not os.path.exists(rig.core.blobs.path(sha(b"OUTSIDE BYTES")))


def test_b3_git_reader_refuses_a_refs_directory_swapped_mid_scan(rig, monkeypatch):
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    evil = rig.outside / "evilrefs"
    evil.mkdir()
    (evil / "main").write_text("e" * 40 + "\n")

    def swap(rel):
        if rel == ".git/refs/heads" and not (rig.p(".git/refs/heads")).is_symlink():
            os.rename(rig.p(".git/refs/heads"), rig.outside / "heads.saved")
            os.symlink(str(evil), rig.p(".git/refs/heads"))

    monkeypatch.setattr(gitstate, "_between_discovery_and_open", swap)
    rig.start()
    refs = replay.state_at(str(rig.out)).git["refs"]
    assert "e" * 40 not in refs.values()


def test_b5_write_after_renames_attaches_to_the_written_file(rig):
    rig.write("a", b"A")
    rig.start()
    rig.rename("a", "b")
    rig.rename("b", "a")
    rig.write("b", b"new b")
    rig.dirty("b", modify=True)
    assert kinds(rig.commit()) == [("created", "b")]


def test_b5_unknown_content_change_is_a_difference(rig):
    rig.write("wo", b"A")
    os.chmod(rig.p("wo"), 0o200)  # write-only: content cannot be read
    rig.start()
    with open(rig.p("wo"), "wb") as f:
        f.write(b"B")
    rig.dirty("wo", modify=True)
    out = rig.commit()
    assert kinds(out) == [("modified", "wo")] and out[0]["content_unknown"] is True and out[0]["epoch"] == 1
    ds = diff(str(rig.out), 0, 1)
    assert [d.status for d in ds] == ["modified"] and "unknown" in ds[0].note


def test_b6_root_permission_loss_and_recovery(rig):
    rig.write("f", b"x")
    rig.write("d/g", b"y")
    rig.start()
    root_wd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == "" and info[2] == "tree")
    os.chmod(rig.root, 0o000)
    try:
        rig.core.feed([RawEvent(root_wd, I.IN_ATTRIB | I.IN_ISDIR, 0, "")], now=rig.t, wall=0.0)
        rig.t += 1
        out = rig.core.tick(force=True)
        assert replay.state_at(str(rig.out)).entries == {} and out[-1]["root_accessible"] is False
    finally:
        os.chmod(rig.root, 0o755)
    rig.core.feed([RawEvent(root_wd, I.IN_ATTRIB | I.IN_ISDIR, 0, "")], now=rig.t, wall=0.0)
    out = rig.commit()
    assert set(replay.state_at(str(rig.out)).entries) == {"f", "d", "d/g"} and out[-1]["root_accessible"] is True


def test_one_content_read_per_inode_per_scan(rig):
    rig.write("a", b"1")
    os.link(rig.p("a"), rig.p("b"))
    rig.start()
    with open(rig.p("a"), "r+b") as f:
        f.write(b"2")
    rig.dirty("a", "b", modify=True)
    out = rig.commit()
    assert out[-1]["reads"] == 1 and sorted(kinds(out)) == [("modified", "a"), ("modified", "b")]


def test_d026_instruction_files_are_never_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.write("CLAUDE.md", b"secret instructions")
    rig.write("sub/AGENTS.md", b"more")
    rig.write("keep.txt", b"k")
    rig.start()
    st = replay.state_at(str(rig.out))
    assert set(st.entries) == {"keep.txt", "sub"}
    # A change to an ignored file alone: no batch.
    rig.write("CLAUDE.md", b"edited")
    rig.dirty("CLAUDE.md", modify=True)
    rig.t += 1
    assert rig.core.tick(force=True) == [] and len(batch_records(str(rig.out))) == 1
    # Moves onto and from an ignored name are a deletion and a creation.
    rig.rename("keep.txt", "CLAUDE.md")
    out = rig.commit_skip()
    assert kinds(out) == [("deleted", "keep.txt")]
    rig.rename("sub/AGENTS.md", "notes.md")
    out = rig.commit_skip()
    assert kinds(out) == [("created", "notes.md")]
    for d, _, fs in os.walk(rig.out / "blobs"):
        for f in fs:
            assert open(os.path.join(d, f), "rb").read() not in (b"secret instructions", b"edited")


def test_d026_ignored_git_refs_make_no_batch(tmp_path):
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "refs" / "t3" / "cp").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    (g / "refs" / "t3" / "cp" / "0").write_text("b" * 40 + "\n")
    (g / "packed-refs").write_text("c" * 40 + " refs/t3/packed\n" + "d" * 40 + " refs/tags/v1\n")
    rig.start()
    assert replay.state_at(str(rig.out)).git["refs"] == {"refs/heads/main": "a" * 40, "refs/tags/v1": "d" * 40}
    (g / "refs" / "t3" / "cp" / "1").write_text("e" * 40 + "\n")
    rig.core.pending.git(rig.t, 0.0)
    rig.t += 1
    assert rig.core.tick(force=True) == [] and len(batch_records(str(rig.out))) == 1


class ScriptedBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.queue = []

    def read(self):
        q, self.queue = self.queue, []
        return q


def test_b8_shutdown_reports_late_overflow_and_a_fatal_root_event(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    be = ScriptedBackend()
    w = Watcher(Config(root=str(root), out=str(tmp_path / "out")), backend=be)
    w.start()
    root_wd = next(wd for wd, info in w.core.wd_info.items() if info[0] == "" and info[2] == "tree")
    real_count = w._count_late

    def late():
        be.queue = [RawEvent(-1, I.IN_Q_OVERFLOW, 0, ""), RawEvent(root_wd, I.IN_DELETE_SELF, 0, "")]
        return real_count()

    w._count_late = late
    w.stop()
    w.shutdown()
    import json

    hb = json.loads((tmp_path / "out" / "heartbeat.json").read_text())
    assert hb["late_overflow"] is True and hb["late_events"] == 2 and hb["status"] == "failed" and "root" in hb["error"]
