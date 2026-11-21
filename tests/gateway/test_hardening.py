"""Round-four hardening: parser-free screening, strict heads and upstream framing,
buffered failures, event budget, early refusals, and smaller fixes."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time

import pytest

from gateway.records import MARKER, BodyStore, Redactor
from gateway.screen import Screen, unescape
from gateway.usage import PIECE, UsageParser

from .conftest import ANTHROPIC_KEY

ESCAPED = "".join("\\u%04x" % ord(c) for c in ANTHROPIC_KEY)
POST = b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}"
A_USAGE = {"input_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1}


def raw(gateway, data: bytes, timeout: float = 5, sock_opts=None) -> bytes:
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=timeout)
    sock.sendall(data)
    out = b""
    try:
        while True:
            part = sock.recv(65536)
            if not part:
                break
            out += part
    except (socket.timeout, ConnectionResetError):
        pass
    sock.close()
    return out


def status_of(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1]) if reply.startswith(b"HTTP/") else 0


# -- 1: screening without a JSON parser ------------------------------------------------------


def test_unescape_handles_surrogates_and_short_escapes():
    assert unescape(b'\\ud83d\\ude00 \\n\\t\\"\\\\\\/') == "\U0001f600 \n\t\"\\/".encode()
    assert unescape(b"\\ud800x") == "�x".encode()


@pytest.mark.parametrize(
    "event",
    [
        # Escaped key after a UTF-8 BOM in the first data field.
        b'data: \xef\xbb\xbf{"t":"' + ESCAPED.encode() + b'"}\n\n',
        # The key in an earlier member that a JSON parser would drop.
        b'data: {"t":"' + ESCAPED.encode() + b'","t":"clean"}\n\n',
        # A huge integer that made a JSON parser give up.
        b'data: {"n":' + b"9" * 5000 + b',"t":"' + ESCAPED.encode() + b'"}\n\n',
    ],
    ids=["bom", "repeated member", "huge integer"],
)
def test_event_forms_a_parser_missed(gateway, upstream, event):
    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(event)
        handler.end_chunked()

    upstream.behavior = behavior
    reply = raw(gateway, POST)
    assert ESCAPED.encode() not in reply
    assert gateway.log_lines()[-1]["gateway_error"] == "credential_in_response"


def test_body_forms_a_parser_missed(gateway, upstream):
    for body in (
        b'\xef\xbb\xbf{"t":"' + ESCAPED.encode() + b'"}',
        b'{"t":"' + ESCAPED.encode() + b'","t":"clean"}',
        b'{"n":' + b"9" * 5000 + b',"t":"' + ESCAPED.encode() + b'"}',
        b"not json at all \\u0073" + ESCAPED.encode()[6:],  # partly escaped, still the key once undone
    ):
        upstream.behavior = lambda h, s, b=body: h.reply(200, b)
        resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
        assert resp.status == 502, body[:40]
        assert ESCAPED.encode() not in data
    assert {x["gateway_error"] for x in gateway.log_lines(4)} == {"credential_in_response"}


def test_screen_leaves_clean_escapes_alone():
    screen = Screen([ANTHROPIC_KEY])
    assert not screen.body(b'{"t":"\\u0041\\n\\ud83d\\ude00"}')


# -- 2: bare CR and control bytes in request heads ---------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nX-A: 1\r\rHost: evil\r\nTransfer-Encoding: bogus\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nX-Fold: a\r b\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\n" + b"".join(b"X-%d: 1\r" % i for i in range(120)) + b"\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nX-Null: a\x00b\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/mes\rsages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/messages\tHTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nX-Del: a\x7fb\r\nContent-Length: 2\r\n\r\n{}",
    ],
    ids=["bare CR hides headers", "fold after bare CR", "120 headers on one line", "NUL", "CR in target", "tab in request line", "DEL"],
)
def test_control_bytes_in_head_refused(gateway, upstream, head):
    reply = raw(gateway, head)
    assert status_of(reply) == 400, reply[:80]
    assert b"Connection: close" in reply
    assert upstream.seen == []


def test_tab_inside_header_value_allowed(gateway, upstream):
    reply = raw(gateway, b"GET /anthropic/v1/models HTTP/1.1\r\nHost: x\r\nX-T: a\tb\r\nConnection: close\r\n\r\n")
    assert status_of(reply) == 200
    assert upstream.seen[-1].header("x-t") == "a\tb"


# -- 3 and 4: upstream framing, and failures before the head is sent -------------------------------


def persistent(head: bytes, body: bytes, hold: threading.Event | None = None):
    """An upstream that writes a response and keeps the connection open."""

    def behavior(handler, seen):
        handler.wfile.write(head + body)
        handler.wfile.flush()
        (hold or threading.Event()).wait(3)

    return behavior


def test_chunked_with_trailing_whitespace_is_decoded(gateway, upstream):
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked \r\n\r\n"
    upstream.behavior = persistent(head, b"7\r\n{\"a\":1}\r\n0\r\n\r\n")
    start = time.monotonic()
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert (resp.status, data) == (200, b'{"a":1}')
    assert time.monotonic() - start < 2  # did not wait for the upstream to close


def test_chunked_event_stream_with_trailing_whitespace(gateway, upstream):
    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\t \r\n\r\n"
    event = b'data: {"x":1}\n\n'
    upstream.behavior = persistent(head, b"%x\r\n%s\r\n0\r\n\r\n" % (len(event), event))
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert data == event


@pytest.mark.parametrize(
    "body,why",
    [
        (b"2\r\n{}\r\n0\r\n", "EOF after the zero chunk"),
        (b"2\r\n{}XX0\r\n\r\n", "bad chunk-data delimiter"),
        (b"5\r\n{}", "closes inside a chunk"),
        (b"2\r\n{}\r\n", "closes before the zero chunk"),
    ],
)
def test_bad_upstream_chunks_on_buffered_body_are_502(gateway, upstream, body, why):
    def behavior(handler, seen):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
        handler.wfile.flush()
        handler.close_connection = True

    upstream.behavior = behavior
    reply = raw(gateway, POST)
    assert status_of(reply) == 502, why
    assert b"{}" not in reply.partition(b"\r\n\r\n")[2] or b"error" in reply
    line = gateway.log_lines()[-1]
    assert (line["status"], line["gateway_error"]) == (502, "upstream_truncated")


def test_bad_upstream_chunks_on_stream_cut_without_clean_end(gateway, upstream):
    event = b'data: {"x":1}\n\n'

    def behavior(handler, seen):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        handler.wfile.write(b"%x\r\n%s\r\n0\r\n" % (len(event), event))  # no final CRLF
        handler.wfile.flush()
        handler.close_connection = True

    upstream.behavior = behavior
    reply = raw(gateway, POST)
    assert event in reply
    assert not reply.endswith(b"0\r\n\r\n")
    assert gateway.log_lines()[-1]["upstream_error"] == "bad chunked trailer"


def test_stalled_buffered_body_is_504(gateway_factory, upstream):
    gw = gateway_factory(upstream_timeout_seconds=1)
    upstream.behavior = persistent(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n", b"{")
    reply = raw(gw, POST, timeout=10)
    assert status_of(reply) == 504
    line = gw.log_lines()[-1]
    assert (line["status"], line["gateway_error"], line["upstream_status"]) == (504, "upstream_timeout", 200)


# -- 5: event budget and read boundaries --------------------------------------------------------


def run_stream(gw, upstream, stream: bytes, split: int, slow_client: bool):
    def behavior(handler, seen):
        # Small writes without Nagle's delay, so tiny splits stay quick.
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        handler.start_chunked()
        try:
            for n, i in enumerate(range(0, len(stream), split)):
                handler.chunk(stream[i : i + split])
                if n % 50 == 0:
                    time.sleep(0.002)
            handler.end_chunked()
        except OSError:
            pass

    upstream.behavior = behavior
    before = len(gw.log_lines(wait_for=0, timeout=0))
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    if slow_client:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.sendall(POST.replace(b"Content-Length: 2", b"Connection: close\r\nContent-Length: 2"))
    out = b""
    try:
        while True:
            if slow_client:
                time.sleep(0.01)
            part = sock.recv(1024 if slow_client else 65536)
            if not part:
                break
            out += part
    except (socket.timeout, ConnectionResetError):
        pass
    sock.close()
    body = out.partition(b"\r\n\r\n")[2]
    line = gw.log_lines(wait_for=before + 1)[-1]
    return body.count(b"data:"), line.get("gateway_error")


def test_event_budget_outcome_independent_of_splits(gateway_factory, upstream):
    big = b'data: {"t":"' + b"y" * 3000 + b'"}\n\n'
    small = b'data: {"t":"ok"}\n\n'
    stream = small + big + small
    results = {}
    for limit in (2048, 1 << 20):
        gw = gateway_factory(max_sse_event_bytes=1 << 20, max_buffered_bytes=1 << 20, max_request_bytes=1024)
        gw.server.state.budget.limit = limit  # leave this much of the shared budget
        outcomes = set()
        for split in (1, 7, 200, 3001, len(stream)):
            for slow in (False, True):
                outcomes.add(run_stream(gw, upstream, stream, split, slow))
        results[limit] = outcomes
        gw.server.shutdown()
        gw.server.server_close()
    # Too little budget for the big event: always refused, the big event is
    # never sent. Every byte held is charged from arrival, so whether the small
    # event before it went out depends on whether it arrived in the same read.
    assert {error for _, error in results[2048]} == {"buffer_limit"}
    assert {delivered for delivered, _ in results[2048]} <= {0, 1}
    # Enough budget: always delivered whole.
    assert results[1 << 20] == {(3, None)}


def test_complete_event_before_an_oversized_tail_is_delivered(gateway_factory, upstream):
    gw = gateway_factory(max_sse_event_bytes=1024)
    stream = b'data: {"t":"first"}\n\n' + b"data: " + b"z" * 2000
    delivered, error = run_stream(gw, upstream, stream, len(stream), False)
    assert (delivered, error) == (1, "sse_event_too_large")


# -- 6: refusals before 100 Continue, and draining before close -----------------------------------


def test_over_limit_with_expect_gets_no_continue(gateway_factory, upstream):
    gw = gateway_factory(max_request_bytes=1024)
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 5000\r\n\r\n")
    reply = sock.recv(4096)
    sock.close()
    assert reply.startswith(b"HTTP/1.1 413")
    assert b"100 Continue" not in reply


def test_within_limit_with_expect_gets_continue(gateway, upstream):
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 2\r\nConnection: close\r\n\r\n")
    assert sock.recv(100).startswith(b"HTTP/1.1 100 Continue")
    sock.sendall(b"{}")
    out = b""
    while True:
        part = sock.recv(4096)
        if not part:
            break
        out += part
    sock.close()
    assert out.startswith(b"HTTP/1.1 200")


def test_early_refusal_is_read_not_reset(gateway_factory, upstream):
    gw = gateway_factory(max_request_bytes=1024)
    got = []
    for _ in range(20):
        sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
        sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 4000000\r\n\r\n")
        try:
            for _ in range(40):
                sock.sendall(b"x" * 65536)  # keeps sending while the gateway refuses
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            got.append(sock.recv(100)[:12])
        except ConnectionResetError:
            got.append(b"reset")
        sock.close()
    assert got == [b"HTTP/1.1 413"] * 20


# -- 8, 9, 10: usage and decoding details -----------------------------------------------------------


def test_terminal_event_without_final_blank_line_is_not_counted(gateway, upstream):
    usage = {"input_tokens": 5, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}}
    stream = b'data: {"type":"response.created","response":{}}\n\n' + b"data: " + json.dumps(
        {"type": "response.completed", "response": {"usage": usage}}
    ).encode() + b"\n"

    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(stream)
        handler.end_chunked()

    upstream.behavior = behavior
    _, data = gateway.request("POST", "/openai/v1/responses", body=b"{}")
    assert data == stream  # relayed as sent
    line = gateway.log_lines()[-1]
    assert line["usage_status"] == "unavailable"


def test_two_member_gzip_across_output_boundary(tmp_path):
    first = b"x" * (2 * PIECE)
    second = b"TAIL-member-two"
    store = BodyStore(str(tmp_path), Redactor([]), max_bytes=4 * PIECE)
    data = gzip.compress(first) + gzip.compress(second)
    name, reason = store.write_request("r1", data, "gzip")
    assert reason is None
    assert (tmp_path / "bodies" / name).read_bytes() == first + second


def test_identity_body_over_cap_short_circuits(tmp_path):
    store = BodyStore(str(tmp_path), Redactor([]), max_bytes=1024)
    name, reason = store.write_request("r2", b"x" * 5000, None)
    assert reason == "too_large"
    assert (tmp_path / "bodies" / name).read_bytes() == MARKER


def test_explicit_null_counter_makes_usage_partial():
    parser = UsageParser("anthropic", "anthropic_messages", streaming=True)
    parser.event(b'data: {"type":"message_start","message":{"usage":' + json.dumps(A_USAGE).encode() + b"}}\n\n")
    parser.event(b'data: {"type":"message_delta","usage":{"output_tokens":null}}\n\n')
    parser.event(b'data: {"type":"message_stop"}\n\n')
    status, error, tokens = parser.status(True)
    assert (status, tokens["output_tokens"]) == ("partial", None)
    assert "output_tokens" in error


def test_mcp_list_tools_is_server_side():
    parser = UsageParser("openai", "openai_responses", streaming=True)
    parser.event(b'data: {"type":"response.output_item.added","item":{"type":"mcp_list_tools"}}\n\n')
    assert parser.server_tool_use is True


# -- 11: config type errors ----------------------------------------------------------------------


def test_config_type_errors_never_show_the_value(tmp_path):
    from gateway.config import ConfigError, load_config

    for bad in ({"upstream": ["https://hunter2-secret.example"]}, {"upstream": {"u": "hunter2-secret"}}):
        doc = {"listen": {"prefix_routes": True}, "routes": [{"prefix": "/a", "credential": {"header": "x-api-key"},
                                                               "allow": [{"method": "POST", "path": "/v1"}], **bad}]}
        path = tmp_path / "c.json"
        path.write_text(json.dumps(doc))
        with pytest.raises(ConfigError) as caught:
            load_config(path, env={})
        assert "hunter2" not in str(caught.value)
        assert "upstream must be a non-empty string" in str(caught.value)


# -- 16: reaper handoff ------------------------------------------------------------------------------


def test_disarm_after_deadline_counts_as_reaped():
    from gateway.proxy import Reaper

    class Owner:
        reaped = False

    reaper = Reaper()
    try:
        a, b = socket.socketpair()
        owner = Owner()
        reaper.arm(1, a, 0.01, owner)
        time.sleep(0.05)  # past the deadline, likely before the next sweep
        reaper.disarm(1)
        assert owner.reaped is True
        a.close()
        b.close()
    finally:
        reaper.stop()


# -- 17: withholding reasons in the log and the meta file ----------------------------------------------


def test_withholding_reasons_in_log_and_meta(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    upstream.behavior = lambda h, s: h.reply(200, b'{"t":"' + ESCAPED.encode() + b'"}')
    gw.request("POST", "/anthropic/v1/messages", body=b"\x00\x01", headers={"Content-Encoding": "br"})
    line = gw.log_lines()[-1]
    bodies = gw.log_dir / "bodies"
    meta = json.loads((bodies / f"{line['id']}.meta.json").read_text())
    assert (line["request_body_withheld"], meta["request_body"]) == ("content_encoding_not_decoded", "not stored: content_encoding_not_decoded")
    # The response was refused before anything was relayed: still a response file.
    assert (line["response_body_withheld"], meta["response_body"]) == ("response_withheld", "not stored: response_withheld")
    assert (bodies / f"{line['id']}.response").read_bytes() == MARKER


def test_no_response_reason(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    for route in gw.server.config.routes:
        route.upstream = f"http://127.0.0.1:{port}"
    gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    line = gw.log_lines()[-1]
    assert line["response_body_withheld"] == "no_response"
    assert line["upstream_sent"] is False
