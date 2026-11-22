"""inotify through ctypes (Linux only), plus the event constants.

The constants and `parse` are platform independent so the event
classification can be tested on any host with synthetic event sequences.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct
from dataclasses import dataclass

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000
IN_ISDIR = 0x40000000

IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

# What every directory watch asks for. No IN_ACCESS or IN_OPEN: reads are not
# changes, and the metric command's own reads would otherwise feed back.
WATCH_MASK = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)

_HDR = struct.Struct("iIII")


@dataclass(frozen=True)
class RawEvent:
    wd: int
    mask: int
    cookie: int
    name: str  # decoded with surrogateescape; "" for events on the watch itself


def parse(buf: bytes) -> list[RawEvent]:
    """Split a read() buffer into events."""
    out = []
    off = 0
    n = len(buf)
    while off + _HDR.size <= n:
        wd, mask, cookie, ln = _HDR.unpack_from(buf, off)
        off += _HDR.size
        if off + ln > n:
            raise ValueError("truncated inotify event")
        raw = buf[off : off + ln].split(b"\0", 1)[0]
        off += ln
        out.append(RawEvent(wd, mask, cookie, os.fsdecode(raw)))
    if off != n:
        raise ValueError("trailing bytes in inotify buffer")
    return out


def describe(mask: int) -> str:
    names = [k for k, v in globals().items() if k.startswith("IN_") and k not in ("IN_NONBLOCK", "IN_CLOEXEC") and v and mask & v == v and v & (v - 1) == 0]
    return "|".join(sorted(names))


class Inotify:
    """One inotify instance. Not thread safe; the watcher uses it from one thread."""

    def __init__(self) -> None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        self._libc = ctypes.CDLL(name, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"inotify_init1: {os.strerror(e)}")
        self.fd = fd
        self.capped = False  # the last read stopped at its buffer cap

    def add_watch(self, path: str, mask: int = WATCH_MASK) -> int:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"inotify_add_watch {path!r}: {os.strerror(e)}")
        return wd

    def add_watch_fd(self, fd: int, mask: int = WATCH_MASK & ~IN_DONT_FOLLOW) -> int:
        """Watch the directory open on `fd`. The path given to the kernel is
        /proc/self/fd/N, which resolves to exactly that inode, so a symlink
        swapped into the tree between resolution and the watch cannot
        redirect it. IN_DONT_FOLLOW is left out because the magic link itself
        must be followed; IN_ONLYDIR still refuses anything but a directory."""
        return self.add_watch(f"/proc/self/fd/{fd}", mask)

    def rm_watch(self, wd: int) -> None:
        if self._libc.inotify_rm_watch(self.fd, wd) < 0:
            e = ctypes.get_errno()
            if e != errno.EINVAL:  # already gone
                raise OSError(e, f"inotify_rm_watch: {os.strerror(e)}")

    def read(self, max_buffers: int = 4) -> list[RawEvent]:
        """Queued events, non-blocking, at most `max_buffers` reads of 1 MiB
        (about 50,000 events) per call so a flood cannot starve deadlines,
        the heartbeat or a stop request. [] when none."""
        events: list[RawEvent] = []
        self.capped = False
        for _ in range(max_buffers):
            try:
                buf = os.read(self.fd, 1 << 20)
            except BlockingIOError:
                return events
            if not buf:
                return events
            events.extend(parse(buf))
        # Stopped at the cap: more may remain, so this read does not show
        # that the queue was empty.
        self.capped = True
        return events

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
