"""The Anthropic Messages wire format, as far as Pi uses it.

What is built here and where each shape comes from:

- Streaming responses copy the event sequence of the driver proof's stored
  responses (run 2026-11-21T210951-74da66, 104 streamed responses):
  message_start, content_block_start, ping (once, after the first block
  start), content_block_delta (text_delta or input_json_delta),
  content_block_stop, then message_delta and message_stop. Text blocks come
  before tool_use blocks. A tool_use block starts with `input: {}` and
  `caller: {"type": "direct"}`; its first input_json_delta is the empty
  string and its last is "}". Each event's JSON has a run of spaces before
  its final brace, as the real ones do.
- The non-streaming body is the message_start message with its content and
  stop reason filled in. Pi never sends a non-streaming request, so that shape
  is inferred from the streamed one and is not compared with a real body.
- Error bodies follow the stored 4xx bodies: {"type": "error", "error":
  {"type", "message"}, "request_id"}. Thread rejections add
  `error.details.error_code`, the field Pi 1.6.0 reads
  (`getThreadErrorCode` in pi-ai's anthropic-messages.js). The codes come
  from Pi's client; how the real API words or numbers those errors is not
  known here.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_BYTES = 32 * 1024 * 1024


# ECMAScript's whitespace, as String.prototype.trim() strips it: the
# WhiteSpace set (TAB, VT, FF, SPACE, NBSP, ZWNBSP U+FEFF and every Zs
# character) and the LineTerminator set (LF, CR, LS, PS). Pi's client decides
# blank text with trim(), so the server must too. Python's str.strip() differs:
# it keeps U+FEFF and strips U+001C to U+001F and U+0085, which trim() keeps.
# U+200B is not whitespace in either.
ES_WHITESPACE = (
    "\u0009\u000b\u000c\u0020\u00a0\ufeff"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
    "\u000a\u000d\u2028\u2029"
)


def es_strip(text: str) -> str:
    """`text.trim()` as ECMAScript defines it."""
    return text.strip(ES_WHITESPACE)


def es_blank(text: str) -> bool:
    """True when ECMAScript's trim() leaves nothing: what Pi treats as blank."""
    return not es_strip(text)


