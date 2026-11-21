"""Request and response framing, target canonicalisation, refusals, limits."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time

import pytest



def raw(gateway, data: bytes, timeout: float = 5) -> bytes:
    """Send bytes, read until the gateway closes (or the timeout)."""
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


POST = b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\n"


# -- B: target canonicalisation ------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        b"/anthropic/v1/messages/%2e%2e/files",
        b"/anthropic/v1/messages/%2E%2E/files",
        b"/anthropic/v1%2fmessages",
        b"/anthropic/v1%5cmessages",
        b"/anthropic/v1/%6dessages",
        b"//anthropic/v1/models",
        b"///anthropic/v1/models",
        b"/anthropic//v1/models",
        b"/anthropic/v1/models/",
        b"/anthropic/./v1/models",
        b"/anthropic/v1/../v1/models",
        b"/anthropic/v1\\models",
        b"/anthropic/v1/models#x",
        b"*",
        b"http://example.com/anthropic/v1/models",
    ],
)
def test_ambiguous_targets_refused(gateway, upstream, target):
    reply = raw(gateway, b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status_of(reply) == 400, reply[:80]
    assert upstream.seen == []
    assert gateway.log_lines()[-1]["gateway_error"] in ("invalid_path", "invalid_request_target")


def test_raw_target_forwarded_unchanged(gateway, upstream):
    reply = raw(gateway, b"GET /anthropic/v1/models? HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert status_of(reply) == 200
    reply = raw(gateway, b"GET /anthropic/v1/models?a=%2F&b= HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert status_of(reply) == 200
    assert [s.path for s in upstream.seen] == ["/v1/models?", "/v1/models?a=%2F&b="]


# -- C: request framing ----------------------------------------------------------


@pytest.mark.parametrize(
    "head_and_body",
    [
        b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
        b"Content-Length: +2\r\n\r\n{}",
        b"Content-Length: 0x2\r\n\r\n{}",
        b"Content-Length: -1\r\n\r\n{}",
        b"Transfer-Encoding: gzip, chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: identity\r\n\r\n{}",
        b"Transfer-Encoding: chunked\r\n\r\n-1\r\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n+2\r\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n0x2\r\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}GARB\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n2\n{}\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\nX: " + b"a" * 9000 + b"\r\n\r\n",
    ],
)
def test_bad_request_framing_refused_and_closed(gateway, upstream, head_and_body):
    reply = raw(gateway, POST + head_and_body)
    assert status_of(reply) == 400, reply[:120]
    assert b"Connection: close" in reply
    assert upstream.seen == []


def test_chunked_with_extension_and_trailer_accepted(gateway, upstream):
    upstream.behavior = lambda h, s: h.reply(200, s.body)
    body = b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n2;ext=1\r\n{}\r\n0\r\nX-T: 1\r\n\r\n"
    reply = raw(gateway, POST + body)
    assert status_of(reply) == 200
    assert reply.endswith(b"{}")
    assert upstream.seen[-1].header("content-length") == "2"


def test_negative_chunk_cannot_bypass_size_limit(gateway_factory, upstream):
    gw = gateway_factory(max_request_bytes=16)
    reply = raw(gw, POST + b"Transfer-Encoding: chunked\r\n\r\n20\r\n" + b"x" * 32 + b"\r\n0\r\n\r\n")
    assert status_of(reply) == 413
    assert upstream.seen == []


# -- D: response framing and interim responses ------------------------------------


def test_short_fixed_length_response_cuts_client(gateway, upstream):
    def behavior(handler, seen):
        handler.send_response_only(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b'{"partial":')
        handler.wfile.flush()
        handler.close_connection = True

    upstream.behavior = behavior
    start = time.monotonic()
    reply = raw(gateway, POST + b"Content-Length: 2\r\n\r\n{}", timeout=10)
    # A buffered body that ends short is never relayed; the client gets 502.
    assert time.monotonic() - start < 5
    assert status_of(reply) == 502
    assert b"partial" not in reply
    line = gateway.log_lines()[-1]
    assert (line["upstream_error"], line["gateway_error"]) == ("truncated", "upstream_truncated")


def test_short_fixed_length_stream_cuts_client(gateway, upstream):
    def behavior(handler, seen):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 500\r\n\r\ndata: {}\n\n")
        handler.wfile.flush()
        handler.close_connection = True

    upstream.behavior = behavior
    reply = raw(gateway, POST + b"Content-Length: 2\r\n\r\n{}", timeout=10)
    assert reply.endswith(b"data: {}\n\n")
    assert gateway.log_lines()[-1]["upstream_error"] == "truncated"


@pytest.mark.parametrize("te", ["gzip, chunked", "chunked, chunked", "identity", "x-custom"])
def test_upstream_transfer_codings_other_than_chunked_refused(gateway, upstream, te):
    def behavior(handler, seen):
        handler.wfile.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: {te}\r\n\r\n".encode())
        handler.wfile.write(b"2\r\n{}\r\n0\r\n\r\n")
        handler.wfile.flush()
        handler.close_connection = True

    upstream.behavior = behavior
    resp, _ = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 502
    assert gateway.log_lines()[-1]["gateway_error"] == "upstream_bad_framing"


def test_chunked_upstream_with_content_length_is_rechunked(gateway, upstream):
    def behavior(handler, seen):
        handler.send_response_only(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "999")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.chunk(b'{"a":1}')
        handler.end_chunked()

    upstream.behavior = behavior
    reply = raw(gateway, POST + b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
    head, _, body = reply.partition(b"\r\n\r\n")
    assert b"Content-Length" not in head
    assert b"Transfer-Encoding: chunked" in head
    assert body == b'7\r\n{"a":1}\r\n0\r\n\r\n'


def test_early_hints_skipped_and_final_response_relayed(gateway, upstream):
    def behavior(handler, seen):
        handler.wfile.write(b"HTTP/1.1 103 Early Hints\r\nLink: </x>; rel=preload\r\n\r\n")
        handler.reply(200, b'{"final":true}')

    upstream.behavior = behavior
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert (resp.status, data) == (200, b'{"final":true}')
    assert gateway.log_lines()[-1]["interim_statuses"] == [103]


def test_conflicting_upstream_lengths_refused(gateway, upstream):
    def behavior(handler, seen):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}")

    upstream.behavior = behavior
    resp, _ = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 502
    assert gateway.log_lines()[-1]["gateway_error"] == "upstream_bad_framing"


# -- encodings ---------------------------------------------------------------------


def test_upstream_always_asked_for_identity(gateway, upstream):
    for offered in ("gzip, deflate, br, zstd", "br"):
        gateway.request("POST", "/anthropic/v1/messages", body=b"{}", headers={"Accept-Encoding": offered})
        assert upstream.seen[-1].all("accept-encoding") == ["identity"]
    raw(gateway, POST + b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")  # no Accept-Encoding at all
    assert upstream.seen[-1].all("accept-encoding") == ["identity"]
    lines = gateway.log_lines(3)
    assert [x["client_accept_encoding"] for x in lines] == ["gzip, deflate, br, zstd", "br", "(none)"]


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "zstd"])
def test_coded_response_body_refused(gateway, upstream, encoding):
    payload = gzip.compress(b'{"a":1}')
    upstream.behavior = lambda h, s: h.reply(200, payload, [("Content-Type", "application/json"), ("Content-Encoding", encoding)])
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 502
    assert payload not in data
    assert gateway.log_lines()[-1]["gateway_error"] == "upstream_encoding_not_screenable"


def test_bodyless_response_passes_whatever_its_encoding(gateway, upstream):
    def behavior(handler, seen):
        handler.wfile.write(b'HTTP/1.1 304 Not Modified\r\nContent-Encoding: br\r\nETag: "x"\r\n\r\n')

    upstream.behavior = behavior
    resp, _ = gateway.request("GET", "/anthropic/api/claude_code/policy_limits")
    assert resp.status == 304
    assert resp.getheader("Content-Encoding") == "br"


# -- J: refusals are JSON, logged, with fresh identity ---------------------------------


def test_unsupported_and_malformed_requests_are_json_and_logged(gateway, upstream):
    replies = [
        raw(gateway, b"BREW /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\n\r\n"),
        raw(gateway, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"),
        raw(gateway, b"GET /anthropic/v1/messages HTTP/9.9\r\nHost: x\r\n\r\n"),
        raw(gateway, b"garbage\r\n\r\n"),
        raw(gateway, b"GET /anthropic/v1/models HTTP/1.1\r\n\r\n"),
        raw(gateway, b"GET /anthropic/v1/models HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n"),
    ]
    for reply in replies:
        head, _, body = reply.partition(b"\r\n\r\n")
        assert status_of(reply) >= 400, reply[:80]
        assert b"Server:" not in head
        assert b"Python" not in reply and b"gateway" not in reply.lower()
        assert json.loads(body)["type"] == "error"
    lines = gateway.log_lines(len(replies))
    assert len(lines) == len(replies)
    assert len({x["id"] for x in lines}) == len(replies)
    assert all(x["id"] and x["gateway_error"] for x in lines)
    assert upstream.seen == []


def test_connect_on_kept_alive_connection_gets_new_id(gateway, upstream):
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(b"GET /anthropic/v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.2)
    sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    time.sleep(0.2)
    sock.close()
    lines = gateway.log_lines(2)
    assert lines[0]["id"] != lines[1]["id"]
    assert lines[1]["method"] == "CONNECT" and lines[1]["status"] == 405


# -- F: resource bounds ------------------------------------------------------------------


def test_connection_cap(gateway_factory, upstream):
    gw = gateway_factory(max_connections=2)
    held = [socket.create_connection(("127.0.0.1", gw.port), timeout=5) for _ in range(2)]
    time.sleep(0.2)
    extra = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        assert extra.recv(100) == b""
    except ConnectionResetError:
        pass
    for s in held:
        s.close()
    time.sleep(0.3)
    resp, _ = gw.request("GET", "/healthz")
    assert resp.status == 200
    assert any(x.get("gateway_error") == "connection_limit" for x in gw.log_lines(1))


def test_slow_upload_cut_at_deadline(gateway_factory, upstream):
    gw = gateway_factory(client_read_timeout_seconds=1)
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    sock.sendall(POST + b"Content-Length: 1000\r\n\r\n")
    start = time.monotonic()
    try:
        for _ in range(100):
            sock.sendall(b"x")
            time.sleep(0.05)
    except OSError:
        pass
    assert time.monotonic() - start < 4
    sock.close()
    assert upstream.seen == []
    assert gw.log_lines()[-1]["gateway_error"] == "client_read_timeout"


def _closes_after(gw) -> float:
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    start = time.monotonic()
    try:
        assert sock.recv(10) == b""
    except ConnectionResetError:
        pass
    sock.close()
    return time.monotonic() - start


def test_idle_connection_closes_after_idle_timeout(gateway_factory, upstream):
    # The documented rule: a connection with no request bytes closes after
    # client_idle_timeout_seconds; the read deadline starts with a request.
    gw = gateway_factory(client_idle_timeout_seconds=0.5, client_read_timeout_seconds=30)
    assert 0.4 < _closes_after(gw) < 2


def test_read_deadline_does_not_close_an_idle_connection(gateway_factory, upstream):
    gw = gateway_factory(client_idle_timeout_seconds=1.5, client_read_timeout_seconds=0.3)
    assert _closes_after(gw) > 1.2


def test_read_deadline_counts_from_first_request_byte(gateway_factory, upstream):
    gw = gateway_factory(client_idle_timeout_seconds=5, client_read_timeout_seconds=0.6)
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    time.sleep(1.0)  # idle longer than the read deadline
    sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    assert sock.recv(100).startswith(b"HTTP/1.1 200")
    sock.close()


# -- E: a connection-closing upstream response can still be cancelled ---------------------


def test_disconnect_during_close_delimited_response(gateway, upstream):
    closed = threading.Event()

    def behavior(handler, seen):
        # No length, no chunking: the body ends when the connection closes.
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        handler.wfile.write(b"data: {}\n\n")
        handler.wfile.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if handler.peer_closed():
                closed.set()
                return
            time.sleep(0.02)

    upstream.behavior = behavior
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(POST + b"Content-Length: 2\r\n\r\n{}")
    assert sock.recv(4096)
    sock.close()
    assert closed.wait(5), "upstream socket was not released"
    assert gateway.log_lines()[-1]["client_disconnected"] is True
