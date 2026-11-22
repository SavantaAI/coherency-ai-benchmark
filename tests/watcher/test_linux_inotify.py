"""The watcher against real inotify. Linux only; run with run_linux.sh.

Every test that runs a watcher ends with `env.finish()`, which stops it
(final scan and batch) and checks that state_at(last) equals an independent
walk of the tree; the fixture fails a test that skipped it."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cohbench.watcher import replay, state_at
from cohbench.watcher.config import Config
from cohbench.watcher.records import HEARTBEAT
from cohbench.watcher.watcher import Watcher
from tests.watcher.helpers import assert_state_matches, batch_records, git_skip, kinds, records, settle, wait_metrics_idle

pytestmark = pytest.mark.linux_inotify

REPO = Path(__file__).resolve().parents[2]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.out = tmp_path / "out"
        self.outside = tmp_path / "outside"
        self.work = tmp_path / "work"
        for d in (self.root, self.outside):
            d.mkdir()
        self.w: Watcher | None = None
        self.checked = False
        self.skip = git_skip

    def start(self, **cfg) -> Watcher:
        cfg.setdefault("quiet_ms", 100)
        cfg.setdefault("heartbeat_s", 0.2)
        cfg.setdefault("work_dir", str(self.work))
        self.w = Watcher(Config(root=str(self.root), out=str(self.out), **cfg))
        self.w.start()
        return self.w

    def settle(self, quiet: float = 0.35) -> None:
        settle(self.w, quiet=quiet)
        assert_state_matches(str(self.out), str(self.root), skip=self.skip)

    def sh(self, script: str) -> None:
        subprocess.run(["sh", "-ec", script], cwd=self.root, check=True)

    def finish(self, skip=None) -> list[dict]:
        self.w.stop()
        self.w.shutdown()
        assert self.w.status == "stopped", self.w.error
        assert_state_matches(str(self.out), str(self.root), skip=skip or self.skip)
        for b in batch_records(str(self.out)):
            assert b["check"] == "ok", b
        self.checked = True
        return records(str(self.out))

    def metrics(self) -> list[dict]:
        with open(self.out / "metrics.jsonl") as f:
            return [json.loads(line) for line in f]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.w is not None and not e.checked:
        try:
            e.w.stop()
            e.w.shutdown()
        except Exception:
            pass
        pytest.fail("test did not stop the watcher and check the final state")


def test_inotify_and_openat2_are_real(env):
    w = env.start()
    assert w.core.root.method == "openat2"
    env.finish()


def test_create_modify_delete(env):
    env.start()
    (env.root / "a.txt").write_text("one\n")
    env.settle()
    with open(env.root / "a.txt", "a") as f:
        f.write("two\n")
    env.settle()
    (env.root / "a.txt").unlink()
    ev = env.finish()
    assert kinds(ev) == [("created", "a.txt"), ("modified", "a.txt"), ("deleted", "a.txt")]
    assert [e["batch"] for e in ev] == [1, 2, 3]
    assert ev[1]["sha256"] == sha(b"one\ntwo\n") and ev[1]["prev_sha256"] == sha(b"one\n")


def test_move_within_tree(env):
    (env.root / "d").mkdir()
    (env.root / "d" / "f").write_text("f")
    env.start()
    os.rename(env.root / "d" / "f", env.root / "d" / "g")
    env.settle()
    os.rename(env.root / "d", env.root / "e")
    env.settle()
    (env.root / "e" / "new").write_text("n")
    ev = env.finish()
    assert kinds(ev) == [("moved", "d/f", "d/g"), ("moved", "d", "e"), ("created", "e/new")]


def test_move_in_from_outside_and_out(env):
    (env.root / "leaves").write_text("bye")
    (env.root / "dir_leaves").mkdir()
    (env.root / "dir_leaves" / "x").write_text("x")
    (env.outside / "arrives").write_text("hi")
    (env.outside / "dir_arrives" / "sub").mkdir(parents=True)
    (env.outside / "dir_arrives" / "sub" / "y").write_text("y")
    env.start()
    os.rename(env.outside / "arrives", env.root / "arrives")
    os.rename(env.root / "leaves", env.outside / "leaves")
    os.rename(env.outside / "dir_arrives", env.root / "dir_arrives")
    os.rename(env.root / "dir_leaves", env.outside / "dir_leaves")
    env.settle()
    (env.outside / "dir_leaves" / "later").write_text("z")  # no longer in the tree
    (env.root / "dir_arrives" / "sub" / "later").write_text("w")
    ev = env.finish()
    assert set(kinds(ev)) == {
        ("created", "arrives"), ("deleted", "leaves"), ("created", "dir_arrives"), ("created", "dir_arrives/sub"),
        ("created", "dir_arrives/sub/y"), ("deleted", "dir_leaves/x"), ("deleted", "dir_leaves"), ("created", "dir_arrives/sub/later"),
    }


def test_new_directory_with_files_written_immediately(env):
    env.start()
    env.sh("\n".join(f"mkdir -p n{i}/a/b && echo {i} > n{i}/a/b/c && echo {i} > n{i}/a/x && mkdir n{i}/z && echo {i} > n{i}/z/y" for i in range(30)))
    ev = env.finish()
    created = {e["path"] for e in ev if e["kind"] == "created"}
    for i in range(30):
        assert {f"n{i}", f"n{i}/a", f"n{i}/a/b", f"n{i}/a/b/c", f"n{i}/a/x", f"n{i}/z", f"n{i}/z/y"} <= created


def test_rename_over_existing_is_a_modification(env):
    (env.root / "x").write_text("old\n")
    (env.root / "s").write_text("alpha\n")
    env.start()
    (env.root / "x.tmp").write_text("new\n")
    os.replace(env.root / "x.tmp", env.root / "x")
    env.settle()
    env.sh("sed -i 's/alpha/beta/' s")
    ev = env.finish()
    assert kinds(ev) == [("modified", "x"), ("modified", "s")]
    assert ev[0]["via_rename"] == "x.tmp" and ev[0]["prev_sha256"] == sha(b"old\n")
    assert ev[1]["sha256"] == sha(b"beta\n") and ev[1]["via_rename"].startswith("sed")
    b = batch_records(str(env.out))
    assert b[1]["renames"] == [["x.tmp", "x"]]


def test_truncate_and_rewrite_is_one_modification(env):
    (env.root / "f").write_text("original\n")
    env.start()
    with open(env.root / "f", "w") as fh:
        fh.flush()
        time.sleep(0.03)
        fh.write("rewritten ")
        fh.flush()
        time.sleep(0.03)
        fh.write("contents\n")
    env.settle()
    ev = env.finish()
    assert kinds(ev) == [("modified", "f")] and ev[0]["sha256"] == sha(b"rewritten contents\n")


def test_slow_writer_held_open_is_one_modification(env):
    (env.root / "f").write_text("before\n")
    env.start()
    with open(env.root / "f", "w") as fh:
        for i in range(5):
            fh.write(f"line {i}\n")
            fh.flush()
            for _ in range(10):  # 250 ms gaps, longer than the quiet period
                env.w.step(max_wait=0.025)
    ev = env.finish()
    assert kinds(ev) == [("modified", "f")]


def test_identical_rewrites(env):
    (env.root / "f").write_text("same\n")
    env.start()
    for _ in range(3):  # three identical writes inside one settling window
        (env.root / "f").write_text("same\n")
    env.settle()
    env.sh(": >> f")  # open for append and close without writing
    ev = env.finish()
    assert kinds(ev) == [("rewritten", "f")]


def test_burst_of_1000_small_files(env):
    env.start()
    (env.root / "burst").mkdir()
    for i in range(1000):
        (env.root / "burst" / f"f{i:04d}.txt").write_text(f"{i}\n")
    ev = env.finish()
    created = [e for e in ev if e["kind"] == "created" and e["path"].startswith("burst/")]
    assert len(created) == 1000


def test_six_mib_file(env):
    env.start()
    data = os.urandom(6 * 1024 * 1024)
    (env.root / "big.bin").write_bytes(data)
    env.settle()
    with open(env.root / "big.bin", "r+b") as f:
        f.seek(100)
        f.write(b"changed")
    ev = env.finish()
    assert kinds(ev) == [("created", "big.bin"), ("modified", "big.bin")]
    assert ev[0]["content"] == "oversize" and ev[0]["size"] == len(data) and ev[0]["sha256"] == sha(data)
    assert not os.path.exists(env.w.blobs.path(ev[0]["sha256"]))


def test_mode_600_file_owned_by_uid_1000(env):
    assert os.getuid() == 1000, "run these tests as uid 1000 (run_linux.sh does)"
    env.start()
    fd = os.open(env.root / "secret.txt", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"private\n")
    os.close(fd)
    (env.root / "closed").mkdir(mode=0o700)
    fd = os.open(env.root / "closed" / "inner", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"inner\n")
    os.close(fd)
    ev = env.finish()
    by = {e["path"]: e for e in ev}
    assert by["secret.txt"]["mode"] == "0600" and by["secret.txt"]["uid"] == 1000 and by["secret.txt"]["sha256"] == sha(b"private\n")
    assert by["closed"]["mode"] == "0700" and by["closed/inner"]["sha256"] == sha(b"inner\n")


def test_symlinks_are_recorded_and_never_followed(env):
    (env.outside / "target_dir").mkdir()
    (env.outside / "target_file").write_text("t")
    (env.root / "inside").mkdir()
    env.start()
    os.symlink(str(env.outside / "target_file"), env.root / "to_file")
    os.symlink(str(env.outside / "target_dir"), env.root / "to_dir")
    os.symlink("inside", env.root / "to_inside")
    os.symlink("missing", env.root / "dangling")
    env.settle()
    (env.outside / "target_dir" / "new").write_text("n")
    (env.outside / "target_file").write_text("changed")
    ev = env.finish()
    assert {e["path"] for e in ev} == {"to_file", "to_dir", "to_inside", "dangling"}
    assert all(e["type"] == "symlink" for e in ev)


def test_hard_links_write_and_chmod(env):
    (env.root / "a").write_text("1\n")
    env.start()
    os.link(env.root / "a", env.root / "b")
    env.settle()
    with open(env.root / "a", "r+") as f:
        f.write("2")
    env.settle()
    os.chmod(env.root / "a", 0o600)
    ev = env.finish()
    assert kinds(ev[:1]) == [("created", "b")]
    assert sorted(kinds(ev[1:3])) == [("modified", "a"), ("modified", "b")]
    assert sorted(kinds(ev[3:])) == [("attrib", "a"), ("attrib", "b")]
    st = state_at(str(env.out))
    assert st.entries["a"].mode == st.entries["b"].mode == 0o600 and st.links() == [["a", "b"]]


def test_deleted_and_recreated_directory(env):
    (env.root / "d").mkdir()
    (env.root / "d" / "x").write_text("x")
    env.start()
    env.sh("rm -rf d && mkdir d && echo y > d/y")
    env.settle()
    (env.root / "d" / "z").write_text("z")  # needs the new directory's watch
    ev = env.finish()
    assert kinds(ev) == [("deleted", "d/x"), ("deleted", "d"), ("created", "d"), ("created", "d/y"), ("created", "d/z")]


def test_attrib_mode_and_type_changes(env):
    (env.root / "f").write_text("f")
    (env.root / "g").write_text("g")
    (env.root / "h").mkdir()
    (env.root / "h" / "in").write_text("i")
    env.start()
    os.chmod(env.root / "f", 0o755)
    env.settle()
    os.unlink(env.root / "g")
    os.symlink("f", env.root / "g")
    env.settle()
    shutil.rmtree(env.root / "h")
    (env.root / "h").write_text("now a file")
    ev = env.finish()
    assert (ev[0]["kind"], ev[0]["path"], ev[0]["changed"]) == ("attrib", "f", ["mode"])
    assert (ev[1]["kind"], ev[1]["path"]) == ("attrib", "g") and ev[1]["changed"][0] == "type"
    assert kinds(ev[2:]) == [("deleted", "h/in"), ("deleted", "h"), ("created", "h")]


def test_ignore_rules_on_real_inotify(env):
    env.skip = lambda r: r.startswith("node_modules") or r.endswith(".log")
    env.start(ignore=["node_modules/**", "**/*.log"])
    env.sh("mkdir -p node_modules/pkg src && echo m > node_modules/pkg/index.js && echo l > src/x.log && echo c > src/main.c")
    ev = env.finish()
    assert {e["path"] for e in ev} == {"src", "src/main.c"}


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@localhost", *args], cwd=root, check=True, capture_output=True)


def test_git_coarse(env):
    env.start(ignore=[".git/**"])
    _git(env.root, "init", "-q", "-b", "main")
    (env.root / "README").write_text("r\n")
    _git(env.root, "add", "README")
    _git(env.root, "commit", "-qm", "one")
    env.settle()
    _git(env.root, "checkout", "-qb", "feature")
    (env.root / "README").write_text("r2\n")
    _git(env.root, "commit", "-qam", "two")
    ev = env.finish()
    assert not [e for e in ev if e["path"].startswith(".git/")]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.root, capture_output=True, text=True, check=True).stdout.strip()
    g = state_at(str(env.out)).git
    assert g["head"] == "ref: refs/heads/feature" and g["refs"]["refs/heads/feature"] == head and g["backend"] == "files"
    assert kinds([e for e in ev if e["kind"] != "git"]) == [("created", "README"), ("modified", "README")]


def test_inaccessible_directory_is_recorded_and_rescanned(env):
    env.start()
    env.sh("mkdir d && echo x > d/f && chmod 000 d")
    env.settle()
    st = state_at(str(env.out))
    assert st.entries["d"].content == "inaccessible" and "d/f" not in st.entries
    os.chmod(env.root / "d", 0o755)
    env.settle()
    (env.root / "d" / "g").write_text("g")
    ev = env.finish()
    assert ("created", "d/f") in kinds(ev) and ("created", "d/g") in kinds(ev)


METRIC_COUNT = "import json, os; n = sum(len(f) for _, _, f in os.walk('.')); print(json.dumps({'files': n}))"
METRIC_HASH = "import hashlib, json; print(json.dumps({'f': hashlib.sha256(open('f', 'rb').read()).hexdigest()}))"


def test_metric_readings_name_their_batch_and_match_it(env):
    (env.root / "f").write_text("zero\n")
    env.start(metric_cmd=[sys.executable, "-c", METRIC_HASH])
    m0 = env.metrics()
    assert [x["reason"] for x in m0] == ["baseline"] and m0[0]["batch"] == 0  # done before running
    for i in range(1, 4):
        (env.root / "f").write_text(f"{i}\n")
        env.settle()
        wait_metrics_idle(env.w)
    env.finish()
    for x in env.metrics():
        assert x["ok"], x
        assert x["metrics"]["f"] == state_at(str(env.out), x["batch"]).entries["f"].sha256


def test_metric_hook_never_touches_the_live_tree(env):
    env.start(metric_cmd=[sys.executable, "-c", "import json, os; open('x', 'w').write('1'); print(json.dumps({'cwd': os.getcwd()}))"])
    (env.root / "a").write_text("a")
    env.settle()
    wait_metrics_idle(env.w)
    env.finish()
    assert not (env.root / "x").exists()
    assert all(x["ok"] and not x["metrics"]["cwd"].startswith(str(env.root)) for x in env.metrics())


def test_metric_hook_failure_timeout_invalid(env):
    env.start(metric_cmd=[sys.executable, "-c", "import sys; print('{\"score\": NaN}'); sys.stderr.write('boom'); sys.exit(4)"], metric_timeout_s=5)
    env.finish()
    assert all(not x["ok"] and x["exit"] == 4 and x["stderr"] == "boom" and x["parse_error"] for x in env.metrics())


def test_metric_hook_timeout(env):
    env.start(metric_cmd=[sys.executable, "-c", "import time; time.sleep(60)"], metric_timeout_s=0.5)
    env.finish()
    m = env.metrics()
    # No change after the baseline: batch 0 is the only batch, so its reading
    # is the only one (a timed-out reading is never reused).
    assert [x["reason"] for x in m] == ["baseline"] and all(x["timed_out"] and x["measured"] for x in m)


SLOW_COUNT = "import json, os, time; time.sleep(1.0); print(json.dumps({'files': sum(len(f) for _, _, f in os.walk('.'))}))"


def test_slow_metric_measures_every_batch_in_order(env):
    """M3: batches committed while a reading runs are queued, not skipped."""
    env.start(metric_cmd=[sys.executable, "-c", SLOW_COUNT])
    for i in range(4):
        (env.root / f"f{i}").write_text("d")
        env.settle(quiet=0.15)
    env.finish()
    m = env.metrics()
    assert [x["batch"] for x in m] == list(range(0, 5)) and all(x["measured"] and x["complete"] for x in m)
    for x in m:
        assert x["metrics"]["files"] == len(state_at(str(env.out), x["batch"]).entries)


def test_backlog_at_shutdown_is_completed_offline_with_the_same_values(env):
    from cohbench.watcher.metrics import LocalExecutor
    from cohbench.watcher.offline import complete

    env.start(metric_cmd=[sys.executable, "-c", SLOW_COUNT], metric_drain_s=0)
    # Completion uses the executor the live run used: here the local one,
    # same workspace path and copy mtime.
    same = lambda d: LocalExecutor(str(env.work / d), None, env.w.cfg.workspace_path, copy_mtime=env.w.cfg.metric_copy_mtime)
    for i in range(4):
        (env.root / f"f{i}").write_text("d")
        env.settle(quiet=0.15)
    env.finish()
    m = env.metrics()
    backlog = [x["batch"] for x in m if x["reason"] == "backlog"]
    assert backlog and all(x["measured"] is False for x in m if x["reason"] == "backlog")
    assert sorted(x["batch"] for x in m) == list(range(0, 5))
    res = complete(str(env.out), same("offline"))
    assert sorted(res["measured"] + res["reused"]) == backlog
    after = env.metrics()
    for x in after:
        if x.get("measured"):
            assert x["metrics"]["files"] == len(state_at(str(env.out), x["batch"]).entries)
    measured = {x["batch"] for x in after if x.get("measured")}
    assert measured == set(range(0, 5)) and all(x["offline"] for x in after if x["batch"] in backlog and x.get("measured"))
    assert complete(str(env.out), same("offline2")) == {"measured": [], "reused": [], "already": 5}
    runs = [x["run"] for x in after if x.get("run") is not None]
    assert runs == sorted(set(runs))  # completion continues the run numbers


def test_no_metric_command_records_no_metrics(env):
    env.start()
    (env.root / "a").write_text("a")
    env.finish()
    assert not (env.out / "metrics.jsonl").exists()


def test_marks_wait_for_the_batch_and_are_acknowledged(env):
    """D-037: the mark does not close the open batch; it is answered once
    the watcher's own quiet period has closed it."""
    env.start(metric_cmd=[sys.executable, "-c", METRIC_COUNT], quiet_ms=1500, max_delay_ms=4000)
    (env.root / "before").write_text("b")
    t_write = time.time()
    rid = "0001"
    (env.out / "control" / f"{rid}.request.json").write_text(json.dumps({"label": "before-correction"}))
    deadline = time.monotonic() + 20
    ack = env.out / "control" / f"{rid}.ack.json"
    while not ack.exists() and time.monotonic() < deadline:
        env.w.step(max_wait=0.05)
    a = json.loads(ack.read_text())
    assert a["ok"] and a["accepted"] and a["label"] == "before-correction" and a["straddled"] is None
    assert a["metric"] is None or a["metric"]["batch"] == a["batch"]  # only if already written
    b = [r for r in batch_records(str(env.out)) if r["batch"] == a["batch"]][0]
    assert b["reason"] == "settled", b  # closed by its quiet period, not by the mark
    from cohbench.watcher.records import parse_utc

    assert parse_utc(a["covered_utc"]) >= t_write + 1.5 - 0.05
    st = state_at(str(env.out), a["batch"])
    assert "before" in st.entries
    assert replay.mark_batch(str(env.out), "before-correction") == a["batch"]
    (env.root / "after").write_text("a")
    ev = env.finish()
    assert "after" not in st.entries and ("created", "after") in kinds(ev)
    assert [x["ok"] for x in env.metrics() if x["batch"] == a["batch"] and x.get("measured")] == [True]


