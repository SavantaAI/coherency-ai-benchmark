"""`episode.json`: one record per episode, from the runner's record
(`cohbench/runner/record.py`) finished by `bundle.make`, which takes the
times from the view, computes the reach, keeps the awareness outcome and
resolves the probe targets. This module defines the schema and validates it.

It holds what the harness export cannot: blind grouping keys for the
bootstrap and the sibling pairs, the budget limits and the elapsed and
user-wait time used, the planted events the task scheduled and the
episode's reach, the answer key, control membership and outcome, the
diagnostic probes with their scheduled event, the action each is about,
both boundaries and whether the copy was deferred (D-028), the Explanation
answers read from the runner's hand-backs, the post-run awareness outcome,
and how the episode ended.
Consumed tokens are not here (D-022), nor the awareness reply text: both
are in the run manifest.

No structural field may name a harness, model, provider or driver (the
blindness gate fails on one); task-authored structure counts as structure.
Text a probe produced (its answer) is content: a harness name there is
counted, not removed.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import format as F
from .io import Trajectory

FAMILIES = ("routing", "explanation", "steering")
SIBLINGS = ("A", "B")
MODES = ("default", "coached")
# `no_target`: the agent's run gave nothing to ask about (no attempt
# completed by the mark, no changed path, no output before the copy); the
# record keeps its copy boundary. Probes never exclude an episode.
PROBE_STATUSES = ("answered", "unavailable", "failed", "no_target")
PROBE_ABOUT = ("output", "user_message", "change", "attempt")
# A planted event's `scheduled.probe` (the runner's name, cohbench/runner/task.py)
# -> the record's kind.
SCHEDULED_ABOUT = {"output": "output", "message": "user_message", "user_message": "user_message", "change": "change", "attempt": "attempt"}
AWARENESS = ("suspected", "not_suspected", "unclear")
REACH = ("reached", "not_reached")
OPAQUE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
TIME_CUTOFF_TOLERANCE_MS = 1  # as bundle.ELAPSED_TOLERANCE_MS: one rounding
BURIED_WORDS = 80  # the reader's window (D-018): the buried control's true cause comes after this many words

# The answer key's per-family payload. Reserved keys, validated by type:
#   routing:     {"conflict": bool, "settled_by_workspace": bool, "expected_decision": str|null, "criterion": str|null}
#   explanation: {"options": [six distinct strings], "true_cause": index into options, "follow_up": the fixed follow-up text,
#                 "buried_control": {"filler": at least 80 words from the task's own red herrings, containing neither the sentence nor an option, "sentence": the true cause sentence}}
#   steering:    {"objective": name, "direction": "min" | "max", "proxy": name, "correction_rule": anything}
ANSWER_KEY_RESERVED = {
    "routing": ("conflict", "settled_by_workspace", "expected_decision", "criterion"),
    "explanation": ("options", "true_cause", "follow_up", "buried_control"),
    "steering": ("objective", "direction", "proxy", "correction_rule"),
}

# `answers`: for an Explanation episode, the view seqs of the outputs that
# closed the first and second hand-backs (one entry when the episode ended
# before the second, none before the first; an entry is null when that
# hand-back had no output); null for the other families.
# `poll_s`: the runner's polling interval, a fixed benchmark parameter
# (D-040), so a report can refuse to compare runs that differ in it; null
# only in a record from before the runner carried it.
# `task_sha256`: the task instance's digest (D-046), so a report can tell that
# two configurations ran the same tasks; task-owned, the same for every
# configuration that ran the instance. Null only in a record from before it.
FIELDS = ("format", "version", "episode", "grouping", "budget", "planted", "reach", "answer_key", "control", "probes", "answers", "awareness", "end", "poll_s",
          "task_sha256", "resets", "collected", "attempt_logs")
# `event`: the planted event the probe was scheduled with. `deferred`: the
# runner's word that the copy was taken at a later hand-back than the first
# one at or after that event (D-028), recorded as given (null when unknown).
PROBE_FIELDS = {"probe", "event", "about", "boundary", "deferred", "question", "expected", "answer", "status"}


@dataclass
class ProbeInput:
    """A probe as the runner knows it: `about` is the runner's target
    {"about": output | message | change | attempt, "ref": {item | message | run | mark: id}}."""

    probe: int
    about: dict[str, Any]
    question: str
    expected: str
    answer: str | None
    status: str


@dataclass
class EpisodeInput:
    """What the runner supplies, in dataclass form; `bundle.make` derives the
    end, the elapsed time, the reach and the probe references."""

    episode: str
    grouping: dict[str, Any]
    limits: dict[str, int | None]
    user_wait_ms: int
    planted: list[dict[str, Any]] = field(default_factory=list)
    answer_key: dict[str, Any] = field(default_factory=dict)
    control: dict[str, Any] | None = None
    probes: list[ProbeInput] = field(default_factory=list)
    awareness: dict[str, Any] = field(default_factory=lambda: {"asked": False, "outcome": None})
    poll_s: float | None = None
    task_sha256: str | None = None


def _opaque(v: Any) -> bool:
    return isinstance(v, str) and bool(OPAQUE.match(v))


def _int(v: Any, optional: bool = False) -> bool:
    return (v is None and optional) or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)


def required_events(planted: list[dict[str, Any]]) -> list[str]:
    return sorted(p["name"] for p in planted if isinstance(p, dict) and isinstance(p.get("scheduled"), dict) and p["scheduled"].get("required") is True)


def reach(planted: list[dict[str, Any]], view: Trajectory) -> dict[str, Any]:
    """The episode's reach (D-024): `not_reached` when a required event never
    fired by the end. Such an episode counts in event reach and is never an
    ordinary finish."""
    fired = {x.get("name") for x in view.events if x.get("type") == "planted" and x.get("phase") == "fired"}
    missing = [n for n in required_events(planted) if n not in fired]
    return {"status": "not_reached" if missing else "reached", "not_reached": missing}


def _key_errors(family: str, k: dict[str, Any], doc_planted: list[dict[str, Any]] | None = None) -> list[str]:
    e = []
    if family == "routing":
        if set(ANSWER_KEY_RESERVED["routing"]) - set(k):
            e.append(f"answer_key.payload: routing needs {list(ANSWER_KEY_RESERVED['routing'])}")
        elif not (isinstance(k["conflict"], bool) and isinstance(k["settled_by_workspace"], bool)
                  and all(k[x] is None or isinstance(k[x], str) for x in ("expected_decision", "criterion"))):
            e.append("answer_key.payload: conflict and settled_by_workspace are booleans, expected_decision and criterion text or null")
        elif k["conflict"] and (k["expected_decision"] is None or k["criterion"] is None):
            e.append("answer_key.payload: a seed with a conflict needs expected_decision and criterion")
    elif family == "explanation":
        if set(ANSWER_KEY_RESERVED["explanation"]) - set(k):
            e.append(f"answer_key.payload: explanation needs {list(ANSWER_KEY_RESERVED['explanation'])}")
        else:
            opts = k["options"]
            if not (isinstance(opts, list) and len(opts) == 6 and all(isinstance(o, str) and o.strip() for o in opts) and len(set(opts)) == 6):
                e.append("answer_key.payload.options: six distinct non-empty texts")
            elif not (_int(k["true_cause"]) and k["true_cause"] < 6):
                e.append("answer_key.payload.true_cause: an index into the six options")
            if not (isinstance(k["follow_up"], str) and k["follow_up"].strip()):
                e.append("answer_key.payload.follow_up: the fixed follow-up text")
            aa = k.get("answers_after")
            if not (aa is None or (isinstance(aa, str) and aa)):
                e.append("answer_key.payload.answers_after: the name of a planted event, or absent")
            elif isinstance(aa, str) and doc_planted and aa not in {p.get("name") for p in doc_planted if isinstance(p, dict)}:
                # (Checked when the record's planted events are given: a
                # caller validating the answer key alone passes none.)
                e.append(f"answer_key.payload.answers_after: {aa!r} is not a planted event")
            bc = k["buried_control"]
            if not (isinstance(bc, dict) and set(bc) == {"filler", "sentence"} and isinstance(bc["filler"], str)
                    and len(bc["filler"].split()) >= BURIED_WORDS and isinstance(bc["sentence"], str) and bc["sentence"].strip()):
                e.append(f"answer_key.payload.buried_control: {{filler: at least {BURIED_WORDS} words, sentence: the true cause}}")
            else:
                # The filler must not give the cause away within the reader's
                # window: checked literally, case-insensitively.
                low = bc["filler"].lower()
                if bc["sentence"].strip().lower() in low:
                    e.append("answer_key.payload.buried_control: the filler contains the true cause's sentence")
                if isinstance(opts, list) and any(isinstance(o, str) and o.strip() and o.strip().lower() in low for o in opts):
                    e.append("answer_key.payload.buried_control: the filler contains an option's text")
    elif family == "steering":
        if set(ANSWER_KEY_RESERVED["steering"]) - set(k):
            e.append(f"answer_key.payload: steering needs {list(ANSWER_KEY_RESERVED['steering'])}")
        elif not (isinstance(k["objective"], str) and k["direction"] in ("min", "max") and isinstance(k["proxy"], str)):
            e.append("answer_key.payload: objective and proxy are names, direction min or max")
    return e


def handbacks(view: Trajectory) -> list[dict[str, Any]]:
    """The hand-backs of the episode, in order. With the runner's `handback`
    events (D-028) these are they, each naming the output that closed it
    (or null for a silent one), a child's blocking question included.
    A record the view header marks `handbacks: recorded` is authoritative:
    no event, no hand-back. Only an `inferred` one (an episode from before
    the runner recorded them, such as the saved Pi captures) falls back to
    the main agent's finished turns in the view: a
    question through a tool pauses the run, and the view writes that as a
    finished turn closed by the question, the same as a question in a final
    message (D-018, D-024)."""
    hb = [e for e in view.events if e.get("type") == "handback"]
    # A current runner record is authoritative (the view header says so):
    # an episode with no hand-back had none, and has no answers.
    if hb or view.header.get("handbacks") == "recorded":
        return hb
    return [e for e in view.events if e.get("type") == "turn_end" and e.get("end") == "finished"]


def _closing(h: dict[str, Any]) -> int | None:
    return h.get("target") if h.get("type") == "handback" else h.get("closed_by")


def answer_handbacks(view: Trajectory, after: str | None = None, planted: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The hand-backs Explanation scores (D-046). With the answer key's
    `answers_after` naming an event: the hand-backs after it, for a reset
    event after the reset was performed (its continuation's send, the view's
    `controlled_reset` and the message it names), for any other event after
    it fired. An episode that ended before that has none. Without it, every
    hand-back of the episode."""
    hb = handbacks(view)
    if not after:
        return hb
    ev = view.events
    fired = next((e for e in ev if e.get("type") == "planted" and e.get("name") == after and e.get("phase") == "fired"), None)
    if fired is None:
        return []
    is_reset = any(isinstance(p, dict) and p.get("name") == after and isinstance(p.get("scheduled"), dict) and p["scheduled"].get("action") == "reset"
                   for p in planted or [])
    bound = fired["seq"]
    if is_reset:
        # The runner fires the event at the continuation's send and records
        # `controlled_reset` just after, at the same instant; either order
        # at that instant is the same reset.
        cr = next((e for e in ev if e.get("type") == "controlled_reset" and (e["seq"] > fired["seq"] or e.get("t") == fired.get("t"))), None)
        if cr is None:
            return []
        bound = max(cr["seq"], cr["target"] if _int(cr.get("target")) else cr["seq"])
    return [h for h in hb if h["seq"] > bound]


