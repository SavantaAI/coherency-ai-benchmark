"""Wire format: event sequence, JSON validity, non-streaming, count_tokens,
errors, headers and ids."""

from __future__ import annotations

import json
import re

from cohbench.scripted import ids

from .conftest import Convo, assemble, calls_of, text_of

EXPECTED_START_KEYS = ["model", "id", "type", "role", "content", "container", "stop_reason", "stop_sequence",
                       "stop_details", "usage", "input_transformations", "diagnostics"]
USAGE_KEYS = {"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "cache_creation",
              "output_tokens", "service_tier", "inference_geo", "speed"}
DELTA_USAGE_KEYS = {"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
                    "output_tokens_details"}


def names(events):
    return [n for n, _ in events]


def test_text_reply_event_sequence(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "ready"}]})
    status, headers, events = server.post(Convo(server).body([{"role": "user", "content": "Reply with ready"}]))
    assert status == 200
    assert headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert names(events) == ["message_start", "content_block_start", "ping", "content_block_delta",
                             "content_block_stop", "message_delta", "message_stop"]
    start = events[0][1]["message"]
    assert list(start) == EXPECTED_START_KEYS
    assert start["model"] == "claude-haiku-5-5" and start["content"] == [] and start["stop_reason"] is None
    assert set(start["usage"]) == USAGE_KEYS
    assert events[1][1]["content_block"] == {"type": "text", "text": ""}
    delta = events[-2][1]
    assert delta["delta"] == {"stop_reason": "end_turn", "stop_sequence": None, "stop_details": None, "container": None}
    assert set(delta["usage"]) == DELTA_USAGE_KEYS
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        assert delta["usage"][k] == start["usage"][k]
    assert start["usage"]["output_tokens"] <= delta["usage"]["output_tokens"]
    assert text_of(assemble(events)) == "ready"


def test_tool_call_events_and_large_input(server):
    big = "\n".join(f"line {i:04d} " + "x" * 40 for i in range(200))
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "write", "path": "/work/project/big.txt", "content": big, "say": "Writing the file now."}],
        "final": "done"}]})
    status, _, events = server.post(Convo(server).body([{"role": "user", "content": "write it"}]))
    assert status == 200
    seq = names(events)
    assert seq[:4] == ["message_start", "content_block_start", "ping", "content_block_delta"]
    starts = [d for n, d in events if n == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["text", "tool_use"]
    tool_start = starts[1]["content_block"]
    assert tool_start["input"] == {} and tool_start["caller"] == {"type": "direct"} and tool_start["name"] == "write"
    assert ids.is_tool_use_id(tool_start["id"])
    deltas = [d["delta"]["partial_json"] for n, d in events if n == "content_block_delta" and d["index"] == 1]
    assert len(deltas) > 100, "a large input is split over many deltas"
    # The last value is a string, so the closing quote and brace come together.
    assert deltas[0] == "" and deltas[-1] == '"}'

    assert json.loads("".join(deltas)) == {"path": "/work/project/big.txt", "content": big}
    # The concatenated JSON uses the real stream's separators.
    assert "".join(deltas).startswith('{"path": "/work/project/big.txt", "content": ')
    msg = assemble(events)
    assert msg["stop_reason"] == "tool_use"
    assert text_of(msg) == "Writing the file now."


def test_every_event_is_padded_json_like_the_real_stream(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "A reply long enough to be split into pieces."}]})
    status, _, data = server.raw("POST", "/v1/messages", json.dumps(
        Convo(server).body([{"role": "user", "content": "hi"}])).encode())
    text = data.decode()
    for line in text.splitlines():
        if line.startswith("data: ") and '"ping"' not in line:
            assert re.fullmatch(r'data: \{.*\S *\}', line), line
    assert 'data: {"type": "ping"}' in text


def test_non_streaming(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "ls"}], "final": "ok"}]})
    c = Convo(server, stream=False)
    status, headers, body = server.post(c.body([{"role": "user", "content": "list"}]))
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert list(body) == EXPECTED_START_KEYS
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0]["type"] == "tool_use" and body["content"][0]["input"] == {"command": "ls", "timeout": None}
    assert set(body["usage"]) == USAGE_KEYS | {"output_tokens_details"}
    assert ids.is_message_id(body["id"])
    c.send([{"role": "user", "content": "list"}])
    second = c.results(["a\nb\n"])
    assert second["stop_reason"] == "end_turn" and text_of(second) == "ok"


