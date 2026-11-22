"""Checks on the final workspace, written as data in the answer key.

A check is an object:

    {"path": GLOB, "regex": PATTERN}                  some file matching GLOB has content matching PATTERN
    {"path": GLOB, "regex": PATTERN, "negate": true}  no file matching GLOB has content matching PATTERN
    {"path": GLOB, "absent": true}                    no file matches GLOB

`GLOB` is matched against the tree's relative paths segment by segment:
`*`, `?` and `[...]` stay inside one segment and `**` matches any number of
segments, none included. `PATTERN` is a Python regular expression searched
in the file's text (UTF-8, undecodable bytes replaced) with `re.MULTILINE`,
and `re.IGNORECASE` when the check says `"ignore_case": true`.

A check holds, fails, or is undecided (None). It is undecided when the answer
depends on a matching file whose content the bundle does not hold: content
unknown (D-032), oversize, unreadable, or a blob missing. A list of checks
holds when every check holds, fails when one fails, and is undecided
otherwise.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from .load import Episode

KEYS = {"path", "regex", "absent", "negate", "ignore_case"}


class CheckError(ValueError):
    pass


def glob_match(pattern: str, path: str) -> bool:
    return _match(pattern.split("/"), path.split("/"))


def _match(p: list[str], s: list[str]) -> bool:
    if not p:
        return not s
    if p[0] == "**":
        return any(_match(p[1:], s[i:]) for i in range(len(s) + 1))
    return bool(s) and fnmatch.fnmatchcase(s[0], p[0]) and _match(p[1:], s[1:])


def parse(spec: Any) -> list[dict[str, Any]]:
    """A check or a list of checks, checked for shape. Raises CheckError."""
    items = spec if isinstance(spec, list) else [spec]
    if not items:
        raise CheckError("an empty list of checks")
    for c in items:
        if not isinstance(c, dict) or set(c) - KEYS or not isinstance(c.get("path"), str) or not c["path"]:
            raise CheckError(f"a check is {{path, regex | absent, negate?, ignore_case?}}: {c!r}"[:200])
        if ("regex" in c) == (c.get("absent") is True):
            raise CheckError("a check has exactly one of regex or absent: true")
        if "regex" in c:
            if not isinstance(c["regex"], str):
                raise CheckError("regex is a string")
            try:
                re.compile(c["regex"])
            except re.error as exc:
                raise CheckError(f"regex does not compile: {exc}") from None
        if "negate" in c and (not isinstance(c["negate"], bool) or "regex" not in c):
            raise CheckError("negate is a boolean, with regex only")
        if "ignore_case" in c and not isinstance(c["ignore_case"], bool):
            raise CheckError("ignore_case is a boolean")
    return items


def run_one(ep: Episode, batch: int | None, c: dict[str, Any]) -> tuple[bool | None, str]:
    tree, unknown = ep.tree(batch), ep.unknown(batch)
    files = sorted(p for p, v in tree.items() if v[0] == "file" and glob_match(c["path"], p))
    if c.get("absent") is True:
        return (not files), (f"no file matches {c['path']}" if not files else f"{files[0]} matches {c['path']}")
    rx = re.compile(c["regex"], re.MULTILINE | (re.IGNORECASE if c.get("ignore_case") else 0))
    hit, undecided = None, []
    for p in files:
        data = None if p in unknown else ep.content(tree[p][2])
        if data is None:
            undecided.append(p)
            continue
        if rx.search(data.decode("utf-8", errors="replace")):
            hit = p
            break
    negate = c.get("negate") is True
    if hit is not None:
        return (not negate), f"{hit} matches"
    if undecided:
        return None, f"content of {undecided[0]} is not in the bundle"
    return negate, (f"no file matching {c['path']} matches" if files else f"no file matches {c['path']}")


def run(ep: Episode, batch: int | None, spec: Any) -> dict[str, Any]:
    """Every check of `spec` on the tree after `batch`: {held, results}."""
    items = parse(spec)
    results = []
    for c in items:
        held, why = run_one(ep, batch, c)
        results.append({"held": held, "why": why})
    vals = [r["held"] for r in results]
    held = False if False in vals else (None if None in vals else True)
    return {"held": held, "results": results}
