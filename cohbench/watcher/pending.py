"""When to scan, and what: dirty paths collected from inotify hints.

One batch at a time. The batch is due when no event has arrived for
`quiet` seconds and no file is open for writing (an IN_MODIFY not yet
followed by IN_CLOSE_WRITE), or when its first event is `max_delay` old,
whichever comes first. `deadline()` is that moment, so a caller that sleeps
until it and then takes the batch never spins: once the deadline has
passed, `due()` is true. Whatever is outstanding (paths, renames, the root
or git flags) always comes with a deadline: `outstanding()` and
`deadline()` agree, and Core checks that after every batch.

Write hints belong to a file lifetime, not a path. The ops log records, in
order, writes, renames, and the end of a lifetime at a path (deletion,
creation of a new file there, a move out, a rename onto it). A write
follows its file through later renames and is dropped when that file's
lifetime ends.

Only activity on tracked paths reaches this class: Core drops events about
ignored names, ignored globs and ignored git refs before they touch the
clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _under(p: str, top: str) -> bool:
    return p == top or p.startswith(top + "/")


@dataclass
class Taken:
    dirty: set[str] = field(default_factory=set)
    # ("write", path) | ("rename", src, dst) | ("gone", path), in arrival order
    ops: list[tuple] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    # (watch descriptor, name): the same hint by the watch it arrived on,
    # resolved by the directory's identity after the scan
    wd_dirty: set[tuple[int, str]] = field(default_factory=set)
    # directories that arrived by an unpaired IN_MOVED_TO: listed again
    moved_in_dirs: set[str] = field(default_factory=set)
    root_dirty: bool = False
    moved_out: set[str] = field(default_factory=set)  # IN_MOVED_FROM seen, pair not seen
    git_dirty: bool = False
    forced: bool = False  # taken at max_delay while still busy
    events: int = 0
    first: float | None = None
    last: float | None = None
    first_wall: float | None = None
    last_wall: float | None = None
    # The first and last notifications that reported a change (create,
    # modify, move, delete, attribute change), apart from the scheduling
    # clock: an open or a close alone moves neither, and a hint the watcher
    # made up itself (a retry) moves neither. None for a batch found only by
    # a sweep.
    first_write: float | None = None
    last_write: float | None = None
    # A lower bound for the first write: the start of the queue read before
    # the one that delivered the first change notification. The write
    # happened after it and at or before first_write (D-037, timing by read).
    first_write_after: float | None = None
    # The read (Core.feed_epoch) that brought this batch's first hint: a mark
    # is covered once no open batch holds a hint read before its instant.
    first_epoch: int | None = None

    def merge(self, later: "Taken") -> None:
        """Append hints that arrived after this batch was taken."""
        self.dirty |= later.dirty
        self.ops.extend(later.ops)
        self.renames.extend(later.renames)
        self.wd_dirty |= later.wd_dirty
        self.moved_in_dirs |= later.moved_in_dirs
        self.moved_out |= later.moved_out
        self.root_dirty = self.root_dirty or later.root_dirty
        self.git_dirty = self.git_dirty or later.git_dirty
        self.forced = self.forced or later.forced
        self.events += later.events
        if self.first is None:
            self.first, self.first_wall = later.first, later.first_wall
        if later.last is not None:
            self.last, self.last_wall = later.last, later.last_wall
        if self.first_write is None:
            self.first_write = later.first_write
            self.first_write_after = later.first_write_after
        if later.last_write is not None:
            self.last_write = later.last_write
        if later.first_epoch is not None and (self.first_epoch is None or later.first_epoch < self.first_epoch):
            self.first_epoch = later.first_epoch

    def final_writes(self) -> set[str]:
        """Where each written file is now, for files whose lifetime did not
        end after the write."""
        live: list[str] = []
        for op in self.ops:
            if op[0] == "write":
                if op[1] not in live:
                    live.append(op[1])
            elif op[0] == "gone":
                live = [p for p in live if not _under(p, op[1])]
            elif op[0] == "rename":
                s, d = op[1], op[2]
                live = [p for p in live if not _under(p, d)]  # the target is replaced
                live = [d + p[len(s) :] if _under(p, s) else p for p in live]
        return set(live)


class Pending:
    def __init__(self, quiet_s: float, max_delay_s: float) -> None:
        self.quiet = quiet_s
        self.max_delay = max_delay_s
        self.open_writes: dict[str, float] = {}  # path -> time of its last IN_MODIFY
        self.epoch = 0  # the current read (Core sets it before each feed)
        self.read_after: float | None = None  # start of the read before the current one
        self._reset()

    def _reset(self) -> None:
        self.dirty: set[str] = set()
        self.ops: list[tuple] = []
        self.wd_dirty: set[tuple[int, str]] = set()
        self.moved_in_dirs: set[str] = set()
        self.root_dirty = False
        self.renames: list[tuple[str, str]] = []
        self.moved_out: set[str] = set()
        self.git_dirty = False
        self.events = 0
        self.first: float | None = None
        self.last: float | None = None
        self.first_wall: float | None = None
        self.last_wall: float | None = None
        self.first_write: float | None = None
        self.last_write: float | None = None
        self.first_write_after: float | None = None
        self.first_epoch: int | None = None
        self._moved_writers: dict[str, dict[str, float]] = {}

    def __bool__(self) -> bool:
        return self.first is not None

    def outstanding(self) -> bool:
        """Anything a later batch must act on."""
        return bool(self.dirty or self.renames or self.root_dirty or self.git_dirty or self.moved_in_dirs or self.wd_dirty)

    def note(self, now: float, wall: float, change: bool = False) -> None:
        if self.first is None:
            self.first, self.first_wall = now, wall
            self.first_epoch = self.epoch
        self.last, self.last_wall = now, wall
        self.events += 1
        if change:
            if self.first_write is None:
                self.first_write = wall
                self.first_write_after = self.read_after if self.read_after is not None else wall
            self.last_write = wall

    def _drop_writers(self, path: str) -> dict[str, float]:
        gone = {p: t for p, t in self.open_writes.items() if _under(p, path)}
        for p in gone:
            del self.open_writes[p]
        return gone

    def touch(self, path: str, now: float, wall: float, *, modify: bool = False, close_write: bool = False, created: bool = False, deleted: bool = False, change: bool = False, wd: int | None = None, name: str | None = None) -> None:
        self.note(now, wall, change)
        self.dirty.add(path)
        if wd is not None and name:
            self.wd_dirty.add((wd, name))
        if created or deleted:
            self.ops.append(("gone", path))
            # The file (or directory) that was open there is gone or
            # replaced: its writer no longer holds any batch.
            self._drop_writers(path)
        if modify:
            self.ops.append(("write", path))
            self.open_writes[path] = now
        if close_write:
            self.open_writes.pop(path, None)

    def rename(self, src: str, dst: str, now: float, wall: float) -> None:
        self.note(now, wall, True)
        self.renames.append((src, dst))
        self.ops.append(("rename", src, dst))
        self.moved_out.discard(src)
        self.dirty.add(src)
        self.dirty.add(dst)
        self._drop_writers(dst)  # whatever was open at the target is replaced
        moving = self._moved_writers.pop(src, {})
        moving.update(self._drop_writers(src))
        for p, t in moving.items():
            self.open_writes[dst + p[len(src) :]] = t

    def moved_from(self, src: str, now: float, wall: float) -> None:
        # Its lifetime at this path ends unless the paired IN_MOVED_TO turns
        # this into a rename (rename() then carries the writes instead).
        self.touch(src, now, wall, change=True)
        self.moved_out.add(src)
        self._moved_writers[src] = self._drop_writers(src)

    def drop_writers(self, path: str) -> None:
        """Nothing open at or below `path` holds a batch any more."""
        self._drop_writers(path)

    def moved_away(self, src: str) -> None:
        """The source of a move whose other end is not tracked: its writers
        are gone for good."""
        self._moved_writers.pop(src, None)

    def git(self, now: float, wall: float) -> None:
        self.note(now, wall, True)
        self.git_dirty = True

    def mark_root(self, now: float, wall: float) -> None:
        self.note(now, wall, True)
        self.root_dirty = True

    def requeue(self, t: Taken, now: float, wall: float) -> None:
        """Give back hints that were taken and not acted on, with a deadline."""
        self.dirty |= t.dirty
        self.ops = t.ops + self.ops
        self.renames = t.renames + self.renames
        self.wd_dirty |= t.wd_dirty
        self.moved_in_dirs |= t.moved_in_dirs
        self.moved_out |= t.moved_out
        self.root_dirty = self.root_dirty or t.root_dirty
        self.git_dirty = self.git_dirty or t.git_dirty
        self.events += t.events
        if t.first is not None and (self.first is None or t.first < self.first):
            self.first, self.first_wall = t.first, t.first_wall
        if self.first is None:
            self.note(now, wall)
            self.events -= 1
        if self.last is None or (t.last is not None and t.last > self.last):
            self.last, self.last_wall = (t.last, t.last_wall) if t.last is not None else (now, wall)
        if t.first_write is not None and (self.first_write is None or t.first_write < self.first_write):
            self.first_write = t.first_write
            self.first_write_after = t.first_write_after
        if t.last_write is not None and (self.last_write is None or t.last_write > self.last_write):
            self.last_write = t.last_write
        if t.first_epoch is not None and (self.first_epoch is None or t.first_epoch < self.first_epoch):
            self.first_epoch = t.first_epoch

    def deadline(self) -> float | None:
        if self.first is None:
            if self.outstanding():
                raise RuntimeError("pending work without a deadline")
            return None
        # Quiet once no event has arrived for `quiet` and every open writer
        # has expired (silent for max_delay since its last write), whichever
        # is later; never later than max_delay after the first event. The
        # writers' expiry times are absolute, so a writer that expires while
        # the watcher waits releases the batch with no further event.
        quiet = self.last + self.quiet
        expiry = max((t + self.max_delay for t in self.open_writes.values()), default=None)
        if expiry is not None and expiry > quiet:
            quiet = expiry
        return min(self.first + self.max_delay, quiet)

    def due(self, now: float) -> bool:
        d = self.deadline()
        return d is not None and now >= d

    def take(self, now: float) -> Taken:
        busy = any(now - w <= self.max_delay for w in self.open_writes.values()) or (self.last is not None and now - self.last < self.quiet)
        t = Taken(dirty=self.dirty, ops=self.ops, renames=self.renames, wd_dirty=self.wd_dirty, moved_in_dirs=self.moved_in_dirs, root_dirty=self.root_dirty, moved_out=self.moved_out, git_dirty=self.git_dirty, forced=busy, events=self.events, first=self.first, last=self.last, first_wall=self.first_wall, last_wall=self.last_wall, first_write=self.first_write, last_write=self.last_write, first_write_after=self.first_write_after, first_epoch=self.first_epoch)
        still_open = {p: w for p, w in self.open_writes.items() if now - w < self.max_delay}
        self._reset()
        # A file still being written holds the next batch too, for a while.
        self.open_writes = still_open
        return t
