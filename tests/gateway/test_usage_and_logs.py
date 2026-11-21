"""Request log fields, usage parsing and completeness, body storage, redaction."""

from __future__ import annotations

import gzip
import json
import socket
import stat
import time

from gateway.records import MARKER, Redactor
from gateway.usage import UsageParser, normalise

from .conftest import ANTHROPIC_KEY, OPENAI_KEY

A_USAGE = {"input_tokens": 12, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 2000, "output_tokens": 34}


def sse(*events: tuple[str | None, dict | str]) -> bytes:
    out = b""
    for name, data in events:
        if name:
            out += f"event: {name}\n".encode()
        payload = data if isinstance(data, str) else json.dumps(data)
        out += f"data: {payload}\n\n".encode()
    return out


def stream_behavior(body: bytes, piece: int = 7):
    def behavior(handler, seen):
        handler.start_chunked()
        # Small pieces, so events are split across chunks.
        for i in range(0, len(body), piece):
            handler.chunk(body[i : i + piece])
        handler.end_chunked()

    return behavior


def test_anthropic_non_streaming_usage(gateway, upstream):
    usage = {**A_USAGE, "cache_creation": {"ephemeral_5m_input_tokens": 100}, "service_tier": "standard"}
    reply = {"id": "msg_1", "type": "message", "model": "claude-x-1", "content": [], "usage": usage}
    upstream.behavior = lambda h, s: h.reply(200, json.dumps(reply).encode())
    body = json.dumps({"model": "claude-x", "max_tokens": 10, "messages": []}).encode()
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=body, headers={"Content-Type": "application/json"})
    assert json.loads(data) == reply
    line = gateway.log_lines()[-1]
    assert (line["route"], line["method"], line["path"], line["route_path"]) == ("anthropic", "POST", "/anthropic/v1/messages", "/v1/messages")
    assert (line["upstream_path"], line["status"], line["model"], line["response_model"]) == ("/v1/messages", 200, "claude-x", "claude-x-1")
    assert line["stream"] is False
    assert line["usage"] == usage
    assert line["usage_status"] == "complete"
    assert line["inference"] == "anthropic_messages"
    assert line["tokens"] == {"input_tokens": 12, "cached_input_tokens": 2000, "cache_write_tokens": 100, "output_tokens": 34}
    assert line["request_bytes"] == len(body)
    assert line["response_bytes"] == len(data)
    assert isinstance(line["duration_ms"], float)
    assert line["time"].endswith("Z")


def test_anthropic_streaming_usage_with_cache_fields(gateway, upstream):
    start_usage = {**A_USAGE, "output_tokens": 1, "cache_creation": {"ephemeral_5m_input_tokens": 1500}}
    body = sse(
        ("message_start", {"type": "message_start", "message": {"id": "m", "model": "claude-x-1", "usage": start_usage}}),
        ("ping", {"type": "ping"}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 42, "server_tool_use": None}}),
        ("message_stop", {"type": "message_stop"}),
    )
    upstream.behavior = stream_behavior(body)
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b'{"model":"claude-x","stream":true}')
    assert data == body
    line = gateway.log_lines()[-1]
    assert (line["stream"], line["response_model"]) == (True, "claude-x-1")
    assert line["usage"] == {**start_usage, "output_tokens": 42, "server_tool_use": None}
    assert line["usage_status"] == "complete"


def test_missing_required_counter_is_partial_and_null(gateway, upstream):
    usage = {"input_tokens": 5, "output_tokens": 2}  # no cache counters
    upstream.behavior = lambda h, s: h.reply(200, json.dumps({"type": "message", "usage": usage}).encode())
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    line = gateway.log_lines()[-1]
    assert line["usage_status"] == "partial"
    assert line["usage_error"] == "missing cache_read_input_tokens, cache_creation_input_tokens"
    assert line["tokens"] == {"input_tokens": 5, "cached_input_tokens": None, "cache_write_tokens": None, "output_tokens": 2}


