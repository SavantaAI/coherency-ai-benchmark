"""The blindness check: structural tells and leftover decorations fail;
tells in agent, user or task text are counted and kept. The scorer view is
the strict gate."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from cohbench.trajectory import io, scorer_view
from cohbench.trajectory.blindness import scan, scan_view

from . import mini
from .conftest import events, export_segment, run, snapshot_segment
from .test_transformations import mini_run


@pytest.fixture(scope="module")
def clean():
    return run([export_segment("pi-subagent.json", "pi-subagent")])


def fixtures():
    return [
        run([export_segment("pi-subagent.json", "pi-subagent")]),
        run([export_segment("pi-export-three-turns.json", "pi-export")]),
        run([snapshot_segment("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic")]),
        run([snapshot_segment("pi-pending-secret-snapshot.json", "pi-pending-secret")]),
        run([snapshot_segment("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json")]),
    ]


def test_normalised_fixtures_are_blind_in_both_outputs():
    for res in fixtures():
        rep = scan(res.trajectory)
        assert rep.ok, (rep.structural, rep.decorations)
        vrep = scan_view(scorer_view(res.trajectory))
        assert vrep.ok and not vrep.structural
        assert res.manifest["blindness"]["ok"] and res.manifest["blindness_scorer_view"]["ok"]


@pytest.mark.parametrize("where", ["error_class", "tool_name", "item_type", "header"])
def test_structural_tells_fail(clean, where):
    t = copy.deepcopy(clean.trajectory)
    if where == "header":
        t.header["format"] = "cohbench-trajectory-pi"
    elif where == "error_class":
        t.events.append({"seq": len(t.events), "t": t.events[-1]["t"], "type": "error", "agent": 0, "segment": 1, "turn": None, "class": "claude_refusal"})
    elif where == "tool_name":
        next(e for e in t.events if e["type"] == "tool_call")["tool"] = "bash"
    else:
        t.events.append({"seq": len(t.events), "t": t.events[-1]["t"], "type": "unknown", "agent": 0, "segment": 1, "turn": None, "item_type": "provider:pi:thing"})
    rep = scan(t)
    assert not rep.ok and rep.structural


@pytest.mark.parametrize(
    "leftover",
    [
        "row 3000\n\n[Showing lines 1957-3001 of 3001 (50.0KB limit). Full output: /tmp/pi-bash-09af1e3a.log]",
        "<persisted-output>\nOutput too large (49.0KB). Full output saved to: /x\n\nPreview (first 2KB):\nrow\n</persisted-output>",
        "row 1\n\n... [12 lines truncated] ...",
        "text\n<system-reminder>\nnote\n</system-reminder>",
        "a\n… output truncated for transport",
    ],
)
def test_decorations_left_in_tool_output_fail(clean, leftover):
    t = copy.deepcopy(clean.trajectory)
    next(e for e in t.events if e["type"] == "tool_call" and e["output"]["available"])["output"]["text"] = leftover
    rep = scan(t)
    assert not rep.ok and rep.decorations and not rep.structural


def test_a_decoration_equal_to_the_command_is_still_caught(clean):
    t = copy.deepcopy(clean.trajectory)
    call = next(e for e in t.events if e["type"] == "tool_call" and e["tool"] == "shell")
    call["input"]["command"] = call["output"]["text"] = "x\n<system-reminder>y</system-reminder>"
    assert scan(t).decorations


def test_leftover_sender_note_and_role_prefix_fail(clean):
    t = copy.deepcopy(clean.trajectory)
    next(e for e in t.events if e["type"] == "user_message")["text"] = "[T3 Code: the agent in T3 thread x sent this message, not the user.]\n\nhello"
    assert [h.tell for h in scan(t).decorations] == ["T3 sender note"]
    t = copy.deepcopy(clean.trajectory)
    next(e for e in t.events if e["type"] == "subagent_start")["prompt"] = "Act as the research sub-agent for this task.\n\nCount."
    assert [h.tell for h in scan(t).decorations] == ["T3 role prefix"]


def test_transport_suffix_in_file_content_fails():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.item(r, "file_change", 1, 2, title="write", fileName="/work/project/a", newStr="abc")])
    t = copy.deepcopy(res.trajectory)
    next(e for e in t.events if e["type"] == "tool_call")["input"]["content"] = "abc\n… output truncated for transport"
    assert [h.tell for h in scan(t).decorations] == ["T3 transport suffix"]


def test_the_same_text_in_agent_content_is_counted_not_failed(clean):
    t = copy.deepcopy(clean.trajectory)
    next(e for e in t.events if e["type"] == "assistant_message")["text"] = "Full output: /tmp/pi-bash-09af1e3a.log] and I am Claude Code"
    rep = scan(t)
    assert rep.ok
    assert rep.content["Pi spill file"] == 1 and rep.content["product: Claude Code"] == 1


def test_agent_naming_its_harness_is_kept_and_counted():
    res = mini_run(lambda r: [mini.user(r, 0, "who are you"), mini.assistant(r, 1, "I am running in T3 Code through the Claude Code harness, model claude-haiku-5-5.")])
    assert events(res, "assistant_message")[0]["text"].startswith("I am running in T3 Code through the Claude Code harness")
    for key in ("blindness", "blindness_scorer_view"):
        b = res.manifest[key]
        assert b["ok"] is True
        assert b["content_by_tell"] == {"driver: T3 Code": 1, "model id": 1, "product: Claude Code": 1}


def test_scorer_view_has_no_structural_fields_to_leak_through():
    """The view of the subagent episode drops the tool calls whose arguments
    and the narration whose text named the harness."""
    res = run([export_segment("pi-subagent.json", "pi-subagent")])
    assert res.manifest["blindness"]["content_by_tell"] == {"product: Pi": 1}
    assert res.manifest["blindness_scorer_view"]["content_total"] == 0
    v = scorer_view(res.trajectory)
    assert {e["type"] for e in v.events} == {"turn_start", "user_message", "to_user", "turn_end"}


def test_agent_copying_native_ids_into_arguments_is_counted():
    res = mini_run(lambda r: [mini.user(r, 0, "x"), mini.dynamic(r, 1, 2, "mcp__t3-code__t3_thread_read", {"threadId": "thread:project:abc:def"}, None)])
    assert res.manifest["blindness"]["ok"]
    assert res.manifest["blindness"]["content_by_tell"]["T3 id"] == 1


def test_cli(clean, tmp_path):
    p = tmp_path / "e.jsonl"
    io.write(clean.trajectory, p)
    assert subprocess.run([sys.executable, "-m", "cohbench.trajectory", "blindness", str(p)], capture_output=True).returncode == 0
    io.write(scorer_view(clean.trajectory), tmp_path / "v.jsonl")
    assert subprocess.run([sys.executable, "-m", "cohbench.trajectory", "blindness", str(tmp_path / "v.jsonl")], capture_output=True).returncode == 0
    t = copy.deepcopy(clean.trajectory)
    next(e for e in t.events if e["type"] == "tool_call" and e["output"]["available"])["output"]["text"] = "x\n<system-reminder>y</system-reminder>"
    io.write(t, p)
    assert subprocess.run([sys.executable, "-m", "cohbench.trajectory", "blindness", str(p)], capture_output=True).returncode == 1
