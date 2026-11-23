"""One test (or more) per transformation rule in cohbench/trajectory/README.md.
Rule numbers match the document."""

from __future__ import annotations

import json
import re

import pytest

from cohbench.trajectory import Hints, NormaliseError, RunnerEvent, Segment, t3, validate

from . import mini
from .conftest import events, export_segment, load, run, snapshot_segment

T3_ID = re.compile(r"turn-item:|provider-thread:|thread:project:|run:thread:|native-item:|toolu_|provider:pi|claudeAgent")


@pytest.fixture(scope="module")
def three():
    return run([export_segment("pi-export-three-turns.json", "pi-export")])


@pytest.fixture(scope="module")
def sub():
    return run([export_segment("pi-subagent.json", "pi-subagent")])


@pytest.fixture(scope="module")
def steer():
    return run([snapshot_segment("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json")])


def mini_run(items_fn, profile=None, hints=None, status="completed"):
    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 30, status=status)
    items = items_fn(r)
    seg = Segment(t3.from_export(mini.export([(th, [r], items)])))
    return run([seg], profile, hints)


def decorations(res, seq=None):
    return [d for d in res.manifest["decorations"] if seq is None or d["seq"] == seq]


# R1 ---------------------------------------------------------------------------


def test_r1_native_ids_become_local_integers(sub):
    text = sub.trajectory.dumps()
    assert not T3_ID.search(text)
    assert [c["call"] for c in events(sub, "tool_call")] == [1, 2, 3]
    assert {e["agent"] for e in events(sub)} == {0, 1}
    m = sub.manifest
    assert m["calls"]["3"]["native_id"].startswith("toolu_")
    assert m["calls"]["2"] == {**m["calls"]["2"], "native_name": "mcp__t3-code__delegate_task", "origin": "driver"}
    assert m["agents"]["1"]["thread"].startswith("thread:delegated-task:")
    assert all(T3_ID.search(v) for v in m["events"].values())
    assert m["turns"]["1"]["runs"][0].startswith("run:thread:")


# R2 ---------------------------------------------------------------------------


def test_r2_times_are_milliseconds_from_the_start_and_in_order(three):
    ev = three.trajectory.events
    assert ev[0]["t"] == 0
    assert all(a["t"] <= b["t"] for a, b in zip(ev, ev[1:]))
    call = events(three, "tool_call")[0]
    assert call["t_end"] >= call["t"]
    assert three.manifest["episode_start_utc"].endswith("Z")


def test_r2_subagent_events_come_between_start_and_end(sub):
    ev = sub.trajectory.events
    start = next(e["seq"] for e in ev if e["type"] == "subagent_start")
    end = next(e["seq"] for e in ev if e["type"] == "subagent_end")
    child = [e["seq"] for e in ev if e["agent"] == 1]
    assert start < min(child) and max(child) < end


# R3 ---------------------------------------------------------------------------


def test_r3_harness_tool_names_become_neutral(three):
    assert {(c["class"], c["tool"]) for c in events(three, "tool_call")} == {("shell", "shell")}
    assert "origin" not in events(three, "tool_call")[0]
    assert three.manifest["calls"]["1"]["native_name"] == "bash"


def test_r3_pi_tools_map_to_neutral_names(pi):
    assert {n: pi.harness_tool(n) for n in ("bash", "read", "write", "edit", "tool_search", "ask_user")} == {
        "bash": "shell", "read": "file_read", "write": "file_write", "edit": "file_edit", "tool_search": "tool_discovery", "ask_user": "ask_user"}


@pytest.mark.parametrize("typ, title", [("command_execution", "Bash"), ("file_change", "Write")])
def test_r3_an_unexpected_title_is_an_unknown_item(typ, title):
    fields = {"input": "ls", "output": "x"} if typ == "command_execution" else {"fileName": "/work/project/a", "newStr": "x"}
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.item(r, typ, 1, 2, title=title, **fields)])
    assert [e["item_type"] for e in events(res, "unknown")] == [typ]
    assert not validate(res.trajectory).ok


