"""Request log and optional body store, with secret redaction.

Covered: every loaded key, as its literal bytes, in log lines, stored bodies,
meta files and tracebacks. Values are redacted before they are serialised, so
JSON escaping cannot hide a key in a log line. A stored request body that is
still holds a key in JSON-escaped form after redaction is not stored.
Stored response bodies hold only units that passed screening.

A stored body is written to a temporary file and published by rename only
when complete. Anything else publishes the fixed marker `[not stored]`. The
reason is one of the fixed codes below; the relay puts it in both the log line
(`request_body_withheld`, `response_body_withheld`) and the meta file.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .screen import unescape
from .usage import DecodeError, Decoder

REDACTED = "[REDACTED]"
MARKER = b"[not stored]\n"
# Reason codes for a body that was not stored.
NOT_DECODED = "content_encoding_not_decoded"
DECODE_FAILED = "decode_failed"
TOO_LARGE = "too_large"
ESCAPED_KEY = "credential_in_escaped_form"
WITHHELD = "response_withheld"  # the gateway refused the response
NO_RESPONSE = "no_response"  # no upstream response arrived
INCOMPLETE = "incomplete"
WRITE_FAILED = "write_failed"
REASONS = (NOT_DECODED, DECODE_FAILED, TOO_LARGE, ESCAPED_KEY, WITHHELD, NO_RESPONSE, INCOMPLETE, WRITE_FAILED)


def _open_private(path: Path, mode: str = "wb"):
    """Open a file readable and writable by the owner only."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, mode)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # A host-mounted directory may refuse; the files are still 0600.
        pass


def _publish(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with _open_private(tmp) as fh:
        fh.write(data)
    os.replace(tmp, path)


class Redactor:
    """Replaces every configured secret in bytes, text or structured values."""

    def __init__(self, secrets: list[str]):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._secrets_b = [s.encode("utf-8") for s in self.secrets]

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        return value

    def data(self, value: bytes) -> bytes:
        for secret in self._secrets_b:
            value = value.replace(secret, REDACTED.encode())
        return value

    def walk(self, value):
        """A copy of a JSON-like value with secrets redacted in every string."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.walk(k): self.walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.walk(v) for v in value]
        return value


def _escaped_secret(data: bytes, secrets: list[str]) -> bool:
    """A key that survives literal redaction, once JSON escapes are undone textually."""
    if not secrets or b"\\" not in data:
        return False
    plain = unescape(data)
    return any(s.encode() in plain for s in secrets)


class RequestLog:
    """Appends one JSON object per line to <log_dir>/requests.jsonl (mode 0600)."""

    def __init__(self, log_dir: str, redactor: Redactor):
        self.dir = Path(log_dir)
        _private_dir(self.dir)
        self.path = self.dir / "requests.jsonl"
        self._redactor = redactor
        self._lock = threading.Lock()
        with self._lock:
            _open_private(self.path, "ab").close()

    def write(self, record: dict) -> None:
        clean = self._redactor.walk(record)
        line = self._redactor.text(json.dumps(clean, separators=(",", ":"), ensure_ascii=False))
        with self._lock:
            with _open_private(self.path, "ab") as fh:
                # A lone surrogate (from an escaped one in a request field)
                # cannot be encoded; write it as its JSON escape instead of
                # losing the row.
                fh.write(line.encode("utf-8", errors="backslashreplace") + b"\n")


class BodyWriter:
    """Collects one response body from screened units into a temporary file.

    `finish(complete=True)` publishes it; anything else publishes the marker.
    """

    def __init__(self, path: Path, redactor: Redactor, max_bytes: int):
        self.path = path
        self._tmp = path.with_name(path.name + ".part")
        self._r = redactor
        self._max = max_bytes
        self._size = 0
        self._done = False
        self.withheld: str | None = None
        try:
            self._fh = _open_private(self._tmp)
        except OSError:
            self._fh = None
            self.withheld = WRITE_FAILED

    def write(self, data: bytes) -> None:
        if self._fh is None or not data or self.withheld:
            return
        self._size += len(data)
        if self._size > self._max:
            self.withhold(TOO_LARGE)
            return
        try:
            self._fh.write(self._r.data(data))
        except OSError:
            self.withhold(WRITE_FAILED)

    def withhold(self, reason: str) -> None:
        if self.withheld is None:
            self.withheld = reason

    def finish(self, complete: bool) -> None:
        if self._done:
            return
        self._done = True
        if not complete:
            self.withhold(INCOMPLETE)
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                self.withhold(WRITE_FAILED)
        if self.withheld is None:
            try:
                os.replace(self._tmp, self.path)
                return
            except OSError:
                self.withhold(WRITE_FAILED)
        try:
            self._tmp.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _publish(self.path, MARKER)
        except OSError:
            pass


class BodyStore:
    """Stores request and response bodies under <log_dir>/bodies/<id>.*"""

    def __init__(self, log_dir: str, redactor: Redactor, max_bytes: int):
        self.dir = Path(log_dir) / "bodies"
        _private_dir(self.dir)
        self._r = redactor
        self._max = max_bytes

    def write_request(self, request_id: str, body: bytes, content_encoding: str | None) -> tuple[str, str | None]:
        """Store the request body decoded, within the size cap.

        Returns (file name, reason code if not stored).
        """
        path = self.dir / f"{request_id}.request"
        reason = None
        plain = bytearray()
        decoder = Decoder(content_encoding)
        if not decoder.supported:
            reason = NOT_DECODED
        elif decoder.encoding == "identity" and len(body) > self._max:
            reason = TOO_LARGE
        else:
            try:
                for piece in decoder.feed(body):
                    plain.extend(piece)
                    if len(plain) > self._max:
                        reason = TOO_LARGE
                        break
                if reason is None:
                    for piece in decoder.finish():
                        plain.extend(piece)
                    if len(plain) > self._max:
                        reason = TOO_LARGE
                    elif not decoder.complete:
                        reason = DECODE_FAILED
            except DecodeError:
                reason = DECODE_FAILED
        data = MARKER
        if reason is None:
            data = self._r.data(bytes(plain))
            # Literal copies are redacted now; one that survives is escaped.
            if _escaped_secret(data, self._r.secrets):
                reason, data = ESCAPED_KEY, MARKER
        del plain
        _publish(path, data)
        return path.name, reason

    def open_response(self, request_id: str) -> BodyWriter:
        return BodyWriter(self.dir / f"{request_id}.response", self._r, self._max)

    def write_marker(self, request_id: str) -> str:
        """Publish the marker as the response body; returns the file name."""
        path = self.dir / f"{request_id}.response"
        _publish(path, MARKER)
        return path.name

    def write_meta(self, request_id: str, meta: dict) -> None:
        clean = self._r.walk(meta)
        text = self._r.text(json.dumps(clean, indent=2, ensure_ascii=False))
        _publish(self.dir / f"{request_id}.meta.json", (text + "\n").encode("utf-8"))
