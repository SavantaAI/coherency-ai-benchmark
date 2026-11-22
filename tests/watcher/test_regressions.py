"""Named regressions from the 2026-11-22 review, against real inotify.

Each sequence runs as one burst, so it lands in one batch (or a few), then
the watcher settles and the log must replay to an independent walk, every
batch's self-check must have passed, and the specific records are checked."""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from cohbench.watcher import state_at
from tests.watcher.helpers import batch_records, kinds, records
from tests.watcher.test_linux_inotify import Env

pytestmark = pytest.mark.linux_inotify


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


def burst(env, script):
    subprocess.run(["sh", "-ec", script], cwd=env.root, check=True)
    env.settle()


def last_batch_kinds(env):
    b = batch_records(str(env.out))[-1]["batch"]
    return kinds(records(str(env.out), batch=b))


def test_rm_rf_dst_then_mv_src_dst(env):
    burst_setup = "mkdir -p src dst && echo c > src/c && echo child > dst/child"
    subprocess.run(["sh", "-ec", burst_setup], cwd=env.root, check=True)
    env.start()
    burst(env, "rm -rf dst && mv src dst")
    assert last_batch_kinds(env) == [("deleted", "dst/child"), ("deleted", "dst"), ("moved", "src", "dst")]
    env.finish()


def test_write_in_subdir_then_mv_parent(env):
    subprocess.run(["sh", "-ec", "mkdir -p d/sub"], cwd=env.root, check=True)
    env.start()
    burst(env, "echo hello > d/sub/new.txt && mv d e")
    assert last_batch_kinds(env) == [("moved", "d", "e"), ("created", "e/sub/new.txt")]
    env.finish()


def test_rm_rf_mkdir_mv_into_recreated_directory(env):
    subprocess.run(["sh", "-ec", "mkdir d && echo old > d/old && echo a > a"], cwd=env.root, check=True)
    env.start()
    burst(env, "rm -rf d && mkdir d && mv a d/b")
    assert last_batch_kinds(env) == [("deleted", "d/old"), ("deleted", "d"), ("created", "d"), ("moved", "a", "d/b")]
    assert "d/b" in state_at(str(env.out)).entries
    env.finish()


def test_mkdir_then_mv_into_it_records_the_parent_first(env):
    subprocess.run(["sh", "-ec", "echo a > a"], cwd=env.root, check=True)
    env.start()
    burst(env, "mkdir d && mv a d/b")
    assert last_batch_kinds(env) == [("created", "d"), ("moved", "a", "d/b")]
    env.finish()


def test_two_renames_onto_one_name(env):
    subprocess.run(["sh", "-ec", "echo A > a && echo C > c"], cwd=env.root, check=True)
    env.start()
    burst(env, "mv a b && mv c b")
    assert last_batch_kinds(env) == [("deleted", "a"), ("moved", "c", "b")]
    st = state_at(str(env.out))
    assert set(st.entries) == {"b"} and st.entries["b"].sha256 == hashlib.sha256(b"C\n").hexdigest()
    env.finish()


def test_rename_chain_with_a_write(env):
    subprocess.run(["sh", "-ec", "echo one > a"], cwd=env.root, check=True)
    env.start()
    burst(env, "mv a b && mv b c && echo two >> c")
    assert last_batch_kinds(env) == [("moved", "a", "c"), ("modified", "c")]
    assert not any(k == ("deleted", "b") or k == ("created", "b") for k in kinds(records(str(env.out))))
    env.finish()


def test_z_then_a_in_one_read_is_one_batch(env):
    env.start()
    (env.root / "z").write_text("z")
    (env.root / "a").write_text("a")
    env.settle()
    b = batch_records(str(env.out))
    assert len(b) == 2 and b[1]["records"] == 2
    assert kinds(records(str(env.out), batch=1)) == [("created", "a"), ("created", "z")]
    with pytest.raises(Exception):
        state_at(str(env.out), "1.5")
    env.finish()


