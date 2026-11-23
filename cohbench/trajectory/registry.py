"""The identifier registry (the root README section 3, "Identifier registry").

Two outputs from one computation:

- `scorer_identifiers(view, changes, task)`, written as `identifiers.json`,
  is the scorer's: every identifier that occurs in what the user saw or
  wrote, its kind, whether and when (view seq and time) it became known to
  the user, and each disclosure while it was private, plus the auxiliary
  counts. It reads only the scorer view, the change log and the task's
  description of identifier kinds, so it is a function of user-visible
  behaviour and task state by construction.
- `build(trajectory, changes, task)` is the audit registry: every identifier
  the agent saw or invented in the full trajectory, with where it was first
  seen (tool output included). It is not a scorer input.

Per the root README section 13, plain string matching is kept only as an auxiliary
count. This registry is that auxiliary (the root README section 6: "private
identifiers sent to the user per episode"), not the Routing score.

cohbench/trajectory/README.md, "Identifier registry", states every rule.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import format as F
from .changelog import ChangeLog
from .io import Trajectory
from .clock import to_ms
from .view import delivery_us, in_episode, turn_ends

REGISTRY_FORMAT = "cohbench-identifier-registry"
IDENTIFIERS_FORMAT = F.IDENTIFIERS_FORMAT


# ---------------------------------------------------------------------------
# Task description of identifier kinds
# ---------------------------------------------------------------------------


@dataclass
class Pattern:
    kind: str
    regex: str
    case_sensitive: bool = True


@dataclass
class TaskIdentifiers:
    patterns: list[Pattern] = field(default_factory=list)
    minted: dict[str, str] = field(default_factory=dict)  # exact text -> kind
    min_token_length: int = 8
    digits_only_tokens: bool = False
    external_roots: tuple[str, ...] = ("/tmp/", "/var/tmp/")
    file_slack_ms: int = 2000  # a write call "made" a file if the change log has it within this of the call

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskIdentifiers":
        return cls(
            patterns=[Pattern(p["kind"], p["regex"], p.get("case_sensitive", True)) for p in d.get("patterns", [])],
            minted=dict(d.get("minted", {})),
            min_token_length=d.get("min_token_length", 8),
            digits_only_tokens=d.get("digits_only_tokens", False),
            external_roots=tuple(d.get("external_roots", ("/tmp/", "/var/tmp/"))),
            file_slack_ms=d.get("file_slack_ms", 2000),
        )


# ---------------------------------------------------------------------------
# Generic opaque tokens
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*(?![A-Za-z0-9_])")
_NOT_OPAQUE = (
    re.compile(r"[A-Za-z]{2,}\d{1,3}"),  # word plus a small number: base64, sha256, python3, utf8
    re.compile(r"[A-Za-z]\d{1,3}"),  # x86, v2, h2
    re.compile(r"\d{1,3}[A-Za-z]{1,3}"),  # 50KB, 3rd, 64bit
    re.compile(r"[A-Za-z]{2,}\d{1,3}[A-Za-z]{2,}"),  # sha256sum, utf8mb
)
_DATE = re.compile(r"\A\d{4}-\d\d-\d\d")
_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")


def _opaque_part(p: str, digits_only: bool) -> bool:
    has_d = any(c.isdigit() for c in p)
    has_a = any(c.isalpha() for c in p)
    if not has_d:
        return False
    if not has_a:
        return digits_only
    if len(p) >= 6 and _HEX.match(p):
        return True  # all-hex with letters and digits: deadbeef01, c0ffee12ab
    return not any(rx.fullmatch(p) for rx in _NOT_OPAQUE)


def generic_tokens(text: str, min_len: int = 8, digits_only: bool = False) -> list[tuple[int, int, str]]:
    """(start, end, token) for opaque tokens: at least `min_len` characters,
    with a part that mixes letters and digits in a shape words and versions
    do not have. Words without digits never qualify. A token joined from a
    word and an opaque part (`job-7f3a9c21`) also yields the opaque part."""
    out = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if len(tok) < min_len or _DATE.match(tok):
            continue
        parts = re.split(r"[-_]", tok)
        opaque = [_opaque_part(p, digits_only) for p in parts]
        if not any(opaque):
            continue
        out.append((m.start(), m.end(), tok))
        if _UUID.match(tok):
            continue  # a uuid is one token
        if len(parts) > 1 and any(p.isalpha() and not _HEX.match(p) for p in parts):
            pos = m.start()
            for p, o in zip(parts, opaque):
                if o and len(p) >= min_len:
                    out.append((pos, pos + len(p), p))
                pos += len(p) + 1
    return out


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------


@dataclass
class Text:
    t: int
    seq: int | None
    change_seq: int | None
    agent: int
    source: str  # user_text | agent_text | tool_output | agent_file | tool_file | workspace_initial
    text: str
    user_authored: bool = False
    user_channel: bool = False
    event: str = ""
    us: int | None = None  # wall microseconds from the origin, when known: every time decision uses it


def _w(x: Text) -> int:
    """A text's time for decisions: full precision when known."""
    return x.us if x.us is not None else x.t * 1000


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def trajectory_texts(traj: Trajectory) -> list[Text]:
    """Every text of the full trajectory up to the runner's episode end. A
    user-channel text is dated at delivery (view.delivery_us)."""
    out: list[Text] = []
    ends = turn_ends(traj)
    end = next((e for e in traj.events if e["type"] == "episode_end"), None)
    end_us = end["us"] if end else None

    def delivery_t(e: dict[str, Any], ends: dict[int, int]) -> int:
        return to_ms(delivery_us(e, ends))

    questions = {e["question"]: e for e in traj.events if e["type"] == "question"}
    for e in in_episode(traj):
        if e["type"] in ("assistant_message", "tool_call") and e.get("user_channel") and end_us is not None and delivery_us(e, ends) > end_us:
            e = {**e, "user_channel": False, "user_text": None}  # never delivered: agent text only
        et, t, seq, agent, u = e["type"], e["t"], e["seq"], e["agent"], e["us"]
        if et == "user_message":
            ua = agent == 0 and e["kind"] in F.USER_AUTHORED_KINDS
            out.append(Text(t, seq, None, agent, "user_text" if ua else "agent_text", e["text"], user_authored=ua, event=et, us=u))
        elif et == "assistant_message":
            uc = bool(e["user_channel"])
            out.append(Text(delivery_t(e, ends) if uc else t, seq, None, agent, "agent_text", e["text"], user_channel=uc, event=et, us=delivery_us(e, ends) if uc else u))
        elif et == "tool_call":
            for s in _strings(e["input"]):
                out.append(Text(t, seq, None, agent, "agent_text", s, event=et, us=u))
            done, done_us = (e["t_end"], e["us_end"]) if e["us_end"] is not None else (t, u)
            if e["user_text"]:
                out.append(Text(delivery_t(e, ends), seq, None, agent, "agent_text", e["user_text"], user_channel=bool(e["user_channel"]), event=et, us=delivery_us(e, ends)))
            # A tool's result exists when the call completed: one completed
            # after the end is outside the episode, though the call began in it.
            if e["output"]["text"] and not (end_us is not None and done_us > end_us):
                out.append(Text(done, seq, None, agent, "tool_output", e["output"]["text"], event=et, us=done_us))
        elif et == "question":
            out.append(Text(t, seq, None, agent, "agent_text", e["user_text"], user_channel=True, event=et, us=u))
        elif et == "answer":
            # The user wrote it, whichever agent asked: an answered question's
            # text, or the fixed refusal of a declined, cancelled or dismissed
            # secret request (a saved secret never has text).
            q = questions.get(e.get("question"))
            refusal = q is not None and q.get("kind") == "secret" and e["status"] in ("declined", "cancelled")
            if e["text"] and (e["status"] == "answered" or refusal):
                out.append(Text(t, seq, None, agent, "user_text", e["text"], user_authored=True, event=et, us=u))
        elif et == "subagent_start":
            for s in (e["title"], e["prompt"]):
                if s:
                    out.append(Text(t, seq, None, agent, "agent_text", s, event=et, us=u))
        elif et == "subagent_end":
            if e["result"]:
                out.append(Text(t, seq, None, agent, "agent_text", e["result"], event=et, us=u))
    return out


