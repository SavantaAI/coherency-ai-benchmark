"""The normalised change log: the filesystem watcher's records reduced to
task state, on the episode clock, and written as `changes.jsonl`.

Input: the watcher's `events.jsonl` and `metrics.jsonl`, the episode's
origin (the opening prompt's delivery), the excluded intervals of the
episode clock, and the runner's end: its time and its end mark.

The reader follows `cohbench/watcher/README.md` and the watcher's code as
of 2026-11-22: `created`, `deleted`, `moved` (with `from`), `modified` and
`attrib` (with `changed`, and `via_rename` when a temp file was renamed over
the path within one batch), `rewritten`, `git`, a `batch` record ending each
batch (`resync` when the watcher fell back to deletions and creations), and
`mark` (with `label` and the batch it closes). Every state record carries
`batch`; batch 0 is the baseline. `tests/trajectory/fixtures/watcher-real/`
is real output of that code. Fields the reader does not use are ignored; an
unknown kind, a record without its batch, or a malformed field is an error.

The harness's instruction files and the driver's git refs are left out at
the source (D-026): the watcher never records them. Nothing here filters
them; the bundle gate fails if one appears.

Every decision is made on the watcher's wall times at full precision
(microseconds), and times are converted to episode milliseconds once, when
records are written. In order (cohbench/trajectory/README.md, "The change log"):

1. The episode end, on the raw log: the batch of the runner's end mark (the
   only mark that selects state), which must agree with the end time
   (closing no later than the end plus the watcher's settling window, with
   no other batch closing between it and the end), else the last batch
   closed at or before the end. A mark's batch whose first event is after
   the end gives way to the batch before it. Later state is dropped.
2. Nothing is merged across batches (D-028): a file created in one batch and
   moved in a later one is a creation and a move. Within a batch the watcher
   already records the net result.
3. Bookkeeping inside an excluded interval is dropped; a change inside one
   is an error.
4. Marks: only those a bundle record refers to are kept (the end mark, a
   correction's delivery mark, a probe's target), numbered 1, 2, ... in
   order of appearance; the labels stay in the audit record (`mark_ids`).
5. One reading per retained batch: the earliest successful reading of
   that batch's exact tree, whatever asked for it and whenever it finished,
   written untimed after its batch record; a failed reading is never kept.
   The others stay in the full record. A reading is never moved. Every
   other retained batch has a `no_metric` record saying why.

Times follow one rule (cohbench/trajectory/README.md, "When a change
happened"): a batch's changes exist from its `first_event`, never before
the previous batch's commit; its tree is in effect from its `last_event`.
A batch committed `settled: false` is kept and flagged.

`read_dir` checks the watcher's heartbeat: a log whose watcher did not stop
cleanly is accepted only up to the runner's end mark.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock, ClockError, to_ms
from .format import CHANGELOG_FORMAT, VERSION, canonical

STATE_KINDS = {"created": "created", "modified": "modified", "rewritten": "rewritten", "deleted": "deleted", "moved": "moved", "attrib": "attribute"}
OTHER_KINDS = ("git", "batch", "mark")
CHANGE_KINDS = tuple(sorted(set(STATE_KINDS.values())))
CONTENT = ("stored", "oversize", "unreadable", "inaccessible", "none")  # "inaccessible": a directory, per the watcher README of 04:20
# The watcher's round 5 writes `baseline`, `change`, `backlog` (a batch not
# measured before shutdown) and `internal`; `final` and `mark` are read in
# older logs.
METRIC_REASONS = ("baseline", "change", "backlog", "internal", "final", "mark")
# The bundle's reasons for a batch without a reading: a closed set that does
# not describe driver activity. The detail (no metric command, skipped by a
# later reading, not recorded, which paths a copy lacked) is in the audit
# record (`ChangeLog.no_reading`).
NO_METRIC_REASONS = ("none_taken", "failed", "incomplete")
NO_READING_DETAIL = ("no_metric_command", "skipped", "not_recorded", "failed", "incomplete", "backlog")
MARK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
# Left out at the source (D-026): the watcher never records them. The bundle
# gate fails if either appears, as a check that the source did its job.
DEFAULT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
DEFAULT_DRIVER_REF_NAMESPACES = ("refs/t3/",)

# The header's keys, exactly (a closed schema).
HEADER_KEYS = ("type", "format", "version", "records", "changes", "batches", "marks", "metrics", "no_metric")

RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "change": ("type", "seq", "batch", "t", "kind", "path", "from", "sha256", "content", "file_type", "mode", "target", "content_unknown"),
    # `t`: when the batch's tree is in effect (its last write; its commit for
    # a batch with no write times). `t_first`: its first write, the instant
    # its changes exist from (null for the baseline and for a batch with no
    # write times); `[t_first, t]` is the batch's write span.
    "batch": ("type", "batch", "t", "t_first", "baseline", "resync", "incomplete", "settled"),
    # `straddled`: the batch that began at or before the mark's instant and
    # ended after it (D-037), whose changes count on neither side of it; null
    # when none did.
    # `straddled_last`: the last batch of the run that straddles the instant
    # (honest write ranges can overlap); equal to `straddled` when one does.
    "mark": ("type", "batch", "t", "mark", "straddled", "straddled_last"),
    "git": ("type", "batch", "t", "head", "refs"),
    # One reading per retained batch, with nothing about why or when it was
    # taken: those are driver activity (marks, handoffs, the post-run turn).
    "metric": ("type", "batch", "ok", "metrics", "absent", "absent_count"),
    "no_metric": ("type", "batch", "reason"),
}


class ChangeLogError(ValueError):
    pass


@dataclass
class ChangeLog:
    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    # Wall microseconds from the origin, one per record, for decisions made
    # downstream (which batch an attempt ran in). Not written; empty for a
    # log read back from changes.jsonl.
    wall_us: list[int] = field(default_factory=list)
    # The runner's mark labels -> their neutral numbers in this log. Labels
    # stay in the audit record; the file holds the numbers. In memory only.
    mark_ids: dict[str, int] = field(default_factory=dict)
    unsettled: set[int] = field(default_factory=set)  # batches the watcher committed unsettled
    # batch -> the kept reading's provenance ({"reason", "end_us"}), for audit. In memory only.
    reading_sources: dict[int, dict[str, Any]] = field(default_factory=dict)
    # batch -> why it has no reading, in detail ({"detail": one of
    # NO_READING_DETAIL, "lacked": paths an incomplete copy lacked}), for audit.
    # In memory only; the file's `no_metric.reason` is the closed set.
    no_reading: dict[int, dict[str, Any]] = field(default_factory=dict)
    # batch -> (first write, last write) in wall microseconds: the span in
    # which the batch's writes happened. Its tree is in effect from the last.
    # In memory only.
    spans: dict[int, tuple[int, int]] = field(default_factory=dict)
    # The watcher's batches left out because their writes went on past the
    # end ({"batch" (the watcher's number), "first_write_us", "last_write_us",
    # "straddles"}), for audit. In memory only.
    outside_end: list[dict[str, Any]] = field(default_factory=list)
    # Marks the watcher did not accept, as audit records ({seq, label, wall, at}). In memory only.
    rejected_marks: list[dict[str, Any]] = field(default_factory=list)
    # Marks whose instant a batch outside the episode straddles (a batch cut
    # at the end): the tree there is ambiguous. In memory only.
    straddled_outside: set[int] = field(default_factory=set)

    def dumps(self) -> str:
        return "".join(canonical(x) + "\n" for x in [self.header, *self.records])

    def batches(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("type") == "batch"]

    def batch_at(self, t: int) -> int | None:
        """The batch whose state was in force at episode time t (milliseconds):
        the last batch closed at or before t. At a tie in milliseconds the
        written log cannot order two records; `batch_at_us` decides."""
        b = None
        for r in self.batches():
            if r["t"] <= t:
                b = r["batch"]
        return b

    def batch_at_us(self, us: int) -> int | None:
        """The batch in force at wall microsecond `us`: the last batch closed at or before it."""
        if len(self.wall_us) != len(self.records):
            raise ChangeLogError("this change log has no wall times (read back from a file); decide on batch_at")
        b = None
        for r, u in zip(self.records, self.wall_us):
            if r.get("type") == "batch" and u <= us:
                b = r["batch"]
        return b

    def tree(self, batch: int | None) -> dict[str, tuple[Any, ...]]:
        """The task tree after `batch`: path -> (file_type, mode, sha256, target)."""
        return self._replay(batch)[0]

    def unknown(self, batch: int | None) -> set[str]:
        """D-032: the paths of the tree after `batch` whose content is unknown
        (a file the watcher could not read, from its first such record on,
        through later metadata changes and renames, until it is deleted)."""
        return self._replay(batch)[1]

    def _replay(self, batch: int | None) -> tuple[dict[str, tuple[Any, ...]], set[str]]:
        tree: dict[str, tuple[Any, ...]] = {}
        unknown: set[str] = set()
        if batch is None:
            return tree, unknown
        for r in self.records:
            if r.get("type") != "change" or r["batch"] > batch:
                continue
            k, p = r["kind"], r["path"]
            if k == "deleted":
                for q in [q for q in tree if q == p or q.startswith(p + "/")]:
                    del tree[q]
                unknown = {q for q in unknown if not (q == p or q.startswith(p + "/"))}
                continue
            if k == "moved" and r["from"] is not None:
                src = r["from"]
                for q in sorted([q for q in tree if q == src or q.startswith(src + "/")]):
                    v = tree.pop(q)
                    if q != src:
                        tree[p + q[len(src):]] = v
                unknown = {(p + q[len(src):]) if (q == src or q.startswith(src + "/")) else q for q in unknown}
            if k != "rewritten":
                tree[p] = (r["file_type"], r["mode"], r["sha256"], r["target"])
            if r.get("content_unknown"):
                unknown.add(p)
            else:
                unknown.discard(p)
        return tree, unknown

    def net_change(self, batch: int) -> bool:
        """D-025, under D-032: the batch changed the task tree, by the type,
        mode, content hash and link target of a path whose content is known
        at both boundaries, between the boundary before it and its own. A
        path whose content is unknown at either boundary does not count. The
        baseline is not a change."""
        if batch <= 0:
            return False
        (t0, u0), (t1, u1) = self._replay(batch - 1), self._replay(batch)
        return any(t0.get(q) != t1.get(q) for q in (set(t0) | set(t1)) - u0 - u1)


def _us(wall: Any, start: datetime, where: str) -> int:
    """Wall microseconds from the origin, at the watcher's full precision."""
    try:
        t = datetime.fromisoformat(str(wall).replace("Z", "+00:00"))
    except ValueError:
        raise ChangeLogError(f"{where}: time {wall!r} is not ISO 8601") from None
    if t.tzinfo is None:
        raise ChangeLogError(f"{where}: time without a zone")
    d = t - start
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def _t(wall: Any, start: datetime, where: str) -> int:
    return to_ms(_us(wall, start, where))


