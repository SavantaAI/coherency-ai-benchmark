"""Logic of cohbench/env and cohbench/driver that needs no Docker."""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

from cohbench.driver import turns
from cohbench.driver.episode import Clock
from cohbench.driver.harness import HARNESSES, harness
from cohbench.env import docker as dk
from cohbench.env import workspace as wsp
from cohbench.env.checks import TELLING, T3_OWN_ARGV, _named_fields

RUN = "run:thread:t:ordinal:1"


def proj(status="running", items=(), requests=(), runs=None, providers=()):
    return {
        "runs": runs if runs is not None else [{"id": RUN, "ordinal": 1, "status": status, "startedAt": "2026-11-22T00:00:00.000Z", "completedAt": None if status == "running" else "2026-11-22T00:00:05.000Z"}],
        "turnItems": list(items),
        "runtimeRequests": list(requests),
        "providerThreads": list(providers),
        "messages": [],
    }


QUESTION_ITEM = {
    "id": "item-q", "runId": RUN, "type": "user_input_request", "status": "waiting", "requestId": "req-1", "title": "Pick",
    "questions": [{"id": "q1", "question": "Which color?", "options": [{"label": "Red", "value": "red"}, {"label": "Blue", "value": "blue"}]}],
}


# ----- turn states -----------------------------------------------------------


def test_classify_states():
    assert turns.classify(proj("running"), RUN)["kind"] == "running"
    assert turns.classify(proj("completed"), RUN)["kind"] == "finished"
    assert turns.classify(proj("failed"), RUN)["kind"] == "finished"
    pending = [{"id": "req-1", "kind": "user_input", "status": "pending"}]
    assert turns.classify(proj("running", [QUESTION_ITEM], pending), RUN)["kind"] == "question"
    # A waiting item whose request is not pending is not a question.
    assert turns.classify(proj("running", [QUESTION_ITEM], [{"id": "req-1", "kind": "user_input", "status": "resolved"}]), RUN)["kind"] == "running"
    sec = {"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "PIN", "fields": [{"name": "pin"}]}
    assert turns.classify(proj("running", [sec]), RUN)["kind"] == "secret"
    # A terminal run is finished whatever it left pending.
    assert turns.classify(proj("interrupted", [sec]), RUN)["kind"] == "finished"
    assert turns.classify(proj("failed", [QUESTION_ITEM], pending), RUN)["kind"] == "finished"
    assert turns.classify(proj(runs=[]), RUN)["kind"] == "running"


def test_pending_requests_anywhere_in_a_projection():
    child_run = "run:child:1"
    sec = {"id": "item-s", "runId": child_run, "type": "secret_request", "secretStatus": "pending", "label": "PIN"}
    p = proj("running", [sec], runs=[{"id": child_run, "status": "running"}])
    q, s_ = turns.pending_requests(p)
    assert not q and [x["id"] for x in s_] == ["item-s"]
    assert turns.secret_of(sec, "thread:child").thread_id == "thread:child"
    done = proj("completed", [sec], runs=[{"id": child_run, "status": "failed"}])
    assert turns.pending_requests(done) == ([], [])


def test_question_and_answers():
    q = turns.question_of(QUESTION_ITEM, "thread:t")
    assert q.text == "Which color?" and q.options == ["red", "blue"] and q.request_id == "req-1" and q.thread_id == "thread:t"
    assert turns.answers_for(q, "BLUE") == {"q1": "blue"}
    assert turns.answers_for(q, "Red") == {"q1": "red"}
    assert turns.answers_for(q, "green") == {"q1": "green"}
    two = {**QUESTION_ITEM, "questions": QUESTION_ITEM["questions"] + [{"id": "q2", "question": "Size?", "options": []}]}
    q2 = turns.question_of(two, "thread:t")
    with pytest.raises(ValueError):
        turns.answers_for(q2, "blue")
    assert turns.answers_for(q2, ["blue", "large"]) == {"q1": "blue", "q2": "large"}
    assert turns.answers_for(q2, {"q1": "red", "q2": "small"}) == {"q1": "red", "q2": "small"}
    with pytest.raises(ValueError):
        turns.answers_for(q2, {"q1": "red"})


def test_final_message_only_when_nothing_follows():
    items = [
        {"id": "a1", "runId": RUN, "type": "assistant_message", "text": "narration", "ordinal": 1},
        {"id": "a2", "runId": RUN, "type": "assistant_message", "text": "the end", "ordinal": 3},
        {"id": "a3", "runId": "other", "type": "assistant_message", "text": "elsewhere", "ordinal": 4},
    ]
    assert turns.final_message(proj("completed", items), RUN) == "the end"
    tool_after = items + [{"id": "c1", "runId": RUN, "type": "command_execution", "ordinal": 5}]
    assert turns.final_message(proj("completed", tool_after), RUN) is None
    sub_after = items + [{"id": "s1", "runId": RUN, "type": "subagent", "ordinal": 6}]
    assert turns.final_message(proj("completed", sub_after), RUN) is None
    # An item without an ordinal sorts last.
    no_ord = items + [{"id": "c2", "runId": RUN, "type": "dynamic_tool", "ordinal": None}]
    assert turns.final_message(proj("completed", no_ord), RUN) is None
    assert turns.final_message(proj("failed", items), RUN) is None


def test_send_to_user_text_comes_from_the_result():
    names = harness("pi").send_to_user_tools
    item = {"type": "dynamic_tool", "toolName": "mcp__t3-code__send_to_user", "status": "completed", "input": {"message": "typed"}, "output": {"structuredContent": {"sentMessage": "shown"}}}
    assert turns.is_send_to_user(item, names)
    assert turns.sent_message(item) == "shown"
    assert turns.sent_message({**item, "status": "failed"}) is None
    assert turns.sent_message({**item, "status": "running"}) is None
    assert not turns.is_send_to_user({**item, "toolName": "mcp__other__send_to_user"}, names)
    assert not turns.is_send_to_user({"type": "dynamic_tool", "toolName": "mcp__t3-code__task_status"}, names)


def test_steering_mechanism():
    assert turns.steering_mechanism({"supportsActiveSteering": True}) == "native_steer"
    assert turns.steering_mechanism({"supportsSteeringByInterruptRestart": True}) == "interrupt_restart"
    assert turns.steering_mechanism({}) == "interrupt_then_queue"


def test_idle_gate_fails_closed():
    idle = proj("completed")
    row = {"status": "completed", "pendingRuntimeRequest": None, "pendingBackgroundTasks": []}
    shell = {"t": row}
    assert turns.idle_problems({"t": idle}, shell, "t", RUN) == []
    assert turns.idle_problems({"t": proj("running")}, shell, "t", RUN)
    assert turns.idle_problems({"t": idle}, {}, "t", RUN) == ["no shell row for thread t"]
    assert turns.idle_problems({"t": idle, "c": RuntimeError("gone")}, {**shell, "c": row}, "t", RUN)
    assert turns.idle_problems({"t": idle}, RuntimeError("down"), "t", RUN)
    assert turns.idle_problems({"t": idle}, {"t": {**row, "status": "running"}}, "t", RUN)
    assert turns.idle_problems({"t": proj("completed", providers=[{"pendingBackgroundTasks": [{"id": "x"}]}])}, shell, "t", RUN)
    # A child needs its own shell row.
    assert turns.idle_problems({"t": idle, "c": idle}, shell, "t", RUN) == ["no shell row for thread c"]
    assert turns.idle_problems({"t": idle, "c": idle}, {**shell, "c": row}, "t", RUN) == []
    # The named run must be in the thread and terminal.
    assert turns.idle_problems({"t": idle}, shell, "t", "run:missing")
    assert turns.idle_problems({"t": idle}, shell, "t", None)


def _gateway_log(tmp: Path, rows: list[dict], bodies: dict[str, tuple[str, str]]) -> Path:
    (tmp / "bodies").mkdir(parents=True)
    (tmp / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for rid, (req, resp) in bodies.items():
        (tmp / "bodies" / f"{rid}.request").write_text(req)
        (tmp / "bodies" / f"{rid}.response").write_text(resp)
    return tmp


def test_refusal_binding(tmp_path):
    req = json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]})
    tool_req = json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}, {"role": "assistant", "content": "x"}, {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}]})
    refused = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}})
    ok = json.dumps({"type": "message"})
    run = {"id": RUN, "status": "failed", "startedAt": "2026-11-22T00:00:00.000Z", "completedAt": "2026-11-22T00:00:05.000Z"}
    row = lambda rid, t, status: {"id": rid, "time": f"2026-11-22T00:00:0{t}.000Z", "method": "POST", "path": "/v1/messages?beta=true", "status": status}  # noqa: E731
    log = _gateway_log(tmp_path / "a", [row("r1", 1, 400)], {"r1": (req, refused)})
    assert turns.refusal(log, run, [], ["hello"])["refused"]
    other = {"id": "o", "startedAt": "2026-11-22T00:00:00.500Z", "completedAt": None}
    assert not turns.refusal(log, run, [other], ["hello"])["refused"]
    assert not turns.refusal(log, run, [], ["something else"])["refused"]
    assert not turns.refusal(log, run, [], ["hello"], unreadable_threads=["x"])["refused"]
    # The first call succeeded, the second was refused for credit balance: refused.
    log2 = _gateway_log(tmp_path / "b", [row("r1", 1, 200), row("r2", 2, 400)], {"r1": (req, ok), "r2": (tool_req, refused)})
    assert turns.refusal(log2, run, [], ["hello"])["refused"]
    # A refusal followed by a success: not refused.
    log3 = _gateway_log(tmp_path / "c", [row("r1", 1, 400), row("r2", 2, 200)], {"r1": (req, refused), "r2": (req, ok)})
    assert not turns.refusal(log3, run, [], ["hello"])["refused"]