# R4 -----------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["mcp__t3-code__", "mcp__t3_code__"])
def test_r4_driver_prefixes_are_removed(prefix):
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.dynamic(r, 1, 2, prefix + "t3_thread_list", {"limit": 3}, {"content": [{"type": "text", "text": "thread:project:x"}]})])
    call = events(res, "tool_call")[0]
    assert (call["tool"], call["class"]) == ("t3_thread_list", "other")
    assert call["input"] == {"raw": '{"limit":3}'}
    assert call["output"] == {"text": None, "available": False, "truncated": False, "clipped": "none", "last_line_whole": True, "exit_code": None}
    assert validate(res.trajectory).ok


def test_r4_an_unlisted_driver_tool_is_an_unknown_event():
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.dynamic(r, 1, 2, "mcp__t3-code__brand_new_tool", {}, None)])
    assert [e["item_type"] for e in events(res, "unknown")] == ["dynamic_tool"]
    rep = validate(res.trajectory)
    assert rep.unknown == 1 and not rep.ok


# R5 -----------------------------------------------------------------------------


def test_r5_native_arguments_map_to_neutral_fields_and_the_rest_is_withheld():
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.dynamic(r, 1, 2, "read", {"path": "/work/project/a.txt", "offset": 3, "extra": 1}, {"content": [{"type": "text", "text": "hi"}]})])
    call = events(res, "tool_call")[0]
    assert call["input"] == {"path": "a.txt", "offset": 3, "limit": None}
    assert {"field": "input.extra", "value": 1, "seq": call["seq"]} in res.manifest["withheld"]
    assert "extra" not in res.trajectory.dumps()


def test_r5_pi_bash_timeout_is_withheld_only_in_the_manifest(pi):
    ta = pi.tool_args("shell", {"command": "ls", "timeout": 10})
    assert ta.args == {"command": "ls"} and ta.withheld == {"timeout": 10}


# R6 -----------------------------------------------------------------------------


@pytest.mark.parametrize("given, want", [
    ("/work/project/notes.txt", "notes.txt"), ("/work/project/./out/a.txt", "out/a.txt"), ("./out/a.txt", "out/a.txt"),
    ("out/a.txt", "out/a.txt"), ("/work/project", "."), ("/tmp/x.log", "/tmp/x.log"), ("/work/project/../etc", "/work/project/../etc"),
])
def test_r6_workspace_paths_are_relative_to_the_root(given, want):
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.dynamic(r, 1, 2, "read", {"path": given}, {"content": [{"type": "text", "text": "x"}]})])
    assert events(res, "tool_call")[0]["input"]["path"] == want


# R7, R8, R9, R10 ----------------------------------------------------------------


def test_r7_pi_truncation_notice_becomes_a_boolean_with_the_detail_in_the_manifest(three):
    big = events(three, "tool_call")[1]
    assert big["output"]["truncated"] is True
    assert big["output"]["text"].endswith("\nEND-T010912")
    assert "Showing lines" not in three.trajectory.dumps() and "pi-bash" not in three.trajectory.dumps()
    d = decorations(three, big["seq"])[0]
    assert d["removed"].startswith("\n\n[Showing lines 1957-3001 of 3001 (50.0KB limit). Full output: /tmp/pi-bash-")
    assert d["truncation"] == {"by": "harness", "lines_omitted": 1956, "edge": "head", "last_line_whole": True}  # Pi keeps the last lines


def test_r8_pi_no_output_placeholder_becomes_empty(pi):
    c = pi.clean_output("shell", "(no output)")
    assert c.text == "" and c.removed == ["(no output)"]
    assert pi.clean_output("shell", "(no output) but more").text == "(no output) but more"


def test_r9_one_final_newline_is_removed_from_shell_output(three):
    first = events(three, "tool_call")[0]
    assert first["output"]["text"].endswith("\nEND-T005522") and first["output"]["truncated"] is False
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.command(r, 1, 2, "x", "a\n\n")])
    assert events(res, "tool_call")[0]["output"]["text"] == "a\n"