class ApiError(Exception):
    """A request the server refuses, in the API's error shape."""

    def __init__(self, status: int, etype: str, message: str, *, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.etype = etype
        self.message = message
        self.code = code

    def body(self, request_id: str) -> dict:
        err: dict[str, Any] = {"type": self.etype, "message": self.message}
        if self.code is not None:
            err["details"] = {"error_code": self.code}
        return {"type": "error", "error": err, "request_id": request_id}


def invalid(message: str, *, code: str | None = None) -> ApiError:
    return ApiError(400, "invalid_request_error", message, code=code)


# ------------------------------------------------------------------ request


@dataclass
class Item:
    """One block of the conversation, flattened.

    `key` is the canonical form used for the conversation hash chain, or None
    for blocks outside the chain (system-role messages, which Pi places
    differently on the wire depending on whether it continues a thread)."""

    role: str               # "user", "assistant", "system"
    kind: str               # "text", "tool_use", "tool_result", "tool_addition", ...
    msg_index: int
    data: dict
    key: tuple | None


@dataclass
class Request:
    raw: dict
    model: str
    stream: bool
    max_tokens: int
    messages: list[dict]
    system: Any
    tools: list[dict] | None
    thread: dict | None
    items: list[Item] = field(default_factory=list)

    @property
    def thread_type(self) -> str:
        return self.thread["type"] if self.thread else "none"


def _blocks(content: Any, path: str) -> list[dict]:
    """Content as a list of blocks. `parse_request` has already checked it."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def tool_result_text(block: dict) -> str:
    c = block.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return "\n".join(b["text"] for b in c if b.get("type") == "text")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def flatten(messages: list[dict]) -> list[Item]:
    """Every content block in order, with its hash-chain key.

    Keys keep only what identifies a block: text (whitespace-trimmed), a tool
    call's id and name (ids are unique and this server made them), a tool
    result's id, text and error flag. Cache markers, `caller`, and the
    string-versus-block form of content do not change a key.

    The canonical form agrees with how Pi 1.6.0 rebuilds history when it
    resends a conversation (pi-ai dist/api/anthropic-messages.js,
    convertMessages, and transform-messages.js): whitespace-only text blocks,
    which Pi drops, get no key; a tool call's input, which Pi resends as
    `arguments ?? {}`, is not keyed; a tool result's text blocks, which Pi
    joins with newlines into one string, key as that joined text; a user
    message's content keys the same as a string or as blocks; system-role
    messages, which Pi moves to just before the next assistant message, are
    left out; consecutive tool results key the same in one message or
    several."""
    items: list[Item] = []
    for i, m in enumerate(messages):
        role = m["role"]
        for b in _blocks(m["content"], f"messages.{i}.content"):
            t = b["type"]
            if role == "system":
                items.append(Item(role, t, i, b, None))
                continue
            if t == "text" and es_blank(b["text"]):
                # Pi 1.6.0 drops whitespace-only text blocks when it builds a
                # request (convertMessages), so they are no part of the history.
                items.append(Item(role, t, i, b, None))
                continue
            if t == "text":
                key = (role, "text", es_strip(b["text"]))
            elif t == "tool_use":
                key = (role, "tool_use", b["id"], b["name"])
            elif t == "tool_result":
                key = (role, "tool_result", b["tool_use_id"], es_strip(tool_result_text(b)), bool(b.get("is_error", False)))
            else:
                key = (role, t, _digest({k: v for k, v in b.items() if k != "cache_control"}))
            items.append(Item(role, t, i, b, key))
    return items


def _check_schema(s: Any, path: str) -> None:
    if not isinstance(s, dict):
        raise invalid(f"{path}: Input should be a valid dictionary")
    req = s.get("required")
    if req is not None and (not isinstance(req, list) or not all(isinstance(x, str) for x in req)):
        raise invalid(f"{path}.required: Input should be a valid list of strings")
    props = s.get("properties")
    if props is not None and not isinstance(props, dict):
        raise invalid(f"{path}.properties: Input should be a valid dictionary")


def _check_tool(t: Any, path: str) -> None:
    if not isinstance(t, dict):
        raise invalid(f"{path}: Input should be a valid dictionary")
    if not isinstance(t.get("name"), str) or not t["name"]:
        raise invalid(f"{path}.name: Field required")
    if "input_schema" in t:
        _check_schema(t["input_schema"], f"{path}.input_schema")
    if "defer_loading" in t and not isinstance(t["defer_loading"], bool):
        raise invalid(f"{path}.defer_loading: Input should be a valid boolean")


def _check_text(b: dict, path: str) -> None:
    if not isinstance(b.get("text"), str):
        raise invalid(f"{path}.text: Input should be a valid string")


def _check_block(b: Any, role: str, path: str) -> None:
    if not isinstance(b, dict) or not isinstance(b.get("type"), str):
        raise invalid(f"{path}: each content block needs a string `type`")
    t = b["type"]
    if t == "text":
        _check_text(b, path)
    elif t == "tool_use":
        if role != "assistant":
            raise invalid(f"{path}: `tool_use` blocks are only allowed in assistant messages")
        for k in ("id", "name"):
            if not isinstance(b.get(k), str) or not b[k]:
                raise invalid(f"{path}.{k}: Input should be a valid string")
        if "input" in b and not isinstance(b["input"], dict):
            raise invalid(f"{path}.input: Input should be a valid dictionary")
    elif t == "tool_result":
        if role != "user":
            raise invalid(f"{path}: `tool_result` blocks are only allowed in user messages")
        if not isinstance(b.get("tool_use_id"), str) or not b["tool_use_id"]:
            raise invalid(f"{path}.tool_use_id: Input should be a valid string")
        if "is_error" in b and not isinstance(b["is_error"], bool):
            raise invalid(f"{path}.is_error: Input should be a valid boolean")
        c = b.get("content")
        if c is not None and not isinstance(c, str):
            if not isinstance(c, list):
                raise invalid(f"{path}.content: Input should be a valid string or list")
            for k, x in enumerate(c):
                if not isinstance(x, dict) or not isinstance(x.get("type"), str):
                    raise invalid(f"{path}.content.{k}: each content block needs a string `type`")
                if x["type"] == "text":
                    _check_text(x, f"{path}.content.{k}")
    elif t in ("tool_addition", "tool_removal"):
        if role != "system":
            raise invalid(f"{path}: `{t}` blocks are only allowed in system messages")
        tool = b.get("tool")
        if not isinstance(tool, dict):
            raise invalid(f"{path}.tool: Input should be a valid dictionary")
        if t == "tool_addition":
            _check_tool(tool.get("definition"), f"{path}.tool.definition")
        elif not isinstance(tool.get("name"), str) or not tool["name"]:
            raise invalid(f"{path}.tool.name: Field required")


def parse_request(raw: Any, *, count_tokens: bool = False) -> Request:
    """Check a request body and build the Request. Raises ApiError (400) for
    anything malformed and nothing else; every field the server later reads
    is checked here."""
    try:
        return _parse(raw, count_tokens)
    except RecursionError:
        raise invalid("The request body is nested too deeply") from None


def _parse(raw: Any, count_tokens: bool) -> Request:
    if not isinstance(raw, dict):
        raise invalid("Request body must be a JSON object")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise invalid("model: Field required" if model is None else "model: Input should be a valid string")
    max_tokens = raw.get("max_tokens")
    if max_tokens is None and not count_tokens:
        raise invalid("max_tokens: Field required")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
        raise invalid("max_tokens: Input should be a positive integer")
    stream = raw.get("stream", False)
    if not isinstance(stream, bool):
        raise invalid("stream: Input should be a valid boolean")
    messages = raw.get("messages")
    if messages is None:
        raise invalid("messages: Field required")
    if not isinstance(messages, list) or not messages:
        raise invalid("messages: Input should be a non-empty list")
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise invalid(f"messages.{i}: Input should be a valid dictionary")
        role = m.get("role")
        if not isinstance(role, str) or role not in ("user", "assistant", "system"):
            raise invalid(f"messages.{i}.role: Input should be 'user' or 'assistant'")
        if "content" not in m:
            raise invalid(f"messages.{i}.content: Field required")
        content = m["content"]
        if not isinstance(content, (str, list)):
            raise invalid(f"messages.{i}.content: Input should be a valid string or list")
        if isinstance(content, list):
            for j, b in enumerate(content):
                _check_block(b, role, f"messages.{i}.content.{j}")
    system = raw.get("system")
    if system is not None:
        if isinstance(system, list):
            for j, b in enumerate(system):
                if not isinstance(b, dict) or b.get("type") != "text":
                    raise invalid(f"system.{j}: Input should be a text block")
                _check_text(b, f"system.{j}")
        elif not isinstance(system, str):
            raise invalid("system: Input should be a valid string or list of text blocks")
    tools = raw.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise invalid("tools: Input should be a valid list")
        for i, t in enumerate(tools):
            _check_tool(t, f"tools.{i}")
    thread = None
    if not count_tokens and raw.get("thread") is not None:
        thread = raw["thread"]
        if not isinstance(thread, dict) or thread.get("type") not in ("create", "continue"):
            raise invalid("thread.type: Input should be 'create' or 'continue'")
        if thread["type"] == "continue" and (not isinstance(thread.get("previous_message_id"), str)
                                             or not thread["previous_message_id"]):
            raise invalid("thread.previous_message_id: Field required")
    req = Request(raw, model, stream, int(max_tokens or 0), messages, system, tools, thread)
    req.items = flatten(messages)
    return req


def check_tool_pairs(messages: list[dict], prior_tool_ids: list[str] | None) -> None:
    """The API's rule: every tool call is answered by a tool_result at the
    start of the next message, and no tool_result names an unknown call.
    `prior_tool_ids` are the calls of the message before `messages` (a thread
    continue), or None."""
    expected = list(prior_tool_ids) if prior_tool_ids else None
    for i, m in enumerate(messages):
        blocks = _blocks(m["content"], f"messages.{i}.content")
        role = m["role"]
        if role == "assistant":
            if expected:
                raise invalid(
                    f"messages.{i}: `tool_use` ids were found without `tool_result` blocks immediately "
                    f"after: {', '.join(expected)}. Each `tool_use` block must have a corresponding "
                    "`tool_result` block in the next message.")
            expected = [b["id"] for b in blocks if b["type"] == "tool_use"] or None
            continue
        if role == "system":
            if expected:
                raise invalid(f"messages.{i}: `tool_result` blocks must immediately follow the `tool_use` message")
            continue
        results = [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"]
        if expected:
            lead = []
            for b in blocks:
                if b["type"] != "tool_result":
                    break
                lead.append(b["tool_use_id"])
            missing = [x for x in expected if x not in lead]
            if missing:
                raise invalid(
                    f"messages.{i}: `tool_use` ids were found without `tool_result` blocks immediately "
                    f"after: {', '.join(missing)}. Each `tool_use` block must have a corresponding "
                    "`tool_result` block in the next message.")
            extra = [x for x in results if x not in expected]
            if extra:
                raise invalid(
                    f"messages.{i}.content: unexpected `tool_use_id` found in `tool_result` blocks: "
                    f"{', '.join(map(str, extra))}. Each `tool_result` block must have a corresponding "
                    "`tool_use` block in the previous message.")
            expected = None
        elif results:
            raise invalid(
                f"messages.{i}.content: unexpected `tool_use_id` found in `tool_result` blocks: "
                f"{', '.join(map(str, results))}. Each `tool_result` block must have a corresponding "
                "`tool_use` block in the previous message.")


def offered_tools(tools: list[dict] | None) -> set[str]:
    """Callable tool names: deferred ones (`defer_loading`) are not callable
    until a `tool_addition` brings them in."""
    return {t["name"] for t in tools or [] if not t.get("defer_loading")}


def tool_schemas(tools: list[dict] | None) -> dict[str, dict]:
    return {t["name"]: t.get("input_schema") or {} for t in tools or [] if not t.get("defer_loading")}


def apply_tool_changes(offered: set[str], schemas: dict[str, dict], items: list[Item]) -> None:
    for it in items:
        if it.role != "system":
            continue
        if it.kind == "tool_addition":
            d = it.data["tool"]["definition"]
            offered.add(d["name"])
            schemas[d["name"]] = d.get("input_schema") or {}
        elif it.kind == "tool_removal":
            name = it.data["tool"]["name"]
            offered.discard(name)
            schemas.pop(name, None)


def schema_problem(schema: dict, value: dict) -> str | None:
    """The two checks a stand-in can make cheaply: required keys present and,
    when the schema forbids extras, no unknown keys."""
    if not isinstance(schema, dict):
        return None
    missing = [k for k in schema.get("required") or [] if k not in value]
    if missing:
        return f"missing required argument(s) {', '.join(missing)}"
    props = schema.get("properties")
    if schema.get("additionalProperties") is False and isinstance(props, dict):
        extra = [k for k in value if k not in props]
        if extra:
            return f"argument(s) {', '.join(extra)} not in the tool's schema"
    return None


# ------------------------------------------------------------------- tokens


# Calibrated on the driver proof's first stored Pi requests (2026-11-21):
# 14,956 and 15,248 characters of system prompt, tools and message, serialised
# as JSON, against 5,549 and 5,669 tokens reported by the real API.
CHARS_PER_TOKEN = 2.7


def estimate_tokens(value: Any) -> int:
    """A rough count from length. Not the real tokenizer."""
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) if text else 0


def prompt_tokens(system: Any, tools: list[dict] | None, messages: list[dict]) -> tuple[int, int]:
    """(total, cached prefix) for a prompt, in estimated tokens. The cached
    prefix ends at the last block carrying a cache marker; Pi marks the
    newest block, so for Pi nearly all of a prompt is cached prefix."""
    total = estimate_tokens(system) + estimate_tokens(tools)
    cached = 0
    for m in messages:
        blocks = m.get("content")
        blocks = [{"type": "text", "text": blocks}] if isinstance(blocks, str) else blocks or []
        for b in blocks:
            total += estimate_tokens({k: v for k, v in b.items() if k != "cache_control"})
            if isinstance(b, dict) and b.get("cache_control"):
                cached = total
    return total, cached


# ----------------------------------------------------------------- response


@dataclass
class Usage:
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int

    def start(self, first_output: int) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation": {"ephemeral_5m_input_tokens": self.cache_creation_input_tokens,
                               "ephemeral_1h_input_tokens": 0},
            "output_tokens": first_output,
            "service_tier": "standard",
            "inference_geo": "global",
            "speed": "standard",
        }

    def delta(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "output_tokens": self.output_tokens,
            "output_tokens_details": {"thinking_tokens": 0},
        }

    def full(self) -> dict:
        u = self.start(self.output_tokens)
        u["output_tokens_details"] = {"thinking_tokens": 0}
        return u


def output_tokens(blocks: list[dict]) -> int:
    n = 0
    for b in blocks:
        if b["type"] == "text":
            n += estimate_tokens(b["text"])
        else:
            n += 30 + estimate_tokens(b["input"])
    return max(1, n)


def tool_input_json(value: dict) -> str:
    # The real stream's concatenated deltas use ", " and ": " separators.
    return json.dumps(value, ensure_ascii=False)


def _pad(rng: random.Random, obj: dict) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s[:-1] + " " * rng.randint(0, 16) + "}"


def _chunks(rng: random.Random, text: str, lo: int, hi: int) -> list[str]:
    out, i = [], 0
    while i < len(text):
        n = rng.randint(lo, hi)
        out.append(text[i:i + n])
        i += n
    return out


def text_pieces(rng: random.Random, text: str) -> list[str]:
    if len(text) <= 12:
        return [text]
    return _chunks(rng, text, 4, 24)


def json_pieces(rng: random.Random, raw: str) -> list[str]:
    """Framed as every tool input in the stored responses (50 calls): an
    empty first delta, the body in pieces, and a last delta that is the
    closing brace, joined by the closing quote when the last value is a
    string (`"}`, 10 calls) and on its own otherwise (`}`, 39 calls). A call
    with no arguments is one empty delta and nothing else (1 call)."""
    if raw == "{}":
        return [""]
    tail = '"}' if raw.endswith('"}') else "}"
    return [""] + _chunks(rng, raw[:-len(tail)], 7, 26) + [tail]


def message_object(model: str, msg_id: str, content: list, stop_reason: str | None, usage: dict) -> dict:
    # Key order as in the stored message_start events.
    return {
        "model": model,
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "container": None,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": usage,
        "input_transformations": [],
        "diagnostics": None,
    }


def wire_block(b: dict) -> dict:
    if b["type"] == "text":
        return {"type": "text", "text": b["text"]}
    return {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"], "caller": {"type": "direct"}}


def sse_events(model: str, msg_id: str, blocks: list[dict], stop_reason: str, usage: Usage,
               seed: str | None = None) -> list[tuple[str, str]]:
    """(event name, data) pairs. Chunk sizes, padding and the first output
    count are drawn from `seed`, which the engine derives from the reply's
    content and position, so the same script and requests give the same
    bytes apart from ids, and a replayed response is byte-identical."""
    rng = random.Random(seed if seed is not None else msg_id)
    events: list[tuple[str, str]] = []
    first_output = min(usage.output_tokens, rng.randint(1, 8))
    events.append(("message_start", _pad(rng, {"type": "message_start", "message": message_object(
        model, msg_id, [], None, usage.start(first_output))})))
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            start = {"type": "text", "text": ""}
        else:
            start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}, "caller": {"type": "direct"}}
        events.append(("content_block_start", _pad(rng, {"type": "content_block_start", "index": i, "content_block": start})))
        if i == 0:
            events.append(("ping", '{"type": "ping"}'))
        if b["type"] == "text":
            for piece in text_pieces(rng, b["text"]):
                events.append(("content_block_delta", _pad(rng, {"type": "content_block_delta", "index": i,
                                                                 "delta": {"type": "text_delta", "text": piece}})))
        else:
            for piece in json_pieces(rng, tool_input_json(b["input"])):
                events.append(("content_block_delta", _pad(rng, {"type": "content_block_delta", "index": i,
                                                                 "delta": {"type": "input_json_delta", "partial_json": piece}})))
        events.append(("content_block_stop", _pad(rng, {"type": "content_block_stop", "index": i})))
    events.append(("message_delta", _pad(rng, {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None, "stop_details": None, "container": None},
        "usage": usage.delta(),
    })))
    events.append(("message_stop", _pad(rng, {"type": "message_stop"})))
    return events


def encode_event(name: str, data: str) -> bytes:
    return f"event: {name}\ndata: {data}\n\n".encode()


def message_body(model: str, msg_id: str, blocks: list[dict], stop_reason: str, usage: Usage) -> dict:
    return message_object(model, msg_id, [wire_block(b) for b in blocks], stop_reason, usage.full())
