"""Path resolution that never leaves the root and never follows a symlink.

Every stat, readlink, open and listing of the watched tree goes through a
file descriptor for the root. On Linux with openat2 (5.6 and later) a path
is resolved in one call with RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS; without it, one component at a time with openat and
O_NOFOLLOW | O_DIRECTORY. A symlink anywhere in a path, including an
intermediate directory replaced by a symlink to somewhere else, makes the
path unreachable (None), so nothing outside the root is ever read.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys

O_PATH = getattr(os, "O_PATH", 0)
O_NOATIME = getattr(os, "O_NOATIME", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
SYS_OPENAT2 = 437  # the same number on every architecture that has it

# Errors that mean "this path is not (or no longer) a reachable entry of the tree".
GONE = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EXDEV)


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


def _components(rel: str) -> list[str]:
    parts = rel.split("/")
    for c in parts:
        if c in ("", ".", "..") or "\0" in c:
            raise ValueError(f"bad relative path {rel!r}")
    return parts


class SafeRoot:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        # O_PATH where available: an O_PATH open raises no inotify event.
        self.fd = os.open(self.root, (O_PATH or os.O_RDONLY) | O_DIRECTORY | O_CLOEXEC)
        self._syscall = None
        self.method = "openat-walk"
        if sys.platform.startswith("linux"):
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                self._syscall = libc.syscall
                self._syscall.restype = ctypes.c_long
                fd = self._openat2(".", O_PATH | O_DIRECTORY)
                os.close(fd)
                self.method = "openat2"
            except OSError as e:
                if e.errno not in (errno.ENOSYS, errno.EPERM):
                    raise
                self._syscall = None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _openat2(self, rel: str, flags: int) -> int:
        how = _OpenHow(flags | O_CLOEXEC, 0, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)
        fd = self._syscall(ctypes.c_long(SYS_OPENAT2), ctypes.c_int(self.fd), ctypes.c_char_p(os.fsencode(rel)), ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e), rel)
        return int(fd)

    def open(self, rel: str, flags: int) -> int:
        """Open `rel` beneath the root. Raises OSError; GONE errnos mean
        the path is not reachable without a symlink."""
        if rel == "":
            return os.open(".", flags | O_CLOEXEC, dir_fd=self.fd)
        comps = _components(rel)
        if self._syscall is not None:
            return self._openat2(rel, flags | os.O_NOFOLLOW)
        cur = self.fd
        opened: list[int] = []
        try:
            for c in comps[:-1]:
                cur = os.open(c, (O_PATH or os.O_RDONLY) | O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=cur)
                opened.append(cur)
            return os.open(comps[-1], flags | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=cur)
        finally:
            for fd in opened:
                os.close(fd)

    def open_parent(self, rel: str) -> tuple[int, str]:
        """(fd of the parent directory, final name). The caller closes fd."""
        i = rel.rfind("/")
        par, name = (rel[:i], rel[i + 1 :]) if i > 0 else ("", rel)
        _components(rel)
        if par == "":
            return os.dup(self.fd), name
        return self.open(par, (O_PATH or os.O_RDONLY) | O_DIRECTORY), name

    def lstat(self, rel: str) -> tuple[os.stat_result | None, str | None]:
        """(stat, None), (None, None) when unreachable, (None, errno name) on EACCES and the like."""
        if rel == "":
            return os.fstat(self.fd), None
        try:
            pfd, name = self.open_parent(rel)
        except OSError as e:
            return None, (None if e.errno in GONE else errno.errorcode.get(e.errno or 0, str(e.errno)))
        try:
            return os.stat(name, dir_fd=pfd, follow_symlinks=False), None
        except OSError as e:
            return None, (None if e.errno in GONE else errno.errorcode.get(e.errno or 0, str(e.errno)))
        finally:
            os.close(pfd)

    def open_dir(self, rel: str) -> int:
        return self.open(rel, os.O_RDONLY | O_DIRECTORY)


def is_dir(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode)
