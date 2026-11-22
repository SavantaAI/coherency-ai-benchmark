"""The third review round's findings, one test or more per finding id."""

from __future__ import annotations

import io
import itertools
import json
import socket
import threading
import time
from contextlib import redirect_stderr

import pytest

from cohbench.scripted import App, Episode, ScriptError, load_script
from cohbench.scripted.__main__ import main as cli_main
from cohbench.scripted.wire import parse_request

from .conftest import Convo, Server, calls_of, text_of

FALLBACK = "Sorry, I can't continue with this right now."
NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
DELEGATE = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "mcp__t3-code__delegate_task")]


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def launch(task: str, role: str | None = "research") -> str:
    prompt = task if role in (None, "general") else f"Act as the {role} sub-agent for this task.\n\n{task}"
    return f"{NOTE}\n\n{prompt}"


def handle(server, convo: Convo, new: list):
    """Decide a request in process, without sending it (so no send attempt is reported)."""
    out = server.episode.handle(parse_request(convo.body(new)))
    msg = {"id": out.msg_id, "content": out.blocks}
    convo.history += new + [{"role": "assistant", "content": out.blocks}]
    convo.last_id = out.msg_id
    convo.replies.append(msg)
    return out


# ------------------------------------------------------------------ B1, 1.1


def test_b1_a_continue_retry_is_keyed_by_its_own_message(server):
    """Two conversations with identical histories (an announced second
    opening), then the same follow-up in each: the second is not a replay of
    the first."""
    server.load({"version": 1, "handlers": [{"steps": [], "final": "ready"}, {"steps": [], "final": "ready"},
                                             {"steps": [], "final": "reply two"}, {"steps": [], "final": "reply three"}]})
    c1, c2 = Convo(server), Convo(server)
    c1.user("x")
    server.raw("POST", "/next", b'{"index": 1, "text": "x"}', admin=True)
    c2.user("x")
    server.raw("POST", "/next", b'{"index": 2, "text": "next"}', admin=True)
    assert text_of(c1.user("next")) == "reply two"
    server.raw("POST", "/next", b'{"index": 3, "text": "next"}', admin=True)
    r = c2.user("next")
    line = server.app.log.lines[-1]
    assert text_of(r) == "reply three" and line["decision"] != "replay" and line["conversation"] == "c2"
    st = server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0, 1, 2, 3]
    assert [a["outcome"] for a in st["announcements"]] == ["arrived"] * 3 and codes(server) == []


def test_b1_children_with_equal_histories_continue_independently(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "delegate", "child": "w", "task": "same task"}, {"do": "delegate", "child": "w", "task": "same task"}],
        "final": "done"}], "children": {"w": {"handlers": [{"steps": [], "final": "same"},
                                                            {"steps": [], "final": "{{user_message}} handled"}]}}})
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    a = Convo(server, tools=DELEGATE)
    a.user(launch("same task", None))
    parent.results(["ok"])
    b = Convo(server, tools=DELEGATE)
    b.user(launch("same task", None))
    assert text_of(a.user("more")) == "more handled"
    r = b.user("more")
    assert text_of(r) == "more handled" and server.app.log.lines[-1]["decision"] != "replay"
    assert server.app.log.lines[-1]["conversation"] == "c3"


def test_b1_rule_a_refusals_carry_identity(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    status, _, _ = server.post({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "y"}],
                                "thread": {"type": "continue", "previous_message_id": "msg_01nothing"}})
    assert status == 400
    line = server.app.log.lines[-1]
    assert line["identity"]["rule"] == "a" and line["identity"]["decision"] == "refused"
    assert "thread_not_found" in line["identity"]["reason"]


# ----------------------------------------------------------------------- B2


def delegating(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "check"}],
                                             "final": "done"}],
                 "children": {"w": {"handlers": [{"steps": [], "final": "child"}]}}})
    parent = Convo(server, tools=DELEGATE)
    return handle(server, parent, [{"role": "user", "content": "go"}]).msg_id


def launch_kind(server) -> str:
    out = server.episode.handle(parse_request(Convo(server, tools=DELEGATE).body(
        [{"role": "user", "content": launch("check", None)}])))
    return out.log["conversation_kind"]


