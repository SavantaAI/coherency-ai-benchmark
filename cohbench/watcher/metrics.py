"""The metric hook, run on a copy of the tree as of one batch.

The live workspace is never handed to the command. For each reading the
manifest of one committed batch is materialised from the blob store (a
mirror updated incrementally, then a fresh copy of it per run) and the
command runs in the copy.

Two executors:

  isolated  (the container image) The copy is built by a separate process
            running as its own unprivileged user, at the workspace's own path
            as that user sees it, so an absolute link into the workspace lands
            in the copy. That user cannot traverse the directory holding the
            live volume and the watcher's output. See isolate.py.
  local     (tests, development) The same materialisation in a directory of
            the watcher's own, run as the watcher's user. Not isolated.

In both, a symlink whose target lies outside the workspace is created
dangling and listed in `links_outside`; content that was not stored
(oversize, unreadable), the contents of inaccessible directories and the
coarse git state are absent and listed in `absent`, recomputed for the whole
snapshot on every reading. Cleanup never follows a symlink.

Every committed batch gets a reading, in batch order, from that batch's
own manifest (an immutable snapshot) and the blob store, so which batches
are measured never depends on timing. A batch whose copy would be identical
to one already measured (same tree digest) reuses that reading and says so
(`reused_from`). At shutdown the queue is drained for at most `drain_s`;
what is left is recorded as not measured (`reason: "backlog"`), and the
offline command completes it later with the same code. A reading is
`complete` only when the copy held every readable tracked file. Any
exception becomes a failed reading; the worker keeps going.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
import traceback
from collections import deque
from typing import Callable

from .blobs import BlobStore
from .manifest import Entry, Manifest, depth, entry_from
from .records import MetricsLog, utc_now

MAX_NUMBER_CHARS = 64
MAX_LIST = 1000
OUTSIDE_PREFIX = "/.outside-workspace/"
DEFAULT_WORKSPACE = "/work/project"
# Every entry of a metric copy gets this mtime (2000-01-01T00:00:00Z) unless
# the launch passes one, so the same tree always looks the same.
DEFAULT_COPY_MTIME = 946684800
# Reasons in `absent` that do not make a reading incomplete: content the
# workspace itself does not give (an unreadable file or link, an
# inaccessible directory) and the coarse git state.
NOT_LACKING = ("file, content unreadable", "symlink target unreadable", "inaccessible directory: contents unknown", "coarse git state (HEAD and refs only): not materialised")


# ---- strict JSON ------------------------------------------------------------


def _bad_constant(name: str):
    raise ValueError(f"{name} is not allowed")


def _int(s: str) -> int:
    if len(s.lstrip("-")) > MAX_NUMBER_CHARS:
        raise ValueError(f"integer with more than {MAX_NUMBER_CHARS} digits")
    return int(s)


def _float(s: str) -> float:
    if len(s) > MAX_NUMBER_CHARS:
        raise ValueError(f"number longer than {MAX_NUMBER_CHARS} characters")
    f = float(s)
    if not math.isfinite(f):
        raise ValueError(f"number {s} is not finite")
    return f


def parse_metrics(stdout: bytes) -> tuple[dict | None, str | None]:
    """stdout must be exactly one strict JSON object: no NaN or Infinity,
    numbers of at most 64 characters. Never raises."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, f"stdout is not UTF-8: {e}"
    if not text.strip():
        return None, "stdout is empty"
    try:
        obj = json.loads(text, parse_constant=_bad_constant, parse_int=_int, parse_float=_float)
    except (ValueError, RecursionError) as e:
        return None, f"stdout is not strict JSON: {e}"
    if not isinstance(obj, dict):
        return None, f"stdout is JSON but not an object ({type(obj).__name__})"
    return obj, None


def ranges(nums: list[int]) -> list[list[int]]:
    """Sorted integers as inclusive [from, to] ranges: complete at any length."""
    out: list[list[int]] = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def expand(rs: list[list[int]]) -> list[int]:
    return [n for a, b in rs for n in range(a, b + 1)]


# ---- safe filesystem helpers ------------------------------------------------

O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_dir_nofollow(name: str, dir_fd: int | None = None) -> int:
    return os.open(name, os.O_RDONLY | O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=dir_fd)


