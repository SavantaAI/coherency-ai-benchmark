"""Append-only call accounting shared by sequential or concurrent CLI runs.

No request text, response text, URL or credentials enter this file. A start
reserves one transport attempt before networking. An unmatched start means
the process ended before accounting completed, so its tokens are unknown.
"""

import fcntl
import json
import os
from pathlib import Path

from .api import EndpointError


class Ledger:
    def __init__(self, path, max_calls):
        self.path = Path(path)
        self.max_calls = max_calls

    def _append(self, make):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            entries = [json.loads(line) for line in stream]
            entry = make(entries)
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return entry

    def start(self, cfg):
        def reserve(entries):
            count = sum(e["event"] == "start" for e in entries)
            if count >= self.max_calls:
                raise EndpointError("the shared API call budget is exhausted")
            return {"event": "start", "call": count + 1, "model": cfg.endpoint["model"], "config": cfg.digest}

        return self._append(reserve)["call"]

    def finish(self, ticket, usage, outcome, ms):
        self._append(lambda _: {"event": "finish", "call": ticket, "usage": usage, "outcome": outcome, "ms": ms})