def _int(v: Any, where: str, name: str, optional: bool = False) -> int | None:
    if v is None and optional:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ChangeLogError(f"{where}: {name} {v!r:.40} is not an integer")
    return v


def _lines(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] != "":
        lines = lines[:-1]  # a last line without a newline is a write in progress
    return [json.loads(x) for x in lines if x.strip()]


def _relpath(p: Any, where: str) -> str:
    if not isinstance(p, str) or not p or p.startswith("/") or any(c in ("", ".", "..") for c in p.split("/")):
        raise ChangeLogError(f"{where}: path {p!r:.60} is not a clean relative path")
    return p


UNMATERIALISED_TYPES = ("fifo", "socket", "char", "block", "other")
ENTRY_TYPES = ("file", "dir", "symlink", *UNMATERIALISED_TYPES)


def straddling(lo: dict[int, int], hi: dict[int, int], at_us: int) -> list[int]:
    """Every batch whose write range strictly contains the instant (the
    watcher's replay.answer_for, never the baseline), in order: a run when
    honest ranges overlap."""
    return sorted(b for b in hi if b > 0 and b in lo and straddles(lo[b], hi[b], at_us))


def straddles(first_write_after_us: int, last_write_us: int, at_us: int) -> bool:
    """The watcher's predicate (D-037, strict at both ends since its last
    round), the one every route uses: a batch straddles an instant when the
    instant lies strictly between its lower bound (`first_write_after`) and
    its last write. At the lower bound the batch's writes all came after; at
    the last write the batch is in effect."""
    return first_write_after_us < at_us < last_write_us


def _unexpected_absent(final: list[dict[str, Any]], batch: int, absent: list[str], count: int) -> list[str]:
    """The paths a reading's copy of `batch` lacked that the tree could have
    given it; ["(absent list cut)"] when the list is cut and cannot be checked."""
    content: dict[str, Any] = {}
    for x in final:
        if x["type"] != "change" or x["batch"] > batch:
            continue
        k, p = x["kind"], x["path"]
        if k == "deleted":
            for q in [q for q in content if q == p or q.startswith(p + "/")]:
                del content[q]
            continue
        if k == "moved" and x["from"] is not None:
            src = x["from"]
            for q in sorted([q for q in content if q == src or q.startswith(src + "/")]):
                v = content.pop(q)
                if q != src:
                    content[p + q[len(src):]] = v
        if k != "rewritten":
            content[p] = (x["content"], x.get("file_type"))
    closed = [q for q, (c, _) in content.items() if c == "inaccessible"]

    def expected(q: str) -> bool:
        # D-036: a file left out for size makes the copy partial; only what
        # no copy can hold is allowed (the git state, a file unreadable in
        # the workspace itself, an inaccessible directory).
        c, typ = content.get(q, (None, None))
        if q == ".git" or q.startswith(".git/") or c in ("unreadable", "inaccessible"):
            return True
        # M3 of round 11: an entry of a type no copy materialises (a fifo, a
        # socket, a device, other) may be absent from a complete reading.
        if typ in UNMATERIALISED_TYPES:
            return True
        return any(q.startswith(d + "/") for d in closed)

    out = [q for q in absent if not expected(q)]
    if count > len(absent):
        out.append("(absent list cut)")
    return out


