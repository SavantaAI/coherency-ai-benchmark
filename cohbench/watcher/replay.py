"""Host side: the tree as of a batch boundary, and diffs between two.

Works from the output directory alone. A batch is committed by its `batch`
record; records of a batch without one (a write in progress at the end of
the file) are left out. Any other inconsistency raises ReplayError: records
out of order, a batch whose record count does not match, a record that is
not legal on the state built so far (see manifest.py), a stored blob that is
missing.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass

from .manifest import Entry, Manifest, ReplayError, apply_record
from .records import BLOBS, EVENTS

STATE_KINDS = {"created", "deleted", "moved", "modified", "attrib", "rewritten", "git"}


def read_events(out_dir: str) -> list[dict]:
    """Every record of every committed batch, plus the marks, in order."""
    path = os.path.join(out_dir, EVENTS)
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    lines.pop()  # b"" when the file ends with a newline, else a write in progress
    recs = []
    for i, line in enumerate(lines, 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ReplayError(f"{path}:{i}: {e}") from e
        if not isinstance(rec, dict) or "seq" not in rec or "kind" not in rec:
            raise ReplayError(f"{path}:{i}: not a record")
        recs.append(rec)
    out: list[dict] = []
    open_batch: list[dict] = []
    prev_seq = 0
    last_batch = -1
    bounds: dict[int, tuple[float, float]] = {}  # batch -> (lo, hi) as the watcher computed them
    all_bounded = True  # every batch so far records its write times
    for rec in recs:
        if rec["seq"] != prev_seq + 1:
            raise ReplayError(f"sequence jumps from {prev_seq} to {rec['seq']}")
        prev_seq = rec["seq"]
        kind = rec["kind"]
        if kind == "mark":
            if open_batch:
                raise ReplayError(f"seq {rec['seq']}: mark inside batch {open_batch[0].get('batch')}")
            b = rec.get("batch")
            if rec.get("accepted", True) is False:
                if b is not None:
                    raise ReplayError(f"seq {rec['seq']}: a mark that was not accepted names batch {b}")
            elif not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= last_batch:
                # A mark names the batch in effect at its instant: the last
                # committed one or an earlier one (D-037).
                raise ReplayError(f"seq {rec['seq']}: mark names batch {b}, last committed is {last_batch}")
            s = rec.get("straddled")
            if rec.get("accepted", True) is not False and rec.get("at") and all_bounded and bounds:
                want = answer_for(_instant(rec["at"]), bounds)
                got = (b, s, rec.get("straddled_last"))
                if got != want:
                    raise ReplayError(f"seq {rec['seq']}: mark answers (batch, straddled, straddled_last) = {got}; the recorded bounds give {want}")
            if s is not None and (rec.get("accepted", True) is False or not isinstance(s, int) or isinstance(s, bool) or not b < s <= last_batch):
                # A straddling batch began before the instant and ended after
                # it: committed, and later than the batch named.
                raise ReplayError(f"seq {rec['seq']}: mark says batch {s} straddles it, but names batch {b} with {last_batch} committed")
            out.append(rec)
            continue
        b = rec.get("batch")
        if b != last_batch + 1:
            raise ReplayError(f"seq {rec['seq']}: batch {b} after batch {last_batch}")
        if kind == "batch":
            if rec.get("records") != len(open_batch):
                raise ReplayError(f"batch {b}: says {rec.get('records')} records, has {len(open_batch)}")
            out.extend(open_batch)
            out.append(rec)
            open_batch = []
            last_batch = b
            if "write_times" not in rec:
                all_bounded = False  # a log from before the bounds were recorded
            else:
                hi = _instant(rec["last_write"]) if rec["write_times"] == "known" else _instant(rec["wall"])
                lo = _instant(rec["first_write_after"]) if rec.get("first_write_after") else hi
                bounds[b] = (lo, hi)
        elif kind in STATE_KINDS:
            open_batch.append(rec)
        else:
            raise ReplayError(f"seq {rec['seq']}: unknown record kind {kind!r}")
    return out


def _instant(s: str) -> float:
    from .records import parse_utc

    try:
        return parse_utc(s)
    except (TypeError, ValueError) as e:
        raise ReplayError(f"bad instant {s!r}") from e


def answer_for(at: float, bounds: dict[int, tuple[float, float]]) -> tuple[int, int | None, int | None]:
    """What a mark for the instant `at` answers, from each committed batch's
    bounds (lo, hi): every write of the batch happened after lo and at or
    before hi. Honest bounds can overlap, so a run of batches can straddle
    the instant: `straddled` is the first batch with lo < at < hi,
    `straddled_last` the last (None, None when there is none), and `batch`
    the last batch with hi <= at that comes before `straddled` (the
    baseline when there is none). The watcher answers with this function and
    replay checks with it."""
    run = [b for b, (lo, hi) in bounds.items() if b > 0 and lo < at < hi]
    first, last = (min(run), max(run)) if run else (None, None)
    before = [b for b, (lo, hi) in bounds.items() if hi <= at and (first is None or b < first)]
    return (max(before) if before else 0), first, last


def batches(out_dir: str) -> list[dict]:
    return [r for r in read_events(out_dir) if r["kind"] == "batch"]


def last_batch(events: list[dict]) -> int:
    b = [r["batch"] for r in events if r["kind"] == "batch"]
    if not b:
        raise ReplayError("no committed batch (not even the baseline)")
    return b[-1]


@dataclass
class State:
    batch: int
    manifest: Manifest

    @property
    def entries(self) -> dict[str, Entry]:
        return self.manifest.entries

    @property
    def git(self) -> dict | None:
        return self.manifest.git

    def links(self) -> list[list[str]]:
        """Groups of paths that are hard links to one inode."""
        groups: dict[tuple, list[str]] = {}
        for p, e in self.entries.items():
            if e.type != "dir":  # files, symlinks and specials can be hard-linked
                groups.setdefault(e.ident, []).append(p)
        return sorted(sorted(g) for g in groups.values() if len(g) > 1)

    def as_dict(self) -> dict:
        return {"batch": self.batch, "entries": {p: e.fields() for p, e in sorted(self.entries.items())}, "git": self.git, "links": self.links()}


def state_at(out_dir: str, batch: int | str = "last", events: list[dict] | None = None, verify_blobs: bool = False) -> State:
    """The tree after batch `batch` (an int, or "last"). Batch 0 is the baseline.

    With verify_blobs, every entry recorded as stored must have its blob, and
    the blob must hash to the entry's sha256; otherwise ReplayError."""
    ev = read_events(out_dir) if events is None else events
    top = last_batch(ev)
    if batch == "last":
        batch = top
    if not isinstance(batch, int) or isinstance(batch, bool) or batch < 0:
        raise ReplayError(f"bad batch number {batch!r}")
    if batch > top:
        raise ReplayError(f"batch {batch} is past the last committed batch ({top})")
    m = Manifest()
    for rec in ev:
        if rec["kind"] in ("batch", "mark"):
            if rec["kind"] == "batch" and rec["batch"] == batch:
                break
            continue
        apply_record(m, rec)
    if verify_blobs:
        verify(out_dir, m)
    return State(batch=batch, manifest=m)