@pytest.mark.parametrize("first_ok", [False, True], ids=["failed then retry in flight", "delivered then retry"])
def test_b2_a_launch_between_send_callbacks_is_a_child(server, first_ok):
    mid = delegating(server)
    server.episode.reply_attempt(mid)
    server.episode.reply_sent(mid, first_ok)
    server.episode.reply_attempt(mid)          # the retry has started writing
    assert launch_kind(server) == "child"      # the launch arrives between the two callbacks
    server.episode.reply_sent(mid, not first_ok)
    assert server.episode.state()["delegations"][0]["status"] == "claimed"


def test_b2_every_attempt_failed_and_none_in_flight_is_not_claimable(server):
    mid = delegating(server)
    server.episode.reply_attempt(mid)
    server.episode.reply_sent(mid, False)
    assert launch_kind(server) == "main"
    assert server.episode.state()["delegations"][0]["status"] == "undelivered"


# ----------------------------------------------------------------------- B3


def scoped_script():
    return {"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "worker", "task": "find marker A",
         "capture": [{"var": "got", "regex": "saw (\\w+)"}]},
        {"do": "delegate", "child": "worker", "task": "find marker B",
         "capture": [{"var": "got", "regex": "saw (\\w+)"}]}]}],
        "final": "parent got {{got}}"}],
        "children": {"worker": {"handlers": [{
            "capture": [{"var": "seen", "regex": "marker (\\w+)"}],
            "steps": [{"do": "bash", "command": "echo {{seen}}"}], "final": "saw {{seen}}"}]}}}


def run_scoped(server, launch_order, result_order, parent_order):
    server.load(scoped_script())
    parent = Convo(server, tools=DELEGATE)
    r = parent.user("go")
    tasks = ["find marker A", "find marker B"]
    children = {}
    out = {}
    for i in launch_order:
        c = Convo(server, tools=DELEGATE)
        rr = c.user(launch(tasks[i], None))
        children[i] = c
        out[f"args {i}"] = calls_of(rr)[0]["input"]
    for i in result_order:
        out[f"final {i}"] = text_of(children[i].results(["printed"]))
    calls = calls_of(r)
    results = [{"type": "tool_result", "tool_use_id": calls[i]["id"], "content": f'{{"summary":"saw {"AB"[i]}"}}'}
               for i in parent_order]
    out["parent"] = text_of(parent.send([{"role": "user", "content": results}]))
    out["parent vars"] = server.episode.state()["vars"]
    return out


def test_b3_each_child_has_its_own_variables_whatever_the_order(server):
    seen = set()
    for launch_order, result_order, parent_order in itertools.product(
            itertools.permutations([0, 1]), itertools.permutations([0, 1]), itertools.permutations([0, 1])):
        seen.add(json.dumps(run_scoped(server, launch_order, result_order, parent_order), sort_keys=True))
    assert len(seen) == 1, seen
    out = json.loads(seen.pop())
    assert out["args 0"]["command"] == "echo A" and out["args 1"]["command"] == "echo B"
    assert out["final 0"] == "saw A" and out["final 1"] == "saw B"
    # The parent learns only through its own captures, in tool-call order: B is the later call.
    assert out["parent"] == "parent got B" and "seen" not in out["parent vars"]


# ------------------------------------------------------------------ B4, 1.4


def test_b4_omitted_role_and_general_are_the_same_envelope():
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
            {"do": "delegate", "child": "a", "task": "t"},
            {"do": "delegate", "child": "b", "task": "t", "input": {"role": "general"}}]}], "final": "x"}],
            "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}, "b": {"handlers": [{"steps": [], "final": "b"}]}}})
    assert "cannot be decided" in e.value.errors[0]


