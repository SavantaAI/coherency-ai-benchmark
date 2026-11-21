"""The reverse proxy: request validation, upstream forwarding, screened relay.

Three listener roles share one state:

- "health": plain HTTP on loopback, answers only /healthz.
- "prefix": plain HTTP with path-prefix routes (/anthropic/...), opt-in.
- "transparent": TLS for the configured upstream hostnames, routed by SNI.

Responses are relayed in screened units (see screen.py): one whole
server-sent event at a time, or one whole buffered body.
"""

from __future__ import annotations

import errno
import http.client
import itertools
import json
import re
import secrets
import select
import selectors
import socket
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import HEALTH_PATH, HOSTNAME_RE, TOKEN_RE, Config, Route, valid_path
from .records import INCOMPLETE, NO_RESPONSE, WITHHELD, BodyStore, Redactor, RequestLog
from .screen import EventSplitter, Screen
from .usage import UsageParser, inference_kind

# Never forwarded in either direction (RFC 9110 section 7.6.1, plus framing).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "expect",
}
# Credentials a client might send. They are dropped and replaced by the route's.
CLIENT_CREDENTIALS = {"x-api-key", "authorization"}
READ_CHUNK = 64 * 1024
MAX_LINE = 8192
MAX_HEADER_BYTES = 65536
MAX_HEADERS = 100
MAX_CHUNK_LINE = 4096
MAX_TRAILER_BYTES = 8192
QUERY_RE = re.compile(r"^[\x21-\x7e]*$")
OWS = " \t"  # optional whitespace in header values: only SP and HTAB
PORT_RE = re.compile(r"^[0-9]{1,5}$")  # ASCII digits only; str.isdigit() also takes superscripts
CONTENT_LENGTH_RE = re.compile(r"^[0-9]{1,15}$")
CHUNK_SIZE_RE = re.compile(rb"^([0-9A-Fa-f]{1,15})(?:[ \t]*;[\x20-\x7e\t]*)?\r\n$")
VERSION_RE = re.compile(r"^HTTP/[0-9]\.[0-9]$")
# Control bytes other than HTAB, including a bare CR, are refused anywhere in
# a request line or header line (the CRLF that ends the line is removed first).
CONTROL = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")
# After a refusal: discard what the client still sends for at most this long
# and this many bytes, then close.
LINGER_SECONDS = 2.0
LINGER_BYTES = 64 * 1024 * 1024
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

# Error bodies seen by clients: neutral wording in the upstream API's shape.
ERRORS = {
    400: ("invalid_request_error", "invalid_request_error", "Invalid request"),
    403: ("permission_error", "invalid_request_error", "Forbidden"),
    404: ("not_found_error", "invalid_request_error", "Not found"),
    405: ("invalid_request_error", "invalid_request_error", "Method not allowed"),
    408: ("invalid_request_error", "invalid_request_error", "Request timeout"),
    413: ("request_too_large", "invalid_request_error", "Request too large"),
    414: ("invalid_request_error", "invalid_request_error", "URI too long"),
    417: ("invalid_request_error", "invalid_request_error", "Expectation failed"),
    421: ("invalid_request_error", "invalid_request_error", "Misdirected request"),
    431: ("invalid_request_error", "invalid_request_error", "Request header fields too large"),
    500: ("api_error", "server_error", "Internal server error"),
    501: ("invalid_request_error", "invalid_request_error", "Not implemented"),
    502: ("api_error", "server_error", "Internal server error"),
    503: ("api_error", "server_error", "Service unavailable"),
    504: ("api_error", "server_error", "Request timed out"),
    505: ("invalid_request_error", "invalid_request_error", "HTTP version not supported"),
}


def error_body(shape: str, status: int) -> bytes:
    anthropic_type, openai_type, message = ERRORS.get(status, ERRORS[400] if status < 500 else ERRORS[502])
    if shape == "openai":
        payload = {"error": {"message": message, "type": openai_type, "param": None, "code": None}}
    else:
        payload = {"type": "error", "error": {"type": anthropic_type, "message": message}}
    return json.dumps(payload).encode()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClientGone(Exception):
    """The client closed its connection."""


class Incomplete(Exception):
    """The request ended (EOF or deadline) before its head or body was complete."""


class UpstreamBroken(Exception):
    """The upstream response ended badly after the client got some of it."""


