"""The seventh review round's findings."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest

from cohbench.scripted import App, Episode, ScriptError, load_script
from cohbench.scripted.wire import parse_request

from .conftest import Convo, Server, calls_of, text_of
from .test_round6 import pi_resend

FALLBACK = "Sorry, I can't continue with this right now."
NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
TOOLS = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "mcp__t3-code__delegate_task")]
ID_RE = re.compile(r"(msg|toolu|req)_01[1-9A-HJ-NP-Za-km-z]{22}")


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


@contextmanager
def fresh_server(tmp_path, name, **kw):
    app = App(Episode(), tmp_path / name, read_timeout=5.0, **kw)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    try:
        yield Server(app, api, admin)
    finally:
        app.stop()


# ------------------------------------------------------------- 1. whitespace

BLANK_IN_JS = ["﻿", " ", " ", "　 \t"]
NOT_BLANK_IN_JS = ["​", "\x85", "\x1c"]       # Python's strip() would remove the last two


@pytest.mark.parametrize("ch", BLANK_IN_JS, ids=["FEFF", "00A0", "2028", "3000"])
def test_1_text_blank_to_ecmascript_is_a_blank_reply(server, ch):
    server.load({"version": 1, "vars": {"x": ch}, "handlers": [{"steps": [], "final": "{{x}}"},
                                                              {"steps": [], "final": "next"}]})
    c = Convo(server, tools=TOOLS)
    assert text_of(c.user("go")) == FALLBACK and codes(server) == ["blank_reply"]


@pytest.mark.parametrize("ch", NOT_BLANK_IN_JS, ids=["200B", "0085", "001C"])
def test_1_text_not_blank_to_ecmascript_is_sent_and_the_resend_resumes(server, ch):
    server.load({"version": 1, "vars": {"x": ch}, "handlers": [{"steps": [], "final": "{{x}}"},
                                                              {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    assert text_of(c.user("go")) == ch and codes(server) == []
    d = Convo(server, tools=TOOLS)
    d.history = pi_resend(c.history)
    assert d.history[-1]["content"][0]["text"] == ch, "Pi keeps it"
    assert text_of(d.user("and now?", full=True)) == "resumed"
    assert server.app.log.lines[-1]["identity"]["decision"] == "resume"


def test_1_narration_and_incoming_text_use_ecmascript_blankness(server):
    server.load({"version": 1, "vars": {"x": "﻿ "}, "handlers": [
        {"steps": [{"do": "say", "text": "{{x}}"}, {"do": "bash", "command": "a"}], "final": "done"},
        {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    assert [b["type"] for b in r["content"]] == ["tool_use"]
    c.results(["ok"])
    # A user message of U+FEFF alone is blank to Pi: not a message; U+200B is one.
    c2 = Convo(server, tools=TOOLS)
    c2.history = pi_resend(c.history)
    c2.send([{"role": "user", "content": [{"type": "text", "text": "﻿"}, {"type": "text", "text": "and now?"}]}],
            full=True)
    assert [u["preview"] for u in server.episode.state()["user_messages"]["main"]] == ["go", "and now?"]


def test_1_a_fallback_blank_to_ecmascript_is_refused_at_load():
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "fallback": "﻿", "handlers": [{"steps": [], "final": "x"}]})
    assert e.value.errors[0].startswith("$.fallback")
    load_script({"version": 1, "fallback": "​", "handlers": [{"steps": [], "final": "x"}]})


# ------------------------------------------------- 2. the delegation tool


@pytest.mark.parametrize("where", ["main", "child"])
def test_2_the_delegation_tool_is_reserved_for_delegate(where):
    call = {"do": "tool", "name": "mcp__t3-code__delegate_task", "input": {"task": "t"}}
    if where == "main":
        script = {"version": 1, "handlers": [{"steps": [call], "final": "x"}]}
        path = "$.handlers[0].steps[0].name"
    else:
        script = {"version": 1, "handlers": [{"steps": [], "final": "x"}],
                  "children": {"a": {"handlers": [{"steps": [{"do": "parallel", "calls": [call]}], "final": "x"}]}}}
        path = "$.children.a.handlers[0].steps[0].calls[0].name"
    with pytest.raises(ScriptError) as e:
        load_script(script)
    assert any(err.startswith(path) and "delegation tool" in err for err in e.value.errors), e.value.errors


def test_2_an_alias_of_the_delegation_tool_is_reserved_too():
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": [
            {"do": "delegate", "child": "a", "task": "t", "tool": "my_delegate"},
            {"do": "tool", "name": "my_delegate", "input": {}}], "final": "x"}],
            "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}}})
    assert e.value.errors[0].startswith("$.handlers[0].steps[1].name")


# ----------------------------------------- 3. the fallback keeps what completed


def test_3_the_fallback_keeps_a_completed_handlers_text_and_delay(server):
    server.load({"version": 1, "vars": {"blank": " "}, "handlers": [
        {"steps": [{"do": "bash", "command": "a"}], "final": "original done", "final_delay": 2},
        {"steps": [{"do": "say", "text": "doomed narration", "delay": 7}], "final": "{{blank}}"}]})
    c = Convo(server, tools=TOOLS)
    out = server.episode.handle(parse_request(c.body([{"role": "user", "content": "go"}])))
    call = out.blocks[0]["id"]
    c.last_id = out.msg_id
    out = server.episode.handle(parse_request(c.body([
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": "a"}]},
        {"role": "user", "content": "correction"}])))
    assert [b["text"] for b in out.blocks] == [f"original done\n\n{FALLBACK}"] and out.delay == 2
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["done", "error"]


# ------------------------------------------------------- 4. admin body size


def exported(server, description="d") -> dict:
    server.load({"version": 1, "description": description, "handlers": [{"steps": [], "final": "x"}]})
    Convo(server, tools=TOOLS).user("go")
    return json.loads(server.raw("GET", "/export", None, admin=True)[2])


def test_4_import_at_the_admin_limit_boundary(server, tmp_path):
    body = json.dumps({"state": exported(server)}).encode()
    limit = len(body) + 1000
    at_limit = body + b" " * (limit - len(body))
    with fresh_server(tmp_path, "a", admin_max_body=limit) as cp:
        assert cp.raw("POST", "/import", at_limit, admin=True)[0] == 200
    with fresh_server(tmp_path, "b", admin_max_body=limit) as cp:
        status, _, resp = cp.raw("POST", "/import", at_limit + b" ", admin=True)
        assert status == 413 and cp.episode.state()["script"] is None


def test_4_export_never_produces_what_import_refuses(tmp_path):
    with fresh_server(tmp_path, "s", admin_max_body=2000) as src:
        exported_body = src.raw("GET", "/export", None, admin=True)
        assert exported_body[0] == 200
        src.load({"version": 1, "description": "x" * 5000, "handlers": [{"steps": [], "final": "x"}]})
        status, _, resp = src.raw("GET", "/export", None, admin=True)
        assert status == 413 and "more than the 2000 bytes" in json.loads(resp)["error"]


def test_4_an_export_larger_than_the_model_limit_imports(server, tmp_path):
    """The reviewer's case: a large script whose export is above the model
    listener's 32 MiB limit (quotes and newlines double in the export)."""
    description = '"\n' * (7 * 1024 * 1024)          # 28 MiB of script text: under the 32 MiB script limit
    text = json.dumps({"version": 1, "description": description, "handlers": [{"steps": [], "final": "x"}]})
    assert server.raw("POST", "/script", text.encode(), admin=True)[0] == 200
    Convo(server, tools=TOOLS).user("go")
    status, _, data = server.raw("GET", "/export", None, admin=True)
    assert status == 200 and len(data) > 32 * 1024 * 1024
    body = b'{"state": ' + data + b"}"
    with fresh_server(tmp_path, "cp") as cp:
        status, _, resp = cp.raw("POST", "/import", body, admin=True)
        assert status == 200, resp[:200]
        assert cp.episode.script.source == text