@pytest.mark.parametrize("bad", [object(), {1: "x"}, b"bytes", (1, 2)], ids=["object", "int key", "bytes", "tuple"])
def test_14_a_non_json_value_is_a_script_error(bad):
    for where in ("task", "role"):
        call = {"do": "delegate", "child": "a", "task": "t", "input": {}}
        if where == "task":
            call["task"] = bad
        else:
            call["input"]["role"] = bad
        with pytest.raises(ScriptError):
            load_script({"version": 1, "handlers": [{"steps": [call], "final": "x"}],
                         "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}}})


# ------------------------------------------------------------------ B5, 1.2


def test_b5_literals_keep_their_written_precision(server):
    text = ('{"version": 1, "vars": {"x": "0.30000000000000001"}, "handlers": [{"steps": [{"do": "branch", "cases": ['
            '{"when": {"var": {"name": "x", "eq": 0.30000000000000001}}, "steps": [{"do": "reply", "text": "equal"}]}],'
            ' "else": [{"do": "reply", "text": "unequal"}]}]}]}')
    status, _, body = server.raw("POST", "/script", text.encode(), admin=True)
    assert status == 200, body
    assert text_of(Convo(server).user("go")) == "equal"
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    assert "0.30000000000000001" in exported["script"]
    copy_app = App(Episode(), None)
    copy_app.episode.import_state(exported)
    assert copy_app.episode.script.source == text
    assert "0.30000000000000001" in server.raw("GET", "/script", None, admin=True)[2].decode()


@pytest.mark.parametrize("value", ["1e9999999999999999999", "1e99999999999999999999", "-1e-99999999999999999999"])
def test_12_numbers_out_of_range_are_a_logged_fallback(server, value):
    server.load({"version": 1, "vars": {"x": value}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": {"var": {"name": "x", "lt": 5}}, "steps": []}]}], "final": "x"}]})
    status, _, events = server.post(Convo(server).body([{"role": "user", "content": "go"}]))
    assert status == 200 and codes(server) == ["not_a_number"]


# ------------------------------------------------------------------ B6, 1.3


def test_b6_a_failed_handlers_narration_and_delay_are_dropped(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "a", "capture": [{"var": "v", "regex": "never(x)"}]},
                   {"do": "say", "text": "Old narration", "delay": 60},
                   {"do": "bash", "command": "use {{v}}"}], "final": "x"},
        {"steps": [], "final": "corrected"}]})
    c = Convo(server)
    handle(server, c, [{"role": "user", "content": "go"}])
    call = calls_of(c.replies[-1])[0]["id"]
    out = handle(server, c, [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": "a"}]},
                             {"role": "user", "content": "use the other one"}])
    assert [b["text"] for b in out.blocks] == ["corrected"] and out.delay == 0
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["error", "done"]


def test_b6_a_completed_handlers_narration_is_kept(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "a", "capture": [{"var": "nope", "regex": "never(x)"}]}],
         "final": "first done", "final_delay": 0.1},
        {"steps": [{"do": "say", "text": "Doomed"}, {"do": "bash", "command": "{{nope}}"}], "final": "x"},
        {"steps": [], "final": "third"}]})
    c = Convo(server)
    handle(server, c, [{"role": "user", "content": "go"}])
    call = calls_of(c.replies[-1])[0]["id"]
    out = handle(server, c, [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": "a"}]},
                             {"role": "user", "content": "two"}, {"role": "user", "content": "three"}])
    assert [b["text"] for b in out.blocks] == ["first done\n\nthird"] and out.delay == pytest.approx(0.1)


# ----------------------------------------------------------------------- B7


def test_b7_a_conversation_running_at_export_keeps_its_script(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "work"}], "final": "p"},
                                             {"steps": [], "final": "old second"}],
                 "children": {"w": {"handlers": [{"steps": [{"do": "bash", "command": "step one"},
                                                            {"do": "bash", "command": "step two"}],
                                                  "final": "original child final"}]}}})
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    child = Convo(server, tools=DELEGATE)
    child.user(launch("work", None))
    parent.results(["ok"])                       # the parent is finished; the child waits for a tool result
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    copy_app = App(Episode(), None)
    api, admin = copy_app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    try:
        cp = Server(copy_app, api, admin)
        replacement = {"version": 1, "handlers": [{"steps": [], "final": "probe answer"}],
                       "children": {"w": {"handlers": [{"steps": [], "final": "replacement child final"}]}}}
        status, _, body = cp.raw("POST", "/import", json.dumps({"state": exported, "next_script": json.dumps(replacement)}).encode(),
                                 admin=True)
        assert status == 200, body
        child.srv = cp
        r = child.results(["one"])
        assert calls_of(r)[0]["input"]["command"] == "step two"
        assert text_of(child.results(["two"])) == "original child final"
        parent.srv = cp
        assert text_of(parent.user("question after import")) == "probe answer"
    finally:
        copy_app.stop()


# ------------------------------------------------------------------ B8, 1.6