def test_empty_or_invalid_usage_is_not_complete(gateway, upstream):
    for usage in ({}, {"input_tokens": "5", "output_tokens": -1, "cache_read_input_tokens": True, "cache_creation_input_tokens": 0}):
        upstream.behavior = lambda h, s, u=usage: h.reply(200, json.dumps({"type": "message", "usage": u}).encode())
        gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    lines = gateway.log_lines(2)
    assert [x["usage_status"] for x in lines] == ["partial", "partial"]
    assert lines[1]["tokens"] == {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": 0, "output_tokens": None}


def test_normalise_validates_nested_details():
    assert normalise("openai_chat", {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": "x"}) == (
        {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None, "output_tokens": 2},
        ["prompt_tokens_details.cached_tokens"],
    )
    tokens, missing = normalise("openai_responses", {"input_tokens": 3, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 9}})
    assert tokens["input_tokens"] is None and missing == ["input_tokens_details.cached_tokens larger than input_tokens"]


def test_openai_non_streaming_usage(gateway, upstream):
    usage = {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12, "prompt_tokens_details": {"cached_tokens": 4}}
    reply = {"id": "c", "object": "chat.completion", "model": "gpt-x-2", "choices": [], "usage": usage}
    upstream.behavior = lambda h, s: h.reply(200, json.dumps(reply).encode())
    gateway.request("POST", "/openai/v1/chat/completions", body=b'{"model":"gpt-x"}', headers={"Content-Type": "application/json"})
    line = gateway.log_lines()[-1]
    assert (line["model"], line["response_model"], line["usage"], line["usage_status"]) == ("gpt-x", "gpt-x-2", usage, "complete")
    # The route has an upstream base path; route_path is what matched.
    assert (line["upstream_path"], line["route_path"]) == ("/base/v1/chat/completions", "/v1/chat/completions")
    assert line["tokens"] == {"input_tokens": 5, "cached_input_tokens": 4, "cache_write_tokens": None, "output_tokens": 3}


def test_openai_streaming_usage_in_final_chunk(gateway, upstream):
    usage = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24, "prompt_tokens_details": {"cached_tokens": 0}}
    body = sse(
        (None, {"id": "c", "object": "chat.completion.chunk", "model": "gpt-x-2", "choices": [{"delta": {"content": "a"}}], "usage": None}),
        (None, {"id": "c", "object": "chat.completion.chunk", "model": "gpt-x-2", "choices": [], "usage": usage}),
        (None, "[DONE]"),
    )
    upstream.behavior = stream_behavior(body, piece=5)
    _, data = gateway.request("POST", "/openai/v1/chat/completions", body=b'{"model":"gpt-x","stream":true}')
    assert data == body
    line = gateway.log_lines()[-1]
    assert (line["stream"], line["usage"], line["usage_status"]) == (True, usage, "complete")


