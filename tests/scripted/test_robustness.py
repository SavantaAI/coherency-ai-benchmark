"""Malformed input never gives a 500, a hang or a dropped connection; reads
have deadlines; concurrent first requests do not race."""

from __future__ import annotations

import copy
import json
import socket
import threading
import time

import pytest

from cohbench.scripted import ScriptError, load_script
from cohbench.scripted.script import regex_problem
from cohbench.scripted.wire import parse_request

from .conftest import Convo, fixture_pairs, text_of

GOOD = {"model": "claude-haiku-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
SCRIPT = {"version": 1, "handlers": [{"steps": [], "final": "x"}]}


def status_of(server, body: bytes, path="/v1/messages", method="POST", headers=None) -> tuple[int, dict]:
    status, hdrs, data = server.raw(method, path, body, headers)
    if method != "HEAD":
        parsed = json.loads(data) if hdrs.get("Content-Type") == "application/json" else None
        if status >= 400:
            assert parsed["type"] == "error" and set(parsed["error"]) >= {"type", "message"}, data
    return status, hdrs


@pytest.mark.parametrize("name,body", [
    ("non-string tool_use id", {**GOOD, "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": 7, "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]}]}),
    ("missing tool_use id", {**GOOD, "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "bash", "input": {}}]}]}),
    ("unhashable tool_use_id", {**GOOD, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ["a"], "content": "y"}]}]}),
    ("tool_addition.tool not an object", {**GOOD, "messages": [
        {"role": "user", "content": "go"}, {"role": "system", "content": [{"type": "tool_addition", "tool": "bash"}]}]}),
    ("schema required not a list", {**GOOD, "tools": [{"name": "bash", "input_schema": {"type": "object", "required": "command"}}]}),
    ("text not a string", {**GOOD, "messages": [{"role": "user", "content": [{"type": "text", "text": 5}]}]}),
    ("system block without text", {**GOOD, "system": [{"type": "text"}]}),
    ("tool_result content not text", {**GOOD, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": None}]}]}]}),
    ("role not a string", {**GOOD, "messages": [{"role": ["user"], "content": "hi"}]}),
])
def test_malformed_requests_are_400(server, name, body):
    server.load(SCRIPT)
    status, _ = status_of(server, json.dumps(body).encode())
    assert status == 400, name


@pytest.mark.parametrize("where", ["metadata", "tool input", "unknown block"])
def test_deep_nesting_is_never_a_5xx(server, where):
    server.load(SCRIPT)
    deep = json.loads("[" * 3000 + "]" * 3000)
    body = copy.deepcopy(GOOD)
    if where == "metadata":
        body["metadata"] = deep
    elif where == "tool input":
        body["messages"] = [{"role": "user", "content": "go"},
                            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"a": deep}}]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "y"}]}]
    else:
        body["messages"] = [{"role": "user", "content": [{"type": "document", "source": deep}]}]
    assert status_of(server, json.dumps(body).encode())[0] in (200, 400)


@pytest.mark.parametrize("raw", [b'{"model": NaN}', b'\xff\xfe', b'{"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "\\ud800"}]}',
                                 b"[" * 100000])
def test_bytes_that_are_not_good_json_are_400(server, raw):
    assert status_of(server, raw)[0] == 400


def test_count_tokens_checks_max_tokens(server):
    body = {"model": "m", "max_tokens": "x", "messages": [{"role": "user", "content": "hi"}]}
    assert status_of(server, json.dumps(body).encode(), path="/v1/messages/count_tokens")[0] == 400


@pytest.mark.parametrize("method", ["GET", "DELETE", "HEAD", "PUT", "OPTIONS"])
def test_method_is_checked_before_the_body(server, method):
    status, hdrs = status_of(server, None, method=method)
    assert status == 405
    # The connection was closed, not left holding an unread body; the server still answers.
    assert status_of(server, json.dumps(GOOD).encode())[0] == 200


def test_a_body_that_never_arrives_gets_408_within_the_deadline(server):
    host, port = server.api
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: k\r\nContent-Length: 100\r\n\r\n{\"model\"")
    t0 = time.monotonic()
    data = s.recv(65536)
    s.close()
    assert data.startswith(b"HTTP/1.1 408"), data[:80]
    assert time.monotonic() - t0 < 5
    assert status_of(server, json.dumps(GOOD).encode())[0] in (200,)