# ----- harness descriptions ----------------------------------------------------


def test_harness_descriptions():
    pi = harness("pi")
    assert pi.model_selection("claude-haiku-5-5") == {"instanceId": "pi", "model": "anthropic/claude-haiku-5-5", "options": [{"id": "thinking", "value": "off"}]}
    assert json.loads(pi.default_model_files("claude-haiku-5-5")["/home/node/.pi/agent/settings.json"])["defaultModel"] == "claude-haiku-5-5"
    assert pi.instruction_file == "AGENTS.md" and pi.exercised and pi.reference
    with pytest.raises(ValueError):
        pi.model_slug("gpt-x", "openai")
    cc = harness("claude")
    assert cc.model_slug("claude-haiku-5-5") == "claude-haiku-5-5" and not cc.exercised and cc.instruction_file == "CLAUDE.md"
    assert cc.default_model_files("claude-haiku-5-5") == {}  # a native harness runs at its defaults
    assert set(HARNESSES) == {"pi", "claude"}
    with pytest.raises(ValueError):
        harness("codex")


# ----- the workspace and its manifest ------------------------------------------


def _members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: m for m in tar.getmembers()}


def _ws(tmp_path: Path) -> Path:
    src = tmp_path / "ws"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    os.chmod(src / "a.txt", 0o640)
    (src / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    os.symlink("a.txt", src / "link")
    (src / "résumé.txt").write_text("r")
    (src / "sub" / ("x" * 120)).write_text("long")
    (src / "tests").mkdir()
    (src / "tests" / "test_app.py").write_text("def test(): pass\n")
    return src


def test_pack_takes_exactly_the_manifest(tmp_path):
    src = _ws(tmp_path)
    m = wsp.manifest_for(src)
    (src / ".DS_Store").write_bytes(b"host")
    packed = wsp.pack(src, m)
    t = _members(packed.tar)
    assert t["."].isdir() and t["."].uid == 1000
    assert t["./a.txt"].mode == 0o640 and t["./a.txt"].uid == 1000 and t["./run.sh"].mode == 0o755
    assert t["./link"].issym() and t["./link"].linkname == "a.txt"
    assert "./résumé.txt" in t and "./sub/" + "x" * 120 in t and "./tests/test_app.py" in t
    assert "./.DS_Store" not in t and packed.host_metadata_skipped == [".DS_Store"]
    # Anything beside the task files that the manifest does not list is refused.
    (src / "answers.json").write_text('{"expected": 42}')
    with pytest.raises(wsp.WorkspaceError, match="not in the manifest: answers.json"):
        wsp.pack(src, m)
    (src / "answers.json").unlink()
    (src / "a.txt").write_text("changed")
    with pytest.raises(wsp.WorkspaceError, match="content differs"):
        wsp.pack(src, m)
    (src / "a.txt").unlink()
    with pytest.raises(wsp.WorkspaceError, match="missing: a.txt"):
        wsp.pack(src, m)


def test_pack_from_a_tar_and_manifest_shapes(tmp_path):
    src = _ws(tmp_path)
    m = wsp.manifest_for(src)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        t.add(src, arcname=".")
    packed = wsp.pack(buf.getvalue(), m)
    assert "./résumé.txt" in _members(packed.tar)
    again = wsp.Manifest.load(json.loads(json.dumps(m.as_dict())))
    assert again.entries == m.entries
    with pytest.raises(wsp.WorkspaceError):
        wsp.Manifest.load([{"path": "../x", "size": 0, "sha256": "0"}])
    with pytest.raises(wsp.WorkspaceError):
        wsp.Manifest.load([{"path": "a"}])


def test_workspace_names_are_checked_narrowly():
    m = wsp.Manifest.load([{"path": p, "type": "dir"} for p in ("tests", "bin/measure", "scores", "src/monitor.py", "test_fix.py")])
    assert wsp.check_names(m) == []
    bad = wsp.Manifest.load([{"path": p, "type": "dir"} for p in ("answer_key.json", "user-card.md", "cohbench", "eval", "probe.txt", "scorer.py", "evaluation")])
    assert sorted(wsp.check_names(bad)) == ["answer_key.json", "cohbench", "eval", "probe.txt", "scorer.py", "user-card.md"]


def test_instruction_sentence_takes_the_workspace_time(tmp_path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "README.md").write_text("r\n")
    os.utime(src / "README.md", (1_700_000_000, 1_700_000_000))
    packed = wsp.pack(src, wsp.manifest_for(src))
    new, rec = wsp.add_instruction_sentence(packed.tar, "AGENTS.md", "Lead with the conclusion.", mtime=packed.newest_mtime)
    assert rec["existed"] is False and rec["mtime"] == packed.newest_mtime
    with tarfile.open(fileobj=io.BytesIO(new)) as t:
        assert t.extractfile("./AGENTS.md").read() == b"Lead with the conclusion.\n"
        assert t.getmember("./AGENTS.md").uid == 1000 and t.getmember("./AGENTS.md").mtime == packed.newest_mtime
    (src / "AGENTS.md").write_text("Existing rules.")
    packed = wsp.pack(src, wsp.manifest_for(src))
    new, rec = wsp.add_instruction_sentence(packed.tar, "AGENTS.md", "Lead with the conclusion.", mtime=packed.newest_mtime)
    assert rec["existed"]
    with tarfile.open(fileobj=io.BytesIO(new)) as t:
        assert t.extractfile("./AGENTS.md").read() == b"Existing rules.\n\nLead with the conclusion.\n"
    with pytest.raises(ValueError):
        wsp.add_instruction_sentence(new, "docs/AGENTS.md", "x", mtime=0)


def test_clock_excludes_infrastructure(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("cohbench.driver.episode.time.monotonic", lambda: now[0])
    c = Clock()
    assert c.elapsed() == 0.0
    c.begin()
    now[0] = 110.0
    with c.exclude("probe"):
        now[0] = 150.0
        assert c.elapsed() == pytest.approx(10.0)
        with c.exclude("nested"):
            now[0] = 160.0
    now[0] = 165.0
    assert c.elapsed() == pytest.approx(15.0)
    assert [e["reason"] for e in c.excluded] == ["probe"] and c.excluded[0]["seconds"] == pytest.approx(50.0)


# ----- names and registry --------------------------------------------------------


def test_opaque_names_and_telling_words():
    names = {dk.opaque_name() for _ in range(200)}
    assert len(names) == 200 and all(n.startswith("n") and len(n) == 13 for n in names)
    assert not any(TELLING.search(n) for n in names)
    for word in ("benchmark", "probe-copy", "watcher", "monitor", "measure", "score", "coherency", "eval", "/test"):
        assert TELLING.search(word), word
    assert T3_OWN_ARGV.match("/home/node/.t3/runtime/versions/0.1.12-nightly.20261121.5051/resource-monitor/linux-arm64/t3-resource-monitor")
    assert not T3_OWN_ARGV.match("/opt/monitor")
    line = "227 219 254:1 /docker/volumes/n0123456789ab/_data /work/project rw,relatime master:26 - ext4 /dev/vda1 rw,discard"
    assert _named_fields(line) == "/docker/volumes/n0123456789ab/_data /work/project /dev/vda1"


def test_placeholder_key_shape():
    for _ in range(200):
        k = dk.placeholder_key()
        assert k.startswith("sk-ant-api03-") and k.endswith("AA") and len(k) == 108
        assert not TELLING.search(k)


def test_registry_removes_each_independently(monkeypatch):
    calls = []

    def fake(kind, fail=()):
        def remove(name):
            calls.append((kind, name))
            if name in fail:
                raise RuntimeError("boom")
            return {kind: name, "confirmed_absent": True}
        return remove

    monkeypatch.setitem(dk._REMOVERS, "container", fake("container", fail=("c2",)))
    monkeypatch.setitem(dk._REMOVERS, "network", fake("network"))
    monkeypatch.setitem(dk._REMOVERS, "volume", fake("volume"))
    monkeypatch.setattr(dk.time, "sleep", lambda s: None)
    r = dk.Registry()
    r.add("network", "n1")
    r.add("volume", "v1")
    r.add("container", "c1")
    r.add("container", "c2")
    res = r.remove_all()
    kinds = [k for k, _ in calls]
    assert kinds.index("container") < kinds.index("volume") < kinds.index("network")
    assert [x["confirmed_absent"] for x in res] == [True, False, True, True]
    assert ("container", "c2") in r.items() and len(r.items()) == 1
    with pytest.raises(ValueError):
        r.add("thing", "x")


def test_stream_scanner_across_chunks():
    s = dk.StreamScanner({"k": b"secretvalue"})
    for chunk in (b"xxsecr", b"etval", b"uexxsecretvaluexx"):
        s.feed(chunk)
    assert s.hits["k"] == 2



def test_one_word_list_for_what_reaches_the_agent():
    from cohbench.env import MEASUREMENT_WORDS, telling_names, telling_words

    assert telling_words("Our simulated user ran the test harness; probe; eval run") == ["simulated user", "test harness", "probe", "eval"]
    assert telling_words("Run the tests, measure the latency, print the score.") == []
    assert telling_names(["tests/test_app.py", "bin/measure", "answer-key.json"]) == ["answer-key.json"]
    assert MEASUREMENT_WORDS.search("Cohbench")
    assert telling_words("Coherence of the answers") == ["coherence"]


def test_final_item_and_driver_version():
    from cohbench.driver import driver_version

    items = [{"id": "a2", "runId": RUN, "type": "assistant_message", "text": "the end", "ordinal": 3}]
    assert turns.final_item(proj("completed", items), RUN)["id"] == "a2"
    assert driver_version() == driver_version() and driver_version().startswith("cohbench-driver 3+")



def test_reserved_names_and_watcher_names():
    from cohbench.driver.harness import DRIVER_REF_NAMESPACES, instruction_files

    assert instruction_files() == ("AGENTS.md", "CLAUDE.md") and DRIVER_REF_NAMESPACES == ("refs/t3/",)
    m = wsp.Manifest.load([{"path": "docs/AGENTS.md", "size": 0, "sha256": "x"}, {"path": "README.md", "size": 0, "sha256": "x"}])
    assert wsp.reserved(m, instruction_files()) == ["docs/AGENTS.md"]


# ----- round 2: the tree-wide state model, sends, scans ---------------------------------------


def test_idle_gate_accepts_any_terminal_status_and_archived_rows():
    stopped = proj("interrupted")
    row = {"status": "interrupted", "pendingRuntimeRequest": None, "pendingBackgroundTasks": []}
    assert turns.idle_problems({"t": stopped}, {"t": row}, "t", RUN) == []
    # A question left waiting by the interrupted run is not pending work.
    left = proj("interrupted", [QUESTION_ITEM], [{"id": "req-1", "kind": "user_input", "status": "pending", "runId": RUN}])
    assert turns.idle_problems({"t": left}, {"t": row}, "t", RUN) == []
    # Activity on a row whose status is terminal still fails.
    assert turns.idle_problems({"t": stopped}, {"t": {**row, "activityRunStatus": "running"}}, "t", RUN)
    # A finished child that T3 archived has its row under archivedThreads.
    rows = turns.shell_rows({"threads": [{"id": "t", **row}], "archivedThreads": [{"id": "c", "status": "completed"}]})
    assert set(rows) == {"t", "c"} and rows["c"]["_archived"]
    assert turns.idle_problems({"t": stopped, "c": proj("completed")}, rows, "t", RUN) == []
    assert turns.idle_problems({"t": stopped, "c": proj("completed")}, {**rows, "c": {**rows["c"], "pendingRuntimeRequest": {"id": "x"}}}, "t", RUN)


CHILD = "thread:child"
CHILD_RUN = "run:child:1"


class FakeT3:
    """Enough of cohbench.driver.t3.T3 for the episode's state model."""

    def __init__(self, tree):
        self.tree_ = tree
        self.dispatched: list[dict] = []
        self.dispatch_error: Exception | None = None
        self.messages: dict[str, str] = {}

    fail_reads = False
    on_respond = None
    fail_items: set = set()
    read_delay = 0.0
    on_read = None  # called with the thread id before each snapshot

    @staticmethod
    def _write_margin(timeout_s):
        """As `Api.rpc_batch`: too little time to start the exec is an unsent
        call, nothing reached T3."""
        from cohbench.driver.t3client import NODE_MARGIN_S, RpcUnsent

        if timeout_s - NODE_MARGIN_S <= 0.1:
            raise RpcUnsent("the call was never started")

    version: dict = {}
    on_shell = None  # called with the shell read's number

    def bump(self, tid):
        """A change to a thread T3 would show as a new shell row."""
        self.version = {**self.version, tid: self.version.get(tid, 0) + 1}

    def shell(self, *, timeout_s=0):
        self._margin(timeout_s)
        self.shells = getattr(self, "shells", 0) + 1
        if self.on_shell:
            self.on_shell(self.shells)
        rows = []
        for t, p in self.tree_.items():
            if not isinstance(p, dict):
                continue
            runs = p.get("runs", [])
            rows.append({"id": t, "status": runs[-1]["status"] if runs else "idle", "itemCount": len(p.get("turnItems", [])),
                         "latestRunId": runs[-1]["id"] if runs else None,
                         "updatedAt": f"{len(runs)}/{len(p.get('turnItems', []))}/{len(p.get('subagents', []))}/{self.version.get(t, 0)}"})
        return {"threads": rows, "archivedThreads": []}

    @staticmethod
    def _margin(timeout_s):
        """The real transport refuses a read with too little time left
        (t3client.run_node): the fake does the same, so a caller that leaves
        its reads no time fails here as it would against T3."""
        from cohbench.driver.t3client import NODE_MARGIN_S, T3Timeout

        if timeout_s - NODE_MARGIN_S <= 0.1:
            raise T3Timeout(f"{timeout_s:.2f} s left, too little for a call")

    def tree(self, tid, *, timeout_s=0):
        self._margin(timeout_s)
        if self.fail_reads:
            from cohbench.driver.t3client import T3Timeout

            raise T3Timeout("read timed out")
        out = {}
        todo = [tid]
        while todo:
            t = todo.pop(0)
            if t in out or t not in self.tree_:
                continue
            out[t] = self.tree_[t]
            if isinstance(out[t], dict):
                todo += [s["childThreadId"] for s in self.tree_[t].get("subagents", []) if s.get("childThreadId")]
        return out

    def snapshot(self, tid, *, timeout_s=0):
        self._margin(timeout_s)
        if self.on_read:
            self.on_read(tid)
        if self.read_delay:
            time.sleep(self.read_delay)
        if self.fail_reads:
            from cohbench.driver.t3client import T3Timeout

            raise T3Timeout("read timed out")
        if isinstance(self.tree_[tid], Exception):
            raise self.tree_[tid]
        return {"projection": self.tree_[tid]}

    def answer_secret(self, tid, item_id, answer, *, timeout_s=0):
        self._write_margin(timeout_s)
        for i in self.tree_[tid]["turnItems"]:
            if i["id"] == item_id:
                i["secretStatus"] = "declined" if answer.get("type") == "decline" else "saved"
        self.dispatched.append({"secret": (tid, item_id, answer.get("type"))})
        if self.after_secret:
            self.after_secret()
        return {"item_id": item_id}

    after_secret = None

    def cancel_queued(self, tid, run_id, *, timeout_s=0):
        self._write_margin(timeout_s)
        for r in self.tree_[tid]["runs"]:
            if r["id"] == run_id and r["status"] == "queued":
                r["status"] = "cancelled"
        self.dispatched.append({"cancel_queued": (tid, run_id)})

    def stop_thread(self, tid, *, timeout_s=0):
        self._write_margin(timeout_s)
        for pt in self.tree_[tid].get("providerThreads", []):
            pt["pendingBackgroundTasks"] = []
        self.bump(tid)
        self.dispatched.append({"stop_thread": tid})
        if self.lose_interrupt:
            from cohbench.driver.t3client import RpcLost

            raise RpcLost("the receipt was lost")

    def interrupt(self, tid, run_id, *, hold_queue=True, cmd=None, timeout_s=0):
        self._write_margin(timeout_s)
        p = self.tree_[tid]
        for r in p["runs"]:
            if r["id"] == run_id and r["status"] not in turns.TERMINAL:
                r["status"] = "interrupted"
        for q in p.get("runtimeRequests", []):
            if q["status"] == "pending":
                q["status"] = "cancelled"
        self.dispatched.append({"interrupt": (tid, run_id)})
        if self.lose_interrupt:
            from cohbench.driver.t3client import RpcLost

            raise RpcLost("the receipt was lost")
        return {"command_id": cmd, "run_id": run_id}

    def respond(self, tid, request_id, answers, *, cmd=None, timeout_s=0):
        self._write_margin(timeout_s)
        self.dispatched.append({"respond": (tid, request_id, answers)})
        if self.on_respond:
            self.on_respond(tid, request_id, answers)
        return {"command_id": cmd, "request_id": request_id}

    def turn_item(self, tid, item_id, *, timeout_s=0):
        self._margin(timeout_s)
        if item_id in self.fail_items:
            from cohbench.driver.t3client import T3Timeout

            raise T3Timeout("item read timed out")
        return next(i for i in self.tree_[tid]["turnItems"] if i["id"] == item_id)

    def dispatch_message(self, tid, text, *, mode="queue", target_run=None, ids=None, timeout_s=0, fresh=False):
        self._write_margin(timeout_s)
        self.dispatched.append({"tid": tid, "text": text, "mode": mode, "ids": ids})
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return {"thread_id": tid, "message_id": ids[1], "command_id": ids[0]}

    lose_interrupt = False

    def launch(self, text, *, model_selection=None, title=None, ids=None, timeout_s=0):
        self._write_margin(timeout_s)
        tid = f"t{len(self.tree_) + 1}"
        self.tree_[tid] = proj("running", runs=[{"id": f"run:{tid}", "ordinal": 1, "status": "running", "userMessageId": ids[1]}])
        self.messages[ids[1]] = f"run:{tid}"
        return {"thread_id": tid, "message_id": ids[1], "command_id": ids[0], "projection": None}

    def find_message(self, mid, *, thread_id=None, timeout_s=0):
        if mid in self.messages:
            return {"thread_id": thread_id or "t", "message": {"id": mid}, "run_id": self.messages[mid]}
        return None

    def run_for_message(self, tid, mid, projection=None, timeout_s=0):
        from cohbench.driver.t3client import T3Timeout

        if mid in self.messages:
            return self.messages[mid]
        raise T3Timeout("no run yet")


def _episode(tree):
    from cohbench.driver.episode import Episode
    from cohbench.env import Upstream

    ep = Episode("pi", model="claude-haiku-5-5", upstream=Upstream.scripted({}), images=object(), workspace=b"", manifest=[], out_dir=Path("/nonexistent/x"), watcher=None)
    ep.t3 = FakeT3(tree)
    ep.thread_id, ep.run_id = "t", RUN
    ep.clock.begin()
    return ep


def _tree_with_child_question(parent_status="completed"):
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "done here", "ordinal": 2, "completedAt": "2026-11-22T00:00:05.000Z"}
    parent = {**proj(parent_status, [final]), "subagents": [{"childThreadId": CHILD}]}
    q = {**QUESTION_ITEM, "id": "item-cq", "runId": CHILD_RUN, "requestId": "req-c"}
    child = proj("running", [q], [{"id": "req-c", "kind": "user_input", "status": "pending"}], runs=[{"id": CHILD_RUN, "status": "running"}])
    return {"t": parent, CHILD: child}


def test_wait_reports_a_childs_question_after_the_parent_finished_and_each_output_once():
    tree = _tree_with_child_question()
    ep = _episode(tree)
    first = ep.wait(timeout_s=2, poll_s=0.01, finish_s=1)
    assert first.state == "question" and first.question.thread_id == CHILD and first.run_status == "completed"
    assert [(o["kind"], o["item_id"]) for o in first.outputs] == [("final", "a-final"), ("question", "item-cq")]
    again = ep.wait(timeout_s=2, poll_s=0.01, finish_s=1)
    assert again.state == "question" and again.outputs == []
    # The child's question resolved and its run ended: the parent's finish
    # is the hand-back, and its final was already given out.
    tree[CHILD] = proj("completed", runs=[{"id": CHILD_RUN, "status": "completed"}])
    done = ep.wait(timeout_s=2, poll_s=0.01, finish_s=1)
    assert done.state == "finished" and done.final_text == "done here" and done.outputs == []


def test_wait_with_a_running_parent_reports_the_question_once():
    tree = _tree_with_child_question("running")
    ep = _episode(tree)
    assert [o["kind"] for o in ep.wait(timeout_s=1, poll_s=0.01, finish_s=1).outputs] == ["question"]
    assert ep.wait(timeout_s=1, poll_s=0.01, finish_s=1).outputs == []


def test_wait_holds_to_its_bound():
    import time as _t

    ep = _episode({"t": proj("running")})
    t0 = _t.monotonic()
    res = ep.wait(timeout_s=0.3, poll_s=0.05, finish_s=0.2)
    assert res.state == "time_limit" and _t.monotonic() - t0 < 0.3 + 0.2 + 0.1


def test_a_lost_send_is_reconciled_by_its_ids():
    from cohbench.driver.episode import DriverFailure
    from cohbench.driver.t3client import RpcLost

    ep = _episode({"t": proj("completed")})
    ep.t3.dispatch_error = RpcLost("no answer")
    real = ep.t3.dispatch_message

    def lost_but_recorded(tid, text, **kw):
        ep.t3.messages[kw["ids"][1]] = "run:2"
        return real(tid, text, **kw)

    ep.t3.dispatch_message = lost_but_recorded
    s = ep.reply("next", timeout_s=3)
    assert s.status == "accepted" and s.run_id == "run:2" and ep.sent[-1] is s and s.message_id == ep.t3.dispatched[-1]["ids"][1]
    # Never recorded by T3: unknown at the deadline is a driver failure.
    ep2 = _episode({"t": proj("completed")})
    ep2.t3.dispatch_error = RpcLost("no answer")
    with pytest.raises(DriverFailure):
        ep2.reply("next", timeout_s=4)
    assert ep2.sent[-1].status == "unknown" and ep2.failure
    with pytest.raises(DriverFailure):
        ep2.reply("again", timeout_s=1)


def test_a_steer_rejected_because_the_run_ended_is_turn_not_running():
    from cohbench.driver.episode import TurnNotRunning
    from cohbench.driver.t3client import RpcFailure

    tree = {"t": {**proj("running"), "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]}}
    ep = _episode(tree)

    def ended_then_rejected(tid, text, **kw):
        tree["t"] = {**tree["t"], "runs": [{**tree["t"]["runs"][0], "status": "completed"}]}
        raise RpcFailure("orchestration.dispatchCommand", {"message": "run is not active"})

    ep.t3.dispatch_message = ended_then_rejected
    with pytest.raises(TurnNotRunning):
        ep.correct("stop", timeout_s=3)
    assert ep.sent[-1].kind == "correction" and ep.sent[-1].status == "rejected"


def test_answer_secret_refuses_the_upstream_key():
    from cohbench.driver.episode import EpisodeError

    ep = _episode({"t": proj("running")})
    secret = "sk-ant-api03-" + "Q" * 93 + "AA"
    ep.upstream = type(ep.upstream)("scripted", key=dk.KeySource(Path("/nonexistent/k"), secret))
    req = turns.secret_of({"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "token"}, "t")
    with pytest.raises(EpisodeError, match="refused"):
        ep.answer_secret(req, f"prefix-{secret}")
    assert not any(s.kind == "secret" for s in ep.sent)


def test_clock_exclusion_yields_its_start():
    c = Clock()
    c.begin()
    with c.exclude("probe") as started:
        time.sleep(0.002)  # a zero-length interval is not recorded
    assert started == c.excluded[0]["start_utc"]


def test_decoded_key_hits(tmp_path):
    import bz2
    import gzip
    import lzma
    import zipfile

    key = dk.KeySource(tmp_path / "k", "sk-ant-api03-" + "W" * 93 + "AA")
    raw = b"x" + key.secret.encode() + b"y"
    assert dk.decoded_key_hits("plain", raw, key) == ["plain"]
    assert dk.decoded_key_hits("a.gz", gzip.compress(raw), key) == ["a.gz (gzip)"]
    assert dk.decoded_key_hits("a.bz2", bz2.compress(raw), key) == ["a.bz2 (bzip2)"]
    assert dk.decoded_key_hits("a.xz", lzma.compress(raw), key) == ["a.xz (xz)"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("inner/cfg.txt", raw)
    assert dk.decoded_key_hits("a.zip", buf.getvalue(), key) == ["a.zip!inner/cfg.txt"]
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as t:
        info = tarfile.TarInfo("cfg")
        info.size = len(raw)
        t.addfile(info, io.BytesIO(raw))
    assert "a.tgz!cfg" in dk.decoded_key_hits("a.tgz", tbuf.getvalue(), key)
    assert dk.decoded_key_hits("clean", gzip.compress(b"nothing"), key) == []
    # A file that claims to be gzip and is not fails closed.
    assert dk.decoded_key_hits("bad.gz", b"\x1f\x8bnot gzip", key)[0].startswith("bad.gz: undecodable")


def test_write_scanned_writes_nothing_on_a_hit(tmp_path):
    key = dk.KeySource(tmp_path / "k", "sk-ant-api03-" + "V" * 93 + "AA")
    with pytest.raises(dk.KeyLeak):
        dk.write_scanned(tmp_path / "out.txt", "log line " + key.secret, [key])
    assert list(tmp_path.iterdir()) == []
    dk.write_scanned(tmp_path / "out.txt", "clean", [key, None])
    assert (tmp_path / "out.txt").read_text() == "clean" and [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_symlink_targets_are_checked_like_names(tmp_path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "notes.md").symlink_to("/Users/someone/user-card.md")
    assert wsp.check_names(wsp.manifest_for(src)) == ["notes.md -> /Users/someone/user-card.md"]


def test_the_manifest_time_is_the_workspace_time(tmp_path):
    """D-027: one mtime for the whole workspace, from the manifest; the
    host's times do not reach the tar."""
    src = tmp_path / "ws"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("a")
    os.symlink("sub/a.txt", src / "link")
    m = wsp.manifest_for(src, mtime=1_650_000_000)
    first = wsp.pack(src, m)
    os.utime(src / "sub" / "a.txt", (1_800_000_000, 1_800_000_000))
    os.utime(src / "sub", (1_900_000_000, 1_900_000_000))
    second = wsp.pack(src, m)
    assert first.tar == second.tar and first.newest_mtime == 1_650_000_000
    with tarfile.open(fileobj=io.BytesIO(second.tar)) as t:
        assert {x.mtime for x in t.getmembers()} == {1_650_000_000}
    new, rec = wsp.add_instruction_sentence(second.tar, "AGENTS.md", "x", mtime=second.newest_mtime)
    assert rec["mtime"] == 1_650_000_000
    # A manifest without the time is refused; a loaded one keeps it.
    with pytest.raises(wsp.WorkspaceError, match="no mtime"):
        wsp.pack(src, wsp.Manifest.load([e.as_dict() for e in m.entries.values()]))
    assert wsp.Manifest.load(m.as_dict()).mtime == 1_650_000_000
    with pytest.raises(wsp.WorkspaceError, match="epoch seconds"):
        wsp.Manifest.load({**m.as_dict(), "mtime": "yesterday"})


def test_the_integration_mark_expression_is_parsed():
    from tests.driver.conftest import _selects_integration

    assert _selects_integration("integration")
    assert _selects_integration("integration and not slow")
    assert not _selects_integration("not integration")
    assert not _selects_integration("")
    assert not _selects_integration("nonintegration")


def test_a_deadline_caps_docker_commands(monkeypatch):
    import time as _t

    with dk.deadline(_t.monotonic() - 1):
        with pytest.raises(dk.DeadlinePassed):
            dk.run(["version"])
    with dk.deadline(_t.monotonic() + 5):
        assert dk.bounded(600) <= 5
        with dk.no_deadline():
            assert dk.bounded(600) == 600


def test_correction_delivery_says_when_no_mark_was_taken():
    ep = _episode({"t": proj("running")})
    s = ep._new_sent("correction", "t", text="stop")
    ep.corrections.append({"message_id": s.message_id, "requested": "native_steer", "observed": None, "run_before": RUN, "run_after": None})
    assert ep.correction_delivery(s).delivered_utc is None
    item = {"id": f"msg:{s.message_id}", "type": "user_message", "runId": RUN, "completedAt": "2026-11-22T00:00:03.000Z"}
    ep.t3.tree_["t"] = proj("running", [item])
    ep.wait(timeout_s=0.2, poll_s=0.05, finish_s=0.1)
    d = ep.correction_delivery(s)
    assert d.delivered_utc == "2026-11-22T00:00:03.000Z" and not d.mark_taken and d.mark_error == "no watcher in this episode" and s.delivery == d



def _parent_done_child_running():
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "A helper is on it.", "ordinal": 2, "completedAt": "2026-11-22T00:00:05.000Z"}
    parent = {**proj("completed", [final]), "subagents": [{"childThreadId": CHILD}], "messages": [{"id": "m1", "createdBy": "user"}]}
    parent["runs"][0]["userMessageId"] = "m1"
    child = proj("running", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "running"}])
    return {"t": parent, CHILD: child}


def test_wait_keeps_waiting_while_a_child_runs_and_through_the_continuation():
    tree = _parent_done_child_running()
    ep = _episode(tree)
    t = ep.wait(timeout_s=0.4, poll_s=0.05)
    # Not a hand-back: the parent's final is out, the state is time_limit.
    assert t.state == "time_limit" and [o["item_id"] for o in t.outputs] == ["a-final"]
    # The child ended and T3 reserved a continuation not yet dispatched.
    tree[CHILD]["runs"][0]["status"] = "completed"
    tree["t"]["runs"][0]["delegatedCompletion"] = {"disposition": "open", "nextGeneration": 2, "delivery": {"generation": 1, "messageId": "m2", "taskIds": ["x"]}}
    assert ep.wait(timeout_s=0.4, poll_s=0.05).state == "time_limit"
    # T3's own message starts the parent's second run, which completes.
    tree["t"]["messages"].append({"id": "m2", "createdBy": "agent", "delegatedCompletion": {"parentRunId": RUN}})
    tree["t"]["runs"].append({"id": "run:2", "ordinal": 2, "status": "completed", "userMessageId": "m2"})
    tree["t"]["turnItems"].append({"id": "a2", "runId": "run:2", "type": "assistant_message", "text": "The helper is done.", "ordinal": 3})
    done = ep.wait(timeout_s=3, poll_s=0.05)
    assert done.state == "finished" and done.final_text == "The helper is done." and done.run_id == "run:2"
    assert [r["started_by"] for r in done.runs] == ["user", "t3"] and [o["item_id"] for o in done.outputs] == ["a2"]


def test_interrupt_stops_the_whole_tree_and_is_a_no_op_after():
    tree = _tree_with_child_question()
    ep = _episode(tree)
    i = ep.interrupt(timeout_s=5)
    assert i["stopped"] and i["interrupted"] == [{"thread_id": CHILD, "run_id": CHILD_RUN}]
    assert tree[CHILD]["runtimeRequests"][0]["status"] == "cancelled"
    again = ep.interrupt(timeout_s=5)
    assert again["stopped"] == [] and again["attempts"] == [] and again["interrupted"] == []


def test_a_read_failure_is_not_turn_not_running():
    from cohbench.driver.episode import DriverFailure, TurnNotRunning

    ep = _episode({"t": proj("running")})
    ep.t3.fail_reads = True
    with pytest.raises(DriverFailure) as exc:
        ep.correct("stop", timeout_s=2)
    assert not isinstance(exc.value, TurnNotRunning) and not exc.value.fatal and ep.failure is None
    ep.t3.fail_reads = False
    ep.t3.tree_["t"] = proj("completed")
    with pytest.raises(TurnNotRunning):
        ep.correct("stop", timeout_s=2)


def test_an_answer_counts_only_when_resolved_with_the_values_sent():
    from cohbench.driver.episode import EpisodeError

    tree = {"t": proj("running", [QUESTION_ITEM], [{"id": "req-1", "kind": "user_input", "status": "pending"}])}
    ep = _episode(tree)
    q = turns.question_of(QUESTION_ITEM, "t")
    req = tree["t"]["runtimeRequests"][0]

    def cancel(tid, rid, answers):
        req["status"] = "cancelled"

    ep.t3.on_respond = cancel
    with pytest.raises(EpisodeError, match="cancelled"):
        ep.answer(q, "blue", timeout_s=3)
    assert ep.sent[-1].status == "rejected"
    req["status"] = "pending"
    ep.t3.on_respond = lambda tid, rid, answers: req.update(status="resolved", answers={"q1": "red"})
    with pytest.raises(EpisodeError, match="other answers"):
        ep.answer(q, "blue", timeout_s=3)
    req.update(status="pending", answers=None)
    ep.t3.on_respond = lambda tid, rid, answers: req.update(status="resolved", answers={"q1": {"answers": ["blue"]}})
    a = ep.answer(q, "Blue", timeout_s=3)
    # What was sent: the option's value for a label given in another case.
    assert a.status == "accepted" and a.submitted == {"q1": "blue"}


def test_every_outbound_text_is_checked_for_the_credential():
    from cohbench.driver.episode import EpisodeError

    ep = _episode({"t": proj("completed")})
    secret = "sk-ant-api03-" + "R" * 93 + "AA"
    ep.upstream = type(ep.upstream)("scripted", key=dk.KeySource(Path("/nonexistent/k"), secret))
    for call in (lambda: ep.reply(f"here: {secret}"), lambda: ep.correct(secret), lambda: ep.probe(secret, scripted_reply="x")):
        with pytest.raises(EpisodeError, match="credential"):
            call()
    assert not ep.sent and not ep.t3.dispatched


def test_clock_intervals_are_microseconds_and_never_empty(monkeypatch):
    c = Clock()
    c.begin()
    with c.exclude("probe"):
        pass
    if c.excluded:
        e = c.excluded[0]
        assert e["end_utc"] > e["start_utc"] and len(e["start_utc"]) == len("2026-11-22T00:00:00.000000Z")
    import cohbench.driver.episode as em

    monkeypatch.setattr(em, "utc_us", lambda: "2026-11-22T00:00:00.000001Z")
    with c.exclude("same instant"):
        pass
    assert all(e["reason"] != "same instant" for e in c.excluded)


def test_a_member_name_holding_the_key_is_named_by_position(tmp_path):
    key = dk.KeySource(tmp_path / "k", "sk-ant-api03-" + "T" * 93 + "AA")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(f"notes-{key.secret}.txt")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    found = dk.tar_member_hits(buf.getvalue(), key)
    assert found == ["member 0 (its name)"] and not any(key.secret in f for f in found)



def test_short_slices_still_reach_the_hand_back():
    """The runner polls in 2 s slices (and shorter); the idle confirmation
    lasts across calls, and every read keeps the transport's margin."""
    for slice_s in (2.0, 0.75):
        ep = _episode({"t": proj("completed", [{"id": "a1", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 1}])})
        states = []
        for _ in range(6):
            r = ep.wait(timeout_s=slice_s, poll_s=min(1.0, max(slice_s / 2, 0.2)))
            states.append(r.state)
            if r.state != "time_limit":
                break
        assert states[-1] == "finished" and len(states) <= 3, (slice_s, states)
        assert [item for t in ep.turn_results for item in t["outputs"]] == ["a1"]


def test_a_change_resets_the_idle_confirmation():
    tree = {"t": proj("completed")}
    ep = _episode(tree)
    assert ep.wait(timeout_s=0.3, poll_s=0.1, finish_s=1).state == "time_limit"
    tree["t"]["turnItems"].append({"id": "late", "runId": RUN, "type": "assistant_message", "text": "x", "ordinal": 9})
    t0 = time.monotonic()
    r = ep.wait(timeout_s=3, poll_s=0.1, finish_s=1)
    assert r.state == "finished" and time.monotonic() - t0 >= 0.9  # confirmed afresh after the change


def test_every_pending_request_is_returned_with_one_actionable():
    tree = _tree_with_child_question("running")
    sec = {"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "PIN", "startedAt": "2026-11-22T00:00:09.000Z"}
    tree["t"]["turnItems"].append(sec)
    tree[CHILD]["turnItems"][0]["startedAt"] = "2026-11-22T00:00:01.000Z"
    ep = _episode(tree)
    r = ep.wait(timeout_s=1, poll_s=0.05, finish_s=1)
    assert [(x["kind"], x["actionable"]) for x in r.requests] == [("question", True), ("secret", False)]
    assert r.state == "question" and [o["kind"] for o in r.outputs if o["kind"] in ("question", "secret")] == ["question", "secret"]
    # Every request output, marked or not, carries its T3 item.
    assert all(o["raw"] and o["raw"]["id"] == o["item_id"] for o in r.outputs)
    again = ep.wait(timeout_s=1, poll_s=0.05, finish_s=1)
    assert [x["new"] for x in again.requests] == [False, False] and again.outputs == []


def test_the_steer_gate_reads_the_whole_tree():
    from cohbench.driver.episode import MainRunEnded, TurnNotRunning

    caps = [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]
    tree = _tree_with_child_question("running")
    tree["t"]["providerSessions"] = caps
    ep = _episode(tree)
    with pytest.raises(TurnNotRunning):  # a child's question is pending: handed back, though the main run lives
        ep.correct("stop", timeout_s=2)
    tree = _parent_done_child_running()
    ep = _episode(tree)
    with pytest.raises(MainRunEnded) as exc:
        ep.correct("stop", timeout_s=2)
    assert exc.value.retryable and not isinstance(exc.value, TurnNotRunning) and not ep.sent


def test_background_work_and_queued_runs_are_busy_and_stopped():
    tree = {"t": proj("completed", providers=[{"pendingBackgroundTasks": [{"id": "bg"}]}])}
    ep = _episode(tree)
    assert ep.wait(timeout_s=0.5, poll_s=0.1, finish_s=1).state == "time_limit"
    i = ep.interrupt(timeout_s=5)
    assert {"stop_thread": "t"} in ep.t3.dispatched and i["stopped"]
    tree2 = {"t": proj("completed")}
    tree2["t"]["runs"].append({"id": "run:q", "ordinal": 2, "status": "queued"})
    ep2 = _episode(tree2)
    ep2.interrupt(timeout_s=5)
    assert {"cancel_queued": ("t", "run:q")} in ep2.t3.dispatched
    # The cycle the interrupt cut is reported as interrupted, though the main run had completed.
    assert ep2.wait(timeout_s=3, poll_s=0.1, finish_s=1).state == "interrupted"


def test_after_the_decline_a_failed_read_leads_to_another_sample():
    tree = {"t": {**proj("running", [{"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "PIN"}]),
                  "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]}}
    ep = _episode(tree)
    req = turns.secret_of(tree["t"]["turnItems"][0], "t")
    reads = {"n": 0}
    real_snapshot = ep.t3.snapshot

    def flaky(tid, *, timeout_s=0):
        # The first tree read after the decline fails (the decline's own
        # check reads the secret's thread first; skip that one).
        if ep.t3.dispatched and any("secret" in d for d in ep.t3.dispatched):
            reads["n"] += 1
            if reads["n"] == 2:
                from cohbench.driver.t3client import T3Timeout

                raise T3Timeout("read timed out")
        return real_snapshot(tid, timeout_s=timeout_s)

    ep.t3.snapshot = flaky
    s = ep.refuse_secret(req, "No PIN.", timeout_s=8)
    # The failed read leads to another sample, which shows the run live: steered.
    assert s.kind == "refusal" and s.mechanism == "native_steer" and ep.t3.dispatched[-1]["mode"] == "steer" and s.route_basis == "the main run was live"


def test_an_unread_payload_holds_the_hand_back():
    names = harness("pi").send_to_user_tools
    full = {"id": "stu", "runId": RUN, "type": "dynamic_tool", "toolName": names[0], "status": "completed", "output": {"structuredContent": {"sentMessage": "hi"}}, "ordinal": 1}
    projected = {k: v for k, v in full.items() if k != "output"} | {"outputOmitted": True}
    ep = _episode({"t": proj("completed", [projected])})
    ep.t3.turn_item = lambda tid, item_id, timeout_s=0: (_ for _ in ()).throw(__import__("cohbench.driver.t3client", fromlist=["T3Timeout"]).T3Timeout("x")) if ep.t3.fail_items else full
    ep.t3.fail_items = {"stu"}
    r = ep.wait(timeout_s=2, poll_s=0.1, finish_s=1)
    assert r.state == "time_limit" and r.unresolved == ["stu"]
    ep.t3.fail_items = set()
    r = ep.wait(timeout_s=2, poll_s=0.1, finish_s=1)
    assert r.state == "finished" and [o["item_id"] for o in r.outputs] == ["stu"]


def test_a_payload_in_the_projection_is_used_and_a_lost_one_ends_the_wait():
    from cohbench.driver.episode import UNRESOLVED_LIMIT, DriverFailure

    names = harness("pi").send_to_user_tools
    item = {"id": "stu", "runId": RUN, "type": "dynamic_tool", "toolName": names[0], "status": "completed", "output": {"structuredContent": {"sentMessage": "hi"}}, "ordinal": 1}
    ep = _episode({"t": proj("completed", [item])})
    ep.t3.fail_items = {"stu"}  # the full read fails, the projected payload is enough
    r = ep.wait(timeout_s=3, poll_s=0.1, finish_s=1)
    assert r.state == "finished" and [o["text"] for o in r.outputs if o["kind"] == "message"] == ["hi"]
    lost = {k: v for k, v in item.items() if k != "output"} | {"id": "stv", "outputOmitted": True}
    ep2 = _episode({"t": proj("completed", [lost])})
    ep2.t3.fail_items = {"stv"}
    with pytest.raises(DriverFailure, match="stv") as exc:
        for _ in range(UNRESOLVED_LIMIT + 2):
            assert ep2.wait(timeout_s=0.3, poll_s=0.1).state == "time_limit"
    assert exc.value.fatal

def test_a_tree_that_stays_unreadable_is_a_driver_failure():
    from cohbench.driver.episode import DriverFailure

    ep = _episode({"t": proj("completed"), CHILD: RuntimeError("gone")})
    ep.t3.tree_["t"]["subagents"] = [{"childThreadId": CHILD}]
    with pytest.raises(DriverFailure) as exc:
        for _ in range(20):
            assert ep.wait(timeout_s=0.3, poll_s=0.05, finish_s=1).state == "time_limit"
    assert exc.value.fatal


def test_reserved_names_at_any_depth():
    m = wsp.Manifest.load([{"path": "AGENTS.md", "type": "dir"}, {"path": "AGENTS.md/notes.txt", "size": 0, "sha256": "x"}])
    assert wsp.reserved(m, ("AGENTS.md",)) == ["AGENTS.md", "AGENTS.md/notes.txt"]



def test_packing_takes_everything_from_the_manifest(tmp_path):
    """D-031: modes, types and targets from the manifest, never the host;
    every directory listed."""
    src = tmp_path / "ws"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f.txt").write_text("x")
    m = wsp.manifest_for(src, mtime=1_650_000_000)
    os.chmod(src / "d", 0o700)  # the host's mode changes; the manifest's stays
    os.chmod(src / "d" / "f.txt", 0o600)
    with tarfile.open(fileobj=io.BytesIO(wsp.pack(src, m).tar)) as t:
        assert t.getmember("./d").mode == m.entries["d"].mode and t.getmember("./d/f.txt").mode == m.entries["d/f.txt"].mode
    rows = [e.as_dict() for e in m.entries.values() if e.path != "d"]
    with pytest.raises(wsp.WorkspaceError, match="not listed as a directory"):
        wsp.pack(src, wsp.Manifest.load({"mtime": 1, "entries": rows}))
    no_mode = [{**e.as_dict(), "mode": None} if e.path == "d" else e.as_dict() for e in m.entries.values()]
    with pytest.raises(wsp.WorkspaceError, match="gives no mode"):
        wsp.pack(src, wsp.Manifest.load({"mtime": 1, "entries": no_mode}))


def test_the_steer_gate_defers_on_an_unreadable_child():
    from cohbench.driver.episode import DriverFailure, TurnNotRunning

    tree = {"t": {**proj("running"), "subagents": [{"childThreadId": CHILD}], "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]},
            CHILD: RuntimeError("unreadable")}
    ep = _episode(tree)
    with pytest.raises(DriverFailure) as exc:
        ep.correct("stop", timeout_s=3)
    assert not exc.value.fatal and not isinstance(exc.value, TurnNotRunning) and not ep.sent


def test_the_clock_stops_at_an_earlier_scored_end():
    import datetime as _dt

    c = Clock()
    c.begin()
    time.sleep(0.05)
    end = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    time.sleep(0.2)
    with c.exclude("export"):
        time.sleep(0.01)
    c.freeze(end)
    frozen = c.elapsed()
    time.sleep(0.05)
    assert c.elapsed() == frozen and 0.04 <= frozen <= 0.15


def test_a_short_slice_makes_a_read():
    ep = _episode({"t": proj("running")})
    before = ep.reads
    # No polling time at all: the one read still happens, inside the finish allowance.
    ok_before = ep.reads_ok
    assert ep.wait(timeout_s=0, poll_s=0.01).state == "time_limit"
    assert ep.reads == before + 1 and ep.reads_ok == ok_before + 1  # one read, and it returned a projection



def test_a_send_near_the_deadline_is_unsent_and_not_fatal():
    from cohbench.driver.episode import DriverFailure

    tree = {"t": {**proj("running"), "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]}}
    ep = _episode(tree)
    real_tree = ep._tree

    def slow_tree(at):
        # The read leaves less than the transport needs to start the dispatch.
        out = real_tree(at)
        time.sleep(1.0)
        return out

    ep._tree = slow_tree
    with pytest.raises(DriverFailure) as exc:
        ep.correct("stop", timeout_s=2.5)
    assert not exc.value.fatal and getattr(exc.value, "unsent", False) and ep.failure is None
    assert ep.sent[-1].status == "unsent" and ep.corrections == [] and not [d for d in ep.t3.dispatched if "mode" in d]


def test_an_accepted_message_without_a_run_keeps_the_tree_busy():
    final = {"id": "a1", "runId": RUN, "type": "assistant_message", "text": "turn 1", "ordinal": 1}
    tree = {"t": proj("completed", [final])}
    ep = _episode(tree)
    assert ep.wait(timeout_s=3, poll_s=0.1).state == "finished"
    s = ep.reply("next", timeout_s=4)
    assert s.run_id is None
    r = ep.wait(timeout_s=1.5, poll_s=0.1)
    assert r.state == "time_limit" and ep.idle_gate()  # never handed back on turn 1's run
    ep.t3.messages[s.message_id] = "run:2"
    tree["t"]["messages"] = [{"id": s.message_id, "runId": "run:2", "createdBy": "user"}]
    tree["t"]["runs"].append({"id": "run:2", "ordinal": 2, "status": "completed", "userMessageId": s.message_id})
    tree["t"]["turnItems"].append({"id": "a2", "runId": "run:2", "type": "assistant_message", "text": "turn 2", "ordinal": 2})
    r = ep.wait(timeout_s=3, poll_s=0.1)
    assert r.state == "finished" and r.run_id == "run:2" and r.final_text == "turn 2"


def test_outputs_after_an_unresolved_payload_wait_for_it():
    names = harness("pi").send_to_user_tools
    full = {"id": "stu", "runId": RUN, "type": "dynamic_tool", "toolName": names[0], "status": "completed", "output": {"structuredContent": {"sentMessage": "hi"}},
            "ordinal": 1, "completedAt": "2026-11-22T00:00:01.000Z"}
    projected = {k: v for k, v in full.items() if k != "output"} | {"outputOmitted": True}
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 2, "completedAt": "2026-11-22T00:00:05.000Z"}
    ep = _episode({"t": proj("completed", [projected, final])})
    real = ep.t3.turn_item
    ep.t3.fail_items = {"stu"}
    r = ep.wait(timeout_s=2, poll_s=0.1)
    assert r.unresolved == ["stu"] and r.outputs == []  # the later final is held
    ep.t3.fail_items = set()
    ep.t3.turn_item = lambda tid, item_id, timeout_s=0: full if item_id == "stu" else real(tid, item_id, timeout_s=timeout_s)
    r = ep.wait(timeout_s=3, poll_s=0.1)
    assert r.state == "finished" and [o["item_id"] for o in r.outputs] == ["stu", "a-final"]


def test_forty_finished_children_with_two_second_slices():
    """Settled children are read once and then only checked through T3's
    shell rows, so the tree read does not grow with them."""
    final = {"id": "a1", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 1}
    kids = {f"c{n}": proj("completed", runs=[{"id": f"run:c{n}", "ordinal": 1, "status": "completed"}]) for n in range(40)}
    tree = {"t": {**proj("completed", [final]), "subagents": [{"childThreadId": k} for k in kids]}, **kids}
    ep = _episode(tree)
    ep.t3.read_delay = 0.5
    states = []
    for _ in range(8):
        r = ep.wait(timeout_s=2.0, poll_s=1.0)
        states.append(r.state)
        if r.state != "time_limit":
            break
    assert states[-1] == "finished", states
    assert len(ep._tree_cache) == 40


def test_a_continuation_started_while_the_children_are_read_is_seen():
    tree = _parent_done_child_running()
    ep = _episode(tree)
    flipped = {"done": False}

    def race(tid):
        # The child ends and T3 starts the parent's continuation after the
        # parent was read and before the child is.
        if tid == CHILD and not flipped["done"]:
            flipped["done"] = True
            tree[CHILD]["runs"][0]["status"] = "completed"
            tree["t"]["runs"].append({"id": "run:cont", "ordinal": 2, "status": "running", "userMessageId": "m2"})

    ep.t3.on_read = race
    i = ep.interrupt(timeout_s=8)
    assert {"thread_id": "t", "run_id": "run:cont"} in i["interrupted"], i


def test_idle_confirmation_starts_again_after_an_unreadable_read():
    tree = {"t": {**proj("completed"), "subagents": [{"childThreadId": CHILD}]}, CHILD: proj("completed", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "completed"}])}
    ep = _episode(tree)
    assert ep.wait(timeout_s=0.2, poll_s=0.1).state == "time_limit"  # idle seen
    tree[CHILD] = RuntimeError("unreadable")
    ep._tree_cache.clear()
    assert ep.wait(timeout_s=0.2, poll_s=0.1).state == "time_limit"
    tree[CHILD] = proj("completed", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "completed"}])
    t0 = time.monotonic()
    r = ep.wait(timeout_s=3, poll_s=0.1)
    assert r.state == "finished" and time.monotonic() - t0 >= 0.9  # confirmed afresh


def test_the_steer_gate_defers_inside_the_capability_wait():
    from cohbench.driver.episode import DriverFailure, TurnNotRunning

    tree = {"t": {**proj("running"), "subagents": [{"childThreadId": CHILD}]}, CHILD: proj("running", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "running"}])}
    ep = _episode(tree)
    calls = {"n": 0}

    def later(tid):
        # Capabilities arrive on the second read, with the child unreadable.
        if tid == "t":
            calls["n"] += 1
            if calls["n"] >= 3:
                tree["t"]["providerSessions"] = [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]
                tree[CHILD] = RuntimeError("unreadable")

    ep.t3.on_read = later
    with pytest.raises(DriverFailure) as exc:
        ep.correct("stop", timeout_s=6)
    assert not exc.value.fatal and not isinstance(exc.value, TurnNotRunning) and not ep.sent and calls["n"] >= 3



def test_a_child_added_while_the_tree_is_read_is_not_missed():
    """F1: a live child appearing during the sample makes it inconsistent;
    the next sample reads the child and interrupt stops it."""
    tree = {"t": proj("completed")}
    ep = _episode(tree)

    def add_child(n):
        if n == 2 and "kid" not in tree:
            tree["kid"] = proj("running", runs=[{"id": "run:kid", "ordinal": 1, "status": "running"}])
            tree["t"]["subagents"] = [{"childThreadId": "kid"}]
            ep.t3.bump("t")

    ep.t3.on_shell = add_child
    i = ep.interrupt(timeout_s=8)
    assert {"thread_id": "kid", "run_id": "run:kid"} in i["interrupted"], i


def test_a_cached_child_that_restarts_during_a_sample_is_not_missed():
    """F1: a settled, cached child restarts between the sample's shell reads:
    the sample is discarded and wait does not hand back."""
    kid = proj("completed", runs=[{"id": "run:kid", "ordinal": 1, "status": "completed"}])
    tree = {"t": {**proj("completed"), "subagents": [{"childThreadId": "kid"}]}, "kid": kid}
    ep = _episode(tree)
    assert ep.wait(timeout_s=0.3, poll_s=0.1).state == "time_limit"
    assert "kid" in ep._tree_cache

    def restart(n):
        if tree["kid"]["runs"][-1]["status"] == "completed" and n > ep.t3.shells_at_start:
            tree["kid"]["runs"].append({"id": "run:kid2", "ordinal": 2, "status": "running"})
            ep.t3.bump("kid")

    ep.t3.shells_at_start = ep.t3.shells + 1
    ep.t3.on_shell = restart
    assert ep.wait(timeout_s=3, poll_s=0.1).state == "time_limit"


def test_outputs_wait_for_an_unreadable_child():
    """F2: nothing is given out from a sample with an unreadable thread; once
    the child is readable its earlier output comes first."""
    names = harness("pi").send_to_user_tools
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 2, "completedAt": "2026-11-22T00:00:05.000Z"}
    stu = {"id": "stu", "runId": CHILD_RUN, "type": "dynamic_tool", "toolName": names[0], "status": "completed", "output": {"structuredContent": {"sentMessage": "hi"}},
           "ordinal": 1, "completedAt": "2026-11-22T00:00:04.000Z"}
    child = proj("completed", [stu], runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "completed"}])
    tree = {"t": {**proj("completed", [final]), "subagents": [{"childThreadId": CHILD}]}, CHILD: RuntimeError("unreadable")}
    ep = _episode(tree)
    r = ep.wait(timeout_s=1, poll_s=0.5)
    assert r.outputs == [] and r.state == "time_limit"
    tree[CHILD] = child
    r = ep.wait(timeout_s=3, poll_s=0.1)
    assert r.state == "finished" and [o["item_id"] for o in r.outputs] == ["stu", "a-final"]


def test_a_delivery_between_the_childs_read_and_the_shell_is_not_reordered():
    """F2: a child delivers after its snapshot and before the closing shell
    read: the sample is discarded; the next consistent one gives both, in order."""
    names = harness("pi").send_to_user_tools
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 2, "completedAt": "2026-11-22T00:00:05.000Z"}
    child = proj("running", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "running"}])
    tree = {"t": {**proj("completed", [final]), "subagents": [{"childThreadId": CHILD}]}, CHILD: child}
    ep = _episode(tree)

    def deliver(tid):
        if tid == CHILD and not child["turnItems"]:
            def later(n):
                if not child["turnItems"]:
                    child["turnItems"].append({"id": "stu", "runId": CHILD_RUN, "type": "dynamic_tool", "toolName": names[0], "status": "completed",
                                               "output": {"structuredContent": {"sentMessage": "hi"}}, "ordinal": 1, "completedAt": "2026-11-22T00:00:04.000Z"})
                    child["runs"][0]["status"] = "completed"
                    ep.t3.bump(CHILD)
            ep.t3.on_shell = later

    ep.t3.on_read = deliver
    seen = []
    for _ in range(4):
        r = ep.wait(timeout_s=2, poll_s=0.1)
        seen += [o["item_id"] for o in r.outputs]
        if r.state != "time_limit":
            break
    assert seen == ["stu", "a-final"] and r.state == "finished"


