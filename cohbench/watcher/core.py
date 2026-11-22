"""The watcher's logic: inotify hints in, verified batches out.

inotify events only say which paths may have changed. When the pending
batch is due, the scanner reads every dirty path (see scan.py), `delta.plan`
derives records from the difference between the last manifest and the
scanned one, and before anything is written the records are applied to the
last manifest with the replay code and compared with the scanned manifest.
If they do not reproduce it, the batch is written as a `resync` (deletions
and creations from the plain difference) and the batch record says why.
The log therefore always replays, and it replays to what was scanned.

`Backend` is the watch interface: inotify in production, a fake in tests.
"""

from __future__ import annotations

import errno
import os
import threading
from dataclasses import replace
import time
from typing import Callable, Protocol

from . import delta
from . import inotify as ino
from .blobs import BlobStore
from .config import Config
from .gitstate import GIT, scan_git
from .ignore import IgnoreRules
from .manifest import Manifest, join, under
from .pending import Pending, Taken
from .records import EventLog, parse_utc, utc_from_epoch
from .safefs import SafeRoot
from .scan import Scanner, rewrite

COOKIE_TTL_S = 5.0
# Notifications that report a change, for a batch's first_write and
# last_write (an open or a close alone is not one).
CHANGE_MASK = ino.IN_CREATE | ino.IN_MODIFY | ino.IN_MOVED_FROM | ino.IN_MOVED_TO | ino.IN_DELETE | ino.IN_ATTRIB
VALIDATE_ROUNDS = 4
# Reads per validation round until one empties the queue; a queue still not
# empty after this many leaves the batch unsettled.
MAX_DRAIN_READS = 64


def _us(t: float | None) -> float | None:
    """An instant as the records hold it (UTC, microseconds)."""
    return None if t is None else parse_utc(utc_from_epoch(t))
MAX_LISTED_HINTS = 200


class Backend(Protocol):
    def add_watch_fd(self, fd: int) -> int: ...
    def rm_watch(self, wd: int) -> None: ...


