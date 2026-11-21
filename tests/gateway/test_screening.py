"""Response screening: whole units, every status, head and body."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from gateway.records import MARKER
from gateway.screen import EventSplitter, Screen

from .conftest import ANTHROPIC_KEY

ESCAPED = "".join("\\u%04x" % ord(c) for c in ANTHROPIC_KEY)
POST = b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}"


def raw(gateway, data: bytes, timeout: float = 5) -> bytes:
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


def stream(*chunks: bytes, pause: float = 0.05):
    def behavior(handler, seen):
        handler.start_chunked()
        for chunk in chunks:
            handler.chunk(chunk)
            time.sleep(pause)
        handler.end_chunked()

    return behavior


# -- bodies ------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["literal", "escaped"])
@pytest.mark.parametrize("status", [200, 400, 401, 500])
def test_key_in_buffered_body_refused(gateway_factory, upstream, form, status):
    gw = gateway_factory(store_bodies=True)
    key = ANTHROPIC_KEY if form == "literal" else ESCAPED
    body = ('{"type":"error","error":{"message":"bad key %s"}}' % key).encode()
    upstream.behavior = lambda h, s: h.reply(status, body)
    reply = raw(gw, POST)
    assert reply.startswith(b"HTTP/1.1 502")
    assert ANTHROPIC_KEY.encode() not in reply and ESCAPED.encode() not in reply
    line = gw.log_lines()[-1]
    assert (line["gateway_error"], line["upstream_status"]) == ("credential_in_response", status)
    for path in (gw.log_dir / "bodies").iterdir():
        assert ANTHROPIC_KEY.encode() not in path.read_bytes()


def test_clean_error_body_passes_byte_identical(gateway, upstream):
    body = b'{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens: Field required"}}'
    upstream.behavior = lambda h, s: h.reply(400, body)
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert (resp.status, data) == (400, body)


def test_large_body_under_cap_byte_identical(gateway_factory, upstream):
    gw = gateway_factory(max_response_body_bytes=12 * 1024 * 1024)
    body = json.dumps({"type": "message", "content": [{"type": "text", "text": "x" * (10 * 1024 * 1024)}]}).encode()
    upstream.behavior = lambda h, s: h.reply(200, body)
    resp, data = gw.request("POST", "/anthropic/v1/messages", body=b"{}", timeout=60)
    assert resp.status == 200
    assert resp.getheader("Content-Length") == str(len(body))
    assert data == body


def test_body_over_cap_refused(gateway_factory, upstream):
    gw = gateway_factory(max_response_body_bytes=4096)
    upstream.behavior = lambda h, s: h.reply(200, b'{"x":"' + b"y" * 5000 + b'"}')
    resp, _ = gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 502
    assert gw.log_lines()[-1]["gateway_error"] == "response_too_large"


def test_chunked_body_keeps_chunked_framing(gateway, upstream):
    upstream.behavior = stream(b'{"a":', b"1}", pause=0.01)
    # stream() sends text/event-stream; send a plain chunked JSON body instead.

    def behavior(handler, seen):
        handler.start_chunked(content_type="application/json")
        handler.chunk(b'{"a":')
        handler.chunk(b"1}")
        handler.end_chunked()

    upstream.behavior = behavior
    reply = raw(gateway, POST.replace(b"Content-Length: 2", b"Connection: close\r\nContent-Length: 2"))
    head, _, body = reply.partition(b"\r\n\r\n")
    assert b"Transfer-Encoding: chunked" in head
    assert body == b'7\r\n{"a":1}\r\n0\r\n\r\n'


# -- events ------------------------------------------------------------------------


def test_escaped_key_split_across_chunks_with_late_terminator(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    clean = b'event: message_start\ndata: {"type":"ping"}\n\n'
    bad = ('event: content_block_delta\ndata: {"text":"%s"}\n' % ESCAPED).encode()
    upstream.behavior = stream(clean, bad[:20], bad[20:], b"\n", b'data: {"after":1}\n\n')
    reply = raw(gw, POST, timeout=10)
    assert reply.count(b'{"type":"ping"}') == 1  # the clean event before it went out
    assert b"content_block_delta" not in reply
    assert ESCAPED.encode() not in reply and ANTHROPIC_KEY.encode() not in reply
    assert b'{"after":1}' not in reply
    line = gw.log_lines()[-1]
    assert line["gateway_error"] == "credential_in_response"
    assert (gw.log_dir / "bodies" / f"{line['id']}.response").read_bytes() == MARKER


def test_key_across_two_data_lines(gateway, upstream):
    # Neither line holds the key; joined as an SSE client joins them, the JSON does.
    event = ('data: {"text":\ndata: "%s"}\n\n' % ESCAPED).encode()
    upstream.behavior = stream(event)
    reply = raw(gateway, POST)
    assert ESCAPED.encode() not in reply
    assert gateway.log_lines()[-1]["gateway_error"] == "credential_in_response"


def test_literal_key_in_event(gateway, upstream):
    clean = b'data: {"n":1}\n\n'
    bad = b": comment " + ANTHROPIC_KEY.encode() + b"\n\n"
    upstream.behavior = stream(clean, bad, b'data: {"n":2}\n\n')
    reply = raw(gateway, POST)
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")  # the stream had started
    assert clean in body  # the event before it went out
    assert b"comment" not in body and ANTHROPIC_KEY.encode() not in reply  # the bad event did not
    assert b'{"n":2}' not in body
    assert not body.endswith(b"0\r\n\r\n")  # no clean end of the chunked body
    assert gateway.log_lines()[-1]["gateway_error"] == "credential_in_response"


def test_event_over_cap_cuts_connection(gateway_factory, upstream):
    gw = gateway_factory(max_sse_event_bytes=4096)
    upstream.behavior = stream(b'data: {"x":"' + b"y" * 3000, b"y" * 3000 + b'"}\n\n')
    reply = raw(gw, POST)
    assert b"yyyy" not in reply
    assert gw.log_lines()[-1]["gateway_error"] == "sse_event_too_large"


def test_events_delivered_one_by_one_before_upstream_finishes(gateway, upstream):
    release = threading.Event()
    # CR-only and CRLF line endings, events split across chunks.
    first = b'event: a\r\ndata: {"n":1}\r\n\r\n'
    second = b'event: b\rdata: {"n":2}\r\r'

    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(first[:9])
        handler.chunk(first[9:] + second[:5])
        release.wait(10)
        handler.chunk(second[5:])
        handler.end_chunked()

    upstream.behavior = behavior
    conn = gateway.conn()
    conn.request("POST", "/anthropic/v1/messages", body=b"{}")
    resp = conn.getresponse()
    got = b""
    while len(got) < len(first):
        part = resp.read1(65536)
        assert part
        got += part
    assert got == first  # the whole first event, nothing of the second
    release.set()
    assert got + resp.read() == first + second


# -- head ---------------------------------------------------------------------------


def head_behavior(status_line: bytes, headers: bytes = b""):
    def behavior(handler, seen):
        handler.wfile.write(status_line + b"\r\nContent-Type: application/json\r\n" + headers + b"Content-Length: 2\r\n\r\n{}")

    return behavior


@pytest.mark.parametrize(
    "status_line,headers",
    [
        (b"HTTP/1.1 401 " + ANTHROPIC_KEY.encode(), b""),
        (b"HTTP/1.1 200 OK", b"x-" + ANTHROPIC_KEY.encode() + b": 1\r\n"),
        (b"HTTP/1.1 200 OK", b"x-echo: " + ANTHROPIC_KEY.encode() + b"\r\n"),
    ],
    ids=["status reason", "header name", "header value"],
)
def test_key_in_response_head_refused(gateway, upstream, status_line, headers):
    upstream.behavior = head_behavior(status_line, headers)
    reply = raw(gateway, POST)
    assert reply.startswith(b"HTTP/1.1 502")
    assert ANTHROPIC_KEY.encode() not in reply
    assert gateway.log_lines()[-1]["gateway_error"] == "credential_in_response_head"


# -- the building blocks ------------------------------------------------------------


def test_event_splitter_line_endings():
    sp = EventSplitter(1 << 20)
    assert sp.feed(b"data: 1\n\ndata: 2\r\n\r\ndata: 3\r\r") == [b"data: 1\n\n", b"data: 2\r\n\r\n"]
    # "\r\r" at the end may yet be "\r\r\n": the event is complete once the next byte shows which.
    assert sp.feed(b"d") == [b"data: 3\r\r"]
    # A trailing CR waits for the next byte: CR LF CR LF is one blank line, not two.
    assert sp.feed(b"ata: 4\r\n\r") == []
    assert sp.feed(b"\n") == [b"data: 4\r\n\r\n"]
    assert sp.feed(b"data: 5\n") == []
    assert sp.finish() == ([], b"data: 5\n")


def test_event_splitter_pending_cr_resolved_at_end():
    sp = EventSplitter(1 << 20)
    assert sp.feed(b"data: x\r\r") == []
    # At end of stream the held CR is a line break, so the event is whole.
    assert sp.finish() == ([b"data: x\r\r"], b"")


def test_event_splitter_state_reset_after_finish():
    sp = EventSplitter(1 << 20)
    # Leave the scan position deep inside a long partial line, then finish.
    sp.feed(b"data: " + b"y" * 500 + b"\ndata: z")
    assert sp.finish() == ([], b"data: " + b"y" * 500 + b"\ndata: z")
    # Stale scan state would skip past this short event's blank line.
    assert sp.feed(b"a\n\n") == [b"a\n\n"]


def test_screen_forms():
    screen = Screen([ANTHROPIC_KEY])
    assert screen.body(ANTHROPIC_KEY.encode())
    assert screen.body(('{"a":["%s"]}' % ESCAPED).encode())
    assert screen.body(('{"%s":1}' % ESCAPED).encode())
    assert not screen.body(b'{"a":"clean"}')
    assert screen.event(('data: {"a":\ndata: "%s"}\n\n' % ESCAPED).encode())
    assert not screen.event(b'data: {"a":1}\n\n')
    assert screen.head("OK", [("x", ANTHROPIC_KEY)])