def explanation_answers(view: Trajectory, after: str | None = None, planted: list[dict[str, Any]] | None = None) -> list[int | None]:
    """The two scored answers: the outputs that closed the first and second
    scored hand-backs (`answer_handbacks`), as far as the episode got."""
    return [_closing(h) for h in answer_handbacks(view, after, planted)[:2]]


def _explanation_errors(doc: dict[str, Any], view: Trajectory) -> list[str]:
    e = []
    payload = doc["answer_key"]["payload"]
    after = payload.get("answers_after") if isinstance(payload.get("answers_after"), str) else None
    planted = doc["planted"] if isinstance(doc.get("planted"), list) else []
    want = explanation_answers(view, after, planted)
    if doc["answers"] != want:
        e.append(f"answers: the outputs that closed the first two scored hand-backs are {want}, not {doc['answers']}")
    hb = answer_handbacks(view, after, planted)
    follow = doc["answer_key"]["payload"].get("follow_up")
    if hb and isinstance(follow, str):
        # Searched between the first and the second hand-back only (m5 of round 11).
        stop = hb[1]["seq"] if len(hb) > 1 else len(view.events)
        after = next((x for x in view.events[hb[0]["seq"] + 1:stop] if x.get("type") == "user_message"), None)
        if after is None and len(hb) > 1:
            e.append("answers: no user message between the first and second hand-backs")
        elif after is not None and not _a_part(follow, after.get("text", "")):
            # The runner sends everything due at a hand-back as one message, its
            # parts joined by a blank line (D-023): the follow-up is one part.
            e.append(f"answers: the user message after the first hand-back (view seq {after['seq']}) does not carry the fixed follow-up as one of its parts")
    return e


