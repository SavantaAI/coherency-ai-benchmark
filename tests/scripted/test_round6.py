"""The sixth review round's findings, and one resume test per rule of Pi's
history conversion."""

from __future__ import annotations

import copy
import json

import pytest

from cohbench.scripted import Episode, ScriptError, load_script

from .conftest import Convo, calls_of, text_of

FALLBACK = "Sorry, I can't continue with this right now."
TOOLS = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "probe", "mcp__t3-code__delegate_task")]


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


# ECMAScript's trim() whitespace, written out from the spec (WhiteSpace and
# LineTerminator), independently of the server's copy.
JS_TRIM = ("\t\v\f \u00a0\ufeff\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
           "\u2009\u200a\u202f\u205f\u3000\n\r\u2028\u2029")


def js_blank(text: str) -> bool:
    return not text.strip(JS_TRIM)


def pi_resend(history: list) -> list:
    """The history as Pi 1.6.0 rebuilds it for a full resend. This is a
    Python reconstruction, not Pi's code: it was written from and checked
    against pi-ai 1.6.0's dist/api/anthropic-messages.js (convertMessages)
    and dist/api/transform-messages.js (transformMessages), identical in the
    harness image and the global bun install. It also takes Pi's native
    form, separate {"role": "toolResult", "toolCallId", "content", "isError"}
    messages, as Pi stores tool results before converting them. Its rules:
    whitespace-only text blocks dropped and messages left empty dropped; tool
    call input as `arguments ?? {}`; tool result text blocks joined into one
    string; consecutive tool results in one user message; older user
    messages as plain strings; system messages just before the next
    assistant message."""
    out: list = []
    held: list = []
    native = []
    for m in copy.deepcopy(history):
        if m.get("role") == "toolResult":   # Pi's native tool result, before conversion
            text = "\n".join(b["text"] for b in m["content"] if b.get("type") == "text")
            m = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": m["toolCallId"], "content": text,
                                              "is_error": bool(m.get("isError"))}]}
        native.append(m)
    for m in native:
        content = m["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        keep = []
        for b in blocks:
            if b.get("type") == "text" and js_blank(b["text"]):
                continue
            b = {k: v for k, v in b.items() if k != "cache_control"}
            if b.get("type") == "tool_use":
                b["input"] = b.get("input") or {}
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                b["content"] = "\n".join(x["text"] for x in b["content"] if x.get("type") == "text")
            keep.append(b)
        if not keep:
            continue
        if m["role"] == "system":
            held.append({"role": "system", "content": keep})
            continue
        if m["role"] == "assistant":
            out.extend(held)
            held = []
        if (m["role"] == "user" and all(b.get("type") == "tool_result" for b in keep) and out
                and out[-1]["role"] == "user" and all(b.get("type") == "tool_result" for b in out[-1]["content"])):
            out[-1]["content"].extend(keep)
            continue
        if m["role"] == "user" and len(keep) == 1 and keep[0].get("type") == "text":
            out.append({"role": "user", "content": keep[0]["text"]})
        else:
            out.append({"role": m["role"], "content": keep})
    return out + held


def resumes(server, convo: Convo, question="and now?") -> dict:
    c = Convo(server, tools=convo.tools)
    c.history = pi_resend(convo.history)
    reply = c.user(question, full=True)
    line = server.app.log.lines[-1]
    assert line["identity"]["rule"] == "c" and line["identity"]["decision"] == "resume", line["identity"]
    return reply


# ---------------------------------------------------------------- B1: blank


def test_b1_blank_narration_is_skipped_and_the_resend_resumes(server):
    server.load({"version": 1, "vars": {"blank": " "}, "handlers": [
        {"steps": [{"do": "say", "text": "{{blank}}"}, {"do": "bash", "command": "x", "say": "\t{{blank}}"}],
         "final": "done"}, {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    assert [b["type"] for b in r["content"]] == ["tool_use"]
    assert server.app.log.lines[-1]["skipped_narration"] == ["$.handlers[0].steps[0].text", "$.handlers[0].steps[1].say"]
    assert text_of(c.results(["ok"])) == "done"
    assert text_of(resumes(server, c)) == "resumed"


@pytest.mark.parametrize("where", ["final", "reply"])
def test_b1_a_blank_turn_ending_text_is_a_handler_failure(server, where):
    if where == "final":
        h = {"steps": [], "final": "{{blank}}"}
    else:
        h = {"steps": [{"do": "reply", "text": "  {{blank}}"}]}
    server.load({"version": 1, "vars": {"blank": "\n"}, "handlers": [h, {"steps": [], "final": "next"}]})
    c = Convo(server, tools=TOOLS)
    assert text_of(c.user("go")) == FALLBACK and codes(server) == ["blank_reply"]
    assert server.episode.state()["handlers"]["main"][0]["status"] == "error"
    assert text_of(resumes(server, c)) == "next"


def test_b1_a_final_with_trailing_whitespace_resumes(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "done  \n"}, {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    c.user("go")
    assert text_of(resumes(server, c)) == "resumed"


def test_b1_a_call_with_no_arguments_resumes(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "tool", "name": "probe", "input": {}}], "final": "done"},
                                             {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    assert calls_of(r)[0]["input"] == {}
    c.results(["probed"])
    c.history[1]["content"][0].pop("input")          # Pi resends `arguments ?? {}`
    assert text_of(resumes(server, c)) == "resumed"


def test_b1_tool_result_text_joined_by_pi_resumes(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "bash", "command": "a"}, {"do": "bash", "command": "b"}]}], "final": "done"},
        {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    ids = [x["id"] for x in calls_of(r)]
    # The first result arrives as two text blocks; Pi's resend joins them into one string.
    c.send([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": ids[0],
         "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]},
        {"type": "tool_result", "tool_use_id": ids[1], "content": "b"}]}])
    resend = pi_resend(c.history)
    merged = [m for m in resend if m["role"] == "user" and isinstance(m["content"], list)
              and all(b.get("type") == "tool_result" for b in m["content"])]
    assert len(merged) == 1 and merged[0]["content"][0]["content"] == "line one\nline two"
    assert text_of(resumes(server, c)) == "resumed"


