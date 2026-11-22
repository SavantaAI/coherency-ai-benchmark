"""Test helpers: an independent tree walk, a fake watch backend, and
in-process watcher control."""

from __future__ import annotations

import hashlib
import os
import stat
import time

from cohbench.watcher import replay


def walk_manifest(root: str, skip=None) -> dict[str, tuple]:
    """(type, mode, uid, gid, ino, sha256) for every path under root, computed
    without watcher code. Symlinks are read, never followed. A directory this
    process cannot list and search contributes no children."""
    skip = skip or (lambda rel: False)
    out: dict[str, tuple] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            names = sorted(os.listdir(full_dir))
            stats = [(n, os.lstat(os.path.join(full_dir, n))) for n in names]
        except PermissionError:
            continue
        for name, st in stats:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if skip(rel):
                continue
            full = os.path.join(full_dir, name)
            mode = stat.S_IMODE(st.st_mode)
            sha = None
            if stat.S_ISDIR(st.st_mode):
                t = "dir"
                stack.append(rel)
            elif stat.S_ISLNK(st.st_mode):
                t = "symlink"
                sha = hashlib.sha256(os.fsencode(os.readlink(full))).hexdigest()
            elif stat.S_ISREG(st.st_mode):
                t = "file"
                try:
                    with open(full, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                except PermissionError:
                    sha = None
            else:
                t = "other"
            out[rel] = (t, mode, st.st_uid, st.st_gid, st.st_ino, sha)
    return out


def git_skip(rel: str) -> bool:
    return rel == ".git" or rel.startswith(".git/")


def replay_manifest(out_dir: str, batch="last") -> dict[str, tuple]:
    st = replay.state_at(out_dir, batch)
    return {p: (e.type if e.type in ("dir", "symlink", "file") else "other", e.mode, e.uid, e.gid, e.ino, e.sha256) for p, e in st.entries.items()}


def state_difference(out_dir: str, root: str, skip=git_skip, batch="last") -> str | None:
    got = replay_manifest(out_dir, batch)
    want = walk_manifest(root, skip)
    if got == want:
        return None
    missing = sorted(set(want) - set(got))[:10]
    extra = sorted(set(got) - set(want))[:10]
    differ = sorted(p for p in set(got) & set(want) if got[p] != want[p])[:10]
    return f"replay differs from a walk: missing={missing} extra={extra} differ={[(p, got[p], want[p]) for p in differ]}"


def assert_state_matches(out_dir: str, root: str, skip=git_skip, batch="last") -> None:
    d = state_difference(out_dir, root, skip, batch)
    if d:
        raise AssertionError(d)


def records(out_dir: str, *, batch: int | None = None, include_baseline: bool = False) -> list[dict]:
    """State records (no batch or mark records)."""
    ev = replay.read_events(out_dir)
    out = []
    for r in ev:
        if r["kind"] in ("batch", "mark"):
            continue
        if batch is not None and r["batch"] != batch:
            continue
        if not include_baseline and r["batch"] == 0:
            continue
        out.append(r)
    return out


def batch_records(out_dir: str) -> list[dict]:
    return [r for r in replay.read_events(out_dir) if r["kind"] == "batch"]


def kinds(evs: list[dict]) -> list[tuple]:
    return [(e["kind"], e.get("from"), e["path"]) if e["kind"] == "moved" else (e["kind"], e["path"]) for e in evs if e["kind"] not in ("batch", "mark")]


class FakeBackend:
    """Watches as numbers keyed by inode; nothing is delivered."""

    def __init__(self) -> None:
        self.next = 1
        self.by_ident: dict[tuple[int, int], int] = {}
        self.removed: list[int] = []
        self.fd = None

    def add_watch_fd(self, fd: int) -> int:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(20, "not a directory")
        key = (st.st_dev, st.st_ino)
        if key not in self.by_ident:
            self.by_ident[key] = self.next
            self.next += 1
        return self.by_ident[key]

    def rm_watch(self, wd: int) -> None:
        self.removed.append(wd)
        for k, v in list(self.by_ident.items()):
            if v == wd:
                del self.by_ident[k]

    def read(self):
        return []


def settle(w, quiet: float = 0.35, timeout: float = 30.0) -> None:
    """Step a watcher until nothing has happened for `quiet` seconds."""
    end = time.monotonic() + timeout
    last = time.monotonic()
    while time.monotonic() < end:
        out = w.step(max_wait=0.02)
        now = time.monotonic()
        if out or w.core.pending or w.core.overflowed:
            last = now
        elif now - last >= quiet:
            return
    raise AssertionError("watcher did not settle")


def wait_metrics_idle(w, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        w.step(max_wait=0.02)
        if w.metrics.idle():
            time.sleep(0.05)
            if w.metrics.idle():
                return
    raise AssertionError("metric runs did not finish")
