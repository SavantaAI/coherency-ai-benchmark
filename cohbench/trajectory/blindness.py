"""Blindness check: harness tells in a normalised trajectory.

Three kinds of hit:

- structural: a tell in any field that is not content (event types, classes,
  tool names, statuses, keys, the header). The format's closed vocabularies
  should make these impossible; one is a bug in an adapter. Fails the check.
- decoration: a harness or driver decoration (a truncation notice, a spill
  file path, an injected reminder) left inside a tool's output text. The
  profile should have removed it. Fails the check.
- content: a tell inside text the agent, the user or the task produced (the
  agent naming itself, a command that reads `~/.pi`, a T3 thread id the agent
  copied into a tool argument). Counted, never removed, and the check passes.
  The counts go to the run manifest, not the trajectory.

See docs/trajectory-format.md, "Blindness check", for why content tells are
counted and kept.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import format as F
from .io import Trajectory

# Tells that may not appear in any structural field (case-insensitive).
STRUCTURAL_TELLS: dict[str, str] = {
    "pi": r"\bpi\b",
    "claude": r"claude",
    "codex": r"codex",
    "anthropic": r"anthropic",
    "openai": r"openai",
    "t3": r"\bt3\b|t3[-_ ]?code",
    "earendil": r"earendil",
    # A model id does not stop at its number (`gpt-4o`, `o3-mini`).
    "model family": r"\b(?:(?:haiku|sonnet|opus|fable|gemini)\b|gpt-?\d[\w.-]*|o[1-9](?:-(?:mini|pro|preview))?\b)",
    "bash tool name": r"\bbash\b",
    "tool_search": r"tool_?search",
    "mcp prefix": r"mcp__",
    "T3 id": r"provider-thread:|turn-item:|native-item:|provider-turn:|run-attempt:|thread:project:|run:thread:|node:|provider:",
    "tool call id": r"\btoolu_",
    "uuid": r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    "home path": r"/home/|/\.pi\b|/\.claude|/\.t3\b|/tmp/pi-bash",
}

# Decorations that may not survive where format.DECORATION_PATHS says.
DECORATIONS: dict[str, str] = {
    "pi truncation notice": r"\[Showing lines \d+-\d+ of \d+[^\]\n]*\]\s*\Z|Full output: /tmp/pi-bash-[0-9A-Za-z]+\.log\]",
    "claude persisted output": r"<persisted-output>|Output too large \([^)\n]*\)\. Full output saved to:",
    "claude truncation notice": r"\.\.\. \[\d+ lines truncated\] \.\.\.\s*\Z",
    "claude empty output": r"<system>Tool ran without output or errors</system>",
    "system reminder": r"<system-reminder>",
    "T3 transport suffix": r"… output truncated for transport\s*\Z",
    "T3 sender note": r"\A\[T3 Code:",
    "T3 role prefix": r"\AAct as the [^\n.]{1,60} sub-agent for this task\.",
}

# Tells counted in content. Specific enough that ordinary prose does not match.
CONTENT_TELLS: dict[str, str] = {
    "product: Claude Code": r"\bClaude Code\b|\bclaude-code\b|@anthropic-ai/claude-code",
    # Case-sensitive "Pi" as a word: the agent naming its harness ("The Pi provider is available").
    "product: Pi": r"\bPi\b|pi-coding-agent|@earendil-works",
    "product: Codex": r"\bCodex\b",
    "driver: T3 Code": r"\bT3 Code\b|\bt3code\b|\[T3 Code:",
    "model id": r"\bclaude-(?:opus|sonnet|haiku|fable)-\d|\banthropic/claude",
    "T3 tool prefix": r"mcp__t3[-_]code__",
    "harness tool name": r"\b(?:ToolSearch|AskUserQuestion|TodoWrite|MultiEdit|NotebookEdit|WebFetch|ExitPlanMode|EnterPlanMode|BashOutput|KillShell|tool_search)\b",
    "T3 id": r"provider-thread:|turn-item:|native-item:|provider-turn:|thread:project:|thread:delegated-task:|run:thread:",
    "tool call id": r"\btoolu_[A-Za-z0-9]{12,}",
    "Pi session file": r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d{3}Z_[0-9a-f-]{36}\.jsonl",
    "harness home path": r"/home/node/\.(?:pi|claude)\b|~/\.(?:pi|claude)\b|\.pi/agent|\.claude\.json|/tool-results/",
    "driver home path": r"/home/node/\.t3\b|~/\.t3\b",
    "Pi spill file": r"/tmp/pi-bash-",
    "instruction file": r"\bCLAUDE\.md\b|\bAGENTS\.md\b",
    "harness env": r"\bCLAUDE_CODE_\w+|\bPI_OFFLINE\b|\bANTHROPIC_\w+",
}

_S = {k: re.compile(v, re.I) for k, v in STRUCTURAL_TELLS.items()}
_D = {k: re.compile(v) for k, v in DECORATIONS.items()}
_C = {k: re.compile(v) for k, v in CONTENT_TELLS.items()}


@dataclass
class Hit:
    seq: int | None
    field: str
    tell: str
    sample: str


@dataclass
class BlindnessReport:
    structural: list[Hit] = field(default_factory=list)
    decorations: list[Hit] = field(default_factory=list)
    content: Counter = field(default_factory=Counter)  # tell -> count
    content_fields: Counter = field(default_factory=Counter)  # "event.field" -> count
    content_hits: list[Hit] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.structural and not self.decorations

    def as_dict(self) -> dict[str, Any]:
        h = lambda xs: [x.__dict__ for x in xs[:50]]  # noqa: E731
        return {
            "ok": self.ok,
            "structural": h(self.structural),
            "decorations": h(self.decorations),
            "content_total": sum(self.content.values()),
            "content_by_tell": dict(sorted(self.content.items())),
            "content_by_field": dict(sorted(self.content_fields.items())),
            "content_samples": h(self.content_hits),
        }


def _leaves(obj: Any, path: str = "") -> Iterator[tuple[str, Any, bool]]:
    """(dotted path, value, is_key) for every string leaf and every key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            yield p, k, True
            yield from _leaves(v, p)
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield from _leaves(v, f"{path}.{n}")
    elif isinstance(obj, str):
        yield path, obj, False