def _clear_fd(fd: int) -> None:
    for name in os.listdir(fd):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            try:
                cfd = _open_dir_nofollow(name, fd)
            except PermissionError:
                # It is a directory (lstat said so) owned by us; give us back
                # access to it. Never reached through a symlink.
                os.chmod(name, 0o700, dir_fd=fd)
                cfd = _open_dir_nofollow(name, fd)
            try:
                if os.fstat(cfd).st_ino != st.st_ino:
                    raise OSError(f"{name} changed while being removed")
                if stat.S_IMODE(os.fstat(cfd).st_mode) & 0o700 != 0o700:
                    os.fchmod(cfd, 0o700)
                _clear_fd(cfd)
            finally:
                os.close(cfd)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def clear_dir(path: str) -> None:
    """Remove everything inside `path` without following any symlink and
    without changing anything outside it."""
    fd = _open_dir_nofollow(path)
    try:
        _clear_fd(fd)
    finally:
        os.close(fd)


def _chmod_nofollow(path: str, mode: int, is_dir: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | O_CLOEXEC | (O_DIRECTORY if is_dir else 0)
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, mode)
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


# ---- materialisation ----------------------------------------------------------


def classify_link(path: str, target: str, workspace: str, m: Manifest | None = None) -> str | None:
    """Where the link at `path` resolves inside the workspace (a path relative
    to it), or None when it ends outside. One resolver for every target,
    direct or reached through other links: the walk keeps an absolute
    position and goes component by component in filesystem order. Inside the
    workspace, a component that is a symlink recorded in the manifest is
    replaced by its target before the next `..` is applied (up to 40 links);
    outside it, components are taken as plain directories (nothing outside
    is recorded). A component that does not exist, or a file with more
    components after it, ends the walk inside (the link dangles, but
    inside)."""
    ws = [c for c in workspace.split("/") if c]
    n = len(ws)
    pos = list(ws) + [c for c in posixpath.dirname(path).split("/") if c]
    todo: list[str] = []
    hops = 0

    def splice(t: str) -> None:
        nonlocal pos
        if t.startswith("/"):
            pos = []
        todo.extend(reversed([x for x in t.split("/") if x]))

    splice(target)
    while todo:
        c = todo.pop()
        if c == ".":
            continue
        if c == "..":
            if pos:
                pos.pop()  # "/.." is "/"
            continue
        pos.append(c)
        if len(pos) <= n or pos[:n] != ws:
            continue  # outside the workspace (or the workspace itself)
        rel = "/".join(pos[n:])
        e = m.entries.get(rel) if m is not None else None
        if e is not None and e.type == "symlink" and e.target is not None:
            hops += 1
            if hops > 40:
                return "/".join([rel, *reversed(todo)])  # a loop: dangles, reaches nothing
            pos.pop()
            splice(e.target)
        elif (e is None or e.type != "dir") and todo:
            # A file or a missing component in the middle: the link dangles,
            # inside. The rest of the path is kept, so a link made from this
            # resolution fails at the same component.
            return "/".join([rel, *reversed(todo)])
    if len(pos) >= n and pos[:n] == ws:
        return "/".join(pos[n:])
    return None


def omissions(m: Manifest, workspace: str, have_blob: Callable[[str], bool]) -> tuple[dict, dict]:
    """(absent, links_outside) for the whole snapshot."""
    absent: dict[str, str] = {}
    outside: dict[str, str] = {}
    for p, e in m.entries.items():
        if e.type == "dir":
            if e.content == "inaccessible":
                absent[p] = "inaccessible directory: contents unknown"
        elif e.type == "symlink":
            if e.target is None:
                absent[p] = "symlink target unreadable"
            elif classify_link(p, e.target, workspace, m) is None:
                outside[p] = e.target
        elif e.type == "file":
            if e.content != "stored" or not e.sha256:
                absent[p] = f"file, content {e.content}"
            elif not have_blob(e.sha256):
                absent[p] = "file, blob missing"
        else:
            absent[p] = f"{e.type}: not materialised"
    if m.git is not None:
        absent[".git"] = "coarse git state (HEAD and refs only): not materialised"
    return absent, outside


def lacking(absent: dict) -> list[str]:
    """Paths whose absence makes a reading incomplete: every readable
    tracked file the copy does not hold (oversize, blob missing). Other
    kinds of entry (fifos, sockets, devices) are not files and are never
    materialised."""
    return sorted(p for p, why in absent.items() if why not in NOT_LACKING and why.startswith("file"))


