"""Transparent mode: TLS for the upstream hostnames, routing by SNI and Host."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
from pathlib import Path

import pytest

from gateway import tls
from gateway.config import load_config
from gateway.proxy import GatewayServer

from .conftest import ANTHROPIC_KEY, ANY, OPENAI_KEY, FakeUpstream

HOSTS = ["api.anthropic.com", "api.openai.com"]


class TLSUpstream(FakeUpstream):
    """The fake upstream behind TLS, with a certificate for localhost."""

    def __init__(self, context: ssl.SSLContext):
        super().__init__()
        self.context = context

    def get_request(self):
        sock, addr = super().get_request()
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    @property
    def url(self) -> str:
        return f"https://localhost:{self.server_address[1]}"


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    base = tmp_path_factory.mktemp("certs")
    gateway_certs = tls.generate(base / "gateway", HOSTS)
    upstream_certs = tls.generate(base / "upstream", ["localhost"])
    return {"gateway": gateway_certs, "upstream": upstream_certs}


@pytest.fixture
def tls_upstream(certs):
    crt, key = certs["upstream"]["leaves"]["localhost"]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    server = TLSUpstream(ctx)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class Transparent:
    def __init__(self, server: GatewayServer, plain: GatewayServer, log_dir: Path, ca: Path):
        self.server = server
        self.plain = plain
        self.log_dir = log_dir
        self.ca = ca

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def client_context(self) -> ssl.SSLContext:
        # Trusts only the gateway's CA, as the agent's container would.
        return ssl.create_default_context(cafile=str(self.ca))

    def conn(self, hostname: str, timeout: float = 10) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(hostname, self.port, timeout=timeout, context=self.client_context())
        port = self.port
        # Resolve the real hostname to the gateway, like --add-host does.
        conn._create_connection = lambda addr, *a, **k: socket.create_connection(("127.0.0.1", port), *a, **k)
        return conn

    def request(self, hostname, method, path, body=None, headers=None):
        conn = self.conn(hostname)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def log_lines(self, wait_for: int = 1) -> list[dict]:
        import time

        path = self.log_dir / "requests.jsonl"
        for _ in range(250):
            lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
            if len(lines) >= wait_for:
                return lines
            time.sleep(0.02)
        return lines


@pytest.fixture
def transparent_factory(tmp_path, tls_upstream, certs):
    started: list[GatewayServer] = []

    def start(
        *, trust_upstream: bool = True, store_bodies: bool = False, openai_key: bool = True, upstream_host: str = "localhost",
        max_request_bytes: int | None = None,
    ) -> Transparent:
        (tmp_path / "anthropic_key").write_text(ANTHROPIC_KEY)
        if openai_key:
            (tmp_path / "openai_key").write_text(OPENAI_KEY)
        cfg_path = tmp_path / "gateway.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "listen": {"host": "127.0.0.1", "port": 0},
                    "log_dir": str(tmp_path / "logs"),
                    "store_bodies": store_bodies,
                    "transparent": {"enabled": True, "host": "127.0.0.1", "port": 0},
                    **({"max_request_bytes": max_request_bytes} if max_request_bytes else {}),
                    "routes": [
                        {
                            "name": "anthropic",
                            "prefix": "/anthropic",
                            "upstream": tls_upstream.url.replace("localhost", upstream_host),
                            "allow": ANY,
                            "hosts": ["api.anthropic.com"],
                            "credential": {"header": "x-api-key", "key_file": str(tmp_path / "anthropic_key")},
                            "usage": "anthropic",
                        },
                        {
                            "name": "openai",
                            "prefix": "/openai",
                            "upstream": tls_upstream.url.replace("localhost", upstream_host),
                            "allow": ANY,
                            "hosts": ["api.openai.com"],
                            "credential": {
                                "header": "authorization",
                                "format": "Bearer {key}",
                                "key_file": str(tmp_path / "openai_key"),
                            },
                            "usage": "openai",
                        },
                    ],
                }
            )
        )
        config = load_config(cfg_path, env={})
        # The upstream client context: by default the system trust store, which
        # does not know the fake upstream's CA.
        upstream_ctx = ssl.create_default_context()
        if trust_upstream:
            upstream_ctx = ssl.create_default_context(cafile=str(certs["upstream"]["ca.crt"]))
        plain = GatewayServer(config, upstream_ctx)
        leaves = {h: certs["gateway"]["leaves"][h] for h in config.transparent_hosts()}
        server_ctx = tls.server_context(leaves)
        server = GatewayServer(config, tls_context=server_ctx, state=plain.state)
        for s in (plain, server):
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            started.append(s)
        return Transparent(server, plain, tmp_path / "logs", certs["gateway"]["ca.crt"])

    yield start
    for s in started:
        s.shutdown()
        s.server_close()


def _handshake(certs, host, alpn=None):
    """Client handshake against the gateway's TLS context; returns (client socket, server SNI)."""
    ctx = ssl.create_default_context(cafile=str(certs["gateway"]["ca.crt"]))
    if alpn:
        ctx.set_alpn_protocols(alpn)
    sctx = tls.server_context(certs["gateway"]["leaves"])
    a, b = socket.socketpair()
    result = {}

    def serve(sctx, a, result):
        try:
            s = sctx.wrap_socket(a, server_side=True)
            result["sni"] = s.gateway_sni
            result["alpn"] = s.selected_alpn_protocol()
            s.close()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=serve, args=(sctx, a, result))
    t.start()
    c = ctx.wrap_socket(b, server_hostname=host)
    cert = c.getpeercert()
    c.close()
    t.join(5)
    return cert, result