def test_a_send_failure_after_the_log_line_is_logged(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x", "final_delay": 0.5}]})
    host, port = server.api
    body = json.dumps(Convo(server).body([{"role": "user", "content": "hi"}])).encode()
    s = socket.create_connection((host, port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: k\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
    time.sleep(0.1)
    s.close()  # gone before the delayed reply is written
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("send_failed" in x for x in server.app.log.lines):
        time.sleep(0.05)
    failed = [x for x in server.app.log.lines if "send_failed" in x]
    assert failed and failed[0]["message_id"] == server.app.log.lines[0]["message_id"]


@pytest.mark.parametrize("name,script,where", [
    ("final_delay string", {"version": 1, "handlers": [{"steps": [], "final": "x", "final_delay": "1"}]}, "$.handlers[0].final_delay"),
    ("do a list", {"version": 1, "handlers": [{"steps": [{"do": []}], "final": "x"}]}, "$.handlers[0].steps[0].do"),
    ("NaN delay", {"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "x", "delay": float("nan")}], "final": "x"}]},
     "$.handlers[0].steps[0].delay"),
    ("infinite timeout", {"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "x", "timeout": float("inf")}], "final": "x"}]},
     "$.handlers[0].steps[0].timeout"),
    ("NaN in a tool input", {"version": 1, "handlers": [{"steps": [{"do": "tool", "name": "t", "input": {"a": [float("nan")]}}], "final": "x"}]},
     "$.handlers[0].steps[0].input.a[0]"),
    ("end a list", {"version": 1, "handlers": [{"steps": [], "final": "x", "end": []}]}, "$.handlers[0].end"),
    ("version true", {"version": True, "handlers": [{"steps": [], "final": "x"}]}, "$.version"),
])
def test_malformed_scripts_are_path_specific_errors(name, script, where):
    with pytest.raises(ScriptError) as e:
        load_script(script)
    assert any(err.startswith(where) for err in e.value.errors), e.value.errors


@pytest.mark.parametrize("raw", [b"\xff\xfe{}", b'{"version": 1, "handlers": NaN}', b"[" * 100000,
                                 b'{"version": 1, "handlers": [{"steps": [], "final": "\\ud800"}]}'])
def test_bad_bytes_on_the_admin_endpoint_are_400(server, raw):
    status, _, body = server.raw("POST", "/script", raw, admin=True)
    assert status == 400 and json.loads(body)["errors"][0].startswith("$"), body


def test_admin_wrong_method_is_404_not_a_hang(server):
    for method, path in (("DELETE", "/script"), ("HEAD", "/state"), ("GET", "/reset"), ("POST", "/state")):
        assert server.raw(method, path, None, admin=True)[0] == 404
    assert server.raw("GET", "/healthz", None, admin=True)[0] == 200


# ------------------------------------------------------------ regexes


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(\w+\s?)+$", r"(a*)*", r"(?:x|y+)+z", r"((ab)+c?)*d",
                                     r"(?>(a+)+b)"])
def test_nested_unbounded_repeats_are_refused(pattern):
    assert regex_problem(pattern), pattern
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"expect": {"regex": pattern}, "steps": [], "final": "x"}]})
    assert "exponential" in e.value.errors[0]


@pytest.mark.parametrize("pattern", [r"x(y)?", r"^(\d+)", r"job id: (\S+)", r"(ab){1,3}", r"(a{1,3}){1,3}",
                                     r"(?:a+)++b", r"(?>a+)+b", r"Command exited with code (\d+)"])
def test_ordinary_patterns_are_allowed(pattern):
    assert regex_problem(pattern) is None, pattern


def test_the_reviewers_slow_expect_is_refused_at_load():
    with pytest.raises(ScriptError):
        load_script({"version": 1, "handlers": [{"expect": {"regex": r"^(a+)+$"}, "steps": [], "final": "x"}]})


# --------------------------------------------------------- concurrency