def _is(paths: tuple[str, ...], path: str) -> bool:
    """Dotted path patterns: `*` stands for one segment; a trailing `*` for one or more."""
    segs = path.split(".")
    for p in paths:
        ps = p.split(".")
        if ps[-1] == "*" and len(segs) >= len(ps) and all(a in ("*", b) for a, b in zip(ps[:-1], segs)):
            return True
        if len(ps) == len(segs) and all(a in ("*", b) for a, b in zip(ps, segs)):
            return True
    return False


def _sample(text: str, m: re.Match[str]) -> str:
    a, b = max(m.start() - 30, 0), min(m.end() + 30, len(text))
    return text[a:b]


def _scan(traj: Trajectory, content_paths: dict[str, tuple[str, ...]], decoration_paths: dict[str, tuple[str, ...]], skip_header: tuple[str, ...]) -> BlindnessReport:
    rep = BlindnessReport()
    for path, value, is_key in _leaves(traj.header):
        for name, rx in _S.items():
            if isinstance(value, str) and path not in skip_header and rx.search(value):
                rep.structural.append(Hit(None, f"header.{path}", name, value[:80]))
    for ev in traj.events:
        etype = ev.get("type", "") if isinstance(ev.get("type"), str) else ""
        content = content_paths.get(etype, ())
        decorated = decoration_paths.get(etype, ())
        counted: set[str] = set()  # a text repeated in one event (text and user_text) counts once
        for path, value, is_key in _leaves(ev):
            if not is_key and _is(content, path):
                if _is(decorated, path):
                    for name, rx in _D.items():
                        m = rx.search(value)
                        if m:
                            rep.decorations.append(Hit(ev.get("seq"), path, name, _sample(value, m)))
                if value in counted:
                    continue
                counted.add(value)
                for name, rx in _C.items():
                    for m in rx.finditer(value):
                        rep.content[name] += 1
                        rep.content_fields[f"{etype}.{path.split('.')[0]}"] += 1
                        if len(rep.content_hits) < 50:
                            rep.content_hits.append(Hit(ev.get("seq"), path, name, _sample(value, m)))
                continue
            for name, rx in _S.items():
                m = rx.search(value)
                if m:
                    rep.structural.append(Hit(ev.get("seq"), path, name, value[:80]))
    return rep


def scan(traj: Trajectory) -> BlindnessReport:
    """The full trajectory."""
    return _scan(traj, F.CONTENT_PATHS, F.DECORATION_PATHS, ("workspace",))


def scan_view(view: Trajectory) -> BlindnessReport:
    """The scorer view, the strict gate. Its structural fields are closed
    vocabularies and numbers, so any structural hit is a bug."""
    # Agent prose (to_user) is content, counted and never failed; only user
    # text can carry a driver decoration (a sender note).
    return _scan(view, F.VIEW_CONTENT_PATHS, {"user_message": ("text",)}, ())


def scan_doc(doc: Any, content: tuple[str, ...], decorated: tuple[str, ...] = ()) -> BlindnessReport:
    """Any bundle document (a JSON object, or a list of records): strings at
    `content` paths are counted, decorations at `decorated` paths fail, and a
    tell anywhere else fails."""
    rep = BlindnessReport()
    counted: set[str] = set()
    for path, value, is_key in _leaves(doc):
        # A key at a content path is content too (M4 of round 11): the ref
        # names that key a git record's `refs` are names the agent chose.
        if _is(content, path):
            if _is(decorated, path) and not is_key:
                for name, rx in _D.items():
                    m = rx.search(value)
                    if m:
                        rep.decorations.append(Hit(None, path, name, _sample(value, m)))
            if value in counted:
                continue
            counted.add(value)
            for name, rx in _C.items():
                for m in rx.finditer(value):
                    rep.content[name] += 1
                    rep.content_fields[path.split(".")[0]] += 1
                    if len(rep.content_hits) < 50:
                        rep.content_hits.append(Hit(None, path, name, _sample(value, m)))
            continue
        for name, rx in _S.items():
            m = rx.search(value)
            if m:
                rep.structural.append(Hit(None, path, name, value[:80]))
    return rep


def scan_stream(chunks: Iterator[str], overlap: int = 512) -> Counter:
    """`scan_text` over a text given in pieces, counting each match once,
    matches across a piece boundary included (a tell is shorter than
    `overlap` characters)."""
    out: Counter = Counter()
    carry, start = "", 0
    pending = list(chunks)
    for n, piece in enumerate(pending):
        w = carry + piece
        last = n == len(pending) - 1
        bound = len(w) if last else max(start, len(w) - overlap)
        for table in (_S, _D, _C):
            for name, rx in table.items():
                for m in rx.finditer(w):
                    if start <= m.start() < bound:
                        out[name] += 1
        keep = max(0, bound - overlap)
        carry, start = w[keep:], bound - keep
    return out


def scan_text(text: str) -> Counter:
    """Every tell in a text (structural, decoration and content patterns), by
    name. For file contents in the bundle's blob store: agent-written files
    are evidence, so a hit is recorded in the audit record, never a failure."""
    out: Counter = Counter()
    for table in (_S, _D, _C):
        for name, rx in table.items():
            n = sum(1 for _ in rx.finditer(text))
            if n:
                out[name] += n
    return out
