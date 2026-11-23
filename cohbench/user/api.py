"""The HTTP client for an OpenAI-compatible `/chat/completions` endpoint.

`Client(cfg, key).choose(messages, keys, deadline)` sends one closed
question: the conversation so far, a strict JSON schema whose one field
`choice` must be one of `keys`, temperature 0, the pinned seed and a small
`max_tokens`. It checks the answer itself and checks that the response says
it was served by the pinned model and provider.

Failures raise `EndpointError`, a `PolicyError`, so the runner ends the
episode as unusable: a refused connection, a timeout, an HTTP error, a
redirect, a response that is not one finished chat completion, an answer
outside the options, a served model or provider other than the pinned one, a
response that arrives after the verdict's deadline. A transport error, a 429
or a 5xx is retried at most once, after a short pause, with the identical
request bytes, and only if the deadline leaves room. Nothing is ever guessed.

Nothing the endpoint sends, and no exception text, reaches an error message
or a record as it came: errors carry an HTTP status and a fixed category,
metadata is recorded only when it matches a conservative pattern, and any
occurrence of the key is replaced by `[key]` (`Client.scrub`). The key is read
from a file (`read_key`) and goes only into the Authorization header of a
request that may not be redirected.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Any, Callable, Mapping, Sequence

from cohbench.runner.policy import DeadlineExceeded, EndpointError, PolicyError

from . import prompts as P
from .config import Config, thaw

KEY_ENV = "COHBENCH_CLASSIFIER_KEY_FILE"
KEY_MARK = "[key]"
WITHHELD = "[withheld]"
SAFE_META = re.compile(r"[A-Za-z0-9_.\-]{1,64}")  # used with fullmatch
FINISHED = "stop"
CHUNK = 1 << 16


class ServedMismatch(EndpointError):
    """The response says another model or provider served it."""


def read_key(path: str | os.PathLike[str] | None = None) -> str:
    """The API key from `path`, or from the file named by $COHBENCH_CLASSIFIER_KEY_FILE."""
    p = path or os.environ.get(KEY_ENV)
    if not p:
        raise EndpointError(f"no API key file: set {KEY_ENV} or pass --key-file")
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        raise EndpointError("the API key file cannot be read") from None
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise EndpointError("the API key file is not UTF-8 text") from None
    if not key or any(c.isspace() or not c.isprintable() for c in key):
        raise EndpointError("the API key file must hold one key on one line")
    return key


def retryable_status(code: int) -> bool:
    """429 and every 5xx are retried once; every other status is final."""
    return code == 429 or 500 <= code <= 599


def status_category(code: int) -> str:
    if code == 429:
        return "rate limited"
    if code in (401, 403):
        return "not authorised"
    if code == 404:
        return "not found"
    if 400 <= code <= 499:
        return "request refused"
    if 500 <= code <= 599:
        return "server error"
    return "unexpected status"


class _Transient(Exception):
    """A failure worth one retry: its category, fit to show."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class _Final(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class _Attempt:
    def __init__(self):
        self.cancelled = threading.Event()
        self.reserved = threading.Event()
        self.gate = threading.Lock()
        self.ticket = None
        self.sent = False
        self.submitting = False
        self.submission_done = threading.Event()

    def written(self, entry):
        self.ticket = entry["call"]

    def cancel(self):
        with self.gate:
            self.cancelled.set()
            if not self.submitting:
                self.submission_done.set()


class _SendingSocket:
    """Record transmission only after the socket accepts request bytes."""

    def __init__(self, sock, attempt):
        self.sock, self.attempt = sock, attempt

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data, flags=0):
        remaining = memoryview(data)
        while remaining:
            count = self.sock.send(remaining, flags)
            if not count:
                raise OSError("connection closed during submission")
            self.attempt.sent = True
            remaining = remaining[count:]