def test_one_leaf_per_host_selected_by_sni(certs):
    # A strict client (Python 3.13+ enables VERIFY_X509_STRICT) accepts each
    # chain, and each leaf names only the host that was asked for.
    for host in HOSTS:
        cert, result = _handshake(certs, host)
        assert result.get("sni") == host
        assert cert["subjectAltName"] == (("DNS", host),)


def test_alpn_offering_h2_gets_http11(certs):
    _cert, result = _handshake(certs, "api.anthropic.com", alpn=["h2", "http/1.1"])
    assert result["alpn"] == "http/1.1"


def test_ca_name_and_file_are_configurable(tmp_path):
    out = tls.generate(tmp_path / "t", ["api.anthropic.com"], ca_name="Corp Root", ca_cert_file="root.pem")
    assert out["ca.crt"].name == "root.pem"
    info = ssl._ssl._test_decode_cert(str(out["ca.crt"]))
    assert dict(x[0] for x in info["subject"])["commonName"] == "Corp Root"


def test_routes_by_host_and_swaps_credential(transparent_factory, tls_upstream):
    gw = transparent_factory()
    body = b'{"model":"claude-x","max_tokens":5}'
    resp, _ = gw.request(
        "api.anthropic.com",
        "POST",
        "/v1/messages?beta=true",
        body=body,
        headers={"x-api-key": "sk-dummy", "anthropic-beta": "a,b", "User-Agent": "client/1"},
    )
    assert resp.status == 200
    seen = tls_upstream.seen[-1]
    assert seen.path == "/v1/messages?beta=true"
    assert seen.all("x-api-key") == [ANTHROPIC_KEY]
    assert seen.all("anthropic-beta") == ["a,b"]
    assert seen.all("user-agent") == ["client/1"]
    assert seen.body == body
    assert seen.all("host") == [f"localhost:{tls_upstream.server_address[1]}"]

    gw.request("api.openai.com", "POST", "/v1/responses", body=b"{}", headers={"Authorization": "Bearer sk-dummy"})
    seen = tls_upstream.seen[-1]
    assert seen.path == "/v1/responses"
    assert seen.all("authorization") == [f"Bearer {OPENAI_KEY}"]

    lines = gw.log_lines(2)
    assert [(x["mode"], x["host"], x["route"], x["model"]) for x in lines] == [
        ("transparent", "api.anthropic.com", "anthropic", "claude-x"),
        ("transparent", "api.openai.com", "openai", None),
    ]


