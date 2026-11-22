"""The output directory: events.jsonl, metrics.jsonl, heartbeat.json."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

EVENTS = "events.jsonl"
METRICS = "metrics.jsonl"
HEARTBEAT = "heartbeat.json"
CONFIG = "config.json"
BLOBS = "blobs"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_from_epoch(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(s: str) -> float:
    """An ISO 8601 instant with an explicit UTC offset (`Z` or `+00:00`) as
    Unix seconds. Raises ValueError otherwise."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"{s!r} has no UTC offset")
    return d.timestamp()


def write_json_atomic(path: str, obj: dict) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o644)  # like the logs: readable by the host's reader
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class JsonlLog:
    """Append-only JSON lines. One write call per group of records; a failed
    write is cut back to the last complete group, and if that is impossible
    the log refuses every later write (it is `broken`)."""

    def __init__(self, path: str, append: bool = False) -> None:
        if not append and os.path.exists(path) and os.path.getsize(path) > 0:
            raise FileExistsError(f"{path} already holds records; use a fresh output directory")
        self.path = path
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0), 0o644)
        self._lock = threading.Lock()
        self.offset = os.fstat(self._fd).st_size  # bytes of complete groups
        if append and self.offset:
            with open(path, "rb") as f:
                f.seek(self.offset - 1)
                if f.read(1) != b"\n":
                    os.close(self._fd)
                    raise ValueError(f"{path} ends with a partial line; refusing to append")
        self.broken: str | None = None

    def write(self, records: list[dict]) -> None:
        if not records:
            return
        data = "".join(json.dumps(r, sort_keys=True, ensure_ascii=True) + "\n" for r in records).encode()
        with self._lock:
            if self.broken:
                raise OSError(f"{self.path} is unusable after an earlier write failure: {self.broken}")
            try:
                view = memoryview(data)
                while view:
                    n = os.write(self._fd, view)
                    view = view[n:]
            except OSError as e:
                try:
                    os.ftruncate(self._fd, self.offset)
                except OSError as e2:
                    self.broken = f"{e}; could not cut back: {e2}"
                raise
            self.offset += len(data)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                try:
                    os.fsync(self._fd)
                finally:
                    os.close(self._fd)
                    self._fd = -1


class EventLog:
    """Assigns sequence numbers: 1, 2, ... for every record written. When a
    write fails, the counters go back to the last written group, so nothing
    appended later leaves a gap."""

    def __init__(self, out_dir: str) -> None:
        self._log = JsonlLog(os.path.join(out_dir, EVENTS))
        self._lock = threading.Lock()
        self.last_seq = 0
        self.written_seq = 0  # highest sequence number flushed to the file
        self.count = 0
        self._written_count = 0

    @property
    def broken(self) -> str | None:
        return self._log.broken

    def stamp(self, rec: dict, wall: float | None = None) -> dict:
        """Number the record and give it its time: `wall` (Unix seconds)
        when given, as for a batch record whose time is its commit time."""
        with self._lock:
            self.last_seq += 1
            rec["seq"] = self.last_seq
            self.count += 1
        rec["wall"] = utc_now() if wall is None else utc_from_epoch(wall)
        rec["mono"] = round(time.monotonic(), 6)
        return rec

    def write(self, records: list[dict]) -> None:
        try:
            self._log.write(records)
        except BaseException:
            with self._lock:
                self.last_seq = self.written_seq
                self.count = self._written_count
            raise
        if records:
            with self._lock:
                self.written_seq = max(self.written_seq, max(r["seq"] for r in records))
                self.last_seq = self.written_seq
                self._written_count = self.count

    def close(self) -> None:
        self._log.close()


class MetricsLog(JsonlLog):
    def __init__(self, out_dir: str, append: bool = False) -> None:
        super().__init__(os.path.join(out_dir, METRICS), append=append)