def test_r10_driver_transport_cut_is_a_flag_with_the_suffix_in_the_manifest():
    cut = "item 3272\nitem 3273\nite\n… output truncated for transport"
    res = mini_run(lambda r: [mini.user(r, 0, "go"), mini.item(r, "file_change", 1, 2, title="write", fileName="/work/project/big.txt", newStr=cut)])
    call = events(res, "tool_call")[0]
    assert call["input"] == {"path": "big.txt", "content": "item 3272\nitem 3273\nite", "content_truncated": True}
    d = decorations(res, call["seq"])[0]
    assert d["field"] == "input.newStr" and d["removed"] == "\n… output truncated for transport" and d["truncation"]["by"] == "driver"
    cmd = mini_run(lambda r: [mini.user(r, 0, "go"), mini.command(r, 1, 2, "x", "a\n… output truncated for transport")])
    assert events(cmd, "tool_call")[0]["output"]["truncated"] is True


# R11 ----------------------------------------------------------------------------


def test_r11_t3_sender_note_is_removed_from_user_text():
    res = mini_run(lambda r: [mini.user(r, 0, "[T3 Code: the agent in T3 thread abc sent this message, not the user.]\n\nCount the lines.")])
    assert events(res, "user_message")[0]["text"] == "Count the lines."
    assert res.manifest["decorations"][0]["removed"].startswith("[T3 Code:")


def test_r11_t3_role_prefix_is_removed_from_delegated_prompts(sub):
    start = events(sub, "subagent_start")[0]
    child_msg = events(sub, "user_message", agent=1)[0]
    assert start["prompt"] == child_msg["text"] == events(sub, "tool_call", tool="subagent")[0]["input"]["prompt"]
    assert start["prompt"].startswith("In the project at /work/project")
    assert sum(d["removed"] == "Act as the research sub-agent for this task.\n\n" for d in decorations(sub)) == 2


# R12 ----------------------------------------------------------------------------


def test_r12_send_to_user_text_comes_from_the_result():
    real = load("pi-send-to-user-item.json")

    def items(r):
        it = {**real, "threadId": r["threadId"], "runId": r["id"], "startedAt": mini.ts(1), "completedAt": mini.ts(2), "providerTurnId": "pt"}
        return [mini.user(r, 0, "send it"), it, mini.assistant(r, 3, "done")]

    res = mini_run(items)
    call = events(res, "tool_call")[0]
    assert (call["tool"], call["class"]) == ("send_to_user", "message")
    assert call["user_channel"] is True and call["user_text"] == "Build finished at step 3."
    assert call["input"] == {"message": "Build finished at step 3."}
    assert call["output"]["text"] is None
    assert any(w["field"] == "output" for w in res.manifest["withheld"])


def test_r12_failed_send_to_user_is_not_user_channel():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), {**mini.dynamic(r, 1, 2, "mcp__t3-code__send_to_user", {"message": "hi"}, {"content": [{"type": "text", "text": "error"}]}), "status": "failed"}])
    call = events(res, "tool_call")[0]
    assert call["user_channel"] is False and call["status"] == "error"


def test_r12_request_secret_output_keeps_status_and_files_never_a_value():
    out_value = {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"status": "saved", "value": "1234-SECRET"}}
    out_file = {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"status": "saved", "files": {"pin": "/home/node/.t3/userdata/secrets/files/ab12"}}}
    for out, want in ((out_value, '{"status":"saved"}'), (out_file, '{"files":{"pin":"/home/node/.t3/userdata/secrets/files/ab12"},"status":"saved"}')):
        res = mini_run(lambda r, out=out: [mini.user(r, 0, "x"), mini.dynamic(r, 1, 2, "mcp__t3-code__request_secret", {"label": "PIN", "reason": "r", "delivery": "file", "fields": [{"name": "pin", "label": "PIN", "secret": True, "placeholder": "0000"}]}, out)])
        call = events(res, "tool_call")[0]
        assert call["output"]["text"] == want
        assert "1234-SECRET" not in res.trajectory.dumps()
        assert call["input"]["fields"] == [{"name": "pin", "label": "PIN", "secret": True}]


def test_r12_delegate_task_is_one_neutral_subagent_call(sub):
    call = events(sub, "tool_call", tool="subagent")[0]
    assert call["class"] == "subagent" and set(call["input"]) == {"title", "prompt"}
    assert call["subagent"] == 1
    w = {x["field"] for x in sub.manifest["withheld"] if x["seq"] == call["seq"]}
    assert {"input.target", "input.timeoutMs", "input.mode", "input.role", "output"} <= w
    assert "claude-haiku" not in sub.trajectory.dumps() and '"pi"' not in sub.trajectory.dumps()
    assert sub.manifest["agents"]["1"]["origin"] == "driver"