def test_move_across_ignore_boundaries(env):
    env.skip = lambda r: r.startswith("src/cache") or r.startswith("dst/keep")
    subprocess.run(["sh", "-ec", "mkdir -p src/cache src/keep && echo c > src/cache/c && echo k > src/keep/k && echo a > src/a"], cwd=env.root, check=True)
    env.start(ignore=["src/cache/**", "dst/keep/**"])
    assert set(state_at(str(env.out)).entries) == {"src", "src/keep", "src/keep/k", "src/a"}
    burst(env, "mv src dst")
    assert set(state_at(str(env.out)).entries) == {"dst", "dst/cache", "dst/cache/c", "dst/a"}
    burst(env, "rm -rf dst/keep")  # an ignored child of the moved directory
    env.finish()


def test_ignored_child_of_moved_directory_does_not_linger(env):
    env.skip = lambda r: r.startswith("b/tmp")
    subprocess.run(["sh", "-ec", "mkdir -p a/tmp && echo t > a/tmp/t && echo x > a/x"], cwd=env.root, check=True)
    env.start(ignore=["b/tmp/**"])
    burst(env, "mv a b")
    assert "b/tmp" not in state_at(str(env.out)).entries
    burst(env, "rm -rf b/tmp")
    env.finish()


def test_replacement_directory_keeps_its_watch(env):
    subprocess.run(["sh", "-ec", "mkdir d && echo x > d/x"], cwd=env.root, check=True)
    env.start()
    burst(env, f"mv d {env.outside}/d && mkdir d && echo f > d/f")
    (env.root / "d" / "g").write_text("g")  # only seen if the new d is watched
    env.settle()
    assert {"d/f", "d/g"} <= set(state_at(str(env.out)).entries)
    env.finish()


def test_directory_replaced_by_symlink_to_outside(env):
    subprocess.run(["sh", "-ec", "mkdir d && echo inside > d/f"], cwd=env.root, check=True)
    (env.outside / "f").write_text("OUTSIDE SECRET\n")
    env.start()
    burst(env, f"rm -rf d && ln -s {env.outside} d")
    st = state_at(str(env.out))
    assert st.entries["d"].type == "symlink" and "d/f" not in st.entries
    secret = hashlib.sha256(b"OUTSIDE SECRET\n").hexdigest()
    assert not os.path.exists(env.w.blobs.path(secret))
    (env.outside / "f").write_text("changed outside\n")
    env.settle()
    env.finish()


@pytest.mark.parametrize("depth", [1, 2, 4])
def test_symlinked_intermediate_directory(env, depth):
    inner = "/".join(f"l{i}" for i in range(depth))
    subprocess.run(["sh", "-ec", f"mkdir -p {inner} && echo in > {inner}/f"], cwd=env.root, check=True)
    (env.outside / "deep").mkdir()
    (env.outside / "deep" / "f").write_text("OUT\n")
    (env.outside / "deep" / "g").write_text("OUT G\n")
    env.start()
    burst(env, f"rm -rf {inner} && ln -s {env.outside}/deep {inner}")
    st = state_at(str(env.out))
    assert st.entries[inner].type == "symlink"
    assert not any(p.startswith(inner + "/") for p in st.entries)
    assert not os.path.exists(env.w.blobs.path(hashlib.sha256(b"OUT G\n").hexdigest()))
    env.finish()


def test_chmod_000_does_not_crash(env):
    env.start()
    burst(env, "mkdir d && echo x > d/f && chmod 000 d")
    assert env.w.error is None and state_at(str(env.out)).entries["d"].content == "inaccessible"
    os.chmod(env.root / "d", 0o755)
    env.finish()


def test_hard_link_chmod(env):
    subprocess.run(["sh", "-ec", "echo 1 > a && ln a b"], cwd=env.root, check=True)
    env.start()
    burst(env, "chmod 600 a")
    st = state_at(str(env.out))
    assert st.entries["a"].mode == st.entries["b"].mode == 0o600
    env.finish()


def test_open_append_close_records_nothing(env):
    subprocess.run(["sh", "-ec", "echo x > f"], cwd=env.root, check=True)
    env.start()
    burst(env, ": >> f")
    assert len(batch_records(str(env.out))) == 1
    env.finish()


