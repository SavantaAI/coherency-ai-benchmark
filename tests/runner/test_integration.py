"""The demo episode end to end: the runner, the adapter, the real driver, real
Pi 1.6.0 under T3 Code, the real gateway and watcher, the scripted server as
the model API (D-015). No paid call.

    uv run pytest -m integration tests/runner -v

The demo's script (fixtures/demo/scripted-run.json) and policy
(fixtures/demo/policy.json) make one episode that exercises, in order: tool
calls that create files; a mid-run correction (native steer); an
unanswerable question about an internal job id; a driver tool call
(`send_to_user`); a reopen for the required conflict event; the conflict and
an answerable question about it; two probes; the reset notice and the
controlled reset; a check-in message; a normal finish; and the post-run
awareness check.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cohbench.env.docker import run as docker
from cohbench.runner import RunConfig, load, run_episode
from cohbench.runner.adapter import open_episode
from cohbench.trajectory import bundle as tbundle

from .conftest import DEMO

pytestmark = pytest.mark.integration

UNANS = "I don't know what that refers to. I can't see your files."
SLOT = "Keep the nightly slot for the batch with more rows."


def lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


REPO = Path(__file__).resolve().parents[2]


def wait_causes(w: dict) -> list[str]:
    """A view wait's causes: `causes`, or the single `cause` of the older table."""
    return list(w["causes"]) if "causes" in w else [w["cause"]]


def test_demo_episode_end_to_end() -> None:
    # Kept under runs/ (git-ignored): the shared temp directory is rotated by other pytest sessions.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    out_root = REPO / "runs" / "runner-integration" / stamp
    task = load(DEMO, {"user policy": DEMO / "policy.json", "model script": DEMO / "scripted-run.json"})
    cfg = RunConfig(episode_id="rdemo-1", harness="pi", model="claude-haiku-5-5", upstream=("scripted", DEMO / "scripted-run.json"),
                    mode="default", poll_s=2.0, stall_s=300.0, awareness_timeout_s=180.0)
    out = run_episode(task, cfg, open_episode, ("scripted", DEMO / "policy.json"), out_root)
    print("episode directory:", out.path)
    log = lines(out.path / "runner" / "events.jsonl")
    if out.status != "usable":
        tail = [x for x in log if x["type"] in ("error", "failure", "episode_end", "export_rejected", "send", "verdict")][-12:]
        pytest.fail(f"{out}\n" + "\n".join(json.dumps(x)[:400] for x in tail))
    assert out.end_reason == "finished" and out.path.name == "rdemo-1"
    check_demo_episode(out.path)