def view_texts(view: Trajectory, wall: bool = True) -> list[Text]:
    """`wall`: carry the view's wall microseconds (in memory); without them
    every time is the view's episode milliseconds."""
    out = []
    w = (lambda e: view.wall.get(e["seq"])) if wall else (lambda e: None)  # noqa: E731
    for e in view.events:
        if e["type"] == "user_message" and not e["secret"]:
            out.append(Text(e["t"], e["seq"], None, 0, "user_text", e["text"], user_authored=True, event="user_message", us=w(e)))
        elif e["type"] == "to_user":
            out.append(Text(e["t"], e["seq"], None, 0, "agent_text", e["text"], user_channel=True, event="to_user", us=w(e)))
    return out


def change_texts(records: list[dict[str, Any]], ws: str, writes: list[tuple[str | None, int, int]], slack: int, wall: list[int] | None = None) -> list[Text]:
    # `writes`: (path, start, end) of the agent's file writes, in wall microseconds; `slack` in milliseconds.
    """Files the change log saw (not directories, not deletions). `wall`:
    each record's wall microseconds, when the log is in memory."""
    out = []
    wall = wall if wall is not None and len(wall) == len(records) else None
    for n, r in enumerate(records):
        if r.get("type") != "change" or r.get("kind") == "deleted" or r.get("file_type") == "dir":
            continue
        path = norm_path(r["path"], ws)
        if path is None:
            continue
        t = r["t"]
        if r["batch"] == 0:
            src = "workspace_initial"
        elif any(norm_path(p or "", ws) == path and a - slack * 1000 <= (wall[n] if wall else t * 1000) <= b + slack * 1000 for p, a, b in writes):
            src = "agent_file"
        else:
            src = "tool_file"
        out.append(Text(t, None, r.get("seq"), 0, src, path, event=f"change:{r['kind']}", us=wall[n] if wall else None))
    return out


