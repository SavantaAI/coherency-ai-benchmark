"""HTTP servers: the model API, and a separate admin listener.

The API listener answers only `POST /v1/messages` and
`POST /v1/messages/count_tokens`. The admin listener is a different socket
(by default 127.0.0.1 inside the server's own container) and answers
`GET /state`, `GET /script`, `GET /export`, `POST /script`, `POST /reset`,
`POST /next`, `POST /import` and `GET /healthz`. No admin path exists on the
API listener.

Every malformed request gets a 4xx in the API's error shape. The path and
method are checked before the body is read. A body that does not arrive
within `read_timeout` seconds of the request head gets 408 and the
connection is closed. Responses carry no `Server` header and nothing that
names this program.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import ids
from .engine import Episode, ImportRefused, Outcome, _system_text
from .script import ScriptError, load_script
from .wire import (MAX_BODY_BYTES, ApiError, encode_event, message_body, parse_request, prompt_tokens,
                   sse_events)

API_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
# The admin listener's body limit for /import. A script is at most
# MAX_BODY_BYTES (32 MiB, the /script limit). An export holds the script and a
# replacement script as JSON strings (escaping can double them) plus the
# replies and state, so the import limit is eight times the script limit.
# GET /export refuses to produce anything larger, so every export imports.
ADMIN_MAX_BODY_BYTES = 8 * MAX_BODY_BYTES
IMPORT_WRAPPER_OVERHEAD = b'{"state": }'   # what the CLI puts around an export file
# ASCII only (str.isdigit accepts "\xb2"), and short enough that int() never
# meets Python's limit on the digits it converts.
DIGITS = re.compile(r"[0-9]{1,15}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The default backlog of 5 drops connections when many clients arrive at
    # once (measured on Python 3.12.8 with 16 clients).
    request_queue_size = 64


class RequestLog:
    """One JSON line per API request, and a state file rewritten after each.

    The first time a system prompt is seen its text is written once as
    `system-<sha256 prefix>.txt`; log lines carry only its hash and length.
    Every write holds the log's lock."""

    def __init__(self, log_dir: str | os.PathLike | None):
        self.dir = Path(log_dir) if log_dir else None
        self.lock = threading.Lock()
        self.lines: list[dict] = []
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.path = self.dir / "requests.jsonl"
            # Fail at start, not on the first request, if the log is not writable.
            with open(self.path, "a", encoding="utf-8"):
                pass

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            self.lines.append(record)
            if self.dir:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

    def _publish(self, name: str, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.dir / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def system_prompt(self, sha: str, text: str) -> None:
        if not self.dir:
            return
        name = f"system-{sha[:16]}.txt"
        with self.lock:
            if not (self.dir / name).exists():
                self._publish(name, text)

    def state(self, capture) -> None:
        """Publish the state. `capture` is called under the log's lock, so
        snapshots are taken and published in one order and an older snapshot
        can never replace a newer one."""
        if not self.dir:
            return
        with self.lock:
            self._publish("state.json", json.dumps(capture(), indent=1, ensure_ascii=False))


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Seconds a socket read may wait: a request head, a body, an idle keep-alive.
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _head(self, status: int, headers: list[tuple[str, str]]) -> None:
        # send_response would add a Server header; write the status line ourselves.
        self.send_response_only(status)
        self.send_header("Date", formatdate(usegmt=True))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        """The whole body within `timeout` seconds of starting to read it:
        a total deadline, not a per-read one, so a trickled body is cut off."""
        n = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or n is None or not DIGITS.fullmatch(n.strip()):
            self.close_connection = True
            raise ApiError(411, "invalid_request_error", "Content-Length is required")
        n = int(n.strip())
        if n > limit:
            self.close_connection = True
            raise ApiError(413, "request_too_large", "Request exceeds the maximum allowed number of bytes.")
        deadline = time.monotonic() + self.timeout
        parts, got = [], 0
        try:
            while got < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(n - got, 65536))
                if not chunk:
                    self.close_connection = True
                    raise ApiError(400, "invalid_request_error", "The request body ended early")
                parts.append(chunk)
                got += len(chunk)
        except (socket.timeout, TimeoutError):
            self.close_connection = True
            raise ApiError(408, "invalid_request_error", "The request body did not arrive in time") from None
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        return b"".join(parts)

    def _json(self, status: int, obj: Any, extra: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self._head(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body))), *(extra or [])])
        if self.command != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()


def make_api_handler(app: "App", read_timeout: float) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(_Base):
        timeout = read_timeout

        def do_POST(self) -> None:  # noqa: N802
            app.handle_api(self)

        do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_POST

    return ApiHandler


def make_admin_handler(app: "App", read_timeout: float) -> type[BaseHTTPRequestHandler]:
    class AdminHandler(_Base):
        timeout = read_timeout

        def do_POST(self) -> None:  # noqa: N802
            app.handle_admin(self)

        do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_POST

    return AdminHandler


