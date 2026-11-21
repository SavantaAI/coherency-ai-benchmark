"""Token usage extraction from screened response units.

The relay hands this module whole SSE events, or a whole non-streaming body,
only after they passed screening. Parsing never affects relaying.

Usage is expected only on model inference endpoints. Each endpoint and
response shape has required counters; a counter that is missing or not a
non-negative integer stays null and makes the record partial.
"""

from __future__ import annotations

import json
import zlib
from typing import Iterator

from .screen import sse_data

DECODABLE = ("gzip", "x-gzip", "deflate", "identity")
PIECE = 1024 * 1024

# (usage format, method, route-relative path) -> inference kind.
INFERENCE = {
    ("anthropic", "POST", "/v1/messages"): "anthropic_messages",
    ("openai", "POST", "/v1/chat/completions"): "openai_chat",
    ("openai", "POST", "/v1/responses"): "openai_responses",
}

# OpenAI Responses output items that the provider executes itself.
OPENAI_SERVER_TOOL_ITEMS = {
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "mcp_call",
    "mcp_list_tools",
}


def inference_kind(usage_format: str | None, method: str | None, route_path: str | None) -> str | None:
    return INFERENCE.get((usage_format, method, route_path))


class DecodeError(Exception):
    pass


class Decoder:
    """Undoes gzip (all members) or deflate (zlib or raw) incrementally.

    Used for request bodies kept in the body store. Responses are always
    requested uncompressed.
    """

    def __init__(self, content_encoding: str | None):
        enc = (content_encoding or "").strip(" \t").lower()
        self.encoding = enc or "identity"
        self.supported = self.encoding in DECODABLE
        self._kind = {"gzip": "gzip", "x-gzip": "gzip", "deflate": "deflate"}.get(self.encoding)
        self._z = zlib.decompressobj(47) if self._kind == "gzip" else None
        self._head = b""
        self.complete = self._kind is None

    def _pieces(self, data: bytes) -> Iterator[bytes]:
        z = self._z
        while data or (not z.eof and z.unconsumed_tail):
            out = z.decompress(data, PIECE)
            if out:
                yield out
            if z.eof:
                # Everything after the member's end is in unused_data, and
                # only there: unconsumed_tail must not be added to it.
                rest = z.unused_data
                if self._kind == "gzip" and rest:
                    self._z = z = zlib.decompressobj(47)
                    data = rest
                    continue
                if rest:
                    raise DecodeError("bytes after the end of the compressed stream")
                return
            data = z.unconsumed_tail
            if not data and not out:
                return

    def feed(self, data: bytes) -> Iterator[bytes]:
        if not data:
            return
        if self._kind is None:
            yield data
            return
        if self._kind == "deflate" and self._z is None:
            self._head += data
            if len(self._head) < 2:
                return
            b0, b1 = self._head[0], self._head[1]
            zlib_header = (b0 & 0x0F) == 8 and (b0 * 256 + b1) % 31 == 0
            self._z = zlib.decompressobj(15 if zlib_header else -15)
            data, self._head = self._head, b""
        if self._z.eof:
            if self._kind != "gzip":
                raise DecodeError("bytes after the end of the compressed stream")
            self._z = zlib.decompressobj(47)
        try:
            yield from self._pieces(data)
        except zlib.error as exc:
            raise DecodeError(str(exc)) from None
        self.complete = self._z.eof

    def finish(self) -> Iterator[bytes]:
        if self._kind is None:
            return
        if self._z is None:
            if self._head:
                raise DecodeError("compressed stream too short")
            return
        try:
            out = self._z.flush()
        except zlib.error as exc:
            raise DecodeError(str(exc)) from None
        if out:
            yield out
        self.complete = self._z.eof