def test_a_stop_whose_receipt_was_lost_still_counts():
    """F3: the child's run shows interrupted, the receipt was lost: the stop
    counts, and the next hand-back is interrupted."""
    tree = _tree_with_child_question()
    ep = _episode(tree)
    ep.t3.lose_interrupt = True
    i = ep.interrupt(timeout_s=8)
    assert {"thread_id": CHILD, "run_id": CHILD_RUN} in i["interrupted"] and i["attempts"][0]["outcome"].startswith("lost")
    assert ep.wait(timeout_s=3, poll_s=0.1).state == "interrupted"


def test_calls_that_provably_sent_nothing_are_unsent():
    """F4: a deadline checked before docker exec, and a failed websocket
    ticket, are unsent, not lost."""
    from cohbench.driver.t3client import Api, RpcUnsent
    from cohbench.env.docker import DeadlinePassed

    class Box:
        name = "box"

        def __init__(self, mode):
            self.mode = mode

        def exec(self, cmd, **kw):
            if self.mode == "deadline":
                raise DeadlinePassed("the call's deadline has passed")
            import subprocess

            return subprocess.CompletedProcess(cmd, 3, stdout='{"fatal":"ticket","status":503,"body":""}\n', stderr="")

    for mode in ("deadline", "ticket"):
        with pytest.raises(RpcUnsent):
            Api(Box(mode), "tok").dispatch({"type": "run.interrupt", "commandId": "c"}, timeout_s=30)


