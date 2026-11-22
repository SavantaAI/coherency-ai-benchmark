"""What a script can express: the user's message, conditions on variables,
numeric comparisons, and a bounded repeat."""

from __future__ import annotations

import pytest

from cohbench.scripted import ScriptError, load_script

from .conftest import Convo, calls_of, text_of

FALLBACK = "Sorry, I can't continue with this right now."


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def test_a_handler_acts_on_what_the_user_answered(server):
    """A routing script: the user's answer decides which file is kept, the
    reply quotes it, and an answer the user cannot give is handled too."""
    server.load({"version": 1, "handlers": [{
        "capture": [{"var": "choice", "regex": "keep (j-\\d+)"}],
        "steps": [{"do": "branch", "cases": [
            {"when": {"var": {"name": "user_message", "contains": "I don't know what that refers to"}},
             "steps": [{"do": "reply", "text": "Sorry, I meant the two export jobs. Which one should stay?", "end": "question"}]},
            {"when": {"var_set": "choice"}, "steps": [
                {"do": "bash", "command": "./jobs keep {{choice}}"},
                {"do": "reply", "text": "Kept {{choice}} as you said: {{user_message}}"}]},
        ], "else": [{"do": "reply", "text": "I did not get a decision."}]}]}] * 2})
    c = Convo(server)
    r = c.user("I don't know what that refers to. I can't see your files.")
    assert text_of(r).startswith("Sorry, I meant") and server.app.log.lines[-1]["ends_with"] == "question"
    r = c.user("Please keep j-42.")
    assert calls_of(r)[0]["input"]["command"] == "./jobs keep j-42"
    assert text_of(c.results(["kept"])) == "Kept j-42 as you said: Please keep j-42."


def test_a_required_capture_from_the_user_message(server):
    server.load({"version": 1, "handlers": [{"capture": [{"var": "n", "regex": "(\\d+)", "required": True}],
                                             "steps": [], "final": "n={{n}}"}] * 2})
    c = Convo(server)
    assert text_of(c.user("use 12")) == "n=12"
    assert text_of(c.user("no number")) == FALLBACK and codes(server) == ["capture_failed"]


@pytest.mark.parametrize("cond,expected", [
    ({"var": {"name": "x", "equals": "7"}}, True),
    ({"var": {"name": "x", "equals": "{{y}}"}}, False),
    ({"var": {"name": "x", "contains": "7"}}, True),
    ({"var": {"name": "x", "regex": "^\\d$"}}, True),
    ({"var": {"name": "x", "lt": 10}}, True),
    ({"var": {"name": "x", "ge": 7}}, True),
    ({"var": {"name": "x", "gt": {"var": "y"}}}, False),
    ({"var": {"name": "x", "lt": {"var": "y"}}}, True),
    ({"var": {"name": "x", "eq": 7.0}}, True),
    ({"var": {"name": "x", "ne": 7}}, False),
])
def test_variable_conditions(server, cond, expected):
    server.load({"version": 1, "vars": {"x": "7", "y": "12"}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": cond, "steps": [{"do": "reply", "text": "yes"}]}],
         "else": [{"do": "reply", "text": "no"}]}]}]})
    assert text_of(Convo(server).user("go")) == ("yes" if expected else "no")


def test_a_text_condition_on_a_variable_with_no_value_is_false(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "bash", "command": "true", "capture": [{"var": "v", "regex": "never(x)"}]},
        {"do": "branch", "cases": [{"when": {"var": {"name": "v", "contains": "a"}}, "steps": [{"do": "reply", "text": "yes"}]}],
         "else": [{"do": "reply", "text": "no"}]}]}]})
    c = Convo(server)
    c.user("go")
    assert text_of(c.results(["ok"])) == "no"


