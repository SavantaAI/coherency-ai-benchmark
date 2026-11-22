"""Writable storage private to one metric invocation and its descendants."""

import ctypes
import json
import os
import sys


def private_mounts(paths):
    """Unprivileged user and mount namespaces; no container capability added."""
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(value):
        if value < 0:
            raise OSError(ctypes.get_errno(), "metric storage namespace")
        return value

    uid, gid = os.getuid(), os.getgid()
    cwd = os.getcwd()
    saved = []
    try:
        checked(libc.unshare(0x10000000 | 0x00020000))  # NEWUSER, NEWNS
        for name, value in (("setgroups", "deny"), ("uid_map", f"{uid} {uid} 1"), ("gid_map", f"{gid} {gid} 1")):
            with open("/proc/self/" + name, "w") as f:
                f.write(value)
        checked(libc.mount(None, b"/", None, (1 << 18) | (1 << 14), None))  # private, recursive
        for path in paths:
            fd = checked(libc.syscall(428, -100, os.fsencode(path), 1 | os.O_CLOEXEC))
            saved.append((path, fd))
        # Mark every existing mount read-only, including writable mounts
        # outside the usual temporary directories. The detached copies above
        # keep only the invocation's storage and workspace writable.
        attr = (ctypes.c_uint64 * 4)(1, 0, 0, 0)
        checked(libc.syscall(442, -100, b"/", 0x8000, ctypes.byref(attr), ctypes.sizeof(attr)))
        for path in ("/tmp", "/var/tmp", "/dev/shm"):
            if os.path.isdir(path):
                checked(libc.mount(b"tmpfs", os.fsencode(path), b"tmpfs", 2 | 4, b"mode=1777,size=1g"))
        for path, fd in saved:
            os.makedirs(path, mode=0o700, exist_ok=True)
            checked(libc.syscall(429, fd, b"", -100, os.fsencode(path), 4))
        os.chdir(cwd)
        # Do not let the measured command keep this namespace's capabilities.
        header = (ctypes.c_uint32 * 2)(0x20080522, 0)
        data = (ctypes.c_uint32 * 6)()
        checked(libc.capset(ctypes.byref(header), ctypes.byref(data)))
    finally:
        for _, fd in saved:
            os.close(fd)


def command(cmd, paths):
    paths = [os.path.realpath(p) for p in paths]
    if sys.platform == "linux":
        return [sys.executable, os.path.abspath(__file__), json.dumps(paths), *cmd]
    if sys.platform == "darwin":
        rules = " ".join("(subpath " + json.dumps(p) + ")" for p in paths)
        profile = '(version 1) (allow default) (deny file-write*) (allow file-write* (literal "/dev/null") ' + rules + ')'
        return ["/usr/bin/sandbox-exec", "-p", profile, *cmd]
    raise OSError("metric write confinement is supported on Linux and macOS")


if __name__ == "__main__":
    # An exec helper avoids running Python after fork in the metric worker's
    # threaded parent. The measured command replaces this helper's argv.
    private_mounts(json.loads(sys.argv[1]))
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