def _parent(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def build(events: Iterable[dict[str, Any]], start_utc: str | datetime, metrics: Iterable[dict[str, Any]] = (), *,
          marks: Iterable[str] | None = None, keep_marks: Iterable[str] = (), excluded: Iterable[Any] = (), end_us: int | None = None,
          end_t: int | None = None, end_mark: str | None = None, settle_ms: int = 100) -> ChangeLog:
    """`start_utc` is the origin (the opening prompt's delivery). `end_us` is
    the runner's end in wall microseconds from it (`end_t`, milliseconds, for
    convenience); `end_mark` the runner's end mark, the only mark that
    selects state, which must agree with it within the watcher's settling
    window (`settle_ms`). `marks`: the labels the runner issued (others are
    an error). `keep_marks`: the labels a bundle record refers to (the end
    mark is always one); every other mark is dropped, and the kept ones are
    numbered 1, 2, ... in order of appearance."""
    start = start_utc if isinstance(start_utc, datetime) else datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
    if end_us is None and end_t is not None:
        end_us = end_t * 1000
    allowed_marks = set(marks) if marks is not None else None
    keep = set(keep_marks) | ({end_mark} if end_mark is not None else set())
    try:
        clock = Clock([{"us": _us(i["start_utc"], start, "excluded interval"), "us_end": _us(i["end_utc"], start, "excluded interval"), "reason": i["reason"]}
                       if isinstance(i, dict) and "start_utc" in i else i for i in excluded])
    except ClockError as exc:
        raise ChangeLogError(str(exc)) from None
    # J3 of round 10: a mark the watcher did not accept (`accepted: false`)
    # is an audit record. It took no reading and names no state, may lack a
    # batch, and never fails the build by its shape; it is set aside first.
    rejected_marks: list[dict[str, Any]] = []
    raw = []
    for r in events:
        if isinstance(r, dict) and r.get("kind") == "mark" and r.get("accepted") is False:
            rejected_marks.append({k: r.get(k) for k in ("seq", "label", "wall", "at") if isinstance(r.get(k), (str, int)) and not isinstance(r.get(k), bool)})
        else:
            raw.append(r)
    labels: set[str] = set()
    for n, r in enumerate(raw):
        if not isinstance(r, dict):
            raise ChangeLogError(f"watcher record {n}: not an object")
        if r.get("kind") not in STATE_KINDS and r.get("kind") not in OTHER_KINDS:
            raise ChangeLogError(f"watcher record {n}: unknown kind {r.get('kind')!r}")
        for need in ("seq", "wall", "batch"):
            if need not in r:
                raise ChangeLogError(f"watcher record {n}: missing {need!r}")
        _int(r["seq"], f"watcher record {n}", "seq")
        _int(r["batch"], f"watcher record {n}", "batch")
        where = f"watcher record {n} (seq {r['seq']})"
        if r["kind"] in STATE_KINDS:
            _relpath(r.get("path"), where)
            if r["kind"] == "moved":
                _relpath(r.get("from"), where)
            if r.get("content") is not None and r["content"] not in CONTENT:
                raise ChangeLogError(f"{where}: content {r['content']!r} is not one of {CONTENT}")
            sha = r.get("sha256")
            if sha is not None and not (isinstance(sha, str) and HEX64.match(sha)):
                raise ChangeLogError(f"{where}: sha256 {sha!r:.20} is not a SHA-256 digest")
        if r["kind"] == "mark":
            label = r.get("label")
            if not isinstance(label, str) or not MARK_ID.match(label):
                raise ChangeLogError(f"{where}: a mark carries a runner-issued id, not {label!r:.60}")
            if allowed_marks is not None and label not in allowed_marks:
                raise ChangeLogError(f"{where}: mark {label!r} is not one the runner issued")
            if label in labels:
                raise ChangeLogError(f"{where}: mark {label!r} appears twice; mark ids are unique")
            labels.add(label)
    us_of = [_us(r["wall"], start, f"watcher record {n} (seq {r['seq']})") for n, r in enumerate(raw)]
    # A mark the watcher did not accept (`accepted: false`: its drain did not
    # finish) took no reading and names no state: it resolves no boundary,
    # probe target or correction delivery. A record without the field counts
    # as accepted.
    rejected = [x.get("label") for x in rejected_marks]
    raw_marks_ok = {r["label"] for r in raw if r["kind"] == "mark"}

    # When a change happened (one rule, cohbench/trajectory/README.md, "When a
    # change happened"): the watcher commits a batch about one settling
    # window after the last write it saw. A path a batch created or changed
    # exists from the batch's `first_event`, never earlier than the previous
    # batch's commit; the batch's tree is in effect from its `last_event`.
    # Without those fields, the record time.
    begins: dict[int, int] = {}
    close: dict[int, int] = {}
    # The batches whose first write is known (the watcher's `first_write`,
    # or `first_event` in older output): `t_first` on the batch record.
    first_known: set[int] = set()
    # The lower end of the span in which an instant is straddled by a batch
    # (the watcher's rule, its round 6): the recorded `first_write_after`,
    # whatever the batch's `write_times`. Without it: `first_write`; for a
    # batch whose write times are `unknown`, the previous batch's last write;
    # for a sweep-only one (`none`), its commit.
    lo: dict[int, int] = {}
    prev_commit: int | None = None
    prev_close: int | None = None
    for n, r in enumerate(raw):
        if r["kind"] != "batch":
            continue
        # The watcher's `first_write` and `last_write` count only change
        # notifications (create, modify, move, delete, attribute change);
        # both are null for a sweep-only batch, which falls back to its
        # commit time. Output from before them has `first_event` and
        # `last_event` only, which are used instead.
        fk, lk = ("first_write", "last_write") if "first_write" in r or "last_write" in r else ("first_event", "last_event")
        fe = _us(r[fk], start, f"watcher record {n}") if isinstance(r.get(fk), str) else None
        le = _us(r[lk], start, f"watcher record {n}") if isinstance(r.get(lk), str) else None
        wt = r.get("write_times")
        if fe is not None and wt in (None, "known"):
            first_known.add(r["batch"])
        if fk == "first_write" and fe is None and le is None:
            fe = le = us_of[n]  # a sweep-only (`none`) or `unknown` batch: its changes and its tree at the commit
        b = r["batch"]
        if fe is not None:
            begins[b] = max(fe, prev_commit) if prev_commit is not None else fe
        close[b] = le if le is not None else us_of[n]
        if b in begins:
            close[b] = max(close[b], begins[b])
        # T1 of round 12: the watcher's recorded lower bound, for every kind of
        # batch that has one, so an instant resolved here gives exactly what
        # the watcher's own mark gives. The fallbacks are for logs without it.
        if isinstance(r.get("first_write_after"), str):
            lo[b] = min(_us(r["first_write_after"], start, f"watcher record {n}"), close[b])
        elif "write_times" in r:
            lo[b] = close[b]  # the watcher's own rule: no lower bound, no span (replay.answer_for)
        elif wt == "unknown":
            lo[b] = prev_close if prev_close is not None else close[b]
        elif wt == "none":
            lo[b] = close[b]
        else:
            lo[b] = begins.get(b, close[b])
        prev_commit = us_of[n]
        prev_close = close[b]
    open_ids = sorted({r["batch"] for r in raw if r["kind"] not in ("batch", "mark") and r["batch"] not in close})
    if open_ids and (len(open_ids) > 1 or (close and open_ids[0] < max(close))):
        raise ChangeLogError(f"batches {open_ids} have records but no closing batch record")

    # The episode end, on the raw log and full-precision times, before any
    # projection. The runner's end mark is the only mark that selects state;
    # it must agree with the end time. Without one, the last batch closed at
    # or before the end.
    last_batch = max(close) if close else None
    if end_mark is not None and end_mark in rejected:
        raise ChangeLogError(f"the runner's end mark {end_mark!r} was not accepted by the watcher (its drain did not finish): it names no state")
    if end_mark is not None:
        m = next((r for r in raw if r["kind"] == "mark" and r.get("label") == end_mark), None)
        if m is None:
            raise ChangeLogError(f"the runner's end mark {end_mark!r} is not in the watcher log")
        mb = m["batch"]
        if end_us is not None:
            if mb in close and close[mb] > end_us + settle_ms * 1000:
                raise ChangeLogError(f"the end mark's batch {mb} closed {(close[mb] - end_us) / 1000:.1f} ms after the end, beyond the settling window")
            later = sorted(b for b, u in close.items() if b > mb and u <= end_us)
            if later:
                raise ChangeLogError(f"batches {later} closed after the end mark {end_mark!r} and before the end: the mark and the end disagree")
        last_batch = mb
    elif end_us is not None:
        # By the end instant (no end mark, or an elapsed cutoff, D-035): the
        # tree in effect at the end, the last batch whose last write is at or
        # before it.
        before = [b for b, u in close.items() if u <= end_us]
        last_batch = max(before) if before else None
    # No evidence the bundle cannot date to before the end: a batch whose
    # writes went on past the end (straddling it, or all after it) is outside
    # the episode, its changes, reading and paths included, and listed in the
    # audit record; the last tree is the batch before it, and an end mark on
    # it moves there.
    outside: list[dict[str, Any]] = []
    while end_us is not None and last_batch is not None and last_batch in close and close[last_batch] > end_us:
        outside.append({"batch": last_batch, "first_write_us": lo[last_batch], "last_write_us": close[last_batch],
                        "straddles": lo[last_batch] <= end_us})
        last_batch = max((b for b in close if b < last_batch), default=None)
    listed = {x["batch"] for x in outside}
    for b in sorted(close):
        if end_us is not None and b not in listed and (last_batch is None or b > last_batch) and straddles(lo[b], close[b], end_us):
            outside.append({"batch": b, "first_write_us": lo[b], "last_write_us": close[b], "straddles": True})
    bounded = end_us is not None or end_mark is not None

    def mark_at(n: int) -> int:
        r = raw[n]
        return _us(r["at"], start, f"watcher record {n}") if isinstance(r.get("at"), str) else us_of[n]

    def within(n: int) -> bool:
        r = raw[n]
        if r["kind"] == "mark" and end_mark is not None and r.get("label") == end_mark:
            return True
        if r["batch"] not in close or (bounded and (last_batch is None or r["batch"] > last_batch)):
            return False
        # J2 of round 10: a mark is inside the episode when the instant it
        # stands for (`at`) is at or before the end, whenever the watcher
        # acknowledged it; its record time only for a mark without `at`.
        if r["kind"] == "mark" and r.get("label") != end_mark and end_us is not None and mark_at(n) > end_us:
            return False
        return True

    idx = [n for n in range(len(raw)) if within(n)]
    recs: list[dict[str, Any]] = []
    for n in idx:
        r, u = raw[n], us_of[n]
        kind, batch = r["kind"], r["batch"]
        if kind in STATE_KINDS:
            k = STATE_KINDS[kind]
            changed = r.get("changed")
            if kind == "attrib" and isinstance(changed, list) and changed and set(changed) <= {"identity", "owner"}:
                k = "rewritten"  # nothing the change log keeps changed
            mode = r.get("mode")
            u = begins.get(batch, u)
            recs.append({"type": "change", "_ob": batch, "_us": u, "_seq": r["seq"], "kind": k, "path": r["path"], "from": r.get("from") if kind == "moved" else None,
                         "sha256": r.get("sha256"), "content": r.get("content"), "file_type": r.get("type") if isinstance(r.get("type"), str) else None,
                         "mode": mode if isinstance(mode, str) else None, "target": r.get("target") if isinstance(r.get("target"), str) else None,
                         # A write to a file whose content could not be read
                         # before or after: a change no hash shows (D-032).
                         # The watcher flags every record of an entry whose
                         # content is unknown (its round 4); older output
                         # flags the write only.
                         "_write_unknown": r.get("content_unknown") is True, "content_unknown": False})
        elif kind == "git":
            refs_in = r.get("refs") if isinstance(r.get("refs"), dict) else {}
            u = begins.get(batch, u)
            recs.append({"type": "git", "_ob": batch, "_us": u, "head": r.get("head") if isinstance(r.get("head"), str) else None,
                         "refs": dict(sorted((name, v) for name, v in refs_in.items() if isinstance(v, str)))})
        elif kind == "mark":
            if r["label"] in keep and r["label"] in raw_marks_ok:
                # D-037: a mark names an instant (`at`), the watcher's `batch`
                # is the one in effect there and `straddled` the batch around
                # it. A log from before marks carried them is resolved the same
                # way by time (a mark then closed its batch, so none straddles
                # unless the write times say so).
                at = mark_at(n)
                if "straddled" in r:
                    sb = r["straddled"] if _is_int(r["straddled"]) else None
                    # The watcher's range (its last pass): every batch from
                    # `straddled` to `straddled_last` straddles the instant.
                    sl = r["straddled_last"] if _is_int(r.get("straddled_last")) else sb
                else:
                    run = straddling(lo, close, at)
                    sb, sl = (run[0], run[-1]) if run else (None, None)
                recs.append({"type": "mark", "_ob": batch, "_us": at, "_label": r["label"], "_straddled": sb, "_straddled_last": sl})
        elif kind == "batch":
            # Incomplete: a full rescan after an inotify overflow (moves not
            # trusted), or directories without a watch.
            # `settled`: the watcher's last validation round came back clean;
            # an unsettled boundary may be one the tree never had.
            recs.append({"type": "batch", "_ob": batch, "_us": close[batch], "_first": begins[batch] if batch in first_known else None, "resync": bool(r.get("resync")),
                         "incomplete": bool(r.get("recovered")) or bool(r.get("unwatched")), "settled": r.get("settled") is not False})

    readings: list[dict[str, Any]] = []
    readings_in = list(metrics)
    for n, r in enumerate(readings_in):
        where = f"metric record {n}"
        if not isinstance(r, dict):
            raise ChangeLogError(f"{where}: not an object")
        reason = r.get("reason")
        if reason not in METRIC_REASONS:
            raise ChangeLogError(f"{where}: reason {reason!r} is not one of {METRIC_REASONS}")
        # Not measured (a `backlog` record, or `measured: false`): no reading
        # of the tree, and it may have no end time.
        unmeasured = reason == "backlog" or r.get("measured") is False
        if "end" not in r and not unmeasured:
            raise ChangeLogError(f"{where}: needs an end time")
        label = r.get("label")
        if reason == "mark":
            if not isinstance(label, str) or not MARK_ID.match(label):
                raise ChangeLogError(f"{where}: a mark reading carries the mark's runner-issued id")
            if allowed_marks is not None and label not in allowed_marks:
                raise ChangeLogError(f"{where}: mark {label!r} is not one the runner issued")
        skipped: list[int] = []
        for x in r.get("skipped") if isinstance(r.get("skipped"), list) else []:
            if _is_int(x):
                skipped.append(x)
            elif isinstance(x, list) and len(x) == 2 and all(_is_int(v) for v in x):
                skipped += list(range(x[0], x[1] + 1))  # inclusive ranges, the watcher's current form
        absent_in = r.get("absent") if isinstance(r.get("absent"), dict) else {}
        absent = sorted(_relpath(q, where) for q in absent_in)
        count = r.get("absent_count")
        count = len(absent) if count is None else _int(count, where, "absent_count")
        lacking = r.get("lacking")
        readings.append({"type": "metric", "_ob": _int(r.get("batch"), where, "batch", optional=True),
                         "_us": _us(r["end"], start, where) if isinstance(r.get("end"), str) else 1 << 62,
                         "_reason": reason, "ok": bool(r.get("ok")) and not unmeasured, "_unmeasured": unmeasured,
                         "_lacking": sorted(q for q in (lacking if isinstance(lacking, (list, dict)) else []) if isinstance(q, str)),
                         "metrics": r.get("metrics") if isinstance(r.get("metrics"), dict) else None, "absent": absent, "absent_count": count,
                         "_skipped": skipped, "_complete": r.get("complete") if isinstance(r.get("complete"), bool) else None})

    # Excluded intervals: bookkeeping inside one is dropped; a change is an error.
    out: list[dict[str, Any]] = []
    for x in recs:
        if clock.inside(x["_us"]):
            if x["type"] in ("change", "git"):
                what = x.get("path") or "git state"
                raise ChangeLogError(f"change of {what} (watcher seq {x.get('_seq')}) at {x['_us']} us falls inside the excluded {clock.inside(x['_us']).reason} interval")
            continue
        out.append(x)
    recs = out

    # Batches numbered 0, 1, ... as retained; marks move to the retained
    # batch whose state they saw.
    kept_batches = sorted({x["_ob"] for x in recs if x["type"] == "batch"})
    number = {b: n for n, b in enumerate(kept_batches)} if kept_batches and kept_batches[0] == 0 else {b: n + 1 for n, b in enumerate(kept_batches)}

    def to_kept(b: int | None) -> int | None:
        if b is None:
            return None
        cands = [k for k in kept_batches if k <= b]
        return number[cands[-1]] if cands else None

    final: list[dict[str, Any]] = []
    for x in recs:
        if x["type"] in ("batch", "change", "git") and x["_ob"] not in number:
            continue
        y = dict(x)
        y["batch"] = number[x["_ob"]] if x["type"] != "mark" else to_kept(x["_ob"])
        if y["batch"] is None:
            continue
        final.append(y)

    # One reading per retained batch: the earliest reading that measured that
    # batch's whole tree, whatever asked for it (a change, a mark, the
    # baseline, the final one) and whenever it finished: the tree it
    # measured is what matters, not its time relative to the end or to an
    # excluded interval. It measured the whole tree when it succeeded and its
    # copy lacked nothing the tree could give: the only paths it may omit
    # are those no copy can hold (the coarse git state `.git`, a file whose
    # content is oversize or unreadable, an inaccessible directory and what
    # is under it). A failed reading (a timeout, a bad exit, the watcher's
    # `copy verification failed`) or an incomplete one (a missing blob, a
    # cut absent list) is never kept. A reading is never moved; the others
    # stay in the full record.
    best: dict[int, dict[str, Any]] = {}
    failed: set[int] = set()
    backlog: set[int] = set()
    incomplete: dict[int, list[str]] = {}
    for x in readings:
        if x["_ob"] not in number:
            continue
        b = number[x["_ob"]]
        if x["_unmeasured"]:
            backlog.add(b)
            continue
        if not x["ok"]:
            failed.add(b)
            continue
        # D-036: the watcher says whether the copy held every readable
        # tracked file (`complete`); older logs are judged by their absent list.
        extra = ((x["_lacking"] or ["(the watcher marked the copy incomplete)"]) if x["_complete"] is False else []) if x["_complete"] is not None \
            else _unexpected_absent(final, b, x["absent"], x["absent_count"])
        if extra:
            incomplete.setdefault(b, extra)
            continue
        if b not in best or x["_us"] < best[b]["_us"]:
            best[b] = x
    batch_us = {x["batch"]: x["_us"] for x in final if x["type"] == "batch"}
    for b, x in best.items():
        final.append({**x, "batch": b, "_us": batch_us[b]})
    skipped = {number[b] for x in readings for b in x["_skipped"] if b in number}
    no_reading: dict[int, dict[str, Any]] = {}
    for b, u in batch_us.items():
        if b not in best:
            detail = ("no_metric_command" if not readings_in else "incomplete" if b in incomplete else "failed" if b in failed
                      else "backlog" if b in backlog else "skipped" if b in skipped else "not_recorded")
            no_reading[b] = {"detail": detail, **({"lacked": incomplete[b][:20]} if b in incomplete else {})}
            reason = detail if detail in ("failed", "incomplete") else "none_taken"
            final.append({"type": "no_metric", "batch": b, "_us": u, "reason": reason})

    # Order on full-precision times, then write: episode milliseconds,
    # changes numbered from 1, kept marks and readings numbered in order.
    order = {"change": 0, "git": 1, "batch": 2, "metric": 3, "no_metric": 4, "mark": 5}
    final.sort(key=lambda x: (x["_us"], x["batch"], order[x["type"]], x.get("_seq") or 0))
    mark_ids: dict[str, int] = {}
    for x in final:
        if x["type"] == "mark":
            mark_ids[x["_label"]] = len(mark_ids) + 1
    # D-032: a file whose content the watcher could not read (recorded
    # `unreadable`, or a write with `content_unknown`) stays unknown from
    # then on, through metadata changes and renames, until it is deleted.
    # Every change to it carries `content_unknown: true`.
    unk: set[str] = set()
    for x in final:
        if x["type"] != "change":
            continue
        k, p = x["kind"], x["path"]
        if k == "deleted":
            x["content_unknown"] = any(q == p or q.startswith(p + "/") for q in unk)
            unk = {q for q in unk if not (q == p or q.startswith(p + "/"))}
            continue
        if k == "moved" and x["from"] is not None:
            src = x["from"]
            unk = {(p + q[len(src):]) if (q == src or q.startswith(src + "/")) else q for q in unk}
        elif k == "created":
            unk.discard(p)
        if x.pop("_write_unknown", False) or (x["content"] == "unreadable" and x["file_type"] == "file"):
            unk.add(p)
        x["content_unknown"] = p in unk
    written, wall = [], []
    cut_marks: set[int] = set()
    seq = 0
    prev_batch_t: int | None = None
    for x in final:
        y = {k: v for k, v in x.items() if not k.startswith("_")}
        if x["type"] not in ("metric", "no_metric"):
            y["t"] = clock(x["_us"], f"{x['type']} record")
        if x["type"] == "change":
            seq += 1
            y["seq"] = seq
        if x["type"] == "batch":
            y["baseline"] = x["_ob"] == 0
            # The first write, on the same rule as the change records' times
            # (never earlier than the previous batch's commit), and never
            # earlier than the previous batch's `t` nor later than its own: an
            # instant inside an excluded interval counts as its start, where
            # the episode clock stands still.
            f = x["_first"]
            if y["baseline"] or f is None:
                y["t_first"] = None
            else:
                gap = clock.inside(f)
                y["t_first"] = min(y["t"], max(clock(gap.us if gap else f, "a batch's first write"), prev_batch_t or 0))
            prev_batch_t = y["t"]
        if x["type"] == "mark":
            y["mark"] = mark_ids[x["_label"]]
            # The straddling run, in retained numbering; a batch of it outside
            # the episode (cut at the end) leaves the tree there ambiguous.
            run = list(range(x["_straddled"], x["_straddled_last"] + 1)) if x.get("_straddled") is not None and x.get("_straddled_last") is not None else []
            kept = [number[ob] for ob in run if ob in number]
            y["straddled"], y["straddled_last"] = (kept[0], kept[-1]) if kept else (None, None)
            if len(kept) < len(run):
                cut_marks.add(y["mark"])
        written.append(y)
        wall.append(x["_us"])
    counts = {k: sum(1 for x in written if x["type"] == k) for k in RECORD_FIELDS}
    header = {"type": "header", "format": CHANGELOG_FORMAT, "version": VERSION, "records": len(written),
              "changes": counts["change"], "batches": counts["batch"], "marks": counts["mark"], "metrics": counts["metric"], "no_metric": counts["no_metric"]}
    unsettled = {x["batch"] for x in final if x["type"] == "batch" and not x["settled"]}
    sources = {b: {"reason": x["_reason"], "end_us": x["_us"]} for b, x in best.items()}
    spans = {number[ob]: (lo[ob], close[ob]) for ob in kept_batches if ob in number}
    return ChangeLog(header, written, wall, mark_ids, unsettled, sources, no_reading, spans, outside, rejected_marks, cut_marks)


def join(segments: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]], start_utc: str | datetime, **kw: Any) -> ChangeLog:
    """One task-state sequence from one watcher log per context segment (a
    controlled reset starts a new container and a new watcher on the cloned
    volume, D-010). Each later log's baseline must equal the task state the
    previous logs ended in; the join itself adds no change records; batches,
    the batches a reading lists as skipped, and changes are numbered across
    it. The handoff is an excluded interval of the episode clock, passed in
    `excluded`."""
    if not segments:
        return build([], start_utc, **kw)
    proj: dict[str, Any] = {}
    combined_events: list[dict[str, Any]] = []
    combined_metrics: list[dict[str, Any]] = []
    seq_off = batch_off = 0
    for n, (events, metrics) in enumerate(segments):
        events, metrics = list(events), list(metrics)
        check_sequence(events, f"watcher log {n + 1}")
        if n > 0:
            before = build(combined_events, start_utc, combined_metrics, **proj)
            last = max((r["batch"] for r in before.records if r["type"] == "batch"), default=None)
            baseline = build([r for r in events if r.get("batch") == 0], start_utc, **proj)
            want, got = before.tree(last), baseline.tree(0)
            differ = sorted(p for p in set(want) | set(got) if want.get(p) != got.get(p))
            gits = [r for r in before.records if r["type"] == "git"]
            g_want = (gits[-1]["head"], gits[-1]["refs"]) if gits else None
            g_got = next(((r["head"], r["refs"]) for r in baseline.records if r["type"] == "git"), None)
            if differ or (g_got is not None and g_want is not None and g_got != g_want):
                raise ChangeLogError(f"watcher log {n + 1}: its baseline differs from the task state the previous log ended in: "
                                     f"{differ[:10] or ['git state']}")
            # The repeated baseline goes; marks issued on it stay, on the
            # batch the previous log ended in (the same state).
            events = [r for r in events if r.get("batch") != 0 or r.get("kind") == "mark"]
        # One mapping per log: its baseline reading measured the state the
        # previous log ended in (checked equal above); its later batches continue the numbering.
        prev_last, off, later = batch_off - 1, batch_off, n > 0

        def remap(b: Any) -> Any:
            if not later or not isinstance(b, int) or isinstance(b, bool):
                return b
            return prev_last if b == 0 else b + off - 1

        shifted = []
        for r in events:
            r = dict(r)
            _int(r.get("seq"), f"watcher log {n + 1}", "seq")
            r["seq"] = r["seq"] + seq_off
            # Every field that holds a batch number crosses the join through
            # the same mapping (J1 of round 10): a record's `batch`, and a
            # mark's `straddled`; for readings, `batch` and `skipped` below.
            for key in ("batch", "straddled", "straddled_last"):
                if key in r:
                    r[key] = remap(r[key])
            shifted.append(r)
        ms = []
        for m in metrics:
            # Every reading goes on (a final reading's skipped list included);
            # `build` keeps one per batch, never a handoff's bookkeeping.
            m = {**m, "batch": remap(m.get("batch"))}
            if isinstance(m.get("skipped"), list):
                m["skipped"] = [[remap(x[0]), remap(x[1])] if isinstance(x, list) and len(x) == 2 else remap(x) for x in m["skipped"]]
            ms.append(m)
        combined_events += shifted
        combined_metrics += ms
        seq_off = max([r["seq"] for r in combined_events], default=seq_off)
        batch_off = max([r["batch"] for r in combined_events if isinstance(r.get("batch"), int)], default=-1) + 1
    return build(combined_events, start_utc, combined_metrics, **kw)