# ---- second review ---------------------------------------------------------


def test_b1_directory_returning_through_an_unwatched_parent_keeps_new_children(env):
    subprocess.run(["sh", "-ec", "mkdir d && echo k > d/known"], cwd=env.root, check=True)
    env.start()
    burst(env, "mv d e && mkdir new && mv e new/e && mv new/e d && echo lost > d/lost")
    assert {"d/known", "d/lost", "new"} <= set(state_at(str(env.out)).entries)
    env.w.core.pending.touch("d", env.w.core.clock(), 0.0)
    assert [r for r in env.w.core.tick(force=True) if r["kind"] != "batch"] == []
    env.finish()


def test_b6_root_permission_loss_and_recovery_on_real_inotify(env):
    subprocess.run(["sh", "-ec", "echo x > f && mkdir d && echo y > d/g"], cwd=env.root, check=True)
    env.start()
    os.chmod(env.root, 0o000)
    try:
        env.w.step(max_wait=0.3)
        settle_quietly(env)
        assert state_at(str(env.out)).entries == {}
    finally:
        os.chmod(env.root, 0o755)
    env.settle()
    assert {"f", "d", "d/g"} <= set(state_at(str(env.out)).entries)
    env.finish()


def settle_quietly(env):
    from tests.watcher.helpers import settle

    settle(env.w, quiet=0.35)


def test_nested_permission_loss_and_recovery(env):
    subprocess.run(["sh", "-ec", "mkdir -p a/b && echo x > a/b/f"], cwd=env.root, check=True)
    env.start()
    burst(env, "chmod 000 a/b")
    st = state_at(str(env.out))
    assert st.entries["a/b"].content == "inaccessible" and "a/b/f" not in st.entries
    burst(env, "chmod 755 a/b && echo y > a/b/g")
    assert {"a/b/f", "a/b/g"} <= set(state_at(str(env.out)).entries)
    env.finish()


def test_move_chain_that_leaves_and_reenters_the_workspace(env):
    subprocess.run(["sh", "-ec", "echo a > a && mkdir d && echo x > d/x"], cwd=env.root, check=True)
    env.start()
    burst(env, f"mv a {env.outside}/a && mv {env.outside}/a b && mv d {env.outside}/d && echo y > {env.outside}/d/y && mv {env.outside}/d e")
    assert {"b", "e", "e/x", "e/y"} <= set(state_at(str(env.out)).entries)
    env.finish()


def test_d026_instruction_files_on_real_inotify_and_not_in_the_metric_copy(env):
    import json as _json
    import sys as _sys

    listing = "import json, os; print(json.dumps({'files': sorted(os.path.join(d, f) for d, _, fs in os.walk('.') for f in fs)}))"
    subprocess.run(["sh", "-ec", "echo i > CLAUDE.md && mkdir sub && echo j > sub/AGENTS.md && echo k > keep"], cwd=env.root, check=True)
    env.skip = lambda r: r.startswith(".git") or r.split("/")[-1] in ("CLAUDE.md", "AGENTS.md")
    env.start(metric_cmd=[_sys.executable, "-c", listing])
    n = len(batch_records(str(env.out)))
    burst(env, "echo edited >> CLAUDE.md && echo more >> sub/AGENTS.md")
    assert len(batch_records(str(env.out))) == n  # no batch for ignored files alone
    burst(env, "mv keep CLAUDE.md")
    assert kinds(records(str(env.out), batch=batch_records(str(env.out))[-1]["batch"])) == [("deleted", "keep")]
    env.finish()
    for line in (env.out / "metrics.jsonl").read_text().splitlines():
        files = _json.loads(line)["metrics"]["files"]
        assert not any(f.endswith(("CLAUDE.md", "AGENTS.md")) for f in files), files


def test_d026_ignored_git_refs_on_real_git(env):
    subprocess.run(["sh", "-ec", "git init -q -b main && git -c user.name=u -c user.email=u@x commit -q --allow-empty -m one"], cwd=env.root, check=True)
    env.start()
    n = len(batch_records(str(env.out)))
    burst(env, "git update-ref refs/t3/orchestration/checkpoints/0 HEAD && git update-ref refs/t3/orchestration/checkpoints/1 HEAD && git pack-refs --all")
    assert len(batch_records(str(env.out))) == n
    assert not any(k.startswith("refs/t3/") for k in state_at(str(env.out)).git["refs"])
    env.finish()


