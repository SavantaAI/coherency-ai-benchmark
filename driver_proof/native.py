"""Readers for the harnesses' own session files (Pi session jsonl, Claude Code jsonl).

Text fields are kept exactly as decoded from JSON so checks can compare them
byte for byte with what the script sent and with what T3 exported.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SessionParseError(ValueError):
    pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SessionParseError(f"{path.name}:{n}: {exc}") from None
    return entries


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def summarize(harness: str, files: list[Path]) -> dict[str, Any]:
    jsonl = [p for p in files if p.suffix == ".jsonl"]
    if harness == "pi":
        return {"sessions": [pi_session(p) for p in jsonl]}
    return {"sessions": [claude_session(p) for p in jsonl]}


def pi_session(path: Path) -> dict[str, Any]:
    entries = load_jsonl(path)
    out: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "session_id": next((e.get("id") for e in entries if e.get("type") == "session"), None),
        "models": [e.get("modelId") for e in entries if e.get("type") == "model_change"],
        "thinking_levels": [e.get("thinkingLevel") for e in entries if e.get("type") == "thinking_level_change"],
        "user_texts": [],
        "assistant_texts": [],
        "assistant_text_parts": [],
        "tool_calls": [],
        "tool_results": [],
        "assistant_usage": [],
        "system": None,
        "raw_text": path.read_text(),
    }
    for e in entries:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role == "user":
            out["user_texts"].append(_text_parts(msg.get("content")))
        elif role == "assistant":
            content = msg.get("content") or []
            text = _text_parts(content)
            if text:
                out["assistant_texts"].append(text)
            out["assistant_text_parts"] += [p.get("text", "") for p in (content if isinstance(content, list) else []) if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    out["tool_calls"].append({"id": part.get("id"), "name": part.get("name"), "arguments": part.get("arguments")})
            if "usage" in msg:
                u = msg["usage"]
                out["assistant_usage"].append(
                    {"timestamp": e.get("timestamp"), "model": msg.get("model"), **{k: u.get(k) for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")}, "cost_total": (u.get("cost") or {}).get("total")}
                )
        elif role == "toolResult":
            out["tool_results"].append(
                {"timestamp": e.get("timestamp"), "toolCallId": msg.get("toolCallId"), "toolName": msg.get("toolName"), "isError": msg.get("isError"), "text": _text_parts(msg.get("content"))}
            )
        elif role == "system":
            out["system"] = msg
    return out


def claude_session(path: Path) -> dict[str, Any]:
    entries = load_jsonl(path)
    requests = [e for e in entries if e.get("type") == "api-request"]
    out: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "session_id": path.stem,
        "is_subagent": "subagents" in path.parts,
        "models": [r.get("params", {}).get("model") for r in requests],
        "efforts": [r.get("params", {}).get("output_config", {}).get("effort") for r in requests],
        "shape": next((e.get("shape") for e in entries if e.get("type") == "api-request-shape"), None),
        "deferred_tools": sorted(
            {
                name
                for e in entries
                if isinstance(e.get("attachment"), dict) and e["attachment"].get("type") == "deferred_tools_delta"
                for name in e["attachment"].get("addedNames", [])
            }
        ),
        "user_texts": [],
        "assistant_texts": [],
        "assistant_text_parts": [],
        "tool_calls": [],
        "tool_results": [],
        "assistant_usage": [],
        "api_errors": [],
        "raw_text": path.read_text(),
    }
    for e in entries:
        msg = e.get("message") if isinstance(e.get("message"), dict) else None
        if not msg:
            continue
        if e.get("type") == "assistant":
            content = msg.get("content") or []
            if e.get("isApiErrorMessage"):
                out["api_errors"].append({"timestamp": e.get("timestamp"), "error": e.get("error"), "status": e.get("apiErrorStatus"), "text": _text_parts(content)})
                continue
            text = _text_parts(content)
            if text:
                out["assistant_texts"].append(text)
            out["assistant_text_parts"] += [p.get("text", "") for p in (content if isinstance(content, list) else []) if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    out["tool_calls"].append({"id": part.get("id"), "name": part.get("name"), "arguments": part.get("input")})
            u = msg.get("usage")
            if u and msg.get("model") != "<synthetic>":
                out["assistant_usage"].append({"timestamp": e.get("timestamp"), "model": msg.get("model"), **{k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}})
        elif e.get("type") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                if not e.get("isMeta"):
                    out["user_texts"].append(content)
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
                    c = part.get("content")
                    out["tool_results"].append({"timestamp": e.get("timestamp"), "toolCallId": part.get("tool_use_id"), "isError": part.get("is_error"), "text": _text_parts(c)})
                elif part.get("type") == "text" and not e.get("isMeta"):
                    out["user_texts"].append(part.get("text", ""))
    return out


def session_for_native_id(sessions: list[dict[str, Any]], native_id: str) -> dict[str, Any] | None:
    """T3's attempts[].nativeThreadId is a Pi session path or a Claude session uuid."""
    for s in sessions:
        if s["path"].endswith("/" + Path(native_id).name) or s["session_id"] == native_id or Path(s["path"]).stem == native_id:
            return s
    return None


