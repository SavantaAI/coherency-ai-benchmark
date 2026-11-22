"""The metric runner on its own (any host)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import pytest

from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.manifest import Entry, Manifest
from cohbench.watcher.metrics import LocalExecutor, MetricRunner, clear_dir, expand, parse_metrics, ranges
from cohbench.watcher.records import MetricsLog

PY = sys.executable


@pytest.mark.parametrize(
    "stdout,ok",
    [
        (b'{"a": 1}', True),
        (b'  {"a": 1.5, "b": [1, 2]}\n', True),
        (b"", False),
        (b"[1, 2]", False),
        (b'{"a": 1}{"b": 2}', False),
        (b"not json", False),
        (b"\xff\xfe", False),
        (b'{"score": NaN}', False),
        (b'{"score": Infinity}', False),
        (b'{"score": -Infinity}', False),
        (b'{"score": 1e400}', False),
        (b'{"score": ' + b"9" * 5000 + b"}", False),
    ],
)
def test_strict_parse(stdout, ok):
    obj, err = parse_metrics(stdout)
    assert (obj is not None and err is None) is ok


def test_deep_nesting_never_raises():
    obj, err = parse_metrics(b'{"a": ' + b"[" * 100000 + b"]" * 100000 + b"}")
    assert (obj is None) == (err is not None)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def manifest(blobs: BlobStore) -> Manifest:
    def F(ino, data, mode=0o644, content="stored"):
        if content == "stored":
            blobs.put(sha(data), data)
        return Entry(type="file", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=len(data), sha256=sha(data), content=content)

    D = lambda ino, mode=0o755, content="none": Entry(type="dir", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=None, sha256=None, content=content)
    return Manifest.of({
        "a.txt": F(1, b"alpha\n"),
        "run.sh": F(2, b"#!/bin/sh\n", mode=0o755),
        "big.bin": F(3, b"B" * 10, content="oversize"),
        "d": D(4),
        "d/b.txt": F(5, b"beta\n"),
        "closed": D(6, mode=0o700, content="inaccessible"),
        "ln": Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=7, size=5, sha256=sha(b"a.txt"), content="stored", target="a.txt"),
        "abs": Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=8, size=22, sha256="x" * 64, content="stored", target="/work/project/d/b.txt"),
        "escape": Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=9, size=11, sha256="y" * 64, content="stored", target="/etc/passwd"),
        "up": Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=10, size=5, sha256="z" * 64, content="stored", target="../.."),
    })


LIST = "import json, os; print(json.dumps({'files': sorted(os.path.join(d, f) for d, _, fs in os.walk('.') for f in fs), 'exec': os.access('run.sh', os.X_OK), 'link': os.readlink('ln'), 'abs': open('abs').read(), 'escape': os.path.exists('escape'), 'cwd': os.getcwd()}))"


def make(tmp_path, code, timeout=5.0):
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    blobs = BlobStore(str(out))
    m = manifest(blobs)
    ex = LocalExecutor(str(tmp_path / "work"), blobs, "/work/project")
    r = MetricRunner([PY, "-c", code], timeout, 1 << 20, 64, MetricsLog(str(out)), ex)
    return r, m, out


def records(out):
    return [json.loads(line) for line in (out / "metrics.jsonl").read_text().splitlines()]


def test_runs_on_a_copy_of_the_batch(tmp_path):
    r, m, out = make(tmp_path, LIST)
    rec = r.run_once(3, m, "change")
    assert rec["ok"] and rec["batch"] == 3
    got = rec["metrics"]
    assert sorted(got["files"]) == ["./a.txt", "./abs", "./d/b.txt", "./escape", "./ln", "./run.sh", "./up"]
    assert got["exec"] and got["link"] == "a.txt"
    assert got["abs"] == "beta\n"  # an absolute link into the workspace lands in the copy
    assert got["escape"] is False  # a link out of the workspace is dangling
    assert set(rec["links_outside"]) == {"escape", "up"}
    assert set(rec["absent"]) == {"big.bin", "closed"} and rec["absent_count"] == 2
    # M2: a readable tracked file left out for size makes the reading
    # incomplete; an inaccessible directory does not.
    assert rec["complete"] is False and rec["lacking"] == ["big.bin"]
    # The copy is emptied afterwards; the mirror stays.
    assert sorted(os.listdir(tmp_path / "work")) == ["mirror", "tree"] and os.listdir(tmp_path / "work" / "tree") == []


def test_command_writing_into_its_copy_does_not_leak(tmp_path):
    r, m, out = make(tmp_path, "import json, os; open('junk', 'w').write('x'); os.remove('a.txt'); print(json.dumps({'n': len(os.listdir('.'))}))")
    first = r.run_once(3, m, "change")
    second = r.run_once(3, m, "change")
    assert first["metrics"] == second["metrics"]


def test_failure_timeout_and_internal_errors_are_readings(tmp_path):
    r, m, out = make(tmp_path, "import sys; sys.stderr.write('E'*500); sys.exit(3)")
    rec = r.run_once(3, m, "change")
    assert not rec["ok"] and rec["error"] == "exit status 3" and rec["stderr_truncated"]
    r2, m2, out2 = make(tmp_path / "t", "import time; time.sleep(30)", timeout=0.5)
    t0 = time.monotonic()
    rec = r2.run_once(3, m2, "change")
    assert rec["timed_out"] and time.monotonic() - t0 < 5
    r3, m3, out3 = make(tmp_path / "i", "print('{}')")
    r3.executor.mat.update = lambda m, g: (_ for _ in ()).throw(RuntimeError("disk full"))
    rec = r3.run_once(3, m3, "change")
    assert not rec["ok"] and rec["error"].startswith("internal: RuntimeError")
    assert records(out3)[-1]["error"].startswith("internal")


def test_invalid_numbers_fail_the_reading(tmp_path):
    r, m, out = make(tmp_path, "print('{\"score\": NaN}')")
    rec = r.run_once(3, m, "change")
    assert not rec["ok"] and rec["exit"] == 0 and rec["metrics"] is None


def slow_runner(tmp_path, code="import time; time.sleep(0.3); print('{}')"):
    out = tmp_path / "out"
    out.mkdir()
    blobs = BlobStore(str(out))
    m = manifest(blobs)
    r = MetricRunner([PY, "-c", code], 5, 1 << 20, 64, MetricsLog(str(out)), LocalExecutor(str(tmp_path / "w"), blobs, "/work/project"), blobs)
    return r, m, out, blobs


def variant(m, blobs, n):
    """m with a.txt holding n: a different tree per n."""
    data = f"alpha {n}\n".encode()
    blobs.put(sha(data), data)
    e = m.entries["a.txt"]
    return Manifest.of({**m.entries, "a.txt": Entry(**{**e.__dict__, "sha256": sha(data), "size": len(data)})})


def test_every_committed_batch_is_measured_in_order(tmp_path):
    """M3: batches committed while a slow reading runs are all measured,
    each from its own manifest, in batch order."""
    r, m, out, blobs = slow_runner(tmp_path, "import hashlib, json, time; time.sleep(0.3); print(json.dumps({'a': open('a.txt').read()}))")
    r.enqueue(0, variant(m, blobs, 0))
    r.finish_one()
    r.start()
    for b in (1, 2, 3, 4):
        r.enqueue(b, variant(m, blobs, b))  # all while batch 1 is running
    assert r.finish(30) == []
    recs = records(out)
    assert [x["batch"] for x in recs] == [0, 1, 2, 3, 4]
    assert [x["metrics"]["a"] for x in recs] == [f"alpha {b}\n" for b in range(5)]
    assert all(x["measured"] and x["reused_from"] is None for x in recs) and "skipped" not in recs[1]
    starts, ends = [x["start"] for x in recs], [x["end"] for x in recs]
    assert all(starts[i + 1] >= ends[i] for i in range(len(recs) - 1)), "runs overlapped"


def test_identical_tree_reuses_the_reading(tmp_path):
    r, m, out, blobs = slow_runner(tmp_path, "print('{\"n\": 1}')")
    r.enqueue(0, m)
    r.enqueue(1, variant(m, blobs, 1))
    # Batch 2 changes only what a copy does not hold (owner, inode).
    e = m.entries["d/b.txt"]
    r.enqueue(2, Manifest.of({**m.entries, "d/b.txt": Entry(**{**e.__dict__, "uid": 1234, "ino": 999})}))
    r.finish(30)
    recs = records(out)
    assert [(x["batch"], x["run"], x["reused_from"]) for x in recs] == [(0, 1, None), (1, 2, None), (2, None, 0)]
    assert recs[2]["metrics"] == recs[0]["metrics"] and recs[2]["tree_sha256"] == recs[0]["tree_sha256"] and recs[2]["reused_run"] == 1
    assert recs[2]["complete"] == recs[0]["complete"] and recs[2]["measured"]


def test_backlog_at_shutdown_is_recorded_and_completed_offline(tmp_path):
    r, m, out, blobs = slow_runner(tmp_path, "import json, time; time.sleep(0.4); print(json.dumps({'a': open('a.txt').read()}))")
    r.enqueue(0, variant(m, blobs, 0))
    r.finish_one()
    r.start()
    for b in (1, 2, 3):
        r.enqueue(b, variant(m, blobs, b))
    time.sleep(0.1)
    left = r.finish(0.0)  # no time to drain: the run in progress finishes, the rest is backlog
    recs = records(out)
    assert left == [2, 3]
    assert [(x["batch"], x["reason"]) for x in recs] == [(0, "baseline"), (1, "change"), (2, "backlog"), (3, "backlog")]
    assert all(x["measured"] is False and x["metrics"] is None for x in recs[2:])


def test_reading_by_batch(tmp_path):
    r, m, out, blobs = slow_runner(tmp_path, "print('{}')")
    r.enqueue(0, m)
    assert r.reading(0) is None  # queued, not yet read
    r.finish_one()
    r.start()
    r.enqueue(1, variant(m, blobs, 1))
    r.finish(30)
    assert [r.reading(b)["batch"] for b in (0, 1)] == [0, 1] and r.reading(1)["logged"] and r.reading(2) is None


def test_batches_must_be_queued_in_order(tmp_path):
    r, m, out, blobs = slow_runner(tmp_path, "print('{}')")
    r.enqueue(0, m)
    with pytest.raises(RuntimeError):
        r.enqueue(2, m)


def test_omissions_are_listed_on_every_reading(tmp_path):
    r, m, out = make(tmp_path, "print('{}')")
    first = r.run_once(3, m, "change")
    second = r.run_once(4, m, "change")
    third = r.measure(5, m)
    assert first["absent"] == second["absent"] == third["absent"] and "closed" in third["absent"]


def test_git_state_is_flagged_absent(tmp_path):
    r, m, out = make(tmp_path, "print('{}')")
    m.git = {"head": "ref: refs/heads/main", "refs": {}, "backend": "files"}
    assert ".git" in r.run_once(3, m, "change")["absent"]


def test_unwritable_reading_is_not_ok(tmp_path):
    r, m, out = make(tmp_path, "print('{}')")
    r.log.write = lambda recs: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    rec = r.run_once(3, m, "change")
    assert rec["logged"] is False and rec["ok"] is False and rec["measured"] is False


def test_cleanup_never_follows_links_out_of_the_copy(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    (outside / "keep").write_text("k")
    copy = tmp_path / "copy"
    (copy / "d").mkdir(parents=True)
    os.symlink(str(outside), copy / "link_to_dir")
    os.symlink(str(outside / "keep"), copy / "d" / "link_to_file")
    os.chmod(copy / "d", 0o000)
    clear_dir(str(copy))
    assert os.listdir(copy) == []
    assert stat_mode(outside) == 0o755 and (outside / "keep").read_text() == "k"


def stat_mode(p):
    import stat

    return stat.S_IMODE(os.stat(p).st_mode)


def test_ranges_are_complete_at_any_length():
    nums = list(range(5, 3000)) + [4000, 4001, 4003]
    rs = ranges(nums)
    assert rs == [[5, 2999], [4000, 4001], [4003, 4003]] and expand(rs) == nums
