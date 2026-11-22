"""Third-review fixes, on any host: no cached read across validation rounds
(finding 2), hints given back with a deadline (finding 3 and M1), ignored
activity invisible to records and to batching (D-026, M5), write hints per
file lifetime and unknown-content versions per inode (M2, M3)."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher import scan as scanmod
from cohbench.watcher.inotify import RawEvent
from tests.watcher.helpers import kinds, records
from tests.watcher.test_core_host import Rig


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def root_wd(core):
    return next(wd for wd, info in core.wd_info.items() if info[0] == "" and info[2] == "tree")


def test_same_tick_overwrite_during_a_scan_is_not_lost(rig, monkeypatch):
    rig.write("a", b"AAA")
    rig.start()
    # A coarse clock: timestamps never move, so only the event can tell.
    monkeypatch.setattr(scanmod, "_statsig", lambda st: (st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_size))
    monkeypatch.setattr(scanmod, "_rsig", lambda st: (st.st_size, st.st_mode, st.st_uid, st.st_gid))
    rig.core.scanner.cache.clear()
    real = scanmod.Scanner._read
    fired = []

    def overwrite(self, st, name, dfd, res, rel, *a):
        e = real(self, st, name, dfd, res, rel, *a)
        if rel == "a" and not fired:
            fired.append(1)
            with open(rig.p("a"), "r+b") as f:
                f.write(b"CCC")  # same length, same tick
            rig.core.pending.touch("a", rig.t, 0.0, modify=True)  # what inotify reports
        return e

    monkeypatch.setattr(scanmod.Scanner, "_read", overwrite)
    rig.write("a", b"BBB")
    rig.dirty("a", modify=True)
    out = rig.commit()
    st = replay.state_at(str(rig.out)).entries["a"]
    assert st.sha256 == sha(b"CCC"), (st, out[-1])
    b = out[-1]
    assert b["settled"] and b["reads"] >= 2, {k: b[k] for k in ("settled", "reads", "validation_rounds", "unsettled")}


class Scripted:
    """A backend whose read() returns queued events (a FakeBackend otherwise)."""

    def __init__(self, inner):
        self.inner = inner
        self.queue = []

    def add_watch_fd(self, fd):
        return self.inner.add_watch_fd(fd)

    def rm_watch(self, wd):
        return self.inner.rm_watch(wd)

    def read(self):
        q, self.queue = self.queue, []
        return q


def test_root_recovery_arriving_during_validation_is_rescheduled(rig, monkeypatch):
    rig.write("f", b"x")
    rig.start()
    be = Scripted(rig.core.backend)
    rig.core.backend = be
    rwd = root_wd(rig.core)
    os.chmod(rig.root, 0o000)
    try:
        rig.core.pending.mark_root(rig.t, 0.0)
        rig.t += 1
        rig.core.tick(force=True)
        assert replay.state_at(str(rig.out)).entries == {}
    finally:
        os.chmod(rig.root, 0o755)
    # The recovery's event arrives while a mark's batch is being validated.
    rig.write("g", b"y")
    rig.dirty("g")
    be.queue = [RawEvent(rwd, I.IN_ATTRIB | I.IN_ISDIR, 0, "")]
    rig.t += 1
    rig.core.tick(force=True, reason="mark")
    assert rig.core.pending.root_dirty and rig.core.pending.deadline() is not None
    rig.t += 100
    assert rig.core.due()
    rig.core.tick()
    assert {"f", "g"} <= set(replay.state_at(str(rig.out)).entries)


def test_outstanding_hints_without_a_deadline_are_an_error(rig):
    rig.start()
    rig.core.pending.root_dirty = True  # set behind the scheduler's back
    with pytest.raises(RuntimeError):
        rig.core.pending.deadline()


def test_m2_write_then_delete_does_not_follow_another_file(rig):
    rig.write("b", b"bee")
    rig.start()
    rig.write("a", b"x")
    rig.dirty("a", modify=True)
    os.unlink(rig.p("a"))
    rig.core.pending.touch("a", rig.t, 0.0, deleted=True)
    rig.rename("b", "a")
    rig.rename("a", "b")
    out = rig.commit()
    assert kinds(out) == []


def test_m2_with_an_unreadable_file(rig):
    rig.write("b", b"bee")
    os.chmod(rig.p("b"), 0o200)
    rig.start()
    rig.write("a", b"x")
    rig.dirty("a", modify=True)
    os.unlink(rig.p("a"))
    rig.core.pending.touch("a", rig.t, 0.0, deleted=True)
    rig.rename("b", "a")
    rig.rename("a", "b")
    assert kinds(rig.commit()) == []


def test_m3_two_names_of_one_write_only_inode(rig):
    rig.write("a", b"A")
    os.link(rig.p("a"), rig.p("b"))
    os.chmod(rig.p("a"), 0o200)
    rig.start()
    fd = os.open(rig.p("a"), os.O_WRONLY)
    os.write(fd, b"B")
    os.close(fd)
    rig.dirty("a", modify=True)
    out = rig.commit()
    assert sorted(kinds(out)) == [("modified", "a"), ("modified", "b")]
    st = replay.state_at(str(rig.out))
    assert st.entries["a"].epoch == st.entries["b"].epoch == 1


def test_m4_rename_of_an_unreadable_file_keeps_its_epoch(rig):
    rig.write("a", b"A")
    os.chmod(rig.p("a"), 0o200)
    rig.start()
    fd = os.open(rig.p("a"), os.O_WRONLY)
    os.write(fd, b"B")
    os.close(fd)
    rig.dirty("a", modify=True)
    rig.commit()
    rig.rename("a", "c")
    out = rig.commit()
    assert kinds(out) == [("moved", "a", "c")]
    assert replay.state_at(str(rig.out)).entries["c"].epoch == 1


def every_output_text(out):
    for d, _, fs in os.walk(out):
        for f in fs:
            p = os.path.join(d, f)
            if "/blobs/" in p:
                continue
            yield p, open(p, "rb").read()


def test_d026_names_never_appear_in_any_output(tmp_path):
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    rig.write("x", b"1")
    rig.start()
    rwd = root_wd(rig.core)
    feed = lambda *evs: rig.core.feed(list(evs), now=rig.t, wall=1.0)
    # The instruction file written in place, next to a tracked change.
    rig.write("CLAUDE.md", b"notes")
    rig.write("x", b"2")
    feed(RawEvent(rwd, I.IN_CREATE, 0, "CLAUDE.md"), RawEvent(rwd, I.IN_MODIFY, 0, "CLAUDE.md"), RawEvent(rwd, I.IN_MODIFY, 0, "x"))
    rig.commit_skip()
    # Through a temp file named after it, renamed onto it.
    rig.write(".CLAUDE.md.tmp", b"notes 2")
    os.replace(rig.p(".CLAUDE.md.tmp"), rig.p("CLAUDE.md"))
    rig.write("x", b"3")
    feed(RawEvent(rwd, I.IN_CREATE, 0, ".CLAUDE.md.tmp"), RawEvent(rwd, I.IN_MOVED_FROM, 7, ".CLAUDE.md.tmp"), RawEvent(rwd, I.IN_MOVED_TO, 7, "CLAUDE.md"), RawEvent(rwd, I.IN_MODIFY, 0, "x"))
    rig.commit_skip()
    # Renamed to a tracked name and back, and to the other ignored name.
    os.replace(rig.p("CLAUDE.md"), rig.p("notes.md"))
    feed(RawEvent(rwd, I.IN_MOVED_FROM, 8, "CLAUDE.md"), RawEvent(rwd, I.IN_MOVED_TO, 8, "notes.md"))
    rig.commit_skip()
    os.replace(rig.p("notes.md"), rig.p("CLAUDE.md"))
    feed(RawEvent(rwd, I.IN_MOVED_FROM, 9, "notes.md"), RawEvent(rwd, I.IN_MOVED_TO, 9, "CLAUDE.md"))
    rig.commit_skip()
    os.replace(rig.p("CLAUDE.md"), rig.p("AGENTS.md"))
    rig.write("x", b"4")
    feed(RawEvent(rwd, I.IN_MOVED_FROM, 10, "CLAUDE.md"), RawEvent(rwd, I.IN_MOVED_TO, 10, "AGENTS.md"), RawEvent(rwd, I.IN_MODIFY, 0, "x"))
    rig.commit_skip()
    # An ignored ref, and deletion.
    (g / "refs" / "t3").mkdir()
    (g / "refs" / "t3" / "cp").write_text("a" * 40 + "\n")
    refs_wd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == ".git/refs")
    feed(RawEvent(refs_wd, I.IN_CREATE | I.IN_ISDIR, 0, "t3"))
    os.unlink(rig.p("AGENTS.md"))
    feed(RawEvent(rwd, I.IN_DELETE, 0, "AGENTS.md"))
    rig.commit_skip()
    from cohbench.watcher.watcher import public_config

    (rig.out / "config.json").write_text(json.dumps(public_config(rig.cfg)))
    for p, data in every_output_text(str(rig.out)):
        for bad in (b"CLAUDE.md", b"AGENTS.md", b"refs/t3"):
            assert bad not in data, f"{bad!r} in {p}"
    assert kinds(records(str(rig.out))) == [("modified", "x"), ("modified", "x"), ("created", "notes.md"), ("deleted", "notes.md"), ("modified", "x")]


def schedule_batches(tmp_path, with_ignored: bool):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    rig.write("x", b"0")
    rig.write("CLAUDE.md", b"c")
    rig.start()
    rwd = root_wd(rig.core)
    refs_wd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == ".git/refs")
    t0 = rig.t
    task = {0.00: 1, 0.08: 2, 0.30: 3, 0.36: 4}
    ignored = {0.04: "ref", 0.12: "write", 0.20: "rename", 0.33: "write"}
    out = []
    n = 0
    for step in range(0, 120):
        now = t0 + step / 100
        rig.t = now
        k = round(step / 100, 2)
        if k in task:
            rig.write("x", str(task[k]).encode())
            rig.core.feed([RawEvent(rwd, I.IN_MODIFY, 0, "x"), RawEvent(rwd, I.IN_CLOSE_WRITE, 0, "x")], now=now, wall=now)
        if with_ignored and k in ignored:
            what = ignored[k]
            if what == "ref":
                rig.core.feed([RawEvent(refs_wd, I.IN_CREATE | I.IN_ISDIR, 0, "t3")], now=now, wall=now)
            elif what == "write":
                rig.core.feed([RawEvent(rwd, I.IN_MODIFY, 0, "CLAUDE.md"), RawEvent(rwd, I.IN_CLOSE_WRITE, 0, "CLAUDE.md")], now=now, wall=now)
            else:
                n += 1
                rig.core.feed([RawEvent(rwd, I.IN_MOVED_FROM, 100 + n, "CLAUDE.md"), RawEvent(rwd, I.IN_MOVED_TO, 100 + n, "AGENTS.md")], now=now, wall=now)
        got = rig.core.tick(now=now)
        if got:
            b = got[-1]
            out.append((round(now - t0, 2), kinds(got), b["events"], b["first_event"], b["last_event"]))
    return out


def test_m5_ignored_activity_does_not_change_task_batches(tmp_path):
    plain = schedule_batches(tmp_path / "a", with_ignored=False)
    noisy = schedule_batches(tmp_path / "b", with_ignored=True)
    assert plain == noisy and len(plain) == 2
