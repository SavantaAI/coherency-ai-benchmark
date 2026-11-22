"""One scan: the previous manifest plus fresh readings of every dirty path.

Rules:
  - A dirty path is lstat'ed through the root descriptor. Unreachable (gone,
    or reachable only through a symlink) means absent, with its subtree.
  - A directory is listed, recursively, only when it is new at that path,
    has a different identity than before, was inaccessible, or has no watch.
    Its watch is added before it is listed.
  - A directory renamed inside the tree (a paired rename whose destination
    has the source's identity) is carried over with its subtree instead of
    re-listed, unless an ignore pattern could treat the two locations
    differently; then the destination is listed.
  - File content is read only when the (size, mtime, ctime) signature of the
    inode differs from the last read of that inode: one open and one read
    per changed file.
  - Every other path of a hard-linked inode that was rescanned is rescanned.
  - Ignore rules are applied to the current paths during the scan.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .blobs import BlobStore
from .gitstate import GIT
from .ignore import IgnoreRules
from .manifest import Entry, Manifest, depth, join, name_of, parent, under
from .safefs import GONE, O_CLOEXEC, O_DIRECTORY, O_NOATIME, O_PATH, SafeRoot


CACHE_MARGIN_NS = 2_000_000_000


class Watches(Protocol):
    def watch(self, rel: str, fd: int, ident: tuple[int, int]) -> bool: ...
    def has_watch(self, ident: tuple[int, int]) -> bool: ...


def ftype(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char"
    if stat.S_ISBLK(mode):
        return "block"
    return "other"


def ecode(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, str(e.errno))


class Race(Exception):
    """The entry changed between stat and read; scan it again next batch."""


@dataclass
class ScanResult:
    manifest: Manifest
    redirty: set[str] = field(default_factory=set)
    listed: list[str] = field(default_factory=list)
    listed_set: set[str] = field(default_factory=set)
    # Directories validation listed to compare their tracked children.
    checked_children: list[str] = field(default_factory=list)
    reads: int = 0
    unstable: set[str] = field(default_factory=set)
    swept: int = 0
    swept_paths: set[str] = field(default_factory=set)
    # path -> lstat signature when the scan built its entry, for validation
    seen: dict[str, tuple] = field(default_factory=dict)
    # (inode identity, signature) -> entry fields already read in this scan
    read_once: dict[tuple, tuple] = field(default_factory=dict)
    root_accessible: bool = True
    git_read: bool = False
    # Directories whose listing came out incomplete because the tree moved
    # under it (a subdirectory found by the listing could no longer be
    # opened by path, or turned out to be another directory): listed again.
    lost: set[str] = field(default_factory=set)
    # Every path the scan looked at, found or not.
    looked: set[str] = field(default_factory=set)
    # Paths reported or seen moving since an earlier round: never served
    # from a cache in this round.
    no_reuse: set[str] = field(default_factory=set)


def rewrite(p: str, renames: list[tuple[str, str]]) -> str:
    for s, d in renames:
        if under(p, s):
            p = d + p[len(s) :]
    return p


SINGLE_READ_MAX = 64 << 20


class Scanner:
    def __init__(self, root: SafeRoot, ignore: IgnoreRules, max_blob: int, blobs: BlobStore, watches: Watches, read_git: Callable[[], dict | None]) -> None:
        self.root = root
        self.ignore = ignore
        self.max_blob = max_blob
        self.blobs = blobs
        self.watches = watches
        self.read_git = read_git
        # inode identity -> (signature, sha256, size, content, error)
        self.cache: dict[tuple[int, int], tuple] = {}
        # path -> full lstat signature at its last scan, for the sweep
        self.statsig: dict[str, tuple] = {}
        self.sweep_enabled = True

    def tracked(self, rel: str) -> bool:
        if not rel:
            return False
        if under(rel, GIT):
            return False  # only the coarse git state is recorded
        return not self.ignore.ignored(rel)

    # ---- one entry -----------------------------------------------------

    def _dir_entry(self, st: os.stat_result, content: str = "none") -> Entry:
        return Entry(type="dir", mode=stat.S_IMODE(st.st_mode), uid=st.st_uid, gid=st.st_gid, dev=st.st_dev, ino=st.st_ino, size=None, sha256=None, content=content)

    def _entry(self, st: os.stat_result, name: str, dfd: int, res: ScanResult, rel: str) -> Entry:
        t = ftype(st.st_mode)
        base = dict(type=t, mode=stat.S_IMODE(st.st_mode), uid=st.st_uid, gid=st.st_gid, dev=st.st_dev, ino=st.st_ino)
        if t == "dir":
            return self._dir_entry(st)
        if t == "symlink":
            try:
                tgt = os.readlink(name, dir_fd=dfd)
            except OSError as e:
                if e.errno in GONE or e.errno == errno.EINVAL:
                    raise Race(rel) from None
                return Entry(size=st.st_size, sha256=None, content="unreadable", error=ecode(e), **base)
            raw = os.fsencode(tgt)
            sha = hashlib.sha256(raw).hexdigest()
            self.blobs.put(sha, raw)
            return Entry(size=len(raw), sha256=sha, content="stored", target=tgt, **base)
        if t != "file":
            return Entry(size=None, sha256=None, content="none", **base)
        # Mode and owner are part of the signature: whether the content can be
        # read depends on them, and a chmod in the same clock tick as an
        # earlier read can leave ctime unchanged.
        sig = _rsig(st)
        key = (st.st_dev, st.st_ino)
        fresh = rel in res.no_reuse
        hit = None if fresh else self.cache.get(key)
        if hit is not None and hit[0] == sig:
            _, sha, size, content, err = hit
            return Entry(size=size, sha256=sha, content=content, error=err, **base)
        # One content open per changed inode per scan, across hard links.
        once = None if fresh else res.read_once.get((key, sig))
        if once is not None:
            sha, size, content, err = once
            return Entry(size=size, sha256=sha, content=content, error=err, **base)
        e = self._read(st, name, dfd, res, rel, base, sig, key)
        if e.content != "unreadable" and rel not in res.unstable:
            # Shared with the other links of the inode in this pass only;
            # an unstable read is never shared.
            res.read_once[(key, sig)] = (e.sha256, e.size, e.content, e.error)
        return e

    def _read(self, st, name, dfd, res, rel, base, sig, key) -> Entry:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_CLOEXEC
        try:
            try:
                fd = os.open(name, flags | O_NOATIME, dir_fd=dfd)
            except PermissionError as e:
                if not O_NOATIME or e.errno != errno.EPERM:
                    raise
                fd = os.open(name, flags, dir_fd=dfd)
        except OSError as e:
            if e.errno in GONE:
                raise Race(rel) from None
            return Entry(size=st.st_size, sha256=None, content="unreadable", error=ecode(e), **base)
        res.reads += 1
        read_start = time.time_ns()
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_ino != st.st_ino:
                raise Race(rel)
            h = hashlib.sha256()
            keep = before.st_size <= self.max_blob
            # One read call for the whole file where memory allows, so a
            # watcher of the workspace sees a single access notification.
            data = os.read(fd, before.st_size + 1) if before.st_size < SINGLE_READ_MAX else b""
            h.update(data)
            total = len(data)
            kept = bytearray(data) if keep else None
            if before.st_size >= SINGLE_READ_MAX or total > before.st_size:
                while True:
                    chunk = os.read(fd, 1 << 24)
                    if not chunk:
                        break
                    h.update(chunk)
                    total += len(chunk)
                    if kept is not None:
                        if total > self.max_blob:
                            kept = None
                        else:
                            kept += chunk
            if kept is not None and total > self.max_blob:
                kept = None
            after = os.fstat(fd)
        finally:
            os.close(fd)
        sha = h.hexdigest()
        raw_total = total
        if kept is not None:
            self.blobs.put(sha, bytes(kept))
            content = "stored"
        else:
            content = "oversize"
        stable = _rsig(after) == _rsig(before) == sig and after.st_size == raw_total
        # Racy-clean guard: file timestamps are coarse (a clock tick of up to
        # 10 ms), so a write right after this read can leave size, mtime and
        # ctime unchanged. Only cache a reading of a file whose timestamps
        # are well older than the read itself.
        if not stable:
            res.unstable.add(rel)
            res.redirty.add(rel)
        elif max(after.st_mtime_ns, after.st_ctime_ns) < read_start - CACHE_MARGIN_NS:
            self.cache[key] = (sig, sha, total, content, None)
        base = dict(base, mode=stat.S_IMODE(after.st_mode), uid=after.st_uid, gid=after.st_gid)
        return Entry(size=total, sha256=sha, content=content, **base)

    # ---- tree operations -----------------------------------------------

    @staticmethod
    def _drop(m: Manifest, p: str) -> None:
        if p not in m.entries:
            return
        for k in sorted(m.subtree(p), key=depth, reverse=True):
            m._remove(k)
        m._remove(p)

    def _seen(self, res: ScanResult, p: str, st: os.stat_result) -> None:
        sig = _statsig(st)
        self.statsig[p] = sig
        res.seen[p] = sig

    def _list(self, rel: str, m: Manifest, res: ScanResult) -> None:
        """List `rel` (already in m as a directory, or the root) and everything below it."""
        stack = [rel]
        while stack:
            d = stack.pop()
            want = m.entries.get(d)
            try:
                # Always a fresh open file description: on tmpfs a directory
                # read again through an old one can miss entries created
                # since it was opened (offset directories, Linux 6.6 and later).
                fd = self.root.open_dir(d)
            except OSError as e:
                if e.errno in GONE:
                    if d:
                        if d != rel:
                            res.lost.add(parent(d))
                        self._drop(m, d)
                    continue
                if d == "":
                    res.root_accessible = False
                    for k in list(m.children("")):
                        self._drop(m, k)
                elif want is not None:
                    for k in list(m.children(d)):
                        self._drop(m, k)
                    m.entries[d] = _inacc(want)
                continue
            try:
                fst = os.fstat(fd)
                if d and want is not None and (fst.st_dev, fst.st_ino) != want.ident:
                    res.redirty.add(d)
                    if d != rel:
                        res.lost.add(parent(d))
                    self._drop(m, d)
                    continue
                self.watches.watch(d, fd, (fst.st_dev, fst.st_ino))
                try:
                    names = sorted(os.listdir(fd))
                except PermissionError:
                    if d:
                        m.entries[d] = _inacc(m.entries[d])
                    else:
                        res.root_accessible = False
                    continue
                if d:
                    self._seen(res, d, fst)
                res.listed.append(d)
                res.listed_set.add(d)
                for k in [c for c in m.children(d)]:
                    self._drop(m, k)
                for name in names:
                    child = join(d, name)
                    if not self.tracked(child):
                        continue
                    try:
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError as e:
                        if e.errno in GONE:
                            continue
                        # Readable but not searchable: the listing is all we get.
                        for k in [c for c in m.children(d)]:
                            self._drop(m, k)
                        if d:
                            m.entries[d] = _inacc(m.entries[d])
                        else:
                            res.root_accessible = False
                        break
                    self._seen(res, child, st)
                    if stat.S_ISDIR(st.st_mode):
                        if accessible(st):
                            m._add(child, self._dir_entry(st))
                            stack.append(child)
                        else:
                            m._add(child, self._dir_entry(st, "inaccessible"))
                    else:
                        try:
                            m._add(child, self._entry(st, name, fd, res, child))
                        except Race:
                            res.redirty.add(child)
            finally:
                os.close(fd)

    def _scan_path(self, p: str, m: Manifest, res: ScanResult, force_list: bool = False) -> None:
        res.looked.add(p)
        if not self.tracked(p):
            self._drop(m, p)
            return
        par = parent(p)
        if par and (par not in m.entries or m.entries[par].type != "dir"):
            # Its parent is gone from the scanned tree; so is it.
            self._drop(m, p)
            return
        st, err = self.root.lstat(p)
        old = m.entries.get(p)
        if st is None:
            # Gone, reachable only through a symlink, or behind a directory
            # this process cannot search: not part of the scanned tree.
            self._drop(m, p)
            return
        ident = (st.st_dev, st.st_ino)
        self._seen(res, p, st)
        if stat.S_ISDIR(st.st_mode):
            fresh = force_list or old is None or old.type != "dir" or old.ident != ident or old.content == "inaccessible" or not self.watches.has_watch(ident)
            if not accessible(st):
                if old is not None and old.type == "dir" and old.ident == ident:
                    for k in list(m.children(p)):
                        self._drop(m, k)
                    m.entries[p] = self._dir_entry(st, "inaccessible")
                else:
                    self._drop(m, p)
                    m._add(p, self._dir_entry(st, "inaccessible"))
                return
            if fresh:
                self._drop(m, p)
                m._add(p, self._dir_entry(st))
                self._list(p, m, res)
            else:
                m.entries[p] = self._dir_entry(st)
            return
        try:
            pfd, name = self.root.open_parent(p)
        except OSError:
            self._drop(m, p)
            return
        try:
            e = self._entry(st, name, pfd, res, p)
        except Race:
            res.redirty.add(p)
            return
        finally:
            os.close(pfd)
        if old is not None and old.type == "dir":
            self._drop(m, p)
        if p in m.entries:
            m.entries[p] = e
        else:
            m._add(p, e)

    def scan(self, old: Manifest, dirty: set[str], renames: list[tuple[str, str]], full: bool, git_dirty: bool, force_list: set[str] | None = None) -> ScanResult:
        m = Manifest() if full else old.copy()
        res = ScanResult(manifest=m)
        if full:
            self._list("", m, res)
            self._finish(res, git=True)
        else:
            self.rescan(res, dirty, renames, git_dirty, force_list or set(), sweep=self.sweep_enabled)
        return res

    def rescan(self, res: ScanResult, dirty: set[str], renames: list[tuple[str, str]], git_dirty: bool, force_list: set[str], sweep: bool = False) -> None:
        """Bring res.manifest up to date for these dirty paths (in place).
        Every rescan starts with an empty per-pass read memo: nothing read in
        an earlier pass is reused."""
        res.read_once = {}
        m = res.manifest
        performed = [(s, d) for s, d in renames if self._relocate(m, s, d)]
        todo = self._sweep(m, res) if sweep else set()
        for p in dirty | force_list:
            # Where the path is now (all observed renames), and where the
            # manifest's entry for it went (the relocations done above):
            # both may be stale.
            todo.add(p)
            todo.add(rewrite(p, renames))
            todo.add(rewrite(p, performed))
        for s, d in renames:
            todo.add(s)
            todo.add(d)
        todo.discard("")
        local_listed: set[str] = set()
        scanned: set[str] = set()
        before = len(res.listed)
        for p in sorted(todo, key=lambda q: (depth(q), q)):
            if _covered(p, local_listed):
                continue
            self._scan_path(p, m, res, force_list=p in force_list)
            for d in res.listed[before:]:
                local_listed.add(d)
            before = len(res.listed)
            scanned.add(p)
        listed_files = {c for d in local_listed for c in m.children(d)}
        self._link_closure(m, res, scanned | listed_files)
        self._finish(res, git=git_dirty)

    def _finish(self, res: ScanResult, git: bool) -> None:
        m = res.manifest
        self._fix_orphans(m, res)
        for k in [k for k in self.statsig if k not in m.entries]:
            del self.statsig[k]
        if git:
            m.git = self.read_git()
            res.git_read = True
        live = {e.ident for e in m.entries.values() if e.type == "file"}
        for k in [k for k in self.cache if k not in live]:
            del self.cache[k]

    def _stat_children(self, par: str, names: list[str]) -> dict[str, os.stat_result | None]:
        """lstat names inside `par`, resolving `par` with the component-safe
        resolver (an O_PATH open, which raises no inotify event) and the
        names with fstatat relative to it. Never a pathname with a slash.
        Returns None for every name when `par` cannot be resolved."""
        out: dict[str, os.stat_result | None] = {}
        try:
            pfd = self.root.open(par, (O_PATH or os.O_RDONLY) | O_DIRECTORY) if par else os.dup(self.root.fd)
        except OSError:
            return {n: None for n in names}
        try:
            for n in names:
                try:
                    out[n] = os.stat(n, dir_fd=pfd, follow_symlinks=False)
                except OSError:
                    out[n] = None
        finally:
            os.close(pfd)
        return out

    def _sweep(self, m: Manifest, res: ScanResult) -> set[str]:
        """lstat every known path and return those whose inode, mode, owner,
        size, mtime or ctime moved since their last scan. This catches what
        no hint names: a change made through a hard link that was itself
        replaced within the same batch, for example. Directory by directory
        through the component-safe resolver; nothing is opened for reading,
        so inotify watchers see nothing."""
        out: set[str] = set()
        by_parent: dict[str, list[str]] = {}
        for p in m.entries:
            by_parent.setdefault(parent(p), []).append(p)
        for par, kids in by_parent.items():
            sts = self._stat_children(par, [name_of(k) for k in kids])
            for k in kids:
                st = sts.get(name_of(k))
                if st is None or _statsig(st) != self.statsig.get(k):
                    out.add(k)
        res.swept = len(out)
        res.swept_paths = set(out)
        return out

    def validate(self, res: ScanResult) -> set[str]:
        """Re-lstat every path of the scanned tree (and every path this scan
        looked at), through the resolver. Returns those whose identity, mode,
        owner, size, mtime or ctime moved since they were last stat'ed: the
        scan did not see one instant. Covers the whole tree, not only what
        this scan read, because a change through a hard link raises an event
        under one name only."""
        moved: set[str] = set()
        dir_meta: dict[str, os.stat_result] = {}
        want: dict[str, tuple | None] = {}
        for p in res.manifest.entries:
            want[p] = self.statsig.get(p)
        for p, sig in res.seen.items():
            if p not in want:
                want[p] = None if p not in res.manifest.entries else sig
        by_parent: dict[str, list[str]] = {}
        for p in want:
            by_parent.setdefault(parent(p), []).append(p)
        for par, kids in by_parent.items():
            sts = self._stat_children(par, [name_of(k) for k in kids])
            for k in kids:
                st = sts.get(name_of(k))
                if k not in res.manifest.entries:
                    if st is not None:
                        moved.add(k)  # gone from the scan, present now
                elif st is None or _statsig(st) != want[k]:
                    w = want[k]
                    if st is not None and w is not None and stat.S_ISDIR(st.st_mode) and res.manifest.entries[k].type == "dir" and (st.st_ino, st.st_mode, st.st_uid, st.st_gid) == (w[0], w[1], w[2], w[3]):
                        dir_meta[k] = st  # only times or size moved: check the children
                    else:
                        moved.add(k)
        for k, st in dir_meta.items():
            if self._tracked_children_agree(k, res.manifest, res):
                # Ignored children come and go without making the batch
                # unsettled: what matters is the tracked child set.
                self.statsig[k] = _statsig(st)
                if k in res.seen:
                    res.seen[k] = _statsig(st)
            else:
                moved.add(k)
        return moved

    def _tracked_children_agree(self, d: str, m: Manifest, res: ScanResult | None = None) -> bool:
        if res is not None:
            res.checked_children.append(d)
        e = m.entries.get(d)
        if e is None or e.content == "inaccessible":
            return False
        try:
            fd = self.root.open_dir(d)
        except OSError:
            return False
        try:
            names = {n for n in os.listdir(fd) if self.tracked(join(d, n))}
        except OSError:
            return False
        finally:
            os.close(fd)
        return names == {name_of(c) for c in m.children(d)}

    def _relocate(self, m: Manifest, s: str, d: str) -> bool:
        """Carry a renamed directory's subtree to its new path when its
        identity there is confirmed and no ignore pattern could differ."""
        e = m.entries.get(s)
        if e is None or e.type != "dir" or under(d, s) or d in m.entries:
            return False
        if not self.tracked(d) or self.ignore.may_affect(s) or self.ignore.may_affect(d) or under(d, GIT) or under(GIT, d):
            return False
        par = parent(d)
        if par and (par not in m.entries or m.entries[par].type != "dir"):
            return False
        st, _ = self.root.lstat(d)
        if st is None or not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != e.ident:
            return False
        sub = sorted(m.subtree(s), key=depth)
        carried = [(k, m.entries[k]) for k in sub]
        for k in reversed(sub):
            m._remove(k)
        m._remove(s)
        m._add(d, e)
        for k, v in carried:
            m._add(d + k[len(s) :], v)
        for k in [s] + [k for k, _ in carried]:
            if k in self.statsig:
                self.statsig[d + k[len(s) :]] = self.statsig.pop(k)
        return True

    def _link_closure(self, m: Manifest, res: ScanResult, scanned: set[str]) -> None:
        by_ident: dict[tuple[int, int], list[str]] = {}
        for p, e in m.entries.items():
            if e.type != "dir":
                by_ident.setdefault(e.ident, []).append(p)
        todo = [p for p in scanned if p in m.entries and m.entries[p].type != "dir" and len(by_ident.get(m.entries[p].ident, ())) > 1]
        done = set(scanned)
        while todo:
            p = todo.pop()
            for q in by_ident.get(m.entries[p].ident, []) if p in m.entries else []:
                if q not in done:
                    done.add(q)
                    self._scan_path(q, m, res)
                    todo.append(q)

    def _fix_orphans(self, m: Manifest, res: ScanResult) -> None:
        for p in sorted(m.entries, key=depth):
            if p not in m.entries:
                continue
            par = parent(p)
            if par and (par not in m.entries or m.entries[par].type != "dir"):
                res.redirty.add(p)
                self._drop(m, p)


def _rsig(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_uid, st.st_gid)


def _statsig(st: os.stat_result) -> tuple:
    return (st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _inacc(e: Entry) -> Entry:
    from dataclasses import replace

    return replace(e, content="inaccessible")


def _covered(p: str, listed: set[str]) -> bool:
    if "" in listed:
        return True
    while p:
        if p in listed:
            return True
        p = parent(p)
    return False


def accessible(st: os.stat_result) -> bool:
    """Whether this process can list and search a directory, by its mode
    bits, without opening it (an open is visible to inotify watchers)."""
    euid = os.geteuid()
    if euid == 0:
        return True
    m = st.st_mode
    if st.st_uid == euid:
        need = stat.S_IRUSR | stat.S_IXUSR
    elif st.st_gid == os.getegid() or st.st_gid in os.getgroups():
        need = stat.S_IRGRP | stat.S_IXGRP
    else:
        need = stat.S_IROTH | stat.S_IXOTH
    return m & need == need