def _count(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _details(usage: dict, key: str) -> dict | None:
    value = usage.get(key)
    return value if isinstance(value, dict) else None


def normalise(kind: str | None, usage) -> tuple[dict, list[str]]:
    """Normalised counters and the names of required counters that are missing.

    input_tokens is uncached input, cached_input_tokens cache reads,
    cache_write_tokens cache creation (null for OpenAI, which reports none).
    """
    tokens = {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None, "output_tokens": None}
    if kind is None:
        return tokens, []
    if not isinstance(usage, dict):
        return tokens, ["usage"]
    missing = []

    def need(name, value):
        if value is None:
            missing.append(name)
        return value

    if kind == "anthropic_messages":
        tokens["input_tokens"] = need("input_tokens", _count(usage.get("input_tokens")))
        tokens["cached_input_tokens"] = need("cache_read_input_tokens", _count(usage.get("cache_read_input_tokens")))
        tokens["cache_write_tokens"] = need("cache_creation_input_tokens", _count(usage.get("cache_creation_input_tokens")))
        tokens["output_tokens"] = need("output_tokens", _count(usage.get("output_tokens")))
        return tokens, missing
    if kind == "openai_chat":
        total, out, detail_key, cached_key = "prompt_tokens", "completion_tokens", "prompt_tokens_details", "cached_tokens"
    else:
        total, out, detail_key, cached_key = "input_tokens", "output_tokens", "input_tokens_details", "cached_tokens"
    total_in = need(total, _count(usage.get(total)))
    details = _details(usage, detail_key)
    cached = need(f"{detail_key}.{cached_key}", _count(details.get(cached_key)) if details else None)
    tokens["output_tokens"] = need(out, _count(usage.get(out)))
    if total_in is not None and cached is not None and cached <= total_in:
        tokens["input_tokens"] = total_in - cached
        tokens["cached_input_tokens"] = cached
    elif total_in is not None and cached is not None:
        missing.append(f"{detail_key}.{cached_key} larger than {total}")
    return tokens, missing


class UsageParser:
    """Collects model name, usage and completeness from screened units.

    `status()` returns "complete" when the provider's terminal event (or a
    whole non-streaming body) arrived with every required counter and the
    response finished; "partial" when some usage was seen but not all of that
    holds; and "unavailable" when there is none or none was expected.
    """

    def __init__(self, usage_format: str, kind: str | None, streaming: bool):
        self.format = usage_format
        self.kind = kind
        self.streaming = streaming
        self.expected = kind is not None
        self.usage: dict | None = None
        self.response_model: str | None = None
        self.terminal = False
        self.server_tool_use = False
        self.problems: list[str] = []

    # -- feeding -------------------------------------------------------------

    def event(self, data: bytes) -> None:
        """One whole SSE event."""
        try:
            self._event(data)
        except Exception as exc:  # noqa: BLE001 - parsing must never raise
            self._problem(f"parse error: {exc.__class__.__name__}")

    def body(self, data: bytes) -> None:
        """A whole non-streaming body."""
        if not data:
            return
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError):
            if self.expected:
                self._problem("response body is not JSON")
            return
        try:
            self._object(obj, whole_body=True)
        except Exception as exc:  # noqa: BLE001
            self._problem(f"parse error: {exc.__class__.__name__}")

    def _problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def _event(self, data: bytes) -> None:
        # Only a byte order mark at the start of the stream is ignored.
        first = not getattr(self, "_seen_event", False)
        self._seen_event = True
        payload = sse_data(data, strip_bom=first)
        if payload is None:
            return
        if payload.strip() == b"[DONE]":
            if self.format == "openai":
                self.terminal = True
            return
        try:
            obj = json.loads(payload)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self._problem("invalid event JSON")
            return
        self._object(obj)

    def status(self, transport_complete: bool) -> tuple[str, str | None, dict]:
        """(usage_status, usage_error, normalised tokens). usage_error is None when not expected."""
        tokens, missing = normalise(self.kind, self.usage)
        if not self.expected:
            return "unavailable", None, tokens
        reasons = list(self.problems)
        if not transport_complete:
            reasons.append("response did not finish")
        if self.usage is None:
            return "unavailable", "; ".join(reasons) or "no usage in response", tokens
        if missing:
            reasons.append("missing " + ", ".join(missing))
        if not self.terminal:
            reasons.append("no terminal event")
        if reasons:
            return "partial", "; ".join(reasons), tokens
        return "complete", None, tokens

    # -- shapes ---------------------------------------------------------------

    def _object(self, obj, whole_body: bool = False) -> None:
        if not isinstance(obj, dict):
            return
        if self.format == "anthropic":
            self._anthropic(obj, whole_body)
        elif self.format == "openai":
            self._openai(obj, whole_body)

    def _take(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        if self.usage is None:
            self.usage = {}
        for key, value in usage.items():
            # A counter that is supplied but null or invalid replaces the
            # earlier value: normalise() then reports it missing.
            self.usage[key] = value
        stu = self.usage.get("server_tool_use")
        if isinstance(stu, dict) and any(_count(v) for v in stu.values()):
            self.server_tool_use = True

    def _anthropic(self, obj: dict, whole_body: bool) -> None:
        kind = obj.get("type")
        if kind == "message_start":
            message = obj.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    self.response_model = message["model"]
                self._take(message.get("usage"))
        elif kind == "message_delta":
            # message_delta usage counts are cumulative, so later values win.
            self._take(obj.get("usage"))
        elif kind == "message_stop":
            self.terminal = True
        elif whole_body and kind == "message":
            if isinstance(obj.get("model"), str):
                self.response_model = obj["model"]
            self._take(obj.get("usage"))
            self.terminal = self.usage is not None

    def _openai_items(self, items) -> None:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("type") in OPENAI_SERVER_TOOL_ITEMS:
                    self.server_tool_use = True

    def _openai(self, obj: dict, whole_body: bool) -> None:
        kind = obj.get("type") if isinstance(obj.get("type"), str) else ""
        if kind.startswith("response."):
            # Responses API stream events.
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") in OPENAI_SERVER_TOOL_ITEMS:
                self.server_tool_use = True
            if any(kind.startswith(f"response.{t}.") for t in OPENAI_SERVER_TOOL_ITEMS):
                self.server_tool_use = True
            response = obj.get("response")
            if isinstance(response, dict):
                if isinstance(response.get("model"), str):
                    self.response_model = response["model"]
                self._openai_items(response.get("output"))
                if isinstance(response.get("usage"), dict):
                    self.usage = dict(response["usage"])
                if kind in ("response.completed", "response.incomplete", "response.failed"):
                    self.terminal = True
            return
        if isinstance(obj.get("model"), str):
            self.response_model = obj["model"]
        if whole_body and obj.get("object") == "response":
            self._openai_items(obj.get("output"))
        # Chat completions: usage sits in the final object or the final chunk.
        # Earlier chunks carry "usage": null.
        if isinstance(obj.get("usage"), dict):
            self.usage = dict(obj["usage"])
            if whole_body:
                self.terminal = True
