"""Every bound the watcher records is true (D-042): the checker's five
controlled-clock sequences, and a property that checks every recorded bound
and every mark answer against the true write times, with capped reads and
changes no notification reported."""

from __future__ import annotations

import json
import os
import random

import pytest

from cohbench.watcher import replay
from cohbench.watcher import scan as scanmod
from cohbench.watcher.replay import answer_for
from tests.watcher.test_core_host import Rig
from tests.watcher.test_round5_host import Sim, parse

T0 = Sim.T0


def bounds_of(recs):
    """batch -> (lo, hi) from the batch records, as replay reads them."""
    out = {}
    for r in recs:
        if r["kind"] == "batch":
            hi = parse(r["last_write"]) if r["write_times"] == "known" else parse(r["wall"])
            lo = parse(r["first_write_after"]) if r["first_write_after"] else hi
            out[r["batch"]] = (lo, hi)
    return out


class Scripted(Sim):
    """A Sim whose reads follow a script: read(t) -> "cap" delivers nothing
    and says the cap was hit; anything else delivers the whole queue."""

    def __init__(self, tmp_path, script, **kw):
        super().__init__(tmp_path, **kw)
        real = self.backend.read

        def read():
            if script(round(self.t - T0, 6)) == "cap":
                self.backend.capped = True
                return []
            self.backend.capped = False
            return real()

        self.backend.read = read


# ---- 1. capped reads -----------------------------------------------------------


def test_capped_read_does_not_move_the_lower_bound_or_confirm_a_mark(tmp_path):
    """A write at 0.900 stays queued through a capped read at 1.000 and is
    delivered at 1.030: the batch's lower bound stays at the last read that
    emptied the queue, and a mark for 0.950 counts the write as straddling."""
    s = Scripted(tmp_path, lambda t: "cap" if abs(t - 1.0) < 1e-9 else "", read_every=None or (lambda: 0.0))
    # Reads at 0.85, 1.000 (capped), 1.030, then every 10 ms.
    times = iter([0.85, 1.0, 1.03] + [round(1.03 + 0.01 * k, 3) for k in range(1, 400)])
    s.t = T0 + 0.80
    prev = [0.80]

    def nxt():
        t = next(times)
        d = t - prev[0]
        prev[0] = t
        return d

    s.read_every = nxt
    s.run(3.0, writes=[0.9], marks=[(2.0, 0.95, "m")])
    (b,) = [r for r in s.recs if r["kind"] == "batch"]
    assert abs(parse(b["first_write_after"]) - (T0 + 0.85)) < 1e-6 and abs(parse(b["last_write"]) - (T0 + 1.03)) < 1e-6
    a = s.ack("m")
    assert a["accepted"] and (a["batch"], a["straddled"], a["straddled_last"]) == (0, 1, 1)


def test_validation_reads_past_a_capped_read_before_settling(tmp_path, monkeypatch):
    """A second write to f at 1.110 lands just before the scan, so the
    batch's content holds it, and the validation's first read at 1.120
    stops at its cap before that write's notification. The validation keeps
    reading until a read empties the queue, so the batch records the second
    write's read in its bounds instead of settling on `last_write` 1.000.

    The write is made before the scan reads f, not during validation: a
    write during validation can leave f's lstat signature unchanged where
    timestamps are coarse (Linux tmpfs), and then it rightly belongs to the
    next batch, which tests nothing here."""
    s = Sim(tmp_path, read_every=0.01)
    s.run(1.0, writes=[1.0], rel="f")
    state = {"armed": False, "capped_once": False}
    real_read = s.backend.read

    def read():
        if state["armed"] and not state["capped_once"]:
            state["capped_once"] = True
            s.backend.capped = True
            return []
        s.backend.capped = False
        return real_read()

    s.backend.read = read
    real_scan = scanmod.Scanner.scan

    def scan(self, *a, **k):
        if not state["armed"] and s.t >= T0 + 1.05:
            s.t = T0 + 1.11
            s.write("f")  # in the content the scan is about to read
            s.t = T0 + 1.12
            state["armed"] = True
        return real_scan(self, *a, **k)

    monkeypatch.setattr(scanmod.Scanner, "scan", scan)
    s.run(1.4)
    monkeypatch.undo()
    b = [r for r in s.recs if r["kind"] == "batch"][0]
    assert state["capped_once"]
    assert replay.state_at(s.w.out, b["batch"]).entries["f"].sha256 == __import__("hashlib").sha256(b"2").hexdigest()
    assert parse(b["last_write"]) >= T0 + 1.11