def verify(out_dir: str, m: Manifest) -> None:
    import hashlib

    for p, e in m.entries.items():
        if e.content == "stored" and e.sha256:
            data = _blob(out_dir, e.sha256)
            if hashlib.sha256(data).hexdigest() != e.sha256:
                raise ReplayError(f"blob for {p!r} does not match its hash {e.sha256}")


def move_map(events: list[dict], a: int, b: int) -> dict[str, str]:
    """Where each path present after batch a is after batch b, following
    only recorded `moved` records (a directory move carries its subtree).
    Paths deleted on the way are left out."""
    m = Manifest()
    where: dict[str, str] = {}
    ident: dict[str, tuple] = {}  # orig -> its identity after batch a
    for rec in events:
        if rec["kind"] in ("batch", "mark"):
            if rec["kind"] == "batch" and rec["batch"] == a:
                where = {p: p for p in m.entries}
                ident = {p: e.ident for p, e in m.entries.items()}
            if rec["kind"] == "batch" and rec["batch"] == b:
                break
            continue
        if rec["batch"] > a:
            if rec["kind"] == "moved":
                src, dst = rec["from"], rec["path"]
                for orig, cur in list(where.items()):
                    if cur == dst or cur.startswith(dst + "/"):
                        del where[orig]  # whatever was at the target is replaced
                for orig, cur in list(where.items()):
                    if cur == src or cur.startswith(src + "/"):
                        where[orig] = dst + cur[len(src) :]
            elif rec["kind"] == "deleted":
                for orig, cur in list(where.items()):
                    if cur == rec["path"]:
                        del where[orig]
        apply_record(m, rec)
        if rec["batch"] > a and rec["kind"] in ("modified", "attrib", "created"):
            # A replacement in place (another inode at the path) ends the
            # lineage: the new file is not the old one renamed.
            p = rec["path"]
            e = m.entries.get(p)
            for orig, cur in list(where.items()):
                if cur == p and (e is None or e.ident != ident.get(orig)):
                    del where[orig]
    return where


