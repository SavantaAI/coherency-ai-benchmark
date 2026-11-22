"""The eighth review round's findings: T3's real treatment of delegation, the
abandon entry, and the minors."""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager, redirect_stderr

import pytest

from cohbench.scripted import App, Episode, ScriptError, load_script
from cohbench.scripted.__main__ import main as cli_main
from cohbench.scripted.engine import is_completion_prompt

from .conftest import Convo, Server, calls_of, text_of
from .test_round6 import pi_resend

FALLBACK = "Sorry, I can't continue with this right now."
NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
TOOLS = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "mcp__t3-code__delegate_task")]
TASK_ID = "node:delegated-task:command%3Amcp%3A3f778c78%3Adelegate-task%3A5c3795f0"


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def completion(*task_ids: str) -> str:
    """T3's delegatedCompletionText (ProviderContinuationService.ts)."""
    if len(task_ids) == 1:
        return (f"Delegated task {task_ids[0]} reached a terminal state. "
                f"Use task_status with taskId {task_ids[0]} to read the result.")
    return (f"Delegated tasks {', '.join(task_ids)} reached terminal states. "
            "Use task_status with each taskId to read the results.")


@contextmanager
def fresh_server(tmp_path, name, **kw):
    app = App(Episode(), tmp_path / name, read_timeout=5.0, **kw)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    try:
        yield Server(app, api, admin)
    finally:
        app.stop()


# -------------------------------------------------- 1. T3's launch envelope


def delegating(task: str, role: str | None = "research", child_steps=None) -> dict:
    inp = {} if role is None else {"role": role}
    return {"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": task, "input": inp}],
                                         "final": "parent done"}, {"steps": [], "final": "main next"}],
            "children": {"w": {"handlers": [{"steps": child_steps or [], "final": "child reply"}]}}}


def test_1_a_trailing_newline_is_trimmed_as_t3_trims_it(server):
    server.load(delegating("Check the output.\n"))
    r = Convo(server, tools=TOOLS).user("go")
    assert calls_of(r)[0]["input"]["task"] == "Check the output.\n", "the call carries the task as written"
    launch = f"{NOTE}\n\nAct as the research sub-agent for this task.\n\nCheck the output."
    assert text_of(Convo(server, tools=TOOLS).user(launch)) == "child reply"
    assert server.app.log.lines[-1]["identity"]["decision"] == "child launch" and codes(server) == []


def test_1_a_leading_command_gets_no_sender_note(server):
    """T3's withMessageSenderNote leaves a prompt that starts with a slash
    command or a skill mention unchanged, so the child sees no note."""
    server.load(delegating("/review the last commit", role=None))
    Convo(server, tools=TOOLS).user("go")
    assert text_of(Convo(server, tools=TOOLS).user("/review the last commit")) == "child reply"
    assert server.app.log.lines[-1]["conversation_kind"] == "child"


@pytest.mark.parametrize("text,expected", [("/x", True), ("  /x", True), ("$deploy", True), ("$1a", True),
                                           ("$123", False), ("€:run", True), ("x/y", False), ("$", False)])
def test_1_t3s_leading_command_rule(text, expected):
    from cohbench.scripted.script import t3_leading_command
    assert t3_leading_command(text) is expected


def test_1_a_pair_differing_only_by_what_t3_trims_collides(server):
    # To different children: refused at load, because the launch messages are equal.
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
            {"do": "delegate", "child": "a", "task": "Check it."},
            {"do": "delegate", "child": "b", "task": "Check it.\n"}]}], "final": "x"}],
            "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}, "b": {"handlers": [{"steps": [], "final": "b"}]}}})
    assert "cannot be decided" in e.value.errors[0]
    # To one child: refused at issue, the launch messages equal but the arguments not.
    server.load({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "w", "task": "Check it."}, {"do": "delegate", "child": "w", "task": "Check it.\n"}]}],
        "final": "x"}], "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    assert text_of(Convo(server, tools=TOOLS).user("go")) == FALLBACK and codes(server) == ["ambiguous_delegation"]


@pytest.mark.parametrize("inp,where", [({"role": "researcher"}, "role"), ({"mode": "async"}, "mode")])
def test_1_roles_are_t3s_and_mode_is_wait(inp, where):
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "t", "input": inp}],
                                                 "final": "x"}], "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    assert e.value.errors[0].startswith(f"$.handlers[0].steps[0].input.{where}")


def test_1_a_delegation_always_sends_the_waiting_mode(server):
    server.load(delegating("t", role=None))
    r = Convo(server, tools=TOOLS).user("go")
    assert calls_of(r)[0]["input"] == {"task": "t", "mode": "wait"}


# ----------------------------------------------- 2. T3's completion prompt


def completion_script() -> dict:
    s = delegating("Count the files.", role=None)
    s["handlers"][1] = {"expect": {"contains": "follow-up"}, "steps": [], "final": "the follow-up's own reply"}
    s["on_child_completion"] = {"text": "Thanks, noted.", "capture": [{"var": "finished", "regex": "taskId (\\S+) to"}]}
    return s


def test_2_a_completion_prompt_is_no_user_message(server):
    server.load(completion_script())
    parent = Convo(server, tools=TOOLS)
    parent.user("go")
    # The wait timed out on T3's side; the tool result says so and the handler ends.
    assert text_of(parent.results(['{"status":"running","waitTimedOut":true}'])) == "parent done"
    # Later T3 wakes the parent with its completion prompt.
    r = parent.user(completion(TASK_ID))
    line = server.app.log.lines[-1]
    assert text_of(r) == "Thanks, noted." and line["decision"] == "child_completion" and line["delegation_completion"]
    assert server.episode.state()["vars"]["finished"] == TASK_ID
    # The real follow-up gets its own handler.
    assert text_of(parent.user("here is the follow-up")) == "the follow-up's own reply"
    assert [u["preview"] for u in server.episode.state()["user_messages"]["main"]] == ["go", "here is the follow-up"]
    assert codes(server) == []


