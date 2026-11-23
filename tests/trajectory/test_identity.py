"""The same logical episode from two differently shaped raw inputs normalises
to byte-identical trajectories.

The second input is built by rewriting a saved Pi record into Claude Code's
known shapes: provider instance `claudeAgent` in ids and records, Claude
session uuids for native thread ids, Claude tool names (`Bash`, `Write`,
`Read`, `ToolSearch`, `AskUserQuestion` with its argument and answer shapes,
`file_path`), the other T3 tool prefix, the model slug, and Claude's
truncation and empty-output decorations. The Claude shapes of tool calls and
decorations are UNVERIFIED (no Claude Code turn succeeded in the driver
proof); the test shows that the normaliser maps them to the same output, not
that Claude Code produces exactly them.
"""

from __future__ import annotations

import copy
import json
import re
import uuid

import pytest

from cohbench.trajectory import Segment, t3, validate
from cohbench.trajectory.adapters import ClaudeCodeProfile, PiProfile

from .conftest import load, run

PI_NOTICE = re.compile(r"\n\n\[Showing lines (\d+)-(\d+) of (\d+) \([^)]*\)\. Full output: /tmp/pi-bash-[0-9a-f]+\.log\]\Z")
TITLES = {"bash": "Bash", "write": "Write", "read": "Read", "edit": "Edit", "tool_search": "ToolSearch", "ask_user": "AskUserQuestion"}


def _ids(s: str) -> str:
    s = re.sub(r"provider((?:%(?:25)*3A)|:)pi((?:%(?:25)*3A)|:)", r"provider\1claudeAgent\2", s)
    return s.replace("provider-instance:pi:", "provider-instance:claudeAgent:")


def same(a, b) -> bool:
    """Byte identity, except which edge a clip took. The converter turns
    Pi's notice (Pi keeps the last lines) into Claude Code's (which keeps the
    first) over the same retained text, which no real Claude Code run would
    produce; the edge is real information, kept in the full trajectory and
    read only to decide whether a result line survived whole."""
    mask = lambda t: re.sub(r'"clipped":"(head|tail|both|middle)"', '"clipped":"cut"', t.dumps())  # noqa: E731
    return mask(a) == mask(b)


def _claude_output(text: str) -> str:
    if text == "(no output)":
        return "<system>Tool ran without output or errors</system>"
    m = PI_NOTICE.search(text)
    if m:
        a, b, total = map(int, m.groups())
        return text[: m.start()] + f"\n\n... [{total - (b - a + 1)} lines truncated] ..."
    return text


def _item(i: dict) -> dict:
    ref = i.get("nativeItemRef")
    if ref:
        ref["driver"] = "claudeAgent"
    if i["type"] == "subagent":
        i["driver"] = i["providerInstanceId"] = "claudeAgent"
    title = i.get("title")
    if i["type"] in ("command_execution", "file_change") and title in TITLES:
        i["title"] = TITLES[title]
    if i["type"] == "command_execution" and isinstance(i.get("output"), str):
        i["output"] = _claude_output(i["output"])
    if i["type"] == "dynamic_tool":
        name = i["toolName"]
        if name.startswith("mcp__t3-code__"):
            i["toolName"] = i["title"] = "mcp__t3_code__" + name[len("mcp__t3-code__"):]
        elif name in TITLES:
            i["toolName"] = i["title"] = TITLES[name]
        if name == "read":
            i["input"] = {"file_path": i["input"].pop("path"), **i["input"]}
        if name == "ask_user":
            q = i["input"]
            i["input"] = {"questions": [{"question": q["question"], "options": [{"label": o} for o in q["options"]], "multiSelect": False}]}
            if isinstance(i.get("output"), dict):
                ans = t3.tool_text(i["output"])
                i["output"] = {"content": [{"type": "text", "text": f'User has answered your questions: "{q["question"]}"="{ans}". You can now continue with the user\'s answers in mind.'}]}
        if name.endswith("delegate_task") and isinstance(i["input"].get("target"), dict):
            i["input"]["target"] = {"providerInstanceId": "claudeAgent", "model": "claude-haiku-5-5"}
        if name.endswith("orchestrator_capabilities") and isinstance(i.get("output"), dict):
            i["output"] = {"content": [{"type": "text", "text": '{"inheritedProviderInstanceId":"claudeAgent"}'}]}
    return i


