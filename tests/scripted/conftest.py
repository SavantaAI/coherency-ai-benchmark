"""Helpers for the scripted server tests: a server in this process, an SSE
reader that checks framing strictly, and a client that sends requests the way
Pi 1.6.0 does (thread create, then continue with only the new messages)."""

from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from cohbench.scripted import App, Episode, load_script

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_pairs(name: str) -> list[tuple[dict, str]]:
    d = FIXTURES / name
    reqs = sorted(d.glob("*.request"))
    if not reqs:
        raise FileNotFoundError(f"no fixtures in {d}")
    return [(json.loads(p.read_text()), p.with_suffix(".response").read_text()) for p in reqs]


_first = fixture_pairs("steering")[0][0]
PI_SYSTEM = _first["system"]
PI_TOOLS = _first["tools"]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Events as (name, data). Fails on anything but `event:` then `data:`
    lines ended by a blank line, or on data that is not one JSON object."""
    assert text.endswith("\n\n"), "stream must end with a blank line"
    out = []
    for block in text[:-2].split("\n\n"):
        lines = block.split("\n")
        assert len(lines) == 2, f"event block is not two lines: {block!r}"
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        name, data = lines[0][7:], json.loads(lines[1][6:])
        assert isinstance(data, dict) and data["type"] == name, block
        out.append((name, data))
    return out


def assemble(events: list[tuple[str, dict]]) -> dict:
    """The message a client builds from the events, with tool inputs parsed
    from their concatenated deltas."""
    msg = None
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for name, d in events:
        if name == "message_start":
            msg = dict(d["message"])
        elif name == "content_block_start":
            blocks[d["index"]] = dict(d["content_block"])
            partial[d["index"]] = ""
        elif name == "content_block_delta":
            delta = d["delta"]
            if delta["type"] == "text_delta":
                blocks[d["index"]]["text"] += delta["text"]
            else:
                partial[d["index"]] += delta["partial_json"]
        elif name == "content_block_stop":
            b = blocks[d["index"]]
            if b["type"] == "tool_use":
                # A call with no arguments streams one empty delta, as the
                # real stream did for orchestrator_capabilities.
                b["input"] = json.loads(partial[d["index"]]) if partial[d["index"]] else {}
        elif name == "message_delta":
            msg["stop_reason"] = d["delta"]["stop_reason"]
            msg["final_usage"] = d["usage"]
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


class Server:
    def __init__(self, app: App, api: tuple, admin: tuple):
        self.app, self.api, self.admin = app, api, admin

    @property
    def episode(self) -> Episode:
        return self.app.episode

    def raw(self, method: str, path: str, body: bytes | None, headers: dict | None = None, *, admin: bool = False):
        host, port = self.admin if admin else self.api
        c = http.client.HTTPConnection(host, port, timeout=30)
        h = {"x-api-key": "test-key", "content-type": "application/json"}
        h.update(headers or {})
        c.request(method, path, body=body, headers={k: v for k, v in h.items() if v is not None})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def post(self, body: dict, path: str = "/v1/messages?beta=true") -> tuple[int, dict, object]:
        status, headers, data = self.raw("POST", path, json.dumps(body).encode())
        if headers.get("Content-Type", "").startswith("text/event-stream"):
            return status, headers, parse_sse(data.decode())
        return status, headers, json.loads(data)

    def load(self, script: dict) -> None:
        self.app.episode.load(load_script(script))


@pytest.fixture
def server(tmp_path):
    app = App(Episode(), tmp_path / "log", read_timeout=2.0)
    api, admin = app.start(("127.0.0.1", 0), ("127.0.0.1", 0))
    yield Server(app, api, admin)
    app.stop()


class Convo:
    """One Pi session: thread create, then continue with the new messages.

    `history` is the full conversation as Pi would resend it."""

    def __init__(self, srv: Server, *, tools=None, system=None, model="claude-haiku-5-5", threads=True, stream=True):
        self.srv = srv
        self.tools = PI_TOOLS if tools is None else tools
        self.system = PI_SYSTEM if system is None else system
        self.model = model
        self.threads = threads
        self.stream = stream
        self.history: list[dict] = []
        self.last_id: str | None = None
        self.replies: list[dict] = []

    def body(self, new: list[dict], *, full: bool = False) -> dict:
        b = {"model": self.model, "max_tokens": 128000, "stream": self.stream, "thinking": {"type": "disabled"}}
        if self.threads and self.last_id and not full:
            b.update(messages=new, thread={"type": "continue", "previous_message_id": self.last_id})
        else:
            b.update(messages=self.history + new, system=self.system, tools=self.tools)
            if self.threads:
                b["thread"] = {"type": "create"}
        return b

    def send(self, new: list[dict], *, full: bool = False) -> dict:
        status, headers, data = self.srv.post(self.body(new, full=full))
        assert status == 200, data
        msg = assemble(data) if isinstance(data, list) else data
        self.history += new
        self.history.append({"role": "assistant", "content": [
            {k: v for k, v in b.items() if k in ("type", "text", "id", "name", "input")} for b in msg["content"]]})
        self.last_id = msg["id"]
        self.replies.append(msg)
        return msg

    def user(self, text: str, **kw) -> dict:
        return self.send([{"role": "user", "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]}], **kw)

    def results(self, outputs: list, *, then_user: str | None = None, **kw) -> dict:
        """Answer the last reply's tool calls. `outputs` holds a string (exit
        0) or (text, is_error) per call, in order."""
        calls = [b for b in self.replies[-1]["content"] if b["type"] == "tool_use"]
        assert len(calls) == len(outputs), (calls, outputs)
        content = []
        for call, out in zip(calls, outputs):
            text, err = (out, False) if isinstance(out, str) else out
            content.append({"type": "tool_result", "tool_use_id": call["id"], "content": text, "is_error": err})
        msgs = [{"role": "user", "content": content}]
        if then_user is not None:
            msgs.append({"role": "user", "content": [{"type": "text", "text": then_user}]})
        return self.send(msgs, **kw)


def text_of(msg: dict) -> str:
    return "".join(b["text"] for b in msg["content"] if b["type"] == "text")


def calls_of(msg: dict) -> list[dict]:
    return [b for b in msg["content"] if b["type"] == "tool_use"]
