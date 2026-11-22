"""Property tests: random file operations against real inotify. Linux only.

Every choice comes from a seeded random.Random over sorted listings, and
file data comes from the same generator, so a seed reproduces exactly.

The oracle (`oracle`) is an independent walk giving, for every path: type,
mode, owner, device and inode number (identity within the run), size,
content hash (None when this uid cannot read it) and access status
(`inaccessible` for a directory it cannot list), plus the groups of
hard-linked paths, plus the git state (HEAD and every ref outside refs/t3/)
as git itself reports it. `.git` is otherwise left out of the walk. The
unknown-content `epoch` is not compared: under D-032 it is best effort. Replays are compared on the same fields, and every blob a
replayed state references must exist and hash correctly.

Two modes:

  bursts       1 to 8 operations with the watcher not running, then it
               settles; after every burst, state_at(last) must equal the
               oracle, every batch must have passed its self-check, and every
               committed batch must replay with its blobs verified.
  interleaved  the operations run from inside the scanner, between one
               content read or directory listing and the next, so the tree
               changes while a batch is being read and validated. The state
               after every operation is recorded; every committed batch
               flagged settled must equal one of those states, and once the
               writer is done and the watcher settles, the replay must equal
               the oracle (no file lost).

Sizes: WATCHER_PROPERTY_SEEDS (default 120) seeds per mode from
WATCHER_PROPERTY_SEED_START (default 0), WATCHER_PROPERTY_BURSTS (default
25) bursts per seed. Totals are printed by test_property_totals."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import stat
import subprocess

import pytest

from cohbench.watcher import replay
from cohbench.watcher import scan as scanmod
from cohbench.watcher.config import Config
from cohbench.watcher.watcher import Watcher
from tests.watcher.helpers import batch_records, settle

pytestmark = pytest.mark.linux_inotify

SEEDS = int(os.environ.get("WATCHER_PROPERTY_SEEDS", "120"))
SEED_START = int(os.environ.get("WATCHER_PROPERTY_SEED_START", "0"))
BURSTS = int(os.environ.get("WATCHER_PROPERTY_BURSTS", "25"))
TOTALS: dict = {}


def total(mode: str, key: str, n: int = 1) -> None:
    TOTALS.setdefault(mode, {}).setdefault(key, 0)
    TOTALS[mode][key] += n


# ---- git ------------------------------------------------------------------

IGNORED_REFS = ("refs/t3/",)
GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a",
    "GIT_AUTHOR_EMAIL": "a@example.invalid",
    "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@example.invalid",
}


def git(root: str, *args: str) -> str:
    r = subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.hooksPath=/dev/null", *args], cwd=root, env=GIT_ENV, capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError(f"git {args[0]}: {r.stderr.strip()}")
    return r.stdout


def git_state(root: str) -> dict | None:
    """HEAD and the refs, from git commands (never from the files)."""
    try:
        lines = git(root, "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
        try:
            head = "ref: " + git(root, "symbolic-ref", "-q", "HEAD").strip()
        except OSError:
            head = git(root, "rev-parse", "--verify", "HEAD").strip()  # detached: the commit itself
    except OSError:
        return None
    refs = {}
    for line in lines:
        oid, name = line.split(" ", 1)
        if not name.startswith(IGNORED_REFS):
            refs[name] = oid
    return {"head": head, "refs": dict(sorted(refs.items())), "backend": "files"}


# ---- oracle ----------------------------------------------------------------


def oracle(root: str) -> tuple[dict, list]:
    out: dict[str, tuple] = {}
    groups: dict[int, list[str]] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            names = sorted(os.listdir(full_dir))
            stats = [(n, os.lstat(os.path.join(full_dir, n))) for n in names]
        except PermissionError:
            if rel_dir:
                t, mode, size, sha, _ = out[rel_dir]
                out[rel_dir] = (t, mode, size, sha, "inaccessible")
            continue
        for name, st in stats:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel == ".git":
                continue
            full = os.path.join(full_dir, name)
            mode = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, st.st_dev, st.st_ino)
            if stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", mode, None, None, "ok")
                stack.append(rel)
                continue
            groups.setdefault(st.st_ino, []).append(rel)
            if stat.S_ISLNK(st.st_mode):
                raw = os.fsencode(os.readlink(full))
                out[rel] = ("symlink", mode, len(raw), hashlib.sha256(raw).hexdigest(), "ok")
            elif stat.S_ISREG(st.st_mode):
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                    out[rel] = ("file", mode, len(data), hashlib.sha256(data).hexdigest(), "ok")
                except PermissionError:
                    out[rel] = ("file", mode, st.st_size, None, "unreadable")
            else:
                out[rel] = ("other", mode, None, None, "ok")
    links = sorted(sorted(g) for g in groups.values() if len(g) > 1)
    g = git_state(root)
    if g is not None:
        out[".git (coarse state)"] = ("git", json.dumps(g, sort_keys=True), None, None, "ok")
    return out, links


def replayed(out_dir: str, batch="last", events=None) -> tuple[dict, list]:
    st = replay.state_at(out_dir, batch, events, verify_blobs=True)
    got = {}
    for p, e in st.entries.items():
        t = e.type if e.type in ("dir", "symlink", "file") else "other"
        access = "inaccessible" if e.content == "inaccessible" else ("unreadable" if e.content == "unreadable" else "ok")
        got[p] = (t, (e.mode, e.uid, e.gid, e.dev, e.ino), e.size, e.sha256, access)
    if st.git is not None:
        got[".git (coarse state)"] = ("git", json.dumps(st.git, sort_keys=True), None, None, "ok")
    return got, st.links()


def digest(state: tuple[dict, list]) -> str:
    return hashlib.sha256(json.dumps([sorted(state[0].items()), state[1]]).encode()).hexdigest()


def difference(a: tuple[dict, list], b: tuple[dict, list]) -> str | None:
    if a == b:
        return None
    ea, eb = a[0], b[0]
    missing = sorted(set(eb) - set(ea))[:8]
    extra = sorted(set(ea) - set(eb))[:8]
    differ = [(p, ea[p], eb[p]) for p in sorted(set(ea) & set(eb)) if ea[p] != eb[p]][:8]
    return f"missing={missing} extra={extra} differ={differ} links {a[1]} vs {b[1]}"


# ---- operations ---------------------------------------------------------------


def listing(root: str) -> tuple[list[str], list[str], list[str]]:
    """(dirs, files, symlinks and others), sorted; root is ''. Directories
    that cannot be listed contribute nothing below them."""
    dirs, files, others = [""], [], []
    stack = [""]
    while stack:
        d = stack.pop()
        full = os.path.join(root, d) if d else root
        try:
            names = sorted(os.listdir(full))
        except OSError:
            continue
        for x in names:
            p = f"{d}/{x}" if d else x
            if p == ".git":
                continue  # changed only through git commands
            st = os.lstat(os.path.join(root, p))
            if stat.S_ISDIR(st.st_mode):
                dirs.append(p)
                stack.append(p)
            elif stat.S_ISREG(st.st_mode):
                files.append(p)
            else:
                others.append(p)
    return sorted(dirs), sorted(files), sorted(others)


OPS = ["create", "create", "write", "append", "truncate", "chmod", "rename_file", "rename_over", "mkdir", "rmdir", "rm_rf", "symlink", "hardlink", "dir_to_file", "file_to_dir", "rename_dir", "rename_dir_over", "write_tmp_rename", "out_and_back", "nested_perm_cycle", "root_perm_cycle", "root_lock", "b1_sequence", "hold_open", "release", "git_commit", "git_branch", "git_ignored_ref", "git_pack_refs", "git_detach"]


class Ops:
    def __init__(self, root: str, outside: str, rng: random.Random) -> None:
        self.root = root
        self.outside = outside
        self.rng = rng
        self.n = 0
        self.locked: list[str] = []  # directories made mode 000, to restore
        self.held: list = []  # files open for writing across batches

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel) if rel else self.root

    def fresh(self, d: str) -> str:
        self.n += 1
        name = self.rng.choice(["a", "b", "c", "x", "tmp", "n"]) + str(self.n % 7)
        return f"{d}/{name}" if d else name

    def data(self) -> bytes:
        k = self.rng.randrange(5)
        return [b"", b"x", b"hello\n", b"same\n", self.rng.randbytes(self.rng.randint(1, 3000))][k]

    def restore(self) -> None:
        for f in self.held:
            f.close()
        self.held.clear()
        os.chmod(self.root, 0o755)
        for d in reversed(self.locked):
            try:
                os.chmod(d, 0o755)
            except OSError:
                pass
        self.locked.clear()

    def one(self) -> str:
        dirs, files, others = listing(self.root)
        r = self.rng
        nondirs = files + others
        op = r.choice(OPS)
        try:
            if op == "create":
                with open(self.p(self.fresh(r.choice(dirs))), "wb") as f:
                    f.write(self.data())
            elif op == "write" and files:
                with open(self.p(r.choice(files)), "wb") as f:
                    f.write(self.data())
            elif op == "append" and files:
                with open(self.p(r.choice(files)), "ab") as f:
                    f.write(self.data())
            elif op == "truncate" and files:
                os.truncate(self.p(r.choice(files)), r.randint(0, 3))
            elif op == "chmod" and (files or dirs[1:]):
                if files and r.random() < 0.7:
                    os.chmod(self.p(r.choice(files)), r.choice([0o600, 0o644, 0o755, 0o400, 0o200]))
                elif dirs[1:]:
                    os.chmod(self.p(r.choice(dirs[1:])), r.choice([0o755, 0o700, 0o750]))
            elif op == "rename_file" and nondirs:
                os.rename(self.p(r.choice(nondirs)), self.p(self.fresh(r.choice(dirs))))
            elif op == "rename_over" and len(nondirs) >= 2:
                a, b = r.sample(nondirs, 2)
                os.rename(self.p(a), self.p(b))
            elif op == "mkdir":
                os.mkdir(self.p(self.fresh(r.choice(dirs))))
            elif op == "rmdir" and dirs[1:]:
                os.rmdir(self.p(r.choice(dirs[1:])))
            elif op == "rm_rf" and dirs[1:]:
                shutil.rmtree(self.p(r.choice(dirs[1:])))
            elif op == "symlink":
                tgt = r.choice(["a1", "../x", "/etc/passwd", self.outside, "missing", "."] + nondirs[:3])
                os.symlink(tgt, self.p(self.fresh(r.choice(dirs))))
            elif op == "hardlink" and files:
                os.link(self.p(r.choice(files)), self.p(self.fresh(r.choice(dirs))))
            elif op == "dir_to_file" and dirs[1:]:
                d = r.choice(dirs[1:])
                shutil.rmtree(self.p(d))
                if r.random() < 0.5:
                    with open(self.p(d), "wb") as f:
                        f.write(self.data())
                else:
                    os.symlink(r.choice([self.outside, "nowhere"]), self.p(d))
            elif op == "file_to_dir" and nondirs:
                f = r.choice(nondirs)
                os.unlink(self.p(f))
                os.mkdir(self.p(f))
                if r.random() < 0.5:
                    with open(os.path.join(self.p(f), "inner"), "wb") as fh:
                        fh.write(b"inner")
            elif op == "rename_dir" and dirs[1:]:
                src = r.choice(dirs[1:])
                dst = self.fresh(r.choice([d for d in dirs if not (d == src or d.startswith(src + "/"))]))
                os.rename(self.p(src), self.p(dst))
            elif op == "rename_dir_over" and len(dirs) >= 3:
                src, dst = r.sample(dirs[1:], 2)
                os.rename(self.p(src), self.p(dst))
            elif op == "write_tmp_rename" and files:
                tgt = r.choice(files)
                with open(self.p(tgt + ".swp"), "wb") as f:
                    f.write(self.data())
                os.rename(self.p(tgt + ".swp"), self.p(tgt))
            elif op == "out_and_back" and (nondirs or dirs[1:]):
                src = r.choice(nondirs + dirs[1:])
                away = os.path.join(self.outside, f"away{self.n}")
                self.n += 1
                os.rename(self.p(src), away)
                if os.path.isdir(away) and not os.path.islink(away):
                    with open(os.path.join(away, "while_out"), "wb") as f:
                        f.write(self.data())
                os.rename(away, self.p(self.fresh(r.choice([d for d in listing(self.root)[0]]))))
            elif op == "nested_perm_cycle" and dirs[1:]:
                d = self.p(r.choice(dirs[1:]))
                if self.locked and r.random() < 0.6:
                    os.chmod(self.locked.pop(), 0o755)
                else:
                    os.chmod(d, 0o000)
                    self.locked.append(d)
            elif op == "root_perm_cycle":
                os.chmod(self.root, 0o000)
                os.chmod(self.root, 0o755)
            elif op == "root_lock":
                os.chmod(self.root, 0o000)  # stays until restore(), maybe across batches
            elif op == "hold_open" and len(self.held) < 3:
                f = open(self.p(self.fresh(r.choice(dirs))), "wb")
                f.write(self.data())
                f.flush()
                self.held.append(f)
            elif op == "release" and self.held:
                f = self.held.pop(0)
                f.write(self.data())
                f.close()
            elif op == "b1_sequence" and dirs[1:]:
                d = r.choice([x for x in dirs[1:] if "/" not in x] or dirs[1:])
                e, new = self.fresh(""), self.fresh("")
                os.rename(self.p(d), self.p(e))
                os.mkdir(self.p(new))
                os.rename(self.p(e), self.p(f"{new}/e"))
                os.rename(self.p(f"{new}/e"), self.p(d))
                with open(os.path.join(self.p(d), f"lost{self.n}"), "wb") as f:
                    f.write(self.data())
            elif op == "git_commit":
                git(self.root, "commit", "-q", "--allow-empty", "-m", f"c{self.n}")
                self.n += 1
            elif op == "git_branch":
                git(self.root, "branch", "-f", self.rng.choice(["topic", "feature", "x"]), "HEAD")
            elif op == "git_ignored_ref":
                git(self.root, "update-ref", f"refs/t3/cp/{self.n % 5}", "HEAD")
                self.n += 1
            elif op == "git_detach":
                # Detach HEAD, or attach it again: the same commit, so the
                # worktree does not change.
                try:
                    git(self.root, "symbolic-ref", "-q", "HEAD")
                    git(self.root, "checkout", "-q", "--detach", "HEAD")
                except OSError:
                    git(self.root, "checkout", "-q", "main")
            elif op == "git_pack_refs":
                git(self.root, "pack-refs", "--all")
            else:
                return op + ":skipped"
        except OSError:
            return op + ":failed"
        return op


def count(mode: str, done: str) -> None:
    total(mode, "ops_attempted")
    if done.endswith((":failed", ":skipped")):
        total(mode, "ops_not_applied")
    total(mode, "op:" + done.split(":")[0])
    if done.endswith(":failed"):
        total(mode, "failed:" + done.split(":")[0])


def make_tree(tmp_path, seed):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "x").write_text("outside\n")
    git(str(root), "init", "-q", "-b", "main")
    git(str(root), "commit", "-q", "--allow-empty", "-m", "start")
    rng = random.Random(seed)
    ops = Ops(str(root), str(outside), rng)
    for _ in range(rng.randint(0, 6)):
        ops.one()
    ops.restore()
    return root, ops, rng


def finish(w, mode):
    w.stop()
    w.shutdown()
    assert w.status == "stopped", w.error
    bs = batch_records(str(w.out))
    total(mode, "batches", len(bs))
    total(mode, "resyncs", sum(1 for x in bs if x["resync"]))
    total(mode, "unsettled_batches", sum(1 for x in bs if not x.get("settled", True)))
    bad = [x for x in bs if x["check"] != "ok" and "legal order" not in x.get("check_error", "")]
    assert not bad, f"self-check failed: {bad[0].get('check_error')}"
    ev = replay.read_events(str(w.out))
    for x in bs:
        replay.state_at(str(w.out), x["batch"], ev, verify_blobs=True)
    return bs, ev


def unlock_all(root):
    for d, ds, _ in os.walk(root):
        for x in ds:
            p = os.path.join(d, x)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IRWXU)


@pytest.mark.parametrize("seed", range(SEED_START, SEED_START + SEEDS))
def test_bursts(tmp_path, seed):
    root, ops, rng = make_tree(tmp_path, seed)
    w = Watcher(Config(root=str(root), out=str(tmp_path / "out"), quiet_ms=30, max_delay_ms=300, heartbeat_s=1.0))
    w.start()
    try:
        for b in range(BURSTS):
            for _ in range(rng.randint(1, 8)):
                count("bursts", ops.one())
            settle(w, quiet=0.08)
            total("bursts", "bursts")
            d = difference(replayed(str(w.out)), oracle(str(root)))
            assert d is None, f"seed {seed} burst {b}: {d}"
            # Locks and open handles usually stay into the next burst, so the
            # watcher sees them across batch boundaries.
            if rng.random() < 0.3:
                ops.restore()
    finally:
        ops.restore()
        finish(w, "bursts")
    assert difference(replayed(str(w.out)), oracle(str(root))) is None
    total("bursts", "seeds")
    unlock_all(root)


@pytest.mark.parametrize("seed", range(SEED_START, SEED_START + SEEDS))
def test_interleaved(tmp_path, seed, monkeypatch):
    root, ops, rng = make_tree(tmp_path, seed)
    states: list[str] = []  # digest after each writer operation, in order
    kept: list = []  # (operation, state) for the failure message
    windows: dict[int, tuple[int, int]] = {}  # batch -> state indexes during its commit
    when = random.Random(seed * 7919 + 1)
    queue: list[int] = []

    def record(op):
        o = oracle(str(root))
        states.append(digest(o))
        kept.append((op, o))

    def apply_one():
        if queue:
            queue.pop()
            done = ops.one()
            count("interleaved", done)
            record(done)

    real_read, real_list, real_validate = scanmod.Scanner._read, scanmod.Scanner._list, scanmod.Scanner.validate

    def read_hook(self, *a, **k):
        e = real_read(self, *a, **k)
        if when.random() < 0.5:
            apply_one()
        return e

    def list_hook(self, *a, **k):
        if when.random() < 0.5:
            apply_one()
        return real_list(self, *a, **k)

    def validate_hook(self, *a, **k):
        # Writes during the validation pass itself, too.
        if when.random() < 0.3:
            apply_one()
        return real_validate(self, *a, **k)

    w = Watcher(Config(root=str(root), out=str(tmp_path / "out"), quiet_ms=30, max_delay_ms=300, heartbeat_s=1.0))
    w.start()
    record("start")
    real_commit = w.core.commit

    def commit(taken, full, reason):
        start = len(states) - 1
        out = real_commit(taken, full, reason)
        if out:
            windows[out[-1]["batch"]] = (start, len(states) - 1)
        return out

    w.core.commit = commit
    monkeypatch.setattr(scanmod.Scanner, "_read", read_hook)
    monkeypatch.setattr(scanmod.Scanner, "_list", list_hook)
    monkeypatch.setattr(scanmod.Scanner, "validate", validate_hook)
    try:
        for b in range(BURSTS):
            queue.extend(range(rng.randint(1, 8)))
            apply_one()  # the first starts the batch; the rest land during scans
            for _ in range(400):
                w.step(max_wait=0.02)
                if not queue:
                    break
                if not w.core.pending:
                    apply_one()
            while queue:
                apply_one()
            if rng.random() < 0.3:
                ops.restore()
                record("restore")
            settle(w, quiet=0.08)
            total("interleaved", "bursts")
    finally:
        # Permissions come back while the hooks are still in place, and the
        # commit wrapper stays through shutdown, so the final batches are
        # checked against their windows too.
        ops.restore()
        record("restore")
        monkeypatch.undo()
        try:
            bs, ev = finish(w, "interleaved")
        finally:
            w.core.commit = real_commit
    # The baseline: taken before the hooks, with no writer running.
    assert digest(replayed(str(w.out), 0, ev)) == states[0], f"seed {seed}: the baseline is not the starting tree: {difference(replayed(str(w.out), 0, ev), kept[0][1])}"
    total("interleaved", "settled_batches_checked")
    last_index = 0
    for x in bs:
        if x["batch"] == 0 or not x.get("settled", True) or x["batch"] not in windows:
            continue
        lo, hi = windows[x["batch"]]
        total("interleaved", "settled_batches_checked")
        got = digest(replayed(str(w.out), x["batch"], ev))
        matches = [i for i in range(lo, hi + 1) if states[i] == got]
        if not matches:
            rep = replayed(str(w.out), x["batch"], ev)
            around = [(kept[i][0], difference(rep, kept[i][1])) for i in range(lo, hi + 1)]
            raise AssertionError(f"seed {seed}: batch {x['batch']} is settled but no state of its own scan window: {around}")
        later = [i for i in matches if i >= last_index]
        assert later, f"seed {seed}: batch {x['batch']} is a state from before the previous settled batch"
        last_index = later[0]
    d = difference(replayed(str(w.out)), oracle(str(root)))
    assert d is None, f"seed {seed} at the end: {d}"
    total("interleaved", "seeds")
    unlock_all(root)


def test_property_totals():
    """Runs last (file order): prints what the property tests covered."""
    print(f"\nPROPERTY TOTALS {json.dumps(TOTALS, sort_keys=True)}")
    if SEEDS:
        assert TOTALS["bursts"]["seeds"] == SEEDS and TOTALS["interleaved"]["seeds"] == SEEDS