def bound_message_checks(sessions: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each expected message is {kind, text, message_id, run_id, native_ids, sent_at}.

    A message passes when the harness session its run used holds a user
    message byte-identical to it, at a position after the previous expected
    message bound to the same session, and that native record satisfies no
    other expectation. A user message in the session that contains the text
    with anything added fails the message.
    """
    out = []
    cursor: dict[str, int] = {}
    for exp in sorted(expected, key=lambda e: e["sent_at"]):
        result: dict[str, Any] = {k: exp[k] for k in ("kind", "message_id", "run_id")}
        session = next((session_for_native_id(sessions, n) for n in exp["native_ids"] if session_for_native_id(sessions, n)), None)
        if session is None:
            result.update(passed=False, reason="no copied harness session matches the run's attempt", native_ids=exp["native_ids"])
            out.append(result)
            continue
        texts = session["user_texts"]
        start = cursor.get(session["path"], 0)
        index = next((i for i in range(start, len(texts)) if texts[i] == exp["text"]), None)
        altered = [t[:200] for t in texts if t != exp["text"] and exp["text"].strip() and exp["text"].strip() in t]
        if index is not None:
            cursor[session["path"]] = index + 1
        result.update(
            passed=index is not None and not altered,
            session=session["file"],
            position=index,
            altered_copies=altered,
            t3_note_in_session=any(t.startswith("[T3 Code:") for t in texts),
        )
        out.append(result)
    return out


def injected_material(harness: str, summary: dict[str, Any]) -> dict[str, Any]:
    """The instruction text and tool list the agent actually received."""
    for session in summary["sessions"]:
        if harness == "pi" and session.get("system"):
            system = session["system"]
            sections = system.get("sections", {})
            tools = system.get("toolsAdded") or []
            t3 = sections.get("t3_code_orchestration")
            return {
                "system_sections": list(sections),
                "t3_instructions": t3,
                "t3_instructions_utf8_bytes": len(t3.encode()) if isinstance(t3, str) else None,
                "full_system_sections": sections,
                "tools": [t.get("name") if isinstance(t, dict) else t for t in tools],
                "tool_definitions": tools,
            }
        if harness == "claude" and session.get("shape"):
            shape = session["shape"]
            joined = "\n\n".join(part.get("text", "") for part in shape.get("system", []) if isinstance(part, dict))
            start = joined.find("<runtime_info>")
            t3 = joined[start:] if start >= 0 else None
            direct = [t.get("name") for t in shape.get("tools", []) if isinstance(t, dict)]
            deferred = session.get("deferred_tools", [])
            return {
                "system_prompt": joined,
                "t3_instructions": t3,
                "t3_instructions_utf8_bytes": len(t3.encode()) if t3 else None,
                "t3_markers_found": [m for m in ("<runtime_info>", "## T3 Code orchestration", "<pull_request_linking>") if m in joined],
                "tools": direct + deferred,
                "direct_tools": direct,
                "deferred_tools": deferred,
                "deferred_t3_tools": [t for t in deferred if t.startswith("mcp__t3-code__")],
                "deferred_other_tools": [t for t in deferred if not t.startswith("mcp__t3-code__")],
                "tool_definitions": shape.get("tools", []),
            }
    return {}