def _record(rec: dict) -> None:
    if "providerInstanceId" in rec:
        rec["providerInstanceId"] = "claudeAgent"
    if isinstance(rec.get("modelSelection"), dict):
        rec["modelSelection"] = {"instanceId": "claudeAgent", "model": "claude-haiku-5-5", "options": [{"id": "effort", "value": "low"}]}
    if "model" in rec and rec.get("model"):
        rec["model"] = "claude-haiku-5-5"
    if "driver" in rec:
        rec["driver"] = "claudeAgent"
    if rec.get("nativeThreadId"):
        rec["nativeThreadId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, rec["nativeThreadId"]))


def to_claude_threads(threads: list[dict]) -> list[dict]:
    out = json.loads(_ids(json.dumps(threads)))
    for th in out:
        _record(th["thread"])
        for key in ("runs", "attempts", "subagents", "messages"):
            for r in th.get(key, []):
                _record(r)
        th["items"] = [_item(i) for i in th["items"]]
    return out


def _segments_export(obj: dict) -> list[Segment]:
    return [Segment(t3.from_export(obj))]


def _segments_joined(obj: dict) -> list[Segment]:
    return [Segment(s) for s in t3.from_joined(obj)]


def _diagnostic_with_answer() -> dict:
    snap = load("pi-pending-diagnostic-snapshot.json")
    p = snap["projection"]
    for i in p["turnItems"]:
        if i.get("toolName") == "ask_user":
            i["output"] = {"content": [{"type": "text", "text": "blue"}]}
            i["outputOmitted"] = False
    return {"threads": [{"thread": p["thread"], "runs": p["runs"], "attempts": p["attempts"], "messages": p["messages"], "subagents": p["subagents"], "items": p["turnItems"], "runtimeRequests": p["runtimeRequests"]}]}


CASES = {
    "three turns, Pi truncation, fresh context": lambda: load("pi-export-three-turns.json"),
    "subagent through delegate_task": lambda: load("pi-subagent.json"),
    "pending question with answer": _diagnostic_with_answer,
    "secret request and tool discovery": lambda: (lambda p: {"threads": [{"thread": p["thread"], "runs": p["runs"], "attempts": p["attempts"], "messages": p["messages"], "subagents": p["subagents"], "items": p["turnItems"], "runtimeRequests": p["runtimeRequests"]}]})(load("pi-pending-secret-snapshot.json")["projection"]),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_same_episode_two_shapes_byte_identical(case):
    pi_obj = CASES[case]()
    claude_obj = {"threads": to_claude_threads(copy.deepcopy(pi_obj["threads"]))}
    assert json.dumps(pi_obj) != json.dumps(claude_obj)
    a = run(_segments_export(pi_obj), PiProfile())
    b = run(_segments_export(claude_obj), ClaudeCodeProfile())
    assert validate(a.trajectory).ok and validate(b.trajectory).ok
    assert same(a.trajectory, b.trajectory)
    assert (a.manifest["trajectory_sha256"] == b.manifest["trajectory_sha256"]) == (a.trajectory.dumps() == b.trajectory.dumps())
    # The manifests are where the two differ.
    assert a.manifest["adapter"]["profile"] != b.manifest["adapter"]["profile"]
    assert a.manifest["observed"] != b.manifest["observed"]


def test_handoff_two_segments_byte_identical():
    pi_obj = load("pi-handoff-joined.json")
    claude_obj = copy.deepcopy(pi_obj)
    claude_obj["segments"] = [{**s, **to_claude_threads([s])[0]} for s in pi_obj["segments"]]
    a = run(_segments_joined(pi_obj), PiProfile())
    b = run(_segments_joined(claude_obj), ClaudeCodeProfile())
    assert a.trajectory.header["segments"] == 2
    assert same(a.trajectory, b.trajectory)


def test_wrong_profile_fails_closed():
    """Claude-shaped records through the Pi profile do not normalise quietly:
    the unknown tool names surface as unknown events."""
    from cohbench.trajectory import Hints

    claude_obj = {"threads": to_claude_threads(copy.deepcopy(_diagnostic_with_answer()["threads"]))}
    res = run(_segments_export(claude_obj), PiProfile(), Hints(audit_only=True))
    rep = validate(res.trajectory)
    assert rep.unknown >= 1 and not rep.ok


# Session-backed cases ------------------------------------------------------------


def _claude_sessions(pi_sessions, claude_threads, directory):
    """Claude Code session files for the rewritten threads, read back with the
    Claude profile's own reader. Each holds the same user messages, at the
    same times, as the Pi session (the test asks whether one logical episode
    gives one output, so the timing must be the same), Claude tool names and
    arguments, and Claude-shaped results. File writes carry `file_path` and
    `content`, so the cross-check compares file-change text too."""
    import json as _json

    items = {(i.get("nativeItemRef") or {}).get("nativeId"): i for th in claude_threads for i in th["items"]}
    out = []
    for s in pi_sessions:
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, "/home/node/.pi/agent/sessions/--work-project--/" + s.path.rsplit("/", 1)[-1]))
        rows = []
        for text, ts in zip(s.user_texts, s.user_times):
            rows.append({"type": "user", "timestamp": ts, "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": text}})
        for text in s.assistant_texts:
            rows.append({"type": "assistant", "uuid": str(uuid.uuid4()), "message": {"role": "assistant", "model": "claude-haiku-5-5", "content": [{"type": "text", "text": text}]}})
        for cid, call in sorted(s.calls.items(), key=lambda kv: kv[1]["order"]):
            it = items.get(cid)
            if it is None:
                continue
            name = it.get("toolName") or it.get("title")
            if it["type"] == "command_execution":
                args = {"command": it["input"], "description": "run it"}
            elif it["type"] == "file_change":
                args = {"file_path": it.get("fileName"), "content": (call.get("args") or {}).get("content", it.get("newStr"))}
            else:
                args = it.get("input")
            rows.append({"type": "assistant", "uuid": str(uuid.uuid4()), "message": {"role": "assistant", "model": "claude-haiku-5-5", "content": [{"type": "tool_use", "id": cid, "name": name, "input": args}]}})
            res = s.results.get(cid)
            if res is not None:
                text = res["text"]
                if it["type"] == "command_execution":
                    text = _claude_output(text)
                elif name == "AskUserQuestion":
                    q = it["input"]["questions"][0]["question"]
                    text = f'User has answered your questions: "{q}"="{text}". You can now continue with the user\'s answers in mind.'
                elif it["type"] == "file_change":
                    text = f"File created successfully at: {it.get('fileName')}"
                rows.append({"type": "user", "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": [{"type": "text", "text": text}], "is_error": res["is_error"]}]}})
        path = directory / f"{sid}.jsonl"
        path.write_text("".join(_json.dumps(r) + "\n" for r in rows))
        out.append(ClaudeCodeProfile().read_session(path))
    return out