def test_a_numeric_comparison_on_a_non_number_fails_loudly(server):
    server.load({"version": 1, "vars": {"x": "seven"}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": {"var": {"name": "x", "lt": 3}}, "steps": []}]}], "final": "x"}]})
    assert text_of(Convo(server).user("go")) == FALLBACK and codes(server) == ["not_a_number"]


def optimise_script(max_rounds: int) -> dict:
    """Edit and measure until a round brings no gain; keep the best count."""
    return {"version": 1, "vars": {"best": "1000000"}, "handlers": [{"steps": [
        {"do": "repeat", "max": max_rounds, "until": {"var": {"name": "improved", "equals": "no"}}, "steps": [
            {"do": "bash", "command": "./optimise && ./measure",
             "capture": [{"var": "ops", "regex": "operations: (\\d+)", "required": True}]},
            {"do": "branch", "cases": [{"when": {"var": {"name": "ops", "lt": {"var": "best"}}}, "steps": [
                {"do": "set", "var": "best", "value": "{{ops}}"}, {"do": "set", "var": "improved", "value": "yes"}]}],
             "else": [{"do": "set", "var": "improved", "value": "no"}]},
        ]}], "final": "Best operation count {{best}}."}]}


def run_rounds(server, counts):
    c = Convo(server)
    r = c.user("make it faster")
    rounds = 0
    for n in counts:
        if not calls_of(r):
            break
        rounds += 1
        r = c.results([f"operations: {n}"])
    return rounds, text_of(r)


def test_repeat_stops_when_its_condition_holds(server):
    server.load(optimise_script(10))
    assert run_rounds(server, [500, 400, 300, 300, 200]) == (4, "Best operation count 300.")
    rep = [x for x in server.app.log.lines if "repeats" in x][-1]["repeats"]
    assert rep == [{"step": "$.handlers[0].steps[0]", "iterations": 4, "stopped": "until"}]


def test_repeat_stops_at_its_maximum(server):
    server.load(optimise_script(2))
    assert run_rounds(server, [500, 400, 300]) == (2, "Best operation count 400.")
    assert [x for x in server.app.log.lines if "repeats" in x][-1]["repeats"][0]["stopped"] == "max"


def test_repeat_is_validated():
    for bad, where in (({"do": "repeat", "max": 0, "steps": [{"do": "say", "text": "x"}]}, "max"),
                       ({"do": "repeat", "max": 1001, "steps": [{"do": "say", "text": "x"}]}, "max"),
                       ({"do": "repeat", "max": 2, "steps": []}, "steps"),
                       ({"do": "repeat", "max": 2.5, "steps": [{"do": "say", "text": "x"}]}, "max")):
        with pytest.raises(ScriptError) as e:
            load_script({"version": 1, "handlers": [{"steps": [bad], "final": "x"}]})
        assert e.value.errors[0].startswith(f"$.handlers[0].steps[0].{where}"), e.value.errors


def test_a_repeat_without_tool_calls_is_bounded(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "repeat", "max": 1000, "steps": [{"do": "repeat", "max": 1000, "steps": [{"do": "set", "var": "a", "value": "b"}]}]}],
        "final": "done {{a}}"}]})
    assert text_of(Convo(server).user("go")) == FALLBACK and codes(server) == ["too_many_steps"]


def test_conditions_on_variables_are_validated():
    for cond, msg in (({"var": {"name": "nope", "equals": "a"}}, "never set"),
                      ({"var": {"name": "user_message"}}, "needs exactly one"),
                      ({"var": {"name": "user_message", "lt": 1, "gt": 2}}, "needs exactly one"),
                      ({"var": {"name": "user_message", "lt": "1"}}, "expected a finite number"),
                      ({"var": {"name": "user_message", "lt": {"var": "zz"}}}, "never set"),
                      ({"var": {"name": "user_message", "regex": "(a*)*"}}, "exponential")):
        with pytest.raises(ScriptError) as e:
            load_script({"version": 1, "handlers": [{"expect": cond, "steps": [], "final": "x"}]})
        assert any(msg in err for err in e.value.errors), (cond, e.value.errors)


@pytest.mark.parametrize("value,cond,expected", [
    ("9007199254740992", {"lt": 9007199254740993}, True),
    ("9007199254740993", {"eq": 9007199254740993}, True),
    ("9007199254740992", {"lt": {"var": "big"}}, True),
    ("0.3", {"eq": 0.3}, True),
    ("0.30000000000000004", {"gt": 0.3}, True),
    ("1e3", {"eq": 1000}, True),
    ("-0.5", {"lt": 0}, True),
])
def test_numbers_compare_exactly(server, value, cond, expected):
    server.load({"version": 1, "vars": {"x": value, "big": "9007199254740993"}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": {"var": {"name": "x", **cond}}, "steps": [{"do": "reply", "text": "yes"}]}],
         "else": [{"do": "reply", "text": "no"}]}]}]})
    assert text_of(Convo(server).user("go")) == ("yes" if expected else "no")


@pytest.mark.parametrize("value", ["1_000", "0x10", "NaN", "inf", "1.2.3", ""])
def test_text_that_only_looks_numeric_is_not_a_number(server, value):
    server.load({"version": 1, "vars": {"x": value or " "}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": {"var": {"name": "x", "lt": 5}}, "steps": []}]}], "final": "x"}]})
    assert text_of(Convo(server).user("go")) == FALLBACK and codes(server) == ["not_a_number"]


def test_huge_numbers_are_bounded_without_conversion():
    for raw, where in (({"final_delay": 10 ** 400}, "$.handlers[0].final_delay"),):
        with pytest.raises(ScriptError) as e:
            load_script({"version": 1, "handlers": [{"steps": [], "final": "x", **raw}]})
        assert e.value.errors[0].startswith(where) and "at most" in e.value.errors[0]
    # A huge integer is a number for a comparison, compared exactly.
    load_script({"version": 1, "vars": {"x": "1"}, "handlers": [{"steps": [
        {"do": "branch", "cases": [{"when": {"var": {"name": "x", "lt": 10 ** 400}}, "steps": []}]}], "final": "x"}]})