def test_openai_responses_stream_usage_and_server_tools(gateway, upstream):
    usage = {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 10}, "output_tokens": 6, "total_tokens": 56}
    body = sse(
        ("response.created", {"type": "response.created", "response": {"model": "gpt-x-2", "usage": None}}),
        ("response.output_item.added", {"type": "response.output_item.added", "item": {"type": "web_search_call", "id": "ws_1"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"}),
        ("response.completed", {"type": "response.completed", "response": {"model": "gpt-x-2", "usage": usage}}),
    )
    upstream.behavior = stream_behavior(body)
    gateway.request("POST", "/openai/v1/responses", body=b'{"model":"gpt-x","stream":true}')
    line = gateway.log_lines()[-1]
    assert (line["usage"], line["usage_status"], line["server_tool_use"]) == (usage, "complete", True)


def test_openai_responses_non_streaming_server_tools():
    parser = UsageParser("openai", "openai_responses", streaming=False)
    parser.body(json.dumps({"object": "response", "output": [{"type": "file_search_call"}, {"type": "message"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}}}).encode())
    assert parser.server_tool_use is True
    assert parser.status(True)[0] == "complete"


def test_anthropic_server_tool_use_flagged(gateway, upstream):
    usage = {**A_USAGE, "server_tool_use": {"web_search_requests": 2}}
    upstream.behavior = lambda h, s: h.reply(200, json.dumps({"type": "message", "usage": usage}).encode())
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert gateway.log_lines()[-1]["server_tool_use"] is True


def test_usage_expected_only_on_inference_endpoints(gateway, upstream):
    upstream.behavior = lambda h, s: h.reply(200, b'{"input_tokens": 7}')
    gateway.request("POST", "/anthropic/v1/messages/count_tokens", body=b"{}")
    gateway.request("GET", "/anthropic/api/claude_code/policy_limits")
    upstream.behavior = lambda h, s: h.reply(204, b"", [])
    gateway.request("DELETE", "/openai/v1/responses/r1")
    for line in gateway.log_lines(3):
        assert line["usage_status"] == "unavailable"
        assert "usage_error" not in line and "inference" not in line


def test_unparseable_response_never_breaks_forwarding(gateway, upstream):
    body = b"data: {not json\n\ndata: [1,2\n\n" + b"\xff\xfe" * 10
    upstream.behavior = stream_behavior(body)
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"not json either")
    assert (resp.status, data) == (200, body)
    line = gateway.log_lines()[-1]
    assert line["usage"] is None and line["model"] is None
    assert line["usage_status"] == "unavailable"


def test_parser_survives_garbage_directly():
    parser = UsageParser("anthropic", "anthropic_messages", streaming=False)
    parser.body(b"\x00\x01garbage")
    assert parser.usage is None
    assert parser.status(True)[:2] == ("unavailable", "response body is not JSON")


def test_invalid_event_json_marks_usage_partial(gateway, upstream):
    body = sse(
        ("message_start", {"type": "message_start", "message": {"model": "m", "usage": {**A_USAGE}}}),
        ("message_delta", "{broken"),
        ("message_stop", {"type": "message_stop"}),
    )
    upstream.behavior = stream_behavior(body)
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    line = gateway.log_lines()[-1]
    assert (line["usage_status"], line["usage_error"]) == ("partial", "invalid event JSON")


def test_stream_without_terminal_event_is_partial(gateway, upstream):
    body = sse(("message_start", {"type": "message_start", "message": {"model": "m", "usage": {**A_USAGE}}}))
    upstream.behavior = stream_behavior(body)
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    line = gateway.log_lines()[-1]
    assert (line["usage_status"], line["usage_error"]) == ("partial", "no terminal event")


def test_openai_stream_without_usage_is_unavailable(gateway, upstream):
    # Chat completions report streaming usage only when the caller asks
    # (stream_options.include_usage). The gateway does not add that.
    body = sse((None, {"object": "chat.completion.chunk", "model": "g", "choices": [], "usage": None}), (None, "[DONE]"))
    upstream.behavior = stream_behavior(body)
    gateway.request("POST", "/openai/v1/chat/completions", body=b'{"model":"g","stream":true}')
    line = gateway.log_lines()[-1]
    assert (line["usage_status"], line["usage_error"]) == ("unavailable", "no usage in response")


def test_key_absent_from_logs_and_stored_bodies(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    upstream.behavior = lambda h, s: h.reply(200, json.dumps({"type": "message", "usage": A_USAGE}).encode())
    # The key reaches logged fields through the request: the query string,
    # the model field, and (JSON-escaped) a message.
    escaped = "".join("\\u%04x" % ord(c) for c in ANTHROPIC_KEY)
    request_body = (
        '{"model":"%s","messages":[{"role":"user","content":"my key is %s"}],"note":"%s"}'
        % (ANTHROPIC_KEY, ANTHROPIC_KEY, escaped)
    ).encode()
    resp, _ = gw.request("POST", f"/anthropic/v1/messages?k={ANTHROPIC_KEY}", body=request_body, headers={"x-api-key": "sk-other"})
    assert resp.status == 200
    assert upstream.seen[-1].body == request_body  # forwarded untouched

    openai_body = json.dumps({"model": OPENAI_KEY, "stream": True}).encode()
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 0}}
    upstream.behavior = stream_behavior(sse((None, {"choices": [], "usage": usage}), (None, "[DONE]")))
    gw.request("POST", f"/openai/v1/chat/completions?k={OPENAI_KEY}", body=openai_body, headers={"Content-Type": "application/json"})

    lines = gw.log_lines(wait_for=2)
    assert [x["model"] for x in lines] == ["[REDACTED]", "[REDACTED]"]
    assert all("[REDACTED]" in x["path"] for x in lines)
    files = [p for p in gw.log_dir.rglob("*") if p.is_file()]
    first_id = lines[0]["id"]
    names = {p.name for p in files}
    assert {f"{first_id}.request", f"{first_id}.response", f"{first_id}.meta.json", "requests.jsonl"} <= names
    assert not [n for n in names if n.endswith((".tmp", ".part"))]
    for path in files:
        raw = path.read_bytes()
        assert ANTHROPIC_KEY.encode() not in raw, path
        assert OPENAI_KEY.encode() not in raw, path
    # The first body also held the key JSON-escaped, so it is not stored.
    assert (gw.log_dir / "bodies" / f"{first_id}.request").read_bytes() == MARKER
    assert lines[0]["request_body_withheld"] == "credential_in_escaped_form"
    second = (gw.log_dir / "bodies" / f"{lines[1]['id']}.request").read_bytes()
    assert second == openai_body.replace(OPENAI_KEY.encode(), b"[REDACTED]")
    meta = json.loads((gw.log_dir / "bodies" / f"{first_id}.meta.json").read_text())
    assert dict((n.lower(), v) for n, v in meta["request_headers_sent"])["x-api-key"] == "[credential]"


def test_stored_files_are_private(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    gw.log_lines()
    bodies = gw.log_dir / "bodies"
    assert stat.S_IMODE(bodies.stat().st_mode) == 0o700
    for path in [gw.log_dir / "requests.jsonl", *bodies.iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_request_body_store_decodes_within_cap(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True, max_stored_body_bytes=4096)
    plain = b'{"model":"m","messages":[]}'
    gw.request("POST", "/anthropic/v1/messages", body=gzip.compress(plain), headers={"Content-Encoding": "gzip"})
    gw.request("POST", "/anthropic/v1/messages", body=b"\x00\x01", headers={"Content-Encoding": "br <script>"})
    # 50,000 bytes once decoded, from a small gzip body: over the 4096 cap.
    gw.request("POST", "/anthropic/v1/messages", body=gzip.compress(b"x" * 50000), headers={"Content-Encoding": "gzip"})
    # Two Content-Encoding lines are one stacked coding, which is not decoded.
    conn = gw.conn()
    conn.putrequest("POST", "/anthropic/v1/messages")
    conn.putheader("Content-Encoding", "gzip")
    conn.putheader("Content-Encoding", "gzip")
    data = gzip.compress(gzip.compress(plain))
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    conn.getresponse().read()
    lines = gw.log_lines(4)
    bodies = gw.log_dir / "bodies"
    assert (bodies / f"{lines[0]['id']}.request").read_bytes() == plain
    for line, reason in zip(lines[1:], ["content_encoding_not_decoded", "too_large", "content_encoding_not_decoded"], strict=True):
        assert (bodies / f"{line['id']}.request").read_bytes() == MARKER
        assert line["request_body_withheld"] == reason


def test_meta_written_when_client_disconnects(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)

    def behavior(handler, seen):
        handler.start_chunked()
        try:
            for _ in range(200):
                handler.chunk(b'event: ping\ndata: {"type":"ping"}\n\n')
                time.sleep(0.02)
        except OSError:
            pass

    upstream.behavior = behavior
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    sock.recv(4096)
    sock.close()
    line = gw.log_lines()[-1]
    assert line["client_disconnected"] is True
    meta = json.loads((gw.log_dir / "bodies" / f"{line['id']}.meta.json").read_text())
    assert meta["outcome"]["client_disconnected"] is True
    # An unfinished response body is never published, only the marker.
    assert (gw.log_dir / "bodies" / f"{line['id']}.response").read_bytes() == MARKER


def test_bodies_not_stored_by_default(gateway, upstream):
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    gateway.log_lines()
    assert not (gateway.log_dir / "bodies").exists()


def test_redactor_walks_structures():
    redactor = Redactor(["SECRETKEY123"])
    assert redactor.walk({"a": ["xSECRETKEY123y", {"SECRETKEY123": 1}]}) == {"a": ["x[REDACTED]y", {"[REDACTED]": 1}]}
