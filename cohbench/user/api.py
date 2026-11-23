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

from cohbench.runner.policy import PolicyError

from . import prompts as P
from .config import Config, thaw

KEY_ENV = "COHBENCH_CLASSIFIER_KEY_FILE"
KEY_MARK = "[key]"
WITHHELD = "[withheld]"
SAFE_META = re.compile(r"[A-Za-z0-9_.\-]{1,64}")  # used with fullmatch
FINISHED = "stop"
CHUNK = 1 << 16


class EndpointError(PolicyError):
    """The endpoint failed, timed out, or answered outside what was asked."""


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

    def flush(self):
        """Wait for accounting outside the verdict deadline, before CLI exit."""
        for thread in self._finishes:
            thread.join()
        self._finishes.clear()
        if self._accounting_errors:
            raise EndpointError("the call ledger could not be written")

    def _record_attempt(self, ticket, usage, outcome, ms):
        if self.ledger is None:
            return

        def write():
            try:
                self.ledger.finish(ticket, usage, outcome, ms)
            except Exception:
                self._accounting_errors.append(True)

        # Non-daemon: the process also waits at exit if a runner has no close hook.
        thread = threading.Thread(target=write)
        self._finishes.append(thread)
        thread.start()

    def _post(self, data, deadline):
        """Abandon even blocked DNS/connect work at the attempt cutoff.

        An abandoned daemon can finish platform lookup/connection setup later;
        it checks cancellation before sending a request and closes its socket.
        Its result is never consumed. Socket reads also have the exchange timer.
        """
        budget = min(self.cfg.limits["request_timeout_s"], deadline - self.clock())
        if budget <= 0:
            raise _Final("the verdict's deadline passed before connection setup")
        wall_cutoff = time.monotonic() + budget
        cancelled, done = threading.Event(), threading.Event()
        result = []

        def exchange():
            try:
                result.append((True, self._exchange(data, deadline, cancelled)))
            except (_Final, _Transient) as exc:
                result.append((False, exc))
            except Exception:
                result.append(
                    (False, _Transient("transport error (connection or protocol)"))
                )
            finally:
                done.set()

        thread = threading.Thread(target=exchange, daemon=True)
        thread.start()
        remaining = max(0, min(deadline - self.clock(), wall_cutoff - time.monotonic()))
        if not done.wait(remaining):
            cancelled.set()
            if self.clock() >= deadline:
                raise _Final(
                    "the verdict's deadline passed during connection or response"
                )
            raise _Transient("transport error (timeout)")
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def _exchange(self, data: bytes, deadline: float, fired) -> bytes:
        """One attempt: the raw body of a 200 response, read completely before
        an absolute cutoff (the smaller of the request timeout and what is left
        of the verdict's deadline) that covers creating the TLS context,
        connecting, sending, the status line, the headers and the body.

        The socket is taken right after connecting and held to the end: the
        connection object may let go of it once the headers say the server
        will close, while the response still reads from it. A timer shuts that
        socket at the cutoff, which wakes any blocked read; between partial
        reads the clock is checked too. The response and the connection are
        closed on every exit, and a failure while closing changes nothing.
        Nothing is followed: only status 200 is a completion."""
        start = self.clock()
        budget = min(self.cfg.limits["request_timeout_s"], deadline - start)
        if budget <= 0:
            raise _Final("the verdict's deadline passed before an answer arrived")
        cutoff = start + budget
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
            if self.clock() > deadline:
                raise _Final(
                    "the verdict's deadline passed while the response was read"
                )
            return fired.is_set() or self.clock() > cutoff

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
                if fired.is_set():
                    raise _Transient("transport error (timeout)")
                conn.connect()
                if conn.sock is not None:
                    held.append(conn.sock)
                if fired.is_set():
                    raise _Transient("transport error (timeout)")
                conn.request(
                    "POST",
                    self._path,
                    body=data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._key}",
                    },
                )
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
                        raise _Transient("transport error (timeout)")
                    piece = resp.read1(CHUNK)
                    if not piece:
                        break
                    parts.append(piece)
                if late():
                    raise _Transient("transport error (timeout)")
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
                if self.clock() > deadline:
                    raise _Final(
                        "the verdict's deadline passed while the response was read"
                    ) from None
                if fired.is_set() or self.clock() > cutoff:
                    raise _Transient("transport error (timeout)") from None
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
                raise EndpointError(
                    "the verdict's deadline passed before an answer arrived"
                )
            attempts += 1
            ticket = self.ledger.start(self.cfg) if self.ledger else None
            started = self.clock()
            usage = {}
            outcome = "transport_error"
            try:
                raw = self._post(data, deadline)
                outcome = "invalid_response"
                doc = self._parse(raw)
                usage = _usage(doc)
                choice = self._check(doc, keys)
                served = self._served(doc)
                if self.clock() > deadline:
                    outcome = "deadline_exceeded"
                    raise EndpointError(
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
                outcome = (
                    "deadline_exceeded" if "deadline" in exc.category else "rejected"
                )
                raise EndpointError(exc.category) from None
            except _Transient as exc:
                if "timeout" in exc.category:
                    outcome = "deadline_exceeded"
                failure = exc.category
            finally:
                self._record_attempt(
                    ticket, usage, outcome, round((self.clock() - started) * 1000)
                )
            if attempts > lim["retries"] or deadline - self.clock() <= lim["backoff_s"]:
                raise EndpointError(
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
