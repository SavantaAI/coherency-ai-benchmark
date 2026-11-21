"""Streaming delivery and client disconnects."""

from __future__ import annotations

import socket
import threading
import time

FIRST = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"m","usage":'
    b'{"input_tokens":3,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n'
)
USAGE_START = {"input_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1}
REST = (
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_first_event_arrives_before_upstream_finishes(gateway, upstream):
    release = threading.Event()
    finished = threading.Event()

    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(FIRST)
        release.wait(10)
        handler.chunk(REST)
        handler.end_chunked()
        finished.set()

    upstream.behavior = behavior
    conn = gateway.conn()
    conn.request("POST", "/anthropic/v1/messages", body=b'{"model":"m","stream":true}')
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    got = b""
    while b"\n\n" not in got:
        part = resp.read1(65536)
        assert part, "stream ended before the first event"
        got += part
    # The upstream is still blocked, so the event was not buffered to the end.
    assert not finished.is_set()
    assert got == FIRST
    release.set()
    got += resp.read()
    assert got == FIRST + REST
    assert finished.wait(5)
    line = gateway.log_lines()[-1]
    assert line["stream"] is True
    assert line["request_stream"] is True
    assert line["usage"] == {**USAGE_START, "output_tokens": 5}
    assert line["usage_status"] == "complete"
    assert line["first_body_ms"] < line["duration_ms"]
    assert line["response_bytes"] == len(FIRST + REST)


def _send_request(gateway, body: bytes) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=10)
    sock.sendall(
        b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: gateway\r\nContent-Type: application/json\r\n"
        b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
    )
    return sock


def test_client_disconnect_mid_stream(gateway, upstream):
    closed_seen = threading.Event()
    sent_events = []

    def behavior(handler, seen):
        handler.start_chunked()
        try:
            for i in range(2000):
                handler.chunk(b'event: ping\ndata: {"type":"ping"}\n\n')
                sent_events.append(i)
                if handler.peer_closed():
                    break
                time.sleep(0.02)
        except OSError:
            pass
        closed_seen.set()

    upstream.behavior = behavior
    baseline = threading.active_count()
    sock = _send_request(gateway, b'{"model":"m","stream":true}')
    data = sock.recv(65536)
    assert data.startswith(b"HTTP/1.1 200")
    sock.close()
    # The gateway notices, closes its upstream connection, and frees the request.
    assert closed_seen.wait(5), "gateway kept the upstream connection open"
    assert len(sent_events) < 500
    assert wait_until(lambda: gateway.server.inflight == 0)
    line = gateway.log_lines()[-1]
    assert line["client_disconnected"] is True
    assert line["stream"] is True
    assert line["usage_status"] == "unavailable"
    assert wait_until(lambda: threading.active_count() <= baseline + 1, timeout=5)


def test_client_disconnect_before_upstream_answers(gateway, upstream):
    closed_seen = threading.Event()

    def behavior(handler, seen):
        # A long silent wait, as with a slow non-streaming request.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if handler.peer_closed():
                closed_seen.set()
                return
            time.sleep(0.02)

    upstream.behavior = behavior
    sock = _send_request(gateway, b'{"model":"m"}')
    assert wait_until(lambda: len(upstream.seen) == 1)
    sock.close()
    assert closed_seen.wait(5), "gateway did not abort the upstream request"
    assert wait_until(lambda: gateway.server.inflight == 0)
    line = gateway.log_lines()[-1]
    assert line["client_disconnected"] is True
    assert line["status"] is None


def test_upstream_dies_mid_stream(gateway, upstream):
    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(FIRST)
        # Close without the terminating chunk.
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)

    upstream.behavior = behavior
    sock = _send_request(gateway, b'{"model":"m","stream":true}')
    got = b""
    while True:
        part = sock.recv(65536)
        if not part:
            break
        got += part
    sock.close()
    assert FIRST in got
    # The gateway cut the connection instead of sending a clean end of stream.
    assert not got.endswith(b"0\r\n\r\n")
    line = gateway.log_lines()[-1]
    assert "upstream_error" in line
    # The counts seen so far are kept, but marked as not the final ones.
    assert line["usage"] == USAGE_START
    assert line["usage_status"] == "partial"
