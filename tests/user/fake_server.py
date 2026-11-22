"""A stand-in for an OpenAI-compatible `/chat/completions` endpoint, for the
classifier's tests.

The answer to a request comes from `decide(messages, keys)`, where `keys` is
the enum of the request's response schema; the default picks the first key.
`mode` makes it misbehave once or always: see `MODES`. Every request body and
its Authorization header are kept in `requests`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Decide = Callable[[list[dict[str, str]], list[str]], str]

MODES = {
    "http500": "HTTP 500 on every request",
    "http429": "HTTP 429 on every request",
    "http400": "HTTP 400 on every request (not retried)",
    "fail_once": "HTTP 503 on the first request, then normal",
    "drop_once": "close the connection without a reply on the first request, then normal",
    "slow": "sleep `slow_s` before answering",
    "outside": "answer text that is not one of the options",
    "not_json": "answer content that is not JSON",
    "extra_field": "answer JSON with a second field",
    "two_choices": "two choices in the response",
    "length": "finish_reason length",
    "refusal": "a refusal instead of content",
    "wrong_model": "report another model",
    "wrong_provider": "report another provider",
    "no_provider": "report no provider",
    "error_body": "HTTP 200 with an error object",
    "content_filter": "finish_reason content_filter with otherwise valid content",
    "duplicate": "an answer object with the field twice, the last one valid",
    "drip": "send the body in pieces over `slow_s` seconds",
    "redirect": "HTTP 307 to `redirect_to`",
    "echo_500": "HTTP 500 whose body holds the request's key",
    "echo_400": "HTTP 400 whose body holds the request's key",
    "echo_not_json": "HTTP 200 whose body is not JSON and holds the key",
    "echo_answer": "an answer whose choice is the key",
    "echo_fingerprint": "system_fingerprint is the key",
    "odd_fingerprint": "system_fingerprint with characters outside the safe pattern",
    "echo_provider": "report the key as the provider",
    "echo_finish": "finish_reason is the key",
    "dup_provider": "an envelope with two provider keys, the last one the pinned provider",
    "short_body": "a body 42 bytes shorter than its declared Content-Length, then the connection closes",
    "partial": "HTTP 206 carrying a valid completion",
    "drip_headers": "send the status line and headers in pieces over `slow_s` seconds",
    "bad_status": "a malformed status line holding the request's key",
    "see_other": "HTTP 303 to `redirect_to` (a status a client would ordinarily follow with a GET)",
    "chunked_cut": "a chunked body cut inside the JSON, then the connection closes",
    "chunked_no_crlf": "a chunked body whose JSON is complete, ending `0\\r\\n` without the final line break",
    "nan_envelope": "an envelope holding NaN (a non-standard JSON constant)",
    "drip_close": "Connection: close, then the body in pieces over `slow_s` seconds",
}


class Fake:
    def __init__(self, model: str = "test/model-1", provider: str = "TestProvider", decide: Decide | None = None):
        self.model = model
        self.provider = provider
        self.decide: Decide = decide or (lambda messages, keys: keys[0])
        self.mode: str | None = None
        self.slow_s = 3.0
        self.requests: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.auth: list[str | None] = []
        self.methods: list[str] = []
        self.redirect_to: str | None = None
        self.on_request: Callable[[], None] | None = None  # called before each answer is sent
        self._served = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, doc: Any) -> None:
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                fake.methods.append("GET")
                fake.auth.append(self.headers.get("Authorization"))
                self._send(405, {"error": {"message": "GET"}})

            def do_POST(self) -> None:
                fake.methods.append("POST")
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw or b"{}")
                fake.raw.append(raw)
                fake.requests.append(body)
                auth = self.headers.get("Authorization")
                fake.auth.append(auth)
                key = (auth or "").removeprefix("Bearer ")
                n = fake._served
                fake._served += 1
                if self.path != "/v1/chat/completions":
                    self._send(404, {"error": {"message": "not found"}})
                    return
                m = fake.mode
                if fake.on_request is not None:
                    fake.on_request()
                if m == "bad_status":
                    self.wfile.write(f"HTTP/1.1 XYZ {key}\r\n\r\n".encode())
                    self.close_connection = True
                    return
                if m == "drip_headers":
                    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    for i in range(0, len(head), 4):
                        self.wfile.write(head[i:i + 4])
                        self.wfile.flush()
                        time.sleep(fake.slow_s / (len(head) / 4))
                    self.close_connection = True
                    return
                if m in ("redirect", "see_other"):
                    self.send_response(303 if m == "see_other" else 307)
                    self.send_header("Location", fake.redirect_to or "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if m in ("echo_500", "echo_400"):
                    raw_err = json.dumps({"error": {"message": f"bad key {key}"}}).encode()
                    self.send_response(500 if m == "echo_500" else 400)
                    self.send_header("Content-Length", str(len(raw_err)))
                    self.end_headers()
                    self.wfile.write(raw_err)
                    return
                if m == "echo_not_json":
                    raw_err = f"<html>key {key}</html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw_err)))
                    self.end_headers()
                    self.wfile.write(raw_err)
                    return
                if m == "drop_once" and n == 0:
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                if m in ("http500", "http429", "http400") or (m == "fail_once" and n == 0):
                    code = {"http500": 500, "http429": 429, "http400": 400}.get(m, 503)
                    self._send(code, {"error": {"message": "boom", "code": code}})
                    return
                if m == "error_body":
                    self._send(200, {"error": {"message": "upstream failed"}})
                    return
                if m == "slow":
                    time.sleep(fake.slow_s)
                keys = body["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"]
                got = fake.decide(body["messages"], keys)
                content: Any = json.dumps({"choice": got})
                if m == "outside":
                    content = json.dumps({"choice": "maybe"})
                elif m == "not_json":
                    content = "I think the answer is " + got
                elif m == "extra_field":
                    content = json.dumps({"choice": got, "why": "because"})
                elif m == "duplicate":
                    content = '{"choice": "maybe", "choice": "%s"}' % got
                elif m == "echo_answer":
                    content = json.dumps({"choice": key})
                msg: dict[str, Any] = {"role": "assistant", "content": content}
                if m == "refusal":
                    msg = {"role": "assistant", "content": None, "refusal": "no"}
                finish = {"length": "length", "content_filter": "content_filter", "echo_finish": key}.get(m or "", "stop")
                choice = {"index": 0, "message": msg, "finish_reason": finish}
                doc: dict[str, Any] = {"id": f"gen-{n}", "object": "chat.completion", "model": fake.model,
                                       "choices": [choice] * (2 if m == "two_choices" else 1),
                                       "usage": {"prompt_tokens": sum(len(x["content"]) for x in body["messages"]) // 4,
                                                 "completion_tokens": 6, "prompt_tokens_details": {"cached_tokens": 0}},
                                       "system_fingerprint": {"echo_fingerprint": key, "odd_fingerprint": "fp <b>x</b>"}.get(m or "", "fp-test")}
                if m != "no_provider":
                    doc["provider"] = {"wrong_provider": "OtherProvider", "echo_provider": key}.get(m or "", fake.provider)
                if m == "wrong_model":
                    doc["model"] = "test/model-2"
                if m in ("chunked_cut", "chunked_no_crlf"):
                    raw_ok = json.dumps(doc).encode()
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    if m == "chunked_cut":
                        half = raw_ok[: len(raw_ok) // 2]
                        self.wfile.write(b"%x\r\n" % len(raw_ok) + half)
                    else:
                        self.wfile.write(b"%x\r\n" % len(raw_ok) + raw_ok + b"\r\n0\r\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if m == "nan_envelope":
                    raw_ok = json.dumps(doc).replace('"completion_tokens": 6', '"completion_tokens": NaN').encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw_ok)))
                    self.end_headers()
                    self.wfile.write(raw_ok)
                    return
                if m == "drip_close":
                    raw_ok = json.dumps(doc).encode()
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(raw_ok)))
                    self.end_headers()
                    step = max(len(raw_ok) // 20, 1)
                    for i in range(0, len(raw_ok), step):
                        self.wfile.write(raw_ok[i:i + step])
                        self.wfile.flush()
                        time.sleep(fake.slow_s / 20)
                    self.close_connection = True
                    return
                if m == "dup_provider":
                    raw_ok = json.dumps(doc)[:-1].encode() + b', "provider": "%s"}' % fake.provider.encode()
                    raw_ok = raw_ok.replace(b'"provider": "%s"' % fake.provider.encode(), b'"provider": "OtherProvider"', 1)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw_ok)))
                    self.end_headers()
                    self.wfile.write(raw_ok)
                    return
                if m == "short_body":
                    raw_ok = json.dumps(doc).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw_ok) + 42))
                    self.end_headers()
                    self.wfile.write(raw_ok)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if m == "partial":
                    raw_ok = json.dumps(doc).encode()
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(raw_ok)))
                    self.end_headers()
                    self.wfile.write(raw_ok)
                    return
                if m == "drip":
                    raw_ok = json.dumps(doc).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw_ok)))
                    self.end_headers()
                    step = max(len(raw_ok) // 20, 1)
                    for i in range(0, len(raw_ok), step):
                        self.wfile.write(raw_ok[i:i + step])
                        self.wfile.flush()
                        time.sleep(fake.slow_s / 20)
                    return
                self._send(200, doc)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self) -> "Fake":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