# R13 ----------------------------------------------------------------------------


def test_r13_t3_only_items_are_dropped_and_listed(three):
    dropped = three.manifest["dropped"]
    assert sum(d["type"] == "checkpoint" for d in dropped) == 3
    assert sum(d["reason"] == "T3 workspace preparation step" for d in dropped) == 1
    assert "Workspace ready" not in three.trajectory.dumps()


# R14, R15 -----------------------------------------------------------------------


def test_r14_r15_claude_refusal_becomes_error_events(claude):
    res = run([snapshot_segment("claude-refused-startup-snapshot.json", "claude-refused", profile=claude)], claude)
    assert validate(res.trajectory).ok
    assert [e["class"] for e in events(res, "error")] == ["harness_message", "provider_error"]
    assert events(res, "assistant_message") == []
    assert events(res, "turn_end")[0]["end"] == "failed"
    assert "Credit balance" not in res.trajectory.dumps()
    assert any("Credit balance" in json.dumps(w["value"]) for w in res.manifest["withheld"])


# R16 ----------------------------------------------------------------------------


def test_r16_unknown_item_type_is_kept_as_an_unknown_event():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.item(r, "reasoning", 1, text="thinking about it")])
    assert [x["item_type"] for x in events(res, "unknown")] == ["reasoning"]
    assert "thinking about it" not in res.trajectory.dumps()
    assert any(w["field"] == "item" for w in res.manifest["withheld"])


def test_r16_unknown_harness_tool_is_kept_as_an_unknown_event():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.dynamic(r, 1, 2, "SomeNewTool", {}, None)])
    assert len(events(res, "unknown")) == 1


def test_r16_unknown_status_raises():
    with pytest.raises(NormaliseError):
        mini_run(lambda r: [mini.user(r, 0, "x")], status="exploded")


# R17 ----------------------------------------------------------------------------


def test_r17_user_kinds_from_t3_records(three):
    # The third turn is a fresh-context send the runner did not declare a controlled reset.
    assert [e["kind"] for e in events(three, "user_message")] == ["opening_prompt", "reply", "reply"]


def test_r17_runner_hints_override():
    seg = export_segment("pi-export-three-turns.json")
    second = [i for i in next(iter(seg.threads.values())).items if i["type"] == "user_message"][1]
    res = run([seg], hints=Hints(user_kinds={second["messageId"]: "reopen"}))
    assert [e["kind"] for e in events(res, "user_message")] == ["opening_prompt", "reopen", "reply"]


# R18 ----------------------------------------------------------------------------


def test_r18_final_is_the_last_message_of_a_finished_turn(sub):
    parent = events(sub, "assistant_message", agent=0)
    assert [m["role"] for m in parent] == ["narration", "narration", "final"]
    assert [m["user_channel"] for m in parent] == [False, False, True]
    child = events(sub, "assistant_message", agent=1)
    assert child[0]["role"] == "final" and child[0]["user_channel"] is False


def test_r18_a_message_followed_by_a_tool_call_is_not_final():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.assistant(r, 1, "checking"), mini.command(r, 2, 3, "ls", "a\n")])
    assert events(res, "assistant_message")[0]["role"] == "narration"
    assert validate(res.trajectory).ok


def test_r18_a_waiting_turn_has_no_final_the_question_carries_the_text():
    res = run([snapshot_segment("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic")])
    first = events(res, "assistant_message")[0]
    assert first["text"] == "I'll ask you directly." and first["role"] == "narration" and first["user_channel"] is False
    assert events(res, "assistant_message")[1]["role"] == "final"


def test_r18_interrupted_turn_has_no_final():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.assistant(r, 1, "working")], status="interrupted")
    assert events(res, "assistant_message")[0]["role"] == "narration"
    assert events(res, "turn_end")[0]["end"] == "interrupted"


# R19 ----------------------------------------------------------------------------


