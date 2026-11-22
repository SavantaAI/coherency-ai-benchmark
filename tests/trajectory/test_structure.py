"""Segments, subagents, the SQLite reader and the cross-check against the
harness session."""

from __future__ import annotations

import copy
import json
import sqlite3

from cohbench.trajectory import Hints, RunnerEvent, Segment, t3, validate
from cohbench.trajectory.adapters import PiProfile

from . import mini
from .conftest import FIX, events, export_segment, load, run, sessions, snapshot_segment


def handoff(declared: bool = False):
    pi = PiProfile()
    segs = t3.from_joined(load("pi-handoff-joined.json"))
    hints = None
    if declared:
        tid = next(iter(segs[1]))
        first = next(i for i in segs[1][tid].items if i["type"] == "user_message")
        hints = Hints(runner=[RunnerEvent("controlled_reset", first["startedAt"], {"thread": tid})])
    return run([Segment(segs[0], None, sessions(pi, "pi-handoff-1")), Segment(segs[1], None, sessions(pi, "pi-handoff-2"))], hints=hints)


# Multi-segment episodes ---------------------------------------------------------


def test_handoff_gives_two_segments_in_one_file():
    res = handoff()
    assert validate(res.trajectory).ok
    starts = events(res, "segment_start")
    assert [(s["segment"], s["cause"]) for s in starts] == [(1, "start"), (2, "context")]
    first2 = next(e for e in res.trajectory.events if e["segment"] == 2 and e["type"] == "user_message")
    assert first2["kind"] == "reply"
    last1 = max(e["t"] for e in res.trajectory.events if e["segment"] == 1)
    assert starts[1]["t"] - last1 > 20_000  # the handoff's time between containers is kept
    assert res.manifest["segments"]["1"]["thread"] != res.manifest["segments"]["2"]["thread"]
    assert res.manifest["crosscheck"] == []
    assert [e["turn"] for e in events(res, "turn_start")] == [1, 2, 3, 4, 5, 6]


def test_declared_handoff_is_a_controlled_reset():
    res = handoff(declared=True)
    rep = validate(res.trajectory)
    assert rep.ok, rep.errors
    assert [s["cause"] for s in events(res, "segment_start")] == ["start", "reset"]
    first2 = next(e for e in res.trajectory.events if e["segment"] == 2 and e["type"] == "user_message")
    assert first2["kind"] == "reset"
    cr = events(res, "controlled_reset")[0]
    assert cr["target"] == first2["seq"]


def test_fresh_context_without_a_declaration_is_a_context_boundary():
    three = run([export_segment("pi-export-three-turns.json")])
    assert three.trajectory.header["segments"] == 2
    assert [s["cause"] for s in events(three, "segment_start")] == ["start", "context"]
    assert [e["kind"] for e in events(three, "user_message") if e["segment"] == 2] == ["reply"]


# Subagents ------------------------------------------------------------------------


def test_subagent_events_nest_under_a_local_id():
    res = run([export_segment("pi-subagent.json", "pi-subagent")])
    start = events(res, "subagent_start")[0]
    assert start["subagent"] == 1 and start["agent"] == 0
    assert start["prompt"].startswith("In the project at /work/project")
    child = [e for e in res.trajectory.events if e["agent"] == 1]
    assert [e["type"] for e in child] == ["turn_start", "user_message", "tool_call", "assistant_message", "turn_end"]
    assert child[1]["kind"] == "from_agent"
    end = events(res, "subagent_end")[0]
    assert end == {**end, "subagent": 1, "status": "ok", "result": "1", "agent": 0}
    assert res.manifest["crosscheck"] == []


def test_subagent_without_its_child_thread_keeps_start_and_end():
    obj = load("pi-subagent.json")
    obj["threads"] = obj["threads"][:1]
    res = run([export_segment("pi-subagent.json", obj=obj)])
    assert validate(res.trajectory).ok
    assert [e["agent"] for e in res.trajectory.events if e["agent"] != 0] == []
    assert any("not in the source" in n for n in res.manifest["notes"])


def test_threads_outside_the_episode_are_reported():
    a = load("pi-subagent.json")["threads"]
    b = load("pi-export-three-turns.json")["threads"]
    res = run([Segment(t3.from_export({"threads": a + b}), a[0]["thread"]["id"])])
    assert res.manifest["unvisited_threads"] == [b[0]["thread"]["id"]]


# The SQLite reader -------------------------------------------------------------------

