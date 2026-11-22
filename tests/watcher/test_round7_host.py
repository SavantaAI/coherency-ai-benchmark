"""Seventh-review fixes, on any host."""

from __future__ import annotations

import json
import os
import random
import sys

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.manifest import ReplayError
from tests.watcher.test_core_host import Rig
from tests.watcher.test_round5_host import Sim, parse


def bounds(s):
    """batch -> (first_write_after, last_write) for batches with known times."""
    return {r["batch"]: (parse(r["first_write_after"]), parse(r["last_write"])) for r in s.recs if r["kind"] == "batch" and r.get("write_times") == "known"}


# ---- W2: a mark is a pure observer ------------------------------------------


def test_reviewer_case_a_mark_does_not_split_two_writes(tmp_path):
    """The reviewer's writes to one path at 1.000 s and 1.140 s, regular
    reads every 50 ms. A mark at 1.010 s reads nothing, so the batches are
    the very same with it as without (here two: the reads put 150 ms
    between the two writes' reads)."""
    plain = Sim(tmp_path / "a", read_every=0.05)
    plain.run(3.0, writes=[1.000, 1.140], rel="f")
    marked = Sim(tmp_path / "b", read_every=0.05)
    marked.run(3.0, writes=[1.000, 1.140], marks=[(1.010, None, "m")], rel="f")
    assert plain.batches() == marked.batches()
    assert marked.ack("m")["accepted"]


MARK_SEEDS = int(os.environ.get("WATCHER_MARK_SEEDS", "150"))


@pytest.mark.parametrize("seed", range(MARK_SEEDS))
def test_property_marks_change_nothing_with_repeated_writes_to_one_path(tmp_path, seed):
    """The same writes (all to one path) and the same regular reads give the
    same batches, write times included, with and without marks at arbitrary
    times. Every write lies inside its batch's recorded bounds, and every
    mark's answer agrees with those bounds."""
    rng = random.Random(seed)
    writes, t = [], 0.3
    while t < 10.0:
        if rng.random() < 0.5:
            writes += [round(t + i * rng.choice([0.01, 0.03, 0.07, 0.12]), 4) for i in range(rng.randint(1, 8))]
        else:
            writes += [round(t + i * 0.05, 4) for i in range(rng.randint(10, 50))]
        t = round(t + rng.uniform(0.05, 1.5), 3)
    writes = sorted(set(w for w in writes if w < 12.0))
    marks = [(round(at + rng.choice([0.0, rng.uniform(0, 0.2), rng.uniform(0, 2.5)]), 4), at if rng.random() < 0.7 else None, f"k{k}")
             for k, at in enumerate(sorted(round(rng.uniform(0.2, 12.0), 4) for _ in range(rng.randint(3, 12))))]
    gaps = random.Random(seed * 17 + 3)
    intervals = [gaps.choice([0.002, 0.005, 0.02, 0.05, 0.11]) for _ in range(20000)]

    def reader():
        it = iter(intervals)
        return lambda: next(it)

    plain = Sim(tmp_path / "plain", read_every=reader())
    plain.run(20.0, writes=list(writes), rel="f")
    marked = Sim(tmp_path / "marked", read_every=reader())
    marked.run(20.0, writes=list(writes), marks=marks, rel="f")
    assert plain.batches() == marked.batches(), f"seed {seed}"
    bs = bounds(marked)
    # Every write lies in (first_write_after, last_write] of some batch.
    for w in writes:
        # (lo <= w: the simulated clock can put a write at the very instant
        # a read began; real time always moves on.)
        assert any(lo <= Sim.T0 + w <= hi + 1e-9 for lo, hi in bs.values()), (seed, w)
    got = {m["label"]: m for m in marked.marks()}
    assert len(got) == len(marks) and all(m["accepted"] for m in got.values()), seed
    for m in got.values():
        at, b, st = parse(m["at"]), m["batch"], m["straddled"]
        if b in bs:
            assert bs[b][1] <= at + 1e-6, (seed, m)
        for x, (lo, hi) in bs.items():
            if x > b:
                assert hi > at - 1e-6, (seed, m, x)
                if lo < at < hi:
                    assert x == st, (seed, m, x)
    replay.read_events(marked.w.out)  # replay's own bounds check agrees


