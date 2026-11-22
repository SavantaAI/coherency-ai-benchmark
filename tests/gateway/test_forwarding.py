"""Routing, credential handling, and pass-through of headers and bodies."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading

from .conftest import ANTHROPIC_KEY, OPENAI_KEY


def test_anthropic_key_injected(gateway, upstream):
    resp, _ = gateway.request("POST", "/anthropic/v1/messages", body=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status == 200
    seen = upstream.seen[-1]
    assert seen.path == "/v1/messages"
    assert seen.all("x-api-key") == [ANTHROPIC_KEY]
    assert seen.all("authorization") == []


def test_openai_bearer_injected_under_base_path(gateway, upstream):
    resp, _ = gateway.request("POST", "/openai/v1/chat/completions?x=1", body=b"{}")
    assert resp.status == 200
    seen = upstream.seen[-1]
    assert seen.path == "/base/v1/chat/completions?x=1"
    assert seen.all("authorization") == [f"Bearer {OPENAI_KEY}"]
    assert seen.all("x-api-key") == []


def test_client_credentials_stripped(gateway, upstream):
    dummy = {"x-api-key": "sk-dummy", "Authorization": "Bearer sk-dummy"}
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}", headers=dummy)
    seen = upstream.seen[-1]
    assert seen.all("x-api-key") == [ANTHROPIC_KEY]
    assert seen.all("authorization") == []

    gateway.request("POST", "/openai/v1/responses", body=b"{}", headers=dummy)
    seen = upstream.seen[-1]
    assert seen.all("authorization") == [f"Bearer {OPENAI_KEY}"]
    assert seen.all("x-api-key") == []
    assert "sk-dummy" not in json.dumps(seen.headers)


def test_headers_and_body_pass_through(gateway, upstream):
    def behavior(handler, seen):
        handler.reply(
            201,
            b"created-body",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("request-id", "req_123"),
                ("anthropic-ratelimit-tokens-remaining", "999"),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
        )

    upstream.behavior = behavior
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "héllo"}]}).encode()
    sent = {
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/9.9.9 (external, cli)",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "beta-one,beta-two",
        "x-stainless-retry-count": "0",
        "X-Custom-Thing": "kept as is",
        "Accept-Encoding": "gzip, br",
        "Host": "attacker.example",
    }
    resp, data = gateway.request("POST", "/anthropic/v1/messages?beta=true", body=body, headers=sent)
    seen = upstream.seen[-1]
    assert seen.body == body
    assert seen.path == "/v1/messages?beta=true"
    for name, value in sent.items():
        if name in ("Host", "Accept-Encoding"):
            continue
        assert seen.all(name) == [value], name
    # Responses are always requested uncompressed, so they can be screened.
    assert seen.all("accept-encoding") == ["identity"]
    assert gateway.log_lines()[-1]["client_accept_encoding"] == "gzip, br"
    # The client's Host header is never followed.
    assert seen.all("host") == [f"127.0.0.1:{upstream.server_address[1]}"]
    assert seen.header("content-length") == str(len(body))

    assert resp.status == 201
    assert data == b"created-body"
    assert resp.getheader("request-id") == "req_123"
    assert resp.getheader("anthropic-ratelimit-tokens-remaining") == "999"
    assert resp.msg.get_all("Set-Cookie") == ["a=1", "b=2"]
    assert resp.getheader("Content-Type") == "text/plain; charset=utf-8"


def test_upstream_error_body_passes_through(gateway, upstream):
    error = (
        b'{"type":"error","error":{"type":"invalid_request_error",'
        b'"message":"max_tokens: Field required"},"request_id":"req_abc"}'
    )

    def behavior(handler, seen):
        handler.reply(400, error, [("Content-Type", "application/json"), ("x-should-retry", "false")])

    upstream.behavior = behavior
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b'{"model":"m"}')
    assert resp.status == 400
    assert data == error
    assert resp.getheader("x-should-retry") == "false"
    line = gateway.log_lines()[-1]
    assert line["status"] == 400
    assert line["usage"] is None
    assert line["usage_status"] == "unavailable"
    # Usage was not expected from an error, so no usage_error either.
    assert "usage_error" not in line


def test_rate_limit_status_and_headers(gateway, upstream):
    def behavior(handler, seen):
        handler.reply(429, b'{"type":"error"}', [("Content-Type", "application/json"), ("retry-after", "7")])

    upstream.behavior = behavior
    resp, data = gateway.request("POST", "/openai/v1/chat/completions", body=b"{}")
    assert (resp.status, resp.getheader("retry-after"), data) == (429, "7", b'{"type":"error"}')


def test_unknown_route_refused(gateway, upstream):
    for path in ("/", "/other/v1/messages", "/anthropicx/v1/messages", "/v1/messages"):
        resp, data = gateway.request("POST", path, body=b"{}")
        assert resp.status == 404, path
        assert json.loads(data) == {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}}
        assert resp.getheader("Server") is None
    assert upstream.seen == []
    assert {x["gateway_error"] for x in gateway.log_lines(4)} == {"unknown_route"}


def test_dot_segments_refused(gateway, upstream):
    resp, _ = gateway.request("GET", "/anthropic/../openai/v1/models")
    assert resp.status == 400
    assert upstream.seen == []


def _raw(gateway, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", gateway.port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    return b"".join(chunks)


def test_not_a_forward_proxy(gateway, upstream):
    out = _raw(gateway, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400")
    out = _raw(gateway, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 405")
    assert upstream.seen == []


def test_route_without_key_is_inactive(gateway_factory, upstream):
    gw = gateway_factory(openai_key=False)
    resp, data = gw.request("POST", "/openai/v1/chat/completions", body=b"{}")
    # An inactive route does not exist as far as clients can tell.
    assert resp.status == 404
    assert upstream.seen == []
    resp, data = gw.request("GET", "/healthz")
    assert json.loads(data)["routes"] == {"anthropic": "active"}
    # The other route still works.
    resp, _ = gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 200


def test_health(gateway, upstream):
    resp, data = gateway.request("GET", "/healthz")
    assert resp.status == 200
    payload = json.loads(data)
    assert payload == {"status": "ok", "routes": {"anthropic": "active", "openai": "active"}}
    assert upstream.seen == []
    assert gateway.request("POST", "/healthz", body=b"{}")[0].status == 405
    assert gateway.request("GET", "/healthz", body=b"x")[0].status == 400


def test_large_request_body(gateway, upstream):
    def behavior(handler, seen):
        handler.reply(200, hashlib.sha256(seen.body).hexdigest().encode(), [("Content-Type", "text/plain")])

    upstream.behavior = behavior
    body = os.urandom(24 * 1024 * 1024)
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=body, headers={"Content-Type": "application/octet-stream"}, timeout=60)
    assert resp.status == 200
    assert data.decode() == hashlib.sha256(body).hexdigest()
    assert gateway.log_lines()[-1]["request_bytes"] == len(body)


def test_request_over_limit_refused(gateway_factory, upstream):
    gw = gateway_factory(max_request_bytes=1024)
    resp, _ = gw.request("POST", "/anthropic/v1/messages", body=b"x" * 2048)
    assert resp.status == 413
    assert upstream.seen == []


def test_chunked_request_body(gateway, upstream):
    def behavior(handler, seen):
        handler.reply(200, seen.body)

    upstream.behavior = behavior
    conn = gateway.conn()
    conn.request("POST", "/anthropic/v1/messages", body=iter([b'{"model":', b'"m1"}']), encode_chunked=True)
    resp = conn.getresponse()
    assert resp.read() == b'{"model":"m1"}'
    assert upstream.seen[-1].header("content-length") == "14"
    assert upstream.seen[-1].header("transfer-encoding") is None
    assert gateway.log_lines()[-1]["model"] == "m1"


def test_keep_alive_reuses_client_connection(gateway, upstream):
    conn = gateway.conn()
    conn.connect()
    first = conn.sock
    for _ in range(3):
        conn.request("POST", "/anthropic/v1/messages", body=b"{}")
        resp = conn.getresponse()
        assert resp.status == 200
        assert not resp.will_close
        resp.read()
        # http.client would reconnect silently; the socket must be the same one.
        assert conn.sock is first
    conn.close()
    assert len(upstream.seen) == 3


def test_concurrent_requests(gateway, upstream):
    n = 8
    # Generous limits: on a loaded machine eight handler threads can take
    # several seconds to all arrive; the barrier still proves concurrency.
    barrier = threading.Barrier(n, timeout=45)

    def behavior(handler, seen):
        barrier.wait()  # only passes when all n requests are upstream at once
        handler.reply(200, seen.body)

    upstream.behavior = behavior
    results = [None] * n

    def worker(i):
        resp, data = gateway.request("POST", "/anthropic/v1/messages", body=str(i).encode(), timeout=60)
        results[i] = (resp.status, data)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)
    assert results == [(200, str(i).encode()) for i in range(n)]


def test_slow_upstream_is_not_cut_off(gateway_factory, upstream):
    # The client idle timeout is 1 s, the upstream takes 2.5 s to answer.
    gw = gateway_factory(client_idle_timeout_seconds=1)

    def behavior(handler, seen):
        import time

        time.sleep(2.5)
        handler.reply(200, b'{"done":true}')

    upstream.behavior = behavior
    resp, data = gw.request("POST", "/anthropic/v1/messages", body=b"{}", timeout=20)
    assert (resp.status, data) == (200, b'{"done":true}')


def test_upstream_unreachable(gateway_factory, upstream, tmp_path):
    gw = gateway_factory()
    # Point the route at a closed port.
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    for route in gw.server.config.routes:
        route.upstream = f"http://127.0.0.1:{port}"
    resp, data = gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert resp.status == 502
    assert json.loads(data) == {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}
    line = gw.log_lines()[-1]
    assert (line["gateway_error"], line["route"]) == ("upstream_unreachable", "anthropic")
    resp, data = gw.request("POST", "/openai/v1/chat/completions", body=b"{}")
    assert resp.status == 502
    # The OpenAI route answers in OpenAI's error shape.
    assert json.loads(data) == {"error": {"message": "Internal server error", "type": "server_error", "param": None, "code": None}}


def test_allowlist_limits_method_and_path(gateway_factory, upstream):
    gw = gateway_factory(
        allow=[
            {"method": "POST", "path": "/v1/messages", "match": "exact"},
            {"method": "GET", "path": "/v1/models", "match": "prefix"},
        ]
    )
    assert gw.request("POST", "/anthropic/v1/messages?beta=true", body=b"{}")[0].status == 200
    assert gw.request("GET", "/anthropic/v1/models/claude-x")[0].status == 200
    refused = [
        ("POST", "/anthropic/v1/messages/count_tokens"),
        ("GET", "/anthropic/v1/messages"),
        ("GET", "/anthropic/v1/files"),
        ("POST", "/anthropic/v1/messagesx"),
        ("GET", "/anthropic/v1/modelsx"),
    ]
    for method, path in refused:
        resp, data = gw.request(method, path, body=b"{}" if method == "POST" else None)
        assert resp.status == 403, path
        assert json.loads(data)["error"] == {"type": "permission_error", "message": "Forbidden"}
    assert [s.path for s in upstream.seen] == ["/v1/messages?beta=true", "/v1/models/claude-x"]
    lines = [x for x in gw.log_lines(7) if x.get("gateway_error") == "path_not_allowed"]
    assert len(lines) == 5 and all(x["route"] == "anthropic" for x in lines)
