"""Replay: feed the server the real requests of saved Pi runs, with scripts
that reproduce those runs, and compare every response with the real one.

The requests are the driver proof's stored request bodies (run
2026-11-21T210951-74da66, copied to fixtures/). Each is sent as Pi sent it,
except that ids the real API made (`previous_message_id`, `tool_use_id`) are
replaced by the ids this server made for the corresponding reply.

Each response is normalised, and the normalised forms must be equal byte for
byte: every event name in order, every event's JSON with its key order, the
`content_block_stop` and `message_stop` payloads included. Normalising
removes exactly these differences, each recorded with its category:

- `id`: message and tool call ids are new values. Both sides must have the
  real format, and the mapping from real ids to this server's ids must be
  one to one across the run.
- `usage`: token counts are estimates. Every usage key, in order, and the
  type of every value must be equal; only the numbers are replaced.
- `chunking`: how many deltas a block is split into. The deltas of a block
  are joined before comparing, so the joined text must be equal.
- `padding`: the run of spaces before each event's closing brace. Each line
  must otherwise be the compact JSON of its event, on both sides.
- `json_whitespace`: whitespace between JSON tokens inside a streamed tool
  input. The real stream carries the model's own spacing for nested values
  (`"target": {"providerInstanceId":"pi",...}` in the subagent run); the
  server writes ", " and ": " throughout. Tokens and key order must be equal.
- `headers`: the real API sends about 29 response headers; this server sends
  Date, Content-Type, Transfer-Encoding, Cache-Control and request-id. The
  framing headers' values must equal the real ones. Pi reads none of the
  missing headers except `retry-after`, `retry-after-ms` and
  `x-should-retry`, which the real API sent on none of these responses.

Anything else that differs fails the test.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path

import pytest

from cohbench.scripted import ids

from .conftest import FIXTURES, fixture_pairs

JUSTIFIED = {"id", "usage", "chunking", "padding", "json_whitespace", "headers"}
REAL_HEADERS = json.loads((FIXTURES / "real_stream_headers.json").read_text())

STEERING_SCRIPT = {"version": 1, "handlers": [
    {"on_steer": "abandon", "steps": [
        {"do": "bash", "command": "sleep 6; echo step1", "timeout": 30,
         "say": "I'll run them one at a time, starting with step 1."},
        {"do": "bash", "command": "sleep 6; echo step2", "timeout": 30},
        {"do": "bash", "command": "sleep 6; echo step3", "timeout": 30},
        {"do": "bash", "command": "sleep 6; echo step4", "timeout": 30},
        {"do": "bash", "command": "sleep 6; echo step5", "timeout": 30},
        {"do": "bash", "command": "sleep 6; echo step6", "timeout": 30},
    ], "final": "Ran step1 to step6."},
    {"steps": [{"do": "write", "path": "/work/project/stopped.txt", "content": "halted\n",
                "say": "Stopping after step 3. Creating the file now."}], "final": "STEERED"},
]}

MESSAGING_SCRIPT = {"version": 1, "handlers": [
    {"steps": [], "final": "ready"},
    {"steps": [], "final": "42"},
    {"steps": [
        {"do": "tool", "name": "tool_search", "input": {"query": "send_to_user", "limit": 5}},
        {"do": "tool", "name": "mcp__t3-code__send_to_user", "input": {"message": "Build finished at step 3."}},
    ], "final": "done"},
]}

SUBAGENT_TASK = ("In the project at /work/project, determine the number of lines in README.md (for example with "
                 "`wc -l README.md` or an equivalent tool). Reply with only the number, nothing else.")
SUBAGENT_SCRIPT = {"version": 1, "handlers": [{"steps": [
    {"do": "tool", "name": "mcp__t3-code__orchestrator_capabilities", "input": {},
     "say": "I'll delegate this to a subagent as requested. First I need the orchestrator capabilities."},
    {"do": "delegate", "child": "counter", "task": SUBAGENT_TASK,
     "say": "The Pi provider is available. I'll delegate with the inherited model.",
     "input": {"mode": "wait", "role": "research",
               "target": {"providerInstanceId": "pi", "model": "anthropic/claude-haiku-5-5"},
               "timeoutMs": 45000, "title": "Count README.md lines"},
     "capture": [{"var": "lines", "regex": "\"summary\":\"(\\d+)\"", "required": True}]},
], "final": "{{lines}}"}],
    "children": {"counter": {"handlers": [{"steps": [
        {"do": "bash", "command": "cd /work/project && wc -l README.md", "timeout": 30,
         "capture": [{"var": "counted", "regex": "^(\\d+) ", "required": True}]}], "final": "{{counted}}"}]}}}

HANDOFF_SCRIPT = {"version": 1, "handlers": [
    {"steps": [{"do": "write", "path": "/work/project/notes.txt", "content": "NOTE-42C80A",
                "say": "I'll create the file now."}], "final": "OK"},
    {"steps": [{"do": "bash", "command": "printf '%s-%s\\n' SPILL 9569; seq -f 'row %05g "
                                         "abcdefghijklmnopqrstuvwxyz0123456789' 1 3000"}], "final": "done"},
    {"steps": [{"do": "parallel", "calls": [
        {"do": "write", "path": "/work/project/scratch.txt", "content": "MARK-B38B00"},
        {"do": "write", "path": "/work/project/lib/scratch.txt", "content": "INNER-DCEE61"}]}], "final": "OK"},
    {"steps": [{"do": "bash", "command": "rm -f /work/project/scratch.txt /work/project/lib/scratch.txt"}],
     "final": "OK"},
    # The second container, after the handoff: a fresh conversation.
    {"steps": [], "final": "UNKNOWN"},
    {"steps": [{"do": "read", "path": "/work/project/notes.txt"}], "final": "NOTE-42C80A"},
]}

RUNS = [("steering", STEERING_SCRIPT), ("messaging", MESSAGING_SCRIPT), ("subagent", SUBAGENT_SCRIPT),
        ("handoff", HANDOFF_SCRIPT)]


def raw_events(text: str) -> list[tuple[str, str]]:
    assert text.endswith("\n\n")
    out = []
    for block in text[:-2].split("\n\n"):
        lines = block.split("\n")
        assert len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        out.append((lines[0][7:], lines[1][6:]))
    return out


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Comparison:
    def __init__(self, run: str):
        self.run = run
        self.diffs: list[dict] = []
        self.id_map: dict[str, str] = {}

    def diff(self, where, path, real, mine, category):
        self.diffs.append({"where": where, "path": path, "real": real, "mine": mine, "category": category})

    def map_id(self, real: str, mine: str, where: str, check) -> None:
        ok = check(real) and check(mine)
        if real in self.id_map and self.id_map[real] != mine:
            ok = False
        if mine in self.id_map.values() and self.id_map.get(real) != mine:
            ok = False
        self.id_map[real] = mine
        self.diff(where, "id", real, mine, "id" if ok else "UNJUSTIFIED")

    def normalise(self, text: str, where: str, side: str) -> tuple[list[str], dict[int, int]]:
        """The events with ids, usage numbers, chunking and padding taken
        out, each as compact JSON; and the delta count per block."""
        out: list[list] = []
        counts: dict[int, int] = {}
        for name, data in raw_events(text):
            obj = json.loads(data)
            if name == "ping":
                out.append([name, data])  # compared as sent: '{"type": "ping"}'
                continue
            c = compact(obj)
            if not re.fullmatch(re.escape(c[:-1]) + " *\\}", data):
                self.diff(where, f"{side} {name} line", data[:120], None, "UNJUSTIFIED")
            if name == "message_start":
                obj["message"]["id"] = "<id>"
                obj["message"]["usage"] = _blank_numbers(obj["message"]["usage"])
            elif name == "content_block_start" and obj["content_block"].get("type") == "tool_use":
                obj["content_block"]["id"] = "<id>"
            elif name == "message_delta":
                obj["usage"] = _blank_numbers(obj["usage"])
            elif name == "content_block_delta":
                i = obj["index"]
                counts[i] = counts.get(i, 0) + 1
                if out and out[-1][0] == name and out[-1][1]["index"] == i:
                    prev = out[-1][1]["delta"]
                    key = "text" if prev["type"] == "text_delta" else "partial_json"
                    if obj["delta"]["type"] != prev["type"]:
                        self.diff(where, f"{side} delta types within block {i}", prev["type"], obj["delta"]["type"],
                                  "UNJUSTIFIED")
                    prev[key] += obj["delta"][key]
                    continue
            out.append([name, obj])
        return [compact([n, o]) for n, o in out], counts

    def compare(self, real_text: str, mine_text: str, mine_headers: dict, where: str) -> None:
        real_ev, mine_ev = json_events(real_text), json_events(mine_text)
        self.map_id(real_ev[0]["message"]["id"], mine_ev[0]["message"]["id"], f"{where} message", ids.is_message_id)
        rt = [e["content_block"]["id"] for e in real_ev if e["type"] == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
        mt = [e["content_block"]["id"] for e in mine_ev if e["type"] == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
        if len(rt) != len(mt):
            self.diff(where, "tool calls", rt, mt, "UNJUSTIFIED")
        for r, m in zip(rt, mt):
            self.map_id(r, m, f"{where} tool call", ids.is_tool_use_id)
        ru = _usages(real_ev)
        mu = _usages(mine_ev)
        if [_shape(u) for u in ru] != [_shape(u) for u in mu]:
            self.diff(where, "usage shape", ru, mu, "UNJUSTIFIED")
        elif ru != mu:
            self.diff(where, "usage numbers", ru, mu, "usage")
        rn, rc = self.normalise(real_text, where, "real")
        mn, mc = self.normalise(mine_text, where, "mine")
        rn, mn = self._tool_json(rn, mn, where)
        if rn != mn:
            self.diff(where, "events", None, "\n".join(difflib.unified_diff(rn, mn, "real", "mine", lineterm="")),
                      "UNJUSTIFIED")
        if rc != mc:
            self.diff(where, "deltas per block", rc, mc, "chunking")
        rs, ms = delta_shapes(real_text), delta_shapes(mine_text)
        if rs != ms:
            self.diff(where, "delta framing", rs, ms, "UNJUSTIFIED")
        pad = [[len(d) - len(d[:-1].rstrip(" ")) - 1 for n, d in raw_events(t) if n != "ping"]
               for t in (real_text, mine_text)]
        if pad[0] != pad[1]:
            self.diff(where, "spaces before each closing brace", pad[0], pad[1], "padding")
        self.headers(mine_headers, where)

    def _tool_json(self, rn: list[str], mn: list[str], where: str) -> tuple[list[str], list[str]]:
        """Joined tool input JSON: equal as text, or equal token for token
        (then the difference is whitespace and is recorded as such)."""
        def split(lines):
            out = []
            for line in lines:
                name, obj = json.loads(line)
                if name == "content_block_delta" and obj["delta"]["type"] == "input_json_delta":
                    out.append((line, obj))
                else:
                    out.append((line, None))
            return out
        rs, ms = split(rn), split(mn)
        if len(rs) != len(ms):
            return rn, mn
        r_out, m_out = [], []
        for (rl, ro), (ml, mo) in zip(rs, ms):
            if ro is not None and mo is not None and ro["delta"]["partial_json"] != mo["delta"]["partial_json"]:
                rj, mj = ro["delta"]["partial_json"], mo["delta"]["partial_json"]
                try:
                    same_tokens = compact(json.loads(rj)) == compact(json.loads(mj))
                except json.JSONDecodeError:
                    same_tokens = False
                if same_tokens:
                    self.diff(where, f"block {ro['index']} tool input whitespace", rj, mj, "json_whitespace")
                    ro["delta"]["partial_json"] = mo["delta"]["partial_json"] = compact(json.loads(rj))
                    rl, ml = compact(["content_block_delta", ro]), compact(["content_block_delta", mo])
            r_out.append(rl)
            m_out.append(ml)
        return r_out, m_out

    def headers(self, mine: dict, where: str) -> None:
        mine_l = {k.lower(): v for k, v in mine.items()}
        real_names = set(REAL_HEADERS["header_counts"])
        extra = set(mine_l) - real_names
        if extra:
            self.diff(where, "headers only this server sends", None, sorted(extra), "UNJUSTIFIED")
        for k, values in REAL_HEADERS["values"].items():
            if mine_l.get(k) not in values:
                self.diff(where, f"header {k}", values, mine_l.get(k), "UNJUSTIFIED")
        if not ids.is_request_id(mine_l.get("request-id", "")):
            self.diff(where, "request-id", None, mine_l.get("request-id"), "UNJUSTIFIED")
        missing = sorted(real_names - set(mine_l))
        if {"retry-after", "retry-after-ms", "x-should-retry"} & set(missing):
            self.diff(where, "a header Pi reads is missing", None, missing, "UNJUSTIFIED")
        self.diff(where, "headers the real API also sends", missing, None, "headers")


def delta_shapes(text: str) -> dict:
    """How each block's deltas are framed, which chunking must not change:
    a tool input's first and last delta (the real ones are "" and then "}"
    or '"}'), or a single empty delta for no arguments; text deltas are
    never empty."""
    pieces: dict[int, list] = {}
    keys: dict[int, set] = {}
    for e in json_events(text):
        if e["type"] == "content_block_delta":
            d = e["delta"]
            pieces.setdefault(e["index"], []).append((d["type"], d.get("text", d.get("partial_json"))))
            # Every delta's keys, in order, on the event and inside it.
            keys.setdefault(e["index"], set()).add((tuple(e), tuple(d)))
    out = {}
    for i, ps in sorted(pieces.items()):
        kind = ps[0][0]
        if kind == "input_json_delta":
            vals = [p for _, p in ps]
            out[i] = {"kind": kind, "first": vals[0], "last": vals[-1] if len(vals) > 1 else None,
                      "single_empty": vals == [""]}
        else:
            out[i] = {"kind": kind, "empty_pieces": sum(1 for _, p in ps if p == "")}
        out[i]["keys"] = sorted(keys[i])
    return out


def json_events(text: str) -> list[dict]:
    return [json.loads(d) for _, d in raw_events(text)]


def _blank_numbers(u):
    if isinstance(u, dict):
        return {k: _blank_numbers(v) for k, v in u.items()}
    return "<n>" if isinstance(u, int) and not isinstance(u, bool) else u


def _shape(u):
    if isinstance(u, dict):
        return [[k, _shape(v)] for k, v in u.items()]
    return type(u).__name__


def _usages(events):
    out = []
    for e in events:
        if e["type"] == "message_start":
            out.append(e["message"]["usage"])
        elif e["type"] == "message_delta":
            out.append(e["usage"])
    return out


def replay_responses(server, name: str, script: dict) -> list[tuple[str, str, dict]]:
    """Send a saved run's requests with the real ids mapped to this server's;
    returns (real response, this server's response, its headers) per request."""
    server.load(script)
    id_map: dict[str, str] = {}
    out = []
    for req, real_resp in fixture_pairs(name):
        body = copy.deepcopy(req)
        th = body.get("thread")
        if th and th.get("type") == "continue":
            th["previous_message_id"] = id_map[th["previous_message_id"]]
        for m in body["messages"]:
            for b in m["content"] if isinstance(m["content"], list) else []:
                if b.get("type") == "tool_result":
                    b["tool_use_id"] = id_map[b["tool_use_id"]]
        status, headers, data = server.raw("POST", "/v1/messages?beta=true", json.dumps(body).encode())
        assert status == 200, data
        mine = data.decode()
        real_ev, mine_ev = json_events(real_resp), json_events(mine)
        id_map[real_ev[0]["message"]["id"]] = mine_ev[0]["message"]["id"]
        rt = [e["content_block"]["id"] for e in real_ev if e["type"] == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
        mt = [e["content_block"]["id"] for e in mine_ev if e["type"] == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
        id_map.update(zip(rt, mt))
        out.append((real_resp, mine, headers))
    return out


def replay(server, name: str, script: dict) -> Comparison:
    cmp = Comparison(name)
    for n, (real_resp, mine, headers) in enumerate(replay_responses(server, name, script)):
        cmp.compare(real_resp, mine, headers, f"{name} response {n + 1}")
    return cmp


def report(diffs: list[dict]) -> str:
    lines = []
    for d in diffs:
        lines.append(f"[{d['category']}] {d['where']}: {d['path']}: real={json.dumps(d['real'])[:100]} "
                     f"mine={json.dumps(d['mine'])[:300]}")
    return "\n".join(lines)


@pytest.mark.parametrize("name,script", RUNS, ids=[r[0] for r in RUNS])
def test_replay_matches_the_real_responses(server, name, script, capsys):
    cmp = replay(server, name, script)
    counts: dict[str, int] = {}
    for d in cmp.diffs:
        counts[d["category"]] = counts.get(d["category"], 0) + 1
    with capsys.disabled():
        print(f"\n--- replay {name}: {len(fixture_pairs(name))} responses; differences by category {counts}")
        for d in cmp.diffs:
            if d["category"] in ("json_whitespace", "UNJUSTIFIED"):
                print(report([d]))
    unjustified = [d for d in cmp.diffs if d["category"] not in JUSTIFIED]
    assert not unjustified, report(unjustified)
    assert server.episode.state()["errors"] == []


def test_the_replay_set_covers_what_the_reviewers_asked_for(server):
    """The subagent exchange (a zero-argument call, a delegation, the child)
    and a parallel-call response are in the set."""
    subagent = json_events(fixture_pairs("subagent")[0][1])
    empty = [e for e in subagent if e["type"] == "content_block_delta" and e["index"] == 1]
    assert [e["delta"]["partial_json"] for e in empty] == [""], "the real empty-input form"
    handoff = [json_events(r) for _, r in fixture_pairs("handoff")]
    assert any(sum(e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use" for e in ev) == 2
               for ev in handoff)
    replay(server, "subagent", SUBAGENT_SCRIPT)
    st = server.episode.state()
    assert st["conversations"]["c2"]["kind"] == "child" and st["delegations"][0]["status"] == "claimed"


@pytest.mark.parametrize("mutation", ["stop index", "message_stop", "delta key order", "text", "header",
                                      "leading empty input delta", "extra field in an input delta"])
def test_the_comparison_catches_a_real_difference(server, mutation):
    """Changes the reviewers made by hand to a saved response must fail."""
    if mutation == "leading empty input delta":
        server.load(SUBAGENT_SCRIPT)
        req, real = fixture_pairs("subagent")[0]
    elif mutation == "extra field in an input delta":
        real, mine, headers = replay_responses(server, "messaging", MESSAGING_SCRIPT)[2]  # the tool_search call
    else:
        server.load(MESSAGING_SCRIPT)
        req, real = fixture_pairs("messaging")[0]
    if mutation != "extra field in an input delta":
        status, headers, data = server.raw("POST", "/v1/messages?beta=true", json.dumps(req).encode())
        mine = data.decode()
    if mutation == "stop index":
        real = real.replace('"type":"content_block_stop","index":0', '"type":"content_block_stop","index":99')
    elif mutation == "message_stop":
        real = real.replace('{"type":"message_stop"', '{"type":"message_stop","extra":1')
    elif mutation == "delta key order":
        real = real.replace('"delta":{"stop_reason":"end_turn","stop_sequence":null',
                            '"delta":{"stop_sequence":null,"stop_reason":"end_turn"')
    elif mutation == "text":
        real = real.replace('"text":"ready"', '"text":"steady"')
    elif mutation == "header":
        headers = {**headers, "X-Extra": "1"}
    elif mutation == "extra field in an input delta":
        events = real.split("\n\n")
        deltas = [i for i, b in enumerate(events) if '"input_json_delta"' in b]
        second = deltas[1]
        events[second] = events[second].replace('"type":"input_json_delta",', '"type":"input_json_delta","extra":1,', 1)
        assert '"extra":1' in events[second]
        real = "\n\n".join(events)
    else:
        # The saved zero-argument call is one empty delta; drop it.
        blocks = real.split("\n\n")
        blocks = [b for b in blocks if '"index":1,"delta":{"type":"input_json_delta","partial_json":""}' not in b]
        assert len(blocks) == len(real.split("\n\n")) - 1
        real = "\n\n".join(blocks)
    assert real != mine
    cmp = Comparison("mutated")
    cmp.compare(real, mine, headers, "mutated")
    assert [d for d in cmp.diffs if d["category"] == "UNJUSTIFIED"], mutation