# ---- 2. changes found without a notification -----------------------------------


def test_change_found_by_validation_ends_at_the_scan(tmp_path, monkeypatch):
    """An open writer appends through an ignored hard-link name at 1.130,
    during validation: the batch that commits the tracked alias's change
    has an upper bound at the scan's end, not its notified write's read."""
    s = Sim(tmp_path, read_every=0.01, ignore=["cache/**"])
    (s.root / "cache").mkdir()
    os.link(s.root / "f", s.root / "cache" / "f")
    s.run(0.99)
    done = {"x": False}
    real_validate = scanmod.Scanner.validate

    def validate(self, res):
        if not done["x"]:
            done["x"] = True
            s.t = T0 + 1.13
            with open(s.root / "cache" / "f", "a") as fh:
                fh.write(" appended")
        return real_validate(self, res)

    monkeypatch.setattr(scanmod.Scanner, "validate", validate)
    s.run(1.5, writes=[1.0], rel="g")
    monkeypatch.undo()
    b = [r for r in s.recs if r["kind"] == "batch" and any(x.get("path") == "f" and x.get("batch") == r["batch"] for x in s.recs if x["kind"] != "batch")][0]
    assert parse(b["last_write"]) >= T0 + 1.13


def test_change_between_validation_and_commit_is_bounded_by_the_previous_scan(tmp_path, monkeypatch):
    """A tracked file changes through an ignored hard-link name at 1.140,
    after the scan's validation and before its commit at 1.160. The next
    batch sweeps it up: its lower bound is no later than the previous
    batch's scan start, so it holds 1.140."""
    from cohbench.watcher import delta

    s = Sim(tmp_path, read_every=0.01, ignore=["cache/**"])
    (s.root / "cache").mkdir()
    os.link(s.root / "f", s.root / "cache" / "f")
    s.run(0.99)
    done = {"x": False}
    real_plan = delta.plan

    def plan(*a, **k):
        if not done["x"]:
            done["x"] = True
            s.t = T0 + 1.14
            with open(s.root / "cache" / "f", "a") as fh:
                fh.write(" silent")
            s.t = T0 + 1.16
        return real_plan(*a, **k)

    monkeypatch.setattr(delta, "plan", plan)
    s.run(1.5, writes=[1.0], rel="g")  # batch 1: g, committed at 1.160
    monkeypatch.undo()
    s.run(3.0, writes=[2.0], rel="h")  # batch 2 sweeps up f
    bs = bounds_of(s.recs)
    fb = [x["batch"] for x in s.recs if x.get("path") == "f" and x["kind"] == "modified"][0]
    assert bs[fb][0] < T0 + 1.14 <= bs[fb][1]


# ---- 3. a run of batches around an instant -------------------------------------


def test_overlapping_bounds_make_a_straddled_run():
    """Batch 1 (0.900, 1.160] and batch 2 (1.120, 1.170]: a mark for 1.140
    is straddled by both."""
    b = {0: (0.5, 0.5), 1: (0.9, 1.16), 2: (1.12, 1.17)}
    assert answer_for(1.14, b) == (0, 1, 2)
    assert answer_for(1.165, b) == (1, 2, 2)
    assert answer_for(1.2, b) == (2, None, None)
    assert answer_for(0.95, b) == (0, 1, 1)
    # A batch entirely before the instant but after a straddler is not named.
    assert answer_for(1.15, {0: (0.5, 0.5), 1: (0.9, 1.16), 2: (1.0, 1.1)}) == (0, 1, 1)


