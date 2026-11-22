"""Source for the public sync profiler and its ordinary run history."""

PROFILE = r'''#!/usr/bin/env python3
"""Run the sync against the sample data and show what it costs."""

import builtins
import hashlib
import io
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pwd
import sys
import threading
import time

sys.dont_write_bytecode = True
# Read source even if a previous unit run left bytecode behind.
sys.pycache_prefix = os.devnull
START_MS = time.time_ns() // 1_000_000
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
READ_PATHS = set()
CONSUMED = {}
READING_INPUTS = False
UNCERTAIN = False
ABSENT = "absent"


def workspace_files():
    return {path.resolve() for path in ROOT.rglob("*") if path.is_file()}


def signature(path):
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None


# Capture the baseline before imports, including potential lazy dependencies.
BEFORE = {path: signature(path) for path in workspace_files()}


def input_bytes(path):
    global UNCERTAIN
    path = Path(path).resolve()
    data = path.read_bytes()
    value = hashlib.sha256(data).digest()
    if path in CONSUMED and CONSUMED[path] != value:
        UNCERTAIN = True
    CONSUMED[path] = value
    return data


def read_json(path):
    return json.loads(input_bytes(path))


class SourceInputs(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        data = input_bytes(self.path)
        return self.source_to_code(data, self.path)


class ProjectImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
                and Path(spec.origin).resolve().is_relative_to(ROOT)):
            spec.loader = SourceInputs(fullname, spec.origin)
            return spec
        return None


sys.meta_path.insert(0, ProjectImports())


OPEN_SCOPE = threading.local()
RAW_OPEN = os.open


def descriptor_path(fd):
    try:
        if sys.platform == "darwin":
            import fcntl
            # Darwin F_GETPATH returns the open descriptor's absolute path.
            name = os.fsdecode(fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0])
        else:
            name = os.readlink("/proc/self/fd/%d" % fd)
        return Path(name).resolve() if os.path.isabs(name) else None
    except (OSError, ValueError):
        return None


def open_at(path, flags, mode=0o777, *, dir_fd=None):
    try:
        name = os.fsdecode(path)
        base = descriptor_path(dir_fd) if dir_fd is not None and not os.path.isabs(name) else Path.cwd()
        effective = (base / name).resolve() if base is not None else None
    except (OSError, TypeError, ValueError):
        effective = None
    previous = getattr(OPEN_SCOPE, "effective", ())
    OPEN_SCOPE.effective = effective
    try:
        return RAW_OPEN(path, flags, mode, dir_fd=dir_fd)
    finally:
        if previous == ():
            del OPEN_SCOPE.effective
        else:
            OPEN_SCOPE.effective = previous


os.open = open_at


RAW_IO_OPEN = io.open


def open_file(file, mode="r", buffering=-1, encoding=None, errors=None,
              newline=None, closefd=True, opener=None):
    if opener is None:
        return RAW_IO_OPEN(file, mode, buffering, encoding, errors, newline, closefd)

    def resolved_opener(path, flags):
        fd = opener(path, flags)
        if READING_INPUTS and flags & os.O_ACCMODE != os.O_WRONLY:
            track_path(descriptor_path(fd))
        return fd

    previous = getattr(OPEN_SCOPE, "skip_next", False)
    OPEN_SCOPE.skip_next = True
    try:
        return RAW_IO_OPEN(file, mode, buffering, encoding, errors, newline, closefd, resolved_opener)
    finally:
        OPEN_SCOPE.skip_next = previous


builtins.open = io.open = open_file


def track_path(path):
    global UNCERTAIN
    if path is None:
        UNCERTAIN = True
    elif path.is_relative_to(ROOT) and not path.is_dir():
        READ_PATHS.add(path)


def track_reads(event, args):
    global UNCERTAIN
    if not READING_INPUTS:
        return
    try:
        if event == "open" and getattr(OPEN_SCOPE, "skip_next", False):
            OPEN_SCOPE.skip_next = False
            return
        if event == "open" and args[2] & os.O_ACCMODE != os.O_WRONLY:
            if hasattr(OPEN_SCOPE, "effective"):
                path = OPEN_SCOPE.effective
            elif isinstance(args[0], int):
                path = descriptor_path(args[0])
            else:
                path = Path(os.fsdecode(args[0])).resolve()
            track_path(path)
        elif event == "mmap.__new__":
            if args[0] != -1:
                track_path(descriptor_path(args[0]))
        elif event in ("subprocess.Popen", "os.fork", "os.posix_spawn", "ctypes.dlopen", "ctypes.dlsym"):
            # Child and native reads cannot be attributed by this process's audit.
            UNCERTAIN = True
    except (OSError, ValueError, TypeError, RuntimeError):
        UNCERTAIN = True


sys.addaudithook(track_reads)


def state_digest():
    if UNCERTAIN:
        return None
    paths = set(READ_PATHS)
    digest = hashlib.sha256()
    for path in sorted(paths):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = None
        except OSError:
            return None
        value = ABSENT if data is None else hashlib.sha256(data).digest()
        if BEFORE.get(path, ABSENT) != value or CONSUMED.get(path, value) != value:
            return None
        name = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name)
        if data is None:
            digest.update(((1 << 64) - 1).to_bytes(8, "big"))
        else:
            digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def remember(result):
    try:
        row = {"start_ms": START_MS, "end_ms": time.time_ns() // 1_000_000,
               "state": state_digest(), "result": result}
        base = os.environ.get("XDG_STATE_HOME")
        state_home = Path(base) if base else Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state"
        path = state_home / "profile/runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with path.open("ab", buffering=0) as stream:
            stream.write(line)
    except (OSError, ValueError):
        pass


def main():
    from vendor.__MODULE__ import __CLS__
    import __PKG__

    client = __CLS__(os.path.join(ROOT, "sample"), read_json=read_json)
    result = __PKG__.run(client)
    expected = read_json(ROOT / "sample/expected.json")
    ok = result == expected
    peak_kb = client.largest_response / 1024
    canonical = "peak_kb=%.1f round_trips=%d result=%s" % (peak_kb, client.round_trips, "ok" if ok else "wrong")
    bar = "=" * 50
    print(bar)
    print("   PEAK MEMORY   %7.1f KB    (largest response held)" % peak_kb)
    print(bar)
    print("round trips: %d" % client.round_trips)
    print("result: " + ("matches sample/expected.json" if ok else "DIFFERS from sample/expected.json"))
    print("sync-profile-v1: " + canonical)
    return canonical


outcome = "error"
try:
    READING_INPUTS = True
    outcome = main()
except BaseException as error:
    outcome = "error:" + type(error).__name__
    raise
finally:
    READING_INPUTS = False
    remember(outcome)
'''


def source():
    return PROFILE