# ---- W3: upper bound is the time the read returned --------------------------


def test_write_delivered_during_a_read_is_bounded_by_the_read_return(tmp_path):
    s = Sim(tmp_path, read_every=0.05)
    s.run(0.95)
    real = s.backend.read

    def slow_read():
        # The read begins at 1.000 s; a write lands at 1.001 s while it runs
        # and is delivered by it.
        if abs(s.t - (Sim.T0 + 1.0)) < 1e-9:
            s.t += 0.001
            s.write("f")
        return real()

    s.backend.read = slow_read
    s.run(3.0, marks=[(2.0, 1.000, "m")])
    (b,) = [r for r in s.recs if r["kind"] == "batch"]
    assert abs(parse(b["last_write"]) - (Sim.T0 + 1.001)) < 1e-6 and abs(parse(b["first_write_after"]) - (Sim.T0 + 0.95)) < 1e-6
    a = s.ack("m")
    assert a["accepted"] and a["batch"] == 0 and a["straddled"] == 1


def test_a_capped_read_does_not_confirm_coverage(tmp_path):
    s = Sim(tmp_path)
    real = s.backend.read

    def capped_read():
        s.backend.capped = Sim.T0 + 1.0 <= s.t < Sim.T0 + 1.2
        return real()

    s.backend.read = capped_read
    s.run(2.0, marks=[(1.0, 1.05, "m")])
    a = s.ack("m")
    assert a["accepted"] and parse(a["covered_utc"]) >= Sim.T0 + 1.2 - 1e-9


# ---- W4: a change no notification covered ends at the scan's end -------------


def test_child_created_before_its_directory_is_watched_moves_the_upper_bound(tmp_path):
    s = Sim(tmp_path)
    s.run(0.99)
    (s.root / "new").mkdir()
    s.backend.q += [RawEvent(s.rwd, I.IN_CREATE | I.IN_ISDIR, 0, "new")]
    s.run(1.075)
    (s.root / "new" / "child").write_text("c")  # no watch on new yet: no event
    s.run(2.0)
    (b,) = [r for r in s.recs if r["kind"] == "batch"]
    assert "new/child" in replay.state_at(s.w.out, b["batch"]).entries
    assert parse(b["last_write"]) >= Sim.T0 + 1.08  # the scan's end, not the mkdir's read
    assert b["write_times"] == "known"


def test_notified_write_keeps_its_read_bound(tmp_path):
    s = Sim(tmp_path, read_every=0.05)
    s.run(2.0, writes=[1.01], rel="f")
    (b,) = [r for r in s.recs if r["kind"] == "batch"]
    assert abs(parse(b["last_write"]) - (Sim.T0 + 1.05)) < 1e-6  # the read's return, not the scan's end


# ---- W7: offline settings from the recorded config --------------------------


def _finished_run(tmp_path, **cfg):
    from cohbench.watcher.config import Config
    from cohbench.watcher.watcher import Watcher
    from tests.watcher.helpers import FakeBackend

    root = tmp_path / "root"
    root.mkdir()
    (root / "f").write_text("0")
    out = tmp_path / "out"
    cmd = [sys.executable, "-c", "import json, os; print(json.dumps({'mtime': int(os.stat('f').st_mtime)}))"]
    w = Watcher(Config(root=str(root), out=str(out), metric_cmd=cmd, metric_drain_s=0, work_dir=str(tmp_path / "w"), **cfg), backend=FakeBackend())
    w.start()
    w.metrics.executor.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no time"))
    (root / "f").write_text("1")
    w.core.pending.touch("f", w.core.clock(), w.core.wallclock(), modify=True, change=True)
    w.core.tick(force=True)
    w.stop()
    w.shutdown()
    return out