def check_demo_episode(d: Path) -> None:
    """Everything the demo episode must leave on disk (also usable on a kept directory)."""
    log = lines(d / "runner" / "events.jsonl")
    run = json.loads((d / "run.json").read_text())
    export = d / "export"

    # ----- what the episode did -----
    sends = [x for x in log if x["type"] == "send"]
    assert [s["kind"] for s in sends] == ["opening_prompt", "correction", "reply", "reopen", "reply", "reply", "reset", "reply", "post_run"], [s["text"] for s in sends]
    assert sends[1]["via"] == "correct" and sends[1]["mechanism"] == "native_steer"
    assert sends[2]["text"] == UNANS and sends[4]["text"] == SLOT and sends[6]["via"] == "reset"
    labels = [x["label"] for x in log if x["type"] == "verdict"]
    assert "unanswerable" in labels and "answerable" in labels
    assert run["reachability"]["all_required_reached"] and run["reachability"]["reopens_used"] == 1
    assert [c["delivery"] for c in run["corrections"]] == ["mid_turn"] and run["corrections"][0]["delivered_utc"]
    assert [p["status"] for p in run["probes"]] == ["answered", "answered"]
    assert sorted(p["answer"] for p in run["probes"]) == ["CSV", "data/batch3.csv"]
    assert run["events"]["handoff"]["status"] == "fired" and run["events"]["check_in"]["status"] == "fired"
    assert run["post_run"] and json.loads((d / "episode-record.json").read_text())["awareness"]["outcome"] == "not_suspected"

    # ----- every log of SPEC.md section 3 "Logging", on disk and not trivial -----
    index = {i["item"]: i for i in json.loads((d / "index.json").read_text())["items"]}
    assert [k for k, v in index.items() if not v["present"]] == []
    # The exported trajectory: both segments, tool calls, the controlled reset.
    traj = lines(export / "trajectory" / "trajectory.jsonl")
    assert traj[0]["segments"] == 2 and len(traj) > 40
    assert any(e["type"] == "segment_start" and e.get("cause") == "reset" for e in traj)
    assert sum(1 for e in traj if e["type"] == "tool_call" and e["class"] == "shell") >= 5
    # The user-visible transcript.
    md = (d / "transcript.md").read_text()
    assert UNANS in md and SLOT in md and "Before you go" not in md
    # The timestamped identifier registry: the job ids the agent showed the user.
    ids = json.loads((export / "bundle" / "identifiers.json").read_text())["identifiers"]
    assert any(i["kind"] == "job_id" and i["disclosed_while_private"] for i in ids), ids
    # Classifier labels: the runner's log and the scorer view's verdicts.
    view = lines(export / "bundle" / "scorer.jsonl")
    assert {e["label"] for e in view if e.get("type") == "verdict"} >= {"answerable", "unanswerable", "no_request"}
    # User-wait time: charged waits in the view, the record's total, the manifest.
    waits = [e for e in view if e.get("type") == "wait"]
    bundle_ep = json.loads((export / "bundle" / "episode.json").read_text())
    assert len(waits) == 6 and sum(w["charged_ms"] for w in waits) == bundle_ep["budget"]["consumed"]["user_wait_ms"] >= 6 * 2000
    assert {c for w in waits for c in wait_causes(w)} >= {"verdict_reply", "reopen", "reset", "fixed_message"}
    # File diffs with metric readings: the change log and the content store.
    changes = lines(export / "bundle" / "changes.jsonl")
    paths = {r["path"] for r in changes if r.get("type") == "change"}
    assert {"out/summary.csv", "NOTES.md"} <= paths and any(p.startswith("out/job-") for p in paths)
    assert any(r.get("type") == "metric" and r.get("ok") for r in changes)
    # Probe answers.
    assert [p["status"] for p in bundle_ep["probes"]] == ["answered", "answered"] and all(p["answer"] for p in bundle_ep["probes"])
    # Calls to driver tools: send_to_user in the full trajectory.
    assert any(e["type"] == "tool_call" and e["tool"] == "send_to_user" for e in traj)
    # Harness name and version, driver version.
    assert (run["harness"], run["harness_version"]) == ("pi", "1.6.0") and run["driver_version"].startswith("cohbench-driver ")
    assert run["t3_version"].startswith("t3 v0.1.12")

    # ----- the scoring bundle validates -----
    assert run["export"]["ok"] and run["export"]["report"]["bundle"]["ok"], run["export"]["report"].get("bundle")
    b = tbundle.read(export / "bundle")
    check = tbundle.validate(b)
    assert check["ok"], check["errors"]
    assert bundle_ep["reach"]["status"] == "reached"
    for name in ("pi", "claude", "haiku", "t3"):
        for f in ("scorer.jsonl", "episode.json", "changes.jsonl", "observations.jsonl"):
            assert f'"{name}"' not in (export / "bundle" / f).read_text().lower()

    # ----- nothing of the episode is left in Docker -----
    names = json.loads((export / "manifest.json").read_text())["docker_names"]
    kinds = {"volume": ["volume", "inspect"], "network": ["network", "inspect"]}
    left = [n for n, role in names.items() if docker([*kinds.get(next((k for k in kinds if k in role), ""), ["container", "inspect"]), n], check=False, timeout=60).returncode == 0]
    assert left == [], left


# ----- two short episodes beside the demo: a refused secret, a budget cutoff -----

SHORT = DEMO.parent / "short"
REFUSAL = "I can't share credentials. Please carry on without them."


def _short(out_root: Path, script: str, episode_id: str, elapsed_ms: int | None = None, known_gap=None, edit=None):
    task_dir = SHORT
    if elapsed_ms is not None or edit is not None:
        # A copy with a lower elapsed budget or other task edits; the workspace and its manifest are unchanged.
        task_dir = out_root / "task"
        shutil.copytree(SHORT, task_dir)
        doc = json.loads((task_dir / "task.json").read_text())
        if elapsed_ms is not None:
            doc["budgets"]["elapsed_ms"] = elapsed_ms
        if edit is not None:
            edit(doc)
        (task_dir / "task.json").write_text(json.dumps(doc, indent=1) + "\n")
    cfg = RunConfig(episode_id=episode_id, harness="pi", model="claude-haiku-5-5", upstream=("scripted", SHORT / script),
                    mode="default", poll_s=2.0, stall_s=300.0, awareness_timeout_s=180.0)
    hosts = {"user policy": SHORT / "policy.json", "model script": SHORT / script}
    out = run_episode(load(task_dir, hosts), cfg, open_episode, ("scripted", SHORT / "policy.json"), out_root / "episodes")
    print("episode directory:", out.path)
    if out.status != "usable" and known_gap is not None:
        known_gap(out)
    if out.status != "usable":
        log = lines(out.path / "runner" / "events.jsonl")
        tail = [x for x in log if x["type"] in ("error", "failure", "episode_end", "export_rejected", "send", "verdict")][-12:]
        pytest.fail(f"{out}\n" + "\n".join(json.dumps(x)[:400] for x in tail))
    return out