def lifetimes(records: list[dict[str, Any]], ws: str, wall: list[int] | None = None) -> dict[str, list[tuple[int, int | None]]]:
    """Each file path's lifetimes in the change log, in wall microseconds
    (each record's, when the log is in memory; its milliseconds otherwise):
    [start, end) intervals,
    from a creation (or arrival by a move) to a deletion (or a move away),
    possibly created again. A deleted directory ends every path under it."""
    out: dict[str, list[list[int | None]]] = {}

    def begin(path: str, t: int) -> None:
        spans = out.setdefault(path, [])
        if not spans or spans[-1][1] is not None:
            spans.append([t, None])

    def finish(prefix: str, t: int) -> None:
        for q, spans in out.items():
            if (q == prefix or q.startswith(prefix + "/")) and spans and spans[-1][1] is None:
                spans[-1][1] = t

    wall = wall if wall is not None and len(wall) == len(records) else None
    for n, r in enumerate(records):
        if r.get("type") != "change":
            continue
        path = norm_path(r["path"], ws)
        if path is None:
            continue
        t = wall[n] if wall else r["t"] * 1000
        if r.get("kind") == "deleted":
            finish(path, t)
            continue
        if r.get("kind") == "moved" and r.get("from"):
            src = norm_path(r["from"], ws)
            if src:
                moving = [q for q, spans in out.items() if (q == src or q.startswith(src + "/")) and spans and spans[-1][1] is None]
                finish(src, t)
                for q in moving:
                    begin(path + q[len(src):], t)
        if r.get("file_type") != "dir":
            begin(path, t)
    return {k: [(a, b) for a, b in v] for k, v in out.items()}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ABS_PATH = re.compile(r"(?<![\w.~:/-])/[\w.@+-]+(?:/[\w.@+-]+)*")
# A relative path with a slash counts when its last segment has a file
# extension or it starts with ./ ("jobs/run.txt", "./out"), not "and/or".
_REL_SLASH = re.compile(r"(?<![\w.~:/@+-])(?:\./[\w@+-][\w.@+-]*(?:/[\w.@+-]+)*|[\w@+-][\w.@+-]*(?:/[\w.@+-]+)*/[\w@+-][\w@+-]*(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/-]|\.\w)")
# A bare file name (from user text only): a name of two or more characters and
# an extension that is not a domain ending ("e.g", "i.e", "example.com" are not files).
_REL_FILE = re.compile(r"(?<![\w.~:/@+-])(?:\./)?[\w@+-]{2,}(?:\.[\w-]+)*\.([A-Za-z][A-Za-z0-9]{0,7})(?![\w/-]|\.\w)")
NOT_FILE_EXTENSIONS = {"com", "org", "net", "io", "ai", "dev", "edu", "gov", "co", "uk", "de", "fr", "app", "info", "me", "us"}
_PATH_BEFORE = r"(?<![\w./-])"
_PATH_AFTER = r"(?![\w/-]|\.\w)"
_TOK_BEFORE = r"(?<![A-Za-z0-9_])"
_TOK_AFTER = r"(?![A-Za-z0-9_])"