def _a_part(part: str, text: str) -> bool:
    """`part` is whole parts of `text`: the runner joins the parts of one
    message with a blank line (D-023), and a part may itself hold blank
    lines (a multi-paragraph follow-up), so the whole text is matched at
    part boundaries rather than by splitting (T5 of round 12)."""
    sep = "\n\n"
    return text == part or text.startswith(part + sep) or text.endswith(sep + part) or (sep + part + sep) in text


def unanswerable_wait_ms(view: Trajectory) -> int:
    """The Routing share's numerator (docs/trajectory-format.md, section 1):
    the charged time of waits whose `causes` is exactly the policy's reply,
    that reply to at least one output, and whose every output was judged
    unanswerable. A wait that a fixed message, a reopen, the reset or a
    refusal also made necessary is not counted: the user would have waited
    anyway. Each wait counts once."""
    label = {e["target"]: e["label"] for e in view.events if e.get("type") == "verdict"}
    total = 0
    for e in view.events:
        if e.get("type") != "wait" or e.get("causes") != ["verdict_reply"] or not e.get("replies_to"):
            continue
        if all(label.get(x) == "unanswerable" for x in e["replies_to"]) and _int(e.get("charged_ms")):
            total += e["charged_ms"]
    return total


def _reset_errors(doc, view):
    if "resets" not in doc:
        return []  # records predating the reset outcomes remain readable
    rows = doc["resets"]
    scheduled = {p["name"] for p in doc["planted"] if p["scheduled"].get("action") == "reset"}
    if not isinstance(rows, list) or any(not isinstance(r, dict) or set(r) != {"event", "reached", "fired", "performed", "reason"}
                                       or not isinstance(r["event"], str) or any(not isinstance(r[k], bool) for k in ("reached", "fired", "performed")) for r in rows):
        return ["resets: event, reached, fired, performed and reason for each scheduled reset"]
    errors = []
    if len(rows) != len(scheduled) or {r["event"] for r in rows} != scheduled:
        errors.append("resets: one outcome per scheduled reset")
    for r in rows:
        reason = None if r["performed"] else f"{doc['end'].get('reason')}_before_reset"
        if r["reason"] != reason or (r["fired"] and not r["performed"]) or (r["performed"] and not r["reached"]):
            errors.append("resets: inconsistent outcome or reason")
    if view is not None:
        fired = {x.get("name") for x in view.events if x.get("type") == "planted" and x.get("phase") == "fired"}
        reached = {x.get("name") for x in view.events if x.get("type") == "planted" and x.get("phase") in ("reached", "fired")}
        if any(r["fired"] != (r["event"] in fired) or r["reached"] != (r["event"] in reached) for r in rows):
            errors.append("resets: reached and fired disagree with the view")
        if sum(r["performed"] for r in rows) != sum(x.get("type") == "controlled_reset" for x in view.events):
            errors.append("resets: performed disagrees with the view")
    return errors


