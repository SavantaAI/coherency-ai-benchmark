"""Content fingerprints for local Docker build contexts. No Docker required."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from pathlib import Path


def _pattern_regex(pattern: str):
    """Docker wildcards use path separators, with ** spanning directories."""
    parts = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        i += 1
        if c == "*":
            if i < len(pattern) and pattern[i] == "*":
                i += 1
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
                    parts.append("(?:.*/)?")
                else:
                    parts.append(".*")
            else:
                parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i)
            if end < 0:
                raise ValueError("unclosed character class in Docker ignore pattern")
            chars = pattern[i:end]
            if not chars or chars == "^":
                raise ValueError("empty character class in Docker ignore pattern")
            parts.append("(?!/)[" + chars.replace("[", r"\[") + "]")
            i = end + 1
        elif c == "\\":
            if i == len(pattern):
                raise ValueError("unfinished escape in Docker ignore pattern")
            parts.append(re.escape(pattern[i]))
            i += 1
        else:
            parts.append(re.escape(c))
    return re.compile("".join(parts) + r"\Z")


def _ignore_rules(path: Path):
    rules = []
    if not path.is_file():
        return rules
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("#"):
            continue
        pattern = line.strip()
        if not pattern:
            continue
        include = pattern.startswith("!")
        if include:
            pattern = pattern[1:].strip()
            if not pattern:
                raise ValueError("empty Docker ignore exception")
        pattern = posixpath.normpath(pattern).strip("/")
        if pattern in ("", "."):
            continue
        rules.append((include, pattern, _pattern_regex(pattern)))
    return rules


def _included(name: str, rules):
    ancestors = [name]
    while "/" in name:
        name = name.rsplit("/", 1)[0]
        ancestors.append(name)
    included = True
    for include, _, pattern in rules:
        if any(pattern.fullmatch(path) for path in ancestors):
            included = include
    return included


def _may_include_children(name: str, rules):
    # A literal prefix can rule out whole excluded trees without reading them.
    for include, pattern, _ in rules:
        if include:
            prefix = re.split(r"[*?\[\\]", pattern, maxsplit=1)[0]
            if prefix.startswith(name + "/") or (name + "/").startswith(prefix):
                return True
    return False


def _context_paths(context: Path, rules):
    paths = set()

    def visit(directory: Path):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(context).as_posix()
            included = _included(name, rules)
            if not included and not _may_include_children(name, rules):
                continue
            if included:
                paths.add(path)
                paths.update(
                    parent
                    for parent in path.parents
                    if parent != context and context in parent.parents
                )
            if not path.is_symlink() and path.is_dir():
                visit(path)

    visit(context)
    return sorted(paths, key=lambda path: path.relative_to(context).as_posix())


def inputs_digest(
    dockerfile: Path,
    context: Path,
    build_args: dict[str, str],
    from_digests: dict[str, str] | None = None,
):
    """Hash recipe, filtered context, arguments and locally built parents.

    File records use relative POSIX paths, types, permission bits and SHA-256
    of bytes or symlink targets. Times, owners and host paths are omitted.
    """
    context = Path(context).absolute()
    dockerfile = Path(dockerfile).absolute()
    specific = dockerfile.with_name(dockerfile.name + ".dockerignore")
    ignore = specific if specific.is_file() else context / ".dockerignore"
    rules = _ignore_rules(ignore)
    digest = hashlib.sha256()

    def add(record):
        data = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    def entry(path: Path, name: str):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            add([name, "symlink", mode, os.readlink(path)])
        elif stat.S_ISDIR(info.st_mode):
            add([name, "directory", mode])
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").hexdigest()
            add([name, "file", mode, content])
        else:
            raise ValueError(f"unsupported build context entry: {name}")

    add(["inputs-v1", build_args, from_digests or {}])
    # Docker sends these control files even when ignore rules exclude them.
    entry(dockerfile, "dockerfile")
    if dockerfile.is_symlink():
        entry(dockerfile.resolve(), "dockerfile-target")
    if ignore.is_file():
        entry(ignore, "ignore")
        if ignore.is_symlink():
            entry(ignore.resolve(), "ignore-target")
    for path in _context_paths(context, rules):
        entry(path, "context/" + path.relative_to(context).as_posix())
    return digest.hexdigest()