def marks(out_dir: str, include_rejected: bool = False) -> list[dict]:
    """Mark records. A mark that was not accepted (coverage did not hold
    within its limit) names no batch and is left out unless asked for."""
    return [r for r in read_events(out_dir) if r["kind"] == "mark" and (include_rejected or r.get("accepted", True))]


def mark_batch(out_dir: str, label: str) -> int:
    """The batch the last accepted mark with this label named (the batch in
    effect at its instant; a mark never closes a batch)."""
    found = [r["batch"] for r in marks(out_dir) if r.get("label") == label]
    if not found:
        raise ReplayError(f"no accepted mark labelled {label!r}")
    return found[-1]


@dataclass
class FileDiff:
    path: str
    status: str  # added | removed | modified | mode | owner | identity | type | renamed | git
    old: Entry | None
    new: Entry | None
    old_path: str | None = None
    text: str | None = None  # unified diff when both contents are known text
    note: str | None = None  # anything else worth saying, e.g. unknown content

    def render(self) -> str:
        head = f"{self.status} {self.path}" if self.old_path is None else f"renamed {self.old_path} -> {self.path}"
        parts = [head]
        if self.old and self.new:
            if self.old.mode != self.new.mode:
                parts.append(f"mode {self.old.mode:04o} -> {self.new.mode:04o}")
            if (self.old.uid, self.old.gid) != (self.new.uid, self.new.gid):
                parts.append(f"owner {self.old.uid}:{self.old.gid} -> {self.new.uid}:{self.new.gid}")
        if self.note:
            parts.append(self.note)
        if self.text:
            parts.append(self.text.rstrip("\n"))
        return "\n".join(parts)