def test_a_silent_hand_back_is_dated_when_the_agent_stopped():
    """F7: an interrupted turn with no closing output: `handback_utc` is the
    latest completion among the tree's runs, not when the driver saw it."""
    tree = {"t": {**proj("interrupted"), "subagents": [{"childThreadId": CHILD}]},
            CHILD: proj("interrupted", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "interrupted", "completedAt": "2026-11-22T00:00:07.000Z"}])}
    ep = _episode(tree)
    r = ep.wait(timeout_s=3, poll_s=0.1)
    assert r.state == "interrupted" and r.handback_utc == "2026-11-22T00:00:07.000Z" and r.ended_utc > r.handback_utc


def test_an_unsent_continuation_leaves_the_handoff_standing():
    """F8: the handoff happened; the continuation never reached T3. The
    episode awaits its launch; reset again launches it."""
    from cohbench.driver.episode import DriverFailure

    tree = {"t": proj("completed")}
    ep = _episode(tree)

    class Env:
        segments = []

        def keys(self):
            return []

        def handoff(self, on_stopped=None):
            return {"from_segment": 1, "to_segment": 2, "git": {}}

    ep.env = Env()
    ep.idle_gate = lambda timeout_s=60: []
    with pytest.raises(DriverFailure) as exc:
        ep.reset("We are back.", timeout_s=10, dispatch_timeout_s=1.0)
    e = exc.value
    assert not e.fatal and e.unsent and e.handoff_done and e.done == ["handoff"] and ep.failure is None
    assert ep.segment == 2 and ep.thread_id is None and ep.awaiting_launch == "We are back." and ep.handoffs[-1]["continuation"] == "unsent"
    # The reset is the handoff: recorded once, as unsent so far.
    resets = [e for e in ep.runner_events if e["type"] == "controlled_reset"]
    assert len(resets) == 1 and resets[0]["continuation"] == "unsent"
    s = ep.reset("We are back.", dispatch_timeout_s=10)
    assert s.kind == "reset" and ep.thread_id == s.thread_id and ep.awaiting_launch is None and ep.handoffs[-1]["continuation"] == "launched"
    resets = [e for e in ep.runner_events if e["type"] == "controlled_reset"]
    assert len(resets) == 1 and resets[0]["continuation"] == "launched" and resets[0]["ref"] == {"message": s.message_id}



