"""The fourth review round's findings, one test or more per finding id."""

from __future__ import annotations

import io
import json
import threading
from contextlib import contextmanager, redirect_stderr

import pytest

from cohbench.scripted import App, Episode, ScriptError, load_script
from cohbench.scripted.__main__ import main as cli_main

from .conftest import Convo, Server, calls_of, text_of

NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'
DELEGATE = [{"name": n, "input_schema": {"type": "object"}} for n in ("bash", "mcp__t3-code__delegate_task")]


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def launch(task: str) -> str:
    return f"{NOTE}\n\n{task}"


@contextmanager
def fresh_server(tmp_path, name):
    app = App(Episode(), tmp_path / name, read_timeout=2.0)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    try:
        yield Server(app, api, admin)
    finally:
        app.stop()


# ----------------------------------------------------------------------- M1


def heard(server) -> Convo:
    server.load({"version": 1, "handlers": [{"steps": [], "final": "ready"},
                                             {"steps": [], "final": "heard [{{user_message}}]"}]})
    c = Convo(server)
    c.user("one")
    return c


def test_m1_a_trailing_space_is_different_content(server):
    c = heard(server)
    first = c.body([{"role": "user", "content": [{"type": "text", "text": "choose A"}]}])
    status, _, events = server.post(first)
    joined = "".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta")
    assert status == 200 and joined == "heard [choose A]"
    second = c.body([{"role": "user", "content": [{"type": "text", "text": "choose A "}]}])
    status, _, body = server.post(second)
    assert status == 400 and body["error"]["details"] == {"error_code": "thread_already_continued"}
    assert server.app.log.lines[-1]["identity"]["decision"] == "refused"


def test_m1_a_different_system_block_is_different_content(server):
    c = heard(server)
    def body(tool_name):
        return c.body([{"role": "user", "content": [{"type": "text", "text": "choose A"}]},
                       {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition",
                        "definition": {"name": tool_name, "input_schema": {"type": "object"}}}}]}])
    assert server.post(body("tool_one"))[0] == 200
    status, _, err = server.post(body("tool_two"))
    assert status == 400 and err["error"]["details"]["error_code"] == "thread_already_continued"


def test_m1_a_byte_identical_continue_replays(server):
    c = heard(server)
    body = c.body([{"role": "user", "content": [{"type": "text", "text": "choose A", "cache_control": {"type": "ephemeral"}}]}])
    s1, _, e1 = server.post(body)
    # The cache marker is the one thing left out of the fingerprint.
    body2 = json.loads(json.dumps(body))
    del body2["messages"][0]["content"][0]["cache_control"]
    s2, _, e2 = server.post(body2)
    assert s1 == s2 == 200 and e1[0][1]["message"]["id"] == e2[0][1]["message"]["id"]
    assert server.app.log.lines[-1]["decision"] == "replay"


# ----------------------------------------------------------------------- M2


def async_twice(first: str, second: str) -> dict:
    return {"version": 1, "handlers": [{"steps": [
        {"do": "set", "var": "mark", "value": first},
        {"do": "delegate", "child": "worker", "task": "t"},
        {"do": "set", "var": "mark", "value": second},
        {"do": "delegate", "child": "worker", "task": "t"}], "final": "both issued"}],
        "children": {"worker": {"handlers": [{"steps": [], "final": "I got {{mark}}"}]}}}


@pytest.mark.parametrize("first,second", [("A", "B"), ("B", "A")])
def test_m2_two_outstanding_delegations_with_one_envelope_must_be_equivalent(server, first, second):
    server.load(async_twice(first, second))
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    r = parent.results(['{"taskId":"1","status":"running"}'])   # async: the child has not launched yet
    assert calls_of(r) == [] and text_of(r) == "Sorry, I can't continue with this right now."
    assert codes(server) == ["ambiguous_delegation"]
    assert len(server.episode.state()["delegations"]) == 1
    # The one delegation that was issued is the only thing a launch can mean.
    assert text_of(Convo(server, tools=DELEGATE).user(launch("t"))) == f"I got {first}"


