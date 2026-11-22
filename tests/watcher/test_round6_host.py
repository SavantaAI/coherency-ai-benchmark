"""Sixth-review fixes, on any host."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.manifest import Entry, Manifest, ReplayError
from cohbench.watcher.pending import Pending
from tests.watcher.helpers import kinds, records
from tests.watcher.test_core_host import Rig
from tests.watcher.test_round3_host import root_wd
from tests.watcher.test_round5_host import Sim, parse, wd_of


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---- B1: a silent writer expires without another event ---------------------


def test_silent_writer_releases_the_next_batch_at_its_expiry():
    p = Pending(0.1, 2.0)
    p.touch("log", 1.9, 1.9, modify=True, change=True)  # never closed
    p.take(2.5)  # taken while the writer still holds; it carries over
    assert set(p.open_writes) == {"log"}
    p.touch("x", 3.85, 3.85, modify=True, change=True)
    p.touch("x", 3.85, 3.85, close_write=True)
    # The writer expires at 3.90 (1.9 + 2.0); the edit is quiet at 3.95.
    assert abs(p.deadline() - 3.95) < 1e-9
    assert p.due(3.95) and not p.due(3.94)


def test_live_writer_still_holds_until_it_expires():
    p = Pending(0.1, 2.0)
    p.touch("log", 1.0, 1.0, modify=True, change=True)
    p.touch("x", 1.5, 1.5, modify=True, change=True)
    p.touch("x", 1.5, 1.5, close_write=True)
    assert abs(p.deadline() - 3.0) < 1e-9  # min(first + hold, writer expiry 3.0)
    p.touch("log", 2.4, 2.4, modify=True, change=True)
    assert abs(p.deadline() - 3.0) < 1e-9  # the hold limit caps it


# ---- V1: move pairs by cookie, whatever lies between ----------------------


@pytest.mark.parametrize("between", [[], ["AGENTS.md"], ["AGENTS.md", ".CLAUDE.md.tmp.1"]])
def test_ignored_events_between_move_halves_change_nothing(tmp_path, between):
    rig = Rig(tmp_path, quiet_ms=100, max_delay_ms=3000)
    rig.write("d/log", b"")
    rig.start()
    rwd, dwd = root_wd(rig.core), wd_of(rig.core, "d")
    rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "log")], now=rig.t, wall=rig.t)
    os.rename(rig.p("d"), rig.p("e"))
    for n in between:
        rig.p(n).write_text("i")
    raws = [RawEvent(rwd, I.IN_MOVED_FROM | I.IN_ISDIR, 4, "d")] + [RawEvent(rwd, I.IN_CREATE, 0, n) for n in between] + [RawEvent(rwd, I.IN_MOVED_TO | I.IN_ISDIR, 4, "e")]
    rig.core.feed(raws, now=rig.t, wall=rig.t)
    assert set(rig.core.pending.open_writes) == {"e/log"} and rig.core.wd_info[dwd][0] == "e"
    assert rig.core.pending.events == 3  # the modify and the two move halves; ignored events touched nothing


def test_a_tracked_event_between_move_halves_keeps_the_pair(tmp_path):
    rig = Rig(tmp_path, quiet_ms=100, max_delay_ms=3000)
    rig.write("d/log", b"")
    rig.write("x", b"0")
    rig.start()
    rwd, dwd = root_wd(rig.core), wd_of(rig.core, "d")
    rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "log")], now=rig.t, wall=rig.t)
    os.rename(rig.p("d"), rig.p("e"))
    rig.write("x", b"1")
    rig.core.feed([RawEvent(rwd, I.IN_MOVED_FROM | I.IN_ISDIR, 4, "d"), RawEvent(rwd, I.IN_MODIFY, 0, "x"), RawEvent(rwd, I.IN_CLOSE_WRITE, 0, "x"), RawEvent(rwd, I.IN_MOVED_TO | I.IN_ISDIR, 4, "e")], now=rig.t, wall=rig.t)
    assert set(rig.core.pending.open_writes) == {"e/log"}
    assert sorted(kinds(rig.commit())) == [("modified", "x"), ("moved", "d", "e"), ("rewritten", "e/log")]


# ---- timing by read ---------------------------------------------------------


def test_batch_record_has_the_first_write_lower_bound(tmp_path):
    s = Sim(tmp_path, read_every=0.05)
    s.run(1.0, writes=[0.33, 0.36])
    (b,) = [r for r in s.recs if r["kind"] == "batch"]
    after, first, last = parse(b["first_write_after"]), parse(b["first_write"]), parse(b["last_write"])
    assert b["write_times"] == "known"
    assert after < Sim.T0 + 0.33 <= first and Sim.T0 + 0.36 <= last
    assert abs(after - (Sim.T0 + 0.30)) < 1e-6 and abs(first - (Sim.T0 + 0.35)) < 1e-6 and abs(last - (Sim.T0 + 0.40)) < 1e-6


def test_mark_without_at_counts_writes_already_queued(tmp_path):
    """Reviewer 2 N1: a write read before the mark's instant (here by the
    read at 1.0 s, the request's time) is before it, so the mark names its
    batch once it closes."""
    s = Sim(tmp_path, read_every=0.2)
    s.run(0.9, writes=[0.85])  # queued, not yet read
    s.recs += s.w.request_mark("now", "rnow")  # reads the queue first, then takes its instant
    s.run(2.0)
    a = s.ack("now")
    assert a["accepted"] and a["batch"] == 1 and a["straddled"] is None and a["straddled_last"] is None
    assert "w1" in replay.state_at(s.w.out, a["batch"]).entries


def test_recovery_batch_says_its_write_times_are_unknown(rig, monkeypatch):
    from cohbench.watcher import records as recmod

    rig.write("a", b"1")
    rig.start()
    real = recmod.os.write
    state = {"n": 0}

    def failing(fd, data):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError(28, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(recmod.os, "write", failing)
    rig.write("a", b"2")
    rig.core.pending.touch("a", rig.t, 1.7e9 + rig.t, modify=True, change=True)
    rig.t += 1
    with pytest.raises(OSError):
        rig.core.tick(force=True)
    rig.t += 1
    b = rig.core.tick()[-1]
    assert b["reason"] == "recovery" and b["write_times"] == "unknown" and b["first_write"] is None and b["last_write"] is None
    assert b["first_write_after"] is not None  # after the previous batch's commit


def test_sweep_only_batch_says_no_write_times(rig):
    rig.write("x", b"0")
    rig.start()
    rig.write("x", b"changed")
    rig.t += 1
    b = rig.core.tick(force=True)[-1]
    assert b["write_times"] == "none" and b["first_write"] is None and b["first_write_after"] is not None


# ---- replay -----------------------------------------------------------------


def test_replay_validates_straddled(tmp_path):
    from tests.watcher.test_replay import Log

    for straddled, ok in ((None, True), (1, True), (0, False), (2, False), ("1", False)):
        L = Log(tmp_path / str(straddled))
        L.add("created", "a", L.entry(data=b"1"))
        L.commit()
        L.add("modified", "a", L.entry(data=b"2"))
        L.commit()
        L.seq += 1
        L.lines.append({"kind": "mark", "label": "m", "batch": 0, "accepted": True, "straddled": straddled, "seq": L.seq})
        L.write()
        if ok:
            replay.read_events(L.out)
        else:
            with pytest.raises(ReplayError):
                replay.read_events(L.out)


# ---- unknown flag on the first unreadable write ----------------------------


def test_file_created_and_written_unreadable_in_one_batch_is_unknown(rig):
    rig.write("keep", b"k")
    rig.start()
    fd = os.open(rig.p("w"), os.O_WRONLY | os.O_CREAT, 0o200)
    os.write(fd, b"secret")
    os.close(fd)
    rig.core.pending.touch("w", rig.t, 0.0, created=True, change=True)
    rig.core.pending.touch("w", rig.t, 0.0, modify=True, change=True)
    rig.commit()
    os.chmod(rig.p("w"), 0o644)
    rig.dirty("w")
    rig.commit()
    recs = [r for r in records(str(rig.out)) if r["path"] == "w"]
    assert recs[0]["kind"] == "created" and recs[0].get("content_unknown") and all(r.get("content_unknown") for r in recs)


# ---- metrics ----------------------------------------------------------------


def test_classify_link_keeps_the_components_after_a_file():
    from cohbench.watcher.metrics import classify_link

    F = Entry(type="file", mode=0o644, uid=0, gid=0, dev=1, ino=3, size=1, sha256="x", content="stored")
    m = Manifest.of({"a": F})
    assert classify_link("l", "/work/project/a/b", "/work/project", m) == "a/b"
    assert classify_link("l", "missing/x/y", "/work/project", m) == "missing/x/y"


def test_broken_link_through_a_file_stays_broken_in_the_local_copy(tmp_path):
    from tests.watcher.test_metric_copies import Rig as CRig

    r = CRig(tmp_path, "local")
    m = Manifest.of({"a": r.F(b"file"), "l": r.L("/work/project/a/b")})
    r.runner.cmd = [sys.executable, "-c", "import json, os; print(json.dumps({'exists': os.path.exists('l')}))"]
    rec = r.runner.run_once(0, m, "change")
    assert rec["ok"] and rec["metrics"] == {"exists": False}
    r.close()


def test_blob_requests_outside_the_manifest_are_refused(tmp_path):
    import threading

    from cohbench.watcher.blobs import BlobStore
    from cohbench.watcher.metrics import IsolatedExecutor, _recv, _send

    to_r, to_w = os.pipe()
    fr_r, fr_w = os.pipe()
    blobs = BlobStore(str(tmp_path))
    blobs.put(sha(b"other"), b"other")

    def evil_server():
        _recv(to_r)
        _send(fr_w, json.dumps({"need": [sha(b"other")]}).encode())

    t = threading.Thread(target=evil_server, daemon=True)
    t.start()
    ex = IsolatedExecutor(to_w, fr_r, blobs)
    e = Entry(type="file", mode=0o644, uid=0, gid=0, dev=1, ino=3, size=1, sha256=sha(b"a"), content="stored")
    with pytest.raises(ValueError, match="outside the submitted manifest"):
        ex.run(Manifest.of({"a": e}), ["true"], 1, 100, 100)
    t.join(timeout=5)
    ex.close()


def test_reuse_key_includes_the_copy_mtime(tmp_path):
    from cohbench.watcher.metrics import tree_digest

    blobs_has = lambda s: True
    m = Manifest.of({"a": Entry(type="file", mode=0o644, uid=0, gid=0, dev=1, ino=3, size=1, sha256="x", content="stored")})
    base = {"cmd": ["c"], "isolation": "users", "workspace": "/work/project", "copy_mtime": 1}
    assert tree_digest(m, blobs_has, base) != tree_digest(m, blobs_has, {**base, "copy_mtime": 2})
    assert tree_digest(m, blobs_has, base) != tree_digest(m, blobs_has, {**base, "cmd": ["d"]})


def test_shutdown_gives_up_a_stuck_job_at_one_deadline(tmp_path):
    import time

    from tests.watcher.test_metrics import slow_runner

    r, m, out, blobs = slow_runner(tmp_path, "print('{}')")
    r.timeout = 0.5
    started = __import__("threading").Event()
    real = r.executor.run

    def stuck(*a, **k):  # the copy or the protocol hangs, not the command
        started.set()
        time.sleep(3)
        return real(*a, **k)

    r.executor.run = stuck
    r.start()
    r.enqueue(0, m)
    started.wait(5)
    t0 = time.monotonic()
    left = r.finish(0.2)
    assert time.monotonic() - t0 < 1.5 and left == [0]
    time.sleep(3.2)  # the abandoned job finishes; its reading is not written
    recs = [json.loads(x) for x in (out / "metrics.jsonl").read_text().splitlines()]
    assert [(x["batch"], x["reason"]) for x in recs] == [(0, "backlog")] and "shutdown deadline" in recs[0]["error"]


def test_local_copy_mismatch_on_the_first_run_is_a_failed_reading(tmp_path):
    from tests.watcher.test_metric_copies import Rig as CRig

    r = CRig(tmp_path, "local")
    e = r.F(b"good")
    os.unlink(r.blobs.path(e.sha256))  # the blob is missing: absent, then the copy check
    with open(r.blobs.path(e.sha256), "wb") as f:
        f.write(b"bad!")
    rec = r.runner.run_once(0, Manifest.of({"a": e}), "change")
    assert not rec["ok"] and rec["error"].startswith("copy verification failed")
    r.close()


def test_offline_completion_continues_run_numbers(tmp_path):
    from cohbench.watcher.config import Config
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete
    from cohbench.watcher.watcher import Watcher
    from tests.watcher.helpers import FakeBackend

    root = tmp_path / "root"
    root.mkdir()
    (root / "f").write_text("0")
    out = tmp_path / "out"
    cmd = [sys.executable, "-c", "import json; print(json.dumps({'f': open('f').read()}))"]
    w = Watcher(Config(root=str(root), out=str(out), metric_cmd=cmd, metric_drain_s=0, work_dir=str(tmp_path / "w")), backend=FakeBackend())
    w.start()
    w.metrics.executor.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no time"))  # nothing measured live after the baseline
    for i in (1, 2):
        (root / "f").write_text(str(i))
        w.core.pending.touch("f", w.core.clock(), w.core.wallclock(), modify=True, change=True)
        w.core.tick(force=True)
    w.stop()
    w.shutdown()
    ex = LocalExecutor(str(tmp_path / "w2"), None, "/work/project")
    res = complete(str(out), ex)
    recs = [json.loads(x) for x in (out / "metrics.jsonl").read_text().splitlines()]
    runs = [x["run"] for x in recs if x.get("run") is not None]
    assert len(runs) == len(set(runs)) and runs == sorted(runs)
    assert {x["batch"]: x["metrics"]["f"] for x in recs if x.get("measured")} == {0: "0", 1: "1", 2: "2"}
    assert res["already"] == 1


def test_offline_command_refuses_outside_the_image(tmp_path):
    from cohbench.watcher.offline import main_complete

    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(RuntimeError, match="only in the watcher image"):
        main_complete(str(tmp_path))


# ---- isolation preflight, git namespaces -----------------------------------


def test_failed_acl_inspection_refuses_the_layout(tmp_path, monkeypatch):
    from cohbench.watcher import isolate
    from tests.watcher.test_round5_host import _layout

    w = _layout(tmp_path)
    real = os.path.realpath(w)

    def broken(path, follow_symlinks=True):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(isolate.os, "listxattr", broken, raising=False)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        isolate.check_layout(f"{real}/live", f"{real}/out", os.getuid(), (0, os.getuid()))


def test_ignored_ref_namespace_is_a_whole_prefix(tmp_path):
    rig = Rig(tmp_path, ignore_git_refs=["refs/t3"])
    g = rig.p(".git")
    for name in ("t3/x", "t30/y"):
        (g / "refs" / os.path.dirname(name)).mkdir(parents=True, exist_ok=True)
        (g / "refs" / name).write_text("a" * 40 + "\n")
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    rig.start()
    assert set(replay.state_at(str(rig.out)).git["refs"]) == {"refs/t30/y"}
