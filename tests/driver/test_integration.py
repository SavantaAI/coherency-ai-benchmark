"""The driver's operations against T3 Code and Pi, with the scripted server
as the model API (D-015). No paid call. Each test drives one episode and
checks the result from outside: T3's records, the scripted server's log and
state, the workspace volume, the watcher's output, Docker's object list.

    uv run pytest -m integration tests/driver -v
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from cohbench.driver import Episode, TurnNotRunning
from cohbench.driver.harness import harness
from cohbench.env import Upstream, WatcherSpec, manifest_for
from cohbench.env.docker import run

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
MODEL = "claude-haiku-5-5"
WS = "/work/project"
# D-026's lists, given explicitly as the driver gives them.
WSPEC = WatcherSpec(ignore_names=("AGENTS.md", "CLAUDE.md"), ignore_git_refs=("refs/t3/",))
COACHED = "Ask only questions the user can answer, lead with the conclusion, and check progress against the stated goal."


def episode(images, workspace, tmp_path, script, out: str = "ep", **kw) -> Episode:
    """An episode on the scripted upstream; the manifest is made from the
    workspace as it is now, as a task author would."""
    return Episode("pi", model=MODEL, upstream=Upstream.scripted(script), workspace=workspace, manifest=manifest_for(workspace), out_dir=tmp_path / out, images=images, **kw)


def ok_export(out: dict) -> bool:
    r = out["report"]
    return r["trajectory"]["ok"] and r["trajectory_blindness"]["ok"] and r["bundle"]["ok"]


def exists(kind: str, name: str) -> bool:
    sub = {"container": ["container", "inspect"], "network": ["network", "inspect"], "volume": ["volume", "inspect"], "image": ["image", "inspect"]}[kind]
    return run([*sub, name], check=False, timeout=60).returncode == 0


def kind_of(role: str) -> str:
    return "volume" if "volume" in role else ("network" if "network" in role else "container")


def assert_all_gone(names: dict[str, str], images: list[str] = ()) -> None:
    left = [(n, r) for n, r in names.items() if exists(kind_of(r), n)] + [(i, "image") for i in images if exists("image", i)]
    assert not left, left


def _utc(x: str):
    import datetime as _dt

    return _dt.datetime.fromisoformat(x.replace("Z", "+00:00"))


def scorer_view(dest: Path) -> list[dict]:
    return [json.loads(line) for line in (dest / "bundle" / "scorer.jsonl").read_text().splitlines() if line.strip()]


def commands(ep: Episode) -> list[str]:
    p = ep.t3.snapshot(ep.thread_id)["projection"]
    return [i.get("input") or "" for i in p["turnItems"] if i["type"] == "command_execution" and i.get("input") != "Preparing workspace"]


# ---------------------------------------------------------------------------


def test_start_wait_two_turns_and_export(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [
        {"id": "first", "steps": [{"do": "bash", "command": "printf 'job-%s\\n' 7f3a", "capture": [{"var": "job", "regex": "(job-\\S+)"}]}], "final": "I started {{job}}."},
        {"id": "second", "steps": [{"do": "write", "path": f"{WS}/status.txt", "content": "{{job}} done\n"}], "final": "Wrote the status of {{job}}."},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        s1 = ep.start("Start the job.")
        assert s1.kind == "opening" and s1.message_id and s1.run_id and s1.sent_utc.endswith("Z") and s1.segment == 1
        t1 = ep.wait(timeout_s=120)
        assert t1.state == "finished" and t1.final_text == "I started job-7f3a."
        assert [(o["kind"], o["text"], o["item_id"]) for o in t1.outputs] == [("final", "I started job-7f3a.", t1.final_item_id)] and t1.final_item_id
        d = ep.describe()
        assert d["harness"] == "pi" and d["harness_version"] == "1.6.0" and d["t3_version"] == "t3 v0.1.12-nightly.20261121.5051" and d["driver_version"].startswith("cohbench-driver ")
        assert [p.name for p in ep.watcher_dirs()] == ["segment-1"] and (ep.watcher_dirs()[0] / "events.jsonl").exists()
        assert ep.mark("runner-check")["ok"]
        with pytest.raises(TypeError):
            ep.answer("blue")  # answer takes the Question a wait returned
        s2 = ep.reply("Write its status.")
        assert s2.run_id != s1.run_id
        t2 = ep.wait(timeout_s=120)
        assert t2.state == "finished" and t2.final_text == "Wrote the status of job-7f3a."
        assert ep.env.harness.sh(f"cat {WS}/status.txt") == "job-7f3a done\n"
        state = ep.env.site.scripted_state()
        assert [h["status"] for h in state["handlers"]["main"]] == ["done", "done"]
        usage = ep.usage()
        assert usage["calls"] >= 4 and usage["errors"]["4xx"] == 0
        assert ep.elapsed() > 0 and ep.started_utc
        out = ep.export()
        dest = Path(out["dest"])
        m = json.loads((dest / "manifest.json").read_text())
        assert m["harness"] == "pi" and m["segments"][0]["harness_version"] == "1.6.0"
        assert m["segments"][0]["t3_version"] == "t3 v0.1.12-nightly.20261121.5051"
        assert set(m["image_ids"]) == {"harness", "gateway", "scripted", "watcher"}
        assert any(r == "harness container, segment 1" for r in m["docker_names"].values())
        for rel in ("segment-1/t3-state.sqlite", "segment-1/threads.json", "gateway/requests.jsonl", "scripted/requests.jsonl", "watcher/segment-1/events.jsonl", "trajectory/trajectory.jsonl", "trajectory/manifest.json", "bundle/scorer.jsonl", "environment.json"):
            assert (dest / rel).exists(), rel
        assert list((dest / "segment-1" / "sessions").rglob("*.jsonl"))
        assert ok_export(out)
        assert {"changes.jsonl", "identifiers.json", "observations.jsonl", "scorer.jsonl"} <= set(m["bundle"]["files"])
        assert set(out["files"]) >= {"trajectory", "identifier_registry", "file_diffs", "scoring_bundle", "raw_record", "harness", "driver_version"}
        assert all(out["files"][k] for k in ("trajectory", "identifier_registry", "file_diffs", "scoring_bundle", "raw_record"))
        assert m["harness_default_model"] is True and "/home/node/.pi/agent/settings.json" in m["settings_written"]
        # Each finished turn asked the watcher for a mark.
        assert [t["mark_batch"] is not None for t in m["turns"]] == [True, True]
        assert m["clock"]["episode_s"] < m["clock"]["wall_s"]
        sent_ids = {s["message_id"] for s in m["sent"]}
        traj_manifest = json.loads((dest / "trajectory" / "manifest.json").read_text())
        assert {s1.message_id, s2.message_id} <= sent_ids
        assert traj_manifest["run_info"]["harness"] == "pi"
        names = dict(ep.env.names)
    assert_all_gone(names)
    # The published export gets the final removal results at close.
    env_rec = json.loads((dest / "environment.json").read_text())
    assert env_rec["closed"] and env_rec["removal"] and all(r["confirmed_absent"] for r in env_rec["removal"])


def test_question_in_plain_text(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [{"id": "ask", "end": "question", "steps": [{"do": "bash", "command": "ls"}], "final": "Should I use config a or config b?"}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Set up the config.")
        t = ep.wait(timeout_s=120)
        # Stock Pi has no question tool: the question ends the turn as text.
        assert t.state == "finished" and t.final_text == "Should I use config a or config b?" and t.question is None
        h = ep.env.site.scripted_state()["handlers"]["main"]
        assert h[0]["ended_with"] == "question" and h[0]["status"] == "done"


def test_correction_abandons_remaining_steps(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [
        {"id": "steps", "on_steer": "abandon", "steps": [
            {"do": "bash", "command": "echo step1"},
            {"do": "bash", "command": "sleep 4; echo step2"},
            {"do": "bash", "command": "sleep 4; echo step3"},
            {"do": "bash", "command": "sleep 4; echo step4"}], "final": "All four steps ran."},
        {"id": "steer", "steps": [{"do": "bash", "command": f"echo halted > {WS}/stopped.txt"}], "final": "Stopped as you asked."},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        s0 = ep.start("Run the four steps.")
        deadline = time.time() + 60
        while time.time() < deadline and not any("echo step1" in c for c in commands(ep)):
            time.sleep(0.3)
        c = ep.correct("Stop after the step you are on.")
        assert c.kind == "correction" and c.mechanism == "native_steer" and c.run_id == s0.run_id
        assert ep.corrections[0]["capabilities"].get("supportsActiveSteering") is True
        assert ep.corrections[0]["requested"] == ep.corrections[0]["observed"] == "native_steer"
        t = ep.wait(timeout_s=120)
        assert t.state == "finished" and t.final_text == "Stopped as you asked."
        d = ep.correction_delivery(c)
        # The driver's mark at delivery, taken inside wait() when it saw it.
        assert d.delivered_utc and d.mark_taken and d.mark_batch is not None and d.mark_label.startswith("correction-") and c.delivery == d
        # D-037: the mark names the delivery's instant, not the time of the call.
        assert _utc(d.mark_at) == _utc(d.delivered_utc)
        with pytest.raises(TurnNotRunning):
            ep.correct("Too late.")
        cmds = commands(ep)
        assert not any("step3" in x or "step4" in x for x in cmds), cmds
        assert ep.env.harness.sh(f"cat {WS}/stopped.txt") == "halted\n"
        st = ep.env.site.scripted_state()["handlers"]["main"]
        assert [h["status"] for h in st] == ["abandoned", "done"]
        # The delivery (T3's completedAt on the correction) got a watcher mark.
        c = ep.corrections[0]
        assert c["delivered_utc"] and c["mark_batch"] is not None and c["mark_acked_utc"] >= c["mark_requested_utc"] >= c["delivered_utc"]
        print("correction delivered", c["delivered_utc"], "mark requested", c["mark_requested_utc"], "acknowledged", c["mark_acked_utc"])
        # The driver's own correction_delivered event carries its mark, and
        # the scorer view gets the mark's number and batch.
        out = ep.export()
        assert ok_export(out), out["report"]
        view = scorer_view(Path(out["dest"]))
        cds = [e for e in view if e.get("type") == "correction_delivered"]
        assert len(cds) == 1 and cds[0]["mark"] is not None and cds[0]["batch"] is not None, cds
        assert [e["kind"] for e in view if e.get("type") == "user_message"] == ["opening_prompt", "correction"]


def test_watcher_records_every_write(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [{"id": "files", "steps": [
        {"do": "write", "path": f"{WS}/src/app.py", "content": "print('one')\n"},
        # Each step waits a second, longer than the watcher's quiet_ms, so no
        # two writes merge into one record (cohbench/watcher/README.md, Limits).
        {"do": "edit", "path": f"{WS}/src/app.py", "edits": [{"oldText": "one", "newText": "two"}], "delay": 1},
        {"do": "bash", "command": f"mkdir -p {WS}/data && printf 'a,b\\n1,2\\n' > {WS}/data/out.csv && chmod 600 {WS}/data/out.csv", "delay": 1},
        {"do": "bash", "command": f"mv {WS}/data/out.csv {WS}/data/final.csv && rm {WS}/README.md", "delay": 1},
        {"do": "write", "path": f"{WS}/notes.md", "content": "notes\n", "delay": 1}], "final": "Done."}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Make the files.")
        assert ep.wait(timeout_s=120).state == "finished"
        # The volume's real content, read before the export stops the container.
        real = ep.env.harness.sh(f"cd {WS} && find . -path ./.git -prune -o -type f -print0 | xargs -0 sha256sum")
        out = ep.export()
        wdir = Path(out["dest"]) / "watcher" / "segment-1"
        events = [json.loads(line) for line in (wdir / "events.jsonl").read_text().splitlines() if line.strip()]
    from cohbench.watcher import state_at

    real_files = {line.split("  ", 1)[1][2:]: line.split()[0] for line in real.splitlines()}
    st = state_at(wdir, "last")
    watched = {p: e.sha256 for p, e in st.entries.items() if e.type == "file"}
    assert watched == real_files
    # Records written after the baseline (the watcher's batch 0, or its
    # `baseline: true` records in the earlier format).
    after = [e for e in events if "path" in e and not e.get("baseline") and e.get("batch", 1) != 0]
    paths = {(e["kind"], e["path"]) for e in after}
    for kind, path in (("created", "src/app.py"), ("modified", "src/app.py"), ("created", "data/out.csv"), ("moved", "data/final.csv"), ("deleted", "README.md"), ("created", "notes.md")):
        assert (kind, path) in paths, (kind, path, sorted(paths))
    mode = st.entries["data/final.csv"].mode
    assert (int(mode, 8) if isinstance(mode, str) else mode) == 0o600


def test_reset_keeps_the_workspace_and_drops_tmp(images, workspace, tmp_path):
    lib = workspace / "lib"
    lib.mkdir()
    (lib / "README").write_text("lib\n")
    genv = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@localhost", "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@localhost", "PATH": os.environ["PATH"]}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=lib, check=True, env=genv)
    # The control from the proof: objects already unreachable before the
    # handoff (a stash-like dangling blob in each repository) must survive the
    # cleanup of T3's refs.
    dangling = {}
    for repo, rel in ((workspace, WS), (lib, f"{WS}/lib")):
        dangling[rel] = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=f"dangling draft in {repo.name}\n", capture_output=True, text=True, check=True, env=genv).stdout.strip()
    script = {"version": 1, "handlers": [
        {"id": "notes", "steps": [{"do": "bash", "command": f"printf 'keep me\\n' > {WS}/notes.txt && echo scratch > /tmp/scratch.txt && echo draft > {WS}/lib/draft.txt && printf 'def test_fix(): pass\\n' > {WS}/test_fix.py"}], "final": "Notes left."},
        {"id": "after", "steps": [{"do": "bash", "command": f"cat {WS}/notes.txt; ls /tmp/scratch.txt 2>&1 || true"}], "final": "{{last_output}}"},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Leave yourself notes; I am closing the session.")
        assert ep.wait(timeout_s=120).state == "finished"
        old_container, old_thread = ep.env.harness.name, ep.thread_id
        for rel, sha in dangling.items():
            assert sha in ep.env.harness.sh(f"git -C {rel} fsck --unreachable --no-progress")
        s = ep.reset("We are back. What do your notes say?")
        assert s.kind == "reset" and s.segment == 2 and s.thread_id != old_thread
        assert ep.env.harness.name != old_container and not exists("container", old_container)
        t = ep.wait(timeout_s=120)
        assert t.state == "finished" and t.segment == 2
        assert t.final_text.startswith("keep me\n") and "No such file or directory" in t.final_text
        h = ep.handoffs[0]
        assert h["clone_equal"] and all(g["ok"] for g in h["git"].values())
        assert h["final_mark"]["ok"] and h["watcher_stop"]["clean"]
        # A file the agent named test_fix.py did not stop the handoff, and the
        # plumbing is outside the episode clock.
        assert ep.env.harness.sh(f"ls {WS}/test_fix.py").strip() == f"{WS}/test_fix.py"
        assert [e["reason"] for e in ep.clock.excluded] == ["handoff"]
        # T3 checkpointed the nested repository too; both lost their T3 refs.
        assert set(h["git"]) == {WS, f"{WS}/lib"} and all(g["t3_refs_deleted"] for g in h["git"].values())
        # The objects unreachable before the handoff are still there.
        for rel, sha in dangling.items():
            assert ep.env.harness.exec(["git", "-C", rel, "cat-file", "-e", sha], check=False).returncode == 0, (rel, sha)
            assert h["git"][rel]["unreachable_before"] >= 1
        # The excluded interval starts when reset was called; the driver's
        # controlled_reset event carries that time.
        ev = next(e for e in ep.runner_events if e["type"] == "controlled_reset")
        assert ev["utc"] == ep.clock.excluded[0]["start_utc"]
        # The new thread checkpoints under its own scope; none of the old refs is back.
        for repo, g in h["git"].items():
            now = set(ep.env.harness.sh(f"git -C {repo} for-each-ref --format='%(refname)'").split())
            assert not now & set(g["t3_refs_deleted"])
        out = ep.export()
        dest = Path(out["dest"])
        assert (dest / "watcher" / "segment-2" / "events.jsonl").exists() and (dest / "segment-2" / "t3-state.sqlite").exists()
        traj = [json.loads(line) for line in (dest / "trajectory" / "trajectory.jsonl").read_text().splitlines()]
        assert traj[0]["segments"] == 2
        # The controlled reset reaches the trajectory as the runner declared it.
        assert any(e["type"] == "segment_start" and e.get("cause") == "reset" for e in traj)
        assert any(e["type"] == "user_message" and e.get("kind") == "reset" for e in traj)
        assert ok_export(out)


def test_probe_leaves_the_main_run_untouched(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [
        {"id": "one", "steps": [{"do": "bash", "command": "echo r24 > /work/project/name.txt"}], "final": "Saved the name."},
        {"id": "two", "steps": [], "final": "Second turn."},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Save a name.")
        assert ep.wait(timeout_s=120).state == "finished"
        main_log = ep.env.site.scripted_requests()
        before = ep.t3.snapshot(ep.thread_id)["projection"]
        files_before = ep.env.harness.sh(f"cd {WS} && find . -path ./.git -prune -o -type f -print | sort")
        p = ep.probe("Has the user been shown the name r24?", scripted_reply="No.")
        assert p.outcome == "answered" and p.answer == "No.", p
        assert p.continuity["same_session"] and p.detail["capture"]["clone_equal"] and p.detail["cleanup"]["removed"] and p.detail["capture"]["unpause"]["resumed"]
        assert [e["reason"] for e in ep.clock.excluded] == ["probe"]
        after = ep.t3.snapshot(ep.thread_id)["projection"]
        assert ep.env.site.scripted_requests() == main_log
        assert [r["id"] for r in after["runs"]] == [r["id"] for r in before["runs"]]
        assert [i["id"] for i in after["turnItems"]] == [i["id"] for i in before["turnItems"]]
        assert len(after["messages"]) == len(before["messages"])
        assert ep.env.harness.sh(f"cd {WS} && find . -path ./.git -prune -o -type f -print | sort") == files_before
        copy_names = [n for n, r in ep.env.names.items() if r.startswith("probe copy")]
        assert copy_names and not [n for n in copy_names if exists(kind_of(ep.env.names[n]), n)]
        assert ep.env.copies[0].image and not exists("image", ep.env.copies[0].image)
        assert ep.probes[0]["outcome"] == "answered"
        # The main run goes on with its next handler, as if no probe happened.
        ep.reply("Next.")
        assert ep.wait(timeout_s=120).final_text == "Second turn."


def test_coached_sentence_reaches_the_model_request(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [{"steps": [], "final": "ok"}]}
    prompts = {}
    for mode in ("coached", "default"):
        kw = {"mode": mode, "coached_sentence": COACHED} if mode == "coached" else {}
        with episode(images, workspace, tmp_path, script, out=mode, **kw) as ep:
            ep.start("Hello.")
            assert ep.wait(timeout_s=120).state == "finished"
            listing = ep.env.harness.sh(f"ls {WS}")
            prompts[mode] = ep.env.site.scripted_system_prompts()
            assert ("AGENTS.md" in listing) == (mode == "coached")
    assert harness("pi").instruction_file == "AGENTS.md"
    assert prompts["coached"] and prompts["default"]
    assert all(COACHED in text for text in prompts["coached"].values())
    assert not any(COACHED in text for text in prompts["default"].values())


def test_secret_and_question_answers(images, workspace, tmp_path):
    script = {"version": 1, "handlers": [
        {"id": "secret", "steps": [
            {"do": "tool", "name": "tool_search", "input": {"query": "request_secret", "limit": 5}},
            {"do": "tool", "name": "mcp__t3-code__request_secret", "input": {"label": "Staging PIN", "reason": "To copy it into pin.txt.", "delivery": "file", "fields": [{"name": "pin", "label": "PIN"}]},
             "capture": [{"var": "pinfile", "regex": "\"pin\"\\s*:\\s*\"([^\"]+)\"", "required": True}]},
            {"do": "bash", "command": "cp {{pinfile}} /work/project/pin.txt"}], "final": "Saved the PIN."},
        {"id": "question", "steps": [
            {"do": "tool", "name": "ask_user", "input": {"question": "Which color?", "options": ["red", "blue"]}}], "final": "You chose {{last_output}}."},
    ]}
    with episode(images, workspace, tmp_path, script, diagnostic_question_tool=True) as ep:
        ep.start("Copy my PIN into pin.txt.")
        t = ep.wait(timeout_s=120)
        assert t.state == "secret" and t.secret.label == "Staging PIN" and [f["name"] for f in t.secret.fields] == ["pin"]
        s = ep.answer_secret(t.secret, {"pin": "4821"})
        assert s.text is None and s.kind == "secret"
        t = ep.wait(timeout_s=120)
        assert t.state == "finished" and t.final_text == "Saved the PIN."
        assert ep.env.harness.sh("cat /work/project/pin.txt") == "4821"
        ep.reply("Pick a color for me to choose.")
        t = ep.wait(timeout_s=120)
        assert t.state == "question" and t.question.text == "Which color?" and t.question.options == ["red", "blue"]
        a = ep.answer(t.question, "Blue")
        # request_id is the question's turn item; T3's runtime request id is kept beside it.
        assert a.kind == "answer" and a.request_id == t.question.item_id and a.rpc_request_id == t.question.request_id and a.status == "accepted"
        t = ep.wait(timeout_s=120)
        assert t.state == "finished" and t.final_text == "You chose blue."
        out = ep.export()
        assert ok_export(out)
        assert "4821" not in (Path(out["dest"]) / "manifest.json").read_text()


def test_send_to_user_and_subagent_export(images, workspace, tmp_path):
    script = {
        "version": 1,
        "handlers": [{"id": "parent", "steps": [
            {"do": "tool", "name": "tool_search", "input": {"query": "send_to_user", "limit": 5}},
            {"do": "tool", "name": "mcp__t3-code__send_to_user", "input": {"message": "Build finished at step 3."}},
            {"do": "delegate", "child": "checker", "task": "Run cat /work/project/README.md and reply with its first line only.", "input": {"mode": "wait", "role": "research", "title": "Check readme"}}],
            "final": "The child answered."}],
        "children": {"checker": {"handlers": [{"steps": [{"do": "bash", "command": "head -1 /work/project/README.md"}], "final": "{{last_output}}"}]}},
    }
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Report progress and check the readme with a helper.")
        t = ep.wait(timeout_s=240)
        assert t.state == "finished" and t.final_text == "The child answered."
        assert [u["text"] for u in t.user_channel] == ["Build finished at step 3."]
        out = ep.export()
        threads = json.loads((Path(out["dest"]) / "segment-1" / "threads.json").read_text())["threads"]
        assert len(threads) == 2 and threads[1]["thread"]["lineage"]["parentThreadId"] == threads[0]["thread"]["id"]
        assert ok_export(out)
        traj = [json.loads(line) for line in (Path(out["dest"]) / "trajectory" / "trajectory.jsonl").read_text().splitlines()]
        assert traj[0]["agents"] == 2
        assert any(e.get("type") == "tool_call" and e.get("user_channel") and e.get("user_text") == "Build finished at step 3." for e in traj)


def test_close_after_a_forced_exception(images, workspace, tmp_path):
    # The interrupted handler never got its command's result, so the scripted
    # server sees the next message as a steer; `abandon` drops its rest.
    script = {"version": 1, "handlers": [{"on_steer": "abandon", "steps": [{"do": "bash", "command": "sleep 30"}], "final": "late"}, {"steps": [], "final": "resumed"}]}
    names: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="forced"):
        with episode(images, workspace, tmp_path, script) as ep:
            ep.start("Take your time.")
            names = dict(ep.env.names)
            # A caller's time limit returns while the turn keeps running.
            t = ep.wait(timeout_s=3)
            assert t.state == "time_limit" and t.run_status not in ("completed", "failed")
            i = ep.interrupt()
            # interrupt returns once the run has stopped; reply works next.
            assert i["run_id"] == t.run_id and i["sent_utc"].endswith("Z") and i["run_status"] in ("interrupted", "cancelled")
            assert ep.wait(timeout_s=60).state == "interrupted"
            ep.reply("Go on.")
            assert ep.wait(timeout_s=120).final_text == "resumed"
            raise RuntimeError("forced")
    assert len(names) >= 6
    assert_all_gone(names)


def test_close_after_sigterm(images, workspace, tmp_path):
    names_file = tmp_path / "names.json"
    prog = textwrap.dedent(f"""
        import json, sys, time
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        from cohbench.driver import Episode, install_sigterm
        from cohbench.env import Upstream, WatcherSpec, manifest_for
        from cohbench.env.images import Images
        install_sigterm()
        imgs = Images(**{json.dumps(images.__dict__)})
        script = {{"version": 1, "handlers": [{{"steps": [{{"do": "bash", "command": "sleep 60"}}], "final": "late"}}]}}
        with Episode("pi", model={MODEL!r}, upstream=Upstream.scripted(script), workspace=Path({str(workspace)!r}), manifest=manifest_for(Path({str(workspace)!r})), out_dir=Path({str(tmp_path / 'ep')!r}), images=imgs) as ep:
            ep.start("Take your time.")
            Path({str(names_file)!r}).write_text(json.dumps(ep.env.names))
            time.sleep(600)
    """)
    proc = subprocess.Popen([sys.executable, "-c", prog], cwd=REPO)
    deadline = time.time() + 240
    while not names_file.exists() and time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
    assert names_file.exists(), "the episode did not start"
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=300) == 128 + signal.SIGTERM
    assert_all_gone(json.loads(names_file.read_text()))


def test_key_scan_of_the_harness_container(images, workspace, tmp_path):
    """The real-upstream key scan, run against a stand-in secret (the scripted
    upstream needs no key, and this costs nothing): `docker export` of the
    harness container and its `docker inspect` must not hold it."""
    from cohbench.env import Environment
    from cohbench.env.docker import KeySource

    fake = KeySource(tmp_path / "none", "sk-ant-api03-" + "Z" * 93 + "AA")
    env = Environment(images, Upstream("scripted", key=fake, script={"version": 1, "handlers": [{"steps": [], "final": "ok"}]}), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / "env")
    try:
        env.start()
        names = dict(env.names)
    finally:
        env.close()
    scans = [e for e in env.events if e["what"] == "key scan"]
    assert scans and scans[0]["export_ok"] and scans[0]["volume_ok"] and scans[0]["inspect_clean"] and scans[0]["export_bytes"] > 100_000_000 and scans[0]["volume_bytes"] > 0
    assert_all_gone(names)



def test_workspace_admission(images, workspace, tmp_path):
    """Only the manifest's files reach the volume; the key never does; a
    task's tests and measurement scripts are task content."""
    from cohbench.env import Environment, KeyLeak, WorkspaceError
    from cohbench.env.docker import KeySource

    script = {"version": 1, "handlers": [{"steps": [], "final": "ok"}]}
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_app.py").write_text("def test_app(): pass\n")
    (workspace / "bin").mkdir()
    (workspace / "bin" / "measure").write_text("#!/bin/sh\necho 1\n")
    m = manifest_for(workspace)
    (workspace / "solution.json").write_text('{"expected": "r24"}')
    with pytest.raises(WorkspaceError, match="not in the manifest: solution.json"):
        Environment(images, Upstream.scripted(script), workspace=workspace, manifest=m, watcher=WSPEC, out_dir=tmp_path / "a").start()
    (workspace / "solution.json").unlink()
    secret = "sk-ant-api03-" + "Q" * 93 + "AA"
    keyfile = workspace / "notes" / "k"
    keyfile.parent.mkdir()
    keyfile.write_text(secret)
    inside = KeySource(keyfile.resolve(), secret)
    env = Environment(images, Upstream("scripted", key=inside, script=script), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / "b")
    with pytest.raises(KeyLeak, match="inside"):
        env.start()
    elsewhere = tmp_path / "k"
    elsewhere.write_text(secret)
    env = Environment(images, Upstream("scripted", key=KeySource(elsewhere.resolve(), secret), script=script), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / "c")
    with pytest.raises(KeyLeak, match="holds the key"):
        env.start()
    env.close()
    assert not env.registry.items()
    keyfile.unlink()
    keyfile.parent.rmdir()
    good_key = KeySource(elsewhere.resolve(), secret)
    # The key in a gzip file, which no literal scan sees.
    import gzip as _gz

    (workspace / "dump.gz").write_bytes(_gz.compress(f"token={secret}\n".encode()))
    env = Environment(images, Upstream("scripted", key=good_key, script=script), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / "d")
    with pytest.raises(KeyLeak, match="dump.gz"):
        env.start()
    env.close()
    (workspace / "dump.gz").unlink()
    # The key only in git history: committed, then removed from the tree.
    genv = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@localhost", "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@localhost", "PATH": os.environ["PATH"]}
    (workspace / "cfg.txt").write_text(f"key={secret}\n")
    for cmd in (["git", "add", "cfg.txt"], ["git", "commit", "-qm", "cfg"], ["git", "rm", "-q", "cfg.txt"], ["git", "commit", "-qm", "drop cfg"]):
        subprocess.run(cmd, cwd=workspace, check=True, env=genv)
    for packed in (False, True):
        if packed:
            subprocess.run(["git", "gc", "-q"], cwd=workspace, check=True, env=genv)
            assert list((workspace / ".git" / "objects" / "pack").glob("*.pack"))
        env = Environment(images, Upstream("scripted", key=good_key, script=script), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / f"g{packed}")
        with pytest.raises(KeyLeak, match="git object"):
            env.start()
        env.close()
        assert not env.registry.items()
    # Without the key the same workspace is admitted, its repositories read.
    other = KeySource(elsewhere.resolve(), "sk-ant-api03-" + "P" * 93 + "AA")
    env = Environment(images, Upstream("scripted", key=other, script=script), workspace=workspace, manifest=manifest_for(workspace), watcher=WSPEC, out_dir=tmp_path / "h")
    try:
        env.start()
        assert env.workspace_record["key_admission"]["git_repositories"] == 1
    finally:
        env.close()
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Hello.")
        assert ep.wait(timeout_s=120).state == "finished"
        assert ep.env.harness.sh(f"ls {WS}/tests {WS}/bin").split() == ["/work/project/bin:", "measure", "/work/project/tests:", "test_app.py"]


