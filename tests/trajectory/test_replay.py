"""The rebuild command (cohbench.trajectory.replay) on a synthetic raw
record: an episode whose pending question's answer is known only from the
runner's send log (the native tool output was not kept), written, then
verified against itself (J4 of round 10)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cohbench.trajectory import bundle, replay

from . import mini
from .test_structure import SCHEMA


def _sqlite(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for s in SCHEMA:
        con.execute(s)
    for n, th in enumerate(obj["threads"]):
        tid = th["thread"]["id"]
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?)", (tid, f"2026-11-21T00:00:0{n}Z", json.dumps(th["thread"])))
        for r in th["runs"]:
            con.execute("insert into orchestration_v2_projection_runs values (?,?,?)", (r["id"], tid, json.dumps(r)))
        for i in th["items"]:
            con.execute("insert into orchestration_v2_projection_turn_items values (?,?,?,?)", (i["id"], tid, i["ordinal"], json.dumps(i)))
            con.execute("insert into orchestration_v2_turn_item_positions values (?,?,?)", (tid, i["id"], i["ordinal"]))
    con.commit()
    con.close()


def _ev(type_, t, ref=None, fields=None, end=None):
    return {"type": type_, "utc": mini.ts(t), "ref": ref, "fields": fields or {}, "end_utc": mini.ts(end) if end is not None else None}


def raw_record(tmp_path: Path, *, with_answer_send: bool = True, harness: str = "pi", driver_sent: bool = False) -> Path:
    run_dir = tmp_path / "run"
    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 11)
    opening = mini.user(r, 0, "Pick the faster mode, then run it.")
    ask = mini.dynamic(r, 2, 6, "ask_user", {"question": "Fast or safe?", "options": ["fast", "safe"]}, None)
    q = mini.item(r, "user_input_request", 2.1, 6, requestId="req-1", questions=[{"id": "q", "question": "Fast or safe?", "options": [{"label": "fast"}, {"label": "safe"}]}])
    q["providerTurnId"] = ask["providerTurnId"] = "pt-q"
    final = mini.assistant(r, 10, "Ran it in fast mode.")
    _sqlite(mini.export([(th, [r], [opening, ask, q, mini.command(r, 7, 8, "./run --fast", "ok\n"), final])]), run_dir / "export" / "segment-1" / "t3-state.sqlite")
    (run_dir / "export" / "segment-1" / "sessions").mkdir()
    events = [_ev("verdict", 2.1, {"item": q["id"]}, {"label": "answerable", "reply": "fast"}),
              _ev("wait", 2.1, {"item": q["id"]}, {"charged_ms": 2000, "ended": "reply", "causes": ["verdict_reply"], "replies_to": [{"item": q["id"]}]}, 6),
              _ev("handback", 2.2, {"item": q["id"]}, {"kind": "pending"}),
              _ev("verdict", 10.1, {"item": final["id"]}, {"label": "no_request", "reply": None}),
              _ev("handback", 10.2, {"item": final["id"]}, {"kind": "finished"}),
              _ev("episode_end", 11, None, {"reason": "finished", "stopped_utc": None})]
    manifest = {"episode_start_utc": mini.ts(0), "harness": harness, "segments": [{"segment": 1, "thread_id": th["id"]}], "runner_events": events,
                "clock": {"excluded": []}, "end_mark": "end-1"}
    if driver_sent:
        # The driver's own record of what it sent (the live export seeds from it).
        manifest["sent"] = [{"kind": "opening", "message_id": opening["messageId"], "status": "accepted", "text": opening["text"]},
                            {"kind": "answer", "message_id": None, "request_id": q["id"], "rpc_request_id": "req-1", "status": "accepted", "text": "fast"}]
    (run_dir / "export" / "manifest.json").write_text(json.dumps(manifest))
    wdir = run_dir / "export" / "watcher" / "segment-1"
    wdir.mkdir(parents=True)
    (wdir / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
        {"seq": 1, "wall": mini.ts(0), "kind": "created", "path": "run", "batch": 0, "type": "file", "mode": "0755", "sha256": None, "content": "none"},
        {"seq": 2, "wall": mini.ts(0.01), "kind": "batch", "batch": 0},
        {"seq": 3, "wall": mini.ts(10.6), "kind": "mark", "label": "end-1", "batch": 0, "at": mini.ts(10.5), "straddled": None, "accepted": True}]))
    (wdir / "heartbeat.json").write_text(json.dumps({"status": "stopped"}))
    sends = [{"type": "send", "kind": "opening_prompt", "via": "start", "text": opening["text"], "ref": {"message": opening["messageId"]}, "message": opening["messageId"], "request": None}]
    if with_answer_send:
        # The answer to the pending question: no message id; by its item, with the request id.
        sends.append({"type": "send", "kind": "reply", "via": "answer", "text": "fast", "ref": {"item": q["id"]}, "message": None, "request": "req-1"})
    (run_dir / "runner").mkdir()
    (run_dir / "runner" / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in sends))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(json.dumps({"scoring": {"identifiers": {}, "observations": {"attempts": []}}}))
    (run_dir / "run.json").write_text(json.dumps({"probes": [], "task": {"dir": str(task_dir)}}))
    record = {"format": "cohbench-episode", "version": 1, "episode": "ep-1",
              "grouping": {"family": "routing", "seed": 3, "pair": None, "sibling": None, "control": False, "horizon": "h", "mode": "default"},
              "budget": {"limits": {"elapsed_ms": 60000, "tokens": None, "user_wait_ms": None}, "consumed": {"elapsed_ms": 11000, "user_wait_ms": 2000}},
              "planted": [], "answer_key": {"family": "routing", "payload": {"conflict": False, "settled_by_workspace": False, "expected_decision": None, "criterion": None}},
              "control": None, "probes": [], "awareness": {"asked": False, "reply": None, "outcome": None}, "end": {"reason": "finished", "duration_ms": 11000}, "poll_s": 2.0, "task_sha256": "cd" * 32}
    (run_dir / "episode-record.json").write_text(json.dumps(record))
    return run_dir


def test_a_pending_answer_known_only_from_the_send_log_rebuilds_and_verifies(tmp_path):
    run_dir = raw_record(tmp_path)
    res, b = replay.rebuild(run_dir)
    reply = next(e for e in b.view.events if e["type"] == "user_message" and e["kind"] == "reply")
    assert reply["text"] == "fast"
    assert bundle.validate(b)["ok"]
    bundle.write(b, run_dir / "export" / "bundle")
    out = replay.verify(run_dir)
    assert out == {"ok": True, "differs": [], "validation": {}}
    # The same check from the command line.
    cli = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "verify-episode", str(run_dir)], capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    # A stored file edited afterwards is named.
    (run_dir / "export" / "bundle" / "identifiers.json").write_text('{"format": "x"}')
    assert replay.verify(run_dir)["differs"] == ["identifiers.json"]


def test_a_record_from_before_the_task_digest_rebuilds_without_it(tmp_path):
    """D-046: a raw record from before the runner carried `task_sha256`
    rebuilds into a bundle without the key, as the bundle made then was, and
    it validates on readback; a current record keeps its digest."""
    run_dir = raw_record(tmp_path)
    assert replay.rebuild(run_dir)[1].episode["task_sha256"] == "cd" * 32
    rec = json.loads((run_dir / "episode-record.json").read_text())
    del rec["task_sha256"]
    (run_dir / "episode-record.json").write_text(json.dumps(rec))
    _, b = replay.rebuild(run_dir)
    assert "task_sha256" not in b.episode and bundle.validate(b)["ok"]
    bundle.write(b, run_dir / "export" / "bundle")
    assert "task_sha256" not in json.loads((run_dir / "export" / "bundle" / "episode.json").read_text())
    assert bundle.validate(bundle.read(run_dir / "export" / "bundle"))["ok"]
    assert replay.verify(run_dir) == {"ok": True, "differs": [], "validation": {}}


def test_the_hints_come_from_the_send_log():
    kinds, answers = replay.hints_from_sends([
        {"type": "send", "kind": "opening_prompt", "via": "start", "text": "go", "ref": {"message": "m1"}, "message": "m1", "request": None},
        {"type": "send", "kind": "reply", "via": "answer", "text": "fast", "ref": {"item": "i1"}, "message": None, "request": "r1"},
        {"type": "send", "kind": "refusal", "via": "refuse_secret", "text": "No.", "ref": {"item": "i2"}, "message": None, "request": "r2"},
    ])
    assert kinds == {"m1": "opening_prompt", "i1": "reply", "r1": "reply", "i2": "reply"}
    assert answers == {"i1": "fast", "r1": "fast"}


@pytest.mark.parametrize("case, message", [
    ("no answer in the send log", "cannot rebuild"),
    ("a corrupt T3 backup", "cannot rebuild"),
    ("an unsupported harness", "unsupported harness"),
    ("no stored bundle", "no stored bundle"),
    ("a manifest that is a list", "not a JSON object"),
    ("runner events that are not a list", "missing or of the wrong type"),
])
def test_command_errors(tmp_path, case, message):
    run_dir = raw_record(tmp_path, with_answer_send=case != "no answer in the send log", harness="nope" if case == "an unsupported harness" else "pi")
    if case != "no stored bundle":
        (run_dir / "export" / "bundle").mkdir()
    if case == "a manifest that is a list":
        (run_dir / "export" / "manifest.json").write_text("[]")
    if case == "runner events that are not a list":
        m = json.loads((run_dir / "export" / "manifest.json").read_text())
        m["runner_events"] = {"x": 1}
        (run_dir / "export" / "manifest.json").write_text(json.dumps(m))
    if case == "a corrupt T3 backup":
        (run_dir / "export" / "segment-1" / "t3-state.sqlite").write_bytes(b"not a database" * 100)
    with pytest.raises(replay.ReplayError, match=message):
        replay.verify(run_dir)
    cli = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "verify-episode", str(run_dir)], capture_output=True, text=True)
    assert cli.returncode == 2 and message in cli.stderr and "Traceback" not in cli.stderr


def test_the_driver_record_of_sends_seeds_the_hints(tmp_path):
    """M5 of round 11: an answer the runner did not log as a `send` (it
    landed after a deadline, logged `dispatched_after_deadline`) is still in
    the driver's `sent`; the rebuild seeds from it as the live export does."""
    run_dir = raw_record(tmp_path, with_answer_send=False, driver_sent=True)
    log = run_dir / "runner" / "events.jsonl"
    log.write_text(log.read_text() + json.dumps({"type": "dispatched_after_deadline", "kind": "reply", "via": "answer", "message": None}) + "\n")
    res, b = replay.rebuild(run_dir)
    assert next(e for e in b.view.events if e["type"] == "user_message" and e["kind"] == "reply")["text"] == "fast"
    kinds, answers = replay.hints_from_sends(replay._lines(log), json.loads((run_dir / "export" / "manifest.json").read_text())["sent"])
    assert answers == {next(iter(answers)): "fast", "req-1": "fast"} and len(answers) == 2