# ----------------------------------------------- 5. delegation identity


def two_calls(target_a: str, target_b: str, parallel: bool) -> dict:
    calls = [{"do": "delegate", "child": "w", "task": "check", "input": {"target": {"model": target_a}}},
             {"do": "delegate", "child": "w", "task": "check", "input": {"target": {"model": target_b}}}]
    steps = [{"do": "parallel", "calls": calls}] if parallel else calls
    return {"version": 1, "handlers": [{"steps": steps, "final": "issued"}],
            "children": {"w": {"handlers": [{"steps": [], "final": "a child reply long enough to be cut into pieces"},
                                            {"steps": [], "final": "and a follow-up reply that is also cut up"}]}}}


@pytest.mark.parametrize("parallel", [True, False], ids=["one parallel step", "two steps"])
def test_5_same_envelope_different_target_is_refused_at_issue(server, parallel):
    server.load(two_calls("claude-haiku-5-5", "claude-opus-5-5", parallel))
    parent = Convo(server, tools=TOOLS)
    r = parent.user("go")
    if not parallel:
        assert calls_of(r)
        r = parent.results(["ok"])
    assert text_of(r) == FALLBACK and codes(server) == ["ambiguous_delegation"]


def mask_usage(stream: str) -> str:
    return re.sub(r'"(input_tokens|cache_creation_input_tokens|cache_read_input_tokens|ephemeral_5m_input_tokens)":\d+',
                  r'"\1":0', stream)


