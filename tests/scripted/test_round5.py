"""The fifth review round's findings, one test or more per finding."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest

from cohbench.scripted import App, Episode, ScriptError, load_script
from cohbench.scripted.engine import fingerprint, identity_hash

from .conftest import Convo, Server, calls_of, fixture_pairs, parse_sse, text_of

FALLBACK = "Sorry, I can't continue with this right now."
NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
DELEGATE = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "mcp__t3-code__delegate_task")]


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


@contextmanager
def fresh_server(tmp_path, name):
    app = App(Episode(), tmp_path / name, read_timeout=2.0)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    try:
        yield Server(app, api, admin)
    finally:
        app.stop()


# ------------------------------------------------------------------------ 1

TWO_MARKS = {"version": 1, "handlers": [{"steps": [
    {"do": "set", "var": "mark", "value": "A"},
    {"do": "delegate", "child": "worker", "task": "t"},
    {"do": "set", "var": "mark", "value": "B"},
    {"do": "delegate", "child": "worker", "task": "t"}], "final": "both issued"}],
    "children": {"worker": {"handlers": [{"steps": [], "final": "I got {{mark}}"}]}}}


@pytest.mark.parametrize("child_first", [True, False], ids=["child launches first", "parent continues first"])
def test_1_reusing_an_envelope_with_other_variables_fails_whenever_the_child_launches(server, child_first):
    server.load(TWO_MARKS)
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    child_reply = None
    if child_first:
        child_reply = text_of(Convo(server, tools=DELEGATE).user(f"{NOTE}\n\nt"))
    r = parent.results(['{"taskId":"1","status":"running"}'])
    assert calls_of(r) == [] and text_of(r) == FALLBACK
    assert codes(server) == ["ambiguous_delegation"]
    assert len(server.episode.state()["delegations"]) == 1
    if not child_first:
        child_reply = text_of(Convo(server, tools=DELEGATE).user(f"{NOTE}\n\nt"))
    assert child_reply == "I got A"


# ------------------------------------------------------------------------ 2

ORIGINAL = {"version": 1, "vars": {"x": "orig"}, "handlers": [
    {"steps": [{"do": "bash", "command": "a"}], "final": "original {{x}}"},
    {"steps": [], "final": "second {{x}}"}]}
REPLACEMENT = json.dumps({"version": 1, "vars": {"x": "new"}, "handlers": [{"steps": [], "final": "probe {{x}}"}]})


def test_2_an_original_handler_never_sees_replacement_variables(server, tmp_path):
    server.load(ORIGINAL)
    waiting = Convo(server)
    waiting.user("go")                                   # handler 0 waits on bash
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    with fresh_server(tmp_path, "cp") as cp:
        status, _, body = cp.raw("POST", "/import", json.dumps({"state": exported, "next_script": REPLACEMENT}).encode(),
                                 admin=True)
        assert status == 200, body
        replies = {}
        for name, srv in (("source", server), ("copy", cp)):
            other = Convo(srv).user("a second conversation")  # the replacement starts here on the copy
            w = Convo(srv)
            w.last_id, w.replies = waiting.last_id, list(waiting.replies)
            replies[name] = (text_of(other), text_of(w.results(["done"])))
        assert replies["copy"] == ("probe new", "original orig")
        assert replies["source"] == ("second orig", "original orig")


# ------------------------------------------------------------------------ 3


def test_3_a_create_one_byte_different_is_a_new_message(server):
    (first, _), _ = fixture_pairs("pending")
    server.load({"version": 1, "handlers": [{"steps": [], "final": "zero"}, {"steps": [], "final": "one"}]})
    server.post(first)
    changed = json.loads(json.dumps(first))
    changed["messages"][0]["content"][0]["text"] += " "
    status, _, events = server.post(changed)
    assert status == 200 and "".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta") == "one"
    assert server.app.log.lines[-1]["decision"] != "replay"
    assert len(server.episode.state()["user_messages"]["main"]) == 2


def test_3_a_byte_identical_create_is_still_a_retry(server):
    (first, _), _ = fixture_pairs("pending")
    server.load({"version": 1, "handlers": [{"steps": [], "final": "zero"}, {"steps": [], "final": "one"}]})
    a = server.post(first)[2]
    b = server.post(json.loads(json.dumps(first)))[2]
    assert a[0][1]["message"]["id"] == b[0][1]["message"]["id"] and server.app.log.lines[-1]["decision"] == "replay"


def test_3_a_resume_one_byte_different_is_stale_not_a_replay(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "ready"},
                                             {"steps": [], "final": "heard [{{user_message}}]"},
                                             {"steps": [], "final": "unused"}]})
    c = Convo(server)
    c.user("one")
    history = list(c.history)
    r1 = Convo(server)
    r1.history = list(history)
    assert text_of(r1.user("choose A", full=True)) == "heard [choose A]"
    r2 = Convo(server)
    r2.history = list(history)
    assert text_of(r2.user("choose A ", full=True)) == FALLBACK
    assert codes(server) == ["stale_history"]
    r3 = Convo(server)
    r3.history = list(history)
    assert text_of(r3.user("choose A", full=True)) == "heard [choose A]"   # the identical resume replays
    assert server.app.log.lines[-1]["decision"] == "replay"


# ------------------------------------------------------------------------ 4


def test_4_the_step_limit_is_a_handler_failure_and_the_correction_runs(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "a"},
                   {"do": "say", "text": "Looping now."},
                   {"do": "repeat", "max": 1000, "steps": [
                       {"do": "repeat", "max": 1000, "steps": [{"do": "set", "var": "v", "value": "x"}]}]}],
         "final": "never"},
        {"steps": [], "final": "corrected"}]})
    c = Convo(server)
    c.user("go")
    r = c.results(["a"], then_user="Stop that and do this instead.")
    assert text_of(r) == "corrected"
    st = server.episode.state()
    assert [h["status"] for h in st["handlers"]["main"]] == ["error", "done"]
    assert st["handlers"]["main"][0]["error"] == "too_many_steps"


# ------------------------------------------------------------------- minors


def test_5_cache_markers_are_stripped_only_where_the_protocol_puts_them():
    def msgs(block_marker, data_marker):
        tr = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "x",
                                                                       "cache_control": block_marker}]}
        user = {"type": "text", "text": "hi", "data": {"cache_control": data_marker}, "cache_control": block_marker}
        return [{"role": "user", "content": [tr, user]}]
    # A marker on a block, or on a block inside a tool result, does not count.
    assert fingerprint(msgs({"type": "ephemeral"}, 1)) == fingerprint(msgs(None, 1))
    # A key of the same name inside other data does.
    assert fingerprint(msgs(None, 1)) != fingerprint(msgs(None, 2))
    tools_a = [{"name": "t", "input_schema": {"properties": {"cache_control": {"type": "string"}}}, "cache_control": {"x": 1}}]
    tools_b = [{"name": "t", "input_schema": {"properties": {"cache_control": {"type": "string"}}}}]
    tools_c = [{"name": "t", "input_schema": {"properties": {"other": {"type": "string"}}}}]
    assert identity_hash("m", None, tools_a) == identity_hash("m", None, tools_b)
    assert identity_hash("m", None, tools_b) != identity_hash("m", None, tools_c)


def exported_with_delegation(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "t"}], "final": "x"},
                                             {"steps": [], "final": "y"}],
                 "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    Convo(server, tools=DELEGATE).user("go")
    Convo(server, tools=DELEGATE).user("another")
    server.raw("POST", "/next", b'{"index": 2, "text": null}', admin=True)
    return server.episode.export_state()


def test_6_restore_requires_every_announcement_key(server):
    doc = exported_with_delegation(server)
    assert doc["announcements"][0]["text"] is None
    Episode().import_state(json.loads(json.dumps(doc)))       # a null text is a value
    del doc["announcements"][0]["text"]
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "has no 'text'" in str(e.value)


def test_6_restore_checks_the_parent_conversation(server):
    doc = exported_with_delegation(server)
    doc["delegations"][0]["parent_conversation"] = "c2"       # exists, but did not issue it
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "not the conversation of the reply" in str(e.value)


@pytest.mark.parametrize("value,cond,expected", [("1e401", "gt", True), ("1e399", "lt", True), ("1e400", "eq", True)])
def test_7_a_literal_a_decimal_holds_is_accepted(server, value, cond, expected):
    text = ('{"version": 1, "vars": {"x": "' + value + '"}, "handlers": [{"steps": [{"do": "branch", "cases": ['
            '{"when": {"var": {"name": "x", "' + cond + '": 1e400}}, "steps": [{"do": "reply", "text": "yes"}]}],'
            ' "else": [{"do": "reply", "text": "no"}]}]}]}')
    assert server.raw("POST", "/script", text.encode(), admin=True)[0] == 200
    assert text_of(Convo(server).user("go")) == ("yes" if expected else "no")
    with pytest.raises(ScriptError):
        load_script(text.replace("1e400", "1e99999999999999999999"))


ID_RE = re.compile(r"(msg|toolu|req)_01[1-9A-HJ-NP-Za-km-z]{22}")


def run_sequence(srv) -> str:
    """The same script and requests; the streams with ids masked."""
    srv.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "a long command whose input spans several deltas", "say": "Running it now."}],
         "final": "The command finished, and this reply is long enough to be split into several pieces."},
        {"steps": [], "final": "short"}]})
    out = []
    c = Convo(srv)
    first = c.body([{"role": "user", "content": "go"}])
    status, _, data = srv.raw("POST", "/v1/messages", json.dumps(first).encode())
    out.append(data.decode())
    ev = parse_sse(data.decode())
    msg_id = ev[0][1]["message"]["id"]
    tid = next(d["content_block"]["id"] for n, d in ev if n == "content_block_start" and d["content_block"]["type"] == "tool_use")
    second = {"model": c.model, "max_tokens": 10, "stream": True, "thread": {"type": "continue", "previous_message_id": msg_id},
              "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}]}
    status, _, data = srv.raw("POST", "/v1/messages", json.dumps(second).encode())
    out.append(data.decode())
    return ID_RE.sub("<id>", "\n=====\n".join(out))


def test_8_two_fresh_servers_give_the_same_stream_apart_from_ids(tmp_path):
    with fresh_server(tmp_path, "a") as a, fresh_server(tmp_path, "b") as b:
        sa, sb = run_sequence(a), run_sequence(b)
    assert sa == sb
    assert sa.count("content_block_delta") > 10, "the replies were split into pieces"


def child_streams(srv, order) -> dict:
    srv.load({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "w", "task": "first task"}, {"do": "delegate", "child": "w", "task": "second task"}]}],
        "final": "x"}], "children": {"w": {"handlers": [
            {"steps": [], "final": "a reply long enough to be cut into several pieces: {{user_message}}"}]}}})
    Convo(srv, tools=DELEGATE).user("go")
    out = {}
    for i in order:
        task = ["first task", "second task"][i]
        body = Convo(srv, tools=DELEGATE).body([{"role": "user", "content": f"{NOTE}\n\n{task}"}])
        out[task] = ID_RE.sub("<id>", srv.raw("POST", "/v1/messages", json.dumps(body).encode())[2].decode())
    return out


def test_8_a_childs_stream_does_not_depend_on_launch_order(tmp_path):
    with fresh_server(tmp_path, "a") as a, fresh_server(tmp_path, "b") as b:
        assert child_streams(a, [0, 1]) == child_streams(b, [1, 0])