SCHEMA = [
    "create table orchestration_v2_projection_threads (thread_id text primary key, created_at text, payload_json text)",
    "create table orchestration_v2_projection_runs (run_id text, thread_id text, payload_json text)",
    "create table orchestration_v2_projection_run_attempts (attempt_id text, thread_id text, attempt_ordinal integer, payload_json text)",
    "create table orchestration_v2_projection_messages (message_id text, thread_id text, created_at text, payload_json text)",
    "create table orchestration_v2_projection_subagents (subagent_id text, thread_id text, payload_json text)",
    "create table orchestration_v2_projection_runtime_requests (runtime_request_id text, thread_id text, created_at text, payload_json text)",
    "create table orchestration_v2_projection_turn_items (turn_item_id text, thread_id text, ordinal integer, payload_json text)",
    "create table orchestration_v2_turn_item_positions (thread_id text, turn_item_id text, ordinal integer)",
]


def test_sqlite_reader_matches_the_export(tmp_path):
    """T3's tables (the columns the reader uses, as in the saved backups),
    filled from the export fixture, with positions in reverse insertion order
    so that order comes from the positions table."""
    obj = load("pi-subagent.json")
    db = tmp_path / "state.sqlite"
    con = sqlite3.connect(db)
    for s in SCHEMA:
        con.execute(s)
    for n, th in enumerate(obj["threads"]):
        tid = th["thread"]["id"]
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?)", (tid, f"2026-11-21T00:00:0{n}Z", json.dumps(th["thread"])))
        for r in th["runs"]:
            con.execute("insert into orchestration_v2_projection_runs values (?,?,?)", (r["id"], tid, json.dumps(r)))
        for a in th["attempts"]:
            con.execute("insert into orchestration_v2_projection_run_attempts values (?,?,?,?)", (a["id"], tid, a["attemptOrdinal"], json.dumps(a)))
        for s in th["subagents"]:
            con.execute("insert into orchestration_v2_projection_subagents values (?,?,?)", (s["id"], tid, json.dumps(s)))
        for i in reversed(th["items"]):
            con.execute("insert into orchestration_v2_projection_turn_items values (?,?,?,?)", (i["id"], tid, i["ordinal"], json.dumps(i)))
            con.execute("insert into orchestration_v2_turn_item_positions values (?,?,?)", (tid, i["id"], i["ordinal"]))
    con.commit()
    con.close()
    from_db = t3.from_sqlite(db)
    from_json = t3.from_export(obj)
    assert set(from_db) == set(from_json)
    for tid in from_db:
        assert [i["id"] for i in from_db[tid].items] == [i["id"] for i in from_json[tid].items]
    pi = PiProfile()
    a = run([Segment(from_db, None, sessions(pi, "pi-subagent"))])
    b = run([Segment(from_json, None, sessions(pi, "pi-subagent"))])
    assert a.trajectory.dumps() == b.trajectory.dumps()
    assert a.manifest["sources"][0]["threads"] != b.manifest["sources"][0]["threads"]


# Cross-check against the harness session ----------------------------------------


def test_fixtures_agree_with_their_harness_sessions():
    for res in (
        run([export_segment("pi-export-three-turns.json", "pi-export")]),
        run([export_segment("pi-subagent.json", "pi-subagent")]),
        handoff(),
        run([snapshot_segment("pi-pending-diagnostic-snapshot.json", "pi-pending-diagnostic")]),
        run([snapshot_segment("pi-pending-secret-snapshot.json", "pi-pending-secret")]),
        run([snapshot_segment("pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json")]),
    ):
        assert res.manifest["crosscheck"] == []


def test_crosscheck_reports_disagreements(tmp_path):
    pi = PiProfile()
    sess = sessions(pi, "pi-export")
    tampered = copy.deepcopy(sess)
    s = next(x for x in tampered if x.results)
    s.user_texts[0] = s.user_texts[0] + " (changed)"
    cid = next(iter(s.results))
    s.results[cid]["text"] = "different"
    s.calls["toolu_extra"] = {"name": "bash", "args": {"command": "ls"}, "order": 99}
    seg = export_segment("pi-export-three-turns.json")
    seg.sessions = tampered
    res = run([seg])
    found = {f["finding"] for f in res.manifest["crosscheck"]}
    assert {"user messages differ", "tool output differs", "native tool calls with no T3 item"} <= found
    # The trajectory still comes from T3's record.
    assert not any("(changed)" in e.get("text", "") for e in res.trajectory.events)


