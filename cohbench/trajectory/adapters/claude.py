"""Claude Code (`@anthropic-ai/claude-code` 2.1.330) driven through T3 Code.

UNVERIFIED. No Claude Code turn succeeded in the driver proof (every model
call was refused, D-002), so no T3 record of a Claude Code tool call exists.
What this profile rests on:

- the tool names in the stored request bodies and the session's deferred-tool
  list of run 2026-11-21T210951-74da66 (`claude/startup/injected-tool-names.json`);
- the session file shape of the refused turns (user, assistant and attachment
  entries; the refusal written as an assistant entry with model `<synthetic>`
  and `isApiErrorMessage`);
- the provider instance id `claudeAgent` in T3's records of those turns;
- Claude Code's documented tool argument names (`file_path`, `old_string`,
  `new_string`, `pattern`, `description`, `prompt`) and output decorations
  (`cat -n` line numbers on reads, `<system-reminder>` blocks, the large
  output notices), which were not observed through T3.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Cleaned, NativeSession, Profile, ToolArgs, as_int, as_str, loads_jsonl, pick, prompts_from, text_parts

# Large shell output, two shapes (unverified through T3):
# a middle notice "... [N lines truncated] ..." at the end of the kept head,
_LINES_TRUNC = re.compile(r"\n\n\.\.\. \[(\d+) lines truncated\] \.\.\.\Z")
# and output persisted to a file with a preview.
_PERSISTED = re.compile(r"\A<persisted-output>\nOutput too large \([^)]*\)\. Full output saved to: [^\n]+\n\nPreview \(first [^)]*\):\n(?P<preview>.*?)\n?(?:\.\.\.\n)?</persisted-output>\Z", re.S)
_EMPTY = "<system>Tool ran without output or errors</system>"
_REMINDER = re.compile(r"\n*<system-reminder>.*?</system-reminder>\n*", re.S)
_CAT_N = re.compile(r"^ *\d+(?:\t|→)", re.M)
_ANSWERS = re.compile(r'"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"')

# Every tool the stored request bodies list, other than T3's mcp__t3-code__*.
_OTHER = (
    "ListAgents", "ReportFindings", "ScheduleWakeup", "Skill", "Workflow", "DeferredToolPlaceholder", "CronCreate",
    "CronDelete", "CronList", "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Monitor",
    "SendMessage", "TaskStop", "TodoWrite", "BashOutput", "KillShell",
)


class ClaudeCodeProfile(Profile):
    name = "claude-code"
    verified = False
    shell_titles = ("Bash",)
    file_change_titles = {"Write": "file_write", "Edit": "file_edit", "MultiEdit": "file_edit", "NotebookEdit": "file_edit"}
    harness_tools = {
        "Bash": "shell",
        "Read": "file_read",
        "Write": "file_write",
        "Edit": "file_edit",
        "MultiEdit": "file_edit",
        "NotebookEdit": "file_edit",
        "Glob": "search",
        "Grep": "search",
        "WebFetch": "web_fetch",
        "WebSearch": "web_search",
        "Agent": "subagent",
        "Task": "subagent",
        "AskUserQuestion": "ask_user",
        "ToolSearch": "tool_discovery",
        **{n: "harness_other" for n in _OTHER},
    }

    def tool_args(self, tool: str, args: Any) -> ToolArgs:
        a = args if isinstance(args, dict) else {}
        if tool == "shell":
            n, w = pick(a, {"command": ("command",)})
            return ToolArgs(tool, {"command": as_str(n["command"])}, w)
        if tool == "file_read":
            n, w = pick(a, {"path": ("file_path", "path"), "offset": ("offset",), "limit": ("limit",)})
            return ToolArgs(tool, {"path": as_str(n["path"]), "offset": as_int(n["offset"]), "limit": as_int(n["limit"])}, w)
        if tool == "file_write":
            n, w = pick(a, {"path": ("file_path", "path"), "content": ("content",)})
            return ToolArgs(tool, {"path": as_str(n["path"]), "content": as_str(n["content"]), "content_truncated": False}, w)
        if tool == "file_edit":
            n, w = pick(a, {"path": ("file_path", "notebook_path", "path"), "old_text": ("old_string",), "new_text": ("new_string", "new_source")})
            return ToolArgs(tool, {"path": as_str(n["path"]), "old_text": as_str(n["old_text"]), "new_text": as_str(n["new_text"]), "diff": None, "content_truncated": False}, w)
        if tool == "search":
            n, w = pick(a, {"pattern": ("pattern",), "path": ("path",)})
            return ToolArgs(tool, {"pattern": as_str(n["pattern"]), "path": as_str(n["path"])}, w)
        if tool == "web_fetch":
            n, w = pick(a, {"url": ("url",), "prompt": ("prompt",)})
            return ToolArgs(tool, {"url": as_str(n["url"]), "prompt": as_str(n["prompt"])}, w)
        if tool == "web_search":
            n, w = pick(a, {"query": ("query",)})
            return ToolArgs(tool, {"query": as_str(n["query"])}, w)
        if tool == "subagent":
            n, w = pick(a, {"title": ("description",), "prompt": ("prompt",)})
            return ToolArgs(tool, {"title": as_str(n["title"]), "prompt": as_str(n["prompt"])}, w)
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
        removed: list[str] = []
        if _REMINDER.search(text):
            removed += [m.group(0) for m in _REMINDER.finditer(text)]
            text = _REMINDER.sub("", text)
        if tool == "shell":
            if text == _EMPTY:
                return Cleaned("", None, removed + [text])
            m = _PERSISTED.match(text)
            if m:
                return Cleaned(m.group("preview"), {"by": "harness", "lines_omitted": None, "edge": "tail", "last_line_whole": False}, removed + ["<persisted-output> wrapper"])
            m = _LINES_TRUNC.search(text)
            if m:
                return Cleaned(text[: m.start()], {"by": "harness", "lines_omitted": int(m.group(1)), "edge": "tail", "last_line_whole": True}, removed + [m.group(0)])
        if tool == "file_read":
            lines = text.split("\n")
            body = [ln for ln in lines if ln]
            if body and all(_CAT_N.match(ln) for ln in body):
                removed.append("cat -n line numbers")
                text = "\n".join(_CAT_N.sub("", ln, count=1) for ln in lines)
        if tool == "ask_user":
            pairs = _ANSWERS.findall(text)
            if pairs:
                removed.append(text)
                text = "\n".join(a for _, a in pairs)
        return Cleaned(text, None, removed)

    def read_session(self, path: str | Path) -> NativeSession:
        entries = loads_jsonl(path)
        s = NativeSession(path=str(path), session_id=Path(path).stem)
        order = 0
        for e in entries:
            msg = e.get("message") if isinstance(e.get("message"), dict) else None
            if not msg:
                continue
            if e.get("type") == "assistant":
                if e.get("isApiErrorMessage") or msg.get("model") == "<synthetic>":
                    s.synthetic_ids.add(e.get("uuid"))
                    continue
                for part in msg.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        s.assistant_texts.append(part["text"])
                    elif part.get("type") == "tool_use":
                        s.calls[part.get("id")] = {"name": part.get("name"), "args": part.get("input"), "order": order}
                        order += 1
            elif e.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    if not e.get("isMeta"):
                        s.user_texts.append(content)
                        s.user_times.append(e.get("timestamp"))
                    continue
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result":
                        s.results[part.get("tool_use_id")] = {"text": text_parts(part.get("content")), "is_error": bool(part.get("is_error"))}
                    elif part.get("type") == "text" and not e.get("isMeta"):
                        s.user_texts.append(part.get("text", ""))
                        s.user_times.append(e.get("timestamp"))
        return s

    def session_for(self, sessions: list[NativeSession], native_thread_id: str) -> NativeSession | None:
        # T3's attempts[].nativeThreadId is the Claude session uuid.
        for s in sessions:
            if s.session_id == native_thread_id or Path(s.path).stem == native_thread_id:
                return s
        return None