class App:
    def __init__(self, episode: Episode | None = None, log_dir: str | os.PathLike | None = None,
                 *, read_timeout: float = 30.0, admin_max_body: int = ADMIN_MAX_BODY_BYTES):
        self.episode = episode or Episode()
        self.log = RequestLog(log_dir)
        self.read_timeout = read_timeout
        self.admin_max_body = admin_max_body
        self.servers: list[ThreadingHTTPServer] = []

    # ------------------------------------------------------------- API

    def handle_api(self, h: BaseHTTPRequestHandler) -> None:
        started = time.time()
        rid = ids.request_id()
        path = h.path.split("?", 1)[0]
        # Any request on the model listener ends the server's freshness for import.
        self.episode.mark_answered()
        record: dict[str, Any] = {"time": _iso(started), "request_id": rid, "method": h.command, "path": h.path}
        written = False

        def flush() -> None:
            nonlocal written
            if written:
                return
            written = True
            record["duration_ms"] = round((time.time() - started) * 1000, 1)
            self.log.write(record)
            if "seq" in record:
                self.log.state(self.episode.state)

        outcome: Outcome | None = None
        attempted = False
        try:
            try:
                if path not in API_PATHS:
                    h.close_connection = True
                    raise ApiError(404, "not_found_error", "Not found.")
                if h.command != "POST":
                    h.close_connection = True
                    raise ApiError(405, "invalid_request_error", "Method not allowed.")
                body = h._read_body()
                if not (h.headers.get("x-api-key") or h.headers.get("authorization")):
                    raise ApiError(401, "authentication_error", "x-api-key header is required")
                record["request_bytes"] = len(body)
                raw = _parse_json(body)
                if path == "/v1/messages/count_tokens":
                    req = parse_request(raw, count_tokens=True)
                    total, _ = prompt_tokens(req.system, req.tools, req.messages)
                    record.update(kind="count_tokens", status=200, input_tokens=total)
                    flush()
                    h._json(200, {"input_tokens": total}, [("request-id", rid)])
                    return
                req = parse_request(raw)
                if req.system is not None:
                    text = _system_text(req.system)
                    self.log.system_prompt(hashlib.sha256(text.encode()).hexdigest(), text)
                outcome = self.episode.handle(req)
            except ApiError as e:
                record.update(status=e.status, error={"type": e.etype, "message": e.message, "code": e.code})
                if getattr(e, "identity", None):
                    record["identity"] = e.identity
                flush()
                h._json(e.status, e.body(rid), [("request-id", rid), ("x-should-retry", "false")])
                return
            record.update(outcome.log)
            record["status"] = 200
            # The log line and the state file are on disk before the reply
            # goes out, so a host that acts on the reply reads current state.
            flush()
            self.episode.reply_attempt(outcome.msg_id)  # before the first byte
            attempted = True
            self._send(h, req.stream, outcome, rid)
            self.episode.reply_sent(outcome.msg_id, True)
            self.log.state(self.episode.state)
        except OSError as e:
            # The client went away, or a write timed out, after the log line.
            h.close_connection = True
            if attempted:
                self.episode.reply_sent(outcome.msg_id, False)
                self.log.state(self.episode.state)
            self.log.write({"time": _iso(time.time()), "request_id": rid, "send_failed": f"{type(e).__name__}: {e}"[:300],
                            "message_id": outcome.msg_id if outcome else None})
        except Exception as e:  # a bug; the client still gets the API's error shape
            h.close_connection = True
            record.update(status=500, internal_error=f"{type(e).__name__}: {e}"[:500])
            print("scripted: internal error\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            if written:
                self.log.write({"time": _iso(time.time()), "request_id": rid, "internal_error": record["internal_error"]})
            try:
                h._json(500, ApiError(500, "api_error", "Internal server error").body(rid), [("request-id", rid)])
            except OSError:
                pass
        finally:
            flush()

    def _send(self, h: BaseHTTPRequestHandler, stream: bool, o: Outcome, rid: str) -> None:
        if o.delay > 0:
            time.sleep(o.delay)
        if not stream:
            h._json(200, message_body(o.model, o.msg_id, o.blocks, o.stop_reason, o.usage), [("request-id", rid)])
            return
        h._head(200, [("Content-Type", "text/event-stream; charset=utf-8"), ("Transfer-Encoding", "chunked"),
                      ("Cache-Control", "no-cache"), ("request-id", rid)])
        for name, data in sse_events(o.model, o.msg_id, o.blocks, o.stop_reason, o.usage, o.seed or None):
            chunk = encode_event(name, data)
            h.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            h.wfile.flush()
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()

    # ----------------------------------------------------------- admin

    def handle_admin(self, h: BaseHTTPRequestHandler) -> None:
        path, method = h.path.split("?", 1)[0], h.command
        routes = {("GET", "/healthz"), ("GET", "/state"), ("GET", "/script"), ("GET", "/export"),
                  ("POST", "/script"), ("POST", "/reset"), ("POST", "/next"), ("POST", "/import")}
        try:
            try:
                if (method, path) not in routes:
                    h.close_connection = True
                    raise ApiError(404, "not_found_error", "unknown admin path or method")
                limit = self.admin_max_body if path == "/import" else MAX_BODY_BYTES
                body = h._read_body(limit) if method == "POST" else b""
                if path == "/healthz":
                    h._json(200, {"status": "ok"})
                elif path == "/state":
                    h._json(200, self.episode.state())
                elif path == "/export":
                    data = json.dumps(self.episode.export_state(), ensure_ascii=False, separators=(",", ":")).encode()
                    # /import takes {"state": <export>}: the export plus that wrapper must fit.
                    wrapped = len(data) + len(IMPORT_WRAPPER_OVERHEAD)
                    if wrapped > self.admin_max_body:
                        h._json(413, {"ok": False, "error": f"the export is {len(data)} bytes, {wrapped} with the import "
                                                            f"wrapper, more than the {self.admin_max_body} bytes "
                                                            "/import accepts"})
                    else:
                        h._head(200, [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
                        h.wfile.write(data)
                elif method == "GET":
                    s = self.episode.script
                    if s is None:
                        h._json(200, None)
                    else:
                        # The source text, numbers exactly as written.
                        data = s.source.encode()
                        h._head(200, [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
                        h.wfile.write(data)
                elif path == "/script":
                    try:
                        script = load_script(body)
                    except ScriptError as e:
                        h._json(400, {"ok": False, "errors": e.errors})
                        return
                    self.episode.load(script)
                    self.log.write({"time": _iso(time.time()), "admin": "load", "script": script.summary()})
                    self.log.state(self.episode.state)
                    h._json(200, {"ok": True, "script": script.summary()})
                elif path == "/reset":
                    self.episode.reset()
                    self.log.write({"time": _iso(time.time()), "admin": "reset"})
                    self.log.state(self.episode.state)
                    h._json(200, {"ok": True})
                elif path == "/import":
                    try:
                        data = _parse_json(body)
                        if not isinstance(data, dict) or "state" not in data or set(data) - {"state", "next_script"}:
                            raise ValueError('expected {"state": <export>} or '
                                             '{"state": <export>, "next_script": "<the script\'s JSON text>"}')
                        summary = self.episode.import_state(data["state"], data.get("next_script"))
                    except ImportRefused as e:
                        h._json(409, {"ok": False, "error": str(e)})
                        return
                    except (ValueError, ApiError) as e:
                        h._json(400, {"ok": False, "error": getattr(e, "message", str(e))})
                        return
                    self.log.write({"time": _iso(time.time()), "admin": "import", "imported": summary})
                    self.log.state(self.episode.state)
                    h._json(200, {"ok": True, "imported": summary})
                else:  # /next
                    try:
                        data = _parse_json(body)
                        if not isinstance(data, dict) or set(data) - {"index", "text"} or "index" not in data:
                            raise ValueError('expected {"index": N} or {"index": N, "text": "..."}')
                        a = self.episode.announce(data["index"], data.get("text"))
                    except (ValueError, ApiError) as e:
                        h._json(400, {"ok": False, "error": getattr(e, "message", str(e))})
                        return
                    self.log.write({"time": _iso(time.time()), "admin": "next", "announced": a})
                    self.log.state(self.episode.state)
                    h._json(200, {"ok": True, "announced": a})
            except ApiError as e:
                h._json(e.status, {"ok": False, "error": e.message})
        except OSError:
            h.close_connection = True
        except Exception as e:  # a bug in this server, reported, never silent
            h.close_connection = True
            print("scripted: admin internal error\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            try:
                h._json(500, {"ok": False, "error": f"internal error: {type(e).__name__}"})
            except OSError:
                pass

    # ------------------------------------------------------------ serve

    def start(self, api: tuple[str, int], admin: tuple[str, int] | None) -> tuple[tuple[str, int], tuple[str, int] | None]:
        """Bind and serve in background threads. Returns the bound addresses."""
        api_srv = _Server(api, make_api_handler(self, self.read_timeout))
        self.servers.append(api_srv)
        threading.Thread(target=api_srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        admin_addr = None
        if admin is not None:
            adm = _Server(admin, make_admin_handler(self, self.read_timeout))
            self.servers.append(adm)
            threading.Thread(target=adm.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            admin_addr = adm.server_address[:2]
        return api_srv.server_address[:2], admin_addr

    def stop(self) -> None:
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.servers.clear()


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    x = float(text)
    if not math.isfinite(x):
        raise ValueError(f"{text} is too large for a number")
    return x


def _parse_json(body: bytes) -> Any:
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
        # JSON may escape a lone surrogate ("\ud800"), which is not text and
        # cannot be written to the log or hashed. Refuse it here.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except UnicodeEncodeError:
        raise ApiError(400, "invalid_request_error", "The request body holds a string that is not valid Unicode") from None
    except UnicodeDecodeError as e:
        raise ApiError(400, "invalid_request_error", f"The request body is not UTF-8: {e.reason}") from None
    except RecursionError:
        raise ApiError(400, "invalid_request_error", "The request body is nested too deeply") from None
    except ValueError as e:  # JSONDecodeError is a ValueError
        raise ApiError(400, "invalid_request_error", f"The request body is not valid JSON: {e}") from None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"