def test_completion_takes_the_recorded_copy_mtime(tmp_path):
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete

    out = _finished_run(tmp_path, metric_copy_mtime=1234567890)
    res = complete(str(out), LocalExecutor(str(tmp_path / "w2"), None, "/work/project"))  # executor default mtime
    recs = [json.loads(x) for x in (out / "metrics.jsonl").read_text().splitlines()]
    assert res["measured"] == [1]
    assert {x["batch"]: x["metrics"]["mtime"] for x in recs if x.get("measured")} == {0: 1234567890, 1: 1234567890}


def test_completion_refuses_without_a_readable_config(tmp_path):
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete

    out = _finished_run(tmp_path)
    os.unlink(out / "config.json")
    with pytest.raises(RuntimeError, match="cannot read the run's config"):
        complete(str(out), LocalExecutor(str(tmp_path / "w2"), None, "/work/project"))


def test_completion_refuses_another_workspace_path(tmp_path):
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete

    out = _finished_run(tmp_path)
    with pytest.raises(RuntimeError, match="workspace path"):
        complete(str(out), LocalExecutor(str(tmp_path / "w2"), None, "/elsewhere"))


def test_reuse_key_includes_timeout_and_output_limits(tmp_path):
    from tests.watcher.test_metrics import slow_runner

    r, m, out, blobs = slow_runner(tmp_path, "print('{}')")
    k1 = r.settings()
    r.timeout = 99
    assert r.settings() != k1 and {"timeout", "stdout_max", "stderr_max", "copy_mtime", "workspace"} <= set(k1)


# ---- minors -------------------------------------------------------------------


def test_replay_checks_a_mark_against_the_recorded_bounds(tmp_path):
    from tests.watcher.test_replay import Log

    def log(name, mark):
        L = Log(tmp_path / name)
        L.add("created", "a", L.entry(data=b"1"))
        L.commit()
        L.lines[-1].update(write_times="none", first_write_after=None, wall="2026-01-01T00:00:00.000000Z")
        L.add("modified", "a", L.entry(data=b"2"))
        L.seq += 1
        L.lines.append({"kind": "batch", "batch": 1, "records": 1, "seq": L.seq, "write_times": "known", "first_write_after": "2026-01-01T00:00:01.000000Z", "first_write": "2026-01-01T00:00:01.100000Z", "last_write": "2026-01-01T00:00:02.000000Z"})
        L.open = []
        L.seq += 1
        L.lines.append({"kind": "mark", "label": "m", "accepted": True, "seq": L.seq, **mark})
        L.write()
        return L.out

    replay.read_events(log("ok", {"at": "2026-01-01T00:00:01.500000Z", "batch": 0, "straddled": 1, "straddled_last": 1}))
    replay.read_events(log("after", {"at": "2026-01-01T00:00:03.000000Z", "batch": 1, "straddled": None, "straddled_last": None}))
    for name, mark in (("missed", {"at": "2026-01-01T00:00:01.500000Z", "batch": 0, "straddled": None, "straddled_last": None}),
                       ("early", {"at": "2026-01-01T00:00:01.500000Z", "batch": 1, "straddled": None, "straddled_last": None}),
                       ("outside", {"at": "2026-01-01T00:00:00.500000Z", "batch": 0, "straddled": 1, "straddled_last": 1}),
                       ("no_last", {"at": "2026-01-01T00:00:01.500000Z", "batch": 0, "straddled": 1})):
        with pytest.raises(ReplayError):
            replay.read_events(log(name, mark))


def test_symbolic_refs_into_an_ignored_namespace_are_left_out(tmp_path):
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "refs" / "t3").mkdir(parents=True)
    (g / "refs" / "t3" / "x").write_text("a" * 40 + "\n")
    (g / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    (g / "refs" / "heads" / "alias").write_text("ref: refs/t3/x\n")
    (g / "HEAD").write_text("ref: refs/t3/x\n")
    rig.start()
    st = replay.state_at(str(rig.out)).git
    assert st["refs"] == {"refs/heads/main": "b" * 40} and st["head"] is None
    for d, _, fs in os.walk(rig.out):
        for f in fs:
            assert b"refs/t3" not in open(os.path.join(d, f), "rb").read()