def test_concurrent_first_requests_with_one_new_system_prompt(server, tmp_path):
    server.load({"version": 1, "handlers": [{"steps": [], "final": f"h{i}"} for i in range(16)]})
    statuses, errors = [], []

    def one(i):
        try:
            body = {"model": "m", "max_tokens": 5, "stream": True, "thread": {"type": "create"},
                    "system": "a system prompt no request has used yet", "tools": [],
                    "messages": [{"role": "user", "content": f"message {i}"}]}
            statuses.append(server.post(body)[0])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and statuses == [200] * 16
    assert len(list((tmp_path / "log").glob("system-*.txt"))) == 1
    # The state file is also rewritten after each delivery report, and those
    # writes queue on the log's lock after the clients have their replies.
    # Wait until the directory has stayed free of temporary files for 20
    # polls in a row; a leaked file would never go away.
    deadline, clean = time.monotonic() + 10, 0
    while clean < 20 and time.monotonic() < deadline:
        clean = 0 if list((tmp_path / "log").glob(".tmp-*")) else clean + 1
        time.sleep(0.02)
    assert clean >= 20, list((tmp_path / "log").glob(".tmp-*"))
    assert len(server.episode.state()["user_messages"]["main"]) == 16


# ---------------------------------------------------------------- fuzz

VALUES = [None, True, False, 0, -1, 1.5, 10 ** 30, "", "x", "é", [], {}, [None], [{}], {"type": None},
          {"type": "text"}, [{"type": "text", "text": 1}], "__DELETE__"]


def paths(value, prefix=(), depth=0, limit=6):
    """Every path into a JSON value, to `limit` levels, lists sampled."""
    yield prefix
    if depth >= limit:
        return
    if isinstance(value, dict):
        for k in value:
            yield from paths(value[k], prefix + (k,), depth + 1, limit)
    elif isinstance(value, list):
        for i in sorted({0, len(value) - 1} if value else set()):
            yield from paths(value[i], prefix + (i,), depth + 1, limit)


def mutate(value, path, new):
    value = copy.deepcopy(value)
    if not path:
        return new if new != "__DELETE__" else {}
    cur = value
    for k in path[:-1]:
        cur = cur[k]
    if new == "__DELETE__":
        del cur[path[-1]]
    else:
        cur[path[-1]] = new
    return value


def fuzz_bases():
    steering = fixture_pairs("steering")
    first = copy.deepcopy(steering[0][0])
    first["tools"] = first["tools"][:2]
    first["system"] = [{"type": "text", "text": "short"}]
    history = {"model": "claude-haiku-5-5", "max_tokens": 20, "stream": False, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "go", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "toolu_01x", "name": "bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01x", "content": [{"type": "text", "text": "a"}],
                                      "is_error": False}]},
        {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {
            "name": "t", "input_schema": {"type": "object", "required": ["a"], "properties": {"a": {}}}}}}]},
    ], "tools": [{"name": "bash", "input_schema": {"type": "object", "required": ["command"]}, "defer_loading": False}]}
    cont = {"model": "claude-haiku-5-5", "max_tokens": 20, "stream": True, "messages": [{"role": "user", "content": "x"}],
            "thread": {"type": "continue", "previous_message_id": "msg_01missing"}}
    return [first, history, cont]


def test_fuzz_requests_never_500_and_the_server_stays_up(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "ls"}], "final": "x"}] * 3})
    seen: dict[int, int] = {}
    for base in fuzz_bases():
        for path in list(paths(base)):
            for new in VALUES:
                body = mutate(base, path, new)
                for route in ("/v1/messages", "/v1/messages/count_tokens"):
                    status, _ = status_of(server, json.dumps(body).encode(), path=route)
                    seen[status] = seen.get(status, 0) + 1
                    assert status < 500, (route, path, new)
    assert 400 in seen and 200 in seen
    assert server.raw("GET", "/healthz", None, admin=True)[0] == 200
    assert status_of(server, json.dumps(GOOD).encode())[0] == 200


FEATURE_SCRIPT = {
    "version": 1, "vars": {"best": "100"}, "fallback": "Sorry.",
    "handlers": [
        {"id": "h", "on_steer": "abandon", "end": "question", "final_delay": 0,
         "capture": [{"var": "word", "regex": "(\\w+)", "group": 1, "required": False}],
         "expect": {"any": [{"contains": "a"}, {"var": {"name": "user_message", "regex": "b"}}]},
         "steps": [
             {"do": "bash", "command": "echo {{word}}", "timeout": 5, "say": "s", "delay": 0,
              "capture": [{"var": "n", "regex": "(\\d+)"}]},
             {"do": "branch", "cases": [{"when": {"var": {"name": "n", "lt": {"var": "best"}}},
                                         "steps": [{"do": "set", "var": "best", "value": "{{n}}"}]}],
              "else": [{"do": "say", "text": "no"}]},
             {"do": "repeat", "max": 3, "until": {"var": {"name": "best", "le": 1}}, "steps": [
                 {"do": "parallel", "calls": [{"do": "read", "path": "a", "offset": None, "limit": None},
                                              {"do": "write", "path": "b", "content": "c"}]}]},
             {"do": "delegate", "child": "k", "task": "t {{best}}", "input": {"role": "research"}},
             {"do": "edit", "path": "p", "edits": [{"oldText": "a", "newText": "b"}]},
             {"do": "reply", "text": "r", "end": "statement"},
         ], "final": "f {{best}} {{last_exit}}"}],
    "children": {"k": {"handlers": [{"steps": [{"do": "tool", "name": "bash", "input": {"command": "x"}}], "final": "y"}]}},
}