def test_m2_equivalent_delegations_stay_interchangeable(server):
    server.load(async_twice("A", "A"))
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    parent.results(["ok"])
    assert text_of(parent.results(["ok"])) == "both issued"
    for _ in range(2):
        assert text_of(Convo(server, tools=DELEGATE).user(launch("t"))) == "I got A"
    assert codes(server) == []
    assert [d["status"] for d in server.episode.state()["delegations"]] == ["claimed", "claimed"]


# ----------------------------------------------------------------------- M3

PRECISE = ('{"version": 1, "handlers": [{"steps": [{"do": "branch", "cases": ['
           '{"when": {"var": {"name": "x", "eq": 0.30000000000000001}}, "steps": [{"do": "reply", "text": "equal"}]}],'
           ' "else": [{"do": "reply", "text": "unequal"}]}]}]}')


def precise_export(server) -> tuple[dict, Convo]:
    server.load({"version": 1, "vars": {"x": "0.30000000000000001"}, "handlers": [{"steps": [], "final": "ok"}]})
    c = Convo(server)
    c.user("hello")
    return json.loads(server.raw("GET", "/export", None, admin=True)[2]), c


def probe(cp, c: Convo) -> str:
    """The copy's Pi resends the history with a new question."""
    body = {"model": c.model, "max_tokens": 100, "stream": True, "thread": {"type": "create"}, "system": c.system,
            "tools": c.tools, "messages": c.history + [{"role": "user", "content": "is it?"}]}
    status, _, events = cp.post(body)
    assert status == 200
    return "".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta")


def test_m3_post_import_keeps_the_replacement_text(server, tmp_path):
    exported, c = precise_export(server)
    with fresh_server(tmp_path, "cp") as cp:
        body = json.dumps({"state": exported, "next_script": PRECISE}).encode()
        assert cp.raw("POST", "/import", body, admin=True)[0] == 200
        assert probe(cp, c) == "equal"
        again = json.loads(cp.raw("GET", "/export", None, admin=True)[2])
        assert again["next_script"] == PRECISE


def test_m3_an_inline_replacement_object_is_refused(server, tmp_path):
    exported, _ = precise_export(server)
    with fresh_server(tmp_path, "cp") as cp:
        status, _, body = cp.raw("POST", "/import", json.dumps({"state": exported, "next_script": json.loads(PRECISE)}).encode(),
                                 admin=True)
        assert status == 400 and "string" in json.loads(body)["error"]
        assert cp.episode.state()["script"] is None


@pytest.mark.parametrize("form", ["export file and --next-script", "wrapper file"])
def test_m3_the_cli_passes_the_replacement_text_through(server, tmp_path, form):
    exported, c = precise_export(server)
    export_file = tmp_path / "export.json"
    script_file = tmp_path / "probe.json"
    script_file.write_text(PRECISE)
    with fresh_server(tmp_path, "cp") as cp:
        url = f"http://{cp.admin[0]}:{cp.admin[1]}"
        if form == "wrapper file":
            export_file.write_text('{"state": ' + json.dumps(exported) + ', "next_script": ' + json.dumps(PRECISE) + "}")
            args = ["admin", "--url", url, "import", str(export_file)]
        else:
            export_file.write_text(json.dumps(exported))
            args = ["admin", "--url", url, "import", str(export_file), "--next-script", str(script_file)]
        out = io.StringIO()
        with redirect_stderr(out):
            assert cli_main(args) == 0, out.getvalue()
        assert probe(cp, c) == "equal"
        assert json.loads(cp.raw("GET", "/export", None, admin=True)[2])["next_script"] == PRECISE


def test_m3_the_hash_is_over_the_exact_text():
    a = load_script(PRECISE, known_vars={"x"})
    b = load_script(PRECISE.replace("0.30000000000000001", "0.3"), known_vars={"x"})
    assert a.sha256 != b.sha256 and a.source == PRECISE