def test_replay_accepts_a_straddled_run(tmp_path):
    from tests.watcher.test_replay import Log

    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"1"))
    L.commit()
    L.lines[-1].update(write_times="none", first_write_after=None, wall="2026-01-01T00:00:00.500000Z")
    for b, (lo, hi) in ((1, ("00:00:00.900000", "00:00:01.160000")), (2, ("00:00:01.120000", "00:00:01.170000"))):
        L.add("created", f"x{b}", L.entry(data=b"x"))
        L.commit()
        L.lines[-1].update(write_times="known", first_write_after=f"2026-01-01T{lo}Z", first_write=f"2026-01-01T{hi}Z", last_write=f"2026-01-01T{hi}Z", wall="2026-01-01T00:00:01.200000Z")
    L.seq += 1
    L.lines.append({"kind": "mark", "label": "m", "accepted": True, "at": "2026-01-01T00:00:01.140000Z", "batch": 0, "straddled": 1, "straddled_last": 2, "seq": L.seq})
    L.write()
    replay.read_events(L.out)


# ---- minors ---------------------------------------------------------------------


def test_offline_refuses_another_isolation(tmp_path):
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete
    from tests.watcher.test_round7_host import _finished_run

    out = _finished_run(tmp_path)
    assert json.loads((out / "config.json").read_text())["metric_isolation"] == "none"
    cfg = json.loads((out / "config.json").read_text())
    cfg["metric_isolation"] = "users"
    (out / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(RuntimeError, match="isolation"):
        complete(str(out), LocalExecutor(str(tmp_path / "w2"), None, "/work/project"))


def test_reuse_key_numbers_are_normalised(tmp_path):
    from cohbench.watcher.manifest import Manifest
    from cohbench.watcher.metrics import tree_digest
    from tests.watcher.test_metrics import slow_runner

    r, m, out, blobs = slow_runner(tmp_path, "print('{}')")
    r.timeout = 7
    k1 = tree_digest(Manifest(), lambda s: True, r.settings())
    r.timeout = 7.0
    assert tree_digest(Manifest(), lambda s: True, r.settings()) == k1


def test_symbolic_refs_to_an_ignored_name_with_a_packed_twin_are_left_out(tmp_path):
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    (g / "refs" / "heads" / "alias").write_text("ref: refs/t3/x\n")
    (g / "packed-refs").write_text("a" * 40 + " refs/t3/x\n")
    (g / "HEAD").write_text("ref: refs/t3/x\n")
    rig.start()
    st = replay.state_at(str(rig.out)).git
    assert st["refs"] == {"refs/heads/main": "b" * 40} and st["head"] is None
    for d, _, fs in os.walk(rig.out):
        for f in fs:
            assert b"refs/t3" not in open(os.path.join(d, f), "rb").read()


# ---- the property ---------------------------------------------------------------


class Busy(Sim):
    """Reads at seeded random intervals that sometimes stop at a random cap
    (delivering only part of the queue), and silent writes (no notification,
    as through an ignored hard link) to files that exist from the start."""

    def __init__(self, tmp_path, seed, silent_files=8, **kw):
        root = tmp_path / "root"
        root.mkdir(parents=True)
        for i in range(silent_files):
            (root / f"s{i}").write_text("start")
        super().__init__(tmp_path, **kw)
        rng = random.Random(seed * 101 + 5)
        caps = [rng.choice([None, None, None, 1, 2, 3]) for _ in range(30000)]
        it = iter(caps)
        q = self.backend

        def read():
            cap = next(it)
            if cap is not None and len(q.q) > cap:
                out, q.q = q.q[:cap], q.q[cap:]
                q.capped = True
                return out
            out, q.q = q.q, []
            q.capped = False
            return out

        q.read = read
        self.silent: dict[str, float] = {}

    def silent_write(self, name):
        (self.root / name).write_text(f"silent at {self.t}")
        self.silent[name] = self.t


SEEDS = int(os.environ.get("WATCHER_MARK_SEEDS", "150"))


@pytest.mark.parametrize("seed", range(SEEDS))
def test_property_every_bound_and_mark_answer_is_true(tmp_path, seed):
    rng = random.Random(seed)
    writes, t = [], 0.3
    while t < 10.0:
        if rng.random() < 0.5:
            writes += [round(t + i * rng.choice([0.01, 0.03, 0.08]), 4) for i in range(rng.randint(1, 6))]
        else:
            writes += [round(t + i * 0.05, 4) for i in range(rng.randint(10, 45))]
        t = round(t + rng.uniform(0.05, 1.5), 3)
    writes = sorted(set(w for w in writes if w < 11.0))
    silent = sorted((round(rng.uniform(0.2, 10.5), 4), f"s{i}") for i in range(rng.randint(0, 8)))
    marks = [(round(at + rng.choice([0.0, rng.uniform(0, 0.2), rng.uniform(0, 2.5)]), 4), at if rng.random() < 0.7 else None, f"k{k}")
             for k, at in enumerate(sorted(round(rng.uniform(0.2, 11.5), 4) for _ in range(rng.randint(3, 12))))]
    gaps = random.Random(seed * 13 + 1)
    intervals = [gaps.choice([0.002, 0.005, 0.02, 0.05, 0.11]) for _ in range(30000)]

    def reader():
        it = iter(intervals)
        return lambda: next(it)

    def run(sim, with_marks):
        todo_s = list(silent)
        ms = [tuple(m) + (None,) for m in sorted(marks, key=lambda m: m[0])] if with_marks else []
        todo_w = list(writes)
        while sim.t - T0 <= 20.0:
            nxt = sim.t + sim.read_every()
            while (todo_w and T0 + todo_w[0] < nxt) or (todo_s and T0 + todo_s[0][0] < nxt):
                tw = todo_w[0] if todo_w else 1e9
                ts = todo_s[0][0] if todo_s else 1e9
                if tw <= ts:
                    sim.t = max(sim.t, T0 + todo_w.pop(0))
                    sim.write(f"w{sim.n + 1}")
                else:
                    at, name = todo_s.pop(0)
                    sim.t = max(sim.t, T0 + at)
                    sim.silent_write(name)
            sim.t = nxt
            sim.cycle(ms)

    plain = Busy(tmp_path / "plain", seed, read_every=reader())
    run(plain, False)
    sim = Busy(tmp_path / "marked", seed, read_every=reader())
    run(sim, True)
    assert plain.batches() == sim.batches(), f"seed {seed}"
    bs = bounds_of(sim.recs)
    where: dict[str, int] = {}
    for x in sim.recs:
        if x["kind"] == "created" or (x["kind"] == "modified" and x["path"] in sim.silent):
            where.setdefault(x["path"], x["batch"])
    true = {p: t for p, t in sim.writes.items()} | sim.silent
    # Every recorded bound holds every write the batch recorded.
    for p, b in where.items():
        lo, hi = bs[b]
        assert lo <= true[p] <= hi + 1e-6, (seed, p, true[p] - T0, b, lo - T0, hi - T0)
    got = {m["label"]: m for m in sim.marks()}
    assert len(got) == len(marks) and all(m["accepted"] for m in got.values()), seed
    # Each mark's answer follows the rule over the batches committed before
    # it (the order of the records).
    seen: dict[int, tuple] = {}
    for r in sim.recs:
        if r["kind"] == "batch":
            seen[r["batch"]] = bs[r["batch"]]
        elif r["kind"] == "mark":
            assert (r["batch"], r["straddled"], r["straddled_last"]) == answer_for(parse(r["at"]), seen), (seed, r)
    # Against the true times, for writes that raised a notification (a
    # change with none is not seen by a mark: a stated limit).
    notified = {p: b for p, b in where.items() if p not in sim.silent}
    for m in got.values():
        at = parse(m["at"])
        b, s1, s2 = m["batch"], m["straddled"], m["straddled_last"]
        top = s2 if s2 is not None else b
        for p, wb in notified.items():
            if wb <= b:
                assert true[p] <= at + 1e-6, (seed, m["label"], p)
            if true[p] <= at:
                assert wb <= b or (s1 is not None and s1 <= wb <= s2), (seed, m["label"], p)
            if wb > top:
                assert true[p] > at - 1e-6, (seed, m["label"], p)
    replay.read_events(sim.w.out)
