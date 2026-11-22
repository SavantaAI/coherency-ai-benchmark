"""Fourth-review fixes, on any host."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher import scan as scanmod
from cohbench.watcher.ignore import IgnoreRules
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.manifest import ReplayError
from cohbench.watcher.pending import Pending
from tests.watcher.helpers import kinds, records
from tests.watcher.test_core_host import Rig
from tests.watcher.test_round3_host import root_wd


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---- S1 -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["CLAUDE.md", "d/AGENTS.md", ".CLAUDE.md.tmp", ".CLAUDE.md.tmp.123", "CLAUDE.md.tmp-x1", ".CLAUDE.md.a1b2c3", "CLAUDE.md~", ".CLAUDE.md.swp", ".#CLAUDE.md", "#AGENTS.md#", "CLAUDE.md/inside"])
def test_reserved_names_and_their_temporary_forms_are_ignored(path):
    assert IgnoreRules(names=["CLAUDE.md", "AGENTS.md"]).ignored(path)


@pytest.mark.parametrize("path", ["docs/AGENTS.md.example", "notCLAUDE.md", "CLAUDE.mdx", "my-AGENTS.md", "CLAUDE.md.bak.old"])
def test_other_names_that_contain_them_are_kept(path):
    assert not IgnoreRules(names=["CLAUDE.md", "AGENTS.md"]).ignored(path)


def test_example_files_are_recorded(rig):
    rig.write("docs/AGENTS.md.example", b"e")
    rig.write("notCLAUDE.md", b"n")
    rig.write("CLAUDE.md", b"c")
    rig.start()
    assert set(replay.state_at(str(rig.out)).entries) == {"docs", "docs/AGENTS.md.example", "notCLAUDE.md"}


# ---- S3 -------------------------------------------------------------------


def test_write_times_ignore_a_later_close(rig):
    rig.write("x", b"0")
    rig.start()
    rwd = root_wd(rig.core)
    rig.write("x", b"1")
    rig.core.feed([RawEvent(rwd, I.IN_MODIFY, 0, "x")], now=rig.t, wall=1000.0)
    rig.core.feed([RawEvent(rwd, I.IN_CLOSE_WRITE, 0, "x")], now=rig.t + 1, wall=1001.0)
    rig.t += 2
    b = rig.core.tick()[-1]
    assert b["first_write"] == b["last_write"] == "1970-01-01T00:16:40.000000Z"
    assert b["last_event"] == "1970-01-01T00:16:41.000000Z"


def test_sweep_only_batch_has_no_write_times(rig):
    rig.write("x", b"0")
    rig.start()
    rig.write("x", b"changed")
    rig.t += 1
    b = rig.core.tick(force=True)[-1]
    assert kinds(records(str(rig.out), batch=b["batch"])) == [("modified", "x")]
    assert b["first_write"] is None and b["last_write"] is None


def test_retry_touches_do_not_move_write_times():
    p = Pending(0.1, 2.0)
    p.touch("a", 1.0, 1.0)  # a hint the watcher made itself
    assert p.first_write is None
    p.touch("a", 1.1, 1.1, modify=True, change=True)
    assert p.first_write == p.last_write == 1.1


# ---- B2 -------------------------------------------------------------------


def test_a_deleted_open_file_no_longer_holds_the_batch():
    p = Pending(0.1, 2.0)
    p.touch("tmp", 10.0, 10.0, modify=True, change=True)  # open, never closed
    p.touch("tmp", 10.01, 10.01, deleted=True, change=True)
    p.touch("x", 10.02, 10.02, modify=True, change=True)
    p.touch("x", 10.02, 10.02, close_write=True)
    assert abs(p.deadline() - 10.12) < 1e-9


def test_open_writer_below_a_renamed_directory():
    p = Pending(0.1, 2.0)
    p.touch("d/f", 1.0, 1.0, modify=True, change=True)
    p.moved_from("d", 1.01, 1.01)
    p.rename("d", "e", 1.01, 1.01)
    assert set(p.open_writes) == {"e/f"}
    p.touch("e/f", 1.02, 1.02, close_write=True)
    assert p.open_writes == {}
    p.touch("g/h", 2.0, 2.0, modify=True, change=True)
    p.touch("g", 2.01, 2.01, deleted=True, change=True)  # an ancestor removed
    assert p.open_writes == {}


# ---- S2 -------------------------------------------------------------------


# ---- V1 and V2 ------------------------------------------------------------


def test_packed_refs_of_ignored_namespaces_do_not_touch_the_clock(rig):
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "packed-refs").write_text("a" * 40 + " refs/heads/main\n")
    rig.start()
    gwd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == ".git")
    # A repack: a lock file, then a new inode renamed over packed-refs.
    (g / "packed-refs.lock").write_text("a" * 40 + " refs/heads/main\n" + "b" * 40 + " refs/t3/x\n")
    rig.core.feed([RawEvent(gwd, I.IN_CREATE, 0, "packed-refs.lock"), RawEvent(gwd, I.IN_CLOSE_WRITE, 0, "packed-refs.lock")], now=rig.t, wall=1.0)
    os.rename(g / "packed-refs.lock", g / "packed-refs")
    rig.core.feed([RawEvent(gwd, I.IN_MOVED_FROM, 7, "packed-refs.lock"), RawEvent(gwd, I.IN_MOVED_TO, 7, "packed-refs")], now=rig.t, wall=1.0)
    assert not rig.core.pending
    rig.t += 1
    assert not [r for r in rig.core.tick(force=True) if r["kind"] != "batch"]  # no git or file record
    (g / "packed-refs").write_text("c" * 40 + " refs/heads/main\n" + "b" * 40 + " refs/t3/x\n")
    rig.core.feed([RawEvent(gwd, I.IN_MOVED_TO, 0, "packed-refs")], now=rig.t, wall=1.0)
    assert rig.core.pending.git_dirty


def test_ignored_files_under_a_tracked_directory_do_not_unsettle(rig, monkeypatch):
    rig.write("d/keep", b"k")
    rig.write("x", b"0")
    rig.start()
    real = scanmod.Scanner._read

    def noisy(self, st, name, dfd, res, rel, *a):
        e = real(self, st, name, dfd, res, rel, *a)
        if rel == "x":
            for i in range(3):
                (rig.p("d") / f".CLAUDE.md.tmp.{i}").write_text("t")  # ignored, in tracked d
        return e

    monkeypatch.setattr(scanmod.Scanner, "_read", noisy)
    rig.write("x", b"1")
    rig.dirty("x", modify=True)
    b = rig.commit_skip()[-1]
    assert b["settled"] and b["validation_rounds"] == 1 and b["unsettled"] == []


def test_same_batches_with_ignored_packed_refs_and_ignored_files_under_a_tracked_dir(tmp_path):
    def run(base, noisy):
        base.mkdir(parents=True)
        rig = Rig(base)
        g = rig.p(".git")
        (g / "refs" / "heads").mkdir(parents=True)
        (g / "HEAD").write_text("ref: refs/heads/main\n")
        (g / "packed-refs").write_text("a" * 40 + " refs/heads/main\n")
        rig.write("d/x", b"0")
        rig.start()
        rwd = root_wd(rig.core)
        dwd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == "d")
        gwd = next(wd for wd, info in rig.core.wd_info.items() if info[0] == ".git")
        t0, out, n = rig.t, [], 0
        task = {0.0: 1, 0.08: 2, 0.3: 3, 0.36: 4}
        noise = {0.04: "pack", 0.12: "file", 0.2: "pack", 0.33: "file"}
        for step in range(120):
            now = rig.t = t0 + step / 100
            k = round(step / 100, 2)
            if k in task:
                rig.write("d/x", str(task[k]).encode())
                rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "x"), RawEvent(dwd, I.IN_CLOSE_WRITE, 0, "x")], now=now, wall=now)
            if noisy and k in noise:
                n += 1
                if noise[k] == "pack":
                    (g / "packed-refs").write_text("a" * 40 + " refs/heads/main\n" + f"{n:040d} refs/t3/cp/{n}\n")
                    rig.core.feed([RawEvent(gwd, I.IN_MOVED_TO, 0, "packed-refs")], now=now, wall=now)
                else:
                    (rig.p("d") / f".AGENTS.md.tmp.{n}").write_text("t")
                    rig.core.feed([RawEvent(dwd, I.IN_CREATE, 0, f".AGENTS.md.tmp.{n}"), RawEvent(dwd, I.IN_CLOSE_WRITE, 0, f".AGENTS.md.tmp.{n}")], now=now, wall=now)
            got = rig.core.tick(now=now)
            if got:
                b = got[-1]
                out.append((round(now - t0, 2), kinds(got), b["events"], b["first_write"], b["last_write"], b["settled"]))
        del rwd
        return out

    plain = run(tmp_path / "a", False)
    noisy = run(tmp_path / "b", True)
    assert plain == noisy and len(plain) == 2


# ---- B1, B6, B7, S4 -------------------------------------------------------


def test_written_unreadable_file_is_unknown_on_every_record(rig):
    rig.write("a", b"A")
    os.chmod(rig.p("a"), 0o200)
    rig.start()
    fd = os.open(rig.p("a"), os.O_WRONLY)
    os.write(fd, b"B")
    os.close(fd)
    rig.dirty("a", modify=True)
    rig.commit()
    rig.rename("a", "c")
    rig.commit()
    recs = [r for r in records(str(rig.out)) if r["path"] in ("a", "c")]
    assert [r["kind"] for r in recs] == ["modified", "moved"] and all(r.get("content_unknown") for r in recs)
    assert not [r for r in records(str(rig.out)) if r["kind"] == "rewritten"]


def test_event_log_write_failure_leaves_no_gap(rig, monkeypatch):
    rig.write("a", b"1")
    rig.start()
    from cohbench.watcher import records as recmod

    real = recmod.os.write
    calls = {"n": 0}

    def failing(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            real(fd, bytes(data)[:10])  # a partial line, then the failure
            raise OSError(28, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(recmod.os, "write", failing)
    rig.write("a", b"2")
    rig.dirty("a", modify=True)
    rig.t += 1
    with pytest.raises(OSError):
        rig.core.tick(force=True)
    # No new hint (round 5, B7a): the failed batch's scan already consumed
    # the hint and moved the scanner's signatures, so the watcher must
    # rescan everything on its own.
    assert rig.core.due()
    rig.t += 1
    out = rig.core.tick()
    assert out[-1]["reason"] == "recovery" and out[-1]["recovered"]
    ev = replay.read_events(str(rig.out))  # raises on a gap or a broken line
    assert [r["seq"] for r in ev] == list(range(1, len(ev) + 1))
    assert replay.state_at(str(rig.out)).entries["a"].sha256 == sha(b"2")


def test_event_log_that_cannot_be_cut_back_refuses_writes(rig, monkeypatch):
    rig.start()
    from cohbench.watcher import records as recmod

    monkeypatch.setattr(recmod.os, "write", lambda fd, data: (_ for _ in ()).throw(OSError(5, "I/O error")))
    monkeypatch.setattr(recmod.os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError(5, "I/O error")))
    rig.write("a", b"x")
    rig.dirty("a")
    rig.t += 1
    with pytest.raises(OSError):
        rig.core.tick(force=True)
    monkeypatch.undo()
    rig.dirty("a")
    rig.t += 1
    with pytest.raises(OSError, match="unusable"):
        rig.core.tick(force=True)


def test_diff_checks_blob_hashes(rig):
    rig.write("t.txt", b"one\n")
    rig.start()
    rig.write("t.txt", b"two\n")
    rig.dirty("t.txt", modify=True)
    rig.commit()
    with open(rig.core.blobs.path(sha(b"two\n")), "wb") as f:
        f.write(b"evil\n")
    with pytest.raises(ReplayError):
        replay.diff(str(rig.out), 0, 1)


def test_copy_failure_is_an_attempt_not_a_measurement(tmp_path):
    from tests.watcher.test_metric_copies import Rig as CRig
    from cohbench.watcher.manifest import Manifest

    r = CRig(tmp_path, "local")
    e = r.F(b"good")
    with open(r.blobs.path(e.sha256), "wb") as f:
        f.write(b"bad!")
    rec = r.runner.run_once(4, Manifest.of({"a": e}), "change")
    assert not rec["ok"] and rec["logged"] and 4 not in r.runner.measured
    r.close()


def test_event_log_failure_stops_the_watcher_and_the_heartbeat_says_failed(tmp_path, monkeypatch):
    from cohbench.watcher import records as recmod
    from cohbench.watcher.config import Config
    from cohbench.watcher.watcher import REQUEST, Watcher
    from tests.watcher.helpers import FakeBackend

    root = tmp_path / "root"
    root.mkdir()
    out = tmp_path / "out"
    w = Watcher(Config(root=str(root), out=str(out)), backend=FakeBackend())
    real = recmod.os.write
    state = {"failed": False}

    def failing(fd, data):
        if not state["failed"] and b'"kind": "mark"' in bytes(data):
            state["failed"] = True
            raise OSError(28, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(recmod.os, "write", failing)
    (out / "control").mkdir(parents=True, exist_ok=True)
    (out / "control" / ("m1" + REQUEST)).write_text(json.dumps({"label": "x"}))
    assert w.run() == 1
    hb = json.loads((out / "heartbeat.json").read_text())
    assert hb["status"] == "failed" and "No space left" in hb["error"] and hb["event_log_broken"] is None
    ev = replay.read_events(str(out))
    assert [r["seq"] for r in ev] == list(range(1, len(ev) + 1))


def test_name_forms_are_validated(tmp_path):
    from cohbench.watcher.config import Config

    root = tmp_path / "r"
    root.mkdir()
    for bad in (["{name}.tmp"], ["{name}", "x/{name}"], ["{name}", "plain"], "{name}"):
        with pytest.raises(ValueError):
            Config(root=str(root), out=str(tmp_path / "o"), ignore_name_forms=bad).validate()
    Config(root=str(root), out=str(tmp_path / "o"), ignore_name_forms=["{name}", "{name}.bak"]).validate()