def test_validation_errors_come_back_before_publication(tmp_path, monkeypatch):
    """Reviewer 1's minor of round 11: a rebuild that does not validate is
    reported, and nothing is written."""
    run_dir = raw_record(tmp_path)
    (run_dir / "export" / "bundle").mkdir()
    real = bundle.validate
    monkeypatch.setattr(bundle, "validate", lambda b, *a, **k: {**real(b, *a, **k), "ok": False, "errors": {"blobs": ["blob x missing"]}})
    wrote = []
    monkeypatch.setattr(bundle, "write", lambda *a, **k: wrote.append(a))
    out = replay.verify(run_dir)
    assert out == {"ok": False, "differs": None, "validation": {"blobs": ["blob x missing"]}} and wrote == []


def test_jsonl_readers_split_on_line_feed_only(tmp_path):
    """T4 of round 12: the runner logs with ensure_ascii=False, so U+2028 and
    U+0085 can sit inside a logged string; they are not record boundaries."""
    from cohbench.trajectory.adapters.base import loads_jsonl

    rows = [{"type": "send", "kind": "reply", "text": "one two\u0085three"}, {"type": "x"}]
    p = tmp_path / "log.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert replay._lines(p) == rows
    assert loads_jsonl(p) == rows


QUEUED = Path(__file__).parent / "fixtures" / "queued-refusal"
REPO = Path(__file__).resolve().parents[2]