def test_lost_then_unsent_is_lost_and_reconciled():
    """Item 4: one lost attempt makes the outcome unknown, whatever the retry did."""
    import subprocess

    from cohbench.driver.t3client import Api, RpcLost

    class Box:
        name = "box"
        calls = 0

        def exec(self, cmd, **kw):
            Box.calls += 1
            if Box.calls == 1:  # sent, no answer
                return subprocess.CompletedProcess(cmd, 4, stdout='{"open":true}\n{"sent":0}\n{"fatal":"timeout"}\n', stderr="")
            return subprocess.CompletedProcess(cmd, 3, stdout='{"fatal":"ticket","status":503,"body":""}\n', stderr="")

    with pytest.raises(RpcLost):
        Api(Box(), "tok").dispatch({"type": "run.interrupt", "commandId": "c"}, timeout_s=30)


def test_a_lost_stop_of_background_work_counts():
    """Item 3: thread.stop cleared a child's only background task and lost its
    receipt; the confirmed tree shows the work gone, so it was stopped."""
    child = proj("completed", runs=[{"id": CHILD_RUN, "ordinal": 1, "status": "completed"}], providers=[{"pendingBackgroundTasks": [{"id": "bg"}]}])
    final = {"id": "a-final", "runId": RUN, "type": "assistant_message", "text": "done", "ordinal": 2}
    tree = {"t": {**proj("completed", [final]), "subagents": [{"childThreadId": CHILD}]}, CHILD: child}
    ep = _episode(tree)
    ep.t3.lose_interrupt = True
    i = ep.interrupt(timeout_s=8)
    assert [a["op"] for a in i["stopped"]] == ["thread.stop"] and i["stopped"][0]["outcome"].startswith("lost")
    assert ep.wait(timeout_s=3, poll_s=0.1).state == "interrupted"