@pytest.mark.parametrize("snap, sess, transcript", [
    ("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic", None),
    ("pi-pending-secret-snapshot.json", "pi-pending-secret", None),
    ("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json"),
])
def test_session_backed_episodes_byte_identical(snap, sess, transcript, tmp_path):
    from .conftest import sessions

    p = load(snap)["projection"]
    tr = load(transcript) if transcript else None
    pi_thread = {"thread": p["thread"], "runs": p["runs"], "attempts": p["attempts"], "messages": p["messages"], "subagents": p["subagents"], "items": p["turnItems"], "runtimeRequests": p["runtimeRequests"]}
    claude_thread = to_claude_threads([copy.deepcopy(pi_thread)])[0]
    pi_sess = sessions(PiProfile(), sess)
    a = run([Segment(t3.from_snapshot({"projection": {**p}}, tr), None, pi_sess)], PiProfile())
    claude_proj = {**p, "thread": claude_thread["thread"], "runs": claude_thread["runs"], "attempts": claude_thread["attempts"], "turnItems": claude_thread["items"]}
    claude_tr = {"items": [{"item": t} for t in to_claude_threads([{"thread": p["thread"], "runs": [], "attempts": [], "items": [e["item"] for e in tr["items"]]}])[0]["items"]]} if tr else None
    b = run([Segment(t3.from_snapshot({"projection": claude_proj}, claude_tr), None, _claude_sessions(pi_sess, [claude_thread], tmp_path))], ClaudeCodeProfile())
    assert validate(a.trajectory).ok and validate(b.trajectory).ok
    assert a.manifest["crosscheck"] == [] and b.manifest["crosscheck"] == []
    assert set(a.manifest["output_sources"].values()) == set(b.manifest["output_sources"].values())
    assert same(a.trajectory, b.trajectory)


