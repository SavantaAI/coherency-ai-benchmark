"""Coarse git state of the root `.git` directory: HEAD and the refs.

One traversal per scan: every directory of `.git` and `.git/refs/**` is
opened through the root's component-safe resolver (so a directory swapped
for a symlink between discovery and open is refused, never followed), given
a watch through the callback, listed, and its refs read relative to the
open descriptor by plain names. Only the files backend is understood (loose
refs plus packed-refs); a reftable repository reports `"backend":
"reftable"` and no refs.
"""

from __future__ import annotations

import os
import stat
from typing import Callable

from .safefs import O_CLOEXEC, O_NOATIME, SafeRoot

GIT = ".git"

# Tests replace this to change the tree between discovering a directory and
# opening it.
_between_discovery_and_open: Callable[[str], None] = lambda rel: None


def _read_at(dfd: int, name: str, limit: int) -> bytes | None:
    if "/" in name:
        raise ValueError("a name, not a path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_CLOEXEC
    try:
        try:
            fd = os.open(name, flags | O_NOATIME, dir_fd=dfd)
        except PermissionError:
            fd = os.open(name, flags, dir_fd=dfd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit)
    finally:
        os.close(fd)


def scan_git(root: SafeRoot, watch: Callable[[str, int], None] | None = None, ignore_refs: list[str] | tuple = ()) -> dict | None:
    """The coarse state, or None without a `.git` directory. `watch(rel, fd)`
    is called for `.git` and every directory under `.git/refs`. Refs under
    an ignored namespace (`refs/t3/`, say) are left out, loose and packed,
    and their directories are neither watched nor read."""
    spaces = [ns.rstrip("/") for ns in ignore_refs]

    def skipped(name: str) -> bool:
        # A namespace is a whole path prefix: refs/t3 skips refs/t3 and
        # refs/t3/x, never refs/t30/x.
        return any(name == ns or name.startswith(ns + "/") for ns in spaces)

    def points_into_skipped(value: str) -> bool:
        # A symbolic ref ("ref: refs/t3/x") is filtered by its target too.
        return value.startswith("ref:") and skipped(value[4:].strip())

    try:
        gfd = root.open_dir(GIT)
    except OSError:
        return None
    try:
        if watch:
            watch(GIT, gfd)
        head_raw = _read_at(gfd, "HEAD", 1 << 16)
        head = head_raw.decode("utf-8", "replace").strip() if head_raw is not None else None
        if head is not None and points_into_skipped(head):
            head = None  # HEAD names an ignored ref: left out like the ref itself
        try:
            if stat.S_ISDIR(os.stat("reftable", dir_fd=gfd, follow_symlinks=False).st_mode):
                return {"head": head, "refs": {}, "backend": "reftable"}
        except OSError:
            pass
        refs: dict[str, str] = {}
        packed = _read_at(gfd, "packed-refs", 64 << 20)
        if packed:
            for line in packed.decode("utf-8", "replace").splitlines():
                if not line or line[0] in "#^":
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and not skipped(parts[1].strip()):
                    refs[parts[1].strip()] = parts[0].strip()
    finally:
        os.close(gfd)
    stack = ["refs"]
    while stack:
        rel = stack.pop()
        _between_discovery_and_open(f"{GIT}/{rel}")
        try:
            dfd = root.open_dir(f"{GIT}/{rel}")
        except OSError:
            continue
        try:
            if watch:
                watch(f"{GIT}/{rel}", dfd)
            for name in sorted(os.listdir(dfd)):
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if skipped(f"{rel}/{name}"):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(f"{rel}/{name}")
                elif stat.S_ISREG(st.st_mode) and not name.endswith(".lock"):
                    raw = _read_at(dfd, name, 1 << 16)
                    if raw is not None:
                        val = raw.decode("utf-8", "replace").strip()
                        if not points_into_skipped(val):
                            refs[f"{rel}/{name}"] = val
        finally:
            os.close(dfd)
    return {"head": head, "refs": dict(sorted(refs.items())), "backend": "files"}
