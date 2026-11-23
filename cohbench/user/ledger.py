"""Shared call accounting with an owned cap and atomic tail recovery.

A stable sibling lock protects both replacement and append. Historical ledgers
without a cap header remain readable evidence but cannot be resumed.
No request text, response text, URL or credentials enter this file.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import sys
import tempfile
import time
import warnings
from pathlib import Path

from .api import EndpointError


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def line_bytes(row):
    return (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")


class Ledger:
    def __init__(self, path, max_calls):
        if not positive(max_calls):
            raise EndpointError("the ledger cap must be a positive integer")
        self.path = Path(path)
        self.max_calls = max_calls

    @contextmanager
    def _locked(self, deadline=None, clock=time.monotonic):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The data inode changes during atomic recovery. Never unlink this lock.
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            while True:
                self._check_deadline(deadline, clock)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(
                        min(0.005, max(0, deadline - clock()))
                        if deadline is not None
                        else 0.005
                    )
            try:
                self._check_deadline(deadline, clock)
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _check_deadline(deadline, clock):
        if deadline is not None and clock() >= deadline:
            raise EndpointError(
                "attempt not started: ledger unavailable before deadline"
            )

    def _replace(self, raw):
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            try:
                offset = 0
                while offset < len(raw):
                    count = os.write(fd, raw[offset:])
                    if count <= 0:
                        raise OSError("short ledger replacement write")
                    offset += count
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _read(self, raw):
        lines = raw.splitlines(keepends=True)
        rows = []
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                marker = None
                if number < len(lines):
                    try:
                        marker = json.loads(lines[number])
                    except (ValueError, UnicodeDecodeError):
                        pass
                if (
                    isinstance(marker, dict)
                    and marker.get("event") == "unknown_attempt"
                    and marker.get("line") == number
                ):
                    continue
                if number != len(lines) or line.endswith(b"\n"):
                    raise EndpointError(f"malformed ledger line {number}") from None
                rows.append({"event": "partial", "line": number})
                continue
            event = row.get("event")
            valid = event in (
                "cap",
                "start",
                "finish",
                "not_sent",
                "cap_change",
                "unknown_attempt",
            )
            if event in ("start", "finish", "not_sent", "unknown_attempt"):
                valid = valid and positive(row.get("call"))
            if event == "cap":
                valid = (
                    valid
                    and number == 1
                    and row.get("version") == 1
                    and positive(row.get("max_calls"))
                )
            if event == "cap_change":
                valid = valid and positive(row.get("old")) and positive(row.get("new"))
            if event == "unknown_attempt":
                valid = (
                    valid
                    and positive(row.get("line"))
                    and row.get("outcome") == "unknown"
                )
            if not valid:
                raise EndpointError(f"malformed ledger line {number}")
            rows.append(row)
        return rows

    def _append(
        self,
        make,
        *,
        match_cap=True,
        existing=False,
        deadline=None,
        clock=time.monotonic,
        written=None,
    ):
        with self._locked(deadline, clock):
            if existing and not self.path.exists():
                raise EndpointError("set-cap requires an existing ledger")
            raw = self.path.read_bytes() if self.path.exists() else b""
            rows = self._read(raw)
            # A lone interrupted header has not created a ledger or reserved a call.
            if not rows or (len(rows) == 1 and rows[0].get("event") == "partial"):
                if existing:
                    raise EndpointError(
                        "set-cap requires an existing ledger with a complete cap header"
                    )
                rows = [{"event": "cap", "version": 1, "max_calls": self.max_calls}]
                raw = line_bytes(rows[0])
                self._check_deadline(deadline, clock)
                self._replace(raw)
            first = rows[0]
            if first.get("event") != "cap":
                raise EndpointError(
                    "ledger line 1 must be a cap header; historical ledgers cannot be resumed"
                )
            cap = first["max_calls"]
            for row in rows[1:]:
                if row.get("event") == "cap_change":
                    if row["old"] != cap or row["new"] <= cap:
                        raise EndpointError("invalid ledger cap-change history")
                    cap = row["new"]
            if match_cap and cap != self.max_calls:
                raise EndpointError(
                    f"ledger cap is {cap}, caller requested {self.max_calls}; use an explicit cap change"
                )
            repaired = raw if raw.endswith(b"\n") else raw + b"\n"
            truncated = rows[-1].get("event") == "partial"
            if truncated:
                number = rows.pop()["line"]
                recovered = {
                    "event": "unknown_attempt",
                    "call": sum(
                        r.get("event") in ("start", "unknown_attempt") for r in rows
                    )
                    + 1,
                    "line": number,
                    "outcome": "unknown",
                }
                repaired += line_bytes(recovered)
                rows.append(recovered)
            if repaired != raw:
                self._check_deadline(deadline, clock)
                self._replace(repaired)
                if truncated:
                    warnings.warn(
                        f"ledger line {number} was truncated; counted one attempt with unknown outcome",
                        stacklevel=2,
                    )
            entry = make(rows, cap)
            self._check_deadline(deadline, clock)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                line = line_bytes(entry)
                # One append syscall, with exactly one complete JSONL record.
                if os.write(fd, line) != len(line):
                    raise OSError("short ledger append; final line needs recovery")
                if written is not None:
                    written(entry)
                os.fsync(fd)
            finally:
                os.close(fd)
            return entry

    def start(self, cfg, *, deadline=None, clock=time.monotonic, written=None):
        def reserve(entries, cap):
            count = sum(e.get("event") in ("start", "unknown_attempt") for e in entries)
            if count >= cap:
                raise EndpointError("the shared API call budget is exhausted")
            return {
                "event": "start",
                "call": count + 1,
                "model": cfg.endpoint["model"],
                "config": cfg.digest,
            }

        return self._append(reserve, deadline=deadline, clock=clock, written=written)[
            "call"
        ]

    def not_sent(self, ticket, *, deadline=None):
        self._append(
            lambda entries, cap: {
                "event": "not_sent",
                "call": ticket,
                "reason": "transport did not start",
            },
            match_cap=False,
            deadline=deadline,
        )

    def finish(self, ticket, usage, outcome, ms, *, deadline=None):
        self._append(
            lambda entries, cap: {
                "event": "finish",
                "call": ticket,
                "usage": usage,
                "outcome": outcome,
                "ms": ms,
            },
            match_cap=False,
            deadline=deadline,
        )

    def change_cap(self, new):
        if not positive(new) or new <= self.max_calls:
            raise EndpointError("an explicit cap increase needs a larger positive cap")
        return self._append(
            lambda entries, cap: {"event": "cap_change", "old": cap, "new": new},
            existing=True,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explicitly increase a ledger's shared cap"
    )
    parser.add_argument("command", choices=["set-cap"])
    parser.add_argument("path")
    parser.add_argument("--old", type=int, required=True)
    parser.add_argument("--new", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(Ledger(args.path, args.old).change_cap(args.new), sort_keys=True)
        )
        return 0
    except EndpointError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
