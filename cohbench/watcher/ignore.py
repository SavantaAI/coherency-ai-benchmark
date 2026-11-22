"""Ignore rules: glob patterns matched against paths relative to the root.

Syntax:
  `*`   any run of characters inside one path segment
  `?`   one character inside a segment
  `[..]` a character class, as in fnmatch (`[!..]` negates)
  `**`  as a whole segment: zero or more segments

A pattern is matched against the whole relative path, with `/` separators and
no leading `./`. A path is ignored when the pattern matches the path itself or
any of its ancestors, so `build` and `build/**` both ignore the directory and
everything below it, while `*.pyc` matches only at the top level (write
`**/*.pyc` for every depth).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field


def _segment_regex(seg: str) -> str:
    out = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2 if seg[i + 1 : i + 2] in ("!", "]") else i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = seg[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                body = body.replace("\\", "\\\\")
                out.append(f"(?!/)[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one glob pattern to an anchored regular expression."""
    pat = pattern.strip()
    if not pat:
        raise ValueError("empty ignore pattern")
    if pat.startswith("/"):
        raise ValueError(f"ignore pattern must be relative to the root: {pattern!r}")
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.rstrip("/")
    segs = pat.split("/")
    rx = ""
    open_sep = False  # True when rx already ends with a separator
    for idx, seg in enumerate(segs):
        last = idx == len(segs) - 1
        if seg == "**":
            if last:
                # `x/**` also matches `x` itself; a bare `**` matches anything.
                rx += ".*" if not rx else "(?:/.*)?"
            else:
                rx += "(?:[^/]+/)*" if not rx else "/(?:[^/]+/)*"
                open_sep = True
        else:
            if rx and not open_sep:
                rx += "/"
            rx += _segment_regex(seg)
            open_sep = False
    return re.compile("^" + rx + "$", re.DOTALL)


# Temporary-file forms of an ignored name, matched against one component:
# atomic writers' temporaries (a leading dot or a `.tmp` suffix, often with a
# random tail), editor backups and swap files, Emacs lock and autosave names.
DEFAULT_NAME_FORMS = [
    "{name}",
    ".{name}.tmp",
    ".{name}.tmp.*",
    ".{name}.tmp-*",
    ".{name}.??????",
    "{name}.tmp",
    "{name}.tmp.*",
    "{name}.tmp-*",
    "{name}~",
    ".{name}.swp",
    ".{name}.swo",
    ".{name}.swx",
    "{name}.swp",
    ".#{name}",
    "#{name}#",
]


@dataclass
class IgnoreRules:
    patterns: list[str] = field(default_factory=list)
    # Basenames ignored at any depth (a path with such a component is ignored).
    names: list[str] = field(default_factory=list)
    # Temporary-file forms of those names, as fnmatch patterns over one path
    # component with `{name}` standing for the name. See DEFAULT_NAME_FORMS.
    forms: list[str] = field(default_factory=lambda: list(DEFAULT_NAME_FORMS))

    def __post_init__(self) -> None:
        self._compiled = [compile_pattern(p) for p in self.patterns]
        self._names = set(self.names)
        self._forms = [re.compile(fnmatch.translate(f.replace("{name}", n))) for n in self.names for f in self.forms if f != "{name}"]
        for n in self.names:
            if not n or "/" in n or n in (".", ".."):
                raise ValueError(f"an ignored name is one path component: {n!r}")
        self._literal = []
        for p in self.patterns:
            lead = []
            q = p.strip()
            while q.startswith("./"):
                q = q[2:]
            for seg in q.rstrip("/").split("/"):
                if any(c in seg for c in "*?["):
                    break
                lead.append(seg)
            self._literal.append(lead)

    def may_affect(self, prefix: str) -> bool:
        """Conservative: False only if no pattern can match `prefix`, an
        ancestor of it, or anything below it. Ignored names do not count: a
        rename does not change the names inside the subtree it carries."""
        segs = prefix.split("/") if prefix else []
        for lead in self._literal:
            n = min(len(lead), len(segs))
            if lead[:n] == segs[:n]:
                return True
        return False

    def _match_exact(self, rel: str) -> bool:
        return any(rx.match(rel) for rx in self._compiled)

    def ignored(self, rel: str) -> bool:
        """True when `rel` or any ancestor of it matches a pattern, or any of
        its components is an ignored name."""
        if not rel:
            return False
        segs = rel.split("/")
        if self.names and any(s in self._names or any(rx.match(s) for rx in self._forms) for s in segs):
            # An ignored name as a whole component, or one of its listed
            # temporary forms (`.CLAUDE.md.tmp`, `CLAUDE.md~`, ...). Other
            # names that merely contain it (`AGENTS.md.example`) are kept.
            return True
        if not self._compiled:
            return False
        for n in range(1, len(segs) + 1):
            if self._match_exact("/".join(segs[:n])):
                return True
        return False
