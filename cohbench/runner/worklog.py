"""Workspace state for triggers, read live from the watcher's output directories.

The runner reads the watcher's documented output (`events.jsonl`,
`metrics.jsonl` and the content store `blobs/<two hex>/<sha256>`), one
directory per context segment, in order. It never imports the watcher's code:
the files are the contract.

Only committed batches count (D-017). A record with `batch: b` becomes
visible when the `kind: batch` record that closes batch b has been read;
until then it is held. Reading is incremental and stops at the last complete
line. Batch 0 of every segment is its baseline: it sets the tree and is never
a change, and no metric reading of batch 0 counts.

Every committed batch has two times (D-030, the trajectory unit's rule): it
begins at its `first_event`, never before the previous batch's commit, and
its tree is in effect from its `last_event` (else the latest time among its
state records, else the batch record's own `wall`), never before it begins.
A path the batch created exists, and the batch's changes count, from its
beginning; its contents and its metric reading count from when its tree is
in effect. `as_of(t)` gives the state as it stood at time t: the segment
running then, the tree of the batches in effect by t, the paths of batches
begun by t, and the changes, readings and marks of such batches. The runner
judges each output against the state as of its own delivery (D-027).

Positions are `(segment, batch)` pairs, segments counted from 0 in the order
the directories are given. A `since` baseline is a position; changes and
readings count from the next batch on, so a mark's own metric reading
belongs to the state before the event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

STATE_KINDS = ("created", "modified", "rewritten", "attrib", "attribute")
CHANGE_KINDS = ("created", "modified", "rewritten", "attrib", "attribute", "deleted", "moved")
CONTENT_READ_MAX = 1 << 20

Pos = tuple[int, int]


@dataclass(frozen=True)
class FileState:
    type: str | None
    sha256: str | None
    content: str | None


@dataclass(frozen=True)
class Change:
    pos: Pos
    paths: tuple[str, ...]
    t: datetime | None  # the batch's first write: the change counts from here
    last: datetime | None = None  # the batch's last write


@dataclass(frozen=True)
class Reading:
    pos: Pos
    metrics: dict[str, Any]
    t: datetime | None
    run: Any = None


@dataclass
class _Batch:
    no: int
    t: datetime | None  # its tree is in effect from here
    records: list[dict[str, Any]]
    begin: datetime | None = None  # its paths exist and its changes count from here
    lo: datetime | None = None  # the watcher's bounds (replay.answer_for): every write came after lo
    hi: datetime | None = None  # and at or before hi (last_write, or the commit time without write times)


@dataclass
class _Segment:
    n: int
    root: Path
    events_offset: int = 0
    metrics_offset: int = 0
    committed: int = -1  # the highest batch whose batch record has been read
    held: list[dict[str, Any]] = field(default_factory=list)
    held_readings: list[dict[str, Any]] = field(default_factory=list)
    batches: list[_Batch] = field(default_factory=list)
    tree: dict[str, FileState] = field(default_factory=dict)
    last_commit: datetime | None = None  # the previous batch record's own time

    def start(self) -> datetime | None:
        return self.batches[0].t if self.batches else None


def _dt(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    out = []
    for line in data[: end + 1].splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out, offset + end + 1


def _batch(rec: dict[str, Any]) -> int | None:
    b = rec.get("batch")
    return b if isinstance(b, int) and not isinstance(b, bool) and b >= 0 else None


def _apply(tree: dict[str, FileState], r: dict[str, Any]) -> tuple[str, ...] | None:
    """Apply one state record to a tree; the paths it touched, or None."""
    kind, path = r.get("kind"), r.get("path")
    if kind not in CHANGE_KINDS or not isinstance(path, str):
        return None
    if kind in STATE_KINDS:
        tree[path] = FileState(r.get("type"), r.get("sha256"), r.get("content"))
    elif kind == "deleted":
        for p in [p for p in tree if p == path or p.startswith(path + "/")]:
            del tree[p]
    elif kind == "moved":
        src = r.get("from")
        if isinstance(src, str):
            for p in [p for p in tree if p == src or p.startswith(src + "/")]:
                tree[path + p[len(src):]] = tree.pop(p)
        if r.get("sha256") is not None or r.get("type") is not None:
            tree[path] = FileState(r.get("type"), r.get("sha256"), r.get("content"))
    return (r["from"], path) if kind == "moved" and isinstance(r.get("from"), str) else (path,)


class WorkView:
    """The workspace as triggers read it: all of it, or as of a time."""

    def __init__(self, log: "WorkspaceLog", t: datetime | None):
        self.log, self.t = log, t
        segs = log._segments
        if t is None or not segs:
            self.seg = segs[-1] if segs else None
        else:
            started = [s for s in segs if s.start() is not None and s.start() <= t]  # type: ignore[operator]
            self.seg = started[-1] if started else segs[0]

    def _in(self, b: "_Batch") -> bool:
        """A batch inside the episode: none whose last write falls after the end
        bound (an elapsed deadline), as the bundle leaves it out (D-035)."""
        end = self.log.end_bound
        return end is None or b.t is None or b.t <= end

    def _ok(self, x: datetime | None) -> bool:
        return self.t is None or x is None or x <= self.t

    def _before_end(self, x: datetime | None) -> bool:
        end = self.log.end_bound
        return end is None or x is None or x <= end

    @property
    def changes(self) -> list[Change]:
        return [c for c in self.log.changes if self._ok(c.t) and self._before_end(c.last)]

    @property
    def readings(self) -> list[Reading]:
        return [r for r in self.log.readings if self._ok(r.t) and self._before_end(r.t)]

    @property
    def marks(self) -> dict[str, Pos]:
        return {k: v for k, (v, t) in self.log.mark_times.items() if self._ok(t)}

    def position(self) -> Pos:
        if self.seg is None:
            return (0, 0)
        done = [b.no for b in self.seg.batches if self._ok(b.t)]
        return (self.seg.n, max(done) if done else 0)

    def files(self) -> dict[str, FileState]:
        """The tree in effect: content and metrics read this."""
        if self.seg is None:
            return {}
        if self.t is None:
            return dict(self.seg.tree)
        tree: dict[str, FileState] = {}
        for b in self.seg.batches:
            if not self._ok(b.t):
                break
            for r in b.records:
                _apply(tree, r)
        return tree

    def paths(self) -> set[str]:
        """Every path that exists: the tree in effect, plus what a batch that
        has begun but whose tree is not in effect yet created or changed."""
        out = set(self.files())
        if self.seg is not None and self.t is not None:
            for b in self.seg.batches:
                if not self._ok(b.t) and b.begin is not None and b.begin <= self.t and self._in(b):
                    for r in b.records:
                        if r.get("kind") in STATE_KINDS and isinstance(r.get("path"), str):
                            out.add(r["path"])
        return out

    def content(self, path: str) -> str | None:
        return self.log.content(path, self.files())


class WorkspaceLog:
    def __init__(self) -> None:
        self._segments: list[_Segment] = []
        self.changes: list[Change] = []
        self.readings: list[Reading] = []
        self.marks: dict[str, Pos] = {}
        self.mark_times: dict[str, tuple[Pos, datetime | None]] = {}
        self._blobs: dict[tuple[str, str], str] = {}
        self._replayed: set[tuple[Any, ...]] = set()
        self.reading_records: set[Pos] = set()
        self.end_bound: datetime | None = None  # the episode's end (an elapsed deadline), once known: later batches are outside

    def refresh(self, dirs: list[Path]) -> None:
        for d in dirs:
            if not any(s.root == Path(d) for s in self._segments):
                self._segments.append(_Segment(len(self._segments), Path(d)))
        for seg in self._segments:
            recs, seg.events_offset = _lines(seg.root / "events.jsonl", seg.events_offset)
            for r in recs:
                b = _batch(r)
                if b is None:
                    continue
                if r.get("kind") == "batch":
                    self._close(seg, b, r)
                elif r.get("kind") == "mark" and b <= seg.committed:
                    self._mark(seg, r)
                else:
                    seg.held.append(r)
            mets, seg.metrics_offset = _lines(seg.root / "metrics.jsonl", seg.metrics_offset)
            seg.held_readings.extend(m for m in mets if _batch(m) is not None)
            # Every reading record of a batch, whatever its status (a value, failed,
            # incomplete, backlog): coverage of the metric waits for one (G5).
            self.reading_records.update((seg.n, _batch(m)) for m in mets if _batch(m) is not None)
            self._readings(seg)

    def _close(self, seg: _Segment, b: int, batch_rec: dict[str, Any]) -> None:
        """A batch record closes batch b: its held records become visible."""
        if b <= seg.committed:
            return
        mine = [r for r in seg.held if _batch(r) <= b]  # type: ignore[operator]
        seg.held = [r for r in seg.held if _batch(r) > b]  # type: ignore[operator]
        state = [r for r in mine if r.get("kind") in CHANGE_KINDS]
        # When the batch's state came to be: the watcher's notification time of
        # its last change (`last_event`), not when it read the files after its
        # quiet period; else the records' own times; else the batch record's.
        times = [t for t in (_dt(r.get("seen")) or _dt(r.get("wall")) for r in state) if t is not None]
        # The watcher's write times (`first_write`, `last_write`), else its older
        # notification times (`first_event`, `last_event`).
        t = (_dt(batch_rec.get("last_write")) or _dt(batch_rec.get("last_event"))) if state else None
        t = t or (max(times) if times else _dt(batch_rec.get("wall")))
        begin = (_dt(batch_rec.get("first_write")) or _dt(batch_rec.get("first_event"))) if state else None
        if batch_rec.get("write_times") in ("unknown", "none"):
            # No write instants (an overflow or recovery batch, or a sweep): replayed at its
            # commit time, for "path exists" and "tree in effect" alike.
            t = begin = _dt(batch_rec.get("wall")) or t
        if begin is not None and seg.last_commit is not None and begin < seg.last_commit:
            begin = seg.last_commit
        if begin is None or (t is not None and begin > t):
            begin = t
        if t is not None and begin is not None and t < begin:
            t = begin
        # The watcher's own bounds, as the records hold them, for the straddle rule.
        known = batch_rec.get("write_times") == "known"
        lo = _dt(batch_rec.get("first_write_after")) or (_dt(batch_rec.get("first_write")) if known else None)
        hi = _dt(batch_rec.get("last_write")) if known else _dt(batch_rec.get("wall"))
        seg.committed = b
        seg.last_commit = _dt(batch_rec.get("wall")) or seg.last_commit
        seg.batches.append(_Batch(b, t, state, begin, lo, hi))
        for r in state:
            paths = _apply(seg.tree, r)
            if paths and b > 0:
                self.changes.append(Change((seg.n, b), paths, begin, t))
        for r in mine:
            if r.get("kind") == "mark":
                self._mark(seg, r)
        self._readings(seg)

    def _mark(self, seg: _Segment, r: dict[str, Any]) -> None:
        if r.get("accepted") is False:
            return  # an unaccepted mark names no state
        if isinstance(r.get("label"), str):
            pos = (seg.n, _batch(r))  # type: ignore[arg-type]
            self.marks[r["label"]] = pos
            self.mark_times[r["label"]] = (pos, _dt(r.get("at")) or _dt(r.get("wall")))  # D-037: the instant the mark names

    def _readings(self, seg: _Segment) -> None:
        done = [m for m in seg.held_readings if _batch(m) <= seg.committed]  # type: ignore[operator]
        seg.held_readings = [m for m in seg.held_readings if _batch(m) > seg.committed]  # type: ignore[operator]
        times = {b.no: b.t for b in seg.batches}
        for m in done:
            if _batch(m) == 0 or m.get("reason") == "baseline" or m.get("ok") is not True or not isinstance(m.get("metrics"), dict):
                continue
            if m.get("complete") is False:
                continue  # D-036: a copy that lacked a readable file is no reading
            self.readings.append(Reading((seg.n, _batch(m)), m["metrics"], times.get(_batch(m)), m.get("run")))  # type: ignore[arg-type]

    # -- what triggers read --------------------------------------------------

    def as_of(self, t: datetime | None) -> WorkView:
        return WorkView(self, t)

    def position(self) -> Pos:
        return WorkView(self, None).position()

    def files(self) -> dict[str, FileState]:
        """The newest segment's committed tree."""
        return dict(self._segments[-1].tree) if self._segments else {}

    def paths(self) -> set[str]:
        return set(self.files())

    def open_since(self) -> datetime | None:
        """The earliest time of a state record read but not committed yet (its
        batch still open), or None."""
        ts = [t for seg in self._segments for r in seg.held if r.get("kind") in CHANGE_KINDS
              for t in [_dt(r.get("seen")) or _dt(r.get("wall"))] if t is not None]
        return min(ts) if ts else None

    def fresh_instants(self, until: datetime) -> list[datetime]:
        """The times at which batches committed since the last call change the
        state (each begins, each tree comes into effect), up to `until`: a
        batch committed late can begin before the previous slice ended."""
        out = set()
        for seg in self._segments:
            for b in seg.batches:
                # A batch's first write and its last write are replayed independently: a
                # batch straddling the boundary keeps its last write for a later replay.
                for which, x in (("begin", b.begin), ("tree", b.t)):
                    key = (seg.n, b.no, which)
                    if key in self._replayed or x is None or x > until:
                        continue
                    self._replayed.add(key)
                    out.add(x)
        for r in self.readings:
            # A reading that arrives after its batch was replayed gets its own instant.
            key = (r.pos[0], r.pos[1], "reading", r.run)
            if key not in self._replayed and r.t is not None and r.t <= until:
                self._replayed.add(key)
                out.add(r.t)
        return sorted(out)

    def straddling(self, at: datetime, seg: int | None = None) -> list[Pos]:
        """The batches that straddle the instant `at` by the watcher's rule
        (replay.answer_for): first_write_after < at < last_write (the commit
        time without write times), after the baseline; in segment `seg`, or
        the one in effect at `at`. The run the watcher's mark would name from
        `straddled` through `straddled_last`, derived from the log when no mark
        answered for the instant."""
        n = WorkView(self, at).seg.n if seg is None and self._segments else seg
        return [(s.n, b.no) for s in self._segments if s.n == n for b in s.batches
                if b.no > 0 and b.lo is not None and b.hi is not None and b.lo < at < b.hi]

    def unread_batches(self, upto: datetime) -> list[Pos]:
        """Committed batches in effect by `upto` (after each segment's baseline) that have
        no reading record yet."""
        return [(seg.n, b.no) for seg in self._segments for b in seg.batches
                if b.no > 0 and b.t is not None and b.t <= upto and (seg.n, b.no) not in self.reading_records]

    def mark_at(self, pos: Pos) -> str | None:
        """A mark that names exactly this position, the earliest, or None."""
        hits = [(t or datetime.max.replace(tzinfo=None), k) for k, (p, t) in self.mark_times.items() if p == pos]
        hits = [(t.replace(tzinfo=None) if t.tzinfo else t, k) for t, k in hits]
        return min(hits)[1] if hits else None

    def content(self, path: str, files: dict[str, FileState] | None = None) -> str | None:
        st = (files if files is not None else self.files()).get(path)
        if st is None or st.type not in (None, "file") or st.content != "stored" or not st.sha256:
            return None
        for seg in reversed(self._segments):
            key = (str(seg.root), st.sha256)
            if key in self._blobs:
                return self._blobs[key]
            p = seg.root / "blobs" / st.sha256[:2] / st.sha256
            try:
                with open(p, "rb") as fh:
                    text = fh.read(CONTENT_READ_MAX).decode("utf-8", errors="replace")
            except OSError:
                continue  # not cached: a later read may find it
            if len(self._blobs) > 512:
                self._blobs.clear()
            self._blobs[key] = text
            return text
        return None