def test_an_interrupt_that_only_ever_went_unsent_says_so():
    from cohbench.driver.episode import DriverFailure

    ep = _episode({"t": proj("running")})
    ep.t3._write_margin = staticmethod(lambda timeout_s: (_ for _ in ()).throw(__import__("cohbench.driver.t3client", fromlist=["RpcUnsent"]).RpcUnsent("x")))
    with pytest.raises(DriverFailure) as exc:
        ep.interrupt(timeout_s=3)
    assert not exc.value.fatal and exc.value.unsent


def test_done_lists_only_what_t3_accepted():
    """Item 2: a rejected interrupt, then the run ended on its own and the
    correction went unsent: nothing is listed as done."""
    from cohbench.driver.episode import DriverFailure
    from cohbench.driver.t3client import RpcFailure, RpcUnsent

    tree = {"t": {**proj("running"), "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": False}}}]}}
    tree["t"]["providerSessions"][0]["capabilities"]["turns"]["other"] = True  # capabilities present, no steering
    ep = _episode(tree)

    def rejected(tid, run_id, **kw):
        tree["t"]["runs"][0]["status"] = "completed"  # it ended on its own
        raise RpcFailure("orchestration.dispatchCommand", {"message": "not running"})

    ep.t3.interrupt = rejected
    ep.t3.dispatch_message = lambda *a, **k: (_ for _ in ()).throw(RpcUnsent("never started"))
    with pytest.raises(DriverFailure) as exc:
        ep.correct("stop", timeout_s=6)
    e = exc.value
    assert not e.fatal and e.unsent and e.done == [] and e.not_sent == ["correction"]



