"""Incomplete requests, the reaper, buffer budget, connect deadline, timing."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from gateway.proxy import Reaper


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- 2a: an incomplete head never reaches the upstream ---------------------------------


def test_head_cut_by_write_half_close(gateway, upstream):
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n")  # no blank line
    sock.shutdown(socket.SHUT_WR)
    assert sock.recv(100) == b""
    sock.close()
    line = gateway.log_lines()[-1]
    assert (line["gateway_error"], line.get("upstream_path")) == ("incomplete_request", None)
    assert upstream.seen == []


def test_head_cut_by_read_deadline(gateway_factory, upstream):
    gw = gateway_factory(client_read_timeout_seconds=0.5)
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n")
    try:
        assert sock.recv(100) == b""
    except ConnectionResetError:
        pass
    sock.close()
    line = gw.log_lines()[-1]
    assert line["gateway_error"] == "client_read_timeout"
    assert line.get("upstream_path") is None
    assert upstream.seen == []


def test_request_line_without_line_end(gateway, upstream):
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(b"GET /anthropic/v1/models HTTP/1.1")
    sock.shutdown(socket.SHUT_WR)
    sock.recv(100)
    sock.close()
    assert gateway.log_lines()[-1]["gateway_error"] == "incomplete_request"
    assert upstream.seen == []


# -- 2b: the reaper ---------------------------------------------------------------------


class Owner:
    reaped = False


def test_reaper_disarm_wins_and_flag_resets():
    reaper = Reaper()
    try:
        a, b = socket.socketpair()
        owner = Owner()
        reaper.arm(1, a, 0.2, owner)
        reaper.disarm(1)
        time.sleep(0.6)
        assert owner.reaped is False
        a.sendall(b"x")  # still alive
        assert b.recv(1) == b"x"
        reaper.arm(1, a, 0.1, owner)
        assert wait_until(lambda: owner.reaped)
        reaper.arm(2, b, 30, owner)
        assert owner.reaped is False  # reset for the next request
        a.close()
        b.close()
    finally:
        reaper.stop()
    assert not reaper._thread.is_alive()


def test_server_close_stops_reaper(gateway_factory, upstream):
    gw = gateway_factory()
    thread = gw.server.state.reaper._thread
    assert thread.is_alive()
    gw.server.shutdown()
    gw.server.server_close()
    assert wait_until(lambda: not thread.is_alive())


# -- 2c: buffer budget --------------------------------------------------------------------


def test_buffer_budget_refuses_with_503(gateway_factory, upstream):
    gw = gateway_factory(max_buffered_bytes=1 << 20, max_request_bytes=1 << 20)
    release = threading.Event()

    def behavior(handler, seen):
        release.wait(10)
        handler.reply(200, b"{}")

    upstream.behavior = behavior
    first = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    body = b"x" * 900_000
    first.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body))
    # Hold the first request mid-body, so its budget is taken.
    first.sendall(body[:500_000])
    time.sleep(0.3)
    resp, data = gw.request("POST", "/anthropic/v1/messages", body=b"y" * 600_000)
    assert resp.status == 503
    assert gw.log_lines()[-1]["gateway_error"] == "buffer_limit"
    first.sendall(body[500_000:])
    release.set()
    assert first.recv(100).startswith(b"HTTP/1.1 200")
    first.close()
    assert wait_until(lambda: gw.server.state.budget.used == 0)


# -- 2f: connect deadline and cancellation ---------------------------------------------------


@pytest.fixture
def silent_tls_port():
    """A TCP server that accepts and never answers, so a TLS handshake stalls."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []
    stop = threading.Event()

    def run():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                held.append(conn)
            except OSError:
                continue

    threading.Thread(target=run, daemon=True).start()
    yield server.getsockname()[1], held
    stop.set()
    for c in held:
        c.close()
    server.close()


def _point_at(gw, port):
    for route in gw.server.config.routes:
        route.upstream = f"https://localhost:{port}"


def test_stalled_handshake_hits_connect_deadline(gateway_factory, upstream, silent_tls_port):
    port, _held = silent_tls_port
    gw = gateway_factory(upstream_connect_timeout_seconds=0.5)
    _point_at(gw, port)
    start = time.monotonic()
    resp, _ = gw.request("POST", "/anthropic/v1/messages", body=b"{}", timeout=10)
    assert resp.status == 504
    assert time.monotonic() - start < 3
    assert gw.log_lines()[-1]["gateway_error"] == "upstream_timeout"


def test_client_leaving_during_handshake_frees_the_slot(gateway_factory, upstream, silent_tls_port):
    port, held = silent_tls_port
    gw = gateway_factory(upstream_connect_timeout_seconds=30)
    _point_at(gw, port)
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    sock.sendall(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    assert wait_until(lambda: len(held) == 1)
    sock.close()
    assert wait_until(lambda: gw.server.inflight == 0, timeout=3)
    assert gw.log_lines()[-1]["client_disconnected"] is True


# -- 2i: timing ------------------------------------------------------------------------------


def test_duration_excludes_idle_time_on_a_reused_connection(gateway, upstream):
    conn = gateway.conn()
    conn.request("POST", "/anthropic/v1/messages", body=b"{}")
    conn.getresponse().read()
    time.sleep(1.0)
    conn.request("POST", "/anthropic/v1/messages", body=b"{}")
    conn.getresponse().read()
    conn.close()
    lines = gateway.log_lines(2)
    assert lines[1]["duration_ms"] < 500
    assert lines[1]["time"] > lines[0]["time"]


# -- 3a: one provider per gateway ---------------------------------------------------------------


def test_single_provider_enforced_at_startup(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from gateway.config import ConfigError, check_startup, load_config

    keys = []
    for name in ("a", "b"):
        key = tmp_path / name
        key.write_text("sk-made-up-key-" + name)
        keys.append(str(key))
    env = {"GATEWAY_KEY_FILE_ANTHROPIC": keys[0], "GATEWAY_KEY_FILE_OPENAI": keys[1], "GATEWAY_LOG_DIR": str(tmp_path / "l")}
    cfg = load_config(None, env=env)
    with pytest.raises(ConfigError, match="single_provider"):
        check_startup(cfg)
    check_startup(load_config(None, env={**env, "GATEWAY_KEY_FILE_OPENAI": ""}))
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-m", "gateway"], env={**env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, cwd=root)
    assert out.returncode == 2
    assert "single_provider" in out.stderr
