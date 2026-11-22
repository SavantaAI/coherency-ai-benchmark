"""The task workspace as the agent will see it: exactly the files the caller
declares, nothing else.

A task workspace comes with a manifest: every relative path meant for the
agent, with its type, mode, size and SHA-256 (files) or target (symlinks).
`pack` reads a host directory or a caller's tar, refuses any entry that is
not in the manifest and any manifest entry that is missing or differs, never
packs host metadata (`.DS_Store` and the like, which may sit in a task
directory on a host but are not task content), and returns the tar that
populates the volume. A workspace without a manifest is refused.

The names in the manifest are checked once, before population, against a
narrow pattern: words that would name the measurement itself. A coding task
legitimately has `tests/`, `test_*.py` and scripts that measure things, so
the broad pattern used for this project's own names (cohbench/env/checks.py)
does not apply to task content, and nothing the agent writes later is gated.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

AGENT_UID = 1000
AGENT_GID = 1000

# Host metadata: never packed, never required in a manifest.
HOST_METADATA = re.compile(r"(^|/)(\.DS_Store|\._[^/]*|\.AppleDouble|\.Spotlight-V100|\.Trashes|\.fseventsd|Thumbs\.db|desktop\.ini|Icon\r)(/|$)")

# The one list of words that must not reach the agent because they would
# name the measurement itself (D-019). Narrow on purpose: a coding task has
# tests, scripts that measure things and scores of its own. Used here on the
# workspace manifest's names, and by the runner on every text it sends.
MEASUREMENT_WORDS = re.compile(
    r"cohbench|coheren(?:cy|ce)|benchmark|\beval\b|answer[ _-]?key|user[ _-]?card|scorer|probe|test[ _-]?harness|simulated[ _-]?user",
    re.IGNORECASE,
)
MEASUREMENT_NAMES = MEASUREMENT_WORDS  # the earlier name


def telling_words(text: str) -> list[str]:
    """The measurement words in a text (a message, a prompt, a name), in
    order, lower-cased. Empty when the text may reach the agent."""
    return [m.group(0).lower() for m in MEASUREMENT_WORDS.finditer(text)]


def telling_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """The names (paths) that hold a measurement word."""
    return [n for n in names if MEASUREMENT_WORDS.search(n)]


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    path: str  # relative, POSIX, no `.` or `..` segments
    type: str  # file, dir, symlink
    mode: int | None = None
    size: int | None = None
    sha256: str | None = None
    target: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "type": self.type}
        if self.mode is not None:
            d["mode"] = f"{self.mode:04o}"
        if self.type == "file":
            d["size"], d["sha256"] = self.size, self.sha256
        if self.type == "symlink":
            d["target"] = self.target
        return d


@dataclass
class Manifest:
    entries: dict[str, Entry] = field(default_factory=dict)
    # D-027: one modification time (epoch seconds) for the whole workspace,
    # part of the task. Packing gives it to every entry and refuses a
    # manifest without it.
    mtime: int | None = None

    @staticmethod
    def load(src: str | Path | dict | list) -> "Manifest":
        """From a JSON file, or `{"mtime": <epoch seconds>, "entries": [...]}`,
        or a list of entries (which has no mtime, so cannot be packed). Each
        entry: `path`, `type` (default file), `mode` (octal string, optional),
        and `size` and `sha256` for a file or `target` for a symlink."""
        if isinstance(src, (str, Path)):
            src = json.loads(Path(src).read_text())
        rows = src["entries"] if isinstance(src, dict) else src
        m = Manifest()
        if isinstance(src, dict) and src.get("mtime") is not None:
            t = src["mtime"]
            if not isinstance(t, int) or isinstance(t, bool) or t < 0:
                raise WorkspaceError(f"the manifest's mtime must be whole epoch seconds, not {t!r}")
            m.mtime = t
        for r in rows:
            path = _clean(r["path"])
            kind = r.get("type", "file")
            if kind not in ("file", "dir", "symlink"):
                raise WorkspaceError(f"{path}: type {kind!r}")
            mode = int(r["mode"], 8) if isinstance(r.get("mode"), str) else r.get("mode")
            if kind == "file" and (not isinstance(r.get("size"), int) or not isinstance(r.get("sha256"), str)):
                raise WorkspaceError(f"{path}: a file needs size and sha256")
            if kind == "symlink" and not isinstance(r.get("target"), str):
                raise WorkspaceError(f"{path}: a symlink needs target")
            if path in m.entries:
                raise WorkspaceError(f"{path}: listed twice")
            m.entries[path] = Entry(path, kind, mode, r.get("size"), r.get("sha256"), r.get("target"))
        return m

    def as_dict(self) -> dict[str, Any]:
        return {"mtime": self.mtime, "entries": [e.as_dict() for e in sorted(self.entries.values(), key=lambda e: e.path)]}


def _clean(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
        raise WorkspaceError(f"{path!r} is not a clean relative path")
    return str(p)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def manifest_for(src: Path, *, mtime: int | None = None) -> Manifest:
    """Every entry under a host directory, host metadata left out: a helper
    for writing a task's manifest, not a substitute for one. `mtime` is the
    workspace's one time (D-027); by default the newest regular file's, read
    once here and then fixed in the manifest."""
    m = Manifest()
    newest = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(src).as_posix()
            if HOST_METADATA.search(rel):
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                m.entries[rel] = Entry(rel, "symlink", None, None, None, os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                m.entries[rel] = Entry(rel, "dir", stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                m.entries[rel] = Entry(rel, "file", stat.S_IMODE(st.st_mode), st.st_size, _sha256(p))
                newest = max(newest, int(st.st_mtime))
    m.mtime = mtime if mtime is not None else newest
    return m


@dataclass
class Packed:
    tar: bytes
    entries: int
    newest_mtime: int
    host_metadata_skipped: list[str]


def check_names(manifest: Manifest) -> list[str]:
    """Manifest paths, and the target strings of declared symlinks, with a
    word that names the measurement."""
    bad = telling_names(list(manifest.entries))
    for rel, e in manifest.entries.items():
        if e.type == "symlink" and e.target and telling_words(e.target):
            bad.append(f"{rel} -> {e.target}")
    return bad


def reserved(manifest: Manifest, names: tuple[str, ...]) -> list[str]:
    """Manifest paths whose basename is reserved (D-026: the harnesses'
    instruction files, which the watcher never records, may not be task files)."""
    # Any path component, as the watcher ignores a reserved name at any depth
    # (a directory named AGENTS.md included).
    return [p for p in manifest.entries if any(part in names for part in p.split("/"))]


def pack(source: Path | bytes, manifest: Manifest, *, uid: int = AGENT_UID, gid: int = AGENT_GID) -> Packed:
    """The tar for the volume: exactly the manifest's entries, owned by the
    agent's user, each entry's type, mode and link target from the manifest
    and every entry timed at the manifest's one mtime (D-027, D-031); the
    host supplies file contents only. Raises WorkspaceError on any
    difference between the source and the manifest, an unlisted directory,
    a missing mode, and a manifest without mtime."""
    if manifest.mtime is None:
        raise WorkspaceError("the manifest has no mtime: D-027 makes the workspace's time part of the task")
    found: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    skipped: list[str] = []
    if isinstance(source, (str, Path)):
        src = Path(source)
        if not src.is_dir():
            raise WorkspaceError(f"{src} is not a directory")
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames.sort()
            for name in sorted(dirnames) + sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(src).as_posix()
                if HOST_METADATA.search(rel):
                    skipped.append(rel)
                    if p.is_dir() and not p.is_symlink():
                        dirnames[:] = [d for d in dirnames if d != name]
                    continue
                st = p.lstat()
                info = tarfile.TarInfo("./" + rel)
                info.mode, info.mtime = stat.S_IMODE(st.st_mode), int(st.st_mtime)
                data = None
                if stat.S_ISLNK(st.st_mode):
                    info.type, info.linkname = tarfile.SYMTYPE, os.readlink(p)
                elif stat.S_ISDIR(st.st_mode):
                    info.type = tarfile.DIRTYPE
                elif stat.S_ISREG(st.st_mode):
                    data = p.read_bytes()
                    info.size = len(data)
                else:
                    raise WorkspaceError(f"{rel}: not a file, directory or symlink")
                found[rel] = (info, data)
    else:
        with tarfile.open(fileobj=io.BytesIO(source)) as t:
            for m in t.getmembers():
                raw = m.name
                parts = [x for x in raw.lstrip("/").split("/") if x not in ("", ".")]
                if not parts:
                    continue
                if ".." in parts:
                    raise WorkspaceError(f"unsafe member {raw!r}")
                rel = "/".join(parts)
                if HOST_METADATA.search(rel):
                    skipped.append(rel)
                    continue
                if m.islnk():
                    raise WorkspaceError(f"{rel}: hard links in a caller's tar are not supported; list both paths as files")
                data = t.extractfile(m).read() if m.isfile() else None
                info = tarfile.TarInfo("./" + rel)
                info.mode, info.mtime = m.mode, int(m.mtime)
                if m.issym():
                    info.type, info.linkname = tarfile.SYMTYPE, m.linkname
                elif m.isdir():
                    info.type = tarfile.DIRTYPE
                elif m.isfile():
                    info.size = len(data or b"")
                else:
                    raise WorkspaceError(f"{rel}: not a file, directory or symlink")
                found[rel] = (info, data)

    problems: list[str] = []
    # D-031: what the agent sees comes from the manifest alone: every entry's
    # type, mode and link target, and the one mtime. The host only supplies
    # file contents, which must match the manifest's hashes. Every directory
    # is listed with its mode; a directory the manifest does not list is an
    # error, never an implied 0755.
    for rel, e in manifest.entries.items():
        if e.type in ("file", "dir") and e.mode is None:
            problems.append(f"{rel}: the manifest gives no mode")
        parent = str(PurePosixPath(rel).parent)
        while parent not in ("", "."):
            pe = manifest.entries.get(parent)
            if pe is None or pe.type != "dir":
                problems.append(f"{rel}: its directory {parent} is not listed as a directory")
                break
            parent = str(PurePosixPath(parent).parent)
    for rel, (info, data) in found.items():
        e = manifest.entries.get(rel)
        if e is None:
            problems.append(f"not in the manifest: {rel}")
            continue
        kind = "dir" if info.isdir() else "symlink" if info.issym() else "file"
        if kind != e.type:
            problems.append(f"{rel}: is a {kind}, the manifest says {e.type}")
            continue
        if kind == "file":
            if len(data or b"") != e.size or hashlib.sha256(data or b"").hexdigest() != e.sha256:
                problems.append(f"{rel}: size or content differs from the manifest")
        if kind == "symlink" and info.linkname != e.target:
            problems.append(f"{rel}: target {info.linkname!r}, the manifest says {e.target!r}")
        # The manifest's mode and target, not the host's.
        if e.mode is not None and kind != "symlink":
            info.mode = e.mode
        if kind == "symlink":
            info.mode, info.linkname = 0o777, e.target or ""
    for rel in manifest.entries:
        if rel not in found:
            problems.append(f"missing: {rel}")
    if problems:
        raise WorkspaceError("the workspace does not match its manifest: " + "; ".join(sorted(set(problems))[:30]) + (f" (and {len(problems) - 30} more)" if len(problems) > 30 else ""))

    buf = io.BytesIO()
    newest = manifest.mtime
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as out:
        root = tarfile.TarInfo(".")
        root.type, root.mode, root.mtime, root.uid, root.gid = tarfile.DIRTYPE, 0o755, newest, uid, gid
        out.addfile(root)
        for rel in sorted(found, key=lambda r: (r.count("/"), r)):
            info, data = found[rel]
            info.uid, info.gid, info.uname, info.gname = uid, gid, "", ""
            info.mtime = newest
            out.addfile(info, io.BytesIO(data) if data is not None else None)
    return Packed(buf.getvalue(), len(found), newest, skipped)


def add_instruction_sentence(tar_bytes: bytes, file_name: str, sentence: str, *, mtime: int, uid: int = AGENT_UID, gid: int = AGENT_GID) -> tuple[bytes, dict]:
    """Coached mode (SPEC section 2): the sentence appended on its own line to
    the instruction file at the workspace root, which is created when the
    task has none. The file gets `mtime`, the workspace's own newest file
    time, not the time of the run. In a Git workspace a created file shows as
    untracked, which is what a file the user just added looks like."""
    if "/" in file_name or not file_name:
        raise ValueError("the instruction file sits at the workspace root")
    out = io.BytesIO()
    target = "./" + file_name
    found = False
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as src, tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name in (target, file_name) and m.isfile():
                found = True
                text = (data or b"").decode("utf-8")
                if text and not text.endswith("\n"):
                    text += "\n"
                if text:
                    text += "\n"
                data = (text + sentence + "\n").encode("utf-8")
                m.size = len(data)
                m.mtime = mtime
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
        if not found:
            data = (sentence + "\n").encode("utf-8")
            info = tarfile.TarInfo(target)
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, uid, gid, mtime
            dst.addfile(info, io.BytesIO(data))
    return out.getvalue(), {"file": file_name, "existed": found, "sentence": sentence, "mtime": mtime}