# ----------------------------------------------------------------------- M4

ORIGINAL = {"version": 1, "vars": {"x": "orig"}, "fallback": "original fallback", "handlers": [
    {"steps": [{"do": "bash", "command": "a", "capture": [{"var": "got", "regex": "(ok)", "required": True}]},
               {"do": "delegate", "child": "w", "task": "work"}], "final": "parent {{x}}"}],
    "children": {"w": {"handlers": [{"steps": [
        {"do": "bash", "command": "c", "capture": [{"var": "cv", "regex": "(fine)", "required": True}]}],
        "final": "child {{x}}"}]}}}
REPLACEMENT = json.dumps({"version": 1, "vars": {"x": "new"}, "fallback": "probe fallback",
                          "handlers": [{"steps": [], "final": "probe {{x}}"}],
                          "children": {"w": {"handlers": [{"steps": [], "final": "replacement child {{x}}"}]}}})


def run_on_both(server, tmp_path, prepare, finish):
    """`prepare` brings the original server to a point; it is exported and
    imported, with the replacement, into a fresh server; then `finish` sends
    the same request to both, and both replies are returned."""
    convos = prepare(server)
    exported = json.loads(server.raw("GET", "/export", None, admin=True)[2])
    with fresh_server(tmp_path, "cp") as cp:
        status, _, body = cp.raw("POST", "/import", json.dumps({"state": exported, "next_script": REPLACEMENT}).encode(),
                                 admin=True)
        assert status == 200, body
        replies = []
        for srv in (server, cp):
            replies.append(finish(srv, convos))
        replies.append(cp.episode.state())
    return replies


def parent_waiting(server):
    server.load(ORIGINAL)
    parent = Convo(server, tools=DELEGATE)
    parent.user("go")
    return {"parent": parent}


def answer_parent(result):
    def finish(srv, convos):
        p = convos["parent"]
        c = Convo(srv, tools=DELEGATE)
        c.last_id, c.replies = p.last_id, list(p.replies)
        r = c.results([result])
        if calls_of(r):  # the delegation: let it finish
            r = c.results(['{"summary":"done"}'])
        return text_of(r)
    return finish


@pytest.mark.parametrize("result,expected", [("ok", "parent orig"), ("no match", "original fallback")],
                         ids=["success", "failed capture"])
def test_m4_an_original_main_handler_is_unchanged_by_the_import(server, tmp_path, result, expected):
    original, on_copy, copy_state = run_on_both(server, tmp_path, parent_waiting, answer_parent(result))
    assert original == on_copy == expected
    assert copy_state["vars"].get("x") == "orig", "the replacement's vars apply only once its handler starts"


def child_waiting(server):
    convos = parent_waiting(server)
    convos["parent"].results(["ok"])
    child = Convo(server, tools=DELEGATE)
    child.user(launch("work"))
    convos["child"] = child
    return convos


def answer_child(result):
    def finish(srv, convos):
        ch = convos["child"]
        c = Convo(srv, tools=DELEGATE)
        c.last_id, c.replies = ch.last_id, list(ch.replies)
        return text_of(c.results([result]))
    return finish


@pytest.mark.parametrize("result,expected", [("fine", "child orig"), ("broken", "original fallback")],
                         ids=["success", "failed capture"])
def test_m4_an_active_child_is_unchanged_by_the_import(server, tmp_path, result, expected):
    original, on_copy, _ = run_on_both(server, tmp_path, child_waiting, answer_child(result))
    assert original == on_copy == expected


def test_m4_the_replacement_applies_from_its_first_handler(server, tmp_path):
    def finish(srv, convos):
        c = convos["parent"]
        cv = Convo(srv, tools=DELEGATE)
        cv.history, cv.last_id = list(c.history), None
        return text_of(cv.user("probe question", full=True))
    def prepare(srv):
        srv.load(ORIGINAL)
        p = Convo(srv, tools=DELEGATE)
        p.user("go")
        p.results(["ok"])
        p.results(['{"summary":"done"}'])
        return {"parent": p}
    _, on_copy, state = run_on_both(server, tmp_path, prepare, finish)
    # The replacement's handlers have a scope of their own; the main one is untouched.
    assert on_copy == "probe new" and state["scopes"]["next"]["x"] == "new" and state["vars"]["x"] == "orig"