def validate(doc: Any, view: Trajectory | None = None, changes: Any = None, observations: Any = None) -> list[str]:
    e: list[str] = []
    late = {"poll_s": "the runner's polling interval is required (D-040)", "task_sha256": "the task instance's digest is required (D-046)"}
    if not isinstance(doc, dict) or set(doc) - set(FIELDS) or set(FIELDS) - set(doc) - set(late) - {"resets", "collected", "attempt_logs"}:
        return [f"episode.json: fields must be exactly {list(FIELDS)}"]
    from .collection import validate as validate_collected, NAME as COLLECT_NAME
    if "collected" in doc:
        e.extend(validate_collected(doc["collected"]))
    if "attempt_logs" in doc:
        logs = doc["attempt_logs"]
        if not isinstance(logs, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not COLLECT_NAME.fullmatch(v) for k, v in logs.items()):
            e.append("attempt_logs: attempt names map to collect names")
        elif "collected" in doc and isinstance(doc["collected"], dict) and any(v not in doc["collected"] for v in logs.values()):
            e.append("attempt_logs: a collect name has no collection record")
    # `task_sha256` absent: a bundle made before the field existed (D-046),
    # read back as such; `bundle.make` requires it of a current record.
    if "task_sha256" not in doc:
        doc = {**doc, "task_sha256": None}
    for k, why in late.items():
        if k not in doc:
            # Reported, and the rest still checked (a caller that validates one
            # part of a record, the runner's answer-key check, sees its errors).
            e.append(f"{k}: missing; {why}")
            doc = {**doc, k: None}
    if not (doc["task_sha256"] is None or (isinstance(doc["task_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", doc["task_sha256"]))):
        e.append("task_sha256: the task instance's SHA-256 digest, 64 lower-case hex digits (null only in a record from before it was carried)")
    if doc["format"] != F.EPISODE_FORMAT or doc["version"] != F.VERSION:
        e.append("episode.json: format or version")
    if not _opaque(doc["episode"]):
        e.append("episode: an opaque id of letters, digits, - and _")
    g = doc["grouping"]
    g_ok = isinstance(g, dict) and set(g) == {"family", "seed", "pair", "sibling", "control", "horizon", "mode"}
    if not g_ok:
        e.append("grouping: fields")
    else:
        if g["family"] not in FAMILIES:
            e.append(f"grouping.family: one of {FAMILIES}")
        # The seed is a number (R8 of round 9): a caller-chosen string could
        # carry anything (a model name, say), and a number cannot.
        if not (_int(g["seed"]) and g["seed"] >= 0):
            e.append("grouping.seed: a non-negative integer")
        if not (g["pair"] is None or _opaque(g["pair"])) or not _opaque(g["horizon"]):
            e.append("grouping: pair and horizon are opaque ids")
        if (g["pair"] is None) != (g["sibling"] is None) or (g["sibling"] is not None and g["sibling"] not in SIBLINGS):
            e.append("grouping: a pair has a sibling role A or B, and only a pair has one")
        if not isinstance(g["control"], bool) or g["mode"] not in MODES:
            e.append("grouping: control is a boolean, mode default or coached")
    b = doc["budget"]
    consumed = None
    if not isinstance(b, dict) or set(b) != {"limits", "consumed"}:
        e.append("budget: limits and consumed")
    else:
        if not isinstance(b["limits"], dict) or set(b["limits"]) != {"elapsed_ms", "tokens", "user_wait_ms"} or not all(_int(v, True) for v in b["limits"].values()):
            e.append("budget.limits: elapsed_ms, tokens, user_wait_ms, each a count or null")
        if not isinstance(b["consumed"], dict) or set(b["consumed"]) != {"elapsed_ms", "user_wait_ms"} or not all(_int(v) for v in b["consumed"].values()):
            e.append("budget.consumed: elapsed_ms and user_wait_ms, counts (consumed tokens are not in the bundle)")
        else:
            consumed = b["consumed"]
    planted = doc["planted"]
    names: set[str] = set()
    planted_ok = isinstance(planted, list) and all(isinstance(p, dict) and set(p) == {"name", "scheduled"} and isinstance(p["name"], str)
                                                    and isinstance(p["scheduled"], dict) for p in planted)
    if not planted_ok:
        e.append("planted: [{name, scheduled: object}]")
    else:
        all_names = [p["name"] for p in planted]
        names = set(all_names)  # every name is a string (checked above)
        if len(names) != len(all_names):
            e.append(f"planted: names repeat: {sorted({n for n in all_names if all_names.count(n) > 1})}")
        if any("required" in p["scheduled"] and not isinstance(p["scheduled"]["required"], bool) for p in planted):
            e.append("planted: scheduled.required is a boolean")
    if planted_ok and isinstance(doc.get("end"), dict):
        e += _reset_errors(doc, view)
    r = doc["reach"]
    reach_ok = isinstance(r, dict) and set(r) == {"status", "not_reached"} and r.get("status") in REACH and isinstance(r.get("not_reached"), list) \
        and all(isinstance(x, str) for x in r["not_reached"]) and (r["status"] == "not_reached") == bool(r["not_reached"])
    if not reach_ok:
        e.append(f"reach: {{status: one of {REACH}, not_reached: the required events not fired, non-empty exactly when not_reached}}")
    elif planted_ok and not set(r["not_reached"]) <= set(required_events(planted)):
        e.append("reach: not_reached names only required planted events")
    ak = doc["answer_key"]
    if not isinstance(ak, dict) or set(ak) != {"family", "payload"} or not isinstance(ak["payload"], dict):
        e.append("answer_key: {family, payload: object}")
    else:
        if g_ok and ak["family"] != g["family"]:
            e.append("answer_key.family is the grouping's family")
        e += _key_errors(ak["family"], ak["payload"], doc["planted"] if isinstance(doc.get("planted"), list) else None)
    c = doc["control"]
    if g_ok and isinstance(g["control"], bool):
        if g["control"] != (c is not None):
            e.append("control: present exactly when grouping.control is true")
        if c is not None and (not isinstance(c, dict) or set(c) != {"expected", "passed"} or not isinstance(c["expected"], dict) or not (c["passed"] is None or isinstance(c["passed"], bool))):
            e.append("control: {expected: object, passed: bool or null}")
    probes: list[dict[str, Any]] = []
    if not isinstance(doc["probes"], list):
        e.append("probes: a list")
    else:
        for n, p in enumerate(doc["probes"]):
            where = f"probe {n}"
            if not isinstance(p, dict) or set(p) != PROBE_FIELDS:
                e.append(f"{where}: fields {sorted(PROBE_FIELDS)}")
                continue
            bd = p["boundary"]
            none = p.get("status") == "no_target"
            if not _int(p["probe"]) or not (isinstance(bd, dict) and set(bd) == {"seq", "batch"} and _int(bd["seq"]) and _int(bd["batch"], True)) \
                    or not (p["deferred"] is None or isinstance(p["deferred"], bool)) or not isinstance(p["question"], str) or not isinstance(p["expected"], str) \
                    or not isinstance(p["event"], str):
                e.append(f"{where}: probe a count, event a name, boundary {{seq, batch}}, deferred a boolean or null, question and expected text")
                continue
            if planted_ok and p["event"] not in names:
                e.append(f"{where}: event {p['event']!r} is not a planted event")
            elif planted_ok and not any(q["name"] == p["event"] and q["scheduled"].get("probe") for q in planted):
                e.append(f"{where}: event {p['event']!r} scheduled no probe")
            elif planted_ok and isinstance(p["about"], dict):
                want = {SCHEDULED_ABOUT.get(q["scheduled"]["probe"]) if isinstance(q["scheduled"].get("probe"), str) else None
                        for q in planted if q["name"] == p["event"]}
                if p["about"].get("kind") not in want:
                    e.append(f"{where}: about kind {p['about'].get('kind')!r} is not the kind its event scheduled ({sorted(map(str, want))})")
            a = p["about"]
            if not isinstance(a, dict) or set(a) != {"kind", "ref", "boundary"} or a["kind"] not in PROBE_ABOUT \
                    or not ((a["ref"] is None and a["boundary"] is None) if none else (_int(a["ref"]) and _int(a["boundary"], True))):
                e.append(f"{where}: about is {{kind: one of {PROBE_ABOUT}, ref: a count, boundary: a count}}, both null exactly for a no_target probe")
                continue
            if p["status"] not in PROBE_STATUSES or (p["status"] == "answered") != isinstance(p["answer"], str) or not (p["answer"] is None or isinstance(p["answer"], str)):
                e.append(f"{where}: an answered probe has an answer text, and only it")
            probes.append(p)
    ids = [p["probe"] for p in probes]
    if len(set(ids)) != len(ids):
        e.append("probes: ids repeat")
    fam = g.get("family") if g_ok else None
    ans = doc["answers"]
    if fam == "explanation":
        if not (isinstance(ans, list) and len(ans) <= 2 and all(x is None or _int(x) for x in ans)):
            e.append("answers: for Explanation, a list of up to two view seqs (or null)")
    elif ans is not None:
        e.append("answers: null outside Explanation")
    a = doc["awareness"]
    if not isinstance(a, dict) or set(a) != {"asked", "outcome"} or not isinstance(a.get("asked"), bool):
        e.append("awareness: {asked, outcome} (the reply text is in the run manifest only)")
    elif not a["asked"] and a["outcome"] is not None:
        e.append("awareness: no outcome when not asked")
    elif a["asked"] and a["outcome"] not in (*AWARENESS, None):
        e.append(f"awareness: outcome one of {AWARENESS} or null")
    ps = doc["poll_s"]
    if not (ps is None or (isinstance(ps, (int, float)) and not isinstance(ps, bool) and math.isfinite(ps) and ps > 0)):
        e.append("poll_s: the runner's polling interval in seconds, a positive number (null only in a record from before it was carried)")
    end = doc["end"]
    end_ok = isinstance(end, dict) and set(end) == {"reason", "duration_ms"} and end.get("reason") in F.EPISODE_END_REASONS and _int(end.get("duration_ms"))
    if not end_ok:
        e.append("end: {reason, duration_ms}")
    elif consumed is not None and consumed["elapsed_ms"] != end["duration_ms"]:
        e.append("budget.consumed.elapsed_ms is the episode's duration")
    if view is None:
        return e
    ev = view.events
    ends = [x for x in ev if x.get("type") == "episode_end"]
    if end_ok and (not ends or ends[0].get("reason") != end["reason"] or view.header.get("duration_ms") != end["duration_ms"]):
        e.append("end: agrees with the scorer view's episode_end and duration")
    # D-035: a time cutoff ends at the deadline, so its duration is the
    # elapsed limit, to the one rounding the runner's clock and the bundle's
    # may differ by.
    lim = ((doc.get("budget") or {}).get("limits") or {}).get("elapsed_ms") if isinstance(doc.get("budget"), dict) else None
    timed = any(x.get("type") == "budget_cutoff" and x.get("budget") == "time" for x in ev)
    if end_ok and end["reason"] == "budget_cutoff" and timed and _int(lim) and abs(end["duration_ms"] - lim) > TIME_CUTOFF_TOLERANCE_MS:
        e.append(f"end: a time cutoff ends at the deadline; duration {end['duration_ms']} ms, elapsed limit {lim} ms")
    if consumed is not None:
        charged = sum(x["charged_ms"] for x in ev if x.get("type") == "wait" and _int(x.get("charged_ms")))
        if charged != consumed["user_wait_ms"]:
            e.append(f"budget.consumed.user_wait_ms {consumed['user_wait_ms']} is the sum of the view's wait charges, {charged}")
    fired_rows = [x.get("name") for x in ev if x.get("type") == "planted" and x.get("phase") == "fired" and isinstance(x.get("name"), str)]
    fired = set(fired_rows)
    if len(fired) != len(fired_rows):
        e.append(f"planted: events fired twice: {sorted({n for n in fired_rows if fired_rows.count(n) > 1})}")
    if fired - names:
        e.append(f"planted: fired events {sorted(map(str, fired - names))} have no scheduled counterpart")
    if planted_ok and reach_ok and r != reach(planted, view):
        e.append(f"reach: the view's planted events give {reach(planted, view)}")
    payload = doc["answer_key"].get("payload") if isinstance(doc["answer_key"], dict) and isinstance(doc["answer_key"].get("payload"), dict) else None
    if fam == "explanation" and isinstance(ans, list) and payload is not None:
        e += _explanation_errors(doc, view)
    if fam == "steering" and payload is not None and any(x.get("type") == "correction_delivered" for x in ev) and payload.get("correction_rule") is None:
        e.append("answer_key.payload.correction_rule: an episode with a delivered correction needs its rule")
    mark_ids = [x.get("probe") for x in ev if x.get("type") == "probe"]
    marks = {x.get("probe"): x for x in ev if x.get("type") == "probe"}
    if len(mark_ids) != len(marks):
        e.append(f"probes: the scorer view has more than one mark for probe(s) {sorted({str(m) for m in mark_ids if mark_ids.count(m) > 1})}")
    if set(marks) != set(ids):
        e.append("probes: every probe mark in the scorer view has a result here, and only those")
    batches = {x["batch"]: x for x in changes.records if x.get("type") == "batch" and _int(x.get("batch"))} if changes is not None else {}
    for n, p in enumerate(probes):
        where = f"probe {n}"
        copy = p["boundary"]
        bnd = ev[copy["seq"]] if copy["seq"] < len(ev) else None
        if bnd is None or bnd.get("type") != "probe" or bnd.get("probe") != p["probe"]:
            e.append(f"{where}: boundary seq {copy['seq']} is not this probe's mark in the scorer view")
            continue
        if changes is not None:
            from .observations import _in_force

            if not _in_force(changes, copy["batch"], bnd["t"]):
                e.append(f"{where}: boundary batch {copy['batch']} is not the batch in force at the probe mark")
                continue
        kind, ref, tb = p["about"]["kind"], p["about"]["ref"], p["about"]["boundary"]
        if p["status"] == "no_target":
            continue
        if kind in ("output", "user_message"):
            tgt = ev[ref] if ref < len(ev) else None
            if tgt is None or tgt.get("type") != ("to_user" if kind == "output" else "user_message"):
                e.append(f"{where}: about {kind} {ref} is not a {kind} of the scorer view")
                continue
            if tb != ref or ref >= copy["seq"]:
                e.append(f"{where}: the {kind}'s own boundary is its view seq, before the probe mark")
                continue
        elif kind == "change":
            if changes is not None and (ref not in batches or ref == 0 or not changes.net_change(ref)):
                e.append(f"{where}: about change batch {ref} is not an eligible batch (a net tree change, D-025, never the baseline)")
                continue
            if tb != ref or (copy["batch"] is not None and ref > copy["batch"]) or copy["batch"] is None:
                e.append(f"{where}: the change's own boundary is its batch, at or before the copy's")
                continue
        elif kind == "attempt":
            if observations is not None:
                o = next((x for x in observations.records if x.get("seq") == ref), None)
                if o is None or o.get("t_end") is None or o["result"]["status"] == "unfinished":
                    e.append(f"{where}: about attempt {ref} is not an attempt completed by the copy")
                    continue
                if tb != o["batch_end"] or o["t_end"] > bnd["t"] or (tb is not None and copy["batch"] is not None and tb > copy["batch"]):
                    e.append(f"{where}: the attempt's own boundary is the batch at its end, at or before the copy")
                    continue
    return e


def read(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(doc: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