# ---------------------------------------------------------------------------
# Globs, with the watcher's rules: `*` and `?` stay inside one segment,
# `[...]` is a class, `**` as a whole segment matches any number of segments.
# ---------------------------------------------------------------------------


def _segment_source(seg: str) -> str:
    out, i = [], 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2 if i + 1 < len(seg) and seg[i + 1] in "!^" else i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = seg[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out) + r"\Z"


@lru_cache(maxsize=512)
def _segment_re(seg: str) -> re.Pattern[str]:
    return re.compile(_segment_source(seg))


def glob_problem(pattern: str) -> str | None:
    """Why a glob is not usable, or None."""
    if not pattern or pattern.startswith("/") or "\n" in pattern or "\x00" in pattern:
        return "a relative glob"
    for s in pattern.split("/"):
        if not s:
            return "a relative glob without empty segments"
        if s != "**":
            try:
                re.compile(_segment_source(s))
            except re.error as exc:
                return f"segment {s!r} is not a valid pattern ({exc})"
    return None


def glob_match(pattern: str, path: str) -> bool:
    return _match(tuple(pattern.split("/")), tuple(path.split("/")))


def _match(pat: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pat:
        return not parts
    if pat[0] == "**":
        return any(_match(pat[1:], parts[k:]) for k in range(len(parts) + 1))
    if not parts or not _segment_re(pat[0]).match(parts[0]):
        return False
    return _match(pat[1:], parts[1:])
