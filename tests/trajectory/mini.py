"""Minimal T3-shaped threads for tests that need an item the saved runs did not
produce, or a real item in isolation. Field names follow T3's records as
saved in run 2026-11-21T210951-74da66; values are made up."""

from __future__ import annotations

import itertools
from typing import Any

_n = itertools.count(1)
T0 = "2026-11-21T10:00:00.000Z"


def ts(sec: float) -> str:
    whole = int(sec)
    ms = int(round((sec - whole) * 1000))
    return f"2026-11-21T10:{whole // 60:02d}:{whole % 60:02d}.{ms:03d}Z"


def thread(tid: str = "thread:project:p:t1", instance: str = "pi", parent: str | None = None) -> dict[str, Any]:
    return {
        "id": tid,
        "title": "t",
        "providerInstanceId": instance,
        "modelSelection": {"instanceId": instance, "model": "m", "options": []},
        "lineage": {"parentThreadId": parent, "relationshipToParent": "subagent" if parent else None, "rootThreadId": parent or tid},
    }


def run_rec(tid: str, ordinal: int, start: float, end: float | None, status: str = "completed", fresh: bool = False, user_message_id: str | None = None) -> dict[str, Any]:
    r = {"id": f"run:{tid}:{ordinal}", "threadId": tid, "ordinal": ordinal, "status": status, "requestedAt": ts(start), "startedAt": ts(start), "completedAt": ts(end) if end is not None else None, "userMessageId": user_message_id}
    if fresh:
        r["freshContext"] = True
    return r


def item(run: dict[str, Any], typ: str, start: float, end: float | None = None, **fields: Any) -> dict[str, Any]:
    n = next(_n)
    base = {
        "id": f"turn-item:provider:pi:native-item:x{n}",
        "threadId": run["threadId"],
        "runId": run["id"],
        "providerTurnId": f"provider-turn:{run['id']}",
        "nativeItemRef": {"driver": "pi", "nativeId": f"toolu_x{n}", "strength": "strong"},
        "ordinal": n,
        "status": "completed",
        "title": None,
        "startedAt": ts(start),
        "completedAt": ts(end if end is not None else start),
        "type": typ,
    }
    base.update(fields)
    return base


def user(run: dict[str, Any], start: float, text: str, **fields: Any) -> dict[str, Any]:
    mid = f"msg{next(_n)}"
    run["userMessageId"] = run.get("userMessageId") or mid
    fields = {"createdBy": "user", "inputIntent": "turn_start", **fields}
    return item(run, "user_message", start, nativeItemRef=None, messageId=mid, text=text, **fields)


def assistant(run: dict[str, Any], start: float, text: str) -> dict[str, Any]:
    return item(run, "assistant_message", start, text=text, streaming=False)


def command(run: dict[str, Any], start: float, end: float, cmd: str, output: str, exit_code: int = 0, title: str = "bash") -> dict[str, Any]:
    return item(run, "command_execution", start, end, title=title, input=cmd, output=output, exitCode=exit_code)


def dynamic(run: dict[str, Any], start: float, end: float, name: str, inp: Any, output: Any) -> dict[str, Any]:
    return item(run, "dynamic_tool", start, end, title=name, toolName=name, input=inp, output=output)


def export(threads: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]) -> dict[str, Any]:
    """[(thread, runs, items)] -> the export shape `t3.from_export` reads."""
    return {"threads": [{"thread": th, "runs": runs, "attempts": [], "messages": [], "subagents": [], "items": items} for th, runs, items in threads]}