def test_sni_and_host_mismatch_refused(transparent_factory, tls_upstream):
    gw = transparent_factory()
    resp, data = gw.request("api.anthropic.com", "POST", "/v1/messages", body=b"{}", headers={"Host": "api.openai.com"})
    assert resp.status == 421
    assert json.loads(data) == {"type": "error", "error": {"type": "invalid_request_error", "message": "Misdirected request"}}
    assert gw.log_lines()[-1]["gateway_error"] == "misdirected_request"
    resp, _ = gw.request("api.anthropic.com", "GET", "/v1/models", headers={"Host": "example.com"})
    assert resp.status == 421
    resp, _ = gw.request("api.anthropic.com", "GET", "/v1/models", headers={"Host": "api.anthropic.com:8443"})
    assert resp.status == 421
    assert tls_upstream.seen == []
    # Two Host headers, the first one correct.
    conn = gw.conn("api.anthropic.com")
    conn.putrequest("GET", "/v1/models", skip_host=True)
    conn.putheader("Host", f"api.anthropic.com:{gw.port}")
    conn.putheader("Host", "api.openai.com")
    conn.endheaders()
    assert conn.getresponse().status == 400
    assert tls_upstream.seen == []


def test_unknown_host_refused_at_handshake(transparent_factory, tls_upstream):
    gw = transparent_factory()
    with pytest.raises(ssl.SSLError):
        gw.request("example.com", "GET", "/")
    # A client that sends no server name at all.
    raw = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    ctx = ssl._create_unverified_context()
    with pytest.raises(ssl.SSLError):
        ctx.wrap_socket(raw)
    raw.close()
    lines = gw.log_lines(2)
    assert [(x["gateway_error"], x["host"]) for x in lines] == [("tls_unknown_host", "example.com"), ("tls_unknown_host", None)]
    assert tls_upstream.seen == []


def test_plain_http_on_the_tls_port_refused(transparent_factory, tls_upstream):
    gw = transparent_factory()
    with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n")
        try:
            reply = sock.recv(65536)
        except ConnectionResetError:
            reply = b""
        assert not reply.startswith(b"HTTP/")
    assert tls_upstream.seen == []


def test_host_without_key_gets_no_certificate(transparent_factory, tls_upstream):
    gw = transparent_factory(openai_key=False)
    with pytest.raises(ssl.SSLError):
        gw.request("api.openai.com", "POST", "/v1/chat/completions", body=b"{}")
    assert tls_upstream.seen == []
    assert gw.log_lines()[-1]["gateway_error"] == "tls_unknown_host"


def test_plain_listener_serves_only_health(transparent_factory, tls_upstream):
    gw = transparent_factory()
    assert gw.plain.server_address[0] == "127.0.0.1"
    conn = http.client.HTTPConnection("127.0.0.1", gw.plain.server_address[1], timeout=10)
    conn.request("POST", "/anthropic/v1/messages", body=b"{}")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 404
    conn = http.client.HTTPConnection("127.0.0.1", gw.plain.server_address[1], timeout=10)
    conn.request("GET", "/healthz")
    assert json.loads(conn.getresponse().read()) == {"status": "ok", "routes": {"anthropic": "active", "openai": "active"}}
    assert tls_upstream.seen == []


def test_upstream_certificate_is_verified(transparent_factory, tls_upstream):
    # With the normal system trust store the fake upstream's private CA is
    # unknown, so the gateway must refuse to talk to it.
    gw = transparent_factory(trust_upstream=False)
    resp, data = gw.request("api.anthropic.com", "POST", "/v1/messages", body=b"{}")
    assert resp.status == 502
    assert tls_upstream.seen == []
    line = gw.log_lines()[-1]
    assert line["upstream_error"] == "SSLCertVerificationError"


def test_upstream_hostname_is_verified(transparent_factory, tls_upstream):
    # The upstream's CA is trusted, but its certificate names localhost, and
    # the route dials 127.0.0.1: the hostname check must refuse it.
    gw = transparent_factory(upstream_host="127.0.0.1")
    resp, _ = gw.request("api.anthropic.com", "POST", "/v1/messages", body=b"{}")
    assert resp.status == 502
    assert tls_upstream.seen == []
    assert gw.log_lines()[-1]["upstream_error"] == "SSLCertVerificationError"


