"""Append-only call accounting. The first line owns the shared cap.

Historical ledgers without a cap header remain readable evidence, but cannot
be resumed. A partial final line consumes one unknown attempt on recovery.
No request text, response text, URL or credentials enter this file.
"""

import argparse
import fcntl
import json
import os
import sys
import warnings
from pathlib import Path

from .api import EndpointError


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class Ledger:
    def __init__(self, path, max_calls):
        if not positive(max_calls):
            raise EndpointError("the ledger cap must be a positive integer")
        self.path = Path(path)
        self.max_calls = max_calls

    def _append(self, make, *, match_cap=True):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8", errors="surrogateescape") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            raw = stream.read()
            if not raw:
                header = {"event": "cap", "version": 1, "max_calls": self.max_calls}
                raw = json.dumps(header, sort_keys=True) + "\n"
                stream.write(raw)
            lines = raw.splitlines(keepends=True)
            rows = []
            for number, line in enumerate(lines, 1):
                try:
                    if any(0xDC80 <= ord(c) <= 0xDCFF for c in line):
                        raise ValueError()
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError()
                except ValueError:
                    # A prior recovery marker accounts for this preserved fragment.
                    marker = None
                    if number < len(lines):
                        try:
                            marker = json.loads(lines[number])
                        except ValueError:
                            pass
                    if (
                        isinstance(marker, dict)
                        and marker.get("event") == "unknown_attempt"
                        and marker.get("line") == number
                    ):
                        continue
                    if number != len(lines) or line.endswith("\n"):
                        raise EndpointError(f"malformed ledger line {number}") from None
                    rows.append({"event": "partial", "line": number})
                    continue
                event = row.get("event")
                valid = event in (
                    "cap",
                    "start",
                    "finish",
                    "cap_change",
                    "unknown_attempt",
                )
                if event in ("start", "finish", "unknown_attempt"):
                    valid = valid and positive(row.get("call"))
                if event == "cap":
                    valid = (
                        valid
                        and number == 1
                        and row.get("version") == 1
                        and positive(row.get("max_calls"))
                    )
                if event == "cap_change":
                    valid = (
                        valid and positive(row.get("old")) and positive(row.get("new"))
                    )
                if event == "unknown_attempt":
                    valid = (
                        valid
                        and positive(row.get("line"))
                        and row.get("outcome") == "unknown"
                    )
                if not valid:
                    raise EndpointError(f"malformed ledger line {number}")
                rows.append(row)
            first = rows[0]
            if (
                first.get("event") != "cap"
                or first.get("version") != 1
                or not positive(first.get("max_calls"))
            ):
                raise EndpointError(
                    "ledger line 1 must be a cap header; historical ledgers cannot be resumed"
                )
            cap = first["max_calls"]
            for row in rows[1:]:
                if row.get("event") == "cap_change":
                    if (
                        row.get("old") != cap
                        or not positive(row.get("new"))
                        or row["new"] <= cap
                    ):
                        raise EndpointError("invalid ledger cap-change history")
                    cap = row["new"]
            if match_cap and cap != self.max_calls:
                raise EndpointError(
                    f"ledger cap is {cap}, caller requested {self.max_calls}; use an explicit cap change"
                )
            stream.seek(0, os.SEEK_END)
            if not raw.endswith("\n"):
                stream.write("\n")
            if rows[-1].get("event") == "partial":
                number = rows.pop()["line"]
                ticket = (
                    sum(r.get("event") in ("start", "unknown_attempt") for r in rows)
                    + 1
                )
                recovered = {
                    "event": "unknown_attempt",
                    "call": ticket,
                    "line": number,
                    "outcome": "unknown",
                }
                stream.write(json.dumps(recovered, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                rows.append(recovered)
                warnings.warn(
                    f"ledger line {number} was truncated; counted one attempt with unknown outcome",
                    stacklevel=2,
                )
            entry = make(rows, cap)
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return entry

    def start(self, cfg):
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

        return self._append(reserve)["call"]

    def finish(self, ticket, usage, outcome, ms):
        self._append(
            lambda entries, cap: {
                "event": "finish",
                "call": ticket,
                "usage": usage,
                "outcome": outcome,
                "ms": ms,
            },
            match_cap=False,
        )

    def change_cap(self, new):
        if not positive(new) or new <= self.max_calls:
            raise EndpointError("an explicit cap increase needs a larger positive cap")
        return self._append(
            lambda entries, cap: {"event": "cap_change", "old": cap, "new": new}
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