def _blob(out_dir: str, sha: str) -> bytes:
    import hashlib

    p = os.path.join(out_dir, BLOBS, sha[:2], sha)
    try:
        with open(p, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise ReplayError(f"blob {sha} is recorded as stored but missing") from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise ReplayError(f"blob {sha} does not match its hash")
    return data


def _text(out_dir: str, e: Entry | None) -> tuple[list[str] | None, str | None]:
    if e is None:
        return [], None
    if e.type == "symlink":
        return [f"-> {e.target}\n"], None
    if e.type == "dir":
        return [], ("contents unknown (inaccessible)" if e.content == "inaccessible" else None)
    if e.type != "file":
        return [], None
    if e.content == "stored" and e.sha256:
        data = _blob(out_dir, e.sha256)
        if b"\0" in data:
            return None, "binary"
        try:
            return data.decode("utf-8").splitlines(keepends=True), None
        except UnicodeDecodeError:
            return None, "binary"
    if e.content == "oversize":
        return None, f"oversize ({e.size} bytes, sha256 {e.sha256}), content not stored"
    return None, f"content unknown ({e.content}{', ' + e.error if e.error else ''})"


def _content_diff(out_dir: str, pa: str, a: Entry | None, pb: str, b: Entry | None, la: str, lb: str) -> tuple[str | None, str | None]:
    ta, na = _text(out_dir, a)
    tb, nb = _text(out_dir, b)
    if ta is None or tb is None:
        same_hash = a is not None and b is not None and a.sha256 is not None and a.sha256 == b.sha256
        note = "; ".join(x for x in (na, nb) if x)
        if same_hash:
            return None, f"same content hash; {note}" if note else "same content hash"
        return None, ("content differs: " + note) if note else "content differs"
    lines = []
    for ln in difflib.unified_diff(ta, tb, fromfile=f"a/{pa}@{la}" if a else "/dev/null", tofile=f"b/{pb}@{lb}" if b else "/dev/null"):
        lines.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    return ("".join(lines) or None), (na or nb)


def diff(out_dir: str, batch_a: int | str, batch_b: int | str) -> list[FileDiff]:
    """Every path whose entry differs between the two batch boundaries, and
    the git state if it differs. Never empty when the states differ."""
    ev = read_events(out_dir)
    A = state_at(out_dir, batch_a, ev)
    B = state_at(out_dir, batch_b, ev)
    la, lb = str(A.batch), str(B.batch)
    a, b = A.entries, B.entries
    removed = {p for p in a if p not in b}
    added = {p for p in b if p not in a}
    # Renames: only where the log recorded a move from one path to the other
    # (inode numbers are reused, so equal identity is not evidence).
    renamed: dict[str, str] = {}
    if A.batch < B.batch:
        for src, dst in move_map(ev, A.batch, B.batch).items():
            if src in removed and dst in added and a[src].type == b[dst].type:
                renamed[dst] = src
    elif B.batch < A.batch:
        for src, dst in move_map(ev, B.batch, A.batch).items():
            if dst in removed and src in added and a[dst].type == b[src].type:
                renamed[src] = dst
    sources = set(renamed.values())
    out: list[FileDiff] = []
    for p in sorted(set(a) | set(b)):
        ea, eb = a.get(p), b.get(p)
        if p in renamed:
            src = renamed[p]
            ea = a[src]
            text, note = (None, None)
            if (ea.sha256, ea.target, ea.content) != (eb.sha256, eb.target, eb.content):
                text, note = _content_diff(out_dir, src, ea, p, eb, la, lb)
            out.append(FileDiff(p, "renamed", ea, eb, old_path=src, text=text, note=note))
            continue
        if ea is None:
            text, note = _content_diff(out_dir, p, None, p, eb, la, lb)
            out.append(FileDiff(p, "added", None, eb, text=text, note=note))
        elif eb is None:
            if p in sources:
                continue
            text, note = _content_diff(out_dir, p, ea, p, None, la, lb)
            out.append(FileDiff(p, "removed", ea, None, text=text, note=note))
        elif ea == eb:
            continue
        elif ea.type != eb.type:
            out.append(FileDiff(p, "type", ea, eb, note=f"type {ea.type} -> {eb.type}"))
        elif (ea.sha256, ea.size, ea.content, ea.target, ea.error, ea.epoch) != (eb.sha256, eb.size, eb.content, eb.target, eb.error, eb.epoch):
            text, note = _content_diff(out_dir, p, ea, p, eb, la, lb)
            out.append(FileDiff(p, "modified", ea, eb, text=text, note=note))
        elif ea.mode != eb.mode:
            out.append(FileDiff(p, "mode", ea, eb))
        elif (ea.uid, ea.gid) != (eb.uid, eb.gid):
            out.append(FileDiff(p, "owner", ea, eb))
        else:
            out.append(FileDiff(p, "identity", ea, eb, note=f"inode {ea.dev}:{ea.ino} -> {eb.dev}:{eb.ino}"))
    if A.git != B.git:
        out.append(FileDiff(".git", "git", None, None, note=_git_note(A.git, B.git)))
    if not out and not A.manifest.same(B.manifest):
        raise ReplayError("internal: states differ but no difference was reported")
    return out


def _git_note(ga: dict | None, gb: dict | None) -> str:
    if ga is None or gb is None:
        return f"git directory {'appeared' if ga is None else 'disappeared'}"
    parts = []
    if ga.get("head") != gb.get("head"):
        parts.append(f"HEAD {ga.get('head')} -> {gb.get('head')}")
    ra, rb = ga.get("refs") or {}, gb.get("refs") or {}
    for name in sorted(set(ra) | set(rb)):
        if ra.get(name) != rb.get(name):
            parts.append(f"{name} {ra.get(name)} -> {rb.get(name)}")
    if ga.get("backend") != gb.get("backend"):
        parts.append(f"backend {ga.get('backend')} -> {gb.get('backend')}")
    return "\n".join(parts)


def render(diffs: list[FileDiff]) -> str:
    return "\n".join(d.render() for d in diffs) + ("\n" if diffs else "")