def child_bytes(srv, follow_order) -> dict:
    srv.load(two_calls("claude-haiku-5-5", "claude-haiku-5-5", True))
    Convo(srv, tools=TOOLS).user("go")
    children, out = [], {}
    for i in range(2):
        c = Convo(srv, tools=TOOLS)
        body = c.body([{"role": "user", "content": f"{NOTE}\n\ncheck"}])
        status, _, data = srv.raw("POST", "/v1/messages", json.dumps(body).encode())
        assert status == 200
        c.last_id = re.search(r'"id":"(msg_[^"]+)"', data.decode()).group(1)
        children.append(c)
        out[f"launch {i}"] = mask_usage(ID_RE.sub("<id>", data.decode()))
    for i in follow_order:
        body = children[i].body([{"role": "user", "content": "more"}])
        status, _, data = srv.raw("POST", "/v1/messages", json.dumps(body).encode())
        out[f"follow-up {i}"] = ID_RE.sub("<id>", data.decode())
    return out


def test_5_equivalent_launches_give_the_same_bytes_whichever_claims_which(tmp_path):
    with fresh_server(tmp_path, "a") as a, fresh_server(tmp_path, "b") as b:
        one, two = child_bytes(a, [0, 1]), child_bytes(b, [1, 0])
    assert one == two
    # And the two children's replies are the same bytes as each other: the
    # stream seed is the child's envelope, not the instance it claimed.
    assert one["launch 0"] == one["launch 1"] and one["follow-up 0"] == one["follow-up 1"]


# --------------------------------------------------------------- 6. minors


def test_6_a_lone_surrogate_is_found_beside_a_float_overflowing_literal():
    text = ('{"version": 1, "description": "\\ud800", "vars": {"x": "1"}, "handlers": [{"steps": [{"do": "branch",'
            ' "cases": [{"when": {"var": {"name": "x", "lt": 1e400}}, "steps": []}]}], "final": "x"}]}')
    with pytest.raises(ScriptError) as e:
        load_script(text)
    assert "not valid Unicode" in e.value.errors[0]


def restore_doc(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}, {"steps": [], "final": "y"}]})
    Convo(server, tools=TOOLS).user("go")
    Convo(server, tools=TOOLS).user("again")
    return server.episode.export_state()


def test_6_restore_requires_the_next_scope_when_active(server):
    doc = restore_doc(server)
    doc["next_script"] = json.dumps({"version": 1, "handlers": [{"steps": [], "final": "p"}]})
    doc["next_offset"], doc["next_active"] = 2, True
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "replacement's scope" in str(e.value)


def test_6_restore_requires_each_head_to_belong_to_its_conversation(server):
    doc = restore_doc(server)
    doc["conversations"]["c1"]["head"] = doc["conversations"]["c2"]["head"]
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "belongs to another conversation" in str(e.value)


def test_6_pis_synthetic_no_result_resumes(server):
    """Pi adds an error result "No result provided" for a tool call left
    without one (an interrupted turn) before the next user message."""
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "slow"}], "final": "x"},
                                             {"steps": [], "final": "after the interruption"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    resend = pi_resend(c.history) + [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(r)[0]["id"],
                                      "content": "No result provided", "is_error": True}]},
        {"role": "user", "content": [{"type": "text", "text": "continue where you left off"}]}]
    d = Convo(server, tools=TOOLS)
    d.history = resend[:-1]
    reply = d.send([resend[-1]], full=True)
    line = server.app.log.lines[-1]
    assert line["identity"]["decision"] == "resume" and line["tool_results"] == 1
    # Handler 0 takes the error result and finishes; the new message's handler follows in the same reply.
    assert text_of(reply) == "x\n\nafter the interruption"
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0, 1]