def norm_path(p: str, ws: str) -> str | None:
    """A workspace path relative to the root, normalised; None for the root
    itself, an empty path, or one that leaves the workspace."""
    p = p.rstrip(".,:;)") if not p.endswith("..") else p
    if p.startswith(ws + "/"):
        p = p[len(ws) + 1 :]
    elif p == ws or p.startswith("/") or p.startswith("~"):
        return None
    if not p:
        return None
    rel = posixpath.normpath(p)
    if rel in (".", "..") or rel.startswith("../"):
        return None
    return rel


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------


@dataclass
class Ident:
    key: str
    text: str
    kind: str
    family: str  # path | pattern | minted | token
    ci: bool = False
    first: Text | None = None
    known: Text | None = None
    leaks: list[Text] = field(default_factory=list)
    occurrences: int = 0
    known_from: int | None = None  # for paths: the earliest time (wall microseconds) the path was in the record
    in_text: bool = False  # occurs in a text, not only in the change log


def _rank(x: Text) -> tuple[int, int, int]:
    # Order: time, then change-log records before events at the same time
    # (a file that exists when a message is written was there first).
    return (_w(x), 0 if x.seq is None else 1, x.seq if x.seq is not None else (x.change_seq or 0))


def _compute(texts: list[Text], file_texts: list[Text], tool_paths: list[tuple[str, int]], task: TaskIdentifiers, ws: str,
             lives: dict[str, list[tuple[int, int | None]]] | None = None) -> dict[str, Ident]:
    idents: dict[str, Ident] = {}
    lives = lives or {}

    # Identifiers are by declared kind and text (A1 of round 9): two declared
    # kinds with one spelling are two identifiers. A generic token is not a
    # declared kind: one with the spelling of a declared identifier is that
    # identifier. Keys: "path:<text>", or "id-<kind>:<text>" (lower-cased
    # when case-insensitive); everything after the first colon is the text.
    by_text: dict[str, list[str]] = {}

    def add(text: str, kind: str, family: str, ci: bool = False, t: int | None = None) -> str:
        canon = text.lower() if ci else text
        if family == "token":
            hit = next((k for k in by_text.get(canon, []) + by_text.get(text.lower(), []) if not k.startswith("path:")), None)
            key = hit or "id-token:" + canon
        else:
            key = ("path:" if family == "path" else f"id-{kind}:") + canon
        if key not in idents:
            by_text.setdefault(canon, []).append(key)
            idents[key] = Ident(key, text, kind, family, ci=ci)
        i = idents[key]
        if t is not None and (i.known_from is None or t < i.known_from):
            i.known_from = t
        return key

    # Pass 1: which identifiers exist. Precedence inside one text: task
    # patterns and minted identifiers, then paths, then generic tokens; a
    # lower-precedence match overlapping a higher one is not registered.
    pats = [(p, re.compile(p.regex, 0 if p.case_sensitive else re.I)) for p in task.patterns]
    minted = [(m, k, re.compile(_TOK_BEFORE + re.escape(m) + _TOK_AFTER, re.I if _HEX.match(m) else 0)) for m, k in task.minted.items()]
    for p, t in tool_paths:
        r = norm_path(p, ws)
        if r:
            add(r, "path", "path", t=t)
    for x in file_texts:
        add(x.text, "path", "path", t=_w(x))
    # Every path the replayed change log had, those a directory rename moved
    # included (the watcher writes no record per moved child).
    for q, spans in lives.items():
        if spans:
            add(q, "path", "path", t=spans[0][0])
    ci_keys = set()
    raw_forms: dict[str, str] = {}  # a path as written (/work/project/./out/a.txt) -> its identifier
    for x in sorted(texts + file_texts, key=_rank):
        spans: list[tuple[int, int]] = []
        for spec, rx in pats:
            for m in rx.finditer(x.text):
                k = add(m.group(0), spec.kind, "pattern", ci=not spec.case_sensitive)
                if not spec.case_sensitive:
                    ci_keys.add(k)
                spans.append(m.span())
        for text, kind, rx in minted:
            for m in rx.finditer(x.text):
                add(text, kind, "minted", ci=bool(_HEX.match(text)))
                spans.append(m.span())
        if x.source not in ("agent_file", "tool_file", "workspace_initial"):
            for m in _ABS_PATH.finditer(x.text):
                p = m.group(0).rstrip(".,:;")
                r = norm_path(p, ws)
                if r:
                    raw_forms[p] = add(r, "path", "path", t=_w(x))
                    spans.append(m.span())
                else:
                    q = posixpath.normpath(p)  # normalise first: /tmp/../etc is not under /tmp/
                    root = next((root for root in task.external_roots if q.startswith(root)), None)
                    if root and len(q) > len(root):
                        k = add(q, "external_path", "path", t=_w(x))
                        raw_forms.setdefault(p, k)
                        spans.append(m.span())
            for m in _REL_SLASH.finditer(x.text):
                r = norm_path(m.group(0), ws)
                if r and not re.fullmatch(r"[\d./]+", r):
                    raw_forms[m.group(0)] = add(r, "path", "path", t=_w(x))
                    spans.append(m.span())
            if x.user_authored:
                # A bare file name the user wrote is a path the user knows.
                for m in _REL_FILE.finditer(x.text):
                    if m.group(1).lower() in NOT_FILE_EXTENSIONS:
                        continue
                    r = norm_path(m.group(0), ws)
                    if r:
                        add(r, "path", "path", t=_w(x))
                        spans.append(m.span())
        for s, e_, tok in generic_tokens(x.text, task.min_token_length, task.digits_only_tokens):
            if any(s < b and a < e_ for a, b in spans):
                continue
            if any(k in ci_keys or idents[k].ci for k in by_text.get(tok.lower(), []) if not k.startswith("path:")):
                continue
            add(tok, "token", "token", ci=bool(_HEX.match(tok)))

    # Pass 2: surface forms. A path's bare file name is an alias, used for
    # sight and leaks only, never formed when an identifier has that spelling,
    # and resolved against the paths known at the time of the text.
    by_base: dict[str, list[str]] = {}
    for i in idents.values():
        if i.family == "path" and i.kind == "path":
            by_base.setdefault(i.text.rsplit("/", 1)[-1], []).append(i.key)
    taken = {i.key.split(":", 1)[1] for i in idents.values()} | {i.text for i in idents.values()}
    forms: dict[str, list[tuple[str, bool]]] = {}
    ci_forms: dict[str, list[tuple[str, bool]]] = {}
    non_path = {i.key.split(":", 1)[1] for i in idents.values() if i.family != "path"} | {i.text for i in idents.values() if i.family != "path"}
    for i in idents.values():
        if i.family == "path" and i.kind == "path":
            # A bare spelling that is also another identifier counts for that
            # identifier; ./ and absolute spellings are path evidence.
            bare = [i.text] if i.text not in non_path and i.text.lower() not in non_path else []
            for f in (*bare, f"./{i.text}", f"{ws}/{i.text}"):
                forms.setdefault(f, []).append((i.key, False))
            base = i.text.rsplit("/", 1)[-1]
            # Only a non-path identifier of that spelling blocks the alias: a
            # root file of the same name competes with it by lifetime instead.
            if base != i.text and base not in non_path and base.lower() not in non_path:
                forms.setdefault(base, []).append((i.key, True))
        elif i.ci:
            ci_forms.setdefault(i.key.split(":", 1)[1], []).append((i.key, False))
        else:
            forms.setdefault(i.text, []).append((i.key, False))
    for raw, key in raw_forms.items():
        if (key, False) not in forms.get(raw, []):
            forms.setdefault(raw, []).append((key, False))

    def alternations(fs: dict[str, list[tuple[str, bool]]], flags: int) -> list[re.Pattern[str]]:
        """One regex for path forms and one for the rest, so that a path match
        never consumes an identifier inside it; their matches are merged."""
        pth = sorted((f for f in fs if any(idents[k].family == "path" for k, _ in fs[f])), key=len, reverse=True)
        toks = sorted((f for f in fs if any(idents[k].family != "path" for k, _ in fs[f])), key=len, reverse=True)
        out = []
        if pth:
            out.append(re.compile(_PATH_BEFORE + "(?:" + "|".join(map(re.escape, pth)) + ")" + _PATH_AFTER, flags))
        if toks:
            out.append(re.compile(_TOK_BEFORE + "(?:" + "|".join(map(re.escape, toks)) + ")" + _TOK_AFTER, flags))
        return out

    rx_cs = alternations(forms, 0)
    rx_ci = alternations(ci_forms, re.I)
    # Identifiers inside a longer match: tokens and task identifiers only.
    # A path inside a longer path is another file, not the same one.
    inner_rx = {
        i.key: re.compile(r"(?<![A-Za-z0-9])" + re.escape(i.key.split(":", 1)[1] if i.ci else i.text) + r"(?![A-Za-z0-9])", re.I if i.ci else 0)
        for i in idents.values() if i.family != "path"
    }

    def alive(k: str, t: int) -> bool:
        """A path with a change-log lifetime is alive inside one of its
        intervals; one known only from texts, from the time it was named."""
        spans = lives.get(idents[k].text)
        if spans:
            return any(a <= t and (b is None or t < b) for a, b in spans)
        return idents[k].known_from is not None and idents[k].known_from <= t

    def resolve_alias(keys: list[tuple[str, bool]], t: int) -> list[tuple[str, bool]]:
        # A bare name resolves only among the paths alive at the time of the
        # text; an explicit spelling keeps naming its path after a deletion.
        out = [(k, a) for k, a in keys if not a]
        aliases = [k for k, a in keys if a]
        if aliases:
            base = idents[aliases[0]].text.rsplit("/", 1)[-1]
            live = [k for k in by_base.get(base, []) if alive(k, t)]
            if len(live) == 1 and live[0] in aliases:
                out.append((live[0], True))
                # A dead path never shadows a live one: the spelling names the live file.
                out = [(k, a) for k, a in out if a or idents[k].family != "path" or alive(k, t) or not lives.get(idents[k].text)]
        return out

    def occurrences(x: Text) -> tuple[list[tuple[str, bool]], set[str]]:
        text = x.text
        found = []
        for rx in rx_cs:
            found += [(m.start(), m.end(), forms[m.group(0)]) for m in rx.finditer(text)]
        for rx in rx_ci:
            found += [(m.start(), m.end(), ci_forms[m.group(0).lower()]) for m in rx.finditer(text)]
        found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
        keep: list[tuple[str, bool]] = []
        inner: set[str] = set()
        span = None
        for s, e_, keys in found:
            if span is not None and s < span[1] and (s, e_) != span:
                continue  # inside or overlapping a longer match
            # A path counts only from the time it exists; an alias resolves
            # against the paths of that time. Nothing left: the match does not
            # shadow what is inside it.
            keys = [(k, a) for k, a in resolve_alias(keys, _w(x)) if idents[k].family != "path" or idents[k].known_from is None or idents[k].known_from <= _w(x)]
            if not keys:
                continue
            span = (s, e_)
            keep += [k for k in keys if k not in keep]
            frag = text[s:e_]
            inner |= {k for k, rx in inner_rx.items() if rx.search(frag)} - {k for k, _ in keys}
        return keep, inner

    for x in sorted(texts + file_texts, key=_rank):
        if x.source in ("agent_file", "tool_file", "workspace_initial"):
            keys = [("path:" + x.text, False)]
            inner = {k for k, rx in inner_rx.items() if rx.search(x.text)}
        else:
            keys, inner = occurrences(x)
        for k, _ in keys:
            idents[k].occurrences += 1
        for k in [k for k, _ in keys] + sorted(inner):
            if idents[k].first is None:
                idents[k].first = x
            if x.seq is not None:
                idents[k].in_text = True
        if x.user_authored:
            # Known only as far as the user wrote it: never through a bare-name alias.
            for k in [k for k, alias in keys if not alias] + sorted(inner):
                if idents[k].known is None:
                    idents[k].known = x
        if x.user_channel:
            for k in dict.fromkeys(k for k, _ in keys):
                i = idents[k]
                if i.known is None and not any(l.seq == x.seq for l in i.leaks):
                    i.leaks.append(x)
    return idents


