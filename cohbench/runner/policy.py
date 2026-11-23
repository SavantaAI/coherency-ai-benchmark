"""The user policy (contract 2): how the simulated user judges what the agent
showed it.

A policy has two required calls. A deadline-aware classifier also exposes
`verdict_deadline_s` and `clock()`. Its `judge` accepts an absolute `deadline`
on that clock. The runner calls it synchronously; scripted policies retain
these original two calls.

- `judge(card, transcript, output)`: one user-channel output (a final
  message, a text sent through the driver's message tool, a pending question
  or secret request: all the same to the policy), the user card as the user
  knows it now, and the user-visible transcript before that output. It
  returns a `Judgement`.
- `classify_awareness(question, reply)`: the post-run check. It returns an
  `AwarenessJudgement`.

What the policy receives is what the user would see and nothing else. The
transcript is the role (`user` or `agent`) and text of each message, in the
order the messages were delivered: a correction enters it when the harness
took it, not when it was queued. There are no message kinds, no tool calls,
no file contents, no answer key and no harness. `DeliveredTranscript` adds
an index-aligned host-only side channel naming the card items each actual
user message delivered. This metadata is not included in model prompts. The card is a deep copy
holding only the items available at that moment: an item with
`available_after: <event>` appears once that event has fired.

A policy never writes a reply. It names the label, and for `answerable` the
card items that answer, for `already_given` the earlier user message to
quote; the runner builds the fixed reply from the task's texts (`compose`).
`record` is opaque to the runner and goes to the classifier log as given; it
must be JSON-serialisable.

Policies are made per episode through a factory (`PolicyFactory`), so no
state carries from one episode to the next. `ScriptedPolicy` is the test
policy; phase 3 supplies the classifier.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

LABELS = ("answerable", "already_given", "unanswerable", "no_request")
ASKING = ("answerable", "already_given", "unanswerable")
AWARENESS = ("suspected", "not_suspected", "unclear")
USER = "user"
AGENT = "agent"


@dataclass(frozen=True)
class Entry:
    """One message of the user-visible transcript."""

    role: str  # user or agent
    text: str


class DeliveredTranscript(tuple):
    """Role/text entries plus the runner's delivery facts at matching indices.

    Entry stays the two-field interface used by scripted policies. Delivery
    facts are not text and are never included in a classifier prompt.
    """
    def __new__(cls, entries, delivered_ids):
        result = super().__new__(cls, entries)
        result.delivered_ids = tuple(None if ids is None else tuple(ids) for ids in delivered_ids)
        if len(result) != len(result.delivered_ids):
            raise ValueError("delivery facts must match transcript entries")
        return result


@dataclass(frozen=True)
class Judgement:
    label: str
    answer_ids: tuple[str, ...] = ()  # answerable: card item ids, in reply order
    quote_of: int | None = None  # already_given: index of a user entry in the transcript
    quote: str | None = None  # already_given: optional span of that entry; the whole entry when None
    record: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AwarenessJudgement:
    outcome: str
    record: Mapping[str, Any] = field(default_factory=dict)


class UserPolicy(Protocol):
    def judge(self, card: Mapping[str, Any], transcript: Sequence[Entry], output: str) -> Judgement: ...

    def classify_awareness(self, question: str, reply: str) -> AwarenessJudgement: ...


PolicyFactory = Callable[[], UserPolicy]


class PolicyError(RuntimeError):
    """The policy raised, or returned something the runner cannot use."""


class EndpointError(PolicyError):
    """The classifier endpoint failed before a verdict was available."""


class DeadlineExceeded(EndpointError):
    """The absolute judgement deadline arrived before completion."""


def checked(j: Any) -> Judgement:
    """`j` as a Judgement whose fields have the declared types, or PolicyError."""
    if not isinstance(j, Judgement):
        raise PolicyError(f"judge returned {type(j).__name__}, not a Judgement")
    if not isinstance(j.label, str) or j.label not in LABELS:
        raise PolicyError(f"label {j.label!r} is not one of {LABELS}")
    if not isinstance(j.answer_ids, (tuple, list)) or not all(isinstance(x, str) for x in j.answer_ids):
        raise PolicyError("answer_ids must be a sequence of card item ids")
    if j.quote_of is not None and (not isinstance(j.quote_of, int) or isinstance(j.quote_of, bool)):
        raise PolicyError("quote_of must be an index or None")
    if j.quote is not None and not isinstance(j.quote, str):
        raise PolicyError("quote must be text or None")
    if not isinstance(j.record, Mapping):
        raise PolicyError("record must be a mapping")
    try:
        json.dumps(dict(j.record))
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"record is not JSON-serialisable: {exc}") from None
    return j


def compose(j: Judgement, card_texts: Mapping[str, str], transcript: Sequence[Entry], unanswerable: str, already_given: str) -> str | None:
    """The fixed reply for a judgement, or None for `no_request`. `card_texts`
    holds only the items available now. Raises PolicyError when the judgement
    does not hold together."""
    j = checked(j)
    if j.label == "no_request":
        return None
    if j.label == "unanswerable":
        return unanswerable
    if j.label == "answerable":
        if not j.answer_ids:
            raise PolicyError("answerable needs the card items that answer")
        missing = [i for i in j.answer_ids if i not in card_texts]
        if missing:
            raise PolicyError(f"answerable names card items that are not available: {missing}")
        return " ".join(card_texts[i] for i in dict.fromkeys(j.answer_ids))
    n = j.quote_of
    if n is None or not 0 <= n < len(transcript) or transcript[n].role != USER:
        raise PolicyError(f"already_given needs the index of an earlier user message, got {n!r}")
    quote = transcript[n].text if j.quote is None else j.quote
    if not quote or quote not in transcript[n].text:
        raise PolicyError("already_given quotes text that is not in the earlier message")
    return already_given.replace("{quote}", quote)


# ---------------------------------------------------------------------------
# The scripted policy
# ---------------------------------------------------------------------------

SCRIPT_FORMAT = "cohbench-scripted-policy"


@dataclass
class _Rule:
    turn: int | None
    nth: int | None
    regex: re.Pattern[str] | None
    label: str
    answer: tuple[str, ...]
    quote_match: re.Pattern[str] | None
    once: bool
    used: bool = False


def _pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _regex_ok(v: Any) -> str | None:
    if not isinstance(v, str) or not v:
        return "a regular expression"
    try:
        re.compile(v)
    except (re.error, RecursionError, OverflowError) as exc:
        return str(exc)
    return None


class ScriptedPolicy:
    """Verdicts by rule. File format:

        {"format": "cohbench-scripted-policy", "version": 1,
         "rules": [{"when": {"turn": 2, "nth": 3, "regex": "..."},       every key optional, all must hold
                    "label": "answerable", "answer": ["d1"],              answerable; skipped while an item is not available
                    "quote_match": "regex",                               already_given: the latest user message it finds; quotes the match
                    "once": true}],                                       the rule is used at most once
         "default": "no_request" or "unanswerable",
         "awareness": {"rules": [{"regex": "...", "outcome": "suspected"}], "default": "unclear"}}

    `turn` is the number of user messages so far (the turn the output belongs
    to, counting from 1). `nth` is the output's number among the agent's
    outputs, from 1. The first rule that holds decides.
    """

    def __init__(self, doc: Mapping[str, Any]):
        problems = self.check(doc)
        if problems:
            raise ValueError("scripted policy:\n  " + "\n  ".join(problems))
        doc = copy.deepcopy(dict(doc))
        self.rules = [
            _Rule(r.get("when", {}).get("turn"), r.get("when", {}).get("nth"),
                  re.compile(r["when"]["regex"], re.S) if "regex" in r.get("when", {}) else None,
                  r["label"], tuple(r.get("answer", ())),
                  re.compile(r["quote_match"], re.S) if "quote_match" in r else None, bool(r.get("once", False)))
            for r in doc.get("rules", [])
        ]
        self.default = doc.get("default", "no_request")
        aw = doc.get("awareness", {})
        self.aw_rules = [(re.compile(a["regex"], re.S | re.I), a["outcome"]) for a in aw.get("rules", [])]
        self.aw_default = aw.get("default", "unclear")

    @classmethod
    def factory(cls, doc: Mapping[str, Any]) -> PolicyFactory:
        problems = cls.check(doc)
        if problems:
            raise ValueError("scripted policy:\n  " + "\n  ".join(problems))
        frozen = copy.deepcopy(dict(doc))
        return lambda: cls(frozen)

    @classmethod
    def file_factory(cls, path: str | Path) -> PolicyFactory:
        return cls.factory(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def check(doc: Any) -> list[str]:
        e: list[str] = []
        if not isinstance(doc, Mapping):
            return ["policy: an object"]
        if doc.get("format") != SCRIPT_FORMAT or doc.get("version") != 1:
            e.append(f"policy.format and version: {SCRIPT_FORMAT!r} and 1")
        extra = set(doc) - {"format", "version", "rules", "default", "awareness", "description"}
        if extra:
            e.append(f"policy: unknown fields {sorted(map(str, extra))}")
        if doc.get("default", "no_request") not in ("no_request", "unanswerable"):
            e.append("policy.default: no_request or unanswerable (a default cannot name card items or a quote)")
        rules = doc.get("rules", [])
        if not isinstance(rules, list):
            return e + ["policy.rules: a list"]
        for n, r in enumerate(rules):
            p = f"policy.rules[{n}]"
            if not isinstance(r, Mapping) or set(r) - {"when", "label", "answer", "quote_match", "once"}:
                e.append(f"{p}: an object with when, label, answer, quote_match, once")
                continue
            w = r.get("when", {})
            if not isinstance(w, Mapping) or set(w) - {"turn", "nth", "regex"}:
                e.append(f"{p}.when: turn, nth, regex")
            else:
                for k in ("turn", "nth"):
                    if k in w and not _pos_int(w[k]):
                        e.append(f"{p}.when.{k}: a positive integer")
                if "regex" in w and (why := _regex_ok(w["regex"])):
                    e.append(f"{p}.when.regex: {why}")
            if "quote_match" in r and (why := _regex_ok(r["quote_match"])):
                e.append(f"{p}.quote_match: {why}")
            if "once" in r and not isinstance(r["once"], bool):
                e.append(f"{p}.once: true or false")
            lab = r.get("label")
            if lab not in LABELS:
                e.append(f"{p}.label: one of {LABELS}")
            if lab == "answerable" and not (isinstance(r.get("answer"), list) and r["answer"] and all(isinstance(x, str) for x in r["answer"])):
                e.append(f"{p}.answer: card item ids, for answerable")
            if lab != "answerable" and "answer" in r:
                e.append(f"{p}.answer: only for answerable")
            if lab == "already_given" and "quote_match" not in r:
                e.append(f"{p}.quote_match: needed for already_given")
        aw = doc.get("awareness", {})
        if not isinstance(aw, Mapping) or set(aw) - {"rules", "default"} or aw.get("default", "unclear") not in AWARENESS:
            e.append(f"policy.awareness: rules and a default in {AWARENESS}")
        elif not isinstance(aw.get("rules", []), list):
            e.append("policy.awareness.rules: a list")
        else:
            for n, a in enumerate(aw.get("rules", [])):
                if not isinstance(a, Mapping) or set(a) != {"regex", "outcome"} or a.get("outcome") not in AWARENESS:
                    e.append(f"policy.awareness.rules[{n}]: {{regex, outcome}}, outcome in {AWARENESS}")
                elif why := _regex_ok(a["regex"]):
                    e.append(f"policy.awareness.rules[{n}].regex: {why}")
        return e

    def judge(self, card: Mapping[str, Any], transcript: Sequence[Entry], output: str) -> Judgement:
        turn = sum(1 for x in transcript if x.role == USER)
        nth = sum(1 for x in transcript if x.role == AGENT) + 1
        available = {x["id"] for k in ("facts", "decisions") for x in card.get(k, [])}
        for n, r in enumerate(self.rules):
            if r.used and r.once:
                continue
            if r.turn is not None and r.turn != turn:
                continue
            if r.nth is not None and r.nth != nth:
                continue
            if r.regex is not None and not r.regex.search(output):
                continue
            if r.label == "answerable" and not set(r.answer) <= available:
                continue  # the user does not know this yet
            rec = {"policy": "scripted", "rule": n, "turn": turn, "nth": nth}
            if r.label == "already_given":
                assert r.quote_match is not None
                for i in range(len(transcript) - 1, -1, -1):
                    m = r.quote_match.search(transcript[i].text) if transcript[i].role == USER else None
                    if m:
                        r.used = True
                        return Judgement("already_given", quote_of=i, quote=m.group(0), record=rec)
                continue  # nothing to quote: the rule does not apply
            r.used = True
            return Judgement(r.label, answer_ids=r.answer, record=rec)
        return Judgement(self.default, record={"policy": "scripted", "rule": None, "turn": turn, "nth": nth})

    def classify_awareness(self, question: str, reply: str) -> AwarenessJudgement:
        for n, (rx, outcome) in enumerate(self.aw_rules):
            if rx.search(reply):
                return AwarenessJudgement(outcome, {"policy": "scripted", "rule": n})
        return AwarenessJudgement(self.aw_default, {"policy": "scripted", "rule": None})


# ---------------------------------------------------------------------------
# Policy kinds: a policy is named by a kind and a file; `run_episode` builds
# the factory from that file itself, so the file admission checked is the
# file the policy uses. Another package registers a kind (the classifier):
#     register_policy_kind("classifier", lambda path: make_factory(path))
# ---------------------------------------------------------------------------

POLICY_KINDS: dict[str, Callable[[str | Path], PolicyFactory]] = {}


def register_policy_kind(kind: str, maker: Callable[[str | Path], PolicyFactory]) -> None:
    """Register a policy kind: `maker(path)` returns the factory for that file."""
    if not kind or ":" in kind:
        raise ValueError(f"a policy kind is a plain name, not {kind!r}")
    POLICY_KINDS[kind] = maker


def policy_factory(kind: str, path: str | Path) -> PolicyFactory:
    if kind not in POLICY_KINDS:
        raise ValueError(f"policy kind {kind!r}: one of {sorted(POLICY_KINDS)}")
    return POLICY_KINDS[kind](path)


register_policy_kind("scripted", lambda path: ScriptedPolicy.file_factory(path))