def test_b1_whitespace_only_user_blocks_and_string_content_resume(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "first"}, {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    c.send([{"role": "user", "content": [{"type": "text", "text": "go"}, {"type": "text", "text": "   "}]}])
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0]
    resend = pi_resend(c.history)
    assert resend[0] == {"role": "user", "content": "go"}
    assert text_of(resumes(server, c)) == "resumed"


def test_b1_a_moved_system_message_resumes(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "a"}, {"do": "bash", "command": "b"}],
                                             "final": "done"}, {"steps": [], "final": "resumed"}]})
    c = Convo(server, tools=TOOLS)
    r = c.user("go")
    addition = {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition",
                "definition": {"name": "extra", "input_schema": {"type": "object"}}}}]}
    r = c.send([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(r)[0]["id"],
                                              "content": "a"}]}, addition])
    c.results(["b"])
    assert text_of(resumes(server, c)) == "resumed"


def test_b1_a_reply_pi_never_received_whole_is_not_resumed_from_its_parent(server):
    """Pi drops an assistant message whose stream failed; a later full resend
    therefore ends at the reply before it. The conversation has replied
    since, so the documented answer is stale_history (Pi's own retry repeats
    the continue instead, which rule a answers)."""
    server.load({"version": 1, "handlers": [{"steps": [], "final": "first"}, {"steps": [], "final": "second"},
                                             {"steps": [], "final": "third"}]})
    c = Convo(server, tools=TOOLS)
    c.user("one")
    kept = list(c.history)
    c.user("two")                                     # suppose this reply's stream failed at Pi
    d = Convo(server, tools=TOOLS)
    d.history = pi_resend(kept) + [{"role": "user", "content": "two"}]
    assert text_of(d.user("three", full=True)) == FALLBACK and codes(server) == ["stale_history"]


# --------------------------------------------------------- V1: one level deep


def test_v1_a_child_may_not_delegate():
    child_delegates = {"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "a", "task": "t"}], "final": "x"}],
                       "children": {"a": {"handlers": [{"steps": [
                           {"do": "parallel", "calls": [{"do": "delegate", "child": "a", "task": "deeper"}]}],
                           "final": "x"}]}}}
    with pytest.raises(ScriptError) as e:
        load_script(child_delegates)
    assert e.value.errors == ["$.children.a.handlers[0].steps[0].calls[0]: a child's handlers may not delegate: "
                              "delegation is one level deep"]
    with pytest.raises(ScriptError):
        load_script(json.dumps(child_delegates), root="$next")


# ------------------------------------------------------------ B2, B3: minors


def test_b2_a_message_level_field_makes_a_continue_different(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "ready"}, {"steps": [], "final": "two"}]})
    c = Convo(server, tools=TOOLS)
    c.user("one")
    def body(effort):
        return c.body([{"role": "user", "content": [{"type": "text", "text": "next"}]},
                       {"role": "system", "content": [{"type": "text", "text": "effort"}], "output_config": {"effort": effort}}])
    assert server.post(body("low"))[0] == 200
    assert server.post(body("low"))[0] == 200 and server.app.log.lines[-1]["decision"] == "replay"
    status, _, err = server.post(body("high"))
    assert status == 400 and err["error"]["details"]["error_code"] == "thread_already_continued"


@pytest.mark.parametrize("conv", ["c9", "u1"])
def test_b3_import_checks_each_replys_conversation(server, conv):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    Convo(server, tools=TOOLS).user("go")
    doc = server.episode.export_state()
    doc["nodes"][0]["state"]["conv"] = conv
    with pytest.raises(ValueError) as e:
        Episode().import_state(doc)
    assert "its state names conversation" in str(e.value)
