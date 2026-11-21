"""Fixtures: a fake upstream HTTP server and a gateway pointed at it."""

from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

from gateway.config import load_config
from gateway.proxy import GatewayServer

ANTHROPIC_KEY = "sk-made-up-anthropic-key-0123456789abcdef"
OPENAI_KEY = "sk-made-up-openai-key-fedcba9876543210"
# Every method and path, for tests that are not about the allowlist.
ANY = [{"method": "*", "path": "/", "match": "prefix"}]


@dataclass
class Seen:
    method: str
    path: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.all(name)
        return values[0] if values else None

    def all(self, name: str) -> list[str]:
        return [v for n, v in self.headers if n.lower() == name.lower()]


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "FakeUpstream"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen = Seen(self.command, self.path, list(self.headers.items()), body)
        self.server.seen.append(seen)
        self.server.behavior(self, seen)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _any

    # Helpers for behaviors.
    def reply(self, status: int, body: bytes, headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response_only(status)
        for name, value in headers or [("Content-Type", "application/json")]:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_chunked(self, status: int = 200, content_type: str = "text/event-stream") -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def end_chunked(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def peer_closed(self) -> bool:
        """True when the gateway has closed its side of this connection."""
        import select
        import socket

        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True


def default_behavior(handler: UpstreamHandler, seen: Seen) -> None:
    handler.reply(200, json.dumps({"ok": True, "path": seen.path}).encode())


class FakeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.seen: list[Seen] = []
        self.behavior: Callable[[UpstreamHandler, Seen], None] = default_behavior
        self.state: dict = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def upstream():
    server = FakeUpstream()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@dataclass
class Gateway:
    server: GatewayServer
    log_dir: Path
    keys: dict = field(default_factory=dict)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def conn(self, timeout: float = 10) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def request(self, method, path, body=None, headers=None, timeout: float = 10):
        conn = self.conn(timeout)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def log_lines(self, wait_for: int = 1, timeout: float = 5) -> list[dict]:
        path = self.log_dir / "requests.jsonl"
        deadline = time.monotonic() + timeout
        while True:
            lines = []
            if path.exists():
                lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if len(lines) >= wait_for or time.monotonic() > deadline:
                return lines
            time.sleep(0.02)


def make_gateway(tmp_path: Path, upstream: FakeUpstream, *, openai_key: bool = True, **overrides) -> GatewayServer:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "anthropic_key").write_text(ANTHROPIC_KEY + "\n")
    if openai_key:
        (secrets / "openai_key").write_text(OPENAI_KEY + "\n")
    allow = overrides.pop("allow", ANY)
    cfg = {
        "listen": {"host": "127.0.0.1", "port": 0, "prefix_routes": True},
        "log_dir": str(tmp_path / "logs"),
        "store_bodies": overrides.pop("store_bodies", False),
        "insecure_allow_http_upstream": True,
        "upstream_timeout_seconds": overrides.pop("upstream_timeout_seconds", 30),
        "client_idle_timeout_seconds": overrides.pop("client_idle_timeout_seconds", 30),
        "routes": [
            {
                "name": "anthropic",
                "prefix": "/anthropic",
                "upstream": upstream.url,
                "credential": {"header": "x-api-key", "key_file": str(secrets / "anthropic_key")},
                "usage": "anthropic",
                "allow": allow,
            },
            {
                "name": "openai",
                "prefix": "/openai",
                "upstream": upstream.url + "/base",
                "credential": {
                    "header": "authorization",
                    "format": "Bearer {key}",
                    "key_file": str(secrets / "openai_key"),
                },
                "usage": "openai",
                "allow": allow,
            },
        ],
    }
    cfg.update(overrides)
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(cfg))
    return GatewayServer(load_config(path, env={}))


@pytest.fixture
def gateway_factory(tmp_path, upstream):
    started: list[GatewayServer] = []

    def start(**kwargs) -> Gateway:
        server = make_gateway(tmp_path, upstream, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started.append(server)
        return Gateway(server, tmp_path / "logs", {"anthropic": ANTHROPIC_KEY, "openai": OPENAI_KEY})

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gateway(gateway_factory) -> Gateway:
    return gateway_factory()