def test_after_the_decline_an_inconsistent_sample_leads_to_another():
    """The first sample after the decline is inconsistent (the run resumed and
    the tree changed between the shell reads); the second shows the run live,
    so the words are steered, not queued."""
    tree = {"t": {**proj("running", [{"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "PIN"}]),
                  "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]}}
    ep = _episode(tree)
    req = turns.secret_of(tree["t"]["turnItems"][0], "t")
    state = {"declined_at": None}

    def after_decline(n):
        if any("secret" in d for d in ep.t3.dispatched) and state["declined_at"] is None:
            state["declined_at"] = n
        if state["declined_at"] is not None and n == state["declined_at"] + 1:
            ep.t3.bump("t")  # the closing shell read of the first sample differs

    ep.t3.on_shell = after_decline
    s = ep.refuse_secret(req, "No PIN.", timeout_s=8)
    assert s.mechanism == "native_steer" and s.route_basis == "the main run was live"


def test_the_queued_refusal_when_the_run_has_ended():
    tree = {"t": {**proj("running", [{"id": "item-s", "runId": RUN, "type": "secret_request", "secretStatus": "pending", "label": "PIN"}]),
                  "providerSessions": [{"capabilities": {"turns": {"supportsActiveSteering": True}}}]}}
    ep = _episode(tree)
    req = turns.secret_of(tree["t"]["turnItems"][0], "t")
    real = ep.t3.answer_secret

    def decline_and_end(*a, **k):
        out = real(*a, **k)
        tree["t"]["runs"][0]["status"] = "completed"  # the run ended on the decline
        return out

    ep.t3.answer_secret = decline_and_end
    s = ep.refuse_secret(req, "No PIN.", timeout_s=8)
    assert s.mechanism == "queued" and s.route_basis == "the main run had ended" and ep.t3.dispatched[-1]["mode"] == "queue"


def test_usage_interrupt_boundary_uses_recorded_completion_not_confirmation_poll(monkeypatch):
    now = [0.0]
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("cohbench.driver.episode.time.monotonic", lambda: now[0])
    monkeypatch.setattr("cohbench.driver.episode.time.sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr("cohbench.driver.episode.utc_now", lambda: (base + timedelta(seconds=now[0])).isoformat().replace("+00:00", "Z"))
    tree = {"t": proj("running")}
    ep = _episode(tree)
    stop = ep.t3.interrupt

    def interrupt(*args, **kwargs):
        result = stop(*args, **kwargs)
        tree["t"]["runs"][0]["completedAt"] = "2026-11-22T12:00:00.100Z"
        now[0] += 0.2
        return result
    ep.t3.interrupt = interrupt
    ep.interrupt(timeout_s=5)
    assert now[0] > 1
    assert ep._usage_interrupts == [{"start": "2026-11-22T12:00:00Z", "end": "2026-11-22T12:00:00.100Z"}]