def test_r19_blocking_question_splits_the_turn():
    res = run([snapshot_segment("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic")])
    assert validate(res.trajectory).ok
    seq = [e["type"] for e in res.trajectory.events]
    i = seq.index("question")
    assert seq[i : i + 4] == ["question", "turn_end", "answer", "turn_start"]
    q = events(res, "question")[0]
    assert q["kind"] == "question" and q["user_channel"] is True
    assert q["prompts"] == [{"header": "Question", "text": "Which color do you prefer: red or blue?", "options": [{"label": "red", "description": "red"}, {"label": "blue", "description": "blue"}], "multi_select": False}]
    assert q["user_text"] == "Question\nWhich color do you prefer: red or blue?\n- red\n- blue"
    call = events(res, "tool_call", tool="ask_user")[0]
    assert q["call"] == call["call"] and call["question"] == q["question"]
    assert events(res, "turn_end")[0]["end"] == "waiting"
    a = events(res, "answer")[0]
    assert (a["status"], a["text"]) == ("answered", "blue")
    assert res.manifest["questions"]["1"]["answer_source"] == "the question tool's output"


def test_r19_question_descriptions_that_differ_are_shown():
    def items(r):
        return [mini.user(r, 0, "x"), mini.item(r, "user_input_request", 1, 2, requestId="req1", questions=[{"id": "q", "header": "Deploy", "question": "Which?", "multiSelect": True, "options": [{"label": "a", "description": "the old one"}, {"label": "b"}]}])]

    q = events(mini_run(items, hints=Hints(answers={"req1": "a"})), "question")[0]
    assert q["user_text"] == "Deploy\nWhich? (choose one or more)\n- a: the old one\n- b"
    assert q["prompts"][0]["multi_select"] is True


def test_r19_secret_request_shows_label_reason_fields_and_placeholder():
    res = run([snapshot_segment("pi-pending-secret-snapshot.json", "pi-pending-secret")])
    assert validate(res.trajectory).ok
    q = events(res, "question")[0]
    assert q["kind"] == "secret" and q["prompts"] is None
    assert q["secret"] == {"label": "Staging box PIN", "reason": "You asked me to get your 4-digit staging box PIN so I can copy it to pin.txt in the project.", "fields": [{"label": "4-digit staging PIN", "placeholder": "0000", "secret": True}]}
    assert q["user_text"].endswith("\n- 4-digit staging PIN (0000)")
    a = events(res, "answer")[0]
    assert a["status"] == "saved" and a["text"] is None
    call = events(res, "tool_call", tool="request_secret")[0]
    assert call["question"] == 1 and call["class"] == "question"


def test_r19_runner_answer_hint_wins():
    seg = snapshot_segment("pi-pending-diagnostic-snapshot.json")
    req = next(i for i in next(iter(seg.threads.values())).items if i["type"] == "user_input_request")["requestId"]
    res = run([seg], hints=Hints(answers={req: "blue (from the runner)"}))
    assert events(res, "answer")[0]["text"] == "blue (from the runner)"


def test_r19_a_cancelled_answer_has_no_text():
    def items(r):
        q = mini.item(r, "user_input_request", 1, 2, requestId="req1", status="cancelled", questions=[{"id": "q", "question": "Which?", "options": []}])
        return [mini.user(r, 0, "x"), mini.dynamic(r, 0.5, 2, "ask_user", {"question": "Which?", "options": []}, {"content": [{"type": "text", "text": "never mind"}]}), q]

    a = events(mini_run(items), "answer")[0]
    assert (a["status"], a["text"]) == ("cancelled", None)


def test_r19_unanswered_question_ends_the_turn_waiting():
    def items(r):
        return [mini.user(r, 0, "x"), mini.item(r, "user_input_request", 1, None, status="waiting", requestId="req1", questions=[{"id": "q", "question": "Which?", "options": []}])]

    res = mini_run(items, status="running")
    assert [e["type"] for e in res.trajectory.events][-2:] == ["question", "turn_end"]
    assert events(res, "turn_end")[0]["end"] == "waiting"
    assert validate(res.trajectory).ok


# R20 ----------------------------------------------------------------------------


def test_r20_undeclared_fresh_context_is_a_context_boundary(three):
    assert [s["cause"] for s in events(three, "segment_start")] == ["start", "context"]
    assert [e["segment"] for e in events(three, "user_message")] == [1, 1, 2]