# The file-edit and search classes ------------------------------------------------


def test_edit_and_search_byte_identical():
    from . import mini

    def episode(names):
        th = mini.thread()
        r = mini.run_rec(th["id"], 1, 0, 9)
        items = [
            mini.user(r, 0, "fix it"),
            mini.dynamic(r, 1, 2, names["grep"], names["grep_args"], {"content": [{"type": "text", "text": "a.py:3: x = 1"}]}),
            mini.item(r, "file_change", 3, 4, title=names["edit"], fileName="/work/project/a.py", oldStr="x = 1", newStr="x = 2"),
            mini.assistant(r, 5, "done"),
        ]
        return mini.export([(th, [r], items)])

    pi_obj = episode({"grep": "grep", "grep_args": {"pattern": "x =", "path": "/work/project"}, "edit": "edit"})
    claude_obj = episode({"grep": "Grep", "grep_args": {"pattern": "x =", "path": "/work/project", "output_mode": "content"}, "edit": "Edit"})
    a = run(_segments_export(pi_obj), PiProfile())
    b = run(_segments_export(claude_obj), ClaudeCodeProfile())
    classes = [e["class"] for e in a.trajectory.events if e["type"] == "tool_call"]
    assert classes == ["search", "file_edit"]
    assert same(a.trajectory, b.trajectory)


# A different layout: interrupt and restart against a native steer ---------------


