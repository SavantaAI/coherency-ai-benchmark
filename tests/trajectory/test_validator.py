"""The format validator: schema, order and references, failing closed."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from cohbench.trajectory import io, scorer_view, validate, validate_view

from .conftest import FIX, export_segment, run, snapshot_segment


@pytest.fixture(scope="module")
def good():
    return run([export_segment("pi-subagent.json", "pi-subagent")]).trajectory


@pytest.fixture(scope="module")
def pending():
    return run([snapshot_segment("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic")]).trajectory


def mutate(traj, fn):
    t = copy.deepcopy(traj)
    fn(t)
    return t


def first(t, etype, **match):
    return next(e for e in t.events if e["type"] == etype and all(e.get(k) == v for k, v in match.items()))


def shift_all(t, ms):
    for e in t.events:
        e["t"] += ms
        if e.get("t_end") is not None:
            e["t_end"] += ms
    t.header["duration_ms"] += ms


def test_normalised_fixture_is_valid(good, pending):
    for t in (good, pending):
        rep = validate(t)
        assert rep.ok, rep.errors
        assert rep.unknown == 0


def test_round_trip_through_text(good, tmp_path):
    p = tmp_path / "e.jsonl"
    io.write(good, p)
    back = io.read(p)
    assert back.dumps() == good.dumps()
    assert p.read_text().count("\n") == len(good.events) + 1


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda t: first(t, "tool_call").__setitem__("origin", "driver"), "undeclared field"),
        (lambda t: first(t, "tool_call")["input"].__setitem__("timeout", 10), "undeclared field"),
        (lambda t: first(t, "tool_call")["output"].pop("available"), "missing"),
        (lambda t: first(t, "tool_call").__setitem__("tool", "bash"), "is not one of"),
        (lambda t: first(t, "tool_call").__setitem__("class", "shell"), "is class other"),
        (lambda t: first(t, "user_message").__setitem__("kind", "system"), "is not one of"),
        (lambda t: t.events[3].__setitem__("seq", 99), "expected 3"),
        (lambda t: (t.events[5].__setitem__("us", 0), t.events[5].__setitem__("t", 0)), "before the previous"),
        (lambda t: shift_all(t, 10), "is not"),
        (lambda t: t.events.pop(1), "not agent 0's open turn"),
        (lambda t: first(t, "turn_end").__setitem__("turn", 7), "not agent"),
        (lambda t: first(t, "subagent_start").__setitem__("call", 1), "does not link subagent"),
        (lambda t: first(t, "tool_call", tool="subagent").__setitem__("subagent", 5), "does not name the call"),
        (lambda t: first(t, "subagent_start").__setitem__("call", None), "does not name the call"),
        (lambda t: first(t, "assistant_message", role="final", agent=0).__setitem__("role", "narration"), "final is exactly"),
        (lambda t: first(t, "assistant_message").__setitem__("user_channel", True), "only agent 0's final"),
        (lambda t: first(t, "tool_call", tool="shell")["output"].__setitem__("text", None), "available exactly when"),
        (lambda t: first(t, "tool_call", tool="shell")["output"].__setitem__("truncated", {"by": "harness", "lines_omitted": True}), "does not have type bool"),
        (lambda t: first(t, "subagent_end").__setitem__("status", "open"), "is not one of"),
        (lambda t: first(t, "user_message").__setitem__("type", ["user_message"]), "unknown event type"),
        (lambda t: first(t, "user_message").__setitem__("turn", [1]), "does not have type int"),
        (lambda t: t.header.__setitem__("events", 3), "header: events"),
        (lambda t: t.header.__setitem__("agents", 1), "header: agents"),
        (lambda t: t.events[0].__setitem__("cause", "reset"), "only segment 1 is the start"),
        (lambda t: first(t, "user_message", agent=1).__setitem__("kind", "reply"), "kind from_agent"),
    ],
)
def test_each_rule_fails_closed(good, change, message):
    rep = validate(mutate(good, change))
    assert not rep.ok
    assert any(message in e for e in rep.errors), rep.errors


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda t: first(t, "tool_call", tool="ask_user").__setitem__("question", 2), "does not link it back"),
        (lambda t: first(t, "question").__setitem__("call", 9), "not an earlier tool_call"),
        (lambda t: first(t, "question").__setitem__("prompts", None), "a question has prompts"),
        (lambda t: first(t, "answer").__setitem__("status", "cancelled"), "only an answered question, or a refused secret"),
    ],
)
def test_question_links_fail_closed(pending, change, message):
    rep = validate(mutate(pending, change))
    assert any(message in e for e in rep.errors), rep.errors


def test_a_failed_send_to_user_cannot_be_user_channel():
    from . import mini
    from .test_transformations import mini_run

    res = mini_run(lambda r: [mini.user(r, 0, "x"), {**mini.dynamic(r, 1, 2, "mcp__t3-code__send_to_user", {"message": "hi"}, None), "status": "failed"}])
    t = mutate(res.trajectory, lambda t: (first(t, "tool_call").__setitem__("user_channel", True), first(t, "tool_call").__setitem__("user_text", "hi")))
    assert any("successful send_to_user" in e for e in validate(t).errors)


def test_unknown_events_are_counted_and_fail_unless_allowed(good):
    def add_unknown(t):
        e = {"seq": 0, "t": t.events[3]["t"], "us": t.events[3]["us"], "type": "unknown", "agent": 0, "segment": 1, "turn": 1, "item_type": "reasoning"}
        t.events.insert(3, e)
        for n, x in enumerate(t.events):
            x["seq"] = n
        t.header["events"] += 1

    t = mutate(good, add_unknown)
    rep = validate(t)
    assert rep.unknown == 1 and not rep.ok and not rep.errors
    assert validate(t, allow_unknown=True).ok


def test_answer_rules(good):
    t = copy.deepcopy(good)
    t.events.append({"seq": len(t.events), "t": t.events[-1]["t"], "us": t.events[-1]["us"], "type": "answer", "agent": 0, "segment": 1, "question": 1, "status": "answered", "text": "x", "missing": False, "kind": "reply"})
    t.header["events"] += 1
    assert any("not asked earlier" in e for e in validate(t).errors)


def test_scorer_view_validator(good, pending):
    for t in (good, pending):
        v = scorer_view(t)
        assert validate_view(v).ok, validate_view(v).errors
    v = scorer_view(pending)
    bad = mutate(v, lambda v: v.events[1].__setitem__("tool", "shell"))
    assert any("undeclared" in e for e in validate_view(bad).errors)
    assert any("time 0" in e for e in validate_view(mutate(v, lambda v: v.events[0].__setitem__("t", -1))).errors)
    assert any("shorter" in e for e in validate_view(mutate(v, lambda v: v.header.__setitem__("duration_ms", 1))).errors)
    bad = mutate(v, lambda v: next(e for e in v.events if e["type"] == "user_message").__setitem__("secret", True))
    assert any("secret request carries no text" in e for e in validate_view(bad).errors)


def test_runner_wait_rules(good):
    t = copy.deepcopy(good)
    t.events.append({"seq": len(t.events), "t": t.events[-1]["t"], "us": t.events[-1]["us"], "type": "wait", "agent": 0, "segment": 1, "target": None, "t_end": 0, "us_end": 0,
                     "charged_ms": -5, "ended": "reply", "causes": ["fixed_message"], "replies_to": []})
    t.header["events"] += 1
    errs = validate(t).errors
    assert any("ends before it starts" in e for e in errs) and any("negative charge" in e for e in errs)


def test_a_question_needs_prompts(pending):
    assert any("has prompts" in e for e in validate(mutate(pending, lambda t: first(t, "question").__setitem__("prompts", []))).errors)


def test_no_runs_is_a_normalise_error():
    from cohbench.trajectory import NormaliseError, Segment, t3

    from . import mini

    th = mini.thread()
    with pytest.raises(NormaliseError, match="nothing to normalise"):
        run([Segment(t3.from_export(mini.export([(th, [], [])])))])


def test_cli_exit_codes(good, tmp_path):
    p = tmp_path / "e.jsonl"
    io.write(good, p)
    ok = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "validate", str(p)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    io.write(scorer_view(good), tmp_path / "v.jsonl")
    assert subprocess.run([sys.executable, "-m", "cohbench.trajectory", "validate", str(tmp_path / "v.jsonl")], capture_output=True).returncode == 0
    io.write(mutate(good, lambda t: t.events[3].__setitem__("seq", 99)), p)
    assert subprocess.run([sys.executable, "-m", "cohbench.trajectory", "validate", str(p)], capture_output=True).returncode == 1
    blind = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "blindness", str(FIX / "pi-subagent.json")], capture_output=True, text=True)
    assert blind.returncode != 0  # not a trajectory: the reader refuses it


def test_excluded_intervals_and_missing_answers(good, pending):
    t = mutate(good, lambda t: t.header.__setitem__("excluded", [{"us": 1, "us_end": t.events[-1]["us"], "reason": "probe"}]))
    assert any("inside an excluded interval" in e for e in validate(t).errors)
    t = mutate(good, lambda t: t.header.__setitem__("excluded", [{"us": 5, "us_end": 2, "reason": "probe"}]))
    assert any("excluded" in e for e in validate(t).errors)
    t = mutate(pending, lambda t: first(t, "answer").__setitem__("missing", True))
    assert any("missing marks" in e for e in validate(t).errors)