def test_r20_a_declared_reset_is_a_controlled_reset():
    seg = export_segment("pi-export-three-turns.json")
    third = [i for i in next(iter(seg.threads.values())).items if i["type"] == "user_message"][2]
    res = run([seg], hints=Hints(runner=[RunnerEvent("controlled_reset", third["startedAt"], {"message": third["messageId"]})]))
    assert validate(res.trajectory).ok, validate(res.trajectory).errors
    assert [s["cause"] for s in events(res, "segment_start")] == ["start", "reset"]
    assert events(res, "user_message")[2]["kind"] == "reset"
    cr = events(res, "controlled_reset")[0]
    assert res.trajectory.events[cr["target"]]["kind"] == "reset"


# R21 ----------------------------------------------------------------------------


def test_r21_a_correction_is_dated_at_delivery(steer):
    corr = events(steer, "user_message")[1]
    assert corr["kind"] == "correction" and corr["turn"] == 1
    s = steer.manifest["steering"][0]
    # T3 completes the steer item when Pi takes it (43.494); Pi's session entry says 43.490.
    assert s["mechanism"] == "native" and s["delivery_source"] == "T3 item completion"
    assert s["queued_utc"] == "2026-11-21T21:14:37.206Z" and s["delivered_utc"] == "2026-11-21T21:14:43.494Z"
    # Delivered after step3's call, which Pi had already started.
    order = [(e["type"], (e.get("input") or {}).get("command")) for e in steer.trajectory.events if e["type"] in ("tool_call", "user_message")]
    assert order.index(("user_message", None)) == 0
    assert [x for x in order if x[0] == "user_message"] and order[order.index(("tool_call", "sleep 6; echo step3")) + 1][0] == "user_message"


def test_r21_runner_delivery_time_wins():
    seg = snapshot_segment("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json")
    msg = [i for i in next(iter(seg.threads.values())).items if i["type"] == "user_message"][1]
    res = run([seg], hints=Hints(runner=[RunnerEvent("correction_delivered", "2026-11-21T21:14:43.600Z", {"message": msg["messageId"]})]))
    s = res.manifest["steering"][0]
    assert s["delivery_source"] == "runner" and s["delivered_utc"] == "2026-11-21T21:14:43.600Z"
    cd = events(res, "correction_delivered")[0]
    assert res.trajectory.events[cd["target"]]["kind"] == "correction"


# R22 ----------------------------------------------------------------------------


def test_r22_two_concurrent_subagents():
    th = mini.thread("thread:project:p:parent")
    r = mini.run_rec(th["id"], 1, 0, 20)
    c1, c2 = mini.thread("thread:child:1", parent=th["id"]), mini.thread("thread:child:2", parent=th["id"])
    cr1, cr2 = mini.run_rec(c1["id"], 1, 2, 9), mini.run_rec(c2["id"], 1, 3, 12)
    parent_items = [
        mini.user(r, 0, "do two things"),
        mini.dynamic(r, 1, 2, "mcp__t3-code__delegate_task", {"title": "one", "task": "first"}, {"content": [{"type": "text", "text": "thread:child:1"}]}),
        mini.item(r, "subagent", 2, 9, title="one", prompt="first", result="r1", childThreadId=c1["id"]),
        mini.dynamic(r, 2.5, 3, "mcp__t3-code__delegate_task", {"title": "two", "task": "second"}, {"content": [{"type": "text", "text": "thread:child:2"}]}),
        mini.item(r, "subagent", 3, 12, title="two", prompt="second", result="r2", childThreadId=c2["id"]),
        mini.assistant(r, 5, "both started"),
        mini.assistant(r, 15, "both done"),
    ]
    for it in parent_items[1:5]:
        it["providerTurnId"] = "pt"
    child1 = [mini.user(cr1, 2, "first", createdBy="agent"), mini.command(cr1, 4, 8, "a", "1\n"), mini.assistant(cr1, 8.5, "r1")]
    child2 = [mini.user(cr2, 3, "second", createdBy="agent"), mini.command(cr2, 3.5, 11, "b", "2\n"), mini.assistant(cr2, 11.5, "r2")]
    res = run([Segment(t3.from_export(mini.export([(th, [r], parent_items), (c1, [cr1], child1), (c2, [cr2], child2)])), th["id"])])
    rep = validate(res.trajectory)
    assert rep.ok, rep.errors
    # The parent's message at 5 s stays at 5 s, between the children's events.
    msg = events(res, "assistant_message", text="both started")[0]
    assert msg["t"] == 5000
    assert [e["subagent"] for e in events(res, "subagent_start")] == [1, 2]
    assert [e["call"] for e in events(res, "tool_call")] == [1, 2, 3, 4]


