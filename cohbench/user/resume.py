"""Exclusive calibration output ownership and per-entry provenance checks."""

import fcntl
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .items import ItemError, items_digest


@contextmanager
def output_lock(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ItemError("output is locked by another run") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    # Keep the inode: removing it lets a third process lock a different file.


def locked_output(function):
    @wraps(function)
    def locked(args):
        with output_lock(args.out):
            return function(args)

    return locked


def check_provenance(entries, header, items):
    by_id = {item.id: item for item in items}
    for item_id, entry in entries.items():
        record = entry.get("record")
        if not isinstance(record, dict):
            raise ItemError(f"{item_id}: missing verdict provenance")
        expected = {key: header[key] for key in ("config", "prompt_sha256", "items")}
        expected["item"] = items_digest([by_id[item_id]])
        if "code" in record:
            expected["code"] = header["code"]
        for key, value in expected.items():
            if record.get(key) != value:
                raise ItemError(
                    f"{item_id}: record {key} differs from the output header or item"
                )