def test_2_a_completion_with_a_follow_up_in_one_request(server):
    server.load(completion_script())
    parent = Convo(server, tools=TOOLS)
    parent.user("go")
    parent.results(["ok"])
    r = parent.send([{"role": "user", "content": completion(TASK_ID, TASK_ID + "b")},
                     {"role": "user", "content": "the follow-up"}])
    assert text_of(r) == "the follow-up's own reply"
    assert len(server.episode.state()["user_messages"]["main"]) == 2


@pytest.mark.parametrize("text,expected", [(completion(TASK_ID), True), (completion("a", "b", "c"), True),
                                           (completion(TASK_ID) + " ", False), ("Delegated task x reached a terminal "
                                            "state. Use task_status with taskId y to read the result.", False)])
def test_2_the_completion_prompt_shape(text, expected):
    assert is_completion_prompt(text) is expected


# ----------------------------------------- 3. abandon with a capture that succeeds


def test_3_abandon_applies_a_capture_that_succeeds(server):
    server.load({"version": 1, "vars": {"job": "original"}, "handlers": [
        {"on_steer": "abandon", "steps": [
            {"do": "bash", "command": "./submit", "capture": [{"var": "job", "regex": "(J-\\d+)", "required": True}]},
            {"do": "bash", "command": "./status"}], "final": "x"},
        {"steps": [], "final": "job={{job}}"}]})
    c = Convo(server, tools=TOOLS)
    c.user("go")
    assert text_of(c.results(["queued J-42"], then_user="Stop.")) == "job=J-42"


# --------------------------------------------------------------- 4. minors


def test_4_export_to_import_at_the_boundary_with_the_cli(server, tmp_path, monkeypatch):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    Convo(server, tools=TOOLS).user("go")
    size = len(server.raw("GET", "/export", None, admin=True)[2])
    limit = size + len(b'{"state": }')       # exactly the export plus the import wrapper
    server.app.admin_max_body = limit
    out = tmp_path / "export.json"
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf))
    assert cli_main(["admin", "--url", f"http://{server.admin[0]}:{server.admin[1]}", "export"]) == 0
    sys.stdout.flush()
    out.write_bytes(buf.getvalue())
    monkeypatch.undo()
    assert len(out.read_bytes()) == size, "the CLI wrote the server's bytes unchanged"
    with fresh_server(tmp_path, "cp", admin_max_body=limit) as cp:
        err = io.StringIO()
        with redirect_stderr(err):
            assert cli_main(["admin", "--url", f"http://{cp.admin[0]}:{cp.admin[1]}", "import", str(out)]) == 0
    server.app.admin_max_body = limit - 1
    status, _, body = server.raw("GET", "/export", None, admin=True)
    assert status == 413 and "with the import wrapper" in json.loads(body)["error"]


def test_4_a_replacement_inherits_the_original_delegation_aliases(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "t", "tool": "my_delegate"}],
                                             "final": "x"}], "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    Convo(server, tools=TOOLS).user("hello")
    doc = server.episode.export_state()
    replacement = json.dumps({"version": 1, "handlers": [{"steps": [{"do": "tool", "name": "my_delegate", "input": {}}],
                                                          "final": "p"}]})
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc, replacement)
    assert "delegation tool" in str(e.value)


@pytest.mark.parametrize("a,b", [(1, True), (1, 1.0), (0, False)])
def test_4_delegation_arguments_compare_as_canonical_json(server, a, b):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "delegate", "child": "w", "task": "t", "input": {"timeoutMs": a}},
        {"do": "delegate", "child": "w", "task": "t", "input": {"timeoutMs": b}}], "final": "x"}],
        "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    c = Convo(server, tools=TOOLS)
    c.user("go")
    assert text_of(c.results(["ok"])) == FALLBACK and codes(server) == ["ambiguous_delegation"]


@pytest.mark.parametrize("breakage", ["text", "id", "name", "input"])
def test_4_import_checks_stored_block_shapes(server, breakage):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "x", "say": "Running."}], "final": "x"}]})
    Convo(server, tools=TOOLS).user("go")
    doc = server.episode.export_state()
    text_block, call = doc["nodes"][0]["blocks"]
    del (text_block if breakage == "text" else call)[breakage]
    with pytest.raises(ValueError):
        Episode().import_state(doc)


def test_4_a_resend_from_separate_native_tool_results_resumes(server):
    """Pi stores each tool result as its own message and merges them when it
    converts; the resend is built from that native form."""
    server.load({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "bash", "command": "a"}, {"do": "bash", "command": "b"}]}], "final": "done"},
        {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    ids = [x["id"] for x in calls_of(r)]
    c.results(["A", "B"])
    native = c.history[:2] + [
        {"role": "toolResult", "toolCallId": ids[0], "content": [{"type": "text", "text": "A"}], "isError": False},
        {"role": "toolResult", "toolCallId": ids[1], "content": [{"type": "text", "text": "B"}], "isError": False}] + c.history[3:]
    resend = pi_resend(native)
    assert [b["tool_use_id"] for b in resend[2]["content"]] == ids, "Pi merged them into one message"
    d = Convo(server, tools=TOOLS)
    d.history = resend
    assert text_of(d.user("and now?", full=True)) == "resumed"
    assert server.app.log.lines[-1]["identity"]["decision"] == "resume"