# R23, R24 -----------------------------------------------------------------------


def test_r23_omitted_outputs_come_from_the_harness_session():
    res = run([snapshot_segment("pi-pending-secret-snapshot.json", "pi-pending-secret")])
    shell = events(res, "tool_call", tool="shell")[0]
    assert shell["output"]["text"] == "" and shell["output"]["available"]
    assert set(res.manifest["output_sources"].values()) == {"harness session"}


def test_r23_without_a_session_omitted_outputs_are_unavailable():
    res = run([snapshot_segment("pi-pending-secret-snapshot.json")])
    shell = events(res, "tool_call", tool="shell")[0]
    assert shell["output"] == {"text": None, "available": False, "truncated": False, "clipped": "none", "last_line_whole": True, "exit_code": 0}


def test_r23_transcript_fills_outputs_and_file_content():
    res = run([snapshot_segment("pi-steering-snapshot.json", None, "pi-steering-transcript.json")])
    assert [c["output"]["text"] for c in events(res, "tool_call", tool="shell")] == ["step1", "step2", "step3"]
    fc = events(res, "tool_call", tool="file_write")[0]
    assert fc["input"] == {"path": "stopped.txt", "content": "halted\n", "content_truncated": False}


def test_r24_file_change_has_no_output(steer):
    fc = events(steer, "tool_call", tool="file_write")[0]
    assert fc["output"]["available"] is False


# R25: Claude Code cleaners, UNVERIFIED shapes ----------------------------------


def test_r25_claude_output_cleaners(claude):
    c = claude.clean_output("shell", "a\nb\n\n... [12 lines truncated] ...")
    assert (c.text, c.truncated) == ("a\nb", {"by": "harness", "lines_omitted": 12, "edge": "tail", "last_line_whole": True})
    c = claude.clean_output("shell", "<persisted-output>\nOutput too large (49.0KB). Full output saved to: /home/node/.claude/projects/-work-project/u/tool-results/x.txt\n\nPreview (first 2KB):\nrow 1\nrow 2\n...\n</persisted-output>")
    assert (c.text, c.truncated) == ("row 1\nrow 2", {"by": "harness", "lines_omitted": None, "edge": "tail", "last_line_whole": False})
    assert claude.clean_output("shell", "<system>Tool ran without output or errors</system>").text == ""
    c = claude.clean_output("file_read", "     1\tfirst\n     2\tsecond\n\n<system-reminder>\nbe careful\n</system-reminder>\n")
    assert c.text == "first\nsecond"
    assert claude.clean_output("ask_user", 'User has answered your questions: "Which color?"="blue". You can now continue.').text == "blue"
    assert claude.tool_args("file_edit", {"file_path": "/w/a", "old_string": "x", "new_string": "y", "replace_all": True}).withheld == {"replace_all": True}


def test_r25_every_claude_tool_in_the_request_bodies_is_mapped(claude):
    names = [n for n in load("claude-tool-names.json") if not n.startswith("mcp__")]
    assert len(names) == 26
    assert [n for n in names if claude.harness_tool(n) is None] == []
    assert sum(claude.harness_tool(n) == "harness_other" for n in names) == 16


# R26 ----------------------------------------------------------------------------


def test_r26_non_text_output_parts_are_recorded_not_dropped():
    out = {"content": [{"type": "text", "text": "see image"}, {"type": "image", "mimeType": "image/png"}]}
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.dynamic(r, 1, 2, "read", {"path": "/work/project/a.png"}, out)])
    call = events(res, "tool_call")[0]
    assert call["output"]["text"] == "see image"
    w = [x for x in res.manifest["withheld"] if x["field"] == "output.non_text"]
    assert w and w[0]["seq"] == call["seq"] and w[0]["value"] == [{"type": "image", "mimeType": "image/png"}]


