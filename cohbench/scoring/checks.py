"""Checks on the final workspace, written as data in the answer key.

A check is an object:

    {"path": GLOB, "regex": PATTERN}                  some file matching GLOB has content matching PATTERN
    {"path": GLOB, "regex": PATTERN, "negate": true}  no file matching GLOB has content matching PATTERN
    {"path": GLOB, "absent": true}                    no file matches GLOB
    {"path": GLOB, "json": [ASSERT, ...]}             some file is one JSON document meeting every assertion

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
import json
import math
import re
from decimal import Decimal
from typing import Any

from .load import Episode

KEYS = {"path", "regex", "absent", "negate", "ignore_case", "json"}
MISSING = object()


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
        if isinstance(c, dict) and "json" in c:
            _parse_json_check(c)
            continue
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


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(v) for v in value)
    return type(value) is dict and all(type(k) is str and _json_value(v) for k, v in value.items())


def _parse_json_check(c):
    name = f"json check {c.get('path')!r}"
    if set(c) != {"path", "json"} or not isinstance(c["path"], str) or not c["path"]:
        raise CheckError(f"{name}: expected exactly path and json")
    if not isinstance(c["json"], list) or not c["json"]:
        raise CheckError(f"{name}: json must be a nonempty list of assertions")
    for i, assertion in enumerate(c["json"], 1):
        where = f"{name}, assertion {i}"
        if not isinstance(assertion, dict) or len(assertion) != 2 or "pointer" not in assertion:
            raise CheckError(f"{where}: expected pointer and one of equals, set_equals, absent")
        pointer = assertion["pointer"]
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")) or re.search(r"~(?![01])", pointer):
            raise CheckError(f"{where}: pointer must be an RFC 6901 JSON pointer")
        if "equals" in assertion:
            valid = _json_value(assertion["equals"])
        elif "set_equals" in assertion:
            valid = isinstance(assertion["set_equals"], list) and _json_value(assertion["set_equals"])
        else:
            valid = assertion.get("absent") is True
        if not valid:
            raise CheckError(f"{where}: expected equals with a JSON value, set_equals with a JSON list, or absent: true")


def _pointer(document, pointer):
    value = document
    for part in pointer.split("/")[1:] if pointer else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(part, MISSING)
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            # Bound the text before converting it, including very long indices.
            if len(part) > len(str(len(value))) or int(part) >= len(value):
                return MISSING
            value = value[int(part)]
        else:
            return MISSING
    return value


def _json_equal(a, b):
    numeric = (int, float, Decimal)
    if type(a) in numeric and type(b) in numeric:
        return Decimal(str(a)) == Decimal(str(b))
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


def _multiset_equal(a, b):
    if not isinstance(a, list) or len(a) != len(b):
        return False
    remaining = list(b)
    for value in a:
        match = next((i for i, other in enumerate(remaining) if _json_equal(value, other)), None)
        if match is None:
            return False
        remaining.pop(match)
    return True


def _invalid_constant(value):
    raise ValueError(f"not a JSON number: {value}")


def _json_document(data, assertions):
    try:
        document = json.loads(data.decode("utf-8"), parse_float=Decimal, parse_int=Decimal, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError):
        return False
    for assertion in assertions:
        value = _pointer(document, assertion["pointer"])
        if "absent" in assertion:
            held = value is MISSING
        elif value is MISSING:
            held = False
        elif "equals" in assertion:
            held = _json_equal(value, assertion["equals"])
        else:
            held = _multiset_equal(value, assertion["set_equals"])
        if not held:
            return False
    return True


def run_one(ep: Episode, batch: int | None, c: dict[str, Any]) -> tuple[bool | None, str]:
    tree, unknown = ep.tree(batch), ep.unknown(batch)
    files = sorted(p for p, v in tree.items() if v[0] == "file" and glob_match(c["path"], p))
    if c.get("absent") is True:
        return (not files), (f"no file matches {c['path']}" if not files else f"{files[0]} matches {c['path']}")
    rx = None if "json" in c else re.compile(c["regex"], re.MULTILINE | (re.IGNORECASE if c.get("ignore_case") else 0))
    hit, undecided = None, []
    for p in files:
        data = None if p in unknown else ep.content(tree[p][2])
        if data is None:
            undecided.append(p)
            continue
        matches = _json_document(data, c["json"]) if "json" in c else rx.search(data.decode("utf-8", errors="replace"))
        if matches:
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
