"""Source for the public sync profiler and its ordinary run history."""

PROFILE = r'''#!/usr/bin/env python3
"""Run the sync against the sample data and show what it costs."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
# Read source even if a previous unit run left bytecode behind.
sys.pycache_prefix = os.devnull
START_MS = time.time_ns() // 1_000_000
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
READ_PATHS = set()
READING_INPUTS = True


def track_reads(event, args):
    if READING_INPUTS and event == "open" and isinstance(args[0], (str, bytes)) and args[2] & os.O_ACCMODE != os.O_WRONLY:
        try:
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(ROOT):
                READ_PATHS.add(path)
        except (OSError, ValueError, RuntimeError):
            pass


sys.addaudithook(track_reads)


def state_digest():
    paths = set(READ_PATHS)
    paths.update(ROOT / "sample" / name for name in ("records.json", "rates.json", "expected.json"))
    paths.update(path for path in (ROOT / "__PKG__").rglob("*")
                 if "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"))
    # Include local imported helpers, including lazy imports and vendor code.
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(ROOT) and path != Path(__file__).resolve():
                paths.add(path)
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file():
            continue
        name = path.relative_to(ROOT).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def remember(result):
    try:
        row = {"start_ms": START_MS, "end_ms": time.time_ns() // 1_000_000,
               "state": state_digest(), "result": result}
        path = Path.home() / ".local/state/profile/runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with path.open("ab", buffering=0) as stream:
            stream.write(line)
    except (OSError, ValueError):
        pass


def main():
    from vendor.__MODULE__ import __CLS__
    import __PKG__

    client = __CLS__(os.path.join(ROOT, "sample"))
    result = __PKG__.run(client)
    with open(os.path.join(ROOT, "sample", "expected.json"), encoding="utf-8") as fh:
        expected = json.load(fh)
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