def test_fuzz_scripts_only_ever_raise_script_errors():
    load_script(FEATURE_SCRIPT)
    loaded = refused = 0
    for path in list(paths(FEATURE_SCRIPT, limit=9)):
        for new in VALUES + [float("nan"), float("inf"), "{{nope}}", "(a+)+", {"do": "repeat"}]:
            try:
                load_script(mutate(FEATURE_SCRIPT, path, new))
                loaded += 1
            except ScriptError:
                refused += 1
    assert loaded and refused


def test_fuzz_scripts_that_load_also_run(server):
    """Mutations that still validate must run without a 500."""
    mutations = [(p, v) for p in paths(FEATURE_SCRIPT, limit=9) for v in VALUES]
    ran = 0
    for path, new in mutations:
        try:
            script = load_script(mutate(FEATURE_SCRIPT, path, new))
        except ScriptError:
            continue
        server.episode.load(script)
        c = Convo(server, tools=[{"name": n, "input_schema": {"type": "object"}} for n in
                                 ("bash", "read", "write", "edit", "mcp__t3-code__delegate_task")])
        r = c.user("a 7 b")
        for _ in range(8):
            calls = [b for b in r["content"] if b["type"] == "tool_use"]
            if not calls:
                break
            r = c.results(["3"] * len(calls))
        ran += 1
    assert ran > 50, ran


def test_a_non_finite_number_in_a_request_is_400(server):
    server.load(SCRIPT)
    raw = b'{"model": "m", "max_tokens": 5, "temperature": 1e400, "messages": [{"role": "user", "content": "hi"}]}'
    assert status_of(server, raw)[0] == 400


def test_a_trickled_body_meets_a_total_deadline(server):
    host, port = server.api  # the fixture's read_timeout is 2 s
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: k\r\nContent-Length: 40\r\n\r\n")
    t0 = time.monotonic()
    data = b""
    s.setblocking(False)
    for _ in range(40):
        try:
            s.sendall(b" ")
        except OSError:
            break
        time.sleep(0.25)  # each gap is far below the deadline; the total is not
        try:
            data += s.recv(65536)
        except BlockingIOError:
            pass
        if data:
            break
    s.close()
    assert data.startswith(b"HTTP/1.1 408"), data[:80]
    assert 1.5 < time.monotonic() - t0 < 4


@pytest.mark.parametrize("value", [b"\xb2", b"\xd9\xa3", b"1 2", b"-1", b""])
def test_content_length_must_be_ascii_digits(server, value):
    host, port = server.api
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: k\r\nContent-Length: " + value + b"\r\n\r\n{}")
    data = s.recv(65536)
    s.close()
    assert data.startswith(b"HTTP/1.1 411") or data.startswith(b"HTTP/1.1 400"), data[:80]
    assert status_of(server, json.dumps(GOOD).encode())[0] == 200


def test_state_file_follows_delivery_and_announcements(server, tmp_path):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "delegate", "child": "c", "task": "t"}], "final": "x"},
                                             {"steps": [], "final": "y"}],
                 "children": {"c": {"handlers": [{"steps": [], "final": "z"}]}}})
    Convo(server, tools=[{"name": "mcp__t3-code__delegate_task", "input_schema": {"type": "object"}}]).user("go")
    path = tmp_path / "log" / "state.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        st = json.loads(path.read_text())
        if st["delegations"] and st["delegations"][0]["status"] == "delivered":
            break
        time.sleep(0.05)
    assert st["delegations"][0]["status"] == "delivered"
    server.raw("POST", "/next", b'{"index": 1}', admin=True)
    assert json.loads(path.read_text())["announcements"][0]["outcome"] == "pending"
    assert json.loads(path.read_text()) == json.loads(server.raw("GET", "/state", None, admin=True)[2])
