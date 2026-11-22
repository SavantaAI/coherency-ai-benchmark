"""The core on any host: real files in a temp directory, hints given by hand
(what inotify would report), scans and verified batches for real."""

from __future__ import annotations

import os

import pytest

from cohbench.watcher import inotify as I
from cohbench.watcher import replay
from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.config import Config
from cohbench.watcher.core import Core
from cohbench.watcher.inotify import RawEvent
from cohbench.watcher.records import EventLog
from tests.watcher.helpers import FakeBackend, assert_state_matches, batch_records, kinds


class Rig:
    def __init__(self, tmp_path, **cfg):
        self.root = tmp_path / "root"
        self.root.mkdir(exist_ok=True)
        self.out = tmp_path / "out"
        self.out.mkdir(exist_ok=True)
        self.outside = tmp_path / "outside"
        self.outside.mkdir(exist_ok=True)
        self.t = 100.0
        self.cfg = Config(root=str(self.root), out=str(self.out), **cfg)
        from tests.watcher.helpers import git_skip

        self.skip = git_skip
        self.core = Core(self.cfg, FakeBackend(), EventLog(str(self.out)), BlobStore(str(self.out)), clock=lambda: self.t, wallclock=lambda: 1.7e9 + self.t)

    def start(self):
        return self.core.baseline()

    def p(self, rel):
        return self.root / rel

    def write(self, rel, data: bytes = b"x"):
        p = self.p(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def dirty(self, *rels, modify=False):
        for r in rels:
            self.core.pending.touch(r, self.t, 0.0, modify=modify)

    def rename(self, src, dst):
        os.rename(self.p(src), self.p(dst))
        self.core.pending.rename(src, dst, self.t, 0.0)

    def commit(self):
        self.t += 1.0
        out = self.core.tick(force=True)
        assert_state_matches(str(self.out), str(self.root), skip=self.skip)
        return out

    def commit_skip(self):
        """Commit without the walk comparison (for D-026 tests: the walk would
        include the ignored names)."""
        self.t += 1.0
        out = self.core.tick(force=True)
        from cohbench.watcher.ignore import IgnoreRules

        rules = IgnoreRules(names=self.cfg.ignore_names, forms=self.cfg.ignore_name_forms)
        assert_state_matches(str(self.out), str(self.root), skip=lambda r: self.skip(r) or rules.ignored(r))
        return out

    def last_batch(self):
        return batch_records(str(self.out))[-1]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_baseline_is_batch_zero(rig):
    rig.write("a.txt", b"a")
    rig.write("d/b.txt", b"b")
    os.symlink("a.txt", rig.p("ln"))
    out = rig.start()
    assert out[-1]["kind"] == "batch" and out[-1]["batch"] == 0 and out[-1]["check"] == "ok"
    assert kinds(out) == [("created", "a.txt"), ("created", "d"), ("created", "ln"), ("created", "d/b.txt")]
    assert all(r["batch"] == 0 for r in out)
    assert_state_matches(str(rig.out), str(rig.root))


def test_empty_tree_baseline_still_commits(rig):
    out = rig.start()
    assert out == [out[-1]] and out[-1]["records"] == 0
    assert replay.state_at(str(rig.out), 0).entries == {}


def test_write_temp_then_rename_is_a_modification(rig):
    rig.write("x", b"old")
    rig.start()
    rig.write("x.tmp", b"new")
    rig.dirty("x.tmp", modify=True)
    rig.rename("x.tmp", "x")
    out = rig.commit()
    assert kinds(out) == [("modified", "x")]
    assert out[0]["via_rename"] == "x.tmp" and "identity" in out[0]["changed"]
    assert out[-1]["renames"] == [["x.tmp", "x"]]


def test_rename_over_existing_known_file(rig):
    rig.write("a", b"A")
    rig.write("b", b"B")
    rig.start()
    rig.rename("a", "b")
    assert kinds(rig.commit()) == [("deleted", "b"), ("moved", "a", "b")]


def test_identity_reuse_without_rename_is_not_a_move(rig):
    rig.write("a", b"A")
    rig.start()
    # Same inode at a new path but no rename was observed: delete and create.
    os.rename(rig.p("a"), rig.p("b"))
    rig.dirty("a", "b")
    assert kinds(rig.commit()) == [("deleted", "a"), ("created", "b")]


def test_files_in_a_renamed_directory_are_found(rig):
    rig.write("d/sub/f", b"f")
    rig.start()
    rig.write("d/sub/new.txt", b"hello")
    rig.dirty("d/sub/new.txt", modify=True)
    rig.rename("d", "e")
    assert kinds(rig.commit()) == [("moved", "d", "e"), ("created", "e/sub/new.txt")]


def test_truncate_rewrite_and_identical_rewrite(rig):
    rig.write("f", b"one")
    rig.start()
    rig.write("f", b"two")
    rig.dirty("f", modify=True)
    out = rig.commit()
    assert kinds(out) == [("modified", "f")] and out[0]["prev_sha256"]
    rig.write("f", b"two")
    rig.dirty("f", modify=True)
    assert kinds(rig.commit()) == [("rewritten", "f")]


def test_open_close_without_write_records_nothing(rig):
    rig.write("f", b"same")
    rig.start()
    with open(rig.p("f"), "a"):
        pass
    rig.core.pending.touch("f", rig.t, 0.0, close_write=True)
    assert rig.commit() == []


def test_hard_link_chmod_changes_both(rig):
    rig.write("a", b"1")
    os.link(rig.p("a"), rig.p("b"))
    rig.start()
    os.chmod(rig.p("a"), 0o600)
    rig.dirty("a")  # inotify reports only the name used
    out = rig.commit()
    assert sorted(kinds(out)) == [("attrib", "a"), ("attrib", "b")]
    st = replay.state_at(str(rig.out))
    assert st.entries["a"].mode == st.entries["b"].mode == 0o600
    assert st.links() == [["a", "b"]]


def test_hard_link_write_updates_every_link(rig):
    rig.write("a", b"1")
    os.link(rig.p("a"), rig.p("b"))
    rig.start()
    with open(rig.p("a"), "r+b") as f:
        f.write(b"2")
    rig.dirty("a", modify=True)
    assert sorted(kinds(rig.commit())) == [("modified", "a"), ("modified", "b")]


def test_directory_replaced_by_symlink_never_reads_outside(rig):
    rig.write("d/f", b"inside")
    rig.start()
    (rig.outside / "f").write_bytes(b"OUTSIDE SECRET")
    (rig.p("d") / "f").unlink()
    rig.p("d").rmdir()
    os.symlink(str(rig.outside), rig.p("d"))
    rig.dirty("d/f", "d")
    out = rig.commit()
    assert ("created", "d/f") not in kinds(out)
    assert kinds(out) == [("deleted", "d/f"), ("deleted", "d"), ("created", "d")]
    import hashlib

    secret = hashlib.sha256(b"OUTSIDE SECRET").hexdigest()
    assert not os.path.exists(rig.core.blobs.path(secret))


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_symlinked_intermediate_directory_is_unreachable(rig, depth):
    inner = "/".join(f"l{i}" for i in range(depth))
    rig.write(f"{inner}/f", b"inside")
    rig.start()
    deep = rig.outside / inner
    deep.mkdir(parents=True)
    (deep / "f").write_bytes(b"OUTSIDE")
    (deep / "g").write_bytes(b"OUTSIDE G")
    # Replace the deepest intermediate directory with a symlink to outside.
    target_dir = rig.p(inner)
    (target_dir / "f").unlink()
    target_dir.rmdir()
    os.symlink(str(deep), target_dir)
    rig.dirty(f"{inner}/f", f"{inner}/g", inner)
    out = rig.commit()
    paths = {r["path"] for r in out if r["kind"] == "created"}
    assert f"{inner}/f" not in paths and f"{inner}/g" not in paths
    st = replay.state_at(str(rig.out))
    assert st.entries[inner].type == "symlink"


def test_ignored_subtree_follows_the_rename(tmp_path):
    rig = Rig(tmp_path, ignore=["src/cache/**", "dst/keep/**"])
    rig.write("src/cache/c", b"c")
    rig.write("src/keep/k", b"k")
    rig.write("src/a", b"a")
    rig.start()
    assert set(replay.state_at(str(rig.out)).entries) == {"src", "src/keep", "src/keep/k", "src/a"}
    rig.rename("src", "dst")
    rig.t += 1
    rig.core.tick(force=True)
    st = replay.state_at(str(rig.out))
    # dst/cache is newly included, dst/keep newly ignored.
    assert set(st.entries) == {"dst", "dst/cache", "dst/cache/c", "dst/a"}
    from tests.watcher.helpers import assert_state_matches as asm

    asm(str(rig.out), str(rig.root), skip=lambda r: r.startswith("src/cache") or r.startswith("dst/keep"))


def test_inaccessible_directory_and_recovery(rig):
    rig.write("d/f", b"x")
    rig.start()
    os.chmod(rig.p("d"), 0o000)
    try:
        rig.dirty("d")
        out = rig.commit()
        assert kinds(out) == [("deleted", "d/f"), ("attrib", "d")]
        assert replay.state_at(str(rig.out)).entries["d"].content == "inaccessible"
    finally:
        os.chmod(rig.p("d"), 0o755)
    rig.dirty("d")
    out = rig.commit()
    assert ("created", "d/f") in kinds(out)


def test_git_coarse(tmp_path):
    rig = Rig(tmp_path)
    g = rig.p(".git")
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    rig.start()
    st = replay.state_at(str(rig.out))
    assert st.entries == {} and st.git == {"head": "ref: refs/heads/main", "refs": {}, "backend": "files"}
    (g / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    rig.core.pending.git(rig.t, 0.0)
    out = rig.commit()
    assert kinds(out) == [("git", ".git")] and out[0]["refs"] == {"refs/heads/main": "a" * 40}


def test_overflow_rescans_everything_as_recovered(rig):
    rig.write("keep", b"k")
    rig.write("change", b"1")
    rig.write("drop", b"d")
    rig.start()
    rig.write("change", b"2")
    rig.p("drop").unlink()
    rig.write("newdir/new", b"n")
    os.rename(rig.p("keep"), rig.p("kept"))
    rig.core.feed([RawEvent(-1, I.IN_Q_OVERFLOW, 0, "")], now=rig.t, wall=0.0)
    out = rig.core.tick()
    assert out[-1]["reason"] == "overflow" and out[-1]["recovered"]
    # No renames are trusted after an overflow: the move is a delete and a create.
    assert set(kinds(out)) == {("deleted", "drop"), ("deleted", "keep"), ("created", "kept"), ("created", "newdir"), ("created", "newdir/new"), ("modified", "change")}
    assert_state_matches(str(rig.out), str(rig.root))


def test_self_check_failure_writes_a_resync(rig, monkeypatch):
    rig.write("a", b"1")
    rig.start()
    rig.write("a", b"2")
    rig.dirty("a", modify=True)
    from cohbench.watcher import delta

    real = delta.plan

    def broken(*a, **k):
        recs = real(*a, **k)
        for r in recs:
            if r["kind"] == "modified":
                r["sha256"] = "0" * 64  # a wrong record
        return recs

    monkeypatch.setattr(delta, "plan", broken)
    out = rig.commit()
    b = out[-1]
    assert b["resync"] and b["check"] == "failed" and ("sha256" in b["check_error"] or "'a'" in b["check_error"])
    assert kinds(out) == [("deleted", "a"), ("created", "a")]
