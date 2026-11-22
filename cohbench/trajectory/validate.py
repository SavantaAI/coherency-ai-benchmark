"""Validators for the normalised trajectory and the scorer view: schema,
order and references.

A trajectory with an `unknown` event is reported as not ok unless
`allow_unknown=True`, so an adapter that met an item it could not map cannot
pass silently. Every check is written so that malformed input (a list where a
number belongs, a missing field) is reported, never raised.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import format as F
from .clock import to_ms
from .io import Trajectory


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    unknown: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    tool_classes: dict[str, int] = field(default_factory=dict)
    allow_unknown: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and (self.allow_unknown or self.unknown == 0)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors[:200], "error_count": len(self.errors), "unknown": self.unknown, "counts": self.counts, "tool_classes": self.tool_classes}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _type_ok(value: Any, spec: Any, where: str, errors: list[str]) -> None:
    if isinstance(spec, tuple) and spec[0] == "enum":
        _, values, optional = spec
        if value is None and optional:
            return
        if not isinstance(value, str) or value not in values:
            errors.append(f"{where}: {value!r:.60} is not one of {list(values)[:12]}")
        return
    if spec == F.T.INT:
        ok = _is_int(value)
    elif spec == F.T.OPT_INT:
        ok = value is None or _is_int(value)
    elif spec == F.T.STR:
        ok = isinstance(value, str)
    elif spec == F.T.OPT_STR:
        ok = value is None or isinstance(value, str)
    elif spec == F.T.BOOL:
        ok = isinstance(value, bool)
    elif spec == F.T.OPT_BOOL:
        ok = value is None or isinstance(value, bool)
    elif spec == "intervals":
        ok = isinstance(value, list) and all(
            isinstance(i, dict) and set(i) == {"us", "us_end", "reason"} and _is_int(i["us"]) and _is_int(i["us_end"]) and i["us_end"] > i["us"] and i["reason"] in ("handoff", "probe", "export")
            for i in value)
    elif spec == "seqs":
        ok = isinstance(value, list) and all(_is_int(x) for x in value) and value == sorted(set(value))
    elif spec == "causes":
        # Everything that was due at the hand-back, sorted, each once.
        ok = isinstance(value, list) and bool(value) and all(isinstance(x, str) and x in F.WAIT_CAUSES for x in value) and value == sorted(set(value))
    elif spec == "seqs?":
        ok = value is None or (isinstance(value, list) and all(_is_int(x) for x in value) and len(set(value)) == len(value))
    elif spec in ("input", "output"):
        ok = isinstance(value, dict)  # keys checked against the tool's schema below
    elif spec in (F.PROMPTS, "prompts"):
        ok = (value is None and spec == F.PROMPTS) or (isinstance(value, list) and all(
            isinstance(q, dict) and set(q) == {"header", "text", "options", "multi_select"}
            and (q["header"] is None or isinstance(q["header"], str)) and isinstance(q["text"], str) and isinstance(q["multi_select"], bool)
            and isinstance(q["options"], list)
            and all(isinstance(o, dict) and set(o) == {"label", "description"} and isinstance(o["label"], str) and (o["description"] is None or isinstance(o["description"], str)) for o in q["options"])
            for q in value))
    elif spec == F.SECRET:
        ok = value is None or (
            isinstance(value, dict) and set(value) == {"label", "reason", "fields"}
            and all(value[k] is None or isinstance(value[k], str) for k in ("label", "reason")) and isinstance(value["fields"], list)
            and all(isinstance(f, dict) and set(f) == {"label", "placeholder", "secret"} and all(f[k] is None or isinstance(f[k], str) for k in ("label", "placeholder")) and isinstance(f["secret"], bool) for f in value["fields"]))
    elif spec == "secret_fields":
        ok = isinstance(value, list) and all(
            isinstance(f, dict) and set(f) == {"name", "label", "secret"} and all(f[k] is None or isinstance(f[k], str) for k in ("name", "label")) and isinstance(f["secret"], bool)
            for f in value
        )
    else:
        raise AssertionError(f"unknown spec {spec!r}")
    if not ok:
        errors.append(f"{where}: {value!r:.80} does not have type {spec}")


def _check_keys(obj: dict[str, Any], spec: dict[str, Any], where: str, errors: list[str]) -> bool:
    missing = set(spec) - set(obj)
    extra = set(obj) - set(spec)
    if missing:
        errors.append(f"{where}: missing {sorted(missing)}")
    if extra:
        errors.append(f"{where}: undeclared field(s) {sorted(map(str, extra))}")
    before = len(errors)
    for k, s in spec.items():
        if k in obj:
            _type_ok(obj[k], s, f"{where}.{k}", errors)
    return not missing and len(errors) == before


def _header(h: Any, spec: dict[str, Any], fmt: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(h, dict):
        errors.append("header: not an object")
        return {}
    _check_keys(h, spec, "header", errors)
    if h.get("type") != "header" or h.get("format") != fmt or h.get("version") != F.VERSION:
        errors.append("header: wrong type, format or version")
    return h


def _runner_target(ev: dict[str, Any], events: list[Any], idx: int, where: str, errors: list[str], user_channel: callable) -> None:
    if ev["type"] == "wait":
        if _is_int(ev.get("t_end")) and ev["t_end"] < ev["t"]:
            errors.append(f"{where}: a wait ends before it starts")
        if _is_int(ev.get("charged_ms")) and ev["charged_ms"] < 0:
            errors.append(f"{where}: a negative charge")
        if _is_int(ev.get("charged_ms")) and ev.get("t_end") is None:
            errors.append(f"{where}: a charged wait has an end")
        rt = ev.get("replies_to")
        if isinstance(ev.get("causes"), list) and "verdict_reply" in ev["causes"] and not (rt if isinstance(rt, list) else ev.get("target") is not None):
            errors.append(f"{where}: a wait for the policy's reply names the outputs it replies to")
        if isinstance(rt, list):
            # Outputs delivered up to the wait's end (M2 of round 11).
            limit = ev.get("t_end") if _is_int(ev.get("t_end")) else None
            for x in rt:
                ok = _is_int(x) and 0 <= x < len(events) and isinstance(events[x], dict) and user_channel(events[x])
                if not ok or (limit is not None and _is_int(events[x].get("t")) and events[x]["t"] > limit):
                    errors.append(f"{where}: replies_to {x!r:.20} is not user-channel output delivered by the wait's end")
    if "target" not in ev:
        return
    t = ev["target"]
    if t is None:
        if ev["type"] in ("verdict", "correction_delivered", "unjudged", "notice_delivered"):
            errors.append(f"{where}: needs a target")
        return
    if not _is_int(t) or not 0 <= t < len(events) or not isinstance(events[t], dict):
        errors.append(f"{where}: target {t!r} is not an event of the file")
        return
    tgt = events[t]
    et = ev["type"]
    if et in ("verdict", "wait", "unjudged", "handback") and not user_channel(tgt):
        errors.append(f"{where}: target {t} is not user-channel output")
    if et == "correction_delivered" and not (tgt.get("type") in ("user_message", "answer") and tgt.get("kind") == "correction"):
        errors.append(f"{where}: target {t} is not a correction")
    if et == "notice_delivered" and not (tgt.get("type") == "user_message" and tgt.get("kind") == "reset_notice"):
        errors.append(f"{where}: target {t} is not a reset notice")
    if et == "controlled_reset" and not (tgt.get("type") == "user_message" and tgt.get("kind") == "reset"):
        errors.append(f"{where}: target {t} is not a reset message")
    if et == "reopen" and not (tgt.get("type") in ("user_message", "answer") and tgt.get("kind") == "reopen"):
        errors.append(f"{where}: target {t} is not a reopen message")
    if et in ("verdict", "wait", "unjudged", "handback") and t > idx:
        errors.append(f"{where}: a {et} comes after the output it is about")
    key = "us" if "us" in ev else "t"
    if et in ("correction_delivered", "notice_delivered") and tgt.get(key) != ev.get(key):
        errors.append(f"{where}: the {'correction' if et == 'correction_delivered' else 'notice'} is dated {tgt.get(key)}, its delivery {ev.get(key)}")


def validate(traj: Trajectory, allow_unknown: bool = False) -> Report:
    rep = Report(allow_unknown=allow_unknown)
    errors = rep.errors
    h = _header(traj.header, F.HEADER_FIELDS, F.FORMAT, errors)
    events = traj.events
    counts: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    post = h.get("post_run") if isinstance(h.get("post_run"), list) else []
    if any(not _is_int(x) or not 0 <= x < len(events) for x in post):
        errors.append("header: post_run names events that are not in the file")
    post_set = {x for x in post if _is_int(x)}
    inpost = lambda v: _is_int(v) and v in post_set  # noqa: E731
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "user_message" and e.get("kind") == "post_run" and e.get("agent") == 0 and not inpost(e.get("seq")):
            errors.append(f"event {e.get('seq')}: a post_run message outside the header's post_run events")
        if e.get("type") in F.RUNNER_TYPES and (inpost(e.get("seq")) or inpost(e.get("target"))):
            errors.append(f"event {e.get('seq')} ({e.get('type')}): a runner event in, or naming, the post-run exchange")

    segment = 0
    last_us: int | None = None
    agents_declared = {0}
    agents_ended: set[int] = set()
    open_turn: dict[int, int] = {}
    turns_seen: list[int] = []
    turn_ended: dict[int, str] = {}
    turn_agent: dict[int, int] = {}
    turn_has_question: set[int] = set()
    calls: dict[int, dict[str, Any]] = {}
    questions: dict[int, dict[str, Any]] = {}
    answered: set[int] = set()
    subagent_calls: dict[int, int] = {}
    subagent_started: set[int] = set()
    subagent_started_by: dict[int, int | None] = {}
    saw_opening = False
    agent0_user_messages = 0
    first_user_in_segment: dict[int, int] = {}
    segment_cause: dict[int, str] = {}

    for idx, ev in enumerate(events):
        where = f"event {idx}"
        if not isinstance(ev, dict):
            errors.append(f"{where}: not an object")
            continue
        etype = ev.get("type")
        if not isinstance(etype, str) or etype not in F.EVENT_FIELDS:
            errors.append(f"{where}: unknown event type {etype!r:.40}")
            continue
        where = f"event {idx} ({etype})"
        counts[etype] += 1
        if not _check_keys(ev, {**F.COMMON_FIELDS, **F.EVENT_FIELDS[etype]}, where, errors):
            continue  # the reference checks below need well-typed fields
        if ev["seq"] != idx:
            errors.append(f"{where}: seq {ev['seq']}, expected {idx}")
        # Order is decided on wall microseconds; `t` is their milliseconds.
        u = ev["us"]
        if ev["t"] != to_ms(u):
            errors.append(f"{where}: t {ev['t']} is not {u} us in milliseconds")
        if last_us is not None and u < last_us:
            errors.append(f"{where}: time {u} us before the previous event's {last_us}")
        last_us = u if last_us is None else max(last_us, u)
        if "us_end" in ev and (ev["us_end"] is None) != (ev.get("t_end") is None):
            errors.append(f"{where}: t_end and us_end are null together")
        elif ev.get("us_end") is not None and ev.get("t_end") != to_ms(ev["us_end"]):
            errors.append(f"{where}: t_end {ev.get('t_end')} is not us_end {ev['us_end']} in milliseconds")
        agent = ev["agent"]
        if etype == "segment_start":
            segment += 1
            segment_cause[segment] = ev["cause"]
            if agent != 0:
                errors.append(f"{where}: segments belong to agent 0")
            if (ev["cause"] == "start") != (segment == 1):
                errors.append(f"{where}: segment {segment} cause {ev['cause']!r}; only segment 1 is the start")
            if 0 in open_turn:
                errors.append(f"{where}: a segment starts while agent 0 has an open turn")
        if idx == 0 and etype != "segment_start":
            errors.append("event 0: the first event must be segment_start")
        if ev["segment"] != segment:
            errors.append(f"{where}: segment {ev['segment']}, expected {segment}")
        if etype in F.RUNNER_TYPES:
            if agent != 0:
                errors.append(f"{where}: runner events are on agent 0")
            _runner_target(ev, events, idx, where, errors, lambda x: x.get("user_channel") is True)
            continue
        if agent not in agents_declared:
            errors.append(f"{where}: agent {agent} was not started by a subagent_start")
        if agent in agents_ended:
            errors.append(f"{where}: agent {agent} has events after its subagent_end")

        # An unmapped item is counted (and fails unless allowed), never
        # checked for turn membership: it sits at its run's nearest event.
        turn = ev.get("turn") if "turn" in F.EVENT_FIELDS[etype] and etype != "unknown" else None
        if etype == "turn_start":
            if agent in open_turn:
                errors.append(f"{where}: agent {agent} already has open turn {open_turn[agent]}")
            if turn in turn_agent:
                errors.append(f"{where}: turn {turn} started twice")
            if turn != len(turns_seen) + 1:
                errors.append(f"{where}: turn {turn}, expected {len(turns_seen) + 1}")
            open_turn[agent] = turn
            turn_agent[turn] = agent
            turns_seen.append(turn)
        elif etype == "turn_end":
            if open_turn.get(agent) != turn:
                errors.append(f"{where}: turn {turn} is not agent {agent}'s open turn")
            else:
                del open_turn[agent]
                turn_ended[turn] = ev["end"]
                if ev["end"] == "waiting" and turn not in turn_has_question:
                    errors.append(f"{where}: turn ends waiting with no question in it")
        elif turn is not None:
            if open_turn.get(agent) != turn:
                errors.append(f"{where}: turn {turn} is not agent {agent}'s open turn")
        elif etype in ("user_message", "assistant_message", "tool_call", "question", "subagent_start"):
            errors.append(f"{where}: needs a turn")
        if etype == "answer" and agent in open_turn:
            errors.append(f"{where}: an answer arrives while agent {agent} has an open turn")

        if etype == "user_message":
            kind = ev["kind"]
            if agent == 0 and kind != "from_agent":
                agent0_user_messages += 1
                if kind == "opening_prompt":
                    if saw_opening or agent0_user_messages != 1:
                        errors.append(f"{where}: opening_prompt must be agent 0's first user message, once")
                    saw_opening = True
                elif agent0_user_messages == 1:
                    errors.append(f"{where}: agent 0's first user message must be the opening_prompt")
                first_user_in_segment.setdefault(segment, idx)
                if kind == "reset" and (first_user_in_segment[segment] != idx or segment_cause.get(segment) != "reset"):
                    errors.append(f"{where}: a reset message opens a segment the runner declared a controlled reset")
            elif agent != 0 and kind != "from_agent":
                errors.append(f"{where}: a subagent's messages come from its parent (kind from_agent)")
        elif etype == "assistant_message":
            uc = ev["user_channel"]
            if uc and not (ev["role"] == "final" and agent == 0):
                errors.append(f"{where}: only agent 0's final messages are user channel")
            if ev["role"] == "final" and agent == 0 and not uc:
                errors.append(f"{where}: agent 0's final message is user channel")
            if (ev["user_text"] is not None) != uc or (uc and ev["user_text"] != ev["text"]):
                errors.append(f"{where}: user_text must equal text on a user-channel message and be null otherwise")
        elif etype == "tool_call":
            call, tool = ev["call"], ev["tool"]
            classes[ev["class"]] += 1
            if call in calls or call != len(calls) + 1:
                errors.append(f"{where}: call {call}, expected {len(calls) + 1}")
            calls[call] = ev
            if ev["class"] != F.TOOLS[tool]:
                errors.append(f"{where}: tool {tool} is class {F.TOOLS[tool]}")
            _check_keys(ev["input"], F.input_fields(tool), f"{where}.input", errors)
            out = ev["output"]
            if _check_keys(out, F.OUTPUT_FIELDS, f"{where}.output", errors):
                if out["available"] != (out["text"] is not None):
                    errors.append(f"{where}: output available exactly when it has text")
            if isinstance(ev["us_end"], int) and ev["us_end"] < u:
                errors.append(f"{where}: t_end before t")
            uc = ev["user_channel"]
            if uc != (tool == "send_to_user" and ev["status"] == "ok" and ev["user_text"] is not None):
                errors.append(f"{where}: user_channel is true exactly for successful send_to_user calls that carry user_text")
            if not uc and ev["user_text"] is not None:
                errors.append(f"{where}: user_text on a call outside the user channel")
            if ev["question"] is not None and tool not in ("ask_user", "request_secret"):
                errors.append(f"{where}: only question tools link a question")
            if ev["subagent"] is not None:
                if ev["class"] != "subagent":
                    errors.append(f"{where}: only subagent tools link a subagent")
                subagent_calls[call] = ev["subagent"]
        elif etype == "question":
            q = ev["question"]
            if q in questions or q != len(questions) + 1:
                errors.append(f"{where}: question {q}, expected {len(questions) + 1}")
            questions[q] = ev
            turn_has_question.add(turn)
            if ev["user_channel"] is not True:
                errors.append(f"{where}: questions are user-channel content")
            if (ev["kind"] == "question") != bool(ev["prompts"]) or (ev["kind"] == "secret") != (ev["secret"] is not None):
                errors.append(f"{where}: a question has prompts, a secret request has secret, and not the other")
            c = ev["call"]
            if c is not None:
                if c not in calls:
                    errors.append(f"{where}: call {c} is not an earlier tool_call")
                elif calls[c]["question"] != q:
                    errors.append(f"{where}: call {c} does not link question {q}")
        elif etype == "answer":
            q = ev["question"]
            if q not in questions:
                errors.append(f"{where}: answers question {q}, which was not asked earlier")
            elif q in answered:
                errors.append(f"{where}: question {q} answered twice")
            else:
                answered.add(q)
                kind, st = questions[q]["kind"], ev["status"]
                if kind == "secret" and st == "answered":
                    errors.append(f"{where}: a secret is saved, declined or cancelled, not answered")
                if kind == "question" and st in ("saved", "declined"):
                    errors.append(f"{where}: status {st} is for secrets")
                text_ok = (kind == "question" and st == "answered") or (kind == "secret" and st in ("declined", "cancelled"))
                if ev["text"] is not None and not text_ok:
                    errors.append(f"{where}: only an answered question, or a refused secret, has answer text; a secret's value never")
                if ev["missing"] and not (kind == "question" and st == "answered" and ev["text"] is None):
                    errors.append(f"{where}: missing marks an answered question without text, only")
                if questions[q]["agent"] != agent:
                    errors.append(f"{where}: answered in a different agent's stream")
        elif etype == "subagent_start":
            sub = ev["subagent"]
            if sub != len(agents_declared):
                errors.append(f"{where}: subagent {sub}, expected {len(agents_declared)}")
            agents_declared.add(sub)
            subagent_started.add(sub)
            subagent_started_by[sub] = ev["call"]
            c = ev["call"]
            if c is not None:
                if c not in calls:
                    errors.append(f"{where}: call {c} is not an earlier tool_call")
                elif subagent_calls.get(c) != sub:
                    errors.append(f"{where}: call {c} does not link subagent {sub}")
        elif etype == "subagent_end":
            sub = ev["subagent"]
            if sub not in agents_declared or sub == 0:
                errors.append(f"{where}: subagent {sub} was never started")
            elif sub in agents_ended:
                errors.append(f"{where}: subagent {sub} ended twice")
            agents_ended.add(sub)
            if sub in open_turn:
                errors.append(f"{where}: subagent {sub} ends with turn {open_turn[sub]} open")
        elif etype == "unknown":
            rep.unknown += 1

    for agent, turn in open_turn.items():
        errors.append(f"end: agent {agent} turn {turn} has no turn_end")
    # Links from calls must be answered by the event they name.
    for c, ev in calls.items():
        q = ev["question"]
        if q is not None and (q not in questions or questions[q]["call"] != c):
            errors.append(f"call {c}: links question {q}, which does not link it back")
        s = ev["subagent"]
        if s is not None and subagent_started_by.get(s) != c:
            errors.append(f"call {c}: links subagent {s}, whose subagent_start does not name the call")
    # The final message: the last assistant message of a finished turn with no
    # tool call, question or subagent start after it in that turn.
    by_turn: dict[int, list[dict[str, Any]]] = {}
    for ev in events:
        if isinstance(ev, dict) and _is_int(ev.get("turn")) and ev.get("type") in ("assistant_message", "tool_call", "question", "subagent_start"):
            by_turn.setdefault(ev["turn"], []).append(ev)
    for turn, evs in by_turn.items():
        want = evs[-1] if turn_ended.get(turn) == "finished" and evs[-1].get("type") == "assistant_message" else None
        for ev in evs:
            if ev.get("type") == "assistant_message" and (ev.get("role") == "final") != (ev is want):
                errors.append(f"event {ev.get('seq')} (assistant_message): role {ev.get('role')!r}; final is exactly the last message of a finished turn with nothing after it")
    if not saw_opening:
        errors.append("end: no opening_prompt")

    intervals = [i for i in h.get("excluded", []) if isinstance(i, dict) and _is_int(i.get("us")) and _is_int(i.get("us_end"))] if isinstance(h.get("excluded"), list) else []
    for ev in events:
        for key in ("us", "us_end"):
            v = ev.get(key) if isinstance(ev, dict) else None
            if _is_int(v) and any(i["us"] < v < i["us_end"] for i in intervals):
                errors.append(f"event {ev.get('seq')} ({ev.get('type')}): {key} {v} falls inside an excluded interval")
    rep.counts = dict(sorted(counts.items()))
    rep.tool_classes = dict(sorted(classes.items()))
    ends = [e["t_end"] for e in events if isinstance(e, dict) and _is_int(e.get("t_end"))]
    last = max([to_ms(last_us) if last_us is not None else 0, *ends])
    for key, have in (("events", len(events)), ("segments", segment), ("agents", len(agents_declared)), ("turns", len(turns_seen)), ("duration_ms", last)):
        if _is_int(h.get(key)) and h[key] != have:
            errors.append(f"header: {key} {h[key]}, file has {have}")
    return rep


def validate_view(view: Trajectory) -> Report:
    """The scorer view: closed fields, order, turns and runner targets."""
    rep = Report()
    errors = rep.errors
    h = _header(view.header, F.VIEW_HEADER_FIELDS, F.VIEW_FORMAT, errors)
    counts: Counter[str] = Counter()
    last_t = -1
    open_turn: int | None = None
    turns = 0
    events = view.events
    for idx, ev in enumerate(events):
        where = f"view event {idx}"
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str) or ev["type"] not in F.VIEW_FIELDS:
            errors.append(f"{where}: unknown event")
            continue
        et = ev["type"]
        where += f" ({et})"
        counts[et] += 1
        if not _check_keys(ev, {**F.VIEW_COMMON, **F.VIEW_FIELDS[et]}, where, errors):
            continue
        if ev["seq"] != idx:
            errors.append(f"{where}: seq {ev['seq']}, expected {idx}")
        # Time 0 is the opening prompt's dispatch (D-020), or the opening
        # message itself in a capture without the dispatch time.
        if ev["t"] < 0 or (idx == 0 and h.get("clock_origin") != "dispatch" and ev["t"] != 0):
            errors.append(f"{where}: the view starts at time 0 (at the dispatch, at or after it) and never goes below it")
        if ev["t"] < last_t:
            errors.append(f"{where}: time before the previous event's")
        last_t = max(last_t, ev["t"])
        if et == "turn_start":
            turns += 1
            if open_turn is not None or ev["turn"] != turns:
                errors.append(f"{where}: turn {ev['turn']} does not follow a closed turn {turns - 1}")
            open_turn = ev["turn"]
        elif et == "turn_end":
            if ev["turn"] != open_turn:
                errors.append(f"{where}: turn {ev['turn']} is not open")
            open_turn = None
            c = ev["closed_by"]
            if c is not None:
                tgt = events[c] if 0 <= c < idx and isinstance(events[c], dict) else None
                if ev["end"] != "finished":
                    errors.append(f"{where}: only a finished turn is closed by an output")
                elif tgt is None or tgt.get("type") != "to_user" or tgt.get("turn") != ev["turn"]:
                    errors.append(f"{where}: closed_by {c} is not an earlier to_user of turn {ev['turn']}")
        elif et in ("to_user", "user_message") and ev["turn"] != open_turn:
            errors.append(f"{where}: turn {ev['turn']}, but the open turn is {open_turn}")
        if et == "user_message" and ev["secret"] and ev["text"] != "":
            errors.append(f"{where}: an answer to a secret request carries no text")
        if et in F.RUNNER_TYPES:
            _runner_target(ev, events, idx, where, errors, lambda x: x.get("type") == "to_user")
            tg = ev.get("target")
            if et in ("verdict", "wait", "unjudged", "handback") and _is_int(tg) and 0 <= tg < len(events) and isinstance(events[tg], dict) and _is_int(events[tg].get("t")) and events[tg]["t"] > ev["t"]:
                errors.append(f"{where}: dated before the output it is about reached the user")
            # D-035: a scheduled charge never exceeds the span of the wait's
            # floored stamps. A charge an older runner measured from its own
            # stamps (`wait_charges: measured`) may be 1 ms above it.
            slack = 1 if h.get("wait_charges") == "measured" else 0
            if et == "wait" and _is_int(ev.get("charged_ms")) and _is_int(ev.get("t_end")) and ev["charged_ms"] > ev["t_end"] - ev["t"] + slack:
                errors.append(f"{where}: charged {ev['charged_ms']} ms, more than its {ev['t_end'] - ev['t']} ms on the episode clock")
    if open_turn is not None:
        errors.append(f"end: turn {open_turn} open")
    # At most one verdict per output, and never a verdict and an `unjudged`
    # together. That every output has one is a bundle rule (bundle.verdict_coverage).
    verdicts = Counter(e["target"] for e in events if isinstance(e, dict) and e.get("type") == "verdict" and _is_int(e.get("target")))
    unjudged = Counter(e["target"] for e in events if isinstance(e, dict) and e.get("type") == "unjudged" and _is_int(e.get("target")))
    for t, n in (verdicts + unjudged).items():
        if n > 1:
            errors.append(f"output {t} has {verdicts[t]} verdicts and {unjudged[t]} unjudged records; at most one")
    # Wait attribution (A4 of round 8): `replies_to` is non-empty exactly
    # when the policy's reply was due; each output a wait replies to came
    # after the previous wait and before this one, and no output is replied
    # to twice. A wait ends by the episode end.
    # Each output a wait replies to was delivered at or after the previous
    # wait's end and by this wait's end (M2 of round 11: a second request can
    # arrive while the first is pending).
    prev_end, replied = None, set()
    for e in events:
        if not (isinstance(e, dict) and e.get("type") == "wait" and _is_int(e.get("seq"))):
            continue
        where = f"view event {e['seq']} (wait)"
        rt, cs = e.get("replies_to"), e.get("causes")
        if isinstance(rt, list) and isinstance(cs, list) and bool(rt) != ("verdict_reply" in cs):
            errors.append(f"{where}: replies_to is non-empty exactly when its causes include verdict_reply")
        for x in rt if isinstance(rt, list) else []:
            if not _is_int(x) or not (0 <= x < len(events)) or not isinstance(events[x], dict) or not _is_int(events[x].get("t")):
                continue  # typed above
            xt = events[x]["t"]
            if prev_end is not None and xt < prev_end:
                errors.append(f"{where}: replies to output {x}, delivered before the previous wait's end")
            if _is_int(e.get("t_end")) and xt > e["t_end"]:
                errors.append(f"{where}: replies to output {x}, delivered after this wait's end")
            if x in replied:
                errors.append(f"{where}: output {x} is replied to by two waits")
            replied.add(x)
        prev_end = e["t_end"] if _is_int(e.get("t_end")) else e.get("t") if _is_int(e.get("t")) else prev_end
        if _is_int(e.get("t_end")) and _is_int(h.get("duration_ms")) and any(isinstance(x, dict) and x.get("type") == "episode_end" for x in events) \
                and e["t_end"] > h["duration_ms"]:
            errors.append(f"{where}: ends at {e['t_end']}, after the episode end at {h['duration_ms']}")
    # D-035: every wait lies at a hand-back. With hand-back events in the
    # view, a wait with a target starts at that output's delivery and that
    # output closed a hand-back; one without starts at a silent hand-back's
    # instant; a hand-back has at most one wait.
    hbs = [e for e in events if isinstance(e, dict) and e.get("type") == "handback"]
    # (An older runner's measured waits started elsewhere: replays only.)
    if hbs and h.get("wait_charges") != "measured":
        by_target = {e["target"]: e for e in hbs if _is_int(e.get("target"))}
        silent_t = [e["t"] for e in hbs if e.get("target") is None and _is_int(e.get("t"))]
        used: set[Any] = set()
        for e in events:
            if not (isinstance(e, dict) and e.get("type") == "wait" and _is_int(e.get("t"))):
                continue
            where = f"view event {e.get('seq')} (wait)"
            tg = e.get("target")
            if _is_int(tg):
                out = events[tg] if 0 <= tg < len(events) and isinstance(events[tg], dict) else None
                if tg not in by_target or out is None or out.get("t") != e["t"]:
                    errors.append(f"{where}: starts at {e['t']}, not at the delivery of an output that closed a hand-back")
                key: Any = ("target", tg)
            elif tg is None:
                if e["t"] not in silent_t:
                    errors.append(f"{where}: no output closed its hand-back, and no silent hand-back is at {e['t']}")
                key = ("silent", e["t"])
            else:
                continue
            if key in used:
                errors.append(f"{where}: a second wait for one hand-back")
            used.add(key)
    # A hand-back (D-029) is validated on its own terms: its closing output
    # (any agent's, delivered at or before it) or null; the view's turn
    # `closed_by` is what the main run's own ending said, and need not agree.
    for e in events:
        # A charged wait has a verdict on every output it replies to (on its
        # target, for a wait recorded before `replies_to`); one with none is a
        # hand-back with no output, after which the runner reopens (D-023).
        if not (isinstance(e, dict) and e.get("type") == "wait" and _is_int(e.get("charged_ms")) and e["charged_ms"] > 0):
            continue
        rt = e.get("replies_to")
        need = rt if isinstance(rt, list) else ([e["target"]] if e.get("target") is not None else [])
        if any(not (_is_int(x) and x in verdicts) for x in need):
            errors.append(f"view event {e.get('seq')} (wait): a charged wait has a verdict on every output it replies to")
    rep.counts = dict(sorted(counts.items()))
    for key, have in (("events", len(events)), ("turns", turns)):
        if _is_int(h.get(key)) and h[key] != have:
            errors.append(f"header: {key} {h[key]}, file has {have}")
    if _is_int(h.get("duration_ms")) and h["duration_ms"] < max(last_t, 0):
        errors.append(f"header: duration_ms {h['duration_ms']} is shorter than the last event at {last_t}")
    ends = [e for e in events if isinstance(e, dict) and e.get("type") == "episode_end"]
    if ends and _is_int(h.get("duration_ms")) and h["duration_ms"] != ends[0].get("t"):
        errors.append("header: duration_ms is the runner's episode end")
    if ends and ends[0] is not events[-1]:
        errors.append("the runner's episode_end is the view's last event")
    if ends and isinstance(ends[0], dict) and ends[0].get("reason") == "budget_cutoff" \
            and not any(isinstance(e, dict) and e.get("type") == "budget_cutoff" for e in events):
        errors.append("a budget_cutoff end needs the budget_cutoff row that says which budget")
    return rep
