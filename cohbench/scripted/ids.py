"""Identifiers in the shape the real API uses.

Observed in the driver proof's stored responses (2026-11-21):

    msg_011ChGm1s6X3TATahrxa9eMy      message, time ordered
    req_011ChGm1rwLVv6FLPZMMyKuD      request id header, time ordered
    toolu_012xhHrFd4gghNMW99BR6Axq    tool call, no visible ordering

Each is a prefix, "01", and 22 base58 characters. Message and request ids
begin with a slowly changing time part. Nothing here encodes anything about
this server.
"""

from __future__ import annotations

import secrets
import threading
import time

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TIME_CHARS = 8
_BODY_CHARS = 22

_lock = threading.Lock()
_last_ms = 0


def _b58(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        n, r = divmod(n, 58)
        out.append(ALPHABET[r])
    return "".join(reversed(out))


def _random(width: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(width))


def _time_part() -> str:
    global _last_ms
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            ms = _last_ms + 1
        _last_ms = ms
    # Milliseconds since 2000-01-01 fit in 8 base58 digits for centuries.
    return _b58(ms - 946_684_800_000, _TIME_CHARS)


def message_id() -> str:
    return "msg_01" + _time_part() + _random(_BODY_CHARS - _TIME_CHARS)


def request_id() -> str:
    return "req_01" + _time_part() + _random(_BODY_CHARS - _TIME_CHARS)


def tool_use_id() -> str:
    return "toolu_01" + _random(_BODY_CHARS)


def is_message_id(value: str) -> bool:
    return _shaped(value, "msg_01")


def is_request_id(value: str) -> bool:
    return _shaped(value, "req_01")


def is_tool_use_id(value: str) -> bool:
    return _shaped(value, "toolu_01")


def _shaped(value: str, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    body = value[len(prefix):]
    return len(body) == _BODY_CHARS and all(c in ALPHABET for c in body)