def test_r21_without_a_session_the_t3_completion_still_dates_the_correction():
    res = run([snapshot_segment("pi-steering-snapshot.json", None, "pi-steering-transcript.json")])
    assert res.manifest["steering"][0]["delivered_utc"] == "2026-11-21T21:14:43.494Z"


def test_r21_a_runner_delivery_by_item_moves_the_message():
    seg = snapshot_segment("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json")
    msg = [i for i in next(iter(seg.threads.values())).items if i["type"] == "user_message"][1]
    res = run([seg], hints=Hints(runner=[RunnerEvent("correction_delivered", "2026-11-21T21:14:43.700Z", {"item": msg["id"]})]))
    cd = events(res, "correction_delivered")[0]
    assert res.trajectory.events[cd["target"]]["t"] == cd["t"]
    assert res.manifest["steering"][0]["delivery_source"] == "runner"


def test_r21_unknown_delivery_is_explicit():
    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 9)
    items = [mini.user(r, 0, "go"), mini.command(r, 1, 3, "x", "a"), mini.user(r, 2, "stop", inputIntent="steer", completedAt=mini.ts(2)), mini.assistant(r, 5, "ok")]
    res = run([Segment(t3.from_export(mini.export([(th, [r], items)])))])
    assert res.manifest["steering"][0]["delivery_source"] == "unknown: queue time used"


def test_r19_an_answer_that_cannot_be_recovered_is_an_error():
    with pytest.raises(NormaliseError, match="neither the runner nor the question tool's output gives the answer"):
        run([snapshot_segment("pi-pending-diagnostic-snapshot.json")])
    res = run([snapshot_segment("pi-pending-diagnostic-snapshot.json")], hints=Hints(audit_only=True))
    a = events(res, "answer")[0]
    assert a["missing"] is True and a["text"] is None and validate(res.trajectory).ok


def test_r23_file_content_falls_back_to_the_harness_session():
    res = run([snapshot_segment("pi-steering-snapshot.json", "pi-steering")])
    fc = events(res, "tool_call", tool="file_write")[0]
    assert fc["input"]["content"] == "halted\n"
    assert res.manifest["output_sources"][res.manifest["calls"][str(fc["call"])]["item"]] == "harness session (file content)"


def test_a_thread_with_items_but_no_runs_still_starts_a_segment():
    th = mini.thread()
    fake_run = {"id": "run:gone", "threadId": th["id"]}
    items = [mini.user(fake_run, 0, "hello"), mini.assistant(fake_run, 1, "hi")]
    res = run([Segment(t3.from_export(mini.export([(th, [], items)])))])
    assert res.trajectory.events[0]["type"] == "segment_start"
    assert validate(res.trajectory).ok


def test_the_header_records_the_excluded_intervals():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.assistant(r, 10, "done")], hints=Hints(excluded=[{"start_utc": mini.ts(3), "end_utc": mini.ts(5), "reason": "probe"}]))
    assert res.trajectory.header["excluded"] == [{"us": 3000000, "us_end": 5000000, "reason": "probe"}]
    from cohbench.trajectory import scorer_view

    v = scorer_view(res.trajectory)
    assert v.header["duration_ms"] == 28000  # the run ends at 30 s wall, 2 s excluded


def test_r27_a_native_compaction_is_recorded_with_its_outcome():
    """Mapped from T3's source (no driver export with one was saved): status,
    the harness in `driver`, an optional summary and token counts."""
    res = mini_run(lambda r: [mini.user(r, 0, "/compact"),
                              mini.item(r, "compaction", 1, 2, status="failed", title="Context compaction failed", driver="pi", summary="(none)", beforeTokenCount=900)])
    c = events(res, "compaction")[0]
    assert (c["status"], c["turn"]) == ("error", 1)
    assert validate(res.trajectory).ok
    text = res.trajectory.dumps()
    assert '"pi"' not in text and "900" not in text and "(none)" not in text
    assert {w["field"] for w in res.manifest["withheld"] if w["seq"] == c["seq"]} >= {"driver", "summary", "beforeTokenCount"}
    from cohbench.trajectory import scorer_view

    assert "compaction" not in scorer_view(res.trajectory).dumps()