def test_streaming_incremental_over_tls(transparent_factory, tls_upstream):
    from .test_streaming import FIRST, REST, USAGE_START

    release = threading.Event()
    finished = threading.Event()

    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(FIRST)
        release.wait(10)
        handler.chunk(REST)
        handler.end_chunked()
        finished.set()

    tls_upstream.behavior = behavior
    gw = transparent_factory()
    conn = gw.conn("api.anthropic.com")
    conn.request("POST", "/v1/messages", body=b'{"model":"m","stream":true}', headers={"x-api-key": "sk-dummy"})
    resp = conn.getresponse()
    got = b""
    while b"\n\n" not in got:
        part = resp.read1(65536)
        assert part
        got += part
    assert not finished.is_set()
    assert got == FIRST
    release.set()
    got += resp.read()
    assert got == FIRST + REST
    line = gw.log_lines()[-1]
    assert (line["stream"], line["usage"], line["usage_status"]) == (True, {**USAGE_START, "output_tokens": 5}, "complete")


def test_key_never_reaches_client_or_logs_in_transparent_mode(transparent_factory, tls_upstream):
    def behavior(handler, seen):
        # Echoes the injected credential in a header and in the body.
        echo = json.dumps({"headers": seen.headers, "body": seen.body.decode()}).encode()
        handler.reply(200, echo, [("Content-Type", "application/json"), ("x-echo", seen.header("x-api-key") or "")])

    tls_upstream.behavior = behavior
    gw = transparent_factory(store_bodies=True)
    body = json.dumps({"model": "m", "note": ANTHROPIC_KEY}).encode()
    resp, data = gw.request("api.anthropic.com", "POST", f"/v1/messages?k={ANTHROPIC_KEY}", body=body, headers={"x-api-key": "sk-other"})
    # The key is in the response head: nothing of the response is relayed.
    assert resp.status == 502
    assert resp.getheader("x-echo") is None
    assert ANTHROPIC_KEY.encode() not in data
    line = gw.log_lines()[-1]
    assert line["gateway_error"] == "credential_in_response_head"
    for path in [p for p in gw.log_dir.rglob("*") if p.is_file()]:
        raw = path.read_bytes()
        assert ANTHROPIC_KEY.encode() not in raw, path
        assert OPENAI_KEY.encode() not in raw, path


def test_ca_key_stays_in_tls_dir(certs):
    gateway = certs["gateway"]
    assert gateway["ca.key"].parent == gateway["ca.crt"].parent
    for _crt, key in gateway["leaves"].values():
        assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert oct(gateway["ca.key"].stat().st_mode & 0o777) == "0o600"
    assert oct(gateway["ca.key"].parent.stat().st_mode & 0o777) == "0o700"
    assert b"PRIVATE KEY" not in gateway["ca.crt"].read_bytes()


@pytest.mark.skipif(not hasattr(__import__("select"), "POLLRDHUP"), reason="TLS disconnect detection needs Linux POLLRDHUP")
def test_client_disconnect_over_tls_releases_upstream(transparent_factory, tls_upstream):
    import time

    closed_seen = threading.Event()

    def behavior(handler, seen):
        # Silent until the gateway hangs up, as with a slow model call.
        import select as sel

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            r, _, _ = sel.select([handler.connection], [], [], 0.05)
            if r:
                try:
                    if socket.socket.recv(handler.connection, 1, socket.MSG_PEEK) == b"":
                        break
                except OSError:
                    break
        closed_seen.set()

    tls_upstream.behavior = behavior
    gw = transparent_factory()
    conn = gw.conn("api.anthropic.com")
    conn.putrequest("POST", "/v1/messages")
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    for _ in range(250):
        if tls_upstream.seen:
            break
        time.sleep(0.02)
    conn.sock.close()
    assert closed_seen.wait(5), "gateway did not abort the upstream request"
    line = gw.log_lines()[-1]
    assert line["client_disconnected"] is True


def test_tls_refusal_received_while_client_still_sends(transparent_factory, tls_upstream):
    # The bounded drain lets a client that is still sending read the whole refusal.
    gw = transparent_factory(max_request_bytes=1024)
    for _ in range(5):
        conn = gw.conn("api.anthropic.com")
        conn.putrequest("POST", "/v1/messages")
        conn.putheader("Content-Length", "4000000")
        conn.endheaders()
        try:
            for _ in range(40):
                conn.sock.sendall(b"x" * 65536)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 413
        assert json.loads(body)["error"]["type"] == "request_too_large"
        conn.close()
