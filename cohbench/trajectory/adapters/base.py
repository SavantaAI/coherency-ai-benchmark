"""The harness profile interface.

A profile is the only harness-specific code on the path from raw records to
the normalised trajectory. Every method returns values from the format's
closed vocabularies (neutral tool names, neutral argument names, cleaned
text and a neutral truncation marker), plus the raw material it removed,
which the normaliser sends to the run manifest. The profile never writes
events itself, so it has no way to add a field to the trajectory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NativeSession:
    """What the cross-check reads from a harness's own session file."""

    path: str
    session_id: str | None
    user_texts: list[str] = field(default_factory=list)
    user_times: list[str | None] = field(default_factory=list)  # when the harness recorded each user message
    assistant_texts: list[str] = field(default_factory=list)  # one per text part, in order
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)  # call id -> {"name", "args", "order"}
    results: dict[str, dict[str, Any]] = field(default_factory=dict)  # call id -> {"text", "is_error"}
    synthetic_ids: set[str] = field(default_factory=set)  # assistant entries the harness made up (not the model)


@dataclass
class Cleaned:
    text: str | None
    truncated: dict[str, Any] | None = None  # {"by", "lines_omitted", "edge"}: edge one of format.CLIP_EDGES
    removed: list[str] = field(default_factory=list)  # raw decoration text, for the manifest


@dataclass
class ToolArgs:
    tool: str  # neutral tool name (a key of format.TOOLS)
    args: dict[str, Any]  # neutral input fields
    withheld: dict[str, Any] = field(default_factory=dict)  # native argument name -> value


def loads_jsonl(path: str | Path) -> list[dict[str, Any]]:
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{Path(path).name}:{n}: {exc}") from None
    return out


def text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def pick(args: dict[str, Any], names: dict[str, tuple[str, ...]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map native argument names to neutral ones. `names` is neutral -> the
    native names that may carry it, in preference order. Every native
    argument not used is returned as withheld."""
    out: dict[str, Any] = {}
    used: set[str] = set()
    for neutral, natives in names.items():
        out[neutral] = None
        for n in natives:
            if n in args:
                out[neutral] = args[n]
                used.add(n)
                break
    withheld = {k: v for k, v in args.items() if k not in used}
    return out, withheld


def as_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def as_str(v: Any) -> str | None:
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


class Profile:
    """Base profile. Subclasses fill the tables and override the cleaners."""

    #: For the run manifest only. Never written to the trajectory.
    name = "base"
    verified = False
    #: T3 `command_execution` titles this harness uses for its shell tool.
    shell_titles: tuple[str, ...] = ()
    #: T3 `file_change` titles: native title -> neutral tool.
    file_change_titles: dict[str, str] = {}
    #: Native (unprefixed, non-driver) tool name -> neutral tool name.
    harness_tools: dict[str, str] = {}

    # -- tools ------------------------------------------------------------

    def harness_tool(self, native_name: str) -> str | None:
        return self.harness_tools.get(native_name)

    def tool_args(self, tool: str, args: Any) -> ToolArgs:
        raise NotImplementedError

    # -- output text -------------------------------------------------------

    def clean_output(self, tool: str, text: str | None) -> Cleaned:
        return Cleaned(text)

    # -- native sessions ---------------------------------------------------

    def read_session(self, path: str | Path) -> NativeSession:
        raise NotImplementedError

    def session_for(self, sessions: list[NativeSession], native_thread_id: str) -> NativeSession | None:
        raise NotImplementedError


# Shared by profiles: the question-tool argument shapes.
def prompts_from(args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Everything the user is shown for a question tool's call, in the neutral
    prompt shape. Pi's diagnostic ask_user is {question, options: [str]}; the
    multi-question shape is {questions: [{header, question, options: [str |
    {label, description}], multiSelect}]}."""

    def opts(raw: Any) -> list[dict[str, Any]]:
        out = []
        for o in raw or []:
            if isinstance(o, str):
                out.append({"label": o, "description": None})
            elif isinstance(o, dict) and isinstance(o.get("label"), str):
                d = o.get("description")
                out.append({"label": o["label"], "description": d if isinstance(d, str) else None})
        return out

    def one(q: dict[str, Any]) -> dict[str, Any]:
        h = q.get("header")
        return {"header": h if isinstance(h, str) else None, "text": q.get("question") if isinstance(q.get("question"), str) else "",
                "options": opts(q.get("options")), "multi_select": bool(q.get("multiSelect") or q.get("multi_select"))}

    if isinstance(args.get("questions"), list):
        qs = [one(q) for q in args["questions"] if isinstance(q, dict)]
        withheld = {k: v for k, v in args.items() if k != "questions"}
        extra = [{k: v for k, v in q.items() if k not in ("header", "question", "options", "multiSelect", "multi_select")} for q in args["questions"] if isinstance(q, dict)]
        if any(extra):
            withheld["questions.extra"] = extra
        return qs, withheld
    return [one(args)], {k: v for k, v in args.items() if k not in ("header", "question", "options", "multiSelect", "multi_select")}