# ------------------------------------------------------------------- minors


def exported_mid(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "repeat", "max": 3, "steps": [{"do": "bash", "command": "x"}]},
        {"do": "delegate", "child": "w", "task": "t"}], "final": "x"}],
        "children": {"w": {"handlers": [{"steps": [], "final": "w"}]}}})
    c = Convo(server, tools=DELEGATE)
    c.user("go")
    c.results(["1"])
    c.results(["2"])
    c.results(["3"])
    return server.episode.export_state()


@pytest.mark.parametrize("breakage", ["empty announcement", "repeat count", "repeat count zero", "next script",
                                      "parent conversation", "tool use id"])
def test_restore_rejects_hand_built_exports(server, breakage):
    doc = exported_mid(server)
    if breakage == "empty announcement":
        doc["announcements"], doc["announced"] = [{}], 0
    elif breakage.startswith("repeat count"):
        node = next(n for n in doc["nodes"] if any(q.startswith("@repeat|") for q in n["state"]["queue"]))
        q = next(i for i, x in enumerate(node["state"]["queue"]) if x.startswith("@repeat|"))
        path = node["state"]["queue"][q].rsplit("|", 1)[0]
        node["state"]["queue"][q] = f"{path}|{9999 if breakage == 'repeat count' else 0}"
    else:
        assert doc["delegations"], "the fixture issued a delegation"
        d = doc["delegations"][0]
        if breakage == "next script":
            d["script"] = "next"
        elif breakage == "parent conversation":
            d["parent_conversation"] = "c99"
        else:
            d["tool_use_id"] = "toolu_01nothing"
    with pytest.raises(ValueError):
        Episode().import_state(doc)


@pytest.mark.parametrize("literal", ["1e-99999999999999999999", "1e99999999999999999999"])
def test_a_literal_out_of_decimal_range_is_refused_at_load(literal):
    text = ('{"version": 1, "vars": {"x": "0"}, "handlers": [{"steps": [{"do": "branch", "cases": ['
            '{"when": {"var": {"name": "x", "eq": ' + literal + '}}, "steps": []}]}], "final": "x"}]}')
    with pytest.raises(ScriptError) as e:
        load_script(text)
    assert e.value.errors[0].startswith("$.handlers[0].steps[0].cases[0].when.var.eq")


def test_the_send_race_over_http(server, monkeypatch):
    """The first attempt to send the delegating reply fails; Pi's retry is
    paused before its first byte; the child launch arrives meanwhile and must
    be placed as the child. This fails if the attempt were registered after
    the first write."""
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "w", "task": "check"}],
                                             "final": "done"}],
                 "children": {"w": {"handlers": [{"steps": [], "final": "child"}]}}})
    original = App._send
    calls = {"n": 0}
    paused, release = threading.Event(), threading.Event()

    def patched(self, h, stream, outcome, rid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokenPipeError("the client went away")
        if calls["n"] == 2:
            paused.set()
            release.wait(5)
        return original(self, h, stream, outcome, rid)

    monkeypatch.setattr(App, "_send", patched)
    body = Convo(server, tools=DELEGATE).body([{"role": "user", "content": "go"}])
    with pytest.raises(Exception):
        server.post(body)                       # the first attempt fails
    retry = threading.Thread(target=lambda: server.post(body))
    retry.start()                               # Pi's retry: replayed, then paused before writing
    assert paused.wait(5)
    status, _, events = server.post(Convo(server, tools=DELEGATE).body([{"role": "user", "content": launch("check")}]))
    launched = server.app.log.lines[-1]
    release.set()
    retry.join(5)
    assert status == 200 and launched["conversation_kind"] == "child", launched