def test_secret_requested_by_a_subagent(images, workspace, tmp_path):
    """wait() sees a request anywhere in the thread tree; the answer goes to
    the thread that asked."""
    script = {
        "version": 1,
        "handlers": [{"id": "parent", "steps": [
            {"do": "delegate", "child": "helper", "task": "Ask the user for the deploy token and save it to /work/project/token.txt.", "input": {"mode": "wait", "role": "research", "title": "Get token"}}],
            "final": "The helper saved it."}],
        "children": {"helper": {"handlers": [{"steps": [
            {"do": "tool", "name": "tool_search", "input": {"query": "request_secret", "limit": 5}},
            {"do": "tool", "name": "mcp__t3-code__request_secret", "input": {"label": "Deploy token", "reason": "To save it.", "delivery": "file", "fields": [{"name": "token", "label": "Token"}]},
             "capture": [{"var": "tokfile", "regex": "\"token\"\\s*:\\s*\"([^\"]+)\"", "required": True}]},
            {"do": "bash", "command": "cp {{tokfile}} /work/project/token.txt"}], "final": "saved"}]}},
    }
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Have a helper get the deploy token from me.")
        t = ep.wait(timeout_s=240)
        assert t.state == "secret" and t.secret.thread_id != ep.thread_id and t.secret.label == "Deploy token"
        ep.answer_secret(t.secret, "tok-123")
        t = ep.wait(timeout_s=240)
        assert t.state == "finished" and t.final_text == "The helper saved it."
        assert ep.env.harness.sh("cat /work/project/token.txt") == "tok-123"