def test_b8_an_older_snapshot_cannot_replace_a_newer_one(server, tmp_path):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    captured, release = threading.Event(), threading.Event()

    def slow_capture():
        snapshot = server.episode.state()   # an older snapshot ...
        captured.set()
        release.wait(5)                     # ... held while /next runs
        return snapshot

    older = threading.Thread(target=server.app.log.state, args=(slow_capture,))
    older.start()
    captured.wait(5)
    result = {}
    nxt = threading.Thread(target=lambda: result.update(r=server.raw("POST", "/next", b'{"index": 0}', admin=True)))
    nxt.start()
    time.sleep(0.3)
    release.set()
    older.join(5)
    nxt.join(5)
    assert result["r"][0] == 200
    st = json.loads((tmp_path / "log" / "state.json").read_text())
    assert st["announcements"] and st["announcements"][0]["outcome"] == "pending"


# ------------------------------------------------------------------- minors


def test_import_freshness_is_the_servers_lifetime(server):
    exported = Episode().export_state()
    # count_tokens is enough to end freshness.
    status, _, _ = server.post({"model": "m", "messages": [{"role": "user", "content": "x"}]},
                               path="/v1/messages/count_tokens")
    assert status == 200
    assert server.raw("POST", "/import", json.dumps({"state": exported}).encode(), admin=True)[0] == 409
    # And /reset does not make a server fresh again.
    server.raw("POST", "/reset", b"", admin=True)
    assert server.raw("POST", "/import", json.dumps({"state": exported}).encode(), admin=True)[0] == 409


@pytest.mark.parametrize("field,value", [("next_offset", True), ("announced", False), ("seq", -1), ("vars", {"a": 1})])
def test_restore_checks_types(field, value):
    doc = Episode().export_state()
    doc[field] = value
    with pytest.raises(ValueError):
        Episode().import_state(doc)


def test_restore_checks_the_active_handler(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "x"}], "final": "x"}]})
    Convo(server).user("go")
    doc = server.episode.export_state()
    doc["nodes"][0]["state"]["active"] = 7
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "is not a handler" in str(e.value)


def test_very_long_digit_strings_are_refused_not_crashed(server):
    host, port = server.api
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: k\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n")
    data = s.recv(65536)
    s.close()
    assert data.startswith(b"HTTP/1.1 411") or data.startswith(b"HTTP/1.1 431") or data.startswith(b"HTTP/1.1 400"), data[:60]
    err = io.StringIO()
    with redirect_stderr(err):
        assert cli_main(["admin", "--url", "http://127.0.0.1:1", "next", "9" * 5000]) == 2


def test_log_fields_describe_the_reply_sent(server):
    server.load({"version": 1, "handlers": [
        {"end": "question", "steps": [{"do": "bash", "command": "a"}], "final": "Which one?"},
        {"steps": [{"do": "bash", "command": "b"}], "final": "x"}]})
    c = Convo(server)
    c.user("go")
    r = c.results(["a"], then_user="the second")
    line = server.app.log.lines[-1]
    assert calls_of(r) and line["decision"] == "tool_use" and line["handler"] == 1 and "ends_with" not in line
    assert line["finished"] == [{"handler": 0, "step": "$.handlers[0].final", "ends_with": "question"}]
    assert line["steer"] is True


def test_steer_is_logged_when_the_running_handler_fails(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "a", "capture": [{"var": "v", "regex": "J", "required": True}]}], "final": "x"},
        {"steps": [], "final": "corrected"}]})
    c = Convo(server)
    c.user("go")
    assert text_of(c.results(["no"], then_user="fix")) == "corrected"
    assert server.app.log.lines[-1]["steer"] is True


def test_a_fallback_reply_logs_no_handler(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "bash", "command": "a", "capture": [{"var": "v", "regex": "never(x)"}]},
        {"do": "bash", "command": "{{v}}"}], "final": "x"}]})
    c = Convo(server)
    c.user("go")
    assert text_of(c.results(["a"])) == FALLBACK
    line = server.app.log.lines[-1]
    assert line["decision"] == "fallback" and "handler" not in line and "step" not in line


@pytest.mark.parametrize("args", [["load", "/nonexistent/script.json"], ["import", "/nonexistent/export.json"],
                                  ["load", "BADJSON"], ["import", "BADJSON"]])
def test_admin_cli_exits_2_on_a_missing_file_or_bad_json(tmp_path, args):
    if args[1] == "BADJSON":
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        args = [args[0], str(p)]
    err = io.StringIO()
    with redirect_stderr(err):
        assert cli_main(["admin", "--url", "http://127.0.0.1:1", *args]) == 2
    assert err.getvalue().strip()