def test_crosscheck_reports_a_file_write_cut_by_the_driver():
    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 5)
    r_items = [mini.user(r, 0, "write"), mini.item(r, "file_change", 1, 2, title="write", fileName="/work/project/big.txt", newStr="item 1\nite\n… output truncated for transport")]
    att = {"id": "a1", "runId": r["id"], "attemptOrdinal": 1, "nativeThreadId": "/home/node/.pi/agent/sessions/--work-project--/s.jsonl"}
    obj = mini.export([(th, [r], r_items)])
    obj["threads"][0]["attempts"] = [att]
    from cohbench.trajectory.adapters.base import NativeSession

    nid = r_items[1]["nativeItemRef"]["nativeId"]
    native = NativeSession(path="/x/s.jsonl", session_id="s", user_texts=["write"], calls={nid: {"name": "write", "args": {"path": "/work/project/big.txt", "content": "item 1\nitem 2\n"}, "order": 0}}, results={nid: {"text": "Successfully wrote", "is_error": False}})
    res = run([Segment(t3.from_export(obj), None, [native])])
    f = res.manifest["crosscheck"][0]
    assert f["finding"] == "file change text differs" and f["t3_cut_by_driver"] is True


def test_an_async_subagent_finishing_after_a_context_boundary():
    th = mini.thread("thread:project:p:parent")
    r1 = mini.run_rec(th["id"], 1, 0, 6)
    r2 = mini.run_rec(th["id"], 2, 10, 14, fresh=True)
    c = mini.thread("thread:child:1", parent=th["id"])
    cr = mini.run_rec(c["id"], 1, 2, 12)
    parent = [mini.user(r1, 0, "start it in the background"),
              mini.item(r1, "subagent", 2, 13, title="bg", prompt="work", result="ok", childThreadId=c["id"]),
              mini.assistant(r1, 5, "started"),
              mini.user(r2, 10, "anything new?"), mini.assistant(r2, 13.5, "still running")]
    child = [mini.user(cr, 2, "work", createdBy="agent"), mini.command(cr, 3, 11, "sleep 8", ""), mini.assistant(cr, 11.5, "ok")]
    res = run([Segment(t3.from_export(mini.export([(th, [r1, r2], parent), (c, [cr], child)])), th["id"])])
    rep = validate(res.trajectory)
    assert rep.ok, rep.errors
    end = next(e for e in res.trajectory.events if e["type"] == "subagent_end")
    assert end["segment"] == 2 and end["t"] == 13000


# An interrupt of a tree whose child had a pending question (a real capture) -------


def interrupt_capture(obj=None):
    return run([export_segment("pi-interrupt-child-question.json", "pi-interrupt-child-question", obj=obj)])


def test_the_drivers_interrupt_request_sits_in_its_run():
    """The driver interrupted the tree (a budget cutoff) while the child's
    question was pending. T3 recorded `run_interrupt_request` in the child's
    thread at a time between the question and the cancelled answer, when the
    child had no open turn. It is the driver's own request: audit only."""
    from cohbench.trajectory import scorer_view, validate_view

    res = interrupt_capture()
    rep = validate(res.trajectory)
    assert rep.ok and rep.unknown == 0, rep.errors
    evs = res.trajectory.events
    req = events(res, "interrupt_request")
    assert len(req) == 1 and req[0]["agent"] == 1 and req[0]["turn"] is None
    child = [e["type"] for e in evs if e["agent"] == 1]
    assert child == ["turn_start", "user_message", "tool_call", "question", "turn_end", "interrupt_request", "answer", "turn_start", "turn_end"]
    dropped = {d["type"]: d["reason"] for d in res.manifest["dropped"]}
    assert {"run_interrupt_result", "notification"} <= set(dropped)
    view = scorer_view(res.trajectory)
    assert validate_view(view).ok and not any(e["type"] == "interrupt_request" for e in view.events)


def test_an_unmapped_item_cannot_break_ordering():
    """An item of a type no mapping covers takes the place of its run's
    nearest event and is exempt from turn membership; the trajectory still
    fails unless unknown items are allowed."""
    obj = load("pi-interrupt-child-question.json")
    for th in obj["threads"]:
        for i in th["items"]:
            if i["type"] == "run_interrupt_request":
                i["type"] = "some_new_signal"
    res = interrupt_capture(obj)
    rep = validate(res.trajectory)
    assert rep.errors == [] and rep.unknown == 1 and not rep.ok
    assert validate(res.trajectory, allow_unknown=True).ok
    evs = res.trajectory.events
    u = next(e for e in evs if e["type"] == "unknown")
    assert u["item_type"] == "some_new_signal" and u["agent"] == 1
    # Just after the nearest event of the child's run: its question.
    assert evs[u["seq"] - 1]["type"] in ("question", "turn_end") and evs[u["seq"] - 1]["agent"] == 1
    assert [e["t"] for e in evs] == sorted(e["t"] for e in evs)