def _queued_run(tmp_path: Path) -> Path:
    """The kept raw record of a secret-refusal episode whose refusal went the
    queued route (the export of 2026-11-22T090323 was rejected), with its T3
    backup written back to SQLite."""
    import shutil

    run_dir = tmp_path / "run"
    shutil.copytree(QUEUED, run_dir)
    seg = run_dir / "export" / "segment-1"
    _sqlite(json.loads((seg / "threads.json").read_text()), seg / "t3-state.sqlite")
    return run_dir


def test_a_queued_refusal_exports_and_says_what_the_runner_did(tmp_path):
    """The refusal of a declined secret request, queued as an ordinary user
    message while the run the decline resumed was still going. T3 dates the
    queued run's request before that run completed; the agent received it
    only after. The resumed turn ends at its closing output, the queued
    message opens the next turn, and the bundle says what the runner's own
    log says."""
    from cohbench.trajectory import validate, validate_view

    run_dir = _queued_run(tmp_path)
    res, b = replay.rebuild(run_dir, REPO / "tests" / "runner" / "fixtures" / "short" / "task.json")
    assert validate(res.trajectory).ok, validate(res.trajectory).errors
    v = b.view
    assert validate_view(v).ok and bundle.validate(b)["ok"], bundle.validate(b)["errors"]
    turns = [(e["type"], e["turn"], e["t"]) for e in v.events if e["type"] in ("turn_start", "turn_end")]
    assert turns == [("turn_start", 1, 96), ("turn_end", 1, 664), ("turn_start", 2, 4239), ("turn_end", 2, 11819),
                     ("turn_start", 3, 11842), ("turn_end", 3, 11939)]
    queued = next(e for e in v.events if e["type"] == "user_message" and e["kind"] == "reply")
    assert queued["turn"] == 3 and queued["text"].startswith("I can't share credentials")
    # The runner's log: three verdicts, two hand-backs, one refusal wait charged 2,000 ms.
    log = replay._lines(run_dir / "runner" / "events.jsonl")
    assert [e["label"] for e in v.events if e["type"] == "verdict"] == [r["label"] for r in log if r["type"] == "verdict"]
    assert sum(e["type"] == "handback" for e in v.events) == sum(r["type"] == "hand_back" for r in log) == 2
    (w,) = [e for e in v.events if e["type"] == "wait"]
    ended = next(r for r in log if r["type"] == "user_wait_end")
    assert (w["causes"], w["charged_ms"], w["ended"]) == (ended["causes"], ended["charged_ms"], ended["ended"]) == (["refusal"], 2000, "reply")
    assert v.events[w["target"]]["secret"] is True
    hb = [e for e in v.events if e["type"] == "handback"]
    assert v.events[hb[0]["target"]]["secret"] and v.events[hb[1]["target"]]["turn"] == 3
    assert b.episode["budget"]["consumed"]["user_wait_ms"] == 2000
    # Rebuilt, written, verified against itself.
    bundle.write(b, run_dir / "export" / "bundle")
    assert replay.verify(run_dir, REPO / "tests" / "runner" / "fixtures" / "short" / "task.json") == {"ok": True, "differs": [], "validation": {}}