class Client:
    def __init__(
        self,
        cfg: Config,
        key: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ledger=None,
    ):
        self._finishes = []
        self._transports = []
        self._accounting_errors = []
        self._previous_served = None
        self.ledger = ledger
        self.cfg = cfg
        self._key = key
        u = urllib.parse.urlsplit(
            cfg.endpoint["base_url"].rstrip("/") + "/chat/completions"
        )
        self._https = u.scheme == "https"
        self._host = u.hostname or ""
        self._port = u.port or (443 if self._https else 80)
        self._path = u.path + (f"?{u.query}" if u.query else "")
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.RLock()

    def __repr__(self) -> str:  # never shows the key
        return f"Client(model={self.cfg.endpoint['model']!r})"

    def scrub(self, text: str) -> str:
        """`text` with every occurrence of the key replaced by the marker."""
        return text.replace(self._key, KEY_MARK) if self._key else text

    def meta(self, v: Any) -> str | None:
        """A value from the response fit to record or show: short, plain and
        free of the key, or the withheld marker."""
        if v is None:
            return None
        if isinstance(v, str) and SAFE_META.fullmatch(v) and self._key not in v:
            return v
        return WITHHELD

    def session(self) -> threading.RLock:
        """Held for a whole verdict, so one verdict's questions are not interleaved with another's."""
        return self._lock

    def body(
        self, messages: Sequence[Mapping[str, str]], keys: Sequence[str]
    ) -> dict[str, Any]:
        e, s = self.cfg.endpoint, self.cfg.sampling
        b: dict[str, Any] = thaw(e.get("extra_body", {}))
        b.update(
            model=e["model"],
            messages=[dict(m) for m in messages],
            temperature=s["temperature"],
            seed=s["seed"],
            max_tokens=s["max_tokens"],
            stream=False,
            provider=thaw(e["provider"]),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": self.cfg.schema["name"],
                    "strict": True,
                    "schema": P.schema(keys),
                },
            },
        )
        if e.get("protocol", "openrouter") == "openai":
            b.pop("provider")
        return b

    def flush(self, timeout_s=0.25):
        """Bounded end-of-episode cleanup; blocked platform calls remain isolated."""
        end = time.monotonic() + timeout_s
        for thread in self._transports + self._finishes:
            thread.join(max(0, end-time.monotonic()))
        self._transports = [t for t in self._transports if t.is_alive()]
        self._finishes = [t for t in self._finishes if t.is_alive()]
        if self._accounting_errors:
            raise EndpointError("the call ledger could not be written")
        return {"blocked_transport_threads": len(self._transports),
                "pending_accounting_threads": len(self._finishes)}

    def _record_attempt(self, attempt, usage, outcome, ms):
        if self.ledger is None:
            return

        def write():
            try:
                attempt.reserved.wait()
                if attempt.ticket is not None:
                    attempt.submission_done.wait()
                    if not attempt.sent:
                        self.ledger.not_sent(attempt.ticket)
                    self.ledger.finish(attempt.ticket, usage, outcome, ms)
            except Exception:
                self._accounting_errors.append(True)

        # Non-daemon: the process also waits at exit if a runner has no close hook.
        thread = threading.Thread(target=write)
        self._finishes.append(thread)
        thread.start()

    def _post(self, data, cutoff, attempt):
        """Bound reservation and transport by the caller's one absolute cutoff.

        Accounting can wait for a slow reservation flush after the caller leaves.
        The shared gate decides whether a request started before cancellation.
        """
        done, result = threading.Event(), []

        def exchange():
            try:
                try:
                    if self.ledger is not None:
                        self.ledger.start(
                            self.cfg,
                            deadline=cutoff,
                            clock=self.clock,
                            written=attempt.written,
                        )
                finally:
                    attempt.reserved.set()
                result.append((True, self._exchange(data, cutoff, attempt)))
            except (_Final, _Transient, EndpointError) as exc:
                result.append((False, exc))
            except Exception:
                result.append(
                    (False, _Transient("transport error (connection or protocol)"))
                )
            finally:
                attempt.submission_done.set()
                done.set()

        thread = threading.Thread(target=exchange, daemon=True)
        self._transports.append(thread)
        thread.start()
        wall_cutoff = time.monotonic() + max(0, cutoff - self.clock())
        while not done.is_set() and self.clock() < cutoff and time.monotonic() < wall_cutoff:
            done.wait(min(0.05, max(0, cutoff - self.clock()), max(0, wall_cutoff - time.monotonic())))
        completed = done.is_set()
        if not completed or self.clock() >= cutoff:
            attempt.cancel()
            if self.ledger is not None and (
                not attempt.reserved.is_set() or attempt.ticket is None
            ):
                raise _Final("attempt not started: ledger unavailable before deadline")
            raise _Transient("transport error (timeout at attempt deadline)")
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def _exchange(self, data: bytes, cutoff: float, attempt) -> bytes:
        """Send and read before the original cutoff, including setup time.

        Platform lookup may finish after abandonment. It cannot proceed to
        request submission after cancellation. A timer closes a held socket.
        """
        fired = attempt.cancelled
        budget = cutoff - self.clock()
        if budget <= 0 or fired.is_set():
            raise _Transient("transport error (timeout at attempt deadline)")
        held: list[socket.socket] = []
        conn: http.client.HTTPConnection | None = None
        resp: http.client.HTTPResponse | None = None

        def cut_off() -> None:
            fired.set()
            for sock in held:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def late() -> bool:
            return fired.is_set() or self.clock() >= cutoff

        timer = threading.Timer(budget, cut_off)
        timer.daemon = True
        timer.start()
        try:
            try:
                if self._https:
                    conn = http.client.HTTPSConnection(
                        self._host,
                        self._port,
                        timeout=budget,
                        context=ssl.create_default_context(),
                    )
                else:
                    conn = http.client.HTTPConnection(
                        self._host, self._port, timeout=budget
                    )
                if late():
                    raise _Transient("transport error (timeout at attempt deadline)")
                conn.connect()
                if conn.sock is not None:
                    held.append(conn.sock)
                    conn.sock = _SendingSocket(conn.sock, attempt)
                if late():
                    raise _Transient("transport error (timeout at attempt deadline)")
                # Admit submission under the same gate as cancellation.
                # Do not hold the gate during potentially blocking socket I/O.
                with attempt.gate:
                    if late():
                        raise _Transient(
                            "transport error (timeout at attempt deadline)"
                        )
                    attempt.submitting = True
                try:
                    conn.request(
                        "POST",
                        self._path,
                        body=data,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self._key}",
                        },
                    )
                finally:
                    attempt.submission_done.set()
                resp = conn.getresponse()
                status = resp.status
                if status != 200:
                    if 300 <= status <= 399:
                        raise _Final(
                            f"HTTP {status} (redirect refused: an authenticated request is never followed)"
                        )
                    category = f"HTTP {status} ({status_category(status)})"
                    raise (
                        _Transient(category)
                        if retryable_status(status)
                        else _Final(category)
                    )
                declared = resp.getheader("Content-Length")
                parts: list[bytes] = []
                while True:
                    if late():
                        raise _Transient(
                            "transport error (timeout at attempt deadline)"
                        )
                    piece = resp.read1(CHUNK)
                    if not piece:
                        break
                    parts.append(piece)
                if late():
                    raise _Transient("transport error (timeout at attempt deadline)")
                raw = b"".join(parts)
                # A declared length is read up to and no further; ending short of it is a cut response.
                if declared is not None and (
                    not declared.strip().isdigit() or int(declared) != len(raw)
                ):
                    raise _Transient(
                        "transport error (the body ended before its declared length)"
                    )
                return raw
            except (_Transient, _Final):
                raise
            except (http.client.HTTPException, OSError, ValueError):
                if fired.is_set() or self.clock() > cutoff:
                    raise _Transient(
                        "transport error (timeout at attempt deadline)"
                    ) from None
                raise _Transient("transport error (connection or protocol)") from None
        finally:
            timer.cancel()
            for closer in (resp, conn):
                if closer is not None:
                    try:
                        closer.close()
                    except Exception:  # noqa: BLE001 - teardown never replaces the outcome
                        pass

    def _parse(self, raw: bytes) -> Mapping[str, Any]:
        try:
            doc = json.loads(
                raw, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
            )
        except _Duplicate:
            raise EndpointError("the response repeats a field") from None
        except _Constant:
            raise EndpointError(
                "the response holds a non-standard JSON constant (NaN or Infinity)"
            ) from None
        except ValueError:
            raise EndpointError("the response is not JSON") from None
        if not isinstance(doc, Mapping):
            raise EndpointError("the response is not a JSON object")
        return doc

    def choose(
        self,
        messages: Sequence[Mapping[str, str]],
        keys: Sequence[str],
        deadline: float,
    ) -> dict[str, Any]:
        """Decide against the deadline before scheduling the accounting write."""
        data = json.dumps(self.body(messages, keys), ensure_ascii=False).encode("utf-8")
        lim = self.cfg.limits
        attempts = 0
        t0 = self.clock()
        while True:
            if deadline - self.clock() <= 0:
                raise DeadlineExceeded(
                    "the verdict's deadline passed before an answer arrived"
                )
            attempts += 1
            started = self.clock()
            cutoff = min(deadline, started + lim["request_timeout_s"])
            attempt = _Attempt()
            usage = {}
            outcome = "transport_error"
            try:
                raw = self._post(data, cutoff, attempt)
                outcome = "invalid_response"
                doc = self._parse(raw)
                usage = _usage(doc)
                choice = self._check(doc, keys)
                served = self._served(doc)
                if self.clock() > deadline:
                    outcome = "deadline_exceeded"
                    raise DeadlineExceeded(
                        "the verdict's deadline passed before the answer was accepted"
                    )
                answer = {
                    "choice": choice,
                    "served": served,
                    "usage": usage,
                    "ms": round((self.clock() - t0) * 1000),
                    "attempts": attempts,
                }
                if (
                    self._previous_served is not None
                    and served != self._previous_served
                ):
                    answer["served_change"] = {
                        "before": self._previous_served,
                        "after": served,
                    }
                self._previous_served = served
                outcome = "accepted"
                return answer
            except _Final as exc:
                expired = self.clock() >= deadline
                outcome = "deadline_exceeded" if expired else "rejected"
                error = DeadlineExceeded if expired else EndpointError
                raise error(exc.category) from None
            except _Transient as exc:
                if self.clock() >= deadline:
                    outcome = "deadline_exceeded"
                failure = exc.category
            finally:
                self._record_attempt(
                    attempt, usage, outcome, round((self.clock() - started) * 1000)
                )
            if attempts > lim["retries"] or deadline - self.clock() <= lim["backoff_s"]:
                error = DeadlineExceeded if self.clock() >= deadline else EndpointError
                raise error(
                    f"{failure} after {attempts} attempt{'s' if attempts > 1 else ''}"
                )
            self.sleep(lim["backoff_s"])

    def _check(self, doc: Mapping[str, Any], keys: Sequence[str]) -> str:
        if "error" in doc:
            raise EndpointError(
                "the endpoint reported an error in a successful response"
            )
        ch = doc.get("choices")
        if not isinstance(ch, list) or len(ch) != 1 or not isinstance(ch[0], Mapping):
            raise EndpointError("the response does not hold exactly one choice")
        fr = ch[0].get("finish_reason")
        if fr != FINISHED:
            raise EndpointError(
                f"the answer did not finish normally (finish_reason {self.meta(fr)})"
            )
        msg = ch[0].get("message")
        if not isinstance(msg, Mapping) or msg.get("refusal"):
            raise EndpointError(
                "the response holds no answer (missing message or a refusal)"
            )
        content = msg.get("content")
        if not isinstance(content, str):
            raise EndpointError("the response holds no answer text")
        try:
            ans = json.loads(
                content, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
            )
        except _Constant:
            raise EndpointError(
                "the answer holds a non-standard JSON constant"
            ) from None
        except _Duplicate:
            raise EndpointError("the answer repeats a field") from None
        except ValueError:
            raise EndpointError("the answer is not JSON") from None
        if (
            not isinstance(ans, Mapping)
            or set(ans) != {P.FIELD}
            or ans[P.FIELD] not in keys
        ):
            raise EndpointError(f"the answer is outside the options {list(keys)}")
        return ans[P.FIELD]

    def _served(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        want = self.cfg.endpoint["expect"]
        model, provider = doc.get("model"), doc.get("provider")
        if model != want["model"]:
            raise ServedMismatch(
                f"served by model {self.meta(model)!r}, the config pins {want['model']!r}"
            )
        if provider != want["provider"]:
            raise ServedMismatch(
                f"served by provider {self.meta(provider)!r}, the config pins {want['provider']!r}"
            )
        got: dict[str, Any] = {"model": model, "provider": provider}
        for field in (
            "system_fingerprint",
            "revision",
            "served_revision",
            "model_revision",
            "weight_revision",
            "weights_revision",
            "deployment_revision",
        ):
            if doc.get(field) is not None:
                got[field] = self.meta(doc[field])
        return got


class _Duplicate(ValueError):
    pass


class _Constant(ValueError):
    pass


def _no_constant(name: str) -> Any:
    raise _Constant(name)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Duplicate()
    return dict(pairs)


def _usage(doc: Mapping[str, Any]) -> dict[str, int]:
    u = doc.get("usage")
    if not isinstance(u, Mapping):
        return {}

    def num(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    out = {k: u[k] for k in ("prompt_tokens", "completion_tokens") if num(u.get(k))}
    det = u.get("prompt_tokens_details")
    if isinstance(det, Mapping) and num(det.get("cached_tokens")):
        out["cached_tokens"] = det["cached_tokens"]
    det = u.get("completion_tokens_details")
    if isinstance(det, Mapping) and num(det.get("reasoning_tokens")):
        out["reasoning_tokens"] = det["reasoning_tokens"]
    return out