def test_b2_scan_races_a_writer_on_real_inotify(env, monkeypatch):
    from cohbench.watcher import scan as scanmod

    subprocess.run(["sh", "-ec", "echo A1 > a && echo B1 > b"], cwd=env.root, check=True)
    env.start()
    real = scanmod.Scanner._read
    fired = []

    def racing(self, st, name, dfd, res, rel, *a):
        e = real(self, st, name, dfd, res, rel, *a)
        if rel == "a" and not fired:
            fired.append(1)
            (env.root / "a").write_text("A2\n")
            (env.root / "b").write_text("B2\n")
        return e

    monkeypatch.setattr(scanmod.Scanner, "_read", racing)
    burst(env, "echo A1x > a && echo B1x > b")
    for b in batch_records(str(env.out)):
        if b["batch"] > 0 and b["settled"]:
            st = state_at(str(env.out), b["batch"])
            pair = (st.entries["a"].sha256, st.entries["b"].sha256)
            import hashlib as h

            assert pair != (h.sha256(b"A1x\n").hexdigest(), h.sha256(b"B2\n").hexdigest()), "a settled batch mixed two states"
    env.finish()


# ---- third review ----------------------------------------------------------


def test_same_length_overwrites_while_marks_are_taken(env):
    import threading
    import time as _time

    (env.root / "f").write_bytes(b"AAAA")
    env.start(quiet_ms=30, max_delay_ms=200)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            with open(env.root / "f", "r+b") as fh:
                fh.write(b"ABCD"[i % 4 : i % 4 + 1] * 4)
            i += 1
            _time.sleep(0.001)

    t = threading.Thread(target=writer)
    t.start()
    try:
        for k in range(15):
            env.w.mark(f"m{k}")
            for _ in range(5):
                env.w.step(max_wait=0.01)
    finally:
        stop.set()
        t.join()
    env.w.mark("final")
    rec = [r for r in __import__("cohbench.watcher", fromlist=["read_events"]).read_events(str(env.out)) if r["kind"] == "mark"][-1]
    assert rec["settled"], rec
    assert state_at(str(env.out), rec["batch"]).entries["f"].sha256 == hashlib.sha256((env.root / "f").read_bytes()).hexdigest()
    env.finish()


def test_d026_nothing_written_names_the_ignored_files_or_refs(env):
    import json as _json
    import sys as _sys

    env.skip = lambda r: r.startswith(".git") or "CLAUDE.md" in r or "AGENTS.md" in r
    subprocess.run(["sh", "-ec", "git init -q -b main && git -c user.name=u -c user.email=u@x commit -q --allow-empty -m one && echo x > x"], cwd=env.root, check=True)
    env.start(metric_cmd=[_sys.executable, "-c", "import json, os; print(json.dumps({'n': len(os.listdir('.'))}))"])
    burst(env, "echo one > CLAUDE.md && echo 1 >> x")
    burst(env, "echo two > .CLAUDE.md.tmp && mv .CLAUDE.md.tmp CLAUDE.md && echo 2 >> x")
    burst(env, "mv CLAUDE.md notes.md")
    burst(env, "mv notes.md CLAUDE.md && echo 3 >> x")
    burst(env, "mv CLAUDE.md AGENTS.md && git update-ref refs/t3/orch/cp/0 HEAD && echo 4 >> x")
    burst(env, "rm AGENTS.md && git update-ref refs/t3/orch/cp/1 HEAD")
    env.w.mark("end")
    env.finish()
    for d, _, fs in os.walk(env.out):
        if "/blobs" in d:
            continue
        for f in fs:
            data = open(os.path.join(d, f), "rb").read()
            for bad in (b"CLAUDE.md", b"AGENTS.md", b"refs/t3"):
                assert bad not in data, (bad, os.path.join(d, f))
    del _json