class Core:
    def __init__(self, cfg: Config, backend: Backend, events: EventLog, blobs: BlobStore, clock: Callable[[], float] = time.monotonic, wallclock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.root = SafeRoot(cfg.root)
        self.backend = backend
        self.events = events
        self.blobs = blobs
        self.clock = clock
        self.wallclock = wallclock
        self.ignore = IgnoreRules(list(cfg.ignore), names=list(cfg.ignore_names), forms=list(cfg.ignore_name_forms))
        # The root .git is never part of the tree: only its coarse state
        # (HEAD and the refs outside the ignored namespaces) is recorded.
        self.scanner = Scanner(self.root, self.ignore, cfg.max_blob_bytes, blobs, self, lambda: scan_git(self.root, self._git_watch, self.cfg.ignore_git_refs))
        self.last_commit: dict = {"settled": True, "unsettled": [], "validation_rounds": 0, "batch": -1}
        self._git_seen: dict | None = None
        self.recover_full = False  # the last batch write failed
        # Marks (D-037): every read of the event queue is numbered; a hint
        # carries the number of the read that brought it.
        self.feed_epoch = 0
        self.on_read: Callable[[float, int], None] | None = None
        # batch -> {lo, hi, committed, settled, write_times} (wall times): its
        # writes happened after lo and at or before hi (D-037)
        self.batch_info: dict[int, dict] = {}
        self.manifest = Manifest()
        self.batch = -1  # last committed batch; the baseline is 0
        self.pending = Pending(cfg.quiet_ms / 1000.0, cfg.max_delay_ms / 1000.0)
        self.cookies: dict[int, tuple[str, float]] = {}
        self.wd_info: dict[int, tuple[str, tuple[int, int] | None, str]] = {}  # wd -> (rel, ident, role)
        self.ident_wd: dict[tuple[int, int], int] = {}
        self.unwatched: dict[str, str] = {}
        self.overflowed = False
        self.overflows = 0
        self.resyncs = 0
        self.fatal: str | None = None
        self.raw_events = 0
        self.lock = threading.Lock()
        self.snapshot: tuple[int, Manifest] | None = None
        self.on_commit: Callable[[int, dict], None] | None = None
        self.control_hint = False

    # ---- watches (the scanner calls these) -------------------------------

    def watch(self, rel: str, fd: int, ident: tuple[int, int], role: str = "tree") -> bool:
        try:
            wd = self.backend.add_watch_fd(fd)
        except OSError as e:
            self.unwatched[rel] = errno.errorcode.get(e.errno or 0, str(e.errno))
            return False
        old = self.wd_info.get(wd)
        if old is not None and old[1] is not None and self.ident_wd.get(old[1]) == wd and old[1] != ident:
            del self.ident_wd[old[1]]
        self.wd_info[wd] = (rel, ident, role)
        if role == "tree":
            self.ident_wd[ident] = wd
        self.unwatched.pop(rel, None)
        return True

    def has_watch(self, ident: tuple[int, int]) -> bool:
        return ident in self.ident_wd

    def watch_path(self, rel: str, role: str) -> bool:
        try:
            fd = self.root.open_dir(rel)
        except OSError:
            return False
        try:
            st = os.fstat(fd)
            return self.watch(rel, fd, (st.st_dev, st.st_ino), role)
        finally:
            os.close(fd)

    def _drop_wd(self, wd: int, remove: bool) -> None:
        info = self.wd_info.pop(wd, None)
        if info is not None and info[1] is not None and self.ident_wd.get(info[1]) == wd:
            del self.ident_wd[info[1]]
        if remove:
            try:
                self.backend.rm_watch(wd)
            except OSError:
                pass

    def _reconcile_watches(self) -> None:
        """Every watch belongs to a directory of the current manifest (by
        identity), the root, or the git directories; the rest are removed."""
        dirs = {e.ident: p for p, e in self.manifest.entries.items() if e.type == "dir"}
        for wd, (rel, ident, role) in list(self.wd_info.items()):
            if role != "tree" or rel == "":
                continue
            p = dirs.get(ident)
            if p is None:
                self._drop_wd(wd, remove=True)
            elif p != rel:
                self.wd_info[wd] = (p, ident, role)
        for rel in list(self.unwatched):
            e = self.manifest.entries.get(rel)
            if e is None or e.type != "dir":
                del self.unwatched[rel]

    # ---- raw events ------------------------------------------------------

    def tracked_path(self, p: str) -> bool:
        return bool(p) and not self.is_git(p) and not self.excluded(p)

    def is_git(self, rel: str) -> bool:
        return under(rel, GIT)

    def excluded(self, rel: str) -> bool:
        """Ignored by name or by glob."""
        return self.ignore.ignored(rel)

    def git_relevant(self, rel: str) -> bool:
        """Whether an event under .git can change HEAD or a
        recorded ref. Anything else under .git (the index, objects, logs,
        config) and every ignored ref namespace is invisible."""
        if rel == GIT:
            return True
        sub = rel[len(GIT) + 1 :]
        for ns in self.cfg.ignore_git_refs:
            if under(sub, ns.rstrip("/")):
                return False
        name = sub.split("/", 1)[0]
        if name in ("HEAD", "HEAD.lock", "packed-refs", "packed-refs.lock"):
            return "/" not in sub
        return name == "refs"

    def _git_changed(self) -> bool:
        """Whether the filtered coarse git state (ignored ref namespaces
        removed, loose and packed) differs from the last one seen. Only then
        may a git event touch the batch clock."""
        st = scan_git(self.root, self._git_watch, self.cfg.ignore_git_refs)
        if st == self._git_seen:
            return False
        self._git_seen = st
        return True

    def feed(self, raws: list[ino.RawEvent], now: float | None = None, wall: float | None = None) -> None:
        """Hints from raw events. An event that concerns only ignored names,
        ignored globs or ignored git refs is dropped before it touches the
        batch clock, the event count or any hint."""
        now = self.clock() if now is None else now
        wall = self.wallclock() if wall is None else wall
        self.feed_epoch += 1
        self.pending.epoch = self.feed_epoch
        self.raw_events += len(raws)
        # Move pairs by cookie within this read, whatever lies between the
        # two halves (an ignored name's event, say): cookie -> index of its
        # IN_MOVED_TO.
        moved_to = {ev.cookie: i for i, ev in enumerate(raws) if ev.mask & ino.IN_MOVED_TO and ev.cookie}
        for i, ev in enumerate(raws):
            m = ev.mask
            if m & ino.IN_Q_OVERFLOW:
                self.overflowed = True
                self.pending.note(now, wall)
                continue
            info = self.wd_info.get(ev.wd)
            if m & ino.IN_IGNORED:
                if info is not None:
                    if info[0] == "" and info[2] == "tree":
                        self.fatal = "the watch on the root was removed (root deleted or unmounted)"
                    self._drop_wd(ev.wd, remove=False)
                    if info[2] == "tree" and info[0]:
                        # Its directory left the tree or went away: nothing
                        # open below it holds a batch any more.
                        self.pending.drop_writers(info[0])
                continue
            if info is None:
                continue
            d, _, role = info
            if role == "control":
                self.control_hint = True
                continue
            if not ev.name:
                if d == "" and role == "tree":
                    if m & (ino.IN_DELETE_SELF | ino.IN_MOVE_SELF | ino.IN_UNMOUNT):
                        self.fatal = "the root was deleted, moved or unmounted"
                    elif m & ino.IN_ATTRIB:
                        # The root's own mode or owner changed: its listing
                        # may have become possible or impossible.
                        self.pending.mark_root(now, wall)
                continue
            rel = join(d, ev.name)
            if role == "git" or self.is_git(rel):
                if self.git_relevant(rel) and self._git_changed():
                    self.pending.git(now, wall)
                continue
            ex = self.excluded(rel)
            if m & ino.IN_MOVED_FROM:
                self.cookies[ev.cookie] = (rel, now, ex)
                if not ex:
                    self.pending.moved_from(rel, now, wall)
                    if m & ino.IN_ISDIR and moved_to.get(ev.cookie, -1) < i:
                        # Unpaired in this read: the directory went
                        # somewhere this watcher does not see (outside, or
                        # under an ignored or unwatched directory). Its
                        # watches stop naming tracked paths now, so a later
                        # event on them cannot bring a writer back; the next
                        # scan reconciles (a late IN_MOVED_TO only costs a
                        # fresh listing of the destination).
                        self._detach(rel)
                continue
            if m & ino.IN_MOVED_TO:
                src = self.cookies.pop(ev.cookie, None) if ev.cookie else None
                if src is not None and not src[2] and not ex:
                    self.pending.rename(src[0], rel, now, wall)
                    if m & ino.IN_ISDIR:
                        self._relocate_wds(src[0], rel)
                elif ex:
                    # Onto an ignored name: the tracked source (already
                    # touched as moved away) is all that is seen, and its
                    # file's lifetime at that path is over.
                    if src is not None and not src[2]:
                        self.pending.ops.append(("gone", src[0]))
                        self.pending.moved_away(src[0])
                        if m & ino.IN_ISDIR:
                            self._detach(src[0])
                else:
                    # From an ignored name or from outside: a creation here.
                    self.pending.touch(rel, now, wall, created=True, change=True, wd=ev.wd, name=ev.name)
                    if m & ino.IN_ISDIR:
                        self.pending.moved_in_dirs.add(rel)
                continue
            if ex:
                continue
            self.pending.touch(rel, now, wall, modify=bool(m & ino.IN_MODIFY), close_write=bool(m & ino.IN_CLOSE_WRITE), created=bool(m & ino.IN_CREATE), deleted=bool(m & ino.IN_DELETE), change=bool(m & CHANGE_MASK), wd=ev.wd, name=ev.name)
        for c, v in list(self.cookies.items()):
            if now - v[1] > COOKIE_TTL_S:
                del self.cookies[c]

    def _detach(self, src: str) -> None:
        """`src` left the tracked tree: drop the watches of its subtree and
        every writer below it."""
        for wd, (rel, _ident, role) in list(self.wd_info.items()):
            if role == "tree" and rel and under(rel, src):
                self._drop_wd(wd, remove=True)
        self.pending.moved_away(src)
        self.pending.drop_writers(src)

    def _relocate_wds(self, src: str, dst: str) -> None:
        for wd, (rel, ident, role) in list(self.wd_info.items()):
            if role == "tree" and rel and under(rel, src):
                self.wd_info[wd] = (dst + rel[len(src) :], ident, role)

    # ---- batches ---------------------------------------------------------

    def due(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return self.overflowed or self.recover_full or self.pending.due(now)

    def tick(self, now: float | None = None, force: bool = False, reason: str | None = None) -> list[dict]:
        """Scan and commit a batch if one is due (or forced). Returns the records written."""
        now = self.clock() if now is None else now
        if self.overflowed or self.recover_full:
            # After an overflow, or after a batch could not be written (its
            # scan already moved the scanner's signatures and consumed its
            # hints): a full rescan against the last committed manifest.
            reason = "overflow" if self.overflowed else "recovery"
            if self.overflowed:
                self.overflowed = False
                self.overflows += 1
            taken = self.pending.take(now)
            out = self.commit(taken, full=True, reason=reason)
            self.recover_full = False
            self._check_scheduled()
            return out
        if not self.pending and not force:
            return []
        if not force and not self.pending.due(now):
            return []
        taken = self.pending.take(now)
        out = self.commit(taken, full=False, reason=reason or ("forced" if taken.forced else "settled"))
        self._check_scheduled()
        return out

    def _check_scheduled(self) -> None:
        """Invariant: after a batch, outstanding hints always have a deadline."""
        if self.pending.outstanding() and self.pending.first is None:
            raise RuntimeError("internal: hints outstanding after a batch but nothing scheduled")

    def baseline(self) -> list[dict]:
        if self.batch != -1:
            raise RuntimeError("baseline already taken")
        rst = os.fstat(self.root.fd)
        if not self.watch("", self.root.fd, (rst.st_dev, rst.st_ino)):
            raise OSError(f"cannot watch the root {self.root.root}: {self.unwatched.get('')}")
        return self.commit(Taken(), full=True, reason="baseline")

    def _git_watch(self, rel: str, fd: int) -> None:
        st = os.fstat(fd)
        self.watch(rel, fd, (st.st_dev, st.st_ino), "git")

    def _read_raw(self) -> int:
        read = getattr(self.backend, "read", None)
        if read is None:
            return 0
        start = self.wallclock()
        raws = read()
        end, end_mono = self.wallclock(), self.clock()
        # A read that stopped at its buffer cap may have left events queued.
        complete = not getattr(self.backend, "capped", False)
        if raws:
            # Timing by read: every event of this read happened after the
            # previous read began and at or before this one returned
            # (D-037): stamped with the return time, the upper bound.
            self.feed(raws, now=end_mono, wall=end)
        if complete:
            # Only a read that emptied the queue moves the lower bound for
            # later events: after a capped one, events queued before it may
            # still be waiting.
            self.pending.read_after = start
        if self.on_read is not None and complete:
            # Every write that completed before `start` is now in a hint.
            self.on_read(start, self.feed_epoch)
        return len(raws)

    def _wd_hints(self, taken: Taken, res) -> tuple[set[str], set[str]]:
        """Hints by watch, resolved by the directory's identity in the
        scanned tree. A watch whose recorded path is not where its directory
        now is (an unpaired move left it stale) gets that directory listed."""
        dirs = {e.ident: p for p, e in res.manifest.entries.items() if e.type == "dir"}
        rst = os.fstat(self.root.fd)
        dirs[(rst.st_dev, rst.st_ino)] = ""
        extra: set[str] = set()
        relist: set[str] = set()
        for wd, name in taken.wd_dirty:
            info = self.wd_info.get(wd)
            if info is None or info[1] is None or info[2] != "tree":
                continue
            where = dirs.get(info[1])
            if where is None:
                continue
            p = join(where, name)
            if where != info[0]:
                if where:
                    relist.add(where)
                extra.add(p)
            elif p not in res.seen:
                extra.add(p)
        return extra, relist

    def commit(self, taken: Taken, full: bool, reason: str) -> list[dict]:
        t0 = self.clock()
        scan_start = self.wallclock()
        baseline = reason == "baseline"
        # Renames are trusted unless this is a full rescan after lost
        # notifications (an overflow, a failed batch write).
        trust = not full or baseline
        full = full or taken.root_dirty
        self.scanner.read_git = lambda: scan_git(self.root, self._git_watch, self.cfg.ignore_git_refs)
        res = self.scanner.scan(self.manifest, taken.dirty, taken.renames, full=full, git_dirty=taken.git_dirty or full, force_list=taken.moved_in_dirs)
        extra, relist = self._wd_hints(taken, res)
        if extra or relist:
            self.scanner.rescan(res, extra, [], False, relist)
        # Validation: a batch is settled only when nothing it read moved and
        # no new event arrived while it was being read.
        settled = False
        unsettled: set[str] = set()
        rounds = 0
        trace = [] if os.environ.get("WATCHER_DEBUG_ROUNDS") else None
        val_moved: set[str] = set()
        queue_left = False
        while rounds < VALIDATE_ROUNDS:
            rounds += 1
            # Read until a read empties the queue: one that stopped at its
            # cap proves nothing about what is still waiting.
            for _ in range(MAX_DRAIN_READS):
                self._read_raw()
                queue_left = bool(getattr(self.backend, "capped", False))
                if not queue_left:
                    break
            moved = self.scanner.validate(res)
            val_moved |= moved
            # Entries the scan could not read because they changed under it
            # (a race) are as unsettled as anything that moved.
            moved |= res.redirty
            res.redirty = set()
            lost, res.lost = res.lost, set()
            moved |= lost
            later = self.pending.take(self.clock()) if self.pending else None
            if not moved and not lost and later is None and not self.overflowed and not queue_left:
                settled = True
                break
            if self.overflowed or (later is not None and later.root_dirty):
                # Not acted on here: everything goes back with a deadline
                # (the overflow flag makes the next tick a full rescan).
                unsettled = {p for p in moved | (later.dirty if later else set()) if self.tracked_path(p)}
                if later is not None:
                    self.pending.requeue(later, self.clock(), self.wallclock())
                for p in moved:
                    self.pending.touch(p, self.clock(), self.wallclock())
                break
            dirty2 = set(moved)
            force2: set[str] = set(lost)
            if later is not None:
                for p in moved | res.looked | set(res.seen):
                    # Anything looked at before a rename that arrived since
                    # is looked at again where the rename put it.
                    q = rewrite(p, later.renames)
                    if q != p:
                        dirty2.add(q)
                force2 |= {rewrite(p, later.renames) for p in lost}
                # Earlier hints of this batch, carried through the renames
                # that arrived since: a path scanned (or found missing)
                # before a rename of its parent is looked at again where it
                # is now.
                for p in taken.dirty:
                    dirty2.add(rewrite(p, later.renames))
                    dirty2.add(rewrite(p, taken.renames + later.renames))
                taken.merge(later)
                dirty2 |= later.dirty
                force2 |= later.moved_in_dirs
            # Nothing read in an earlier round is reused for a path reported
            # or seen moving since.
            res.read_once = {}
            res.no_reuse = set(dirty2) | (later.dirty if later is not None else set())
            if trace is not None:
                trace.append({"moved": sorted(moved), "dirty": sorted(later.dirty) if later else [], "renames": later.renames if later else [], "force": sorted(force2), "wd": sorted(later.wd_dirty) if later else [], "listed_before": list(res.listed)})
            self.scanner.rescan(res, dirty2, later.renames if later else [], bool(later and later.git_dirty), force2)
            if later is not None:
                e2, r2 = self._wd_hints(later, res)
                if e2 or r2:
                    self.scanner.rescan(res, e2, [], False, r2)
            unsettled = dirty2
        if settled:
            unsettled = set()
        unsettled = {p for p in unsettled if self.tracked_path(p)}
        if not settled:
            # The last round's work was not validated: look at all of it
            # again in the next batch (carried through its renames there).
            now_, wall_ = self.clock(), self.wallclock()
            for p in unsettled | res.looked:
                if self.tracked_path(p):
                    self.pending.touch(p, now_, wall_)
        # The scan is over (validation included). A change no notification
        # covered (an entry of a directory listed in this scan, which may
        # have appeared before its watch existed, or anything the lstat
        # sweep flagged) is known only to have happened before now.
        scan_end = self.wallclock()
        hinted = taken.dirty

        def alone(paths: set[str]) -> set[str]:
            return {p for p in paths if p not in hinted and not any(under(h, p) for h in hinted)}

        # Whichever pass found it (the listing, the sweep or validation),
        # a change no notification covered is bounded by the end of the
        # scanning and, below, by the previous batch's scan start.
        found_alone = alone(res.swept_paths) | alone(val_moved)
        uncovered = not baseline and (bool(res.listed) or bool(found_alone))
        new = res.manifest
        if not res.git_read:
            new.git = self.manifest.git
        written = taken.final_writes()
        old = self.manifest
        self._carry_epochs(old, new, written)
        recs = delta.plan(old, new, taken.renames, written, trust_renames=trust, moved_out=taken.moved_out)
        check_error = None
        if recs is None:
            check_error = "the moves could not be put in a legal order"
        else:
            check_error = delta.check(old, new, recs)
        resync = check_error is not None
        if resync:
            self.resyncs += 1
            recs = delta.resync(old, new)
            err = delta.check(old, new, recs)
            if err is not None:
                raise RuntimeError(f"resync records do not reproduce the scanned tree: {err}")
        self.last_commit = {"settled": settled, "unsettled": sorted(unsettled)[:MAX_LISTED_HINTS], "validation_rounds": rounds}
        if not recs and not baseline and reason in ("settled", "forced", "mark", "final"):
            self.last_commit["batch"] = self.batch
            self._after(new, res)
            return []
        batch_no = self.batch + 1
        # Write times: "known" from change notifications; "none" when no
        # notification reported a change (the baseline, a change only the
        # sweep found); "unknown" when notifications were lost (an overflow,
        # a failed batch write). A change no notification covered happened
        # after the previous batch's scan began (the last moment the watcher
        # is known to have looked at the path) and before this scan ended.
        prev_scan = self.batch_info.get(self.batch, {}).get("scan_start")
        if baseline:
            times, lo = "none", None
        elif reason in ("overflow", "recovery"):
            times = "unknown"
            cands = [x for x in (prev_scan, taken.first_write_after) if x is not None]
            lo = min(cands) if cands else None
        elif taken.first_write is not None:
            times, lo = "known", taken.first_write_after
            if uncovered:
                taken.last_write = max(taken.last_write, scan_end)
                if prev_scan is not None:
                    lo = min(lo, prev_scan)
        else:
            times, lo = "none", prev_scan
        # Bounds as the records hold them (microseconds), so the watcher's
        # mark answers and replay's check compute with the same numbers.
        lo = _us(lo)
        if times == "known":
            taken.first_write, taken.last_write = _us(taken.first_write), _us(taken.last_write)
        out = []
        for r in recs:
            r["batch"] = batch_no
            out.append(self.events.stamp(r))
        brec = {
            "kind": "batch",
            "batch": batch_no,
            "records": len(recs),
            "reason": reason,
            "check": "failed" if resync else "ok",
            "resync": resync,
            "recovered": full and not baseline,
            "forced": taken.forced,
            "events": taken.events,
            "first_event": utc_from_epoch(taken.first_wall) if taken.first_wall else None,
            "last_event": utc_from_epoch(taken.last_wall) if taken.last_wall else None,
            "write_times": times,
            "first_write_after": utc_from_epoch(lo) if lo is not None else None,
            "first_write": utc_from_epoch(taken.first_write) if times == "known" else None,
            "last_write": utc_from_epoch(taken.last_write) if times == "known" else None,
            "scan_ms": round((self.clock() - t0) * 1000.0, 2),
            "reads": res.reads,
            "swept_dirty": res.swept,
            "listed": len(res.listed),
            "validation_listed": len(res.checked_children),
            "renames": [list(x) for x in taken.renames[:MAX_LISTED_HINTS]],
            "unstable": sorted(res.unstable)[:MAX_LISTED_HINTS],
            "unwatched": dict(sorted(self.unwatched.items())[:MAX_LISTED_HINTS]),
            "entries": len(new.entries),
            "settled": settled,
            "unsettled": sorted(unsettled)[:MAX_LISTED_HINTS],
            "validation_rounds": rounds,
            "root_accessible": res.root_accessible,
        }
        if check_error is not None:
            brec["check_error"] = check_error[:2000]
        if trace is not None:
            brec["debug_rounds"] = trace + [{"listed_final": list(res.listed), "first_dirty": sorted(taken.dirty)[:50]}]
        committed = _us(self.wallclock())
        out.append(self.events.stamp(brec, wall=committed))
        try:
            self.events.write(out)
        except BaseException:
            self.recover_full = True
            raise
        self.batch = batch_no
        self.last_commit["batch"] = batch_no
        self.batch_info[batch_no] = {"lo": lo if lo is not None else committed, "hi": taken.last_write if times == "known" else committed, "committed": committed, "settled": settled, "write_times": times, "scan_start": scan_start}
        self._after(new, res)
        if self.on_commit is not None:
            self.on_commit(batch_no, brec)
        return out

    @staticmethod
    def _carry_epochs(old: Manifest, new: Manifest, written: set[str]) -> None:
        """Unknown content: an unreadable file's write counter belongs to its
        inode. It is carried across scans and renames by identity, a write
        seen through any name bumps it once, and every surviving name of the
        inode shows the same value. Once nonzero it stays for the inode's
        life, also after the file becomes readable: what was written while
        it could not be read is never known (D-032)."""
        before: dict[tuple, int] = {}
        for e in old.entries.values():
            if e.type == "file" and (e.sha256 is None or e.epoch):
                before[e.ident] = max(before.get(e.ident, 0), e.epoch)
        bumped = {new.entries[w].ident for w in written if w in new.entries and new.entries[w].type == "file" and (new.entries[w].sha256 is None or before.get(new.entries[w].ident))}
        for p, e in list(new.entries.items()):
            if e.type != "file" or (e.sha256 is not None and not before.get(e.ident)):
                continue
            epoch = before.get(e.ident, 0)
            # A write seen while unreadable bumps it, also the first one (a
            # file created and written unreadable in one batch).
            if e.ident in bumped:
                epoch += 1
            if epoch != e.epoch:
                new.entries[p] = replace(e, epoch=epoch)

    def _after(self, new: Manifest, res) -> None:
        self._git_seen = new.git
        with self.lock:
            self.manifest = new
            if self.batch >= 0:
                self.snapshot = (self.batch, new.copy())
        self._reconcile_watches()
        now, wall = self.clock(), self.wallclock()
        for p in res.redirty | res.lost:
            self.pending.touch(p, now, wall)
        res.lost = set()

    def take_snapshot(self) -> tuple[int, Manifest] | None:
        with self.lock:
            return self.snapshot

    def close(self) -> None:
        self.root.close()