class Cut(Exception):
    """Stop relaying; the client connection is cut. `kind` goes to the log."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class Refuse(Exception):
    """Answer with an error before anything of the response was sent."""

    def __init__(self, status: int, kind: str):
        super().__init__(kind)
        self.status = status
        self.kind = kind


class BadRequest(Exception):
    def __init__(self, status: int, kind: str):
        super().__init__(kind)
        self.status = status
        self.kind = kind


# -- shared state ------------------------------------------------------------------


class Reaper:
    """One thread that cuts client sockets whose request took too long to arrive.

    Expiry and disarm run under the same lock, so a handler that has disarmed
    can never have its socket cut afterwards.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[int, tuple[float, socket.socket, object]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gateway-reaper", daemon=True)
        self._thread.start()

    def arm(self, key: int, sock: socket.socket, seconds: float, owner) -> None:
        with self._lock:
            owner.reaped = False
            self._items[key] = (time.monotonic() + seconds, sock, owner)

    def disarm(self, key: int) -> None:
        """Stop watching. A deadline already passed still counts as reaped."""
        with self._lock:
            item = self._items.pop(key, None)
            if item is not None and item[0] <= time.monotonic():
                item[2].reaped = True

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            now = time.monotonic()
            with self._lock:
                for key in [k for k, (d, _, _) in self._items.items() if d <= now]:
                    _, sock, owner = self._items.pop(key)
                    owner.reaped = True
                    try:
                        socket.socket.shutdown(sock, socket.SHUT_RDWR)
                    except OSError:
                        pass


class Budget:
    """Bytes held in request and response buffers across all connections."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self, n: int) -> bool:
        with self._lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True

    def give(self, n: int) -> None:
        with self._lock:
            self.used -= n


class Held:
    """One request's share of the budget, released when the request ends."""

    def __init__(self, budget: Budget):
        self._budget = budget
        self.amount = 0

    def take(self, n: int) -> bool:
        if n <= 0:
            return True
        if not self._budget.take(n):
            return False
        self.amount += n
        return True

    def set(self, n: int) -> bool:
        """Hold exactly n bytes."""
        if n > self.amount:
            return self.take(n - self.amount)
        self._budget.give(self.amount - n)
        self.amount = n
        return True

    def release(self) -> None:
        self.set(0)


class SharedState:
    """What the listeners share: log, body store, ids, counters, limits."""

    def __init__(self, config: Config, ssl_context: ssl.SSLContext | None = None):
        self.redactor = Redactor(config.secrets())
        self.screen = Screen(config.secrets())
        self.request_log = RequestLog(config.log_dir, self.redactor)
        self.body_store = (
            BodyStore(config.log_dir, self.redactor, config.max_stored_body_bytes) if config.store_bodies else None
        )
        # Client context for upstream connections: system trust store, normal
        # certificate and hostname verification.
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.ids = itertools.count(1)
        self.inflight = 0
        self.lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(config.max_connections)
        self.budget = Budget(config.max_buffered_bytes)
        self.reaper = Reaper()
        self.users = 0

    def acquire(self) -> None:
        with self.lock:
            self.users += 1

    def release(self) -> None:
        with self.lock:
            self.users -= 1
            last = self.users == 0
        if last:
            self.reaper.stop()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        config: Config,
        ssl_context: ssl.SSLContext | None = None,
        *,
        tls_context: ssl.SSLContext | None = None,
        state: SharedState | None = None,
        role: str | None = None,
    ):
        self.config = config
        self.state = state or SharedState(config, ssl_context)
        self.state.acquire()
        self.redactor = self.state.redactor
        self.request_log = self.state.request_log
        self.body_store = self.state.body_store
        self.ssl_context = self.state.ssl_context
        self.tls_context = tls_context
        self.transparent = tls_context is not None
        if role is None:
            role = "transparent" if self.transparent else ("prefix" if config.prefix_routes else "health")
        self.role = role
        address = (config.tls_host, config.tls_port) if self.transparent else (config.host, config.port)
        try:
            super().__init__(address, GatewayHandler)
        except Exception:
            self.state.release()
            raise

    def server_close(self):
        super().server_close()
        if not getattr(self, "_released", False):
            self._released = True
            self.state.release()

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls_context is not None:
            # The handshake runs in the request thread, not the accept loop.
            sock = self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def process_request(self, request, client_address):
        if not self.state.connections.acquire(blocking=False):
            self.log_record({"gateway_error": "connection_limit"})
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.state.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        state = self.state
        try:
            super().process_request_thread(request, client_address)
        finally:
            state.connections.release()

    def handle_error(self, request, client_address):
        text = self.redactor.text(traceback.format_exc())
        print(f"gateway: unhandled error\n{text}", file=sys.stderr)

    def next_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{next(self.state.ids):06d}-{secrets.token_hex(3)}"

    @property
    def inflight(self) -> int:
        with self.state.lock:
            return self.state.inflight

    def track(self, delta: int) -> None:
        with self.state.lock:
            self.state.inflight += delta

    def route_for(self, path: str) -> Route | None:
        for route in self.config.active_routes():
            if route.matches(path):
                return route
        return None

    def log_record(self, fields: dict) -> None:
        record = {"time": utc_now(), "id": self.next_id(), "mode": self.role, **fields}
        try:
            self.request_log.write(record)
        except Exception as exc:  # noqa: BLE001
            print(f"gateway: cannot write request log: {exc.__class__.__name__}", file=sys.stderr)


# -- upstream ------------------------------------------------------------------------