def tree_digest(m: Manifest, have_blob: Callable[[str], bool], settings: dict | None = None) -> str:
    """SHA-256 over what a copy of `m` holds and how the command runs on
    it: each path with its type, mode, content hash, link target and
    content state, whether its blob is present, whether a git state exists,
    and the run settings (command, isolation, workspace path, copy mtime).
    Owners, inode numbers and epochs are not in a copy and not in the
    digest. Two batches with the same digest get the same reading."""
    rows = []
    for p in sorted(m.entries):
        e = m.entries[p]
        have = bool(e.type == "file" and e.content == "stored" and e.sha256 and have_blob(e.sha256))
        rows.append([p, e.type, e.mode, e.sha256 if e.type in ("file", "symlink") else None, e.target, e.content, have])
    return hashlib.sha256(json.dumps([rows, m.git is not None, settings or {}], separators=(",", ":"), sort_keys=True).encode()).hexdigest()


class CopyMismatch(Exception):
    """The materialised copy is not the batch's tree."""


def _file_mode(e: Entry) -> int:
    return e.mode | stat.S_IRUSR


class Materializer:
    """Keeps `mirror` equal to a manifest, then copies it to `copy_path` and
    verifies the copy against the manifest before anything runs in it."""

    def __init__(self, work_dir: str, copy_path: str, workspace: str, rewrite_absolute: bool, copy_mtime: int = DEFAULT_COPY_MTIME) -> None:
        self.work = work_dir
        self.copy_mtime = copy_mtime
        self.mirror = os.path.join(work_dir, "mirror")
        os.makedirs(self.mirror, mode=0o700, exist_ok=True)
        self.copy_path = copy_path
        self.workspace = workspace.rstrip("/")
        # Local mode only: the copy is not at the workspace path, so absolute
        # links into the workspace are pointed at the copy instead.
        self.rewrite_absolute = rewrite_absolute
        self.state: dict[str, Entry] = {}
        self.links: dict[str, str] = {}  # path -> target as created
        self.manifest: Manifest | None = None

    @staticmethod
    def _key(e: Entry) -> tuple:
        # A directory's mode is applied to the copy, never to the mirror, so
        # a mode change alone does not make it (or its subtree) stale.
        if e.type == "dir":
            return ("dir", e.content)
        return (e.type, e.sha256, e.target, e.mode, e.content)

    def _rm(self, rel: str) -> None:
        p = os.path.join(self.mirror, rel)
        st = os.lstat(p) if os.path.lexists(p) else None
        if st is None:
            return
        if stat.S_ISDIR(st.st_mode):
            clear_dir(p)
            os.rmdir(p)
        else:
            os.unlink(p)

    def _plan(self, m: Manifest) -> tuple[list[str], list[str]]:
        """(stale top paths to remove, paths to create), with the subtree of
        every stale directory counted as removed."""
        target = m.entries
        stale = sorted((p for p, e in self.state.items() if p not in target or self._key(target[p]) != self._key(e) or (e.type == "symlink" and self.links.get(p) != self._link_target(p, target[p].target, m))), key=depth)
        removed: set[str] = set()
        tops: list[str] = []
        for p in stale:
            if p in removed:
                continue
            tops.append(p)
            removed.add(p)
            removed.update(k for k in self.state if k.startswith(p + "/"))
        remaining = set(self.state) - removed
        create = [p for p in sorted(target, key=lambda q: (depth(q), q)) if p not in remaining]
        return tops, create

    def needed(self, m: Manifest) -> list[str]:
        """Blob hashes the mirror needs to become `m`."""
        _, create = self._plan(m)
        return sorted({m.entries[p].sha256 for p in create if m.entries[p].type == "file" and m.entries[p].content == "stored" and m.entries[p].sha256})

    def _link_target(self, p: str, target: str, m: Manifest) -> str:
        inside = classify_link(p, target, self.workspace, m)
        if inside is None:
            return OUTSIDE_PREFIX + target.lstrip("/")
        if self.rewrite_absolute and target.startswith("/"):
            return os.path.join(self.copy_path, inside) if inside else self.copy_path
        return target

    def update(self, m: Manifest, get_blob: Callable[[str], bytes | None]) -> None:
        tops, create = self._plan(m)
        for p in tops:
            self._rm(p)
            for k in [k for k in self.state if k == p or k.startswith(p + "/")]:
                del self.state[k]
                self.links.pop(k, None)
        # Directories kept: take the new entry (its mode, for the copy).
        for p, e in m.entries.items():
            if p in self.state and e.type == "dir":
                self.state[p] = e
        for p in create:
            e = m.entries[p]
            par = p.rsplit("/", 1)[0] if "/" in p else ""
            if par and par not in self.state:
                continue
            full = os.path.join(self.mirror, p)
            if e.type == "dir":
                os.mkdir(full, 0o700)
                self.state[p] = e
            elif e.type == "symlink" and e.target is not None:
                t = self._link_target(p, e.target, m)
                os.symlink(t, full)
                self.state[p] = e
                self.links[p] = t
            elif e.type == "file" and e.content == "stored" and e.sha256:
                data = get_blob(e.sha256)
                if data is None:
                    continue
                fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fchmod(fd, _file_mode(e))
                finally:
                    os.close(fd)
                self.state[p] = e
        self.manifest = m

    def make_copy(self, absent: dict) -> str:
        """Copy the mirror to copy_path, set exact modes, and verify it
        against the manifest: every expected entry present with its type,
        mode, size, content hash or link target, and nothing else. Raises
        CopyMismatch."""
        m = self.manifest
        if m is None:
            raise CopyMismatch("no manifest materialised")
        expected: set[str] = set()
        for p in sorted(m.entries, key=lambda q: (depth(q), q)):
            e = m.entries[p]
            par = p.rsplit("/", 1)[0] if "/" in p else ""
            if par and par not in expected:
                continue
            if e.type == "dir" or (e.type == "symlink" and e.target is not None) or (e.type == "file" and p not in absent):
                expected.add(p)
        if set(self.state) != expected:
            missing = sorted(expected - set(self.state))[:5]
            extra = sorted(set(self.state) - expected)[:5]
            raise CopyMismatch(f"mirror differs from the manifest: missing {missing}, extra {extra}")
        os.makedirs(self.copy_path, mode=0o755, exist_ok=True)
        clear_dir(self.copy_path)
        shutil.copytree(self.mirror, self.copy_path, symlinks=True, dirs_exist_ok=True)
        for p, e in self.state.items():
            if e.type == "file":
                _chmod_nofollow(os.path.join(self.copy_path, p), _file_mode(e), False)
        # One fixed mtime for every entry, deepest first (setting a child's
        # time does not move its directory's), never through a link.
        t = (self.copy_mtime, self.copy_mtime)
        for p in sorted(self.state, key=depth, reverse=True):
            os.utime(os.path.join(self.copy_path, p), t, follow_symlinks=False)
        os.utime(self.copy_path, t, follow_symlinks=False)
        self._verify(m)
        for p, e in sorted(self.state.items(), key=lambda kv: depth(kv[0]), reverse=True):
            if e.type == "dir":
                got = _chmod_nofollow(os.path.join(self.copy_path, p), e.mode, True)
                if got != e.mode:
                    raise CopyMismatch(f"{p}: directory mode {got:04o}, want {e.mode:04o}")
        return self.copy_path

    def _verify(self, m: Manifest) -> None:
        seen: set[str] = set()
        stack = [""]
        while stack:
            d = stack.pop()
            fd = _open_dir_nofollow(os.path.join(self.copy_path, d) if d else self.copy_path)
            try:
                for name in os.listdir(fd):
                    rel = f"{d}/{name}" if d else name
                    seen.add(rel)
                    e = self.state.get(rel)
                    if e is None:
                        raise CopyMismatch(f"{rel}: in the copy, not in the batch")
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if int(st.st_mtime) != self.copy_mtime:
                        raise CopyMismatch(f"{rel}: mtime {int(st.st_mtime)}, want {self.copy_mtime}")
                    if stat.S_ISDIR(st.st_mode):
                        if e.type != "dir":
                            raise CopyMismatch(f"{rel}: a directory, want {e.type}")
                        stack.append(rel)
                    elif stat.S_ISLNK(st.st_mode):
                        if e.type != "symlink" or os.readlink(name, dir_fd=fd) != self.links.get(rel):
                            raise CopyMismatch(f"{rel}: link target differs")
                    elif stat.S_ISREG(st.st_mode):
                        if e.type != "file" or stat.S_IMODE(st.st_mode) != _file_mode(e) or st.st_size != e.size:
                            raise CopyMismatch(f"{rel}: file type, mode or size differs")
                        ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=fd)
                        try:
                            h = hashlib.sha256()
                            while True:
                                b = os.read(ffd, 1 << 20)
                                if not b:
                                    break
                                h.update(b)
                        finally:
                            os.close(ffd)
                        if h.hexdigest() != e.sha256:
                            raise CopyMismatch(f"{rel}: content differs from the batch")
                    else:
                        raise CopyMismatch(f"{rel}: unexpected file type in the copy")
            finally:
                os.close(fd)
        if seen != set(self.state):
            raise CopyMismatch(f"missing from the copy: {sorted(set(self.state) - seen)[:5]}")

    def discard(self) -> None:
        if os.path.lexists(self.copy_path):
            clear_dir(self.copy_path)


