"""Export and import of the whole episode, for a probe copy's server."""

from __future__ import annotations

import copy
import json

import pytest

from cohbench.scripted import App, Episode

from .conftest import Convo, Server, calls_of, text_of

NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
TOOLS_NAMES = ("bash", "mcp__t3-code__delegate_task")

SCRIPT = {"version": 1, "handlers": [
    {"steps": [
        {"do": "bash", "command": "./submit", "capture": [{"var": "job", "regex": "(J-\\d+)", "required": True}]},
        {"do": "delegate", "child": "checker", "task": "check {{job}}", "input": {"role": "research"},
         "capture": [{"var": "verdict", "regex": "\"summary\":\"(\\w+)\""}]}],
     "final": "Job {{job}}: {{verdict}}"},
    {"capture": [{"var": "q", "regex": "(\\w+)\\?$"}],
     "steps": [{"do": "bash", "command": "./status {{job}}"}], "final": "{{job}} {{last_output}} {{q}}"},
], "children": {"checker": {"handlers": [{"steps": [{"do": "bash", "command": "./check {{job}}"}], "final": "ok"}]}}}


@pytest.fixture
def copy_server(tmp_path):
    app = App(Episode(), tmp_path / "copy-log", read_timeout=2.0)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    yield Server(app, api, admin)
    app.stop()


def tools():
    return [{"name": n, "input_schema": {"type": "object"}} for n in TOOLS_NAMES]


def mid_episode(server) -> Convo:
    """Tool results, captures and one completed delegation, then a turn boundary."""
    server.load(SCRIPT)
    parent = Convo(server, tools=tools())
    parent.user("submit the job")
    r = parent.results(["queued as J-7"])
    assert calls_of(r)[0]["input"]["task"] == "check J-7"
    child = Convo(server, tools=tools())
    child.user(f"{NOTE}\n\nAct as the research sub-agent for this task.\n\ncheck J-7")
    assert text_of(child.results(["fine"])) == "ok"
    assert text_of(parent.results(['{"status":"completed","summary":"ok"}'])) == "Job J-7: ok"
    return parent


def probe_body(parent: Convo, question: str) -> dict:
    """What the copy's Pi sends: the whole conversation again, as a create."""
    return {"model": parent.model, "max_tokens": 128000, "stream": True, "thinking": {"type": "disabled"},
            "thread": {"type": "create"}, "system": parent.system, "tools": parent.tools,
            "messages": copy.deepcopy(parent.history) + [{"role": "user", "content": [{"type": "text", "text": question}]}]}


def decision(server, status, events) -> dict:
    assert status == 200
    line = server.app.log.lines[-1]
    blocks = [d["content_block"] for n, d in events if n == "content_block_start"]
    text = "".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta")
    inputs = "".join(d["delta"].get("partial_json", "") for n, d in events if n == "content_block_delta")
    return {"identity": line["identity"], "handler": line.get("handler"), "step": line.get("step"),
            "decision": line.get("decision"), "users": [u["index"] for u in line["user_messages"]],
            "errors": line["errors"], "blocks": [(b["type"], b.get("name")) for b in blocks],
            "text": text, "inputs": inputs, "vars": server.episode.state()["vars"]}


def post(server, body):
    return server.post(body)


def test_an_import_gives_the_decision_the_original_would_give(server, copy_server):
    parent = mid_episode(server)
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    status, _, body = copy_server.raw("POST", "/import", json.dumps({"state": exported}).encode(), admin=True)
    assert status == 200, body
    probe = probe_body(parent, "where is it?")
    original = decision(server, *post(server, probe)[:3:2])
    on_copy = decision(copy_server, *post(copy_server, probe)[:3:2])
    assert original == on_copy
    assert original["identity"]["rule"] == "c" and original["handler"] == 1 and original["users"] == [1]
    assert original["inputs"] == '{"command": "./status J-7", "timeout": null}'
    # And the turn finishes the same way on both.
    for srv in (server, copy_server):
        c = Convo(srv, tools=tools())
        last = next(x for x in reversed(srv.app.log.lines) if "seq" in x)
        c.last_id = last["message_id"]
        c.replies.append({"content": [{"type": "tool_use", "id": last["tool_calls"][0]["id"], "name": "bash"}]})
        assert text_of(c.results(["running"])) == "J-7 running it"


def test_export_import_export_is_the_same_document(server, copy_server):
    mid_episode(server)
    first = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    copy_server.raw("POST", "/import", json.dumps({"state": first}).encode(), admin=True)
    second = json.loads(copy_server.raw("GET", "/export", None, admin=True)[2])
    assert first == second


def test_the_main_server_is_unaffected_by_the_copy(server, copy_server):
    parent = mid_episode(server)
    before = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    copy_server.raw("POST", "/import", json.dumps({"state": before}).encode(), admin=True)
    post(copy_server, probe_body(parent, "where is it?"))
    assert json.loads(server.raw("GET", "/export", None, admin=True)[2]) == before


def test_an_import_with_a_replacement_script_answers_the_next_message(server, copy_server):
    parent = mid_episode(server)
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    probe_script = {"version": 1, "fallback": "No.", "handlers": [{"steps": [], "final": "probe: {{job}} {{verdict}}"}]}
    status, _, body = copy_server.raw("POST", "/import", json.dumps(
        {"state": exported, "next_script": json.dumps(probe_script)}).encode(), admin=True)
    assert status == 200 and json.loads(body)["imported"]["next_offset"] == 1
    status, _, events = post(copy_server, probe_body(parent, "Has the user been shown J-7?"))
    d = decision(copy_server, status, events)
    assert d["text"] == "probe: J-7 ok" and d["handler"] == 1 and d["errors"] == []
    # The replacement covers the next message only; a second probe question is past its end.
    st = copy_server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0, 1]


def test_an_import_is_refused_after_a_request(server, copy_server):
    mid_episode(server)
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    copy_server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    Convo(copy_server).user("hello")
    status, _, body = copy_server.raw("POST", "/import", json.dumps({"state": exported}).encode(), admin=True)
    assert status == 409 and "answered a request in its lifetime" in json.loads(body)["error"]
    assert copy_server.episode.state()["requests"] == 1


@pytest.mark.parametrize("breakage", ["missing reply", "bad format", "bad replacement", "not an object"])
def test_a_broken_import_changes_nothing(server, copy_server, breakage):
    mid_episode(server)
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    body = {"state": exported}
    if breakage == "missing reply":
        exported["nodes"] = exported["nodes"][1:]
    elif breakage == "bad format":
        exported["format"] = "something else"
    elif breakage == "bad replacement":
        body["next_script"] = json.dumps({"version": 1, "handlers": [{"steps": []}]})
    else:
        body = {"state": [1, 2]}
    status, _, resp = copy_server.raw("POST", "/import", json.dumps(body).encode(), admin=True)
    assert status == 400, resp
    if breakage == "bad replacement":
        assert "$next.handlers[0]" in json.loads(resp)["error"]
    st = copy_server.episode.state()
    assert st["requests"] == 0 and st["script"] is None and st["conversations"] == {}
