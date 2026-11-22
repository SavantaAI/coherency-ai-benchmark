"""Host-only collected files and their content-addressed records."""

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

LIMIT = 1 << 20
NAME = re.compile(r"\A[a-z0-9_]+\Z")
SHA = re.compile(r"\A[0-9a-f]{64}\Z")


def specs(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("collect: a list")
    names = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "path"}:
            raise ValueError("collect: each item has name and path")
        name, path = row["name"], row["path"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ValueError("collect.name: distinct names matching [a-z0-9_]+")
        names.add(name)
        if not isinstance(path, str) or "\0" in path or not (path.startswith("/") or path.startswith("~/")):
            raise ValueError("collect.path: absolute or starting with ~/")
        expanded = "/home/node/" + path[2:] if path.startswith("~/") else path
        resolved = "/" + posixpath.normpath(expanded).lstrip("/")
        if resolved == "/work/project" or resolved.startswith("/work/project/"):
            raise ValueError("collect.path: outside the workspace")
    return tuple(dict(r) for r in value)


def validate(records):
    errors = []
    if not isinstance(records, dict):
        return ["collected: an object"]
    for name, r in records.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(r, dict) or set(r) != {"status", "bytes", "sha256"}:
            errors.append("collected: names and record fields")
            continue
        size, status, sha = r["bytes"], r["status"], r["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"collected.{name}: nonnegative byte count")
            continue
        if status == "collected":
            if size > LIMIT or not isinstance(sha, str) or not SHA.fullmatch(sha):
                errors.append(f"collected.{name}: collected size and digest")
        elif status == "absent":
            if size != 0 or sha is not None:
                errors.append(f"collected.{name}: absent has zero bytes and null digest")
        elif status == "oversize":
            if size <= LIMIT or sha is not None:
                errors.append(f"collected.{name}: oversize exceeds the cap and has null digest")
        else:
            errors.append(f"collected.{name}: unknown status")
    return errors


@dataclass
class Collected:
    records: dict = field(default_factory=dict)
    blobs: dict = field(default_factory=dict)

    def content(self, name):
        row = self.records.get(name, {})
        return self.blobs.get(row.get("sha256")) if row.get("status") == "collected" else None

    def write(self, directory):
        directory = Path(directory)
        for sha, data in self.blobs.items():
            p = directory / sha[:2] / sha
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    @classmethod
    def read(cls, records, directory):
        errors = validate(records)
        if errors:
            raise ValueError("; ".join(errors))
        blobs = {}
        for r in records.values():
            if r["status"] != "collected":
                continue
            sha = r["sha256"]
            data = (Path(directory) / sha[:2] / sha).read_bytes()
            if len(data) != r["bytes"] or hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("collected blob size or digest mismatch")
            blobs[sha] = data
        return cls(records, blobs)


class Collector:
    """Replace a segment's snapshot on retries; concatenate segments in order."""

    def __init__(self, declarations=()):
        self.specs = specs(declarations)
        self.segments = {}
        self.error = None

    def capture(self, segment, read):
        self.segments[segment] = {s["name"]: read(s["path"]) for s in self.specs}

    def result(self):
        if self.error is not None:
            raise ValueError(self.error)
        records, blobs = {}, {}
        for s in self.specs:
            parts = [rows[s["name"]] for _, rows in sorted(self.segments.items())]
            size = sum(r["bytes"] for r, _ in parts)
            present = any(r["status"] != "absent" for r, _ in parts)
            status = "oversize" if size > LIMIT else "collected" if present else "absent"
            data = b"".join(b for _, b in parts if b is not None) if status == "collected" else None
            sha = hashlib.sha256(data).hexdigest() if data is not None else None
            if data is not None:
                blobs[sha] = data
            records[s["name"]] = {"status": status, "bytes": size, "sha256": sha}
        return Collected(records, blobs)


def captured(data):
    if data is None:
        return {"status": "absent", "bytes": 0}, None
    return {"status": "oversize" if len(data) > LIMIT else "collected", "bytes": len(data)}, data if len(data) <= LIMIT else None