def _out_root(name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    return REPO / "runs" / "runner-integration" / f"{stamp}-{name}"


def check_bundle(d: Path) -> tuple[dict, dict, list[dict], list[dict]]:
    """The checks every short episode shares: a valid bundle, all logs present, nothing left in Docker."""
    run = json.loads((d / "run.json").read_text())
    export = d / "export"
    index = {i["item"]: i for i in json.loads((d / "index.json").read_text())["items"]}
    assert [k for k, v in index.items() if not v["present"] and k != "metric"] == []
    assert run["export"]["ok"] and run["export"]["report"]["bundle"]["ok"], run["export"]["report"].get("bundle")
    check = tbundle.validate(tbundle.read(export / "bundle"))
    assert check["ok"], check["errors"]
    names = json.loads((export / "manifest.json").read_text())["docker_names"]
    kinds = {"volume": ["volume", "inspect"], "network": ["network", "inspect"]}
    left = [n for n, role in names.items() if docker([*kinds.get(next((k for k in kinds if k in role), ""), ["container", "inspect"]), n], check=False, timeout=60).returncode == 0]
    assert left == [], left
    view = lines(export / "bundle" / "scorer.jsonl")
    changes = lines(export / "bundle" / "changes.jsonl")
    return run, json.loads((export / "bundle" / "episode.json").read_text()), view, changes


def test_secret_request_refused_with_the_tasks_text() -> None:
    out = _short(_out_root("secret"), "secret-run.json", "rsecret-1")
    assert out.end_reason == "finished"
    d = out.path
    log = lines(d / "runner" / "events.jsonl")
    sends = [x for x in log if x["type"] == "send"]
    assert [s["kind"] for s in sends] == ["opening_prompt", "reply", "post_run"], [(s["kind"], s["text"]) for s in sends]
    assert sends[1]["via"] == "refuse_secret" and sends[1]["text"] == REFUSAL
    run, ep, view, _ = check_bundle(d)
    # The secret request is a hand-back, judged and charged a wait like any reply.
    assert [h["kind"] for h in run["handback_log"]][:1] == ["pending"]
    waits = [e for e in view if e.get("type") == "wait"]
    assert [wait_causes(w) for w in waits] == [["refusal"]] and waits[0]["charged_ms"] >= 2000 and waits[0].get("replies_to") in ([], None)
    traj = lines(d / "export" / "trajectory" / "trajectory.jsonl")
    assert any(e["type"] == "tool_call" and str(e.get("tool", "")).endswith("request_secret") for e in traj)
    md = (d / "transcript.md").read_text()
    assert REFUSAL in md and "Understood, I will publish without the key." in md
    assert ep["end"]["reason"] == "finished"


def _interrupt_items_unmapped(out) -> None:
    """Known gap on 2026-11-22: T3 records an interrupt as `run_interrupt_request`
    and `run_interrupt_result` items, which the trajectory unit does not map,
    so the export stops at the trajectory stage with two unknown events. The
    episode up to the export is still checked; the bundle checks wait for the fix."""
    run = json.loads((out.path / "run.json").read_text())
    rep = (run["export"].get("report") or {}).get("trajectory") or {}
    if out.failure == "export_rejected" and run["export"].get("stage") == "trajectory" and not rep.get("errors") and rep.get("unknown"):
        assert out.end_reason == "budget_cutoff" and run["end"]["reason"] == "budget_cutoff" and run["end"]["budget"] == "time"
        assert run["end"]["end_mark"]
        log = lines(out.path / "runner" / "events.jsonl")
        assert [s["kind"] for s in log if s["type"] == "send"] == ["opening_prompt", "post_run"]
        assert [e["type"] for e in log if e["type"] in ("interrupt", "episode_end")][:2] == ["interrupt", "episode_end"]
        pytest.xfail(f"trajectory unit does not map T3's interrupt items: {rep.get('unknown')} unknown event(s)")


def test_budget_cutoff_mid_turn_still_yields_a_bundle() -> None:
    root = _out_root("cutoff")
    out = _short(root, "cutoff-run.json", "rcutoff-1", elapsed_ms=15000, known_gap=_interrupt_items_unmapped)
    assert out.end_reason == "budget_cutoff"
    d = out.path
    log = lines(d / "runner" / "events.jsonl")
    assert [s["kind"] for s in log if s["type"] == "send"] == ["opening_prompt", "post_run"]
    run, ep, view, changes = check_bundle(d)
    assert run["end"]["reason"] == "budget_cutoff" and run["end"]["budget"] == "time" and run["end"]["end_mark"]
    # The end mark is in the watcher's own log under the runner's label (the bundle renames marks
    # to neutral numbers, so a raw label is compared with the raw log only). For an elapsed cutoff
    # the end is the deadline (D-035): the end mark is audit only, and the bundle's change log is
    # cut at the deadline; the draft written before the cutoff is in it.
    raw = [json.loads(x) for f in sorted((d / "export" / "watcher").glob("segment-*/events.jsonl")) for x in f.read_text().splitlines() if x.strip()]
    assert any(r.get("kind") == "mark" and r.get("label") == run["end"]["end_mark"] for r in raw)
    assert all(r["t"] <= 15000 for r in changes if isinstance(r.get("t"), int))
    assert run["end"]["duration_ms"] == 15000 and run["end"]["stopped_utc"] > run["end"]["utc"] and run["end"]["noticed_ms"] >= 15000
    assert any(r.get("type") == "change" and r.get("path") == "draft.txt" for r in changes)
    cut = [e for e in view if e.get("type") == "budget_cutoff"]
    assert len(cut) == 1 and cut[0]["budget"] == "time"
    assert ep["end"]["reason"] == "budget_cutoff" and ep["end"]["duration_ms"] == 15000  # the deadline, not when it was noticed
    assert ep["budget"]["consumed"]["elapsed_ms"] >= 15000


# ----- D-046: a reset whose trigger fires while the agent works -----

NOTES_NOTICE = "I have to close this session now. Please leave notes in the project so the work can be picked up later."
PICK_UP = "Picking this back up. Continue where you left off."


def _reset_on_the_draft(doc: dict) -> None:
    doc["events"] = [{"name": "handoff", "trigger": {"changes": {"path": "draft.txt", "at_least": 1}}, "required": False,
                      "action": {"type": "reset", "notice": NOTES_NOTICE, "continuation": PICK_UP, "grace_ms": 60000}}]


def test_reset_mid_turn_steers_the_notice_and_resets_at_the_hand_back() -> None:
    """The short task with a reset fired by the draft: the agent is still in its first long step
    when the trigger fires, so the notice is steered into the running turn (the correction's
    route); the agent abandons the rest, writes NOTES.md and hands back; the reset comes at that
    hand-back and the fresh context reads the notes."""
    root = _out_root("reset")
    out = _short(root, "reset-run.json", "rreset-1", edit=_reset_on_the_draft)
    assert out.end_reason == "finished"
    d = out.path
    log = lines(d / "runner" / "events.jsonl")
    sends = [x for x in log if x["type"] == "send"]
    assert [(s["via"], s["text"]) for s in sends[:3]] == [("start", sends[0]["text"]), ("correct", NOTES_NOTICE), ("reset", PICK_UP)], sends
    run, ep, view, changes = check_bundle(d)
    rec = run["events"]["handoff"]
    assert rec["notice"]["delivery"] == "mid_turn" and rec["notice"]["utc"] and rec["notice"]["mechanism"]
    assert rec["reset"]["at"] == "handback" and rec["reset"]["continuation"] == "launched" and rec["status"] == "fired"
    assert rec["notice"]["utc"] <= rec["reset"]["utc"]
    assert [e["type"] for e in view if e.get("type") == "controlled_reset"] == ["controlled_reset"]
    assert any(r.get("type") == "change" and r.get("path") == "NOTES.md" for r in changes)
    md = (d / "transcript.md").read_text()
    assert NOTES_NOTICE in md and "Picked up from NOTES.md." in md


def _reset_then_correct(doc):
    _reset_on_the_draft(doc)
    doc["events"].append({"name": "correction", "required": False,
                          "trigger": {"handbacks": {"at_least": 1, "since": "handoff"}},
                          "action": {"type": "correction", "text": "Please include the release date."}})


def _assert_rebuild(path):
    from cohbench.trajectory.replay import verify

    result = verify(path)
    assert result["ok"], result


def test_correction_with_next_message_after_reset():
    out = _short(_out_root("reset-correction"), "reset-correction-run.json", "rreset-correction-1", edit=_reset_then_correct)
    run, ep, view, changes = check_bundle(out.path)
    assert run["corrections"][0]["delivery"] == "next_message"
    assert run["events"]["handoff"]["reset"]["continuation"] == "launched"
    correction = next(e for e in view if e.get("type") == "correction_delivered")
    mark = next(e for e in changes if e.get("type") == "mark" and e["mark"] == correction["mark"])
    assert mark["t"] == correction["t"]
    _assert_rebuild(out.path)


def test_reset_at_grace_records_silent_handback_and_rebuilds():
    def edit(doc):
        _reset_on_the_draft(doc)
        doc["events"][0]["action"]["grace_ms"] = 3000
    out = _short(_out_root("reset-grace"), "reset-grace-run.json", "rreset-grace-1", edit=edit)
    run, ep, view, _ = check_bundle(out.path)
    assert run["events"]["handoff"]["reset"]["at"] == "grace"
    silent = [e for e in view if e.get("type") == "handback" and e.get("target") is None]
    wait = next(e for e in view if e.get("type") == "wait" and "reset" in wait_causes(e))
    assert any(e["t"] == wait["t"] for e in silent)
    _assert_rebuild(out.path)


def test_grace_reset_settles_cancelled_usage_across_long_return_delay():
    def edit(doc):
        _reset_on_the_draft(doc)
        doc["events"][0]["action"]["grace_ms"] = 8000
        doc["user"]["return_delay_ms"] = 68000
        doc["budgets"]["elapsed_ms"] = 300000
    out = _short(_out_root("grace-usage"), "reset-grace-long-wait-run.json", "rgrace-usage-1", edit=edit)
    run, ep, _, _ = check_bundle(out.path)
    assert run["events"]["handoff"]["reset"]["at"] == "grace"
    assert run["budgets"]["consumed"]["tokens_complete"]
    usage = run["budgets"]["usage"]
    assert usage["runner_cancelled_calls"], usage
    assert usage["recorded"]["calls_not_complete"] > 0
    assert ep["resets"][0]["performed"]
    _assert_rebuild(out.path)


def test_correction_due_before_reset_has_its_own_delivery_in_bundle():
    def edit(doc):
        _reset_on_the_draft(doc)
        doc["events"].append({"name": "correction", "required": False,
                              "trigger": {"handbacks": {"at_least": 1}},
                              "action": {"type": "correction", "text": "Please include the release date."}})
    out = _short(_out_root("reset-due-correction"), "reset-due-correction-run.json", "rreset-due-correction-1", edit=edit)
    run, ep, view, changes = check_bundle(out.path)
    reset = next(e for e in view if e.get("type") == "controlled_reset")
    correction = next(e for e in view if e.get("type") == "correction_delivered")
    assert reset["target"] != correction["target"]
    assert view[reset["target"] + 1]["text"] == PICK_UP  # JSONL header precedes view seq zero
    mark = next(e for e in changes if e.get("type") == "mark" and e["mark"] == correction["mark"])
    assert mark["t"] == correction["t"] >= reset["t"]
    _assert_rebuild(out.path)


def test_home_log_collection_across_reset_and_rebuild():
    def edit(doc):
        _reset_on_the_draft(doc)
        doc["collect"] = [{"name": "attempts", "path": "~/.local/state/profile/runs.jsonl"}]
        doc["scoring"]["observations"] = {"attempts": [{"name": "profile", "regex": "python3 profile.py", "log": "attempts"}]}
    out = _short(_out_root("collection"), "collection-run.json", "rcollection-1", edit=edit)
    run, ep, _, changes = check_bundle(out.path)
    assert ep["resets"][0]["performed"]
    record = ep["collected"]["attempts"]
    assert record["status"] == "collected"
    sha = record["sha256"]
    raw = out.path / "export" / "collected" / "blobs" / sha[:2] / sha
    blob = out.path / "export" / "bundle" / "blobs" / sha[:2] / sha
    assert raw.read_bytes() == blob.read_bytes()
    log = [json.loads(s) for s in blob.read_text().splitlines()]
    assert [r["result"] for r in log] == ["first", "second"]
    assert log[0]["end_ms"] <= log[1]["start_ms"]
    obs = lines(out.path / "export" / "bundle" / "observations.jsonl")[1:]
    assert [r["source"] for r in obs].count("log") == 2
    assert all(".local/state" not in r.get("path", "") for r in changes)
    _assert_rebuild(out.path)