def test_mark_cli_against_a_running_watcher(tmp_path):
    root, out = tmp_path / "root", tmp_path / "out"
    root.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "cohbench.watcher", "run", "--root", str(root), "--out", str(out), "--quiet-ms", "3000", "--max-delay-ms", "6000", "--heartbeat-s", "0.1"], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait_running(out)
        (root / "x").write_text("x")
        r = subprocess.run([sys.executable, "-m", "cohbench.watcher", "mark", str(out), "turn-1"], cwd=REPO, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        a = json.loads(r.stdout)
        assert "x" in state_at(str(out), a["batch"]).entries
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    assert_state_matches(str(out), str(root))


def test_overflow_recovery(env):
    for name in ("keep", "change", "drop", "t1", "t2"):
        (env.root / name).write_text(name)
    env.start()
    max_q = int(Path("/proc/sys/fs/inotify/max_queued_events").read_text())
    (env.root / "change").write_text("changed")
    (env.root / "drop").unlink()
    (env.root / "newdir").mkdir()
    (env.root / "newdir" / "f").write_text("f")
    for i in range(max_q + 100):
        os.utime(env.root / ("t1" if i % 2 else "t2"))
    ev = env.finish()
    b = [x for x in batch_records(str(env.out)) if x["reason"] == "overflow"]
    assert len(b) == 1 and b[0]["recovered"]
    assert {(e["kind"], e["path"]) for e in ev if e["batch"] == b[0]["batch"]} == {("modified", "change"), ("deleted", "drop"), ("created", "newdir"), ("created", "newdir/f")}


def test_heartbeat_alive_and_failure(env):
    env.start()
    hb = env.out / HEARTBEAT
    first = json.loads(hb.read_text())
    assert first["status"] == "running" and first["batch"] == 0
    alive = subprocess.run([sys.executable, "-m", "cohbench.watcher", "alive", str(env.out)], cwd=REPO, capture_output=True, text=True)
    assert alive.returncode == 0, alive.stdout + alive.stderr
    env.finish()
    assert json.loads(hb.read_text())["status"] == "stopped"


def test_unexpected_exception_makes_the_heartbeat_say_failed(tmp_path):
    root, out = tmp_path / "root", tmp_path / "out"
    root.mkdir()
    import threading

    w = Watcher(Config(root=str(root), out=str(out), quiet_ms=50, heartbeat_s=0.1))
    real_start = w.start

    def start_then_break():
        real_start()

        def boom(*a, **k):
            raise RuntimeError("injected")

        w.core.tick = boom
        (root / "a").write_text("a")

    w.start = start_then_break
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", w.run()))
    t.start()
    t.join(timeout=20)
    assert not t.is_alive()
    code = result["code"]
    hb = json.loads((out / HEARTBEAT).read_text())
    assert code == 1 and hb["status"] == "failed" and "injected" in hb["error"]


def _wait_running(out: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if json.loads((out / HEARTBEAT).read_text())["status"] == "running":
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("watcher did not start")


def test_sigterm_final_scan_and_final_reading(tmp_path):
    root, out = tmp_path / "root", tmp_path / "out"
    root.mkdir()
    cmd = [sys.executable, "-m", "cohbench.watcher", "run", "--root", str(root), "--out", str(out), "--quiet-ms", "1000", "--heartbeat-s", "0.1", "--metric-cmd", json.dumps([sys.executable, "-c", METRIC_COUNT]), "--work-dir", str(tmp_path / "w")]
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait_running(out)
        for i in range(5):
            (root / f"late{i}").write_text(str(i))
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert code == 0, proc.stderr.read().decode()
    assert set(state_at(str(out)).entries) == {f"late{i}" for i in range(5)}
    m = [json.loads(line) for line in (out / "metrics.jsonl").read_text().splitlines()]
    last = state_at(str(out)).batch
    # The final batch's reading is the last one, like any batch's.
    assert m[-1]["measured"] and m[-1]["batch"] == last and m[-1]["metrics"]["files"] == 5
    assert [x["batch"] for x in m] == list(range(last + 1))
    hb = json.loads((out / HEARTBEAT).read_text())
    assert hb["status"] == "stopped" and hb["still_changing"] is False
    assert_state_matches(str(out), str(root))


def test_write_racing_shutdown_is_reported_not_merged(env):
    (env.root / "f").write_text("v1\n")
    env.start(metric_cmd=[sys.executable, "-c", METRIC_HASH])
    real_finish = env.w.metrics.finish

    def finish_after_a_late_write(drain_s):
        (env.root / "f").write_text("v2-late\n")  # after the final scan
        return real_finish(drain_s)

    env.w.metrics.finish = finish_after_a_late_write
    env.w.stop()
    env.w.shutdown()
    hb = json.loads((env.out / HEARTBEAT).read_text())
    assert hb["status"] == "stopped" and hb["still_changing"] is True and hb["late_events"] >= 1
    final = env.metrics()[-1]
    assert final["measured"] and final["metrics"]["f"] == state_at(str(env.out), final["batch"]).entries["f"].sha256 == sha(b"v1\n")
    env.checked = True


def test_output_inside_root_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Watcher(Config(root=str(tmp_path), out=str(tmp_path / "out")))


def test_reused_output_directory_is_refused(env):
    env.start()
    env.finish()
    with pytest.raises(FileExistsError):
        Watcher(Config(root=str(env.root), out=str(env.out)))


def test_write_through_a_hard_link_in_an_ignored_directory_reaches_the_next_batch(env):
    """D-032 (B4): inotify reports nothing for the tracked name, so the write
    is recorded by the next batch's sweep (here started by another edit; a
    mark does not scan)."""
    (env.root / "cache").mkdir()
    (env.root / "f").write_text("old\n")
    os.link(env.root / "f", env.root / "cache" / "f")
    env.start(ignore=["cache/**"], metric_cmd=[sys.executable, "-c", METRIC_HASH], quiet_ms=200, max_delay_ms=3000)
    with open(env.root / "cache" / "f", "r+") as f:
        f.write("new, longer\n")
    a = env.w.mark("quiet")  # names the baseline: no batch was open, and it scans nothing
    assert a["accepted"] and a["batch"] == 0 and state_at(str(env.out), 0).entries["f"].sha256 == sha(b"old\n")
    (env.root / "other").write_text("o")
    settle(env.w)
    b = state_at(str(env.out)).batch
    assert b >= 1 and state_at(str(env.out), b).entries["f"].sha256 == sha(b"new, longer\n")
    env.finish(skip=lambda r: git_skip(r) or r == "cache" or r.startswith("cache/"))
    reading = [x for x in env.metrics() if x["batch"] == b and x.get("measured")][0]
    assert reading["ok"] and reading["metrics"]["f"] == sha(b"new, longer\n")