def test_count_tokens(server):
    status, headers, body = server.post({"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hello there"}],
                                         "system": "sys", "tools": []}, path="/v1/messages/count_tokens")
    assert status == 200 and set(body) == {"input_tokens"} and body["input_tokens"] > 0
    assert server.episode.state()["requests"] == 0, "count_tokens is not an episode request"


def test_ids_and_headers(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    status, headers, events = server.post(Convo(server).body([{"role": "user", "content": "hi"}]))
    assert "Server" not in headers
    assert ids.is_request_id(headers["request-id"])
    assert ids.is_message_id(events[0][1]["message"]["id"])
    joined = json.dumps(headers).lower() + json.dumps(events).lower()
    for word in ("script", "fake", "mock", "bench", "test", "scenario", "stub", "simulat"):
        assert word not in joined.replace("test-key", ""), word


def error_of(status, headers, body):
    assert body["type"] == "error" and set(body) == {"type", "error", "request_id"}
    assert ids.is_request_id(body["request_id"]) and headers["request-id"] == body["request_id"]
    assert headers["x-should-retry"] == "false"
    return status, body["error"]["type"], body["error"]["message"]


def test_malformed_requests(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}]})
    good = {"model": "claude-haiku-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}

    def post(body, **kw):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        status, headers, data = server.raw("POST", kw.get("path", "/v1/messages"), raw, kw.get("headers"))
        return error_of(status, headers, json.loads(data))

    assert post(b"{not json")[:2] == (400, "invalid_request_error")
    assert post([1, 2])[:2] == (400, "invalid_request_error")
    assert post({k: v for k, v in good.items() if k != "model"}) == (400, "invalid_request_error", "model: Field required")
    assert post({k: v for k, v in good.items() if k != "max_tokens"})[2] == "max_tokens: Field required"
    assert post({**good, "messages": []})[0] == 400
    assert post({**good, "messages": [{"role": "robot", "content": "x"}]})[2].startswith("messages.0.role")
    assert post({**good, "messages": [{"role": "user", "content": [{"text": "no type"}]}]})[0] == 400
    assert post({**good, "stream": "yes"})[2] == "stream: Input should be a valid boolean"
    assert post({**good, "thread": {"type": "fork"}})[0] == 400
    assert post({**good, "thread": {"type": "continue"}})[2] == "thread.previous_message_id: Field required"
    st, et, msg = post({**good, "thread": {"type": "continue", "previous_message_id": ids.message_id()}})
    assert (st, et) == (400, "invalid_request_error") and "no message" in msg
    # A tool call left without its result, and a result for no call.
    tid = ids.tool_use_id()
    st, _, msg = post({**good, "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "bash", "input": {}}]},
        {"role": "user", "content": "next"}]})
    assert st == 400 and "without `tool_result` blocks" in msg and tid in msg
    st, _, msg = post({**good, "messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "x"}]}]})
    assert st == 400 and "unexpected `tool_use_id`" in msg
    # Auth, path, method.
    assert post(good, headers={"x-api-key": None})[:2] == (401, "authentication_error")
    assert post(good, path="/v1/complete")[:2] == (404, "not_found_error")
    status, headers, data = server.raw("GET", "/v1/messages", None)
    assert error_of(status, headers, json.loads(data))[0] == 405
    # None of these touched the episode, and the connection stayed usable.
    assert server.episode.state()["requests"] == 0


def test_thread_errors_carry_pis_error_codes(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "x"}, {"steps": [], "final": "y"}]})
    c = Convo(server)
    first = c.user("one")
    status, headers, body = server.post({"model": "claude-other", "max_tokens": 5, "messages": [
        {"role": "user", "content": "two"}], "thread": {"type": "continue", "previous_message_id": first["id"]}})
    assert status == 400 and body["error"]["details"] == {"error_code": "thread_fingerprint_mismatch"}
    status, headers, body = server.post({"model": "claude-haiku-5-5", "max_tokens": 5, "messages": [
        {"role": "user", "content": "two"}], "thread": {"type": "continue", "previous_message_id": ids.message_id()}})
    assert status == 400 and body["error"]["details"] == {"error_code": "thread_not_found"}
