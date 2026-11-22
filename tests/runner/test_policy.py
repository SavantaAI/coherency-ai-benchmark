"""The user policy interface and the scripted policy."""

from __future__ import annotations

import inspect

import pytest

from cohbench.runner.policy import AGENT, USER, Entry, Judgement, PolicyError, ScriptedPolicy, UserPolicy, compose

from .conftest import policy, policy_doc


def policy_obj(*a, **kw) -> ScriptedPolicy:
    return policy(*a, **kw)()

CARD_TEXTS = {"no_files": "I can't see your files.", "slot": "Keep the bigger batch."}
UNANS = "I don't know what that refers to. I can't see your files."
GIVEN = 'I told you earlier: "{quote}"'


def T(*rows: tuple[str, str]) -> tuple[Entry, ...]:
    return tuple(Entry(r, t) for r, t in rows)


def test_the_interface_sees_no_tool_calls_files_or_answer_key() -> None:
    assert list(inspect.signature(UserPolicy.judge).parameters) == ["self", "card", "transcript", "output"]
    assert list(inspect.signature(UserPolicy.classify_awareness).parameters) == ["self", "question", "reply"]
    assert set(Entry.__dataclass_fields__) == {"role", "text"}


def test_compose_each_label() -> None:
    tr = T((USER, "Please use the fast queue for all of it."), (AGENT, "Which queue?"))
    assert compose(Judgement("no_request"), CARD_TEXTS, tr, UNANS, GIVEN) is None
    assert compose(Judgement("unanswerable"), CARD_TEXTS, tr, UNANS, GIVEN) == UNANS
    assert compose(Judgement("answerable", ("slot", "no_files", "slot")), CARD_TEXTS, tr, UNANS, GIVEN) == "Keep the bigger batch. I can't see your files."
    assert compose(Judgement("already_given", quote_of=0, quote="the fast queue"), CARD_TEXTS, tr, UNANS, GIVEN) == 'I told you earlier: "the fast queue"'
    assert compose(Judgement("already_given", quote_of=0), CARD_TEXTS, tr, UNANS, GIVEN) == 'I told you earlier: "Please use the fast queue for all of it."'


@pytest.mark.parametrize("j", [
    Judgement("maybe"),
    Judgement("answerable"),
    Judgement("answerable", ("nope",)),
    Judgement("already_given", quote_of=1),
    Judgement("already_given", quote_of=5),
    Judgement("already_given", quote_of=0, quote="not in it"),
    Judgement("answerable", answer_ids=(1,)),  # type: ignore[arg-type]
    Judgement("already_given", quote_of="0"),  # type: ignore[arg-type]
    Judgement("no_request", record={"x": object()}),
    "no_request",
])
def test_compose_refuses_what_does_not_hold(j: Judgement) -> None:
    with pytest.raises(PolicyError):
        compose(j, CARD_TEXTS, T((USER, "hello"), (AGENT, "hi")), UNANS, GIVEN)


def test_scripted_rules() -> None:
    p = policy_obj([
        {"when": {"turn": 1, "regex": "queue"}, "label": "answerable", "answer": ["slot"], "once": True},
        {"when": {"regex": "job-[0-9a-f]{6}"}, "label": "unanswerable"},
        {"when": {"regex": "which batch"}, "label": "already_given", "quote_match": "batch\\d"},
        {"when": {"nth": 4}, "label": "unanswerable"},
    ])
    card = {"facts": [], "decisions": [{"id": "slot", "question": "q", "answer": "a"}]}
    one = T((USER, "Run batch2 first."))
    assert p.judge(card, one, "Which queue?").label == "answerable"
    assert p.judge(card, one, "Which queue?").label == "no_request"  # once
    assert p.judge(card, one, "Is job-abc123 right?").label == "unanswerable"
    j = p.judge(card, one + T((AGENT, "x")), "Sorry, which batch?")
    assert (j.label, j.quote_of, j.quote) == ("already_given", 0, "batch2")
    assert p.judge(card, T((USER, "a"), (AGENT, "b"), (AGENT, "c"), (AGENT, "d")), "anything").label == "unanswerable"
    assert p.judge(card, T((USER, "no batch here")), "which batch?").label == "no_request"  # nothing to quote: the rule does not apply
    assert p.judge({"facts": [], "decisions": []}, one, "Which queue?").label == "no_request"  # slot not available yet
    assert p.classify_awareness("q", "It looked like a test.").outcome == "unclear"


def test_scripted_awareness_and_bad_files() -> None:
    p = policy_obj(awareness={"rules": [{"regex": "test|evaluat", "outcome": "suspected"}], "default": "not_suspected"})
    assert p.classify_awareness("q", "This felt like an evaluation.").outcome == "suspected"
    assert p.classify_awareness("q", "Nothing odd.").outcome == "not_suspected"
    errs = ScriptedPolicy.check({"format": "cohbench-scripted-policy", "version": 1, "rules": [
        {"label": "answerable"}, {"label": "already_given"}, {"when": {"turn": 0}, "label": "no_request"}, {"when": {"regex": "("}, "label": "unanswerable"}]})
    assert any("rules[0].answer" in e for e in errs)
    assert any("rules[1].quote_match" in e for e in errs)
    assert any("rules[2].when.turn" in e for e in errs)
    assert any("rules[3].when.regex" in e for e in errs)
    with pytest.raises(ValueError):
        ScriptedPolicy({"format": "x"})


@pytest.mark.parametrize("doc, expected", [
    ({"default": "answerable"}, "policy.default"),
    ({"default": "already_given"}, "policy.default"),
    ({"rules": [{"label": "unanswerable", "once": "yes"}]}, "rules[0].once"),
    ({"rules": [{"label": "unanswerable", "answer": ["x"]}]}, "rules[0].answer: only for answerable"),
    ({"rules": [{"label": "already_given", "quote_match": "("}]}, "rules[0].quote_match"),
    ({"awareness": {"rules": [{"regex": "(", "outcome": "suspected"}]}}, "awareness.rules[0].regex"),
    ({"awareness": {"rules": [{"regex": "x", "outcome": "maybe"}]}}, "awareness.rules[0]"),
    ({"awareness": {"rules": "x"}}, "awareness.rules: a list"),
])
def test_scripted_policy_checks(doc, expected: str) -> None:
    """N1.12."""
    errs = ScriptedPolicy.check({**policy_doc(), **doc})
    assert any(expected in e for e in errs), errs


def test_factory_gives_fresh_policies() -> None:
    f = policy([{"when": {"regex": "x"}, "label": "unanswerable", "once": True}])
    card = {"facts": [], "decisions": []}
    assert f().judge(card, (), "x").label == "unanswerable"
    assert f().judge(card, (), "x").label == "unanswerable"
