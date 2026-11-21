"""Round five: Host port, last-resort handling, bodyless store, sent and
completion state, event budget, bounded model scan, and small fixes."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from gateway.proxy import _request_model
from gateway.screen import Screen, sse_data
from gateway.summary import episode
from gateway.usage import UsageParser

from .conftest import ANTHROPIC_KEY

POST = b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}"
A_USAGE = {"input_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1}


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


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- 1: Host port and whitespace ---------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [b"x:\xb2\xb3", b"x:\xb9", b"x:" + b"9" * 4400, b"x:0", b"x:65536", b"x:+1", b"x:\xa0443"],
    ids=["superscripts", "superscript one", "4400 digits", "zero", "too large", "plus", "nbsp"],
)
def test_bad_host_port_is_a_logged_400(gateway, upstream, host):
    reply = raw(gateway, b"GET /anthropic/v1/models HTTP/1.1\r\nHost: " + host + b"\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 400")
    assert gateway.log_lines()[-1]["gateway_error"] == "invalid_host_header"
    assert upstream.seen == []


def test_only_sp_and_htab_are_trimmed(gateway, upstream):
    # \xa0 and \x85 are not optional whitespace: these values are invalid.
    for head in (
        b"Content-Length: 2\xa0\r\n\r\n{}",
        b"Transfer-Encoding: chunked\x85\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        b"Expect: 100-continue\xa0\r\nContent-Length: 2\r\n\r\n{}",
    ):
        reply = raw(gateway, b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\n" + head)
        assert reply[:12] in (b"HTTP/1.1 400", b"HTTP/1.1 417"), head
    assert upstream.seen == []


def test_any_exception_still_ends_in_a_logged_refusal(gateway, upstream, monkeypatch):
    from gateway import proxy

    def boom(self):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(proxy.GatewayHandler, "_target", boom)
    reply = raw(gateway, b"GET /anthropic/v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 500")
    line = gateway.log_lines()[-1]
    assert (line["status"], line["gateway_error"], line["detail"]) == (500, "internal_error", "RuntimeError")


# -- 2: bodyless responses with the store on ------------------------------------------------------


@pytest.mark.parametrize("status,method", [(304, "GET"), (204, "DELETE"), (200, "HEAD")])
def test_bodyless_response_stores_an_empty_file(gateway_factory, upstream, status, method):
    gw = gateway_factory(store_bodies=True)

    def behavior(handler, seen):
        handler.send_response_only(status)
        if status == 200:
            handler.send_header("Content-Length", "10")
        handler.end_headers()

    upstream.behavior = behavior
    resp, _ = gw.request(method, "/anthropic/v1/models")
    assert resp.status == status
    line = gw.log_lines()[-1]
    assert "response_body_withheld" not in line
    assert (gw.log_dir / "bodies" / f"{line['id']}.response").read_bytes() == b""
    meta = json.loads((gw.log_dir / "bodies" / f"{line['id']}.meta.json").read_text())
    assert meta["response_body"] == "stored"


def test_client_gone_before_head_is_incomplete_not_withheld(gateway_factory, upstream):
    gw = gateway_factory(store_bodies=True)
    release = threading.Event()

    def behavior(handler, seen):
        handler.send_response_only(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b'{"slow":')
        handler.wfile.flush()
        release.wait(5)  # the buffered body never completes while the client is there

    upstream.behavior = behavior
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    sock.sendall(POST)
    assert wait_until(lambda: len(upstream.seen) == 1)
    time.sleep(0.3)
    sock.close()
    line = gw.log_lines()[-1]
    release.set()
    assert line["client_disconnected"] is True
    assert line["response_body_withheld"] == "incomplete"


# -- 3: sent state and relay completion in episode totals ---------------------------------------------


@pytest.fixture
def silent_tls_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []
    stop = threading.Event()

    def run():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    threading.Thread(target=run, daemon=True).start()
    yield server.getsockname()[1], held
    stop.set()
    for c in held:
        c.close()
    server.close()


def _episode(gw):
    return episode(gw.log_lines())


def test_connect_timeout_is_not_a_call(gateway_factory, upstream, silent_tls_port):
    port, _ = silent_tls_port
    gw = gateway_factory(upstream_connect_timeout_seconds=0.3)
    for route in gw.server.config.routes:
        route.upstream = f"https://localhost:{port}"
    gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    gw.request("POST", "/anthropic/v1/messages", body=b"{}")
    lines = gw.log_lines(2)
    assert [x["upstream_sent"] for x in lines] == [False, False]
    totals = _episode(gw)
    assert (totals["calls"], totals["calls_outcome_unknown"]) == (0, 0)


def test_disconnect_during_handshake_is_not_a_call(gateway_factory, upstream, silent_tls_port):
    port, held = silent_tls_port
    gw = gateway_factory(upstream_connect_timeout_seconds=30)
    for route in gw.server.config.routes:
        route.upstream = f"https://localhost:{port}"
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    sock.sendall(POST)
    assert wait_until(lambda: len(held) == 1)
    sock.close()
    line = gw.log_lines()[-1]
    assert (line["upstream_sent"], line["client_disconnected"]) == (False, True)
    assert _episode(gw)["calls"] == 0


def _stream_start(handler):
    handler.start_chunked()
    handler.chunk(b'data: {"type":"message_start","message":{"usage":' + json.dumps(A_USAGE).encode() + b"}}\n\n")


def test_client_cancel_mid_stream_is_unknown(gateway, upstream):
    def behavior(handler, seen):
        _stream_start(handler)
        try:
            for _ in range(200):
                handler.chunk(b'data: {"type":"ping"}\n\n')
                time.sleep(0.02)
        except OSError:
            pass

    upstream.behavior = behavior
    sock = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    sock.sendall(POST)
    sock.recv(4096)
    sock.close()
    line = gateway.log_lines()[-1]
    assert (line["status"], line["relay_complete"], line["usage_status"]) == (200, False, "partial")
    totals = episode([line])
    assert (totals["calls"], totals["calls_outcome_unknown"], totals["usage_status"]) == (1, 1, "partial")


def test_upstream_stall_mid_stream_is_unknown(gateway_factory, upstream):
    gw = gateway_factory(upstream_timeout_seconds=1)
    release = threading.Event()

    def behavior(handler, seen):
        _stream_start(handler)
        release.wait(5)

    upstream.behavior = behavior
    reply = raw(gw, POST, timeout=10)
    release.set()
    assert reply.startswith(b"HTTP/1.1 200")
    line = gw.log_lines()[-1]
    assert (line["relay_complete"], line["upstream_error"], line["usage_status"]) == (False, "timeout", "partial")
    totals = episode([line])
    assert (totals["calls"], totals["calls_outcome_unknown"]) == (1, 1)


def test_completed_stream_is_ok(gateway, upstream):
    def behavior(handler, seen):
        _stream_start(handler)
        handler.chunk(b'data: {"type":"message_stop"}\n\n')
        handler.end_chunked()

    upstream.behavior = behavior
    gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    line = gateway.log_lines()[-1]
    assert (line["upstream_sent"], line["relay_complete"]) == (True, True)
    totals = episode([line])
    assert (totals["calls"], totals["calls_outcome_unknown"], totals["usage_status"]) == (1, 0, "complete")


# -- 4: event budget while the writer is stalled ---------------------------------------------------------


def test_reservation_covers_queued_events_while_writer_stalls(gateway_factory, upstream, monkeypatch):
    from gateway import proxy

    gw = gateway_factory()
    # Small enough to arrive in a single read on loopback: the reservation is
    # for bytes the relay has read, so the test needs all three read at once.
    events = [b'data: {"t":"' + bytes([65 + i]) * 1000 + b'"}\n\n' for i in range(3)]
    stalled = threading.Event()
    release = threading.Event()
    real_write = proxy.Relay._write

    def slow_write(self, data, chunked):
        stalled.set()
        release.wait(5)  # a client that does not read
        return real_write(self, data, chunked)

    monkeypatch.setattr(proxy.Relay, "_write", slow_write)

    def behavior(handler, seen):
        handler.start_chunked()
        handler.chunk(b"".join(events))  # all three arrive in one read
        handler.end_chunked()

    upstream.behavior = behavior
    result = {}
    t = threading.Thread(target=lambda: result.update(r=gw.request("POST", "/anthropic/v1/messages", body=b"{}")))
    t.start()
    assert stalled.wait(5)
    time.sleep(0.2)
    # Writing event 1 is stalled; events 2 and 3 are queued. All three are charged.
    assert gw.server.state.budget.used == sum(len(e) for e in events)
    release.set()
    t.join(10)
    assert result["r"][1] == b"".join(events)
    assert wait_until(lambda: gw.server.state.budget.used == 0)


# -- 5: bounded model scan ---------------------------------------------------------------------------------


def test_model_scan_cases():
    assert _request_model(b'{"model":"m","stream":true}', "application/json") == ("m", True)
    assert _request_model(b'{"stream":false,"x":{"model":"inner"},"model":"outer"}', None) == ("outer", False)
    assert _request_model(b' {"a":[{"b":"}]\\"{"}],"model" : "x\\u0041"}', None) == ("xA", None)
    assert _request_model(b'{"model":5}', None) == (None, None)
    assert _request_model(b"[1]", None) == (None, None)


def test_model_scan_work_is_bounded():
    # A field beyond the bound is not found, however large the body.
    late = json.dumps({"messages": [{"role": "user", "content": "x" * 100}] * 50000, "model": "late"}).encode()
    start = time.monotonic()
    assert _request_model(late, "application/json") == (None, None)
    assert time.monotonic() - start < 0.5
    huge_string = b'{"system":"' + b"a" * 20_000_000 + b'","model":"m"}'
    start = time.monotonic()
    assert _request_model(huge_string, None) == (None, None)
    assert time.monotonic() - start < 0.5
    # The model first, as both harnesses send it, is found in a large body.
    early = json.dumps({"model": "early", "messages": [{"role": "user", "content": "x"}] * 500000}).encode()
    assert _request_model(early, None)[0] == "early"


# -- 7: small fixes ----------------------------------------------------------------------------------------


def test_stream_initial_bom_does_not_drop_the_first_field():
    parser = UsageParser("anthropic", "anthropic_messages", streaming=True)
    parser.event(b'\xef\xbb\xbfdata: {"type":"message_start","message":{"usage":' + json.dumps(A_USAGE).encode() + b"}}\n\n")
    parser.event(b'data: {"type":"message_stop"}\n\n')
    assert parser.status(True)[0] == "complete"
    assert sse_data(b'\xef\xbb\xbfdata: x\n\n') == b"x"


def test_surrogate_pairs_matched_before_single_escapes():
    screen = Screen([ANTHROPIC_KEY])
    pair = "\\ud83d\\ude00"
    assert not screen.body(('{"t":"%s"}' % pair).encode())
    escaped = "".join("\\u%04x" % ord(c) for c in ANTHROPIC_KEY)
    # A key right after a pair, and after a lone high surrogate, is still found.
    assert screen.body(('{"t":"%s%s"}' % (pair, escaped)).encode())
    assert screen.body(('{"t":"\\ud83d%s"}' % escaped).encode())


def test_chunk_extension_after_whitespace(gateway, upstream):
    upstream.behavior = lambda h, s: h.reply(200, s.body)
    reply = raw(gateway, b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n2 ;ext=1\r\n{}\r\n0\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"{}")

    def chunked(handler, seen):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n2\t;x\r\n{}\r\n0\r\n\r\n")
        handler.wfile.flush()

    upstream.behavior = chunked
    resp, data = gateway.request("POST", "/anthropic/v1/messages", body=b"{}")
    assert (resp.status, data) == (200, b"{}")


# -- closing-review minors ---------------------------------------------------

def test_request_scan_long_string_stays_cheap():
    """A long string before `stream` must not cost per-character regex state."""
    import tracemalloc

    from gateway.proxy import _request_model

    body = b'{"model":"m","pad":"' + b"a" * 900_000 + b'","stream":true}'
    tracemalloc.start()
    try:
        model, stream = _request_model(body, "application/json")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (model, stream) == ("m", True)
    assert peak < 8 * 1024 * 1024


def test_bom_inside_a_stream_is_not_ignored_for_usage():
    """Only a stream-initial byte order mark is ignored when counting usage."""
    from gateway.screen import sse_data

    marked = b'\xef\xbb\xbfdata: {"a":1}\n\n'
    assert sse_data(marked) == b'{"a":1}'
    assert sse_data(marked, strip_bom=False) is None


def test_log_row_survives_a_lone_surrogate(tmp_path):
    import json

    from gateway.records import Redactor, RequestLog

    log = RequestLog(str(tmp_path), Redactor([]))
    log.write({"model": "x\ud800y"})
    row = json.loads((tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["model"] == "x\ud800y"
