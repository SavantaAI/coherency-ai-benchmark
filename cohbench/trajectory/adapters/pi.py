"""Pi (`@earendil-works/pi-coding-agent` 1.6.0) driven through T3 Code.

Written against run 2026-11-21T210951-74da66. What that run exercised is
marked "observed"; the rest follows Pi's tool set as listed in its session
(`read`, `bash`, `edit`, `write`, `tool_search`) and is marked unverified in
cohbench/trajectory/README.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Cleaned, NativeSession, Profile, ToolArgs, as_int, as_str, loads_jsonl, pick, prompts_from, text_parts

# Observed: "\n\n[Showing lines 1957-3001 of 3001 (50.0KB limit). Full output: /tmp/pi-bash-09af1e3a1bcccd9d.log]"
_BASH_TRUNC = re.compile(r"\n\n\[Showing lines (\d+)-(\d+) of (\d+)(?: \([^)\]]*\))?\. Full output: /tmp/pi-bash-[0-9A-Za-z]+\.log\]\Z")
# Unverified: the same notice without the spill file, and the read tool's continuation notice.
_GENERIC_TRUNC = re.compile(r"\n\n\[Showing lines (\d+)-(\d+) of (\d+)(?: \([^)\]]*\))?\.(?: [^\]\n]*)?\]\Z")
_NO_OUTPUT = "(no output)"  # observed in Pi's session for a command that printed nothing


def _edge(first: int, last: int, total: int) -> str:
    """Which edges "lines first-last of total" cut away."""
    head, tail = first > 1, last < total
    return "both" if head and tail else "head" if head else "tail" if tail else "none"


class PiProfile(Profile):
    name = "pi"
    verified = True
    shell_titles = ("bash",)
    file_change_titles = {"write": "file_write", "edit": "file_edit"}
    harness_tools = {
        "bash": "shell",
        "read": "file_read",
        "write": "file_write",
        "edit": "file_edit",
        "grep": "search",
        "find": "search",
        "ls": "search",
        "tool_search": "tool_discovery",
        # The driver proof's diagnostic dialog extension (not part of the reference harness).
        "ask_user": "ask_user",
    }

    def tool_args(self, tool: str, args: Any) -> ToolArgs:
        a = args if isinstance(args, dict) else {}
        if tool == "shell":
            n, w = pick(a, {"command": ("command",)})
            return ToolArgs(tool, {"command": as_str(n["command"])}, w)
        if tool == "file_read":
            n, w = pick(a, {"path": ("path",), "offset": ("offset",), "limit": ("limit",)})
            return ToolArgs(tool, {"path": as_str(n["path"]), "offset": as_int(n["offset"]), "limit": as_int(n["limit"])}, w)
        if tool == "file_write":
            n, w = pick(a, {"path": ("path",), "content": ("content",)})
            return ToolArgs(tool, {"path": as_str(n["path"]), "content": as_str(n["content"]), "content_truncated": False}, w)
        if tool == "file_edit":
            n, w = pick(a, {"path": ("path",), "old_text": ("oldText",), "new_text": ("newText",)})
            return ToolArgs(tool, {"path": as_str(n["path"]), "old_text": as_str(n["old_text"]), "new_text": as_str(n["new_text"]), "diff": None, "content_truncated": False}, w)
        if tool == "search":
            n, w = pick(a, {"pattern": ("pattern",), "path": ("path",)})
            return ToolArgs(tool, {"pattern": as_str(n["pattern"]), "path": as_str(n["path"])}, w)
        if tool == "tool_discovery":
            n, w = pick(a, {"query": ("query",)})
            return ToolArgs(tool, {"query": as_str(n["query"])}, w)
        if tool == "ask_user":
            qs, w = prompts_from(a)
            return ToolArgs(tool, {"prompts": qs}, w)
        return ToolArgs("harness_other", {}, dict(a))

    def clean_output(self, tool: str, text: str | None) -> Cleaned:
        if text is None:
            return Cleaned(None)
        if tool == "shell":
            if text == _NO_OUTPUT:
                return Cleaned("", None, [text])
            m = _BASH_TRUNC.search(text)
            if m:
                a, b, total = (int(x) for x in m.groups())
                return Cleaned(text[: m.start()], {"by": "harness", "lines_omitted": max(total - (b - a + 1), 0), "edge": _edge(a, b, total), "last_line_whole": True}, [m.group(0)])
        if tool in ("shell", "file_read", "search"):
            m = _GENERIC_TRUNC.search(text)
            if m:
                a, b, total = (int(x) for x in m.groups())
                return Cleaned(text[: m.start()], {"by": "harness", "lines_omitted": max(total - (b - a + 1), 0), "edge": _edge(a, b, total), "last_line_whole": True}, [m.group(0)])
        return Cleaned(text)

    def read_session(self, path: str | Path) -> NativeSession:
        entries = loads_jsonl(path)
        s = NativeSession(path=str(path), session_id=next((e.get("id") for e in entries if e.get("type") == "session"), None))
        order = 0
        for e in entries:
            if e.get("type") != "message":
                continue
            msg = e.get("message") or {}
            role = msg.get("role")
            if role == "user":
                s.user_texts.append(text_parts(msg.get("content")))
                s.user_times.append(e.get("timestamp"))
            elif role == "assistant":
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        s.assistant_texts.append(part["text"])
                    elif part.get("type") == "toolCall":
                        s.calls[part.get("id")] = {"name": part.get("name"), "args": part.get("arguments"), "order": order}
                        order += 1
            elif role == "toolResult":
                s.results[msg.get("toolCallId")] = {"text": text_parts(msg.get("content")), "is_error": bool(msg.get("isError"))}
        return s

    def session_for(self, sessions: list[NativeSession], native_thread_id: str) -> NativeSession | None:
        # T3's attempts[].nativeThreadId is the session file's path inside the container.
        base = Path(native_thread_id).name
        for s in sessions:
            if Path(s.path).name == base:
                return s
        return None