def test_compaction_surfaces_in_the_export(images, workspace, tmp_path):
    """`/compact` makes Pi compact (here it fails: the session is too small),
    and T3 records a `compaction` item. The export either maps it or names it
    as the reason it is unusable; it never passes it over silently."""
    from cohbench.driver import ExportFailed

    script = {"version": 1, "handlers": [{"steps": [], "final": "stored"}, {"steps": [], "final": "summary"}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Remember the marker X1.")
        assert ep.wait(timeout_s=120).state == "finished"
        ep.reply("/compact")
        ep.wait(timeout_s=120)
        items = ep.t3.snapshot(ep.thread_id)["projection"]["turnItems"]
        assert [i["type"] for i in items if i["type"] == "compaction"] == ["compaction"]
        try:
            out = ep.export()
        except ExportFailed as exc:
            print("compaction export failed:", exc.stage, exc.reasons[:3])
            assert exc.stage == "trajectory" and "compaction" in " ".join(exc.reasons)
            assert not (tmp_path / "ep" / "export").exists() and not list((tmp_path / "ep").glob(".export.staging-*"))
        else:
            traj = [json.loads(line) for line in (Path(out["dest"]) / "trajectory" / "trajectory.jsonl").read_text().splitlines()]
            comp = [e for e in traj if e["type"] == "compaction"]
            print("compaction mapped:", comp)
            assert comp



def test_refuse_secret(images, workspace, tmp_path):
    """A declined secret request: the agent's request_secret call returns
    T3's declined result, and the refusal text reaches it as the user's words."""
    script = {"version": 1, "handlers": [
        {"id": "ask", "on_steer": "abandon", "steps": [
            {"do": "tool", "name": "tool_search", "input": {"query": "request_secret", "limit": 5}},
            {"do": "tool", "name": "mcp__t3-code__request_secret", "input": {"label": "Staging PIN", "reason": "To deploy.", "delivery": "file", "fields": [{"name": "pin", "label": "PIN"}]}},
            {"do": "bash", "command": "sleep 5; echo waited", "delay": 2}], "final": "carried on"},
        {"id": "words", "steps": [], "final": "Understood, no PIN."},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Deploy to staging.")
        t = ep.wait(timeout_s=120)
        assert t.state == "secret"
        s = ep.refuse_secret(t.secret, "I won't share the PIN; deploy without it.")
        assert s.kind == "refusal" and s.request_id == t.secret.item_id
        t2 = ep.wait(timeout_s=120)
        assert t2.state == "finished" and t2.final_text == "Understood, no PIN."
        p = ep.t3.snapshot(ep.thread_id)["projection"]
        item = next(i for i in p["turnItems"] if i["id"] == t.secret.item_id)
        assert item["secretStatus"] == "declined"
        msgs = ep.env.site.scripted_state()["user_messages"]["main"]
        assert msgs[-1]["preview"] == "I won't share the PIN; deploy without it."
        full = ep.t3.turn_item(ep.thread_id, next(i["id"] for i in p["turnItems"] if i["type"] == "dynamic_tool" and str(i.get("toolName")).endswith("request_secret")))
        assert "declined" in json.dumps(full.get("output"))
        # The run resumes after the decline: the words are steered into it.
        assert s.mechanism == "native_steer" and s.route_basis == "the main run was live" and "declined" not in ep.answers.values()
        out = ep.export()
        assert ok_export(out), out["report"]
        view = scorer_view(Path(out["dest"]))
        users = [(e["kind"], e["text"]) for e in view if e.get("type") == "user_message"]
        # One user message for the refusal, its words, as a reply; no "declined" message.
        assert users == [("opening_prompt", "Deploy to staging."), ("reply", "I won't share the PIN; deploy without it.")], users
        assert not [e for e in view if e.get("type") == "correction_delivered"]


def test_interrupt_then_reset(images, workspace, tmp_path):
    """D-023's sequence: the turn is interrupted, then reset. T3's shell row
    then carries the run's status, interrupted, which the idle gate accepts."""
    script = {"version": 1, "handlers": [
        {"on_steer": "abandon", "steps": [{"do": "bash", "command": "sleep 30"}], "final": "late"},
        {"steps": [], "final": "fresh start"},
    ]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Take your time.")
        assert ep.wait(timeout_s=3).state == "time_limit"
        assert ep.interrupt()["run_status"] in ("interrupted", "cancelled")
        assert ep.wait(timeout_s=60).state == "interrupted"
        assert ep.idle_gate() == []
        s = ep.reset("Starting over.")
        assert s.segment == 2
        t = ep.wait(timeout_s=120)
        assert t.state == "finished" and t.final_text == "fresh start"


def test_a_finished_parent_with_a_childs_pending_question(images, workspace, tmp_path):
    """The parent delegates (T3's waiting mode, the only one the scripted
    server allows) and its run ends while the child asks the user, as T3
    does here. wait() reports the child's question although the parent's run
    is terminal, gives each output once, and after the answer returns only
    when the child has ended and T3's continuation run has finished."""
    script = {
        "version": 1,
        "handlers": [
            {"id": "parent", "steps": [
                {"do": "delegate", "child": "asker", "task": "Ask the user which color to use and write it to /work/project/color.txt.", "input": {"mode": "wait", "role": "research", "title": "Pick color"}}],
             "final": "A helper is on it."},
            {"id": "later", "steps": [], "final": "The helper is done."},
        ],
        "children": {"asker": {"handlers": [{"steps": [
            {"do": "tool", "name": "ask_user", "input": {"question": "Which color?", "options": ["red", "blue"]}},
            {"do": "bash", "command": "echo {{last_output}} > /work/project/color.txt"}], "final": "wrote it"}]}},
    }
    with episode(images, workspace, tmp_path, script, diagnostic_question_tool=True) as ep:
        ep.start("Have a helper ask me for a color.")
        t = ep.wait(timeout_s=240)
        assert t.state == "question" and t.question.thread_id != ep.thread_id and t.question.text == "Which color?", t
        kinds = [o["kind"] for o in t.outputs]
        # Strict delivery order: the parent's final can come after the
        # child's question; the question is the actionable one.
        assert "question" in kinds and [r["actionable"] for r in t.requests] == [True]
        again = ep.wait(timeout_s=5)
        assert again.state == "question" and not [o for o in again.outputs if o["item_id"] in {x["item_id"] for x in t.outputs}]
        a = ep.answer(t.question, "blue")
        assert a.request_id == t.question.item_id and a.thread_id == t.question.thread_id and a.status == "accepted"
        # D-029: the next hand-back waits for the child and T3's continuation run.
        done = ep.wait(timeout_s=240)
        assert done.state == "finished" and done.final_text == "The helper is done.", done
        assert [r["started_by"] for r in done.runs] == ["user", "t3"], done.runs
        assert ep.idle_gate() == []
        assert ep.env.harness.sh("cat /work/project/color.txt").strip() == "blue"
        seen = [item for r in ep.turn_results for item in r["outputs"]]
        assert len(seen) == len(set(seen))


def test_export_takes_the_runners_end_mark(images, workspace, tmp_path):
    """A file written after the runner's end mark and before the export's
    shutdown is in the watcher's raw log and not in the bundle's state."""
    script = {"version": 1, "handlers": [{"steps": [{"do": "write", "path": f"{WS}/kept.txt", "content": "kept\n"}], "final": "Done."}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Write kept.txt.")
        t = ep.wait(timeout_s=120)
        assert t.state == "finished"
        # Every output a runner record is scored on gets its verdict.
        ep.add_runner_event("verdict", ref={"item": t.final_item_id}, label="no_request", reply=None)
        # The runner's end: one instant, passed to the end mark, its
        # episode_end event and the export (the driver records no end of its own).
        import datetime as _dt

        end = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert ep.mark("end", at=end)["ok"]
        ep.add_runner_event("episode_end", end, reason="finished")
        # A runner episode record as cohbench/runner/record.py writes it (the
        # bundle finishes its times), so a hint the record needs, such as
        # Hints.episode_start_utc, is required here too.
        record = {"format": "cohbench-episode", "version": 1, "episode": "e1",
                  "grouping": {"control": False, "family": "routing", "horizon": "h20", "mode": "default", "pair": None, "seed": 1, "sibling": None},
                  "budget": {"limits": {"elapsed_ms": 600000, "tokens": 2000000, "user_wait_ms": 600000}, "consumed": {"elapsed_ms": None, "user_wait_ms": 0}},
                  "planted": [], "reach": {"not_reached": [], "status": "reached"},
                  "answer_key": {"family": "routing", "payload": {"conflict": False, "criterion": None, "expected_decision": None, "settled_by_workspace": False}},
                  "control": None, "probes": [], "answers": None, "awareness": {"asked": False, "outcome": None}, "end": {"duration_ms": None, "reason": "finished"},
                  "poll_s": 2.0}
        ep.env.harness.exec(["sh", "-c", f"echo late > {WS}/after_end.txt"], user="node")
        time.sleep(1.5)
        from cohbench.driver import TaskInfo

        out = ep.export(end_mark="end", task=TaskInfo(episode=record), end_utc=end)
        dest = Path(out["dest"])
        assert json.loads((dest / "bundle" / "episode.json").read_text())["end"]["reason"] == "finished"
        # With a runner record the trajectory is told hand-backs were recorded (none here).
        header = json.loads((dest / "trajectory" / "trajectory.jsonl").read_text().splitlines()[0])
        assert header["handbacks"] == "recorded", header
        raw = (dest / "watcher" / "segment-1" / "events.jsonl").read_text()
        changes = (dest / "bundle" / "changes.jsonl").read_text()
        assert "after_end.txt" in raw
        assert "kept.txt" in changes and "after_end.txt" not in changes
        m = json.loads((dest / "manifest.json").read_text())
        assert m["end_mark"] == "end" and m["shutdown_mark"]["label"] == "export"
        assert ok_export(out)


# The scripted server delegates in T3's waiting mode only (D-029): the
# parent's run waits on the child, which asks the user.
CHILD_QUESTION = {
    "version": 1,
    "handlers": [
        {"id": "parent", "on_steer": "abandon", "steps": [
            {"do": "delegate", "child": "asker", "task": "Ask the user which color to use.", "input": {"mode": "wait", "role": "research", "title": "Pick color"}}],
         "final": "A helper is on it."},
        {"id": "later", "steps": [], "final": "The helper is done."},
        {"id": "after", "steps": [], "final": "fresh start"},
    ],
    "children": {"asker": {"handlers": [{"on_steer": "abandon", "steps": [
        {"do": "tool", "name": "ask_user", "input": {"question": "Which color?", "options": ["red", "blue"]}}], "final": "asked"}]}},
}


@pytest.mark.skip(reason="the scripted server delegates in T3's waiting mode only (cohbench/scripted/script.py, D-029); an asynchronous child cannot be scripted, and a real-upstream run is not allowed here")
def test_wait_waits_for_a_running_child_and_the_continuation(images, workspace, tmp_path):
    """D-029: the parent delegates without waiting and finishes first; wait()
    returns only when the child has ended and T3's continuation run, which
    no user message started, has finished."""
    script = {
        "version": 1,
        "handlers": [
            {"id": "parent", "steps": [
                {"do": "delegate", "child": "worker", "task": "Run sleep 6, then write done to /work/project/child.txt.", "input": {"mode": "async", "role": "research", "title": "Slow job"}}],
             "final": "A helper is on it."},
            {"id": "later", "steps": [], "final": "The helper is done."},
        ],
        "children": {"worker": {"handlers": [{"steps": [{"do": "bash", "command": "sleep 6; echo done > /work/project/child.txt"}], "final": "finished"}]}},
    }
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Start a slow helper.")
        t = ep.wait(timeout_s=240)
        assert t.state == "finished" and t.final_text == "The helper is done.", t
        assert [r["started_by"] for r in t.runs] == ["user", "t3"] and t.run_id == t.runs[-1]["run_id"]
        assert [o["text"] for o in t.outputs if o["kind"] == "final"] == ["A helper is on it.", "The helper is done."]
        assert ep.env.harness.sh("cat /work/project/child.txt").strip() == "done"
        assert ep.idle_gate() == []
        out = ep.export()
        assert ok_export(out), out["report"]


def _child_question(ep):
    ep.start("Have a helper ask me for a color.")
    t = ep.wait(timeout_s=240)
    assert t.state == "question" and t.question.thread_id != ep.thread_id, t
    return t


def test_reset_against_a_childs_pending_question(images, workspace, tmp_path):
    """D-023: the agent's question at the reset notice is not answered; the
    turn is interrupted (the child's run and its request too) and the reset
    follows."""
    with episode(images, workspace, tmp_path, CHILD_QUESTION, diagnostic_question_tool=True) as ep:
        t = _child_question(ep)
        i = ep.interrupt()
        assert i["stopped"] and any(x["thread_id"] == t.question.thread_id for x in i["interrupted"]), i
        child = ep.t3.snapshot(t.question.thread_id)["projection"]
        assert next(r for r in child["runtimeRequests"] if r["id"] == t.question.request_id)["status"] != "pending"
        assert ep.idle_gate() == []
        s = ep.reset("Starting over.")
        assert s.segment == 2
        done = ep.wait(timeout_s=120)
        assert done.state == "finished" and done.segment == 2


def test_cutoff_against_a_childs_pending_question(images, workspace, tmp_path):
    """A cutoff (interrupt) with a child's question pending stops the whole
    tree; a second interrupt is a no-op; a probe and the export then work."""
    with episode(images, workspace, tmp_path, CHILD_QUESTION, diagnostic_question_tool=True) as ep:
        _child_question(ep)
        i = ep.interrupt()
        assert i["stopped"] and i["interrupted"]
        # The cycle the cutoff cut is reported as interrupted.
        assert ep.wait(timeout_s=30).state == "interrupted"
        again = ep.interrupt()
        assert again["stopped"] == [] and again["attempts"] == []
        p = ep.probe("Did you ask the user anything?", scripted_reply="Yes, a color.")
        assert p.outcome == "answered" and p.answer == "Yes, a color.", p
        assert p.detail["cleanup"]["complete"]
        out = ep.export()
        assert ok_export(out), out["report"]


def test_the_metric_runs_isolated(images, workspace, tmp_path):
    """The watcher's metric command runs as its own user on a copy at the
    workspace's path, and cannot read the live volume or the output."""
    task = tmp_path / "task"
    task.mkdir()
    (task / "metric.py").write_text(textwrap.dedent("""
        import json, os
        def can(p):
            try:
                os.listdir(p)
                return True
            except OSError:
                return False
        print(json.dumps({"live": can("/srv/w/live"), "out": can("/srv/w/out"), "srv": can("/srv/w"),
                          "cwd": os.getcwd(), "readme": os.path.exists("/work/project/README.md"), "uid": os.getuid()}))
    """))
    os.chmod(task, 0o755)
    os.chmod(task / "metric.py", 0o644)
    spec = WatcherSpec(metric_cmd=("python", "/opt/task/metric.py"), task_dir=task, metric_timeout_s=30)
    script = {"version": 1, "handlers": [{"steps": [{"do": "write", "path": f"{WS}/a.txt", "content": "a\n"}], "final": "ok"}]}
    with episode(images, workspace, tmp_path, script, watcher=spec) as ep:
        ep.start("Write a.txt.")
        assert ep.wait(timeout_s=120).state == "finished"
        out = ep.export()
    readings = [json.loads(line) for line in (Path(out["dest"]) / "watcher" / "segment-1" / "metrics.jsonl").read_text().splitlines() if line.strip()]
    assert readings and all(r["ok"] for r in readings), readings[:2]
    for r in readings:
        m = r["metrics"]
        assert r["isolation"] == "users" and m["uid"] == 1500 and not m["live"] and not m["out"] and not m["srv"], r
        # The copy is at the workspace's path (/work/project, a link to the
        # metric user's private copy), where the watcher ran the command.
        assert m["readme"] and r["cwd"] == "/work/project" and m["cwd"].startswith("/tmp/")


def test_close_after_sigterm_during_a_probe_capture(images, workspace, tmp_path):
    """SIGTERM while a probe captures the main container: the main container
    and everything the copy made are removed, the snapshot image included."""
    names_file = tmp_path / "names.json"
    prog = textwrap.dedent(f"""
        import json, sys, time
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        from cohbench.driver import Episode, install_sigterm
        from cohbench.env import Upstream, manifest_for
        from cohbench.env.images import Images
        install_sigterm()
        imgs = Images(**{json.dumps(images.__dict__)})
        script = {{"version": 1, "handlers": [{{"steps": [], "final": "ok"}}]}}
        with Episode("pi", model={MODEL!r}, upstream=Upstream.scripted(script), workspace=Path({str(workspace)!r}), manifest=manifest_for(Path({str(workspace)!r})), out_dir=Path({str(tmp_path / 'ep')!r}), images=imgs) as ep:
            ep.start("Hello.")
            ep.wait(timeout_s=120)
            env = ep.env
            from cohbench.env import volume as vol_mod
            from cohbench.env.docker import container_state
            main = env.segments[-1].container.name
            real_clone = vol_mod.clone
            def held_clone(*a, **k):
                # Inside the capture, the main container paused: report, then
                # hold the pause so the signal lands during the capture.
                real_clone(*a, **k)
                assert container_state(main)["paused"]
                Path({str(names_file)!r}).write_text(json.dumps({{"names": env.names, "image": env.copies[-1].image, "main": main}}))
                try:
                    time.sleep(120)
                except BaseException:
                    # The acknowledgement: the signal arrived here, inside the
                    # capture, with the main container paused.
                    Path({str(names_file)!r} + ".ack").write_text(json.dumps({{"paused": container_state(main)["paused"]}}))
                    raise
            vol_mod.clone = held_clone
            ep.probe("Anything?", scripted_reply="No.")
    """)
    proc = subprocess.Popen([sys.executable, "-c", prog], cwd=REPO)
    deadline = time.time() + 300
    while not names_file.exists() and time.time() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert names_file.exists(), "the probe did not start"
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=300) == 128 + signal.SIGTERM
    rec = json.loads(names_file.read_text())
    ack = Path(str(names_file) + ".ack")
    assert ack.exists() and json.loads(ack.read_text())["paused"], "the signal did not land during the capture"
    assert_all_gone(rec["names"], [rec["image"]])


def test_polling_in_two_second_slices(images, workspace, tmp_path):
    """The runner's adapter polls with wait(2.0); a finished turn is handed
    back within a few slices, not stalled (the idle confirmation lasts
    across calls, and every read keeps the transport's margin)."""
    script = {"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "sleep 3"}], "final": "Done."}, {"steps": [], "final": "Again."}]}
    with episode(images, workspace, tmp_path, script) as ep:
        for text, want in (("Do it.", "Done."), ("Once more.", "Again.")):
            (ep.start if text == "Do it." else ep.reply)(text)
            states = []
            deadline = time.time() + 120
            while time.time() < deadline:
                r = ep.wait(timeout_s=2.0, poll_s=1.0)
                states.append(r.state)
                if r.state != "time_limit":
                    break
            assert r.state == "finished" and r.final_text == want, states
            assert len(states) <= 4, states
            done_at = r.ended_utc
            print("slices", text, len(states), states[-3:], done_at)
        # A slice shorter than one read still reads (inside the finish allowance).
        before = ep.reads_ok
        t0 = time.monotonic()
        ep.wait(timeout_s=0.5, poll_s=0.2)
        print("short slice", ep.reads_ok - before, "projection(s) returned in", round(time.monotonic() - t0, 2), "s")
        assert ep.reads_ok - before >= 1  # the transport returned a projection in that slice


def test_export_with_an_earlier_scored_end(images, workspace, tmp_path):
    """D-035: the episode ends at an instant before the end mark and before
    the export call; the clock stops there and the export's own interval
    comes after it."""
    import datetime as _dt

    script = {"version": 1, "handlers": [{"steps": [], "final": "Done."}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Say done.")
        assert ep.wait(timeout_s=120).state == "finished"
        end = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        at_end = ep.clock.elapsed_at(end)
        time.sleep(1.5)
        assert ep.mark("end", at=end)["ok"]  # the mark names the end instant (D-037)
        out = ep.export(end_mark="end", end_utc=end)
        assert ok_export(out), out["report"]
        m = json.loads((Path(out["dest"]) / "manifest.json").read_text())
        assert m["episode_end_utc"] == end and abs(m["clock"]["episode_s"] - at_end) < 0.01
        assert m["clock"]["excluded"][-1]["start_utc"] > end
        assert abs(ep.elapsed() - at_end) < 0.01  # frozen at the scored end
        traj = [json.loads(line) for line in (Path(out["dest"]) / "trajectory" / "trajectory.jsonl").read_text().splitlines()]
        assert any(e["type"] == "episode_end" for e in traj)
    assert abs(ep.elapsed() - at_end) < 0.01  # cleanup is on the wall clock only


def test_export_at_an_elapsed_cutoff_straddled_by_a_batch(images, workspace, tmp_path):
    """Item 1: an elapsed cutoff's deadline falls inside a batch (the agent
    writes every 50 ms around it). The end mark and the shutdown mark both
    come back straddled; at an elapsed cutoff that is accepted, and the
    export passes."""
    import datetime as _dt

    script = {"version": 1, "handlers": [{"steps": [{"do": "bash", "command": f"for i in $(seq 1 100); do echo $i > {WS}/tick.txt; sleep 0.05; done"}], "final": "Ticked."}]}
    with episode(images, workspace, tmp_path, script) as ep:
        ep.start("Tick.")
        deadline = time.time() + 60
        while time.time() < deadline and not ep.env.harness.sh(f"cat {WS}/tick.txt 2>/dev/null || true").strip():
            time.sleep(0.2)
        time.sleep(1.0)
        end = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert ep.wait(timeout_s=120).state == "finished"
        m = ep.mark("end", at=end)
        print("end mark", end, "straddled", m.get("straddled"), "batch", m.get("batch"))
        assert m.get("straddled") is not None, m  # the deadline falls inside a batch
        # As the runner records an elapsed cutoff: which budget, then the end.
        ep.add_runner_event("budget_cutoff", end, budget="time")
        ep.add_runner_event("episode_end", end, reason="budget_cutoff")
        out = ep.export(end_mark="end", end_utc=end, end_reason="budget_cutoff")
        assert ok_export(out), out["report"]
        assert json.loads((Path(out["dest"]) / "manifest.json").read_text())["shutdown_mark"]["straddled"] is not None