class UpstreamResponse(http.client.HTTPResponse):
    """Skips interim 1xx responses other than 100 (which http.client skips itself).

    Without this, http.client would hand back a 103 as the final response.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interim: list[int] = []

    def _read_status(self):
        while True:
            version, status, reason = super()._read_status()
            if 100 < status < 200 and status != 101:
                http.client.parse_headers(self.fp)
                self.interim.append(status)
                if len(self.interim) > 20:
                    raise http.client.HTTPException("too many interim responses")
                continue
            return version, status, reason


class Upstream:
    """One upstream connection that another thread can cancel at any stage.

    The connect and the TLS handshake run non-blocking in short slices, so
    cancellation and the connect deadline apply to them too. After that, the
    socket reference is kept through response completion: http.client
    clears conn.sock for a connection-closing response, but the response
    keeps reading from it.
    """

    SLICE = 0.25

    def __init__(self, route: Route, timeout: float, connect_timeout: float, ssl_context: ssl.SSLContext):
        self.route = route
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.ssl_context = ssl_context
        if route.scheme == "https":
            self.conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                route.upstream_host, route.upstream_port, timeout=timeout, context=ssl_context
            )
        else:
            self.conn = http.client.HTTPConnection(route.upstream_host, route.upstream_port, timeout=timeout)
        self.conn.response_class = UpstreamResponse
        self._lock = threading.Lock()
        self._aborted = False
        self._sock = None

    def _set(self, sock) -> None:
        with self._lock:
            self._sock = sock
            if self._aborted:
                raise ClientGone()

    def _check(self, deadline: float) -> float:
        if self.aborted:
            raise ClientGone()
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("upstream connect deadline")
        return min(left, self.SLICE)

    def _wait(self, sock, events: int, deadline: float) -> bool:
        """Wait for readiness with selectors (no FD_SETSIZE limit)."""
        with selectors.DefaultSelector() as sel:
            sel.register(sock, events)
            return bool(sel.select(self._check(deadline)))

    def connect(self) -> None:
        host, port = self.route.upstream_host, self.route.upstream_port
        # Name resolution uses the system resolver and cannot be cancelled;
        # the connect deadline starts after it.
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        deadline = time.monotonic() + self.connect_timeout
        raw = None
        last: Exception | None = None
        for family, kind, proto, _name, addr in infos:
            sock = socket.socket(family, kind, proto)
            sock.setblocking(False)
            try:
                self._set(sock)
                err = sock.connect_ex(addr)
                if err not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
                    raise OSError(err, "connect failed")
                while True:
                    if self._wait(sock, selectors.EVENT_WRITE, deadline):
                        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if err:
                            raise OSError(err, "connect failed")
                        break
                raw = sock
                break
            except (ClientGone, TimeoutError):
                sock.close()
                raise
            except OSError as exc:
                last = exc
                sock.close()
        if raw is None:
            raise last or OSError("no address")
        sock = raw
        if self.route.scheme == "https":
            sock = self.ssl_context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            self._set(sock)
            while True:
                try:
                    sock.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    self._wait(sock, selectors.EVENT_READ, deadline)
                except ssl.SSLWantWriteError:
                    self._wait(sock, selectors.EVENT_WRITE, deadline)
        sock.setblocking(True)
        sock.settimeout(self.timeout)
        self.conn.sock = sock

    def abort(self) -> None:
        """Unblock any thread working on the upstream socket."""
        with self._lock:
            self._aborted = True
            sock = self._sock
        if sock is not None:
            try:
                # The plain socket method, so a TLS socket's state is not
                # touched from this thread.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


class DisconnectWatch:
    """Watches the client socket while the handler waits on the upstream.

    If the client closes its connection, or half-closes it, the upstream
    connection is aborted so the handler thread and the upstream socket are
    released at once, rather than when the upstream next sends data.
    """

    def __init__(self, client: socket.socket, upstream: Upstream):
        self._client = client
        self._upstream = upstream
        self._done = threading.Event()
        self.client_gone = False
        self._thread = threading.Thread(target=self._run, name="gateway-watch", daemon=True)

    def start(self) -> "DisconnectWatch":
        self._thread.start()
        return self

    def stop(self) -> None:
        # No join: the thread notices within one poll interval and exits.
        self._done.set()

    def _gone(self) -> None:
        self.client_gone = True
        self._upstream.abort()

    def _run(self) -> None:
        if hasattr(select, "POLLRDHUP"):
            self._run_poll()
        else:
            self._run_peek()

    def _run_poll(self) -> None:
        """Linux: wait for the peer's FIN or a reset, ignoring readable data."""
        poller = select.poll()
        try:
            poller.register(self._client.fileno(), select.POLLRDHUP | select.POLLHUP | select.POLLERR)
        except (OSError, ValueError):
            return
        while not self._done.is_set():
            try:
                events = poller.poll(250)
            except (OSError, ValueError):
                return
            if self._done.is_set():
                return
            if events:
                self._gone()
                return

    def _run_peek(self) -> None:
        """Elsewhere: peek at the raw socket; an empty read means the peer closed."""
        with selectors.DefaultSelector() as sel:
            try:
                sel.register(self._client, selectors.EVENT_READ)
            except (OSError, ValueError):
                return
            while not self._done.is_set():
                try:
                    readable = bool(sel.select(0.25))
                except (OSError, ValueError):
                    readable = True
                if self._done.is_set():
                    return
                if not readable:
                    continue
                try:
                    peek = socket.socket.recv(self._client, 1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    peek = b""
                if not peek:
                    self._gone()
                # Data: a pipelined request, or TLS bytes that cannot be read here.
                # Either way stop; a failed write still ends the request.
                return


# -- handler -------------------------------------------------------------------------


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: GatewayServer
    server_version = ""
    sys_version = ""

    def version_string(self) -> str:
        return ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    # -- connection ----------------------------------------------------------

    def setup(self) -> None:
        self.timeout = self.server.config.client_idle_timeout
        self._tls_error = None
        self.reaped = False
        if self.server.transparent:
            self.request.settimeout(min(self.timeout, 30))
            try:
                self.request.do_handshake()
            except (ssl.SSLError, OSError) as exc:
                self._tls_error = exc
        super().setup()

    def handle(self) -> None:
        if self._tls_error is not None:
            sni = getattr(self.request, "gateway_sni", None)
            known = sni in self.server.config.transparent_hosts()
            self.server.log_record(
                {
                    "host": sni,
                    "gateway_error": "tls_handshake_failed" if known else "tls_unknown_host",
                    "detail": self._tls_error.__class__.__name__,
                }
            )
            return
        self.close_connection = True
        self.handle_one_request()
        while not self.close_connection:
            self.handle_one_request()

    def _begin(self) -> None:
        """Fresh identity and state for every request, before anything can fail."""
        self._request_id = self.server.next_id()
        self._start_wall = utc_now()
        self._arrival = time.monotonic()
        self._first_body_at: float | None = None
        self._done_at: float | None = None
        self._route: Route | None = None
        self._route_path: str | None = None
        self._host: str | None = None
        self._raw_target: str | None = None
        self._deferred = None
        self._head_sent = False
        self.command = None
        self.path = None
        self.request_version = "HTTP/1.1"
        self.headers = http.client.HTTPMessage()
        self.raw_requestline = b""

    def handle_one_request(self) -> None:
        self.close_connection = True
        cfg = self.server.config
        reaper = self.server.state.reaper
        key = id(self)
        # Wait for the first byte of a request; the socket's idle timeout
        # bounds the wait.
        try:
            if not self.rfile.peek(1):
                return
        except (OSError, ValueError):
            return
        self._begin()
        reaper.arm(key, self.connection, cfg.client_read_timeout, self)
        try:
            try:
                self._read_head()
                if self.reaped:
                    raise Incomplete()
                self._dispatch()
            except BadRequest as exc:
                self._refuse(exc.status, exc.kind)
            except Incomplete:
                self.close_connection = True
                self._log({"gateway_error": "client_read_timeout" if self.reaped else "incomplete_request",
                           "client_disconnected": True})
            except (ClientGone, OSError):
                self.close_connection = True
                self._log({"gateway_error": "client_read_timeout" if self.reaped else "client_gone",
                           "client_disconnected": True})
            except Exception as exc:  # noqa: BLE001
                # Last resort: no request ends without a logged refusal.
                self.close_connection = True
                text = self.server.redactor.text(traceback.format_exc())
                print(f"gateway: internal error\n{text}", file=sys.stderr)
                self._deferred = None
                if self._head_sent:
                    self._log({"gateway_error": "internal_error", "detail": exc.__class__.__name__})
                else:
                    self._refuse(500, "internal_error", {"detail": exc.__class__.__name__})
            try:
                self.wfile.flush()
            except OSError:
                self.close_connection = True
        finally:
            reaper.disarm(key)

    # -- reading the request head ---------------------------------------------

    def _readline(self, limit: int) -> bytes:
        line = self.rfile.readline(limit + 1)
        if len(line) > limit:
            return line
        if not line.endswith(b"\n"):
            # EOF, a write-half close or the reaper before the line ended.
            raise Incomplete()
        return line

    def _read_head(self) -> None:
        """Read the request line and header block; EOF before the blank line is Incomplete.

        The header object is built from (name, value) pairs parsed here, not
        by email.parser, which would treat a bare CR as a line break.
        """
        line = self._readline(MAX_LINE)
        self.raw_requestline = line
        if len(line) > MAX_LINE:
            raise BadRequest(414, "uri_too_long")
        if not line.endswith(b"\r\n") or CONTROL.search(line[:-2]) or b"\t" in line:
            raise BadRequest(400, "malformed_request_line")
        words = line[:-2].decode("iso-8859-1").split(" ")
        if len(words) != 3 or not all(words):
            raise BadRequest(400, "malformed_request_line")
        method, target, version = words
        self.command, self.path = method, target
        self._raw_target = target
        if not VERSION_RE.match(version):
            raise BadRequest(400, "malformed_request_line")
        if version not in ("HTTP/1.1", "HTTP/1.0"):
            raise BadRequest(505, "unsupported_version")
        self.request_version = version
        headers = http.client.HTTPMessage()
        count = 0
        total = 0
        while True:
            line = self._readline(MAX_LINE)
            if len(line) > MAX_LINE:
                raise BadRequest(431, "headers_too_large")
            if line == b"\r\n":
                break
            count += 1
            total += len(line)
            if count > MAX_HEADERS or total > MAX_HEADER_BYTES:
                raise BadRequest(431, "headers_too_large")
            if not line.endswith(b"\r\n"):
                raise BadRequest(400, "malformed_header")
            content = line[:-2]
            if CONTROL.search(content) or content[:1] in (b" ", b"\t"):
                # Bare CR, other control bytes, or obsolete line folding.
                raise BadRequest(400, "malformed_header")
            name, sep, value = content.partition(b":")
            name_text = name.decode("iso-8859-1")
            if not sep or not TOKEN_RE.match(name_text):
                raise BadRequest(400, "malformed_header")
            # Message.__setitem__ appends the pair as given; nothing re-parses it.
            headers[name_text] = value.strip(b" \t").decode("iso-8859-1")
        self.headers = headers
        tokens = {t.strip(OWS).lower() for v in headers.get_all("Connection", []) for t in v.split(",")}
        self.close_connection = version == "HTTP/1.0" or "close" in tokens
        if method not in METHODS:
            raise BadRequest(405, "method_not_allowed")

    # -- responses written by the gateway itself -----------------------------

    def _shape(self) -> str:
        if self._route is not None:
            return self._route.error_shape
        if self.server.transparent:
            route = self.server.config.route_for_host(getattr(self.connection, "gateway_sni", None) or "")
            if route is not None:
                return route.error_shape
        return "anthropic"

    def _send_json(self, status: int, body: bytes, close: bool) -> int:
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._done_at = time.monotonic()
        return len(body)

    def _refuse(self, status: int, kind: str, extra: dict | None = None) -> None:
        self.close_connection = True
        try:
            sent = self._send_json(status, error_body(self._shape(), status), close=True)
        except OSError:
            sent = 0
        self._log({"status": status, "gateway_error": kind, "request_bytes": 0, "response_bytes": sent, **(extra or {})})
        self._linger()

    def _linger(self) -> None:
        """Close gently after an early refusal.

        The client may still be sending a body. Closing with unread bytes
        would reset the connection before it reads the response, so: flush,
        shut down the write side, discard incoming bytes for a short bounded
        time, then let the connection close.
        """
        sock = self.connection
        try:
            self.wfile.flush()
            socket.socket.shutdown(sock, socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + LINGER_SECONDS
        budget = LINGER_BYTES
        try:
            while budget > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                socket.socket.settimeout(sock, min(left, 0.25))
                try:
                    data = socket.socket.recv(sock, min(65536, budget))
                except (socket.timeout, TimeoutError):
                    continue
                if not data:
                    break
                budget -= len(data)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None) -> None:  # noqa: ARG002
        """Kept for anything in the base class that still calls it."""
        self._refuse(code, "malformed_request")

    def _log(self, fields: dict) -> None:
        if self._deferred is not None:
            # Inside _forward: written after the meta file, so a reader that
            # sees the log line also finds the stored files complete.
            self._deferred.append(fields)
            return
        route = self._route
        end = self._done_at if self._done_at is not None else time.monotonic()
        record = {
            "time": self._start_wall,
            "id": self._request_id,
            "mode": self.server.role,
            "route": route.name if route else None,
            "usage_format": route.usage_format if route else None,
            "host": self._host,
            "method": self.command,
            "path": self._raw_target,
            "route_path": self._route_path,
            "status": None,
        }
        record.update(fields)
        # From the first byte of the request to the last byte sent back.
        record["duration_ms"] = round((end - self._arrival) * 1000, 1)
        if self._first_body_at is not None:
            record["first_body_ms"] = round((self._first_body_at - self._arrival) * 1000, 1)
        try:
            self.server.request_log.write(record)
        except Exception as exc:  # noqa: BLE001
            print(f"gateway: cannot write request log: {exc.__class__.__name__}", file=sys.stderr)

    # -- validation ----------------------------------------------------------

    def _target(self) -> tuple[str, str]:
        """(path, query part including '?') from the raw request line."""
        target = self._raw_target or ""
        if not target.startswith("/"):
            # Absolute-form, authority-form and '*' targets: not a forward proxy.
            raise BadRequest(400, "invalid_request_target")
        if "#" in target:
            raise BadRequest(400, "invalid_request_target")
        path, sep, query = target.partition("?")
        if not valid_path(path):
            raise BadRequest(400, "invalid_path")
        if not QUERY_RE.match(query):
            raise BadRequest(400, "invalid_query")
        return path, sep + query

    def _host_header(self) -> tuple[str, int | None]:
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            raise BadRequest(400, "invalid_host_header")
        value = values[0].strip(OWS).lower()
        host, sep, port = value.rpartition(":") if ":" in value else (value, "", "")
        if sep and (not PORT_RE.match(port) or not 1 <= int(port) <= 65535):
            raise BadRequest(400, "invalid_host_header")
        if not HOSTNAME_RE.match(host):
            raise BadRequest(400, "invalid_host_header")
        return host, int(port) if sep else None

    def _framing(self) -> tuple[str, int]:
        """("none" | "length" | "chunked", declared length)."""
        te = self.headers.get_all("Transfer-Encoding") or []
        cl = self.headers.get_all("Content-Length") or []
        if te and cl:
            raise BadRequest(400, "conflicting_framing")
        if te:
            if len(te) != 1 or te[0].strip(OWS).lower() != "chunked" or self.request_version != "HTTP/1.1":
                raise BadRequest(400, "unsupported_transfer_encoding")
            return "chunked", 0
        if cl:
            if len(cl) != 1 or not CONTENT_LENGTH_RE.match(cl[0].strip(OWS)):
                raise BadRequest(400, "invalid_content_length")
            return "length", int(cl[0].strip(OWS))
        return "none", 0

    def _expect(self) -> bool:
        values = self.headers.get_all("Expect") or []
        if not values:
            return False
        if len(values) != 1 or values[0].strip(OWS).lower() != "100-continue":
            raise BadRequest(417, "unsupported_expectation")
        return self.request_version == "HTTP/1.1"

    def _read_body(self, framing: str, length: int, held: Held) -> bytes:
        limit = self.server.config.max_request_bytes
        if framing == "none":
            return b""
        if framing == "length":
            # Size and budget were checked (and taken) in _forward, before
            # any 100 Continue.
            body = self.rfile.read(length)
            if len(body) != length:
                raise Incomplete()
            return body
        body = bytearray()
        while True:
            line = self.rfile.readline(MAX_CHUNK_LINE + 1)
            if not line:
                raise Incomplete()
            match = CHUNK_SIZE_RE.match(line)
            if not match:
                if not line.endswith(b"\n") and len(line) <= MAX_CHUNK_LINE:
                    raise Incomplete()
                raise BadRequest(400, "invalid_chunk")
            size = int(match.group(1), 16)
            if size == 0:
                break
            if len(body) + size > limit:
                raise BadRequest(413, "request_too_large")
            if not held.take(size):
                raise BadRequest(503, "buffer_limit")
            data = self.rfile.read(size)
            if len(data) != size:
                raise Incomplete()
            body.extend(data)
            del data
            crlf = self.rfile.read(2)
            if len(crlf) < 2:
                raise Incomplete()
            if crlf != b"\r\n":
                raise BadRequest(400, "invalid_chunk")
        trailer = 0
        while True:
            line = self.rfile.readline(MAX_TRAILER_BYTES + 1)
            if not line:
                raise Incomplete()
            trailer += len(line)
            if trailer > MAX_TRAILER_BYTES:
                raise BadRequest(400, "invalid_trailer")
            if not line.endswith(b"\n"):
                raise Incomplete()
            if not line.endswith(b"\r\n"):
                raise BadRequest(400, "invalid_trailer")
            if line == b"\r\n":
                break
        return bytes(body)

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self) -> None:
        path, query = self._target()
        framing, length = self._framing()
        host, port = self._host_header()
        role = self.server.role
        if role == "transparent":
            sni = getattr(self.connection, "gateway_sni", None)
            self._host = sni
            route = self.server.config.route_for_host(sni or "")
            self._route = route
            bound = self.server.server_address[1]
            effective = port if port is not None else 443
            if route is None or host != sni or effective != bound:
                raise BadRequest(421, "misdirected_request")
            self._route_path = path
            if not route.allows(self.command, path):
                raise BadRequest(403, "path_not_allowed")
            self._run_forward(route, path, query, framing, length)
            return
        if path == HEALTH_PATH:
            if self.command not in ("GET", "HEAD"):
                raise BadRequest(405, "method_not_allowed")
            if framing != "none" and not (framing == "length" and length == 0):
                raise BadRequest(400, "body_not_allowed")
            self._health()
            return
        if role != "prefix":
            raise BadRequest(404, "unknown_route")
        route = self.server.route_for(path)
        if route is None:
            raise BadRequest(404, "unknown_route")
        self._route = route
        rest = path[len(route.prefix):] or "/"
        self._route_path = rest
        if not route.allows(self.command, rest):
            raise BadRequest(403, "path_not_allowed")
        self._run_forward(route, rest, query, framing, length)

    def _health(self) -> None:
        routes = {r.name: "active" for r in self.server.config.active_routes()}
        body = json.dumps({"status": "ok", "routes": routes}).encode()
        self.close_connection = True
        self._send_json(200, body, close=True)

    def _run_forward(self, route: Route, rest: str, query: str, framing: str, length: int) -> None:
        self.server.track(1)
        held = Held(self.server.state.budget)
        try:
            self._forward(route, rest, query, framing, length, held)
        except (BadRequest, ClientGone, Incomplete, OSError):
            raise
        except Exception as exc:  # noqa: BLE001
            # A gateway bug. Record it, drop the connection, keep serving.
            self.close_connection = True
            text = self.server.redactor.text(traceback.format_exc())
            print(f"gateway: internal error\n{text}", file=sys.stderr)
            self._deferred = None
            if self._head_sent:
                self._log({"gateway_error": "internal_error", "detail": exc.__class__.__name__})
            else:
                self._refuse(500, "internal_error", {"detail": exc.__class__.__name__})
        finally:
            held.release()
            self.server.track(-1)

    # -- forwarding ----------------------------------------------------------

    def _upstream_headers(self, route: Route, body: bytes, framing: str) -> tuple[list[tuple[str, str]], str]:
        """Headers for the upstream, and the client's Accept-Encoding as sent (or "(none)")."""
        connection_tokens = {
            token.strip(OWS).lower() for value in self.headers.get_all("Connection", []) for token in value.split(",")
        }
        drop = HOP_BY_HOP | CLIENT_CREDENTIALS | connection_tokens | {"host", "content-length", route.credential_header}
        headers: list[tuple[str, str]] = [("Host", route.upstream_host_header)]
        ae_values = self.headers.get_all("Accept-Encoding")
        client_ae = ", ".join(ae_values) if ae_values else "(none)"
        ae_done = False
        for name, value in self.headers.items():
            lname = name.lower()
            if lname == "accept-encoding":
                # The one header besides the credential that the gateway
                # changes: responses are always requested uncompressed, so
                # every byte can be screened and parsed as it arrives.
                if not ae_done:
                    ae_done = True
                    headers.append((name, "identity"))
                continue
            if lname in drop:
                continue
            headers.append((name, value))
        if not ae_done:
            headers.append(("Accept-Encoding", "identity"))
        if framing != "none":
            headers.append(("Content-Length", str(len(body))))
        headers.append((route.credential_header, route.credential_value()))
        return headers, client_ae

    def _forward(self, route: Route, rest: str, query: str, framing: str, length: int, held: Held) -> None:
        cfg = self.server.config
        store = self.server.body_store
        expect_continue = self._expect()
        # Limits first: a refused request never gets a 100 Continue.
        if framing == "length":
            if length > cfg.max_request_bytes:
                raise BadRequest(413, "request_too_large")
            if not held.take(length):
                raise BadRequest(503, "buffer_limit")
        if expect_continue and framing != "none":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        try:
            body = self._read_body(framing, length, held)
        except BadRequest:
            raise
        except (ClientGone, Incomplete, OSError):
            self.close_connection = True
            self._log({
                "gateway_error": "client_read_timeout" if self.reaped else "incomplete_request",
                "client_disconnected": True,
                "usage_status": "unavailable",
            })
            return
        self.server.state.reaper.disarm(id(self))
        if self.reaped:
            # The deadline fired as the body completed: treat it as cut.
            self.close_connection = True
            self._log({"gateway_error": "client_read_timeout", "client_disconnected": True, "usage_status": "unavailable"})
            return
        model, req_stream = _request_model(body, self.headers.get("Content-Type", ""))
        upstream_target = route.upstream_base_path + rest + query
        record: dict = {
            "upstream_path": upstream_target,
            "model": model,
            "request_stream": req_stream,
            "request_bytes": len(body),
            # upstream_sent: the request bytes went to the upstream, so it may
            # have generated output. relay_complete: the whole response was
            # relayed, which a 2xx status alone does not say.
            "upstream_sent": False,
            "relay_complete": False,
        }
        headers, client_ae = self._upstream_headers(route, body, framing)
        if client_ae != "identity":
            record["client_accept_encoding"] = client_ae

        if store is not None:
            try:
                encoding = ", ".join(self.headers.get_all("Content-Encoding") or []) or None
                name, withheld = store.write_request(self._request_id, body, encoding)
                record["request_body_file"] = name
                if withheld:
                    record["request_body_withheld"] = withheld
            except OSError as exc:
                record["store_error"] = exc.__class__.__name__

        upstream = Upstream(route, cfg.upstream_timeout, cfg.upstream_connect_timeout, self.server.ssl_context)
        watch = DisconnectWatch(self.connection, upstream).start()
        relay = Relay(self, route, record, held)
        resp = None
        self._deferred = []
        try:
            try:
                upstream.connect()
                conn = upstream.conn
                conn.putrequest(self.command, upstream_target, skip_host=True, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                # From here the upstream may generate output, so a lost
                # response has an unknown outcome rather than none.
                record["upstream_sent"] = True
                conn.endheaders(body if framing != "none" else None)
                del body
                held.set(0)  # the request buffer is gone; the budget goes to the response
                resp = conn.getresponse()
            except Exception as exc:  # noqa: BLE001
                if watch.client_gone or upstream.aborted:
                    self.close_connection = True
                    record["client_disconnected"] = True
                    relay.finish_into(record, transport_complete=False)
                    self._log(record)
                    return
                timed_out = isinstance(exc, (TimeoutError, socket.timeout))
                record["upstream_error"] = exc.__class__.__name__
                self._refuse_upstream(record, 504 if timed_out else 502,
                                      "upstream_timeout" if timed_out else "upstream_unreachable")
                return
            relay.run(resp, watch, upstream)
        except Refuse as exc:
            relay.refused = True
            self._refuse_upstream(record, exc.status, exc.kind)
        except ClientGone:
            self.close_connection = True
            record["client_disconnected"] = True
            relay.finish_into(record, transport_complete=False)
            self._log(record)
        except UpstreamBroken as exc:
            # Some of the response is out; the only signal left is to cut the
            # connection so the client sees a truncated response.
            self.close_connection = True
            record["upstream_error"] = str(exc)
            relay.finish_into(record, transport_complete=False)
            self._log(record)
        except Cut as exc:
            self.close_connection = True
            record["gateway_error"] = exc.kind
            relay.finish_into(record, transport_complete=False)
            self._log(record)
        finally:
            watch.stop()
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            upstream.close()
            if store is not None:
                relay.store_outcome(resp is not None)
                self._store_meta(record, route, headers, resp, relay)
            else:
                relay.close()
            deferred, self._deferred = self._deferred, None
            for fields in deferred or []:
                self._log(fields)

    def _refuse_upstream(self, record: dict, status: int, kind: str) -> None:
        self.close_connection = True
        record["usage_status"] = "unavailable"
        record.pop("usage", None)
        if self._head_sent:
            # Too late for an error response.
            record["gateway_error"] = kind
            self._log(record)
            return
        try:
            sent = self._send_json(status, error_body(self._shape(), status), close=True)
        except OSError:
            sent = 0
        if "status" in record and record.get("status") is not None:
            record["upstream_status"] = record["status"]
        record.update(status=status, gateway_error=kind, response_bytes=sent)
        self._log(record)

    def _store_meta(self, record: dict, route: Route, sent_headers, resp, relay: "Relay") -> None:
        masked = CLIENT_CREDENTIALS | {route.credential_header}
        meta = {
            "id": self._request_id,
            "time": self._start_wall,
            "route": route.name,
            "mode": self.server.role,
            "host": self._host,
            "method": self.command,
            "path": self._raw_target,
            "upstream": route.upstream,
            "upstream_path": record.get("upstream_path"),
            "request_headers_sent": [[n, "[credential]" if n.lower() in masked else v] for n, v in sent_headers],
            "status": record.get("status"),
            "response_headers": [[n, v] for n, v in resp.getheaders()] if resp is not None and relay.head_clean else None,
            "request_body": _note(record.get("request_body_withheld")),
            "response_body": _note(record.get("response_body_withheld")),
            "outcome": {
                k: record.get(k)
                for k in ("gateway_error", "upstream_error", "client_disconnected", "usage_status")
                if record.get(k) is not None
            },
        }
        try:
            self.server.body_store.write_meta(self._request_id, meta)
        except OSError as exc:
            print(f"gateway: cannot store meta: {exc.__class__.__name__}", file=sys.stderr)


class Relay:
    """Relays one upstream response in screened units.

    The response body is read with the gateway's own strict reader (one
    framing decision, used for validation and decoding alike). Usage parsing
    and the body store see a unit only after it passed screening and was
    sent.
    """

    def __init__(self, handler: GatewayHandler, route: Route, record: dict, held: Held):
        self.h = handler
        self.route = route
        self.record = record
        self.held = held
        self.cfg = handler.server.config
        self.screen = handler.server.state.screen
        self.parser: UsageParser | None = None
        self.writer = None
        self.reader: BodyReader | None = None
        self.sent = 0
        self.head_clean = False
        self.refused = False
        self._finished = False

    # -- head ----------------------------------------------------------------

    def _check_head(self, resp) -> tuple[str, bool]:
        """Validate the response head. Returns (framing, streaming)."""
        h, record = self.h, self.record
        record["status"] = resp.status
        record["ttfb_ms"] = round((time.monotonic() - h._arrival) * 1000, 1)
        if resp.interim:
            record["interim_statuses"] = resp.interim
        if self.screen.head(resp.reason or "", resp.getheaders()):
            print("gateway: ALERT a response head contained a configured credential; refused", file=sys.stderr)
            raise Refuse(502, "credential_in_response_head")
        self.head_clean = True
        if resp.status == 101:
            raise Refuse(502, "upstream_protocol_switch")
        no_body = h.command == "HEAD" or resp.status in (204, 304)
        framing = upstream_framing(resp.msg, no_body)
        if framing is None:
            raise Refuse(502, "upstream_bad_framing")
        encodings = [v.strip(OWS).lower() for v in (resp.msg.get_all("Content-Encoding") or []) if v.strip(OWS)]
        if encodings:
            record["content_encoding"] = ", ".join(encodings)
        if not no_body and any(e != "identity" for e in encodings):
            # Asked for identity; a coded body could not be screened.
            raise Refuse(502, "upstream_encoding_not_screenable")
        content_type = (resp.getheader("Content-Type", "") or "").split(";", 1)[0].strip(OWS).lower()
        streaming = content_type == "text/event-stream"
        record["stream"] = streaming
        return framing, streaming

    def _send_head(self, resp, framing: str) -> None:
        """framing for the client: "none", "length:N", "chunked" or "close"."""
        h = self.h
        h.send_response_only(resp.status, resp.reason)
        resp_connection = {t.strip(OWS).lower() for v in (resp.msg.get_all("Connection") or []) for t in v.split(",")}
        for name, value in resp.getheaders():
            lname = name.lower()
            if lname in HOP_BY_HOP or lname in resp_connection:
                continue
            if lname == "content-length" and framing != "none":
                continue
            h.send_header(name, value)
        if framing.startswith("length:"):
            h.send_header("Content-Length", framing.split(":", 1)[1])
        elif framing == "chunked":
            h.send_header("Transfer-Encoding", "chunked")
        elif framing == "close":
            h.close_connection = True
        if h.close_connection:
            h.send_header("Connection", "close")
        try:
            h.end_headers()
        except OSError:
            raise ClientGone() from None
        h._head_sent = True
        h._done_at = time.monotonic()

    def _write(self, data: bytes, chunked: bool) -> None:
        h = self.h
        try:
            if chunked:
                h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            else:
                h.wfile.write(data)
        except OSError:
            raise ClientGone() from None
        now = time.monotonic()
        if h._first_body_at is None:
            h._first_body_at = now
        h._done_at = now  # duration ends at the last successful write
        self.sent += len(data)

    def _write_end(self) -> None:
        try:
            self.h.wfile.write(b"0\r\n\r\n")
        except OSError:
            raise ClientGone() from None
        self.h._done_at = time.monotonic()

    def _read(self, watch, upstream) -> bytes:
        try:
            data = self.reader.read()
        except (UpstreamTruncated, UpstreamTimeout):
            if watch.client_gone or upstream.aborted:
                raise ClientGone() from None
            raise
        if not data and (watch.client_gone or upstream.aborted):
            # An aborted read can look like a normal end of a close-delimited body.
            raise ClientGone()
        return data

    # -- body ----------------------------------------------------------------

    def run(self, resp, watch, upstream) -> None:
        h, record = self.h, self.record
        framing, streaming = self._check_head(resp)
        ok_2xx = 200 <= resp.status < 300 and framing != "none"
        kind = inference_kind(self.route.usage_format, h.command, h._route_path) if ok_2xx else None
        self.parser = UsageParser(self.route.usage_format, kind, streaming)
        self.reader = BodyReader(resp.fp, framing, upstream_length(resp.msg) if framing == "length" else None)
        if framing == "none":
            self._send_head(resp, "none")
            self._open_store()  # an empty, complete body (204, 304, HEAD)
        elif streaming:
            self._relay_events(resp, watch, upstream, framing)
        else:
            self._relay_body(resp, watch, upstream, framing)
        record["relay_complete"] = True
        self.finish_into(record, transport_complete=True)
        if self.writer is not None:
            self.writer.finish(complete=True)
        h._log(record)

    def _open_store(self) -> None:
        store = self.h.server.body_store
        if store is not None:
            self.writer = store.open_response(self.h._request_id)

    def _withhold(self) -> None:
        if self.writer is not None:
            self.writer.withhold(WITHHELD)

    def _alert(self, what: str) -> None:
        print(f"gateway: ALERT a response {what} contained a configured credential; not relayed", file=sys.stderr)

    def _relay_body(self, resp, watch, upstream, framing: str) -> None:
        """Buffer the whole body, screen it, then send it with its framing.

        Nothing has been sent yet, so every failure here is an error response.
        """
        cap = self.cfg.max_response_body_bytes
        declared = self.reader.length
        if declared is not None and declared > cap:
            raise Refuse(502, "response_too_large")
        body = bytearray()
        try:
            while True:
                data = self._read(watch, upstream)
                if not data:
                    break
                if len(body) + len(data) > cap:
                    raise Refuse(502, "response_too_large")
                if not self.held.take(len(data)):
                    raise Refuse(503, "buffer_limit")
                body.extend(data)
        except UpstreamTruncated as exc:
            self.record["upstream_error"] = str(exc)
            raise Refuse(502, "upstream_truncated") from None
        except UpstreamTimeout:
            self.record["upstream_error"] = "timeout"
            raise Refuse(504, "upstream_timeout") from None
        data = bytes(body)
        del body
        if self.screen.body(data):
            self._alert("body")
            raise Refuse(502, "credential_in_response")
        if framing == "length":
            downstream = f"length:{len(data)}"
        else:
            downstream = "chunked" if self.h.request_version == "HTTP/1.1" else "close"
        self._send_head(resp, downstream)
        self._open_store()
        if data:
            self._write(data, downstream == "chunked")
        if downstream == "chunked":
            self._write_end()
        self.parser.body(data)
        if self.writer is not None:
            self.writer.write(data)

    def _relay_events(self, resp, watch, upstream, framing: str) -> None:
        """Send one whole, screened event at a time.

        Budget: every byte read is charged on arrival, whether it is the
        incomplete tail or part of complete events still queued. Events are
        taken off the queue one at a time, and each event's bytes are released
        once its downstream write and the side copies are done.
        """
        h = self.h
        if framing == "length":
            downstream = f"length:{self.reader.length}"
            chunked = False
        else:
            chunked = h.request_version == "HTTP/1.1"
            downstream = "chunked" if chunked else "close"
        self._send_head(resp, downstream)
        self._open_store()
        splitter = EventSplitter(self.cfg.max_sse_event_bytes)
        try:
            while True:
                data = self._read(watch, upstream)
                if not data:
                    break
                if not self.held.take(len(data)):
                    self._withhold()
                    raise Cut("buffer_limit")
                splitter.append(data)
                del data
                while True:
                    event = splitter.next_event()
                    if event is None:
                        break
                    self._event(event, chunked, dispatched=True)
                    del event
                if splitter.pending() > splitter.max_event:
                    self._withhold()
                    raise Cut("sse_event_too_large")
        except UpstreamTruncated as exc:
            raise UpstreamBroken(str(exc)) from None
        except UpstreamTimeout:
            raise UpstreamBroken("timeout") from None
        events, rest = splitter.finish()
        while events:
            event = events.pop(0)
            self._event(event, chunked, dispatched=True)
            del event
        if rest:
            # No closing blank line: a client discards it, so it is relayed
            # (screened) but not counted for usage.
            self._event(rest, chunked, dispatched=False)
        del rest, events
        self.held.set(0)
        if chunked:
            self._write_end()

    def _event(self, event: bytes, chunked: bool, dispatched: bool) -> None:
        """Screen, send and record one event. Its bytes are already charged."""
        if len(event) > self.cfg.max_sse_event_bytes:
            self._withhold()
            raise Cut("sse_event_too_large")
        if self.screen.event(event):
            self._alert("event")
            self._withhold()
            raise Cut("credential_in_response")
        self._write(event, chunked)
        if dispatched:
            self.parser.event(event)
        if self.writer is not None:
            self.writer.write(event)
        self.held.set(max(self.held.amount - len(event), 0))

    # -- results -------------------------------------------------------------

    def finish_into(self, record: dict, transport_complete: bool) -> None:
        if self._finished:
            return
        self._finished = True
        record["response_bytes"] = self.sent
        if self.parser is None:
            record.setdefault("usage_status", "unavailable")
            return
        status, error, tokens = self.parser.status(transport_complete)
        record["response_model"] = self.parser.response_model
        record["usage"] = self.parser.usage
        record["usage_status"] = status
        if self.parser.kind is not None:
            record["inference"] = self.parser.kind
            record["tokens"] = tokens
        if self.parser.server_tool_use:
            record["server_tool_use"] = True
        if error:
            record["usage_error"] = error

    def store_outcome(self, had_response: bool) -> None:
        """Publish the response file (body or marker) and record the reason."""
        store = self.h.server.body_store
        record = self.record
        if self.writer is not None:
            self.writer.finish(complete=False)  # no-op if run() published it
            record["response_body_file"] = self.writer.path.name
            if self.writer.withheld:
                record["response_body_withheld"] = self.writer.withheld
            return
        try:
            record["response_body_file"] = store.write_marker(self.h._request_id)
        except OSError:
            pass
        if self.refused:
            record["response_body_withheld"] = WITHHELD
        elif had_response:
            # The upstream answered but the client left before the head went out.
            record["response_body_withheld"] = INCOMPLETE
        else:
            record["response_body_withheld"] = NO_RESPONSE

    def close(self) -> None:
        if self.writer is not None:
            self.writer.finish(complete=False)


class UpstreamTruncated(Exception):
    """The upstream body ended before its framing said it would."""


class UpstreamTimeout(Exception):
    """The upstream went silent for longer than upstream_timeout_seconds."""


def upstream_framing(msg, no_body: bool) -> str | None:
    """The one framing decision for an upstream response body.

    "none", "chunked", "length" or "close"; None means refuse (502).
    Transfer-Encoding must be absent or a single `chunked` (optional
    whitespace around it allowed); Content-Length values must agree.
    """
    if no_body:
        return "none"
    te = msg.get_all("Transfer-Encoding") or []
    if te:
        if len(te) != 1 or te[0].strip(" \t").lower() != "chunked":
            return None
        return "chunked"
    lengths = {v.strip(" \t") for v in (msg.get_all("Content-Length") or [])}
    if not lengths:
        return "close"
    if len(lengths) != 1 or not CONTENT_LENGTH_RE.match(next(iter(lengths))):
        return None
    return "length"


def upstream_length(msg) -> int:
    return int((msg.get_all("Content-Length") or ["0"])[0].strip(" \t"))


class BodyReader:
    """Strict reader for an upstream response body on http.client's buffered socket file.

    http.client parses the status line and headers only; the body is read
    here. A missing final CRLF, a bad chunk delimiter or an early end is
    UpstreamTruncated; socket silence is UpstreamTimeout.
    """

    def __init__(self, fp, framing: str, length: int | None):
        self.fp = fp
        self.framing = framing
        self.length = length
        self._left = length if framing == "length" else 0
        self._done = framing == "none"

    def _io(self, fn, *args):
        try:
            return fn(*args)
        except (socket.timeout, TimeoutError):
            raise UpstreamTimeout() from None
        except (OSError, ValueError, AttributeError):
            raise UpstreamTruncated("connection lost") from None

    def read(self) -> bytes:
        if self._done:
            return b""
        if self.framing == "close":
            data = self._io(self.fp.read1, READ_CHUNK)
            if not data:
                self._done = True
            return data
        if self.framing == "length":
            if self._left == 0:
                self._done = True
                return b""
            data = self._io(self.fp.read1, min(self._left, READ_CHUNK))
            if not data:
                raise UpstreamTruncated("truncated")
            self._left -= len(data)
            return data
        return self._read_chunked()

    def _read_chunked(self) -> bytes:
        if self._left == 0:
            line = self._io(self.fp.readline, MAX_CHUNK_LINE + 1)
            if not line:
                raise UpstreamTruncated("truncated")
            match = CHUNK_SIZE_RE.match(line)
            if not match:
                raise UpstreamTruncated("bad chunk size line")
            size = int(match.group(1), 16)
            if size == 0:
                total = 0
                while True:
                    trailer = self._io(self.fp.readline, MAX_TRAILER_BYTES + 1)
                    total += len(trailer)
                    if not trailer.endswith(b"\r\n") or total > MAX_TRAILER_BYTES:
                        raise UpstreamTruncated("bad chunked trailer")
                    if trailer == b"\r\n":
                        break
                self._done = True
                return b""
            self._left = size
        data = self._io(self.fp.read1, min(self._left, READ_CHUNK))
        if not data:
            raise UpstreamTruncated("truncated")
        self._left -= len(data)
        if self._left == 0:
            crlf = self._io(self.fp.read, 2)
            if crlf != b"\r\n":
                raise UpstreamTruncated("bad chunk delimiter")
        return data


def _note(reason: str | None) -> str:
    return f"not stored: {reason}" if reason else "stored"


# Bound on the work of finding `model` and `stream` in a request body: the
# scan stops after looking at this many bytes, however large the body is.
SCAN_LIMIT = 1024 * 1024
# Unrolled: runs of ordinary characters are matched in one step, so a long
# string costs no per-character backtracking state.
_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')
_INTERESTING = re.compile(rb'["{}\[\]]')
_SPACE = re.compile(rb"[ \t\r\n]*")
_LITERAL = re.compile(rb"true|false|null|-?[0-9][0-9eE.+-]*")


def _request_model(body: bytes, content_type: str) -> tuple[str | None, bool | None]:
    """The top-level `model` and `stream` fields of a JSON request body.

    No object graph is built: a scan walks the top-level object, skipping
    nested values by bracket depth with C-speed regular expressions, and
    stops when both fields are found or after SCAN_LIMIT bytes. A field not
    found within the bound is None.
    """
    if not body or (content_type and "json" not in content_type.lower()):
        return None, None
    model = stream = None
    limit = min(len(body), SCAN_LIMIT)
    pos = _SPACE.match(body, 0, limit).end()
    if body[pos:pos + 1] != b"{":
        return None, None
    pos += 1
    try:
        while pos < limit and (model is None or stream is None):
            pos = _SPACE.match(body, pos, limit).end()
            if body[pos:pos + 1] in (b",",):
                pos += 1
                continue
            if body[pos:pos + 1] != b'"':
                break  # end of the object, or not JSON
            key_m = _STRING.match(body, pos, limit)
            if key_m is None:
                break
            key = key_m.group()
            pos = _SPACE.match(body, key_m.end(), limit).end()
            if body[pos:pos + 1] != b":":
                break
            pos = _SPACE.match(body, pos + 1, limit).end()
            first = body[pos:pos + 1]
            if first == b'"':
                value_m = _STRING.match(body, pos, limit)
                if value_m is None:
                    break
                if key == b'"model"' and model is None:
                    try:
                        value = json.loads(value_m.group())
                    except ValueError:
                        value = None
                    model = value if isinstance(value, str) else None
                pos = value_m.end()
            elif first in (b"{", b"["):
                # Skip a nested value by depth, jumping between strings and brackets.
                depth = 0
                while pos < limit:
                    m = _INTERESTING.search(body, pos, limit)
                    if m is None:
                        pos = limit
                        break
                    ch = m.group()
                    if ch == b'"':
                        sm = _STRING.match(body, m.start(), limit)
                        if sm is None:
                            pos = limit
                            break
                        pos = sm.end()
                        continue
                    depth += 1 if ch in (b"{", b"[") else -1
                    pos = m.end()
                    if depth == 0:
                        break
            else:
                lit = _LITERAL.match(body, pos, limit)
                if lit is None:
                    break
                if key == b'"stream"' and stream is None and lit.group() in (b"true", b"false"):
                    stream = lit.group() == b"true"
                pos = lit.end()
    except (ValueError, IndexError):
        pass
    return model, stream