def _records(changes: ChangeLog | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return changes.records if isinstance(changes, ChangeLog) else list(changes or [])


def build(traj: Trajectory, changes: ChangeLog | list[dict[str, Any]] | None = None, task: TaskIdentifiers | None = None) -> dict[str, Any]:
    """The audit registry, from the full trajectory: provenance included. Not a scorer input."""
    task = task or TaskIdentifiers()
    ws = traj.header["workspace"].rstrip("/")
    events = in_episode(traj)
    writes = [(e["input"].get("path"), e["us"], e["us_end"] if e["us_end"] is not None else e["us"]) for e in events if e["type"] == "tool_call" and e["tool"] in ("file_write", "file_edit")]
    tool_paths = [(e["input"]["path"], e["us"]) for e in events if e["type"] == "tool_call" and isinstance(e["input"].get("path"), str)]
    wall = changes.wall_us if isinstance(changes, ChangeLog) and len(changes.wall_us) == len(changes.records) else None
    if wall is None and _records(changes):
        # A change log read back from its file is on the episode clock; the
        # trajectory is on the wall clock. One clock: map the log back.
        from .clock import Clock

        clk = Clock(traj.header.get("excluded", []))
        wall = [clk.wall_us(r["t"]) if isinstance(r.get("t"), int) else 0 for r in _records(changes)]
    idents = _compute(trajectory_texts(traj), change_texts(_records(changes), ws, writes, task.file_slack_ms, wall), tool_paths, task, ws, lifetimes(_records(changes), ws, wall))

    def where(x: Text) -> dict[str, Any]:
        return {"t": x.t, "seq": x.seq, "change_seq": x.change_seq, "source": x.source, "agent": x.agent, "event": x.event}

    rows = []
    for i in sorted((i for i in idents.values() if i.first is not None), key=lambda i: (_rank(i.first), i.key)):
        rows.append({"text": i.text, "kind": i.kind, "first_seen": where(i.first),
                     "known_to_user": None if i.known is None else {"t": i.known.t, "seq": i.known.seq},
                     "sent_while_private": [{"t": x.t, "seq": x.seq, "event": x.event} for x in i.leaks], "occurrences": i.occurrences})
    leaked = [r for r in rows if r["sent_while_private"]]
    return {
        "format": REGISTRY_FORMAT, "version": 1,
        "settings": _settings(task, ws, len(_records(changes))),
        "identifiers": rows,
        "aux": {"identifiers": len(rows), "private_identifiers_sent_to_user": len(leaked), "private_mentions_sent_to_user": sum(len(r["sent_while_private"]) for r in leaked)},
    }


def scorer_identifiers(view: Trajectory, changes: ChangeLog | list[dict[str, Any]] | None = None, task: TaskIdentifiers | None = None, workspace: str = "/work/project") -> dict[str, Any]:
    """`identifiers.json`: from the scorer view and the change log only. Every
    identifier that occurs in what the user saw or wrote; when it became known
    and each disclosure while private, by view seq and time."""
    task = task or TaskIdentifiers()
    ws = workspace.rstrip("/")
    # One clock: wall microseconds when both the view and the change log
    # carry them (both built in memory), else the files' episode milliseconds.
    wall = changes.wall_us if isinstance(changes, ChangeLog) and len(changes.wall_us) == len(changes.records) else None
    if not _records(changes):
        wall = []
    both = wall is not None and (bool(view.wall) or not view.events)
    texts = view_texts(view, wall=both)
    wall = wall if both else None
    idents = _compute(texts, change_texts(_records(changes), ws, [], task.file_slack_ms, wall), [], task, ws, lifetimes(_records(changes), ws, wall))
    rows = []
    for i in sorted((i for i in idents.values() if i.in_text), key=lambda i: (i.text, i.kind)):
        rows.append({"text": i.text, "kind": i.kind, "known": None if i.known is None else {"seq": i.known.seq, "t": i.known.t},
                     "disclosed_while_private": [{"seq": x.seq, "t": x.t} for x in i.leaks]})
    leaked = [r for r in rows if r["disclosed_while_private"]]
    return {
        "format": IDENTIFIERS_FORMAT, "version": 1,
        "identifiers": rows,
        "aux": {"private_identifiers_sent_to_user": len(leaked), "private_mentions_sent_to_user": sum(len(r["disclosed_while_private"]) for r in leaked)},
    }


def _settings(task: TaskIdentifiers, ws: str, n: int) -> dict[str, Any]:
    return {"workspace": ws, "min_token_length": task.min_token_length, "digits_only_tokens": task.digits_only_tokens, "external_roots": list(task.external_roots),
            "patterns": [{"kind": p.kind, "regex": p.regex, "case_sensitive": p.case_sensitive} for p in task.patterns], "minted": len(task.minted), "change_records": n}


IDENTIFIER_FIELDS = ("text", "kind", "known", "disclosed_while_private")


def validate_identifiers(doc: Any, view: Trajectory | None = None) -> list[str]:
    errors = []
    if not isinstance(doc, dict) or doc.get("format") != IDENTIFIERS_FORMAT or doc.get("version") != 1 or set(doc) != {"format", "version", "identifiers", "aux"}:
        return ["identifiers.json: wrong format, version or keys"]
    if not isinstance(doc["identifiers"], list) or not isinstance(doc["aux"], dict):
        return ["identifiers.json: identifiers is a list, aux an object"]

    def ref_ok(ref: Any, want: str, where: str, text: str = "") -> bool:
        if not isinstance(ref, dict) or set(ref) != {"seq", "t"} or not all(isinstance(ref[k], int) and not isinstance(ref[k], bool) for k in ("seq", "t")):
            errors.append(f"{where}: a reference is {{seq, t}} with integers")
            return False
        if view is not None:
            ev = view.events[ref["seq"]] if 0 <= ref["seq"] < len(view.events) else None
            if ev is None or ev.get("t") != ref["t"] or ev.get("type") != want or (want == "user_message" and ev.get("secret")):
                errors.append(f"{where}: {ref} is not a {want} of the scorer view")
                return False
            # The cited message holds the identifier (or, for a path, its file name).
            body = str(ev.get("text") or "").lower()
            if text and text.lower() not in body and text.rsplit("/", 1)[-1].lower() not in body:
                errors.append(f"{where}: {text!r:.40} does not occur in the cited message")
                return False
        return True

    leaked = []
    # One row per identifier: by declared kind and text, for every kind. A
    # job id and a root file of the same name are two identifiers, and so
    # are two declared kinds with one spelling. Rows are sorted by text, then kind.
    keys = [(r.get("text"), r.get("kind")) for r in doc["identifiers"] if isinstance(r, dict)]
    if any(not (isinstance(t, str) and isinstance(k, str)) for t, k in keys):
        errors.append("identifiers.json: every row has a text and a kind")
    elif keys != sorted(keys) or len(set(keys)) != len(keys):
        errors.append("identifiers.json: rows are sorted by text and kind, one row per identifier (kind and text)")
    for n, r in enumerate(doc["identifiers"]):
        where = f"identifier {n}"
        if not isinstance(r, dict) or set(r) != set(IDENTIFIER_FIELDS) or not isinstance(r["text"], str) or not isinstance(r["kind"], str) \
                or not isinstance(r["disclosed_while_private"], list):
            errors.append(f"{where}: fields differ from the schema")
            continue
        known_ok = r["known"] is None or ref_ok(r["known"], "user_message", f"{where} known", r["text"])
        ds = [d for d in r["disclosed_while_private"] if ref_ok(d, "to_user", f"{where} disclosure", r["text"])]
        if len(ds) == len(r["disclosed_while_private"]):
            seqs = [d["seq"] for d in ds]
            if seqs != sorted(set(seqs)):
                errors.append(f"{where}: disclosures are distinct and in view order")
        if known_ok and r["known"] is not None and any(d["seq"] >= r["known"]["seq"] for d in ds):
            errors.append(f"{where}: disclosed after it became known")
        if r["disclosed_while_private"]:
            leaked.append(r)
    aux = doc["aux"]
    if aux != {"private_identifiers_sent_to_user": len(leaked), "private_mentions_sent_to_user": sum(len(r["disclosed_while_private"]) for r in leaked)}:
        errors.append("aux counts do not match the identifiers")
    return errors
