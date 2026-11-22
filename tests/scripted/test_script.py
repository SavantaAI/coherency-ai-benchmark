"""Script validation: every problem is reported with its JSON path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cohbench.scripted import ScriptError, load_script
from cohbench.scripted.script import STEP_TYPES

README = Path(__file__).resolve().parents[2] / "cohbench" / "scripted" / "README.md"
SCHEMA = Path(__file__).resolve().parents[2] / "cohbench" / "scripted" / "schema.json"


def errors(raw) -> list[str]:
    with pytest.raises(ScriptError) as e:
        load_script(raw)
    return e.value.errors


def test_minimal_script_loads():
    s = load_script({"version": 1, "handlers": [{"steps": [], "final": "hi"}]})
    assert len(s.handlers) == 1 and s.handlers[0].end == "statement" and s.handlers[0].on_steer == "finish"


def test_all_problems_are_listed_with_paths():
    errs = errors({
        "version": 2,
        "extra": True,
        "handlers": [
            {"steps": [{"do": "bash"}, {"do": "fly"}, {"do": "read", "path": "x", "offset": "1"}], "final": "x"},
            {"steps": [{"do": "bash", "command": "ls", "capture": [{"var": "v", "regex": "("}]}]},
            {"steps": [], "final": "{{undefined_var}}", "end": "maybe", "on_steer": "panic"},
            {"steps": [{"do": "delegate", "child": "nobody", "task": "t"}], "final": "x"},
            {"steps": [{"do": "branch", "cases": [{"when": {"exitcode": 1}, "steps": []}]}], "final": "x"},
            {"steps": [{"do": "parallel", "calls": [{"do": "say", "text": "x"}]}], "final": "x"},
        ],
    })
    expected = [
        "$.extra: unknown key",
        "$.version: expected 1",
        "$.handlers[0].steps[0]: missing required key 'command'",
        "$.handlers[0].steps[1].do: expected one of",
        "$.handlers[0].steps[2].offset: expected an integer or null",
        "$.handlers[1].steps[0].capture[0].regex: regular expression does not compile",
        "$.handlers[1]: needs `final`",
        "$.handlers[2].end: expected one of statement, question",
        "$.handlers[2].on_steer: expected one of finish, abandon",
        "$.handlers[3].steps[0].child: no child named 'nobody'",
        "$.handlers[4].steps[0].cases[0].when.exitcode: unknown condition",
        "$.handlers[5].steps[0].calls[0].do: a parallel call must be a tool step",
        "$.handlers[2].final: variable 'undefined_var' is never set",
    ]
    for want in expected:
        assert any(e.startswith(want) for e in errs), (want, errs)


def test_not_json():
    assert errors("{")[0].startswith("$: not valid JSON")


def test_variables_may_come_from_vars_set_or_capture():
    load_script({"version": 1, "vars": {"who": "you"}, "handlers": [{"steps": [
        {"do": "set", "var": "greeting", "value": "hello {{who}}"},
        {"do": "bash", "command": "echo {{greeting}}", "capture": [{"var": "echoed", "regex": ".+"}]}],
        "final": "{{echoed}} {{last_output}} {{last_exit}}"}]})


def test_builtins_cannot_be_overwritten():
    errs = errors({"version": 1, "handlers": [{"steps": [{"do": "set", "var": "last_output", "value": "x"}], "final": "x"}]})
    assert errs[0].startswith("$.handlers[0].steps[0].var")


def test_capture_group_must_exist():
    errs = errors({"version": 1, "handlers": [{"steps": [
        {"do": "bash", "command": "x", "capture": [{"var": "a", "regex": "(b)", "group": 2}]}], "final": "x"}]})
    assert errs == ["$.handlers[0].steps[0].capture[0].group: the pattern has 1 group(s)"]


def test_fallback_must_be_plain_text():
    assert errors({"version": 1, "fallback": "{{x}}", "handlers": [{"steps": [], "final": "x"}]})[0].startswith("$.fallback")


def test_final_may_be_left_out_when_every_path_replies():
    load_script({"version": 1, "handlers": [{"steps": [{"do": "branch", "cases": [
        {"when": {"exit": 0}, "steps": [{"do": "reply", "text": "a"}]}], "else": [{"do": "reply", "text": "b"}]}]}]})
    assert errors({"version": 1, "handlers": [{"steps": [{"do": "branch", "cases": [
        {"when": {"exit": 0}, "steps": [{"do": "reply", "text": "a"}]}]}]}]})[0].startswith("$.handlers[0]: needs `final`")


def test_readme_example_and_schema_agree_with_the_validator():
    text = README.read_text()
    start = text.index("```json\n", text.index("## Worked example")) + len("```json\n")
    example = json.loads(text[start:text.index("```", start)])
    s = load_script(example)
    assert s.children, "the worked example includes a child"
    schema = json.loads(SCHEMA.read_text())
    assert set(schema["$defs"]["step"]["properties"]["do"]["enum"]) == set(STEP_TYPES)


@pytest.mark.parametrize("steps", [
    [{"do": "repeat", "max": 1, "steps": [{"do": "reply", "text": "r"}]}],
    [{"do": "reply", "text": "r"}, {"do": "say", "text": "unreachable"}],
    [{"do": "say", "text": "s"}, {"do": "branch", "cases": [{"when": {"exit": 0}, "steps": [
        {"do": "repeat", "max": 3, "steps": [{"do": "bash", "command": "x"}, {"do": "reply", "text": "a"}]}]}],
        "else": [{"do": "reply", "text": "b"}]}, {"do": "bash", "command": "never"}],
])
def test_handlers_that_always_reply_need_no_final(steps):
    load_script({"version": 1, "handlers": [{"steps": steps}]})


@pytest.mark.parametrize("steps", [
    [],
    [{"do": "branch", "cases": [{"when": {"exit": 0}, "steps": [{"do": "reply", "text": "a"}]}]}],
    [{"do": "repeat", "max": 2, "steps": [{"do": "branch", "cases": [{"when": {"exit": 0}, "steps": [
        {"do": "reply", "text": "a"}]}]}]}],
])
def test_handlers_that_can_run_out_of_steps_need_final(steps):
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": steps}]})
    assert e.value.errors[0].startswith("$.handlers[0]: needs `final`")


@pytest.mark.parametrize("raw,where", [
    ({"version": 1, "handlers": [{"steps": [], "final": None}]}, "$.handlers[0].final"),
    ({"version": 1, "handlers": [{"steps": [], "final": "x", "id": None}]}, "$.handlers[0].id"),
    ({"version": 1, "description": None, "handlers": [{"steps": [], "final": "x"}]}, "$.description"),
])
def test_null_is_not_a_string(raw, where):
    with pytest.raises(ScriptError) as e:
        load_script(raw)
    assert any(err.startswith(where) for err in e.value.errors), e.value.errors