def check_sequence(events: list[dict[str, Any]], where: str = "watcher log") -> None:
    """The watcher numbers its records 1, 2, ... with no gap; anything else
    is a log cut or spliced."""
    for n, r in enumerate(events):
        if not isinstance(r, dict) or r.get("seq") != n + 1:
            raise ChangeLogError(f"{where}: record {n} has seq {r.get('seq') if isinstance(r, dict) else None!r} where {n + 1} was due")


def read_dir(out_dir: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """A watcher output directory: its records, its metric readings, and the
    heartbeat's status (None without a heartbeat)."""
    d = Path(out_dir)
    metrics = _lines(d / "metrics.jsonl") if (d / "metrics.jsonl").exists() else []
    status = None
    if (d / "heartbeat.json").exists():
        hb = json.loads((d / "heartbeat.json").read_text(encoding="utf-8"))
        status = hb.get("status") if isinstance(hb, dict) else None
        status = status if isinstance(status, str) else "unknown"
    return _lines(d / "events.jsonl"), metrics, status


def check_status(statuses: list[str | None], end_mark: str | None) -> None:
    """Every watcher stopped cleanly (`stopped`), or the log is read only up
    to the runner's end mark, which only the last watcher can hold."""
    for n, st in enumerate(statuses):
        if st in (None, "stopped"):
            continue
        if n == len(statuses) - 1 and end_mark is not None:
            continue
        raise ChangeLogError(f"watcher log {n + 1}: the watcher's heartbeat says {st!r}, not 'stopped', and no end mark bounds the log")


def build_from_dir(out_dir: str | Path, start_utc: str | datetime, **kw: Any) -> ChangeLog:
    events, metrics, status = read_dir(out_dir)
    check_status([status], kw.get("end_mark"))
    check_sequence(events)
    return build(events, start_utc, metrics, **kw)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(log: ChangeLog) -> list[str]:
    errors = []
    h = log.header
    if not isinstance(h, dict) or h.get("type") != "header" or h.get("format") != CHANGELOG_FORMAT or h.get("version") != VERSION \
            or set(h) != set(HEADER_KEYS):
        return ["header: not a version 1 change log with exactly the header's keys"]
    last = None
    batches: list[int] = []
    closed_at: dict[int, int] = {}
    seqs, marks_seen = [], []
    readings: dict[int, int] = {}  # batch -> metric and no_metric rows
    no_first: list[int] = []
    prev_t: int | None = None  # the previous batch's `t`
    for n, r in enumerate(log.records):
        where = f"change record {n}"
        typ = r.get("type") if isinstance(r, dict) else None
        if not isinstance(typ, str) or typ not in RECORD_FIELDS:
            errors.append(f"{where}: unknown type {typ!r:.30}")
            continue
        want = set(RECORD_FIELDS[typ])
        if typ == "batch" and "t_first" not in r:
            # A bundle made before batches carried their first write reads back
            # without it; every batch of one log is the same either way.
            want.discard("t_first")
            no_first.append(n)
        if set(r) != want:
            errors.append(f"{where}: fields {sorted(set(r) ^ want)} differ from the schema")
            continue
        if not _is_int(r["batch"]):
            errors.append(f"{where}: time and batch are integers")
            continue
        if typ in ("metric", "no_metric"):
            # Untimed: it follows its batch's record, one per batch.
            if r["batch"] not in closed_at:
                errors.append(f"{where}: a {typ} row comes before its batch's record")
            readings[r["batch"]] = readings.get(r["batch"], 0) + 1
            if typ == "metric" and not (isinstance(r["absent"], list) and all(isinstance(q, str) and q for q in r["absent"])
                                        and _is_int(r["absent_count"]) and r["absent_count"] >= len(r["absent"]) and r["ok"] is True
                                        and isinstance(r["metrics"], dict)):
                errors.append(f"{where}: absent is a list of paths and absent_count at least its length; a kept reading succeeded (ok true) with its metrics object")
            if typ == "no_metric" and r["reason"] not in NO_METRIC_REASONS:
                errors.append(f"{where}: reason")
            continue
        if not _is_int(r["t"]) or (last is not None and r["t"] < last):
            errors.append(f"{where}: time and batch are integers, in order")
            continue
        last = r["t"]
        if typ == "batch":
            if batches and r["batch"] != batches[-1] + 1:
                errors.append(f"{where}: batch {r['batch']} does not follow {batches[-1]}")
            if r["baseline"] is not (r["batch"] == 0) or not isinstance(r["settled"], bool):
                errors.append(f"{where}: only batch 0 is the baseline; settled is a boolean")
            tf = r.get("t_first")
            if "t_first" in r and not (tf is None or (_is_int(tf) and (prev_t or 0) <= tf <= r["t"])):
                errors.append(f"{where}: t_first is null or an integer from the previous batch's t to this batch's t")
            if r["baseline"] is True and tf is not None:
                errors.append(f"{where}: the baseline has no first write (t_first null)")
            prev_t = r["t"]
            batches.append(r["batch"])
            closed_at[r["batch"]] = n
        if typ in ("change", "git") and r["batch"] in closed_at:
            errors.append(f"{where}: a {typ} record of batch {r['batch']} after that batch closed")
        if typ == "change":
            seqs.append(r["seq"])
            if not (isinstance(r["kind"], str) and r["kind"] in CHANGE_KINDS) or not (r["content"] is None or (isinstance(r["content"], str) and r["content"] in CONTENT)):
                errors.append(f"{where}: kind or content out of vocabulary")
            clean = lambda q: isinstance(q, str) and bool(q) and not q.startswith("/") and all(c not in ("", ".", "..") for c in q.split("/"))  # noqa: E731
            if not clean(r["path"]) or not (r["from"] is None or clean(r["from"])):
                errors.append(f"{where}: path and from are clean relative paths")
            if not (r["file_type"] is None or (isinstance(r["file_type"], str) and r["file_type"] in ENTRY_TYPES)):
                errors.append(f"{where}: file_type is one of {ENTRY_TYPES} or null")
            if r["sha256"] is not None and not (isinstance(r["sha256"], str) and HEX64.match(r["sha256"])):
                errors.append(f"{where}: sha256 is not a digest")
            if r["content"] == "stored" and r["sha256"] is None:
                errors.append(f"{where}: stored content needs its hash")
            if not isinstance(r["content_unknown"], bool):
                errors.append(f"{where}: content_unknown is a boolean")
        if typ == "mark":
            if not (r["straddled"] is None or (_is_int(r["straddled"]) and r["straddled"] > r["batch"])):
                errors.append(f"{where}: straddled is null or a batch after the mark's")
            if not _is_int(r["mark"]):
                errors.append(f"{where}: a mark is a neutral number, not {r['mark']!r:.40}")
            else:
                marks_seen.append(r["mark"])
    if no_first and len(no_first) != len(batches):
        errors.append(f"change records {no_first[:5]}: batch records without t_first in a log whose other batches carry it")
    if seqs != list(range(1, len(seqs) + 1)):
        errors.append("changes are numbered 1, 2, ... in order")
    if marks_seen != list(range(1, len(marks_seen) + 1)):
        errors.append("marks are numbered 1, 2, ... in order of appearance, each once")
    bset = set(batches)
    for b, k in sorted(readings.items()):
        if k > 1:
            errors.append(f"batch {b} has {k} reading or no_metric rows; exactly one")
    for n, r in enumerate(log.records):
        if isinstance(r, dict) and isinstance(r.get("type"), str) and r.get("type") in RECORD_FIELDS and _is_int(r.get("batch")) and r["batch"] not in bset:
            errors.append(f"change record {n}: batch {r['batch']} has no batch record")
    # A kept reading omits only what no copy can hold (D-036): the git
    # state, a file unreadable in the workspace, an inaccessible directory
    # and what is under it, each a path of its batch's tree.
    if not errors:
        try:
            for n, r in enumerate(log.records):
                if isinstance(r, dict) and r.get("type") == "metric" and isinstance(r.get("absent"), list) and all(isinstance(q, str) for q in r["absent"]):
                    # Each displayed path is checked; a display the watcher capped
                    # (absent_count above the list) is not a cut copy (T2 of round 12).
                    bad = _unexpected_absent(log.records, r["batch"], r["absent"], len(r["absent"]))
                    if bad:
                        errors.append(f"change record {n}: a kept reading claims {bad[:3]} absent, which a copy of batch {r['batch']}'s tree could hold")
            closes = {r["batch"]: r["t"] for r in log.records if isinstance(r, dict) and r.get("type") == "batch" and _is_int(r.get("t"))}
            for n, r in enumerate(log.records):
                if not (isinstance(r, dict) and r.get("type") == "mark" and _is_int(r.get("batch")) and _is_int(r.get("t"))):
                    continue
                sb, sl = r.get("straddled"), r.get("straddled_last")
                if (sb is None) != (sl is None) or (_is_int(sb) and not (_is_int(sl) and sl >= sb)):
                    errors.append(f"change record {n}: straddled and straddled_last are both null, or a run from straddled to straddled_last")
                for x in (sb, sl):
                    if _is_int(x) and x not in batches:
                        errors.append(f"change record {n}: the mark's straddled batch {x} is not in the log")
                if _is_int(sb) and _is_int(sl):
                    # Each batch of the run ended after the instant (ms ties either way).
                    if any(closes.get(x, r["t"]) < r["t"] for x in range(sb, sl + 1)):
                        errors.append(f"change record {n}: a batch from {sb} to {sl} ended before the mark's instant, so it does not straddle it")
                # The batch in force at the mark's instant: the last whose last
                # write is at or before it (a millisecond tie either way).
                b0, nxt = r["batch"], closes.get(r["batch"] + 1)
                if b0 not in closes or closes[b0] > r["t"] or (nxt is not None and nxt < r["t"]):
                    errors.append(f"change record {n}: batch {b0} is not the batch in force at the mark's instant {r['t']}")
        except (TypeError, KeyError, AttributeError):
            errors.append("change records: the tree cannot be replayed from them")
    # D-032: once a file's content is unknown, every later change to it
    # (a metadata change, a rename, its deletion) says so.
    unk: set[str] = set()
    for n, r in enumerate(log.records):
        if not (isinstance(r, dict) and r.get("type") == "change" and isinstance(r.get("path"), str) and isinstance(r.get("content_unknown"), bool)):
            continue
        k, p = r.get("kind"), r["path"]
        if k == "moved" and isinstance(r.get("from"), str):
            src = r["from"]
            unk = {(p + q[len(src):]) if (q == src or q.startswith(src + "/")) else q for q in unk}
        sticky = any(q == p or q.startswith(p + "/") for q in unk) if k == "deleted" else (p in unk and k != "created")
        if sticky and not r["content_unknown"]:
            errors.append(f"change record {n}: {p} has unknown content since an earlier change; content_unknown stays true")
        if k == "deleted":
            unk = {q for q in unk if not (q == p or q.startswith(p + "/"))}
        elif r["content_unknown"]:
            unk.add(p)
        else:
            unk.discard(p)
    if bset - set(readings):
        errors.append(f"batches {sorted(bset - set(readings))} have neither a metric reading nor a no_metric record")
    for k, typ in (("changes", "change"), ("batches", "batch"), ("marks", "mark"), ("metrics", "metric"), ("no_metric", "no_metric")):
        if h.get(k) != sum(1 for r in log.records if isinstance(r, dict) and isinstance(r.get("type"), str) and r.get("type") == typ):
            errors.append(f"header: {k}")
    if h.get("records") != len(log.records):
        errors.append("header: records")
    return errors


def read(path: str | Path) -> ChangeLog:
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").split("\n") if x.strip()]
    if not rows or rows[0].get("format") != CHANGELOG_FORMAT:
        raise ChangeLogError("not a change log")
    return ChangeLog(rows[0], rows[1:])


def write(log: ChangeLog, path: str | Path) -> None:
    Path(path).write_text(log.dumps(), encoding="utf-8")