def _restart_layout(p: dict, tr: dict, delivered: str) -> tuple[dict, dict]:
    """Rebuild the native-steer steering run as an interrupt and a restart, the
    other mechanism the root README section 3 describes: the first run stops when the
    correction is delivered, a second run starts with it. Item ordinals inside
    each run are also reversed, so T3's item order differs from time order."""
    from datetime import datetime

    def ts(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    run1 = copy.deepcopy(p["runs"][0])
    run2 = copy.deepcopy(run1)
    full = {e["item"]["id"]: e["item"] for e in tr["items"]}
    items = [copy.deepcopy({**i, **{k: v for k, v in full.get(i["id"], {}).items() if k not in i}}) for i in p["turnItems"]]
    for i in items:
        if "output" in i:
            i["outputOmitted"] = False
    corr = next(i for i in items if i.get("inputIntent") == "steer")
    run1.update(status="interrupted", completedAt=delivered)
    run2.update(id=run1["id"] + ":restart", ordinal=2, startedAt=delivered, requestedAt=corr["startedAt"], userMessageId=corr["messageId"], completedAt=p["runs"][0]["completedAt"], status="completed")
    corr.update(inputIntent="turn_start", startedAt=delivered, completedAt=delivered)
    first = [i for i in items if i is not corr and ts(i["startedAt"]) < ts(delivered)]
    second = [corr] + [i for i in items if i is not corr and ts(i["startedAt"]) >= ts(delivered)]
    for i in second:
        i["runId"] = run2["id"]
    base = 1000
    for group in (first, second):
        for n, i in enumerate(reversed(group)):
            i["ordinal"] = base + n
        base += 1000
    att = [dict(a) for a in p["attempts"]] + [{**p["attempts"][0], "id": "attempt-restart", "runId": run2["id"]}]
    return {"thread": p["thread"], "runs": [run1, run2], "attempts": att, "messages": [], "subagents": [], "items": first + second, "runtimeRequests": []}, {}


def test_interrupt_and_restart_matches_native_steer():
    from .conftest import sessions

    p = load("pi-steering-snapshot.json")["projection"]
    tr = load("pi-steering-transcript.json")
    a = run([Segment(t3.from_snapshot({"projection": p}, tr), None, sessions(PiProfile(), "pi-steering"))], PiProfile())
    delivered = a.manifest["steering"][0]["delivered_utc"]
    restart, _ = _restart_layout(p, tr, delivered)
    claude = to_claude_threads([restart])[0]
    b = run([Segment(t3.from_export({"threads": [claude]}))], ClaudeCodeProfile())
    rep = validate(b.trajectory)
    assert rep.ok, rep.errors
    assert [x["mechanism"] for x in b.manifest["steering"] if "mechanism" in x] == ["restart"]
    assert any("interrupted_run" in x for x in b.manifest["steering"])
    assert [x["mechanism"] for x in a.manifest["steering"]] == ["native"]
    assert same(a.trajectory, b.trajectory)


def test_session_backed_handoff_with_file_writes(tmp_path):
    """Two sources, file writes cross-checked against Claude-shaped sessions."""
    from .conftest import sessions

    pi_obj = load("pi-handoff-joined.json")
    pi_segs = t3.from_joined(pi_obj)
    pi_sess = [sessions(PiProfile(), "pi-handoff-1"), sessions(PiProfile(), "pi-handoff-2")]
    a = run([Segment(s, None, ss) for s, ss in zip(pi_segs, pi_sess)], PiProfile())
    claude_obj = copy.deepcopy(pi_obj)
    claude_obj["segments"] = [{**s, **to_claude_threads([s])[0]} for s in pi_obj["segments"]]
    claude_segs = t3.from_joined(claude_obj)
    c_sess = []
    for n, (seg, ss) in enumerate(zip(claude_obj["segments"], pi_sess)):
        d = tmp_path / str(n)
        d.mkdir()
        c_sess.append(_claude_sessions(ss, [seg], d))
    b = run([Segment(s, None, ss) for s, ss in zip(claude_segs, c_sess)], ClaudeCodeProfile())
    assert a.manifest["crosscheck"] == [] and b.manifest["crosscheck"] == []
    writes = [e for e in b.trajectory.events if e["type"] == "tool_call" and e["tool"] == "file_write"]
    assert len(writes) == 3
    assert same(a.trajectory, b.trajectory)


def test_claude_session_file_write_text_is_cross_checked(tmp_path):
    from .conftest import sessions

    pi_obj = load("pi-handoff-joined.json")
    seg0 = pi_obj["segments"][0]
    claude_seg = to_claude_threads([copy.deepcopy(seg0)])[0]
    c_sess = _claude_sessions(sessions(PiProfile(), "pi-handoff-1"), [claude_seg], tmp_path)
    nid = next((i.get("nativeItemRef") or {}).get("nativeId") for i in claude_seg["items"] if i["type"] == "file_change")
    c_sess[0].calls[nid]["args"]["content"] = "something else"
    b = run([Segment(t3.from_export({"threads": [claude_seg]}), None, c_sess)], ClaudeCodeProfile())
    assert [f["finding"] for f in b.manifest["crosscheck"]] == ["file change text differs"]
