"""The tree manifest and the one function that applies a record to it.

The watcher's self-check and the host's `state_at` both use `apply_record`,
so a log the watcher accepted is a log that replays. Application is strict:
a record that is not legal on the current manifest raises ReplayError.

Legal means:
  created   path absent, parent present as a directory (or top level)
  deleted   path present and has no children
  moved     source present, destination absent, destination's parent present
            as a directory, destination not inside the source; same identity
            and type before and after; a directory carries its subtree
  modified, attrib
            path present; neither old nor new is a directory unless both are
  rewritten path present and the entry is unchanged
  git       sets the coarse git state
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


class ReplayError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    type: str  # file | dir | symlink | fifo | socket | char | block | other
    mode: int  # permission bits
    uid: int
    gid: int
    dev: int
    ino: int
    size: int | None  # files and symlinks
    sha256: str | None  # content, or the symlink target string
    content: str  # stored | oversize | unreadable | inaccessible | none
    target: str | None = None  # symlink target
    error: str | None = None  # errno name when content could not be read
    # Counts writes seen to a file whose content is unknown (unreadable):
    # two such states are different whenever a write happened between them.
    epoch: int = 0

    @property
    def ident(self) -> tuple[int, int]:
        return (self.dev, self.ino)

    def fields(self) -> dict:
        d = {"type": self.type, "mode": f"{self.mode:04o}", "uid": self.uid, "gid": self.gid, "dev": self.dev, "ino": self.ino, "size": self.size, "sha256": self.sha256, "content": self.content}
        if self.target is not None:
            d["target"] = self.target
        if self.error is not None:
            d["error"] = self.error
        if self.epoch:
            # Once a file this watcher cannot read has been written, its
            # content is unknown for the rest of its inode's life (decision
            # D-032). The counter itself is best-effort audit detail.
            d["epoch"] = self.epoch
            d["content_unknown"] = True
        return d


ENTRY_KEYS = ("type", "mode", "uid", "gid", "dev", "ino", "size", "sha256", "content")


def entry_from(rec: dict) -> Entry:
    try:
        missing = [k for k in ENTRY_KEYS if k not in rec]
        if missing:
            raise ReplayError(f"record {rec.get('seq')} lacks {missing}")
        return Entry(type=rec["type"], mode=int(rec["mode"], 8), uid=rec["uid"], gid=rec["gid"], dev=rec["dev"], ino=rec["ino"], size=rec["size"], sha256=rec["sha256"], content=rec["content"], target=rec.get("target"), error=rec.get("error"), epoch=int(rec.get("epoch", 0)))
    except (TypeError, ValueError) as e:
        raise ReplayError(f"record {rec.get('seq')}: bad entry fields: {e}") from None


def parent(p: str) -> str:
    i = p.rfind("/")
    return p[:i] if i > 0 else ""


def name_of(p: str) -> str:
    return p[p.rfind("/") + 1 :]


def depth(p: str) -> int:
    return p.count("/") + 1 if p else 0


def join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


def under(p: str, top: str) -> bool:
    return p == top or p.startswith(top + "/")


@dataclass
class Manifest:
    entries: dict[str, Entry] = field(default_factory=dict)
    git: dict | None = None
    _kids: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def of(cls, entries: dict[str, Entry], git: dict | None = None) -> "Manifest":
        m = cls(entries=dict(entries), git=git)
        for p in m.entries:
            m._kids.setdefault(parent(p), set()).add(p)
        return m

    def copy(self) -> "Manifest":
        m = Manifest(entries=dict(self.entries), git=self.git)
        m._kids = {k: set(v) for k, v in self._kids.items()}
        return m

    def children(self, p: str) -> set[str]:
        return self._kids.get(p, set())

    def subtree(self, p: str) -> list[str]:
        out, stack = [], [p]
        while stack:
            q = stack.pop()
            for c in self._kids.get(q, ()):
                out.append(c)
                stack.append(c)
        return out

    def _add(self, p: str, e: Entry) -> None:
        self.entries[p] = e
        self._kids.setdefault(parent(p), set()).add(p)

    def _remove(self, p: str) -> None:
        del self.entries[p]
        s = self._kids.get(parent(p))
        if s is not None:
            s.discard(p)
            if not s:
                del self._kids[parent(p)]
        self._kids.pop(p, None)

    def same(self, other: "Manifest") -> bool:
        return self.entries == other.entries and self.git == other.git

    def first_difference(self, other: "Manifest") -> str | None:
        if self.git != other.git:
            return f"git state {self.git!r} != {other.git!r}"
        for p in sorted(set(self.entries) | set(other.entries)):
            a, b = self.entries.get(p), other.entries.get(p)
            if a != b:
                return f"{p!r}: {a} != {b}"
        return None

    def _parent_ok(self, p: str) -> bool:
        par = parent(p)
        return par == "" or (par in self.entries and self.entries[par].type == "dir")


def apply_record(m: Manifest, rec: dict) -> None:
    kind = rec.get("kind")
    path = rec.get("path")
    seq = rec.get("seq")
    e = m.entries
    if kind in ("batch", "mark"):
        return
    if kind == "git":
        m.git = None if rec.get("head") is None and rec.get("refs") is None else {"head": rec.get("head"), "refs": dict(rec.get("refs") or {}), "backend": rec.get("backend")}
        return
    if not isinstance(path, str) or not path or path.startswith("/") or any(c in ("", ".", "..") for c in path.split("/")):
        raise ReplayError(f"seq {seq}: bad path {path!r}")
    if kind == "created":
        if path in e:
            raise ReplayError(f"seq {seq}: created {path!r}, which exists")
        if not m._parent_ok(path):
            raise ReplayError(f"seq {seq}: created {path!r} before its parent")
        m._add(path, entry_from(rec))
    elif kind == "deleted":
        if path not in e:
            raise ReplayError(f"seq {seq}: deleted {path!r}, which does not exist")
        if m.children(path):
            raise ReplayError(f"seq {seq}: deleted {path!r}, which still has children")
        m._remove(path)
    elif kind == "moved":
        src = rec.get("from")
        if src not in e:
            raise ReplayError(f"seq {seq}: moved from {src!r}, which does not exist")
        if path in e:
            raise ReplayError(f"seq {seq}: moved onto {path!r}, which exists")
        if under(path, src):
            raise ReplayError(f"seq {seq}: moved {src!r} into itself")
        if not m._parent_ok(path):
            raise ReplayError(f"seq {seq}: moved to {path!r} before its parent")
        new = entry_from(rec)
        old = e[src]
        if new.ident != old.ident or new.type != old.type:
            raise ReplayError(f"seq {seq}: moved record changes identity or type of {src!r}")
        sub = sorted(m.subtree(src), key=depth) if old.type == "dir" else []
        carried = [(k, e[k]) for k in sub]
        for k in reversed(sub):
            m._remove(k)
        m._remove(src)
        m._add(path, new)
        for k, v in carried:
            m._add(path + k[len(src) :], v)
    elif kind in ("modified", "attrib"):
        if path not in e:
            raise ReplayError(f"seq {seq}: {kind} {path!r}, which does not exist")
        new = entry_from(rec)
        if (new.type == "dir") != (e[path].type == "dir"):
            raise ReplayError(f"seq {seq}: {kind} {path!r} changes a directory's type")
        e[path] = new
    elif kind == "rewritten":
        if path not in e:
            raise ReplayError(f"seq {seq}: rewritten {path!r}, which does not exist")
        if entry_from(rec) != e[path]:
            raise ReplayError(f"seq {seq}: rewritten {path!r} with a different entry")
    else:
        raise ReplayError(f"seq {seq}: unknown record kind {kind!r}")


def with_content(e: Entry, content: str) -> Entry:
    return replace(e, content=content)
