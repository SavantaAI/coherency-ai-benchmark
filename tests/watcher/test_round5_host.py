"""Fifth-review fixes, on any host."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.manifest import ReplayError
from tests.watcher.helpers import kinds
from tests.watcher.test_core_host import Rig
from tests.watcher.test_round3_host import root_wd


def sha(b):
    return hashlib.sha256(b).hexdigest()


def wd_of(core, rel):
    return next(wd for wd, info in core.wd_info.items() if info[0] == rel and info[2] == "tree")


# ---- B2a ------------------------------------------------------------------


def test_directory_moved_under_an_ignored_place_drops_its_writers(tmp_path):
    """The reviewer's order: a background writer inside d, d moved under an
    unwatched ignored directory, the writer keeps writing on d's old watch,
    then a task edit. The edit is due after quiet_ms, not after max_delay."""
    rig = Rig(tmp_path, ignore=["ign/**"], quiet_ms=100, max_delay_ms=3000)
    rig.write("d/log", b"")
    rig.write("x", b"0")
    (rig.p("ign")).mkdir()
    rig.start()
    rwd, dwd = root_wd(rig.core), wd_of(rig.core, "d")
    t0 = rig.t
    rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "log")], now=t0, wall=t0)
    os.rename(rig.p("d"), rig.p("ign/d"))
    raws = [RawEvent(rwd, I.IN_MOVED_FROM | I.IN_ISDIR, 9, "d")]
    rig.core.feed(raws, now=t0 + 0.01, wall=t0 + 0.01)
    assert dwd not in rig.core.wd_info
    rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "log")], now=t0 + 0.02, wall=t0 + 0.02)
    assert rig.core.pending.open_writes == {}
    rig.write("x", b"1")
    rig.core.feed([RawEvent(rwd, I.IN_MODIFY, 0, "x"), RawEvent(rwd, I.IN_CLOSE_WRITE, 0, "x")], now=t0 + 1.04, wall=t0 + 1.04)
    assert abs(rig.core.pending.deadline() - (t0 + 1.14)) < 1e-6
    rig.t = t0 + 1.2
    out = rig.core.tick()
    assert sorted(kinds(out)) == [("deleted", "d"), ("deleted", "d/log"), ("modified", "x")]
    assert rig.core.pending.open_writes == {}


def test_paired_directory_move_keeps_its_writer(tmp_path):
    rig = Rig(tmp_path, quiet_ms=100, max_delay_ms=3000)
    rig.write("d/log", b"")
    rig.start()
    rwd, dwd = root_wd(rig.core), wd_of(rig.core, "d")
    rig.core.feed([RawEvent(dwd, I.IN_MODIFY, 0, "log")], now=rig.t, wall=rig.t)
    os.rename(rig.p("d"), rig.p("e"))
    rig.core.feed([RawEvent(rwd, I.IN_MOVED_FROM | I.IN_ISDIR, 4, "d"), RawEvent(rwd, I.IN_MOVED_TO | I.IN_ISDIR, 4, "e")], now=rig.t, wall=rig.t)
    assert set(rig.core.pending.open_writes) == {"e/log"} and rig.core.wd_info[dwd][0] == "e"


def test_watch_removed_drops_writers_below_it(tmp_path):
    rig = Rig(tmp_path)
    rig.write("d/sub/log", b"")
    rig.start()
    swd = wd_of(rig.core, "d/sub")
    rig.core.feed([RawEvent(swd, I.IN_MODIFY, 0, "log")], now=rig.t, wall=rig.t)
    rig.core.feed([RawEvent(swd, I.IN_IGNORED, 0, "")], now=rig.t, wall=rig.t)
    assert rig.core.pending.open_writes == {}


def test_move_pair_split_across_reads_is_still_a_move(tmp_path):
    rig = Rig(tmp_path)
    rig.write("d/f", b"f")
    rig.start()
    rwd, dwd = root_wd(rig.core), wd_of(rig.core, "d")
    os.rename(rig.p("d"), rig.p("e"))
    rig.core.feed([RawEvent(rwd, I.IN_MOVED_FROM | I.IN_ISDIR, 4, "d")], now=rig.t, wall=rig.t)
    assert dwd not in rig.core.wd_info
    rig.core.feed([RawEvent(rwd, I.IN_MOVED_TO | I.IN_ISDIR, 4, "e")], now=rig.t, wall=rig.t)
    assert kinds(rig.commit()) == [("moved", "d", "e")]
    assert wd_of(rig.core, "e")  # watched again after the fresh listing


# ---- diff lineage ---------------------------------------------------------


def test_replacement_in_place_ends_the_rename_lineage(tmp_path):
    from tests.watcher.test_replay import Log

    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"old\n", ino=10))
    L.commit()
    L.add("modified", "a", L.entry(data=b"new\n", ino=11), changed=["content", "identity"], prev_sha256=sha(b"old\n"))
    L.commit()
    L.add("moved", "b", L.entry(data=b"new\n", ino=11), **{"from": "a"})
    L.commit()
    L.write()
    ds = {d.path: d for d in replay.diff(L.out, 0, 2)}
    assert ds["a"].status == "removed" and ds["b"].status == "added" and ds["b"].old_path is None
    back = {d.path: d for d in replay.diff(L.out, 2, 0)}
    assert back["b"].status == "removed" and back["a"].status == "added"
    # Without the replacement, the same move is a rename.
    L2 = Log(tmp_path / "plain")
    L2.add("created", "a", L2.entry(data=b"old\n", ino=10))
    L2.commit()
    L2.add("moved", "b", L2.entry(data=b"old\n", ino=10), **{"from": "a"})
    L2.commit()
    L2.write()
    assert [(d.status, d.old_path, d.path) for d in replay.diff(L2.out, 0, 1)] == [("renamed", "a", "b")]


# ---- unknown flag ---------------------------------------------------------


def test_unknown_stays_unknown_after_the_file_becomes_readable(tmp_path):
    from tests.watcher.helpers import records

    rig = Rig(tmp_path)
    rig.write("a", b"A")
    os.chmod(rig.p("a"), 0o200)
    rig.start()
    fd = os.open(rig.p("a"), os.O_WRONLY)
    os.write(fd, b"B")
    os.close(fd)
    rig.dirty("a", modify=True)
    rig.commit()
    os.chmod(rig.p("a"), 0o644)
    rig.dirty("a")
    rig.commit()
    rig.write("a", b"B")  # the same bytes again: not `rewritten`
    rig.dirty("a", modify=True)
    rig.commit()
    recs = [r for r in records(str(rig.out)) if r["path"] == "a"]
    assert [r["kind"] for r in recs] == ["modified", "modified", "modified"]  # after the baseline
    assert all(r.get("content_unknown") for r in recs) and recs[1]["sha256"] == sha(b"B")
    assert replay.state_at(str(rig.out)).entries["a"].epoch >= 1


# ---- isolation preflight --------------------------------------------------


def _layout(tmp_path, mode=0o700):
    base = tmp_path / "srv"
    w = base / "w"
    (w / "live").mkdir(parents=True)
    (w / "out").mkdir()
    os.chmod(w, mode)
    os.chmod(base, 0o755)
    return w


def _trusted(tmp_path):
    # Every ancestor of tmp_path: owned by root or by this user on a host.
    return (0, os.getuid())


def test_layout_accepted_as_documented(tmp_path):
    from cohbench.watcher.isolate import check_layout

    w = _layout(tmp_path)
    real = os.path.realpath(w)
    try:
        check_layout(f"{real}/live", f"{real}/out", os.getuid(), _trusted(tmp_path))
    except RuntimeError as e:
        if "writable by nobody else" in str(e):
            pytest.skip(f"this host's temporary directory has a group- or world-writable ancestor: {e}")
        raise


@pytest.mark.parametrize("case", ["mode", "owner", "separate_parents", "link", "same"])
def test_layout_refuses_anything_else(tmp_path, case):
    from cohbench.watcher.isolate import check_layout

    w = _layout(tmp_path, 0o750 if case == "mode" else 0o700)
    real = os.path.realpath(w)
    root, out, uid = f"{real}/live", f"{real}/out", os.getuid()
    if case == "owner":
        uid = uid + 1
    elif case == "separate_parents":
        (tmp_path / "elsewhere").mkdir()
        out = os.path.realpath(tmp_path / "elsewhere")
    elif case == "link":
        os.symlink(f"{real}/live", f"{real}/alias")
        root = f"{real}/alias"
    elif case == "same":
        out = root
    with pytest.raises(RuntimeError):
        check_layout(root, out, uid, _trusted(tmp_path))


# ---- D-037: marks never close a batch -------------------------------------


class QueueBackend(__import__("tests.watcher.helpers", fromlist=["FakeBackend"]).FakeBackend):
    """Events wait in a queue until the watcher reads it, as with inotify."""

    def __init__(self):
        super().__init__()
        self.q: list = []

    def read(self):
        out, self.q = self.q, []
        return out


class Sim:
    """A watcher on a queue backend with one controlled clock (monotonic and
    wall the same), quiet 100 ms, hold limit 2 s. A write happens at its own
    instant and only queues its events; the watcher sees them at its next
    read. The loop between reads does what Watcher.step does: commit a batch
    already due, read the queue, take mark requests, commit what is due,
    answer marks. `read_every` is the read interval in seconds, or a
    callable giving the next one (a busy watcher reads late)."""

    T0 = 1_000_000.0

    def __init__(self, tmp_path, read_every=0.005, **cfg):
        from cohbench.watcher.config import Config
        from cohbench.watcher.watcher import Watcher

        self.root = tmp_path / "root"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "f").write_text("0")
        self.t = self.T0
        self.backend = QueueBackend()
        self.w = Watcher(Config(root=str(self.root), out=str(tmp_path / "out"), quiet_ms=100, max_delay_ms=2000, **cfg), backend=self.backend)
        self.w.core.clock = self.w.core.wallclock = lambda: self.t
        self.w.start()
        self.rwd = root_wd(self.w.core)
        self.n = 0
        self.recs: list[dict] = []
        self.read_every = read_every if callable(read_every) else (lambda: read_every)
        self.writes: dict[str, float] = {}  # created file -> true write instant

    def write(self, rel="f"):
        """At the current instant: one closed write to rel (created if new)."""
        self.n += 1
        p = self.root / rel
        new = not p.exists()
        p.write_text(str(self.n))
        evs = [RawEvent(self.rwd, I.IN_CREATE, 0, rel)] if new else []
        self.backend.q += evs + [RawEvent(self.rwd, I.IN_MODIFY, 0, rel), RawEvent(self.rwd, I.IN_CLOSE_WRITE, 0, rel)]
        if new:
            self.writes[rel] = self.t

    def cycle(self, marks):
        w = self.w
        if w.core.due():
            self.recs += w.core.tick()
        w._read_raw()
        for arr, at, label, limit in [m for m in marks if m[0] <= self.t - self.T0 + 1e-9]:
            marks.remove((arr, at, label, limit))
            self.recs += w.request_mark(label, f"r{label}", at=None if at is None else self.T0 + at, limit_s=limit)
        if w.core.due():
            self.recs += w.core.tick()
        self.recs += w.service_marks()

    def run(self, until, writes=(), marks=(), rel=None):
        """Advance to `until` (seconds after T0). writes: offsets (to `rel`,
        or each to a new file w<n> when rel is None); marks: (arrival offset,
        at offset or None, label[, limit_s])."""
        todo = sorted(writes)
        ms = [tuple(m) + ((None,) if len(m) == 3 else ()) for m in sorted(marks, key=lambda m: m[0])]
        while self.t - self.T0 <= until:
            nxt = self.t + self.read_every()
            while todo and self.T0 + todo[0] < nxt:
                self.t = max(self.t, self.T0 + todo.pop(0))
                self.write(rel or f"w{self.n + 1}")
            self.t = nxt
            self.cycle(ms)

    def batches(self):
        return [(r["batch"], r["records"], r["first_write_after"], r["first_write"], r["last_write"]) for r in self.recs if r["kind"] == "batch"]

    def shape(self):
        """Batches as the files they created: comparable across runs."""
        return [sorted(x["path"] for x in self.recs if x.get("batch") == r["batch"] and x["kind"] not in ("batch", "mark")) for r in self.recs if r["kind"] == "batch"]

    def marks(self):
        return [{k: v for k, v in r.items() if k not in ("seq", "wall", "mono", "covered_utc", "request")} for r in self.recs if r["kind"] == "mark"]

    def ack(self, label):
        with open(os.path.join(self.w.out, "control", f"r{label}.ack.json")) as f:
            return json.load(f)


@pytest.mark.parametrize("arrival", [2.030, 2.050, 2.100, 2.300, 5.0, 9.9])
def test_two_writes_80_ms_apart_are_one_batch_whenever_the_mark_arrives(tmp_path, arrival):
    """The runner's fifth review: writes to one path at 2.000 s and 2.080 s,
    a mark for 2.030 s arriving any time up to 10 s. Same single batch, same
    answer: the baseline is in effect and batch 1 straddles the instant."""
    s = Sim(tmp_path)
    s.run(10.0, writes=[2.000, 2.080], marks=[(arrival, 2.030, "m")], rel="f")
    (b,) = s.batches()
    assert b[0] == 1 and b[1] == 1
    assert parse(b[2]) <= Sim.T0 + 2.000 <= parse(b[3]) and Sim.T0 + 2.080 <= parse(b[4]) < Sim.T0 + 2.090
    assert s.marks() == [{"kind": "mark", "label": "m", "accepted": True, "at": replay_utc(Sim.T0 + 2.03), "batch": 0, "straddled": 1, "straddled_last": 1, "settled": True}]
    a = s.ack("m")
    assert a["ok"] and a["batch"] == 0 and a["straddled"] == 1
    covered = parse(a["covered_utc"]) - Sim.T0
    assert max(arrival, 2.18) - 1e-6 <= covered <= max(arrival, 2.18) + 0.02


def test_mark_during_continuous_writing_is_answered_at_the_hold_limit(tmp_path):
    s = Sim(tmp_path)
    s.run(6.0, writes=[1.0 + i * 0.05 for i in range(100)], marks=[(2.0, None, "busy")], rel="f")
    a = s.ack("busy")
    first = [b for b in s.batches() if b[0] == 1][0]
    assert parse(first[2]) <= Sim.T0 + 1.0 <= parse(first[3])
    assert a["accepted"] and a["batch"] == 0 and a["straddled"] == 1
    assert abs(parse(a["covered_utc"]) - (Sim.T0 + 3.0)) < 0.03


def test_mark_on_a_quiet_tree_is_answered_at_once_and_commits_nothing(tmp_path):
    s = Sim(tmp_path)
    s.run(1.0, writes=[0.2], rel="f")
    (s.root / "f").write_text("changed with no event")  # found only by a sweep
    s.run(5.1, marks=[(5.0, None, "q")])
    a = s.ack("q")
    # Answered at the next regular read: the mark itself reads nothing.
    assert a["accepted"] and abs(parse(a["at"]) - (Sim.T0 + 5.0)) < 1e-6 and abs(parse(a["covered_utc"]) - (Sim.T0 + 5.005)) < 0.002
    assert a["batch"] == 1 and a["straddled"] is None
    assert [b[0] for b in s.batches()] == [1]  # the mark committed nothing
    # The change no notification reported is found by the next batch's sweep.
    s.run(7.0, writes=[6.0], rel="g")
    assert replay.state_at(s.w.out).entries["f"].sha256 == sha(b"changed with no event")


def test_mark_first_covered_after_its_limit_is_not_accepted(tmp_path):
    s = Sim(tmp_path)
    s.run(8.0, marks=[(1.0, 3.9, "late", 2.5)])  # covered only from 3.9 s; limit ends at 3.5 s
    a = s.ack("late")
    assert a["accepted"] is False and a["batch"] is None and a["straddled"] is None


def test_mark_for_an_instant_never_reached_is_not_accepted(tmp_path):
    s = Sim(tmp_path)
    s.run(12.0, marks=[(1.0, 30.0, "future")])
    a = s.ack("future")
    assert a["ok"] is False and a["accepted"] is False and a["batch"] is None and "limit_s" in a["error"]
    assert replay.marks(s.w.out) == [] and [m["accepted"] for m in replay.marks(s.w.out, include_rejected=True)] == [False]
    with pytest.raises(ReplayError):
        replay.mark_batch(s.w.out, "future")


def test_a_late_read_puts_close_writes_in_one_batch_and_the_mark_says_straddled(tmp_path):
    """Timing by read: writes 120 ms apart, read together after a delay,
    share a batch, and a mark between them reports the batch as straddling
    its instant rather than naming either side wrongly."""
    s = Sim(tmp_path, read_every=0.3)
    s.run(4.0, writes=[1.01, 1.13], marks=[(1.07, 1.07, "between")])
    assert [len(x) for x in s.shape()] == [2]
    a = s.ack("between")
    assert a["batch"] == 0 and a["straddled"] == 1


def test_mark_limit_must_exceed_the_hold_limit(tmp_path):
    s = Sim(tmp_path)
    ctl = os.path.join(s.w.out, "control")
    with open(os.path.join(ctl, "x.request.json"), "w") as f:
        json.dump({"label": "x", "limit_s": 1.5}, f)
    s.w.process_control()
    a = json.loads(open(os.path.join(ctl, "x.ack.json")).read())
    assert a["ok"] is False and "hold limit" in a["error"]


def replay_utc(t):
    from cohbench.watcher.records import utc_from_epoch

    return utc_from_epoch(t)


def parse(s):
    from cohbench.watcher.records import parse_utc

    return parse_utc(s)


MARK_SEEDS = int(os.environ.get("WATCHER_MARK_SEEDS", "150"))


@pytest.mark.parametrize("seed", range(MARK_SEEDS))
def test_property_random_marks_leave_the_batches_alone(tmp_path, seed):
    """Random writes (bursts, pauses, a writer that never pauses for longer
    than the quiet period), each creating its own file, read through a queue
    at random intervals (a busy watcher reads late), and marks at random
    instants arriving at random later times. The batches equal those of the
    same writes with no marks; every mark is accepted; every write in the
    named batch or earlier happened at or before the instant; every write at
    or before it is in the named batch or earlier, or in the straddled batch;
    a batch holding writes on both sides of the instant is the straddled
    one."""
    import random

    rng = random.Random(seed)
    writes, t = [], 0.3
    while t < 12.0:
        kind = rng.random()
        if kind < 0.4:
            writes += [round(t + i * rng.choice([0.01, 0.04, 0.08]), 4) for i in range(rng.randint(1, 6))]
        elif kind < 0.55:
            writes += [round(t + i * 0.05, 4) for i in range(rng.randint(20, 60))]  # past the hold limit
        t = round(t + rng.uniform(0.05, 1.5), 3)
    writes = sorted(set(w for w in writes if w < 14.0))
    marks = []
    for k in range(rng.randint(3, 10)):
        at = round(rng.uniform(0.2, 14.0), 4)
        arrival = round(at + rng.choice([0.0, 0.0, rng.uniform(0, 0.3), rng.uniform(0, 3.0)]), 4)
        marks.append((arrival, at, f"k{k}"))
    gaps = random.Random(seed * 31 + 7)
    intervals = [gaps.choice([0.002, 0.005, 0.01, 0.03, 0.08]) for _ in range(20000)]

    def reader():
        it = iter(intervals)
        return lambda: next(it)

    plain = Sim(tmp_path / "plain", read_every=reader())
    plain.run(25.0, writes=list(writes))
    marked = Sim(tmp_path / "marked", read_every=reader())
    marked.run(25.0, writes=list(writes), marks=marks)
    # Marks are pure observers: the same reads give the very same batches,
    # write times included.
    assert plain.batches() == marked.batches() and plain.shape() == marked.shape(), f"seed {seed}"
    got = {m["label"]: m for m in marked.marks()}
    assert set(got) == {m[2] for m in marks} and all(m["accepted"] for m in got.values())
    batch_of = {x["path"]: x["batch"] for x in marked.recs if x["kind"] == "created"}
    for arrival, at, label in marks:
        m, inst = got[label], Sim.T0 + at
        b, st = m["batch"], m["straddled"]
        for path, wt in marked.writes.items():
            wb = batch_of[path]
            if wb <= b:
                assert wt <= inst + 1e-9, (seed, label, path)
            if wt <= inst:
                assert wb <= b or wb == st, (seed, label, path)
        for x in {batch_of[p] for p in marked.writes}:
            times = [marked.writes[p] for p in marked.writes if batch_of[p] == x]
            if min(times) <= inst < max(times):
                assert x == st, (seed, label, x)


def test_mark_is_answered_at_once_while_a_long_metric_run_is_in_progress(tmp_path):
    """The acknowledgement never waits for a reading: the named batch's
    reading is in it only if already written (null here), and the limit
    covers batching only."""
    import sys
    import time as _time

    slow = "import json, time; time.sleep(30 if open('f').read() != '0' else 0); print('{}')"
    s = Sim(tmp_path, metric_cmd=[sys.executable, "-c", slow], metric_timeout_s=3, metric_drain_s=0, work_dir=str(tmp_path / "w"))
    s.run(1.0, writes=[0.2])  # batch 1 commits at 0.3; its reading starts and runs
    deadline = _time.monotonic() + 5
    while s.w.metrics.idle() and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert not s.w.metrics.idle()
    t0 = _time.monotonic()
    s.run(1.6, marks=[(1.5, None, "busy-metric")])
    a = s.ack("busy-metric")
    assert _time.monotonic() - t0 < 1.0
    assert a["ok"] and a["accepted"] and a["batch"] == 1 and a["metric"] is None
    s.w.stop()
    s.w.shutdown()