def clean_env(home: str) -> dict:
    """The metric command's whole environment, the same in every executor."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def execute(cmd: list[str], cwd: str, timeout: float, stdout_max: int, stderr_max: int, env: dict | None = None) -> dict:
    timed_out = False
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, start_new_session=True, close_fds=True, env=env)
        except OSError as e:
            return dict(exit=None, timed_out=False, ok=False, metrics=None, error=f"could not start: {e}", parse_error=None, stderr="", stderr_truncated=False)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = proc.wait()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        fo.seek(0)
        out = fo.read(stdout_max + 1)
        fe.seek(0)
        err = fe.read(stderr_max + 1)
    metrics, perr = None, None
    if not timed_out:
        if len(out) > stdout_max:
            perr = f"stdout exceeds {stdout_max} bytes"
        else:
            metrics, perr = parse_metrics(out)
    error = "timeout" if timed_out else None
    if error is None and code != 0:
        error = f"exit status {code}"
    if error is None and perr is not None:
        error = perr
    return dict(exit=code, timed_out=timed_out, ok=error is None, metrics=metrics, error=error, parse_error=perr, stderr=err[:stderr_max].decode("utf-8", "replace"), stderr_truncated=len(err) > stderr_max)


def kill_other_processes_of_my_uid() -> int:
    """Kill every other process of this real uid (a metric command that
    left the session). Returns how many were signalled."""
    me, uid, n = os.getpid(), os.getuid(), 0
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            os.kill(int(name), signal.SIGKILL)
                            n += 1
                        break
        except (OSError, ValueError):
            continue
    return n


# ---- executors -----------------------------------------------------------------


class LocalExecutor:
    """Same process, same user. For tests and development only."""

    isolation = "none"

    def __init__(self, work_dir: str, blobs: BlobStore, workspace: str, copy_mtime: int = DEFAULT_COPY_MTIME) -> None:
        self.blobs = blobs
        self.workspace = workspace
        self.mat = Materializer(work_dir, os.path.join(work_dir, "tree"), workspace, rewrite_absolute=True, copy_mtime=copy_mtime)

    @property
    def copy_mtime(self) -> int:
        return self.mat.copy_mtime

    @copy_mtime.setter
    def copy_mtime(self, value: int) -> None:
        self.mat.copy_mtime = value

    def run(self, m: Manifest, cmd: list[str], timeout: float, stdout_max: int, stderr_max: int) -> dict:
        absent, outside = omissions(m, self.workspace, self.blobs.has)
        self.mat.update(m, lambda sha: self.blobs.get(sha) if self.blobs.has(sha) else None)
        try:
            tree = self.mat.make_copy(absent)
        except CopyMismatch as e:
            self.mat.discard()
            return dict(exit=None, timed_out=False, ok=False, metrics=None, error=f"copy verification failed: {e}", parse_error=None, stderr="", stderr_truncated=False, absent=absent, links_outside=outside)
        try:
            r = execute(cmd, tree, timeout, stdout_max, stderr_max, env=clean_env(self.mat.work))
        finally:
            self.mat.discard()
        r.update(absent=absent, links_outside=outside, cwd=tree)
        return r

    def close(self) -> None:
        pass


def _send(fd: int, data: bytes) -> None:
    msg = struct.pack(">Q", len(data)) + data
    view = memoryview(msg)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _recv(fd: int) -> bytes:
    def exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise EOFError("metric server pipe closed")
            buf += chunk
        return bytes(buf)

    (n,) = struct.unpack(">Q", exact(8))
    return exact(n)


class IsolatedExecutor:
    """Talks to the metric server (isolate.serve) over two pipes."""

    isolation = "users"

    def __init__(self, to_server: int, from_server: int, blobs: BlobStore, copy_mtime: int = DEFAULT_COPY_MTIME, workspace: str = DEFAULT_WORKSPACE) -> None:
        self.copy_mtime = copy_mtime
        self.workspace = workspace
        self.to = to_server
        self.frm = from_server
        self.blobs = blobs
        self.lock = threading.Lock()
        self.broken: str | None = None

    def run(self, m: Manifest, cmd: list[str], timeout: float, stdout_max: int, stderr_max: int) -> dict:
        if self.broken:
            raise RuntimeError(f"metric server unavailable: {self.broken}")
        job = {"cmd": cmd, "timeout": timeout, "stdout_max": stdout_max, "stderr_max": stderr_max, "entries": [[p, e.fields()] for p, e in m.entries.items()], "git": m.git is not None, "mtime": self.copy_mtime}
        with self.lock:
            try:
                _send(self.to, json.dumps(job).encode())
                need = json.loads(_recv(self.frm))["need"]
                # Only contents of the submitted manifest are ever served.
                allowed = {e.sha256 for e in m.entries.values() if e.type == "file" and e.content == "stored" and e.sha256}
                if not isinstance(need, list) or any(sha not in allowed for sha in need):
                    raise ValueError("the metric server asked for a blob outside the submitted manifest")
                for sha in need:
                    # A presence byte, then the bytes: no content is special.
                    _send(self.to, b"\x01" + self.blobs.get(sha) if self.blobs.has(sha) else b"\x00")
                return json.loads(_recv(self.frm))
            except (OSError, EOFError, ValueError) as e:
                self.broken = f"{type(e).__name__}: {e}"
                raise

    def close(self) -> None:
        for fd in (self.to, self.frm):
            try:
                os.close(fd)
            except OSError:
                pass


def serve(from_watcher: int, to_watcher: int, workspace: str, work_dir: str, kill_strays: bool = False) -> None:
    """The metric server loop: runs as the metric user, never sees the live
    volume or the output directory. Exits when the watcher closes its pipe.
    kill_strays (only as the dedicated metric user) kills every other process
    of that user after each run."""
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        pass  # not the main thread (tests run the server in a thread)
    copy_path = os.path.realpath(workspace)
    os.makedirs(copy_path, mode=0o755, exist_ok=True)
    mat = Materializer(work_dir, copy_path, workspace, rewrite_absolute=False)
    env = clean_env(work_dir)
    while True:
        try:
            job = json.loads(_recv(from_watcher))
        except EOFError:
            return
        try:
            m = Manifest.of({p: entry_from(dict(f, seq=0)) for p, f in job["entries"]})
            if job.get("git"):
                m.git = {"head": None, "refs": {}, "backend": None}
            mat.copy_mtime = int(job.get("mtime", DEFAULT_COPY_MTIME))
            need = mat.needed(m)
            _send(to_watcher, json.dumps({"need": need}).encode())
            blobs: dict[str, bytes | None] = {}
            for sha in need:
                data = _recv(from_watcher)
                blobs[sha] = data[1:] if data[:1] == b"\x01" else None
            absent, outside = omissions(m, workspace, lambda sha: blobs.get(sha, b"") is not None)
            mat.update(m, lambda sha: blobs.get(sha))
            blobs.clear()
            try:
                mat.make_copy(absent)
            except CopyMismatch as e:
                mat.discard()
                r = dict(exit=None, timed_out=False, ok=False, metrics=None, error=f"copy verification failed: {e}", parse_error=None, stderr="", stderr_truncated=False, absent=absent, links_outside=outside)
                _send(to_watcher, json.dumps(r).encode())
                continue
            try:
                r = execute(job["cmd"], workspace, job["timeout"], job["stdout_max"], job["stderr_max"], env=env)
            finally:
                r_killed = kill_other_processes_of_my_uid() if kill_strays else 0
                mat.discard()
            r.update(absent=absent, links_outside=outside, cwd=workspace, stragglers_killed=r_killed)
        except EOFError:
            return
        except Exception as e:
            r = dict(exit=None, timed_out=False, ok=False, metrics=None, error=f"internal (metric server): {type(e).__name__}: {e}", parse_error=None, stderr="", stderr_truncated=False, trace=traceback.format_exc()[-4000:])
        _send(to_watcher, json.dumps(r).encode())


# ---- the runner (in the watcher) -------------------------------------------------


REUSABLE_KEYS = ("exit", "timed_out", "ok", "metrics", "error", "parse_error", "stderr", "stderr_truncated", "absent", "absent_count", "links_outside", "links_outside_count", "complete", "lacking", "cwd")


class MetricRunner:
    """One reading per committed batch, in batch order.

    `enqueue(batch, manifest)` is called for every committed batch with that
    batch's own manifest (never changed afterwards). The worker measures the
    queue front to back. `reading(batch)` gives the batch's reading once it
    is written (or its backlog record)."""

    def __init__(self, cmd: list[str], timeout_s: float, stdout_max: int, stderr_max: int, log: MetricsLog, executor, blobs: BlobStore | None = None, offline: bool = False, first_run: int = 1) -> None:
        self.cmd = list(cmd)
        self.timeout = timeout_s
        self.stdout_max = stdout_max
        self.stderr_max = stderr_max
        self.log = log
        self.executor = executor
        self.blobs = blobs if blobs is not None else executor.blobs
        self.offline = offline
        self.runs = first_run - 1  # run numbers continue across a completion
        self.failures = 0
        self.reused = 0
        self._current: int | None = None  # batch being measured
        self._abandoned: set[int] = set()  # given up on at the shutdown deadline
        self.readings: dict[int, dict] = {}  # batch -> summary of its reading
        self.by_digest: dict[str, dict] = {}  # tree digest -> a reusable reading
        self.backlog: list[int] = []
        self.last_enqueued = -1
        self._cond = threading.Condition(threading.RLock())
        self._queue: deque = deque()
        self._running = False
        self._stop = False
        self._thread: threading.Thread | None = None

    @property
    def isolation(self) -> str:
        return self.executor.isolation

    def settings(self) -> dict:
        """What besides the tree decides a reading (part of the reuse key)."""
        # Numbers normalised, so 7 and 7.0 give the same key.
        return {"cmd": list(self.cmd), "timeout": float(self.timeout), "stdout_max": int(self.stdout_max), "stderr_max": int(self.stderr_max), "isolation": self.executor.isolation, "workspace": getattr(self.executor, "workspace", None), "copy_mtime": int(getattr(self.executor, "copy_mtime", DEFAULT_COPY_MTIME))}

    @property
    def measured(self) -> set[int]:
        return {b for b, r in self.readings.items() if r.get("measured")}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="metric", daemon=True)
        self._thread.start()

    def enqueue(self, batch: int, manifest: Manifest) -> None:
        with self._cond:
            if batch != self.last_enqueued + 1:
                raise RuntimeError(f"internal: batch {batch} queued after {self.last_enqueued}")
            if self._stop:
                raise RuntimeError("internal: batch queued after the metric worker stopped")
            self.last_enqueued = batch
            self._queue.append((batch, manifest))
            self._cond.notify_all()

    def queued(self) -> int:
        with self._cond:
            return len(self._queue) + (1 if self._running else 0)

    def idle(self) -> bool:
        with self._cond:
            return not self._running and not self._queue

    def reading(self, batch: int) -> dict | None:
        """The batch's reading if it has been written, else None."""
        with self._cond:
            r = self.readings.get(batch)
            return dict(r) if r is not None else None

    def finish_one(self) -> None:
        """Measure the queue front in this thread (the baseline, before the
        worker starts)."""
        with self._cond:
            batch, man = self._queue.popleft()
        self.measure(batch, man)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stop and not self._queue:
                    self._cond.wait()
                if self._stop:
                    return
                batch, man = self._queue.popleft()
                self._running = True
                self._current = batch
            try:
                self.measure(batch, man)
            except Exception:  # measure never raises; this is the last guard
                self._internal_failure(traceback.format_exc())
            finally:
                with self._cond:
                    self._running = False
                    self._current = None
                    self._cond.notify_all()

    def finish(self, drain_s: float) -> list[int]:
        """Measure what is queued for at most drain_s seconds, then let the
        job in progress finish, all within one deadline: drain_s plus
        metric_timeout_s from now, covering the copy, the protocol and the
        command. A job still running at that deadline is given up (its
        reading is never written) and recorded, with every batch left, as
        not measured with the reason `backlog`. Returns those batches."""
        start = time.monotonic()
        drain_end, hard_end = start + drain_s, start + drain_s + self.timeout
        stuck: int | None = None
        with self._cond:
            while (self._queue or self._running) and self._thread is not None:
                left = drain_end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(timeout=min(left, 0.5))
            self._stop = True
            self._cond.notify_all()
            while self._running:
                left = hard_end - time.monotonic()
                if left <= 0:
                    stuck = self._current
                    if stuck is not None:
                        self._abandoned.add(stuck)
                    break
                self._cond.wait(timeout=min(left, 0.5))
            left_over = list(self._queue)
            self._queue.clear()
        if self._thread is not None and stuck is None:
            self._thread.join(timeout=10)
        if stuck is not None:
            self._not_measured(stuck, "backlog", f"not measured: still running at the shutdown deadline (metric_drain_s {drain_s:g} s plus metric_timeout_s {self.timeout:g} s)")
        if self._thread is None:
            # Never started (tests, offline): measure in this thread.
            for batch, man in left_over:
                self.measure(batch, man)
            left_over = []
        for batch, _ in left_over:
            self._not_measured(batch, "backlog", f"not measured: the queue was not drained within metric_drain_s ({drain_s:g} s)")
        return ([stuck] if stuck is not None else []) + [b for b, _ in left_over]

    def _not_measured(self, batch: int, why: str, error: str) -> dict:
        rec = {"run": None, "batch": batch, "reason": why, "measured": False, "ok": False, "complete": False, "metrics": None, "error": error, "end": utc_now()}
        self.backlog.append(batch)
        try:
            self.log.write([rec])
            rec["logged"] = True
        except Exception as e:
            rec["logged"] = False
            rec["error"] += f"; could not write the record: {type(e).__name__}: {e}"
        self._done(batch, rec)
        return rec

    def _done(self, batch: int, rec: dict) -> None:
        summary = {k: rec.get(k) for k in ("run", "batch", "ok", "error", "metrics", "logged", "complete", "reused_from", "measured")}
        with self._cond:
            self.readings[batch] = summary

    def _internal_failure(self, tb: str) -> None:
        self.failures += 1
        try:
            self.log.write([{"run": None, "reason": "internal", "ok": False, "error": "metric worker exception", "trace": tb[-4000:], "end": utc_now()}])
        except Exception:
            pass

    def measure(self, batch: int, manifest: Manifest) -> dict:
        """The reading of one batch: reused when an identical tree was
        already measured, else a run. Never raises."""
        try:
            digest = tree_digest(manifest, self.blobs.has, self.settings())
        except Exception:
            digest = None
        prev = self.by_digest.get(digest) if digest else None
        if prev is not None:
            rec = {"run": None, "batch": batch, "reason": "baseline" if batch == 0 else "change", "cmd": self.cmd, "isolation": self.executor.isolation, "tree_sha256": digest, "reused_from": prev["batch"], "reused_run": prev["run"], "measured": True}
            rec.update({k: prev[k] for k in REUSABLE_KEYS if k in prev})
            rec["start"] = rec["end"] = utc_now()
            rec["duration_s"] = 0.0
            if self.offline:
                rec["offline"] = True
            self.reused += 1
            return self._write(batch, rec, ran=True)
        return self.run_once(batch, manifest, digest=digest)

    def run_once(self, batch: int, manifest: Manifest, reason: str | None = None, digest: str | None = None) -> dict:
        self.runs += 1
        reason = reason or ("baseline" if batch == 0 else "change")
        if digest is None:
            try:
                digest = tree_digest(manifest, self.blobs.has, self.settings())
            except Exception:
                digest = None
        rec: dict = {"run": self.runs, "batch": batch, "reason": reason, "cmd": self.cmd, "start": utc_now(), "isolation": self.executor.isolation, "tree_sha256": digest, "reused_from": None}
        if self.offline:
            rec["offline"] = True
        t0 = time.monotonic()
        ran = False
        try:
            r = self.executor.run(manifest, self.cmd, self.timeout, self.stdout_max, self.stderr_max)
            # The command ran (to an exit status or a timeout), as opposed to
            # a failure to prepare the copy or to start it.
            ran = r.get("exit") is not None or bool(r.get("timed_out"))
            absent = r.pop("absent", {})
            outside = r.pop("links_outside", {})
            lack = lacking(absent)
            rec["absent"] = dict(sorted(absent.items())[:MAX_LIST])
            rec["absent_count"] = len(absent)
            rec["links_outside"] = dict(sorted(outside.items())[:MAX_LIST])
            rec["links_outside_count"] = len(outside)
            rec.update(r)
            rec["complete"] = ran and not lack
            rec["lacking"] = lack[:MAX_LIST]
        except Exception as e:
            rec.update(exit=None, timed_out=False, ok=False, metrics=None, error=f"internal: {type(e).__name__}: {e}", parse_error=None, stderr="", stderr_truncated=False, trace=traceback.format_exc()[-4000:], complete=False)
        rec["measured"] = ran
        rec["end"] = utc_now()
        rec["duration_s"] = round(time.monotonic() - t0, 6)
        out = self._write(batch, rec, ran)
        # Reusable: the command ran to an exit status (a timeout depends on
        # the machine) and the reading was written.
        if ran and not rec.get("timed_out") and out.get("logged") and digest:
            self.by_digest.setdefault(digest, out)
        return out

    def _write(self, batch: int, rec: dict, ran: bool) -> dict:
        with self._cond:  # an RLock: _done below takes it again
            if batch in self._abandoned:
                # Given up at the shutdown deadline: its backlog record stands.
                rec["logged"] = False
                return rec
            try:
                self.log.write([rec])
                rec["logged"] = True
            except Exception as e:
                rec["logged"] = False
                rec["ok"] = False
                rec["measured"] = False
                rec["error"] = (rec.get("error") or "") + f"; could not write the reading: {type(e).__name__}: {e}"
        if not rec.get("ok"):
            self.failures += 1
        self._done(batch, rec)
        return rec
