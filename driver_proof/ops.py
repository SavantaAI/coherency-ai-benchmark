"""The driver operations. Each one runs in fresh containers, each behind its
own gateway, and decides its verdict from checks in code.

Both harnesses receive the same user prompts and the same fixture values
(fixtures.Fixtures, one seed per run). Prompts avoid words that would tell the
agent it is being measured. Every message goes through T3's WebSocket RPC
(session.Workspace), so the agent sees the text exactly as sent.

An op raises evidence.Blocked when the gateway log shows the model API
refusing a run's requests; the runner records that and still runs the
container end checks. A failed deciding or hygiene check gives `fail` even
then.
"""

from __future__ import annotations

import secrets
import json
import re
import sqlite3
import time
import traceback
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import container as dk
from . import gatewaylog, native, t3client
from .evidence import Blocked, Recorder
from .harness import PI_DIALOG_EXTENSION, PI_DIALOG_EXTENSION_NAME
from .session import TERMINAL, Sent, Workspace

OpFn = Callable[[Workspace, Recorder], None]

# ---------------------------------------------------------------------------
# Prompts. Identical for both harnesses.
# ---------------------------------------------------------------------------

P_READY = "Reply with exactly one word: ready"
P_DIAG = "Reply with exactly one word: hello"
P_SUM = "What is 17 plus 25? Reply with only the number."
P_SEND_TO_USER = "Use your send_to_user tool to send me this exact text: Build finished at step 3. If that tool is not loaded yet, search your tools for it first. Then reply with only the word done."
SEND_TO_USER_TEXT = "Build finished at step 3."
P_QUESTION = (
    "I want a file named color.txt in the project. Before you create it, ask me which color I prefer, "
    "red or blue. If you have a tool that puts a question to me directly, use it. Wait for my answer, "
    "then write exactly my answer into color.txt and reply with one short sentence naming the color."
)
P_SECRET = (
    "I have a 4-digit PIN for the staging box that only I know. Ask me for it with your request_secret tool, "
    "delivered as a file. Then run one shell command that copies that file to pin.txt in the project, "
    "and reply with the word done."
)
P_SUBAGENT = "Use a subagent to find out how many lines README.md in this project has. Do not count them yourself. Reply with only the number."
P_REMEMBER = "Remember this codeword for later: {word}. Reply only with OK."
P_BIGFILE = (
    "Create a file named big.txt in the project containing the lines item 0001 through item 3400, one per line, in order. "
    "Write it with a single call of your file-writing tool, not with a shell command. Reply only with done."
)
P_ASK = "What was the codeword I gave you earlier? Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_AFTER = "What is 2 plus 3? Reply with only the number."
P_ROT_FIRST = "Remember this codeword for later: {word}. Also create a file named notes.txt in the project containing exactly {note}. Reply only with OK."
P_ROT_NOTE = "What does the file notes.txt in the project contain? Reply with only its contents."
# Snapshot asks differ in wording so each container's history can be told apart.
P_ASK_COPY = "Which codeword did I give you earlier? Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_ASK_ORIG = "Tell me the codeword I gave you earlier. Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_ASK_COPY_2 = "Remind me of the codeword I gave you earlier. Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_ASK_ORIG_2 = "Repeat the codeword I gave you earlier. Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_ASK_COPY_3 = "What codeword did I give you earlier? Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_ASK_ORIG_3 = "Say the codeword I gave you earlier. Reply with just the codeword, or exactly UNKNOWN if I gave none."
P_BUSY = "Run the shell command `until [ -e /tmp/go-{tag} ]; do sleep 1; done; echo busy-done` and then reply with only the word finished."
STEER_TEXT = (
    "Change of plan: stop after the step you are on, skip the remaining steps, and create a "
    "file named stopped.txt containing the word halted. Then reply with STEERED."
)

# Visible-name scan. Words for every name the agent can read, plus words that
# describe the setup, for the files the agent can read.
MEASUREMENT_WORDS = re.compile(r"benchmark|coherency|\beval|\btest|probe|proof", re.IGNORECASE)
SETUP_WORDS = re.compile(r"snapshot|docker commit|docker exec|bind[- ]mount", re.IGNORECASE)

# A command or tool call that reads thread history or harness state.
HISTORY_ACCESS = re.compile(
    r"t3_thread_(read|search|list|transfer)|t3_queue|\bt3\s+thread\b|thread_read|\.t3\b|/home/node/\.t3|T3_BASE_DIR|statev2"
    r"|127\.0\.0\.1:3773|localhost:3773|:3773\b|\.pi/agent|\.pi\b|\.claude\b|/sessions/|\.jsonl",
    re.IGNORECASE,
)


def _exact(reply: str | None, expected: str) -> bool:
    """The reply is the expected text, allowing surrounding whitespace, a final period and backticks or bold."""
    if reply is None:
        return False
    return reply.strip().strip("`*").strip().rstrip(".").strip() == expected


def q_(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# ---------------------------------------------------------------------------
# Container end: run for every container an op starts
# ---------------------------------------------------------------------------


def visible_names(ws: Workspace) -> dict[str, Any]:
    """Names the agent can read that this project controls, and setup words in readable files.
    Incomplete, and so failing, if the server process is not found or any listing or read fails."""
    assert ws.c is not None and ws.api is not None
    problems = []
    host = ws.c.exec(["hostname"], check=False)
    if host.returncode != 0:
        problems.append("hostname failed")
    pids = ws.c.exec(["pgrep", "-f", "t3 serve"], check=False)
    if pids.returncode != 0 or not pids.stdout.split():
        problems.append("no 't3 serve' process matched")
    env_names: set[str] = set()
    for pid in pids.stdout.split():
        env = ws.c.exec(["cat", f"/proc/{pid}/environ"], check=False)
        if env.returncode == 0:
            env_names |= {e.split("=", 1)[0] for e in env.stdout.split("\0") if e}
        else:
            problems.append(f"environment of pid {pid} not readable")
    tree_lines: list[str] = []
    for root in ("/work", "/home/node/.pi/agent", "/home/node/.claude"):
        if ws.c.exec(["test", "-d", root], check=False).returncode != 0:
            continue
        r = ws.c.exec(["find", root, "-maxdepth", "6", "-not", "-path", "*/.git/*", "-not", "-path", "*/sessions/*", "-not", "-path", "*/projects/*"], check=False)
        if r.returncode != 0:
            problems.append(f"find {root} exited {r.returncode}: {r.stderr.strip()[:120]}")
        tree_lines += r.stdout.splitlines()
    files = ["/etc/t3-settings.json", "/usr/local/bin/workspace-entrypoint", *ws.harness.default_model_files()]
    if ws.harness.extension_dir and ws.c.exec(["test", "-d", ws.harness.extension_dir], check=False).returncode == 0:
        r = ws.c.exec(["find", ws.harness.extension_dir, "-type", "f"], check=False)
        if r.returncode != 0:
            problems.append(f"find {ws.harness.extension_dir} exited {r.returncode}")
        files += r.stdout.split()
    texts = []
    for path in files:
        r = ws.c.exec(["cat", path], check=False)
        if r.returncode != 0:
            problems.append(f"could not read {path}")
        texts.append(r.stdout)
    shell = ws.shell()
    titles = [t.get("title", "") for t in shell.get("threads", [])]
    projects = [p.get("title", "") for p in ws.api.get("/api/projects").get("projects", [])]
    hay = "\n".join([host.stdout, *sorted(env_names), *tree_lines, *texts, *titles, *projects])
    return {
        "complete": not problems,
        "problems": problems,
        "files_scanned": files,
        "env_names": sorted(env_names),
        "tree_entries": len(tree_lines),
        "measurement_word_hits": sorted({m.group(0) for m in MEASUREMENT_WORDS.finditer(hay)}),
        "setup_word_hits_in_readable_files": sorted({m.group(0) for m in SETUP_WORDS.finditer("\n".join(texts))}),
        "thread_titles": titles,
        "project_titles": projects,
    }


def names_check(ws: Workspace, rec: Recorder, when: str) -> None:
    try:
        names = visible_names(ws)
        ok = names["complete"] and not names["measurement_word_hits"] and not names["setup_word_hits_in_readable_files"]
        rec.check(f"no measurement or setup words visible to the agent ({ws.label}, {when}); scan complete", ok, names, hygiene=True)
    except Exception as exc:
        rec.check(f"no measurement or setup words visible to the agent ({ws.label}, {when}); scan complete", False, f"scan failed: {exc}", hygiene=True)




def container_end(ws: Workspace, rec: Recorder, *, decides: bool | None = None, phase: str = "end") -> None:
    """Every container: messages byte-identical and bound to their runs, every
    model call on the requested model (gateway log), no measurement words, the
    real key nowhere in the container's filesystem or config, T3's traces.
    A container retired inside an operation (the handoff's first) runs this
    with its own phase before it is removed; the checks are labelled by it.

    The message checks decide only while the op is not blocked (a refused
    harness may not have written its session), and never for a characterisation.
    Each message is checked once, by the phase of the container it went to."""
    if ws.c is None:
        return
    where = ws.label if phase == "end" else f"{ws.label} {phase}"
    msg_decides = (rec.blocked is None) if decides is None else decides
    ws.save_native_sessions("final" if phase == "end" else f"final-{phase}", optional=not msg_decides)
    sessions = ws.sessions()
    expected = []
    to_check = ws.sent[ws.checked_sent:]
    ws.checked_sent = len(ws.sent)
    for s in to_check:
        snap = ws.snapshot_or_last(s.thread_id)
        expected.append({"kind": s.kind, "text": s.text, "message_id": s.message_id, "run_id": s.run_id, "sent_at": s.sent_at, "native_ids": ws.native_ids(snap, s.run_id) if snap else [], "informational": s.informational})
    info_ids = {e["message_id"] for e in expected if e["informational"]}
    for res in native.bound_message_checks(sessions, expected):
        rec.check(f"{where}: {res['kind']} message stored byte-identical in its run's harness session, in order, no T3 note", res["passed"], res, decides=msg_decides and res["message_id"] not in info_ids)
    records = ws.gateway_records()
    rows = gatewaylog.model_rows(records)
    want = ws.harness.model.split("/", 1)[-1]
    bad = [r for r in rows if r["model"] != want or (r["response_model"] is not None and r["response_model"] != want)]
    rec.check(f"{where}: every inference call in the gateway log names {want}, request and response (a missing model fails)", bool(rows) and not bad, {"models": gatewaylog.models(records), "inference_calls": len(rows), "bad_rows": bad[:10]}, hygiene=True)
    nat_models = sorted({str(u.get("model")) for s in sessions for u in s.get("assistant_usage", [])} | ({m for s in sessions for m in s.get("models", [])} if ws.harness.name == "claude" else set()))
    rec.check(f"{where}: harness sessions name only {want} (cross-check)", not nat_models or nat_models == [want], nat_models, decides=False)
    if phase == "end":
        # The gateway log is the topology's, shared by every phase: counted once.
        rec.facts.setdefault("gateway", {})[where] = {"usage": gatewaylog.usage_summary(ws.gateway_dir / "log"), "destinations": gatewaylog.destinations(records)}
    names_check(ws, rec, phase)
    scan = ws.export_scan()
    rec.check(f"{where}: real key nowhere in the container filesystem (docker export scan)", dk.scan_ok(scan), scan, hygiene=True)
    rec.check(f"{where}: real key not in docker inspect", not dk.key_in_inspect(ws.c.name, ws.key), {"env_names": [e.split("=", 1)[0] for e in ws.c.inspect()["Config"]["Env"]]}, hygiene=True)
    collect_traces(ws, rec, phase)


def collect_traces(ws: Workspace, rec: Recorder, phase: str) -> None:
    """Every failed span in every T3 trace file of this container phase, keeping
    every distinct cause (first line) per span name, with the network pattern
    run over all of them."""
    where = f"{ws.label} {phase}"
    res = dk.copy_out_scanned(ws.c.name, f"{dk.T3_BASE_DIR}/userdata/logs", rec.raw_dir / "t3-logs" / ws.label / phase, ws.key)
    failed: dict[str, dict[str, int]] = {}
    files = []
    for f in res["files"]:
        rec.register(f)
        if ".trace.ndjson" not in f.name:
            continue
        files.append(f.name)
        for line in f.read_text(errors="replace").splitlines():
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (span.get("exit") or {}).get("_tag") == "Failure":
                cause = str(span["exit"].get("cause", "")).splitlines()[0][:200] if span["exit"].get("cause") else ""
                bucket = failed.setdefault(span.get("name", "?"), {})
                bucket[cause] = bucket.get(cause, 0) + 1
    network = {n: {c: k for c, k in causes.items() if NETWORK_FAILURE.search(c)} for n, causes in failed.items()}
    network = {n: v for n, v in network.items() if v}
    rec.facts.setdefault("t3_failed_spans", {})[where] = {"failed_spans": failed, "network_related": network, "trace_files": files, "copied": res["ok"], "copy_error": res["error"]}


NETWORK_FAILURE = re.compile(r"EAI_AGAIN|ENOTFOUND|getaddrinfo|fetch failed|ECONNREFUSED|ECONNRESET|ENETUNREACH|EHOSTUNREACH|ETIMEDOUT|UND_ERR|socket hang up|self[- ]signed|unable to verify|certificate has expired")


TOOL_TYPES = ("command_execution", "dynamic_tool", "mcp_tool_call", "file_change", "file_read", "file_search", "web_search", "subagent")


def _is_t3_only(item: dict[str, Any]) -> bool:
    """Items T3 writes itself, with no harness counterpart: checkpoints, the
    workspace-preparation step, and T3's record of a delegated child (checked
    through the child's own thread)."""
    return item["type"] in ("checkpoint", "subagent") or "workspace-preparation" in item["id"]


def thread_export(ws: Workspace, tid: str) -> dict[str, Any]:
    """The payload-bearing export of one thread: the HTTP snapshot's records,
    with every tool item replaced by its full form from getTurnItem."""
    proj = ws.snapshot(tid)["projection"]
    items = []
    for item in sorted(proj["turnItems"], key=lambda i: i.get("ordinal") or 0):
        if item["type"] in TOOL_TYPES or item.get("outputOmitted"):
            full = ws.turn_item(tid, item["id"])
            if full is None:
                raise t3client.T3Error(f"getTurnItem returned nothing for {item['id']}")
            item = {**full, "ordinal": item.get("ordinal")}
        items.append(item)
    return {
        "thread": proj["thread"],
        "runs": sorted(proj["runs"], key=lambda r: r["ordinal"]),
        "attempts": proj["attempts"],
        "messages": proj["messages"],
        "subagents": proj.get("subagents", []),
        "providerTurns": [{"id": t["id"], "tokenUsage": t.get("tokenUsage")} for t in proj.get("providerTurns", [])],
        "items": items,
    }


def _strip_note(native_text: str, t3_text: str) -> str | None:
    """T3 prefixes agent-created messages with a sender note before the harness
    sees them (withMessageSenderNote). Returns which form matched, or None."""
    if native_text == t3_text:
        return "exact"
    if native_text.startswith("[T3 Code:") and native_text.endswith("\n\n" + t3_text):
        return "after T3 sender note"
    return None


def compare_to_native(export: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Every exercised item of one thread against the harness's own record in
    that thread's own session: user text, assistant text, tool inputs, tool
    outputs. An item with no native counterpart where one should exist fails,
    and so does a native tool call with no T3 item."""
    problems: list[str] = []
    groups: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for run in export["runs"]:
        nids = [a["nativeThreadId"] for a in export["attempts"] if a["runId"] == run["id"] and a.get("nativeThreadId")]
        sess = next((native.session_for_native_id(sessions, n) for n in nids if native.session_for_native_id(sessions, n)), None)
        if sess is None:
            problems.append(f"run {run['id'][-24:]}: no copied harness session ({nids})")
            continue
        groups.setdefault(sess["path"], (sess, []))[1].append(run["id"])
    counts = {"user_message": 0, "assistant_message": 0, "tool_input": 0, "tool_output": 0, "sender_note": 0}
    for sess, run_ids in groups.values():
        items = [i for i in export["items"] if i.get("runId") in run_ids]
        t3_user = [i.get("text") or "" for i in items if i["type"] == "user_message"]
        if len(t3_user) != len(sess["user_texts"]):
            problems.append(f"{sess['file']}: {len(t3_user)} user items, {len(sess['user_texts'])} native user messages")
        for a, b in zip(t3_user, sess["user_texts"]):
            form = _strip_note(b, a)
            if form is None:
                problems.append(f"{sess['file']}: user text differs: {a[:60]!r}")
            else:
                counts["user_message"] += 1
                counts["sender_note"] += form != "exact"
        t3_asst = [i.get("text") or "" for i in items if i["type"] == "assistant_message"]
        if t3_asst != sess["assistant_text_parts"]:
            problems.append(f"{sess['file']}: assistant texts differ ({len(t3_asst)} T3, {len(sess['assistant_text_parts'])} native)")
        else:
            counts["assistant_message"] += len(t3_asst)
        calls = {c["id"]: c for c in sess["tool_calls"]}
        results = {r["toolCallId"]: r for r in sess["tool_results"]}
        seen = set()
        for item in items:
            if item["type"] not in TOOL_TYPES or _is_t3_only(item):
                continue
            nid = (item.get("nativeItemRef") or {}).get("nativeId")
            call, result = calls.get(nid), results.get(nid)
            if call is None or result is None:
                problems.append(f"{item['type']} {item['id'][-30:]}: no native {'call' if call is None else 'result'}")
                continue
            seen.add(nid)
            args = call.get("arguments") or {}
            if item["type"] == "file_change":
                # T3 records a file write as the path and the new text; it does
                # not keep the tool's result message.
                path_ok = item.get("fileName") in (args.get("path"), args.get("file_path"))
                text = args.get("content", args.get("newText", args.get("new_string")))
                text_ok = text is None or item.get("newStr") == text
                if path_ok and text_ok:
                    counts["tool_input"] += 1
                else:
                    problems.append(f"{item['id'][-30:]}: file change differs from the call's path or text")
                counts["file_change_result_not_carried"] = counts.get("file_change_result_not_carried", 0) + 1
                continue
            want_input = args.get("command") if item["type"] == "command_execution" else args
            if item.get("input") != want_input:
                problems.append(f"{item['id'][-30:]}: input differs")
            else:
                counts["tool_input"] += 1
            got = item.get("output") if item["type"] == "command_execution" else tool_text(item.get("output"))
            if got != result["text"]:
                problems.append(f"{item['id'][-30:]}: output differs ({len(got or '')} vs {len(result['text'])} chars)")
            else:
                counts["tool_output"] += 1
        missing = [cid for cid in calls if cid not in seen]
        if missing:
            problems.append(f"{sess['file']}: native tool calls with no T3 item: {missing[:5]}")
    return {"ok": not problems and bool(groups), "sessions": len(groups), "compared": counts, "problems": problems[:30]}


def _bound(rec: Recorder, ws: Workspace, sent: Sent, what: str) -> None:
    """Fail fast if a message's run could not be found."""
    if not sent.run_id:
        raise t3client.T3Error(f"{what}: no run id for message {sent.message_id}")


# ---------------------------------------------------------------------------
# 1. Session startup
# ---------------------------------------------------------------------------


def op_startup(ws: Workspace, rec: Recorder) -> None:
    h = ws.harness
    rec.facts["mechanism"] = "WebSocket orchestration.launchThread with modelSelection, then HTTP GET /api/orchestration/threads/<id>"
    sent = ws.launch(P_READY, "session")
    tid = sent.thread_id
    rec.facts["thread_id"] = tid
    procs: list[str] = []
    deadline = time.time() + 30
    while not procs and time.time() < deadline:
        procs = [line for line in ws.c.sh("ps -eo pid,args", check=False).splitlines() if re.search(r"pi-coding-agent|/pi |claude|--mode rpc", line)]
        time.sleep(0.3)
    rec.save_text("harness-process-args.txt", "\n".join(procs) + "\n")
    rec.facts["harness_process_args"] = procs
    outcome = None
    blocked = False
    try:
        outcome = ws.turn_outcome(tid, sent, "first turn")
    except Blocked:
        blocked = True
        raise
    finally:
        # After a refusal everything below is optional evidence: no deciding
        # check, and a failure here cannot replace the Blocked being raised.
        try:
            _startup_records(ws, rec, sent, outcome, decides=not blocked)
        except Exception as exc:
            if not blocked:
                raise
            rec.event("startup evidence after the refusal incomplete", error=f"{type(exc).__name__}: {exc}"[:300])
    rec.check("first turn completed with the reply ready", outcome is not None and outcome["completed"] and _exact((outcome["reply"] or "").lower(), "ready"), {k: v for k, v in (outcome or {}).items() if k != "snap"})


def _startup_records(ws: Workspace, rec: Recorder, sent: Sent, outcome: dict[str, Any] | None, *, decides: bool) -> None:
    h = ws.harness
    tid = sent.thread_id
    snap = (outcome or {}).get("snap") or ws.snapshot(tid)
    rec.save_json("thread-snapshot.json", snap)
    proj = snap["projection"]
    thread = proj["thread"]
    run = next((r for r in proj["runs"] if r["id"] == sent.run_id), {})
    rec.facts["provider_sessions"] = [{k: s.get(k) for k in ("id", "driver", "providerInstanceId", "status", "model", "cwd")} for s in proj.get("providerSessions", [])]
    rec.facts["t3_detected_provider"] = ws.t3_detected_provider()
    rec.check("thread uses the requested provider instance", thread.get("providerInstanceId") == h.provider_instance, thread.get("providerInstanceId"), decides=decides)
    rec.check("thread stores the requested model and options", thread.get("modelSelection") == h.model_selection(), thread.get("modelSelection"), decides=decides)
    rec.check("run carries the requested model and options", run.get("modelSelection") == h.model_selection(), run.get("modelSelection"), decides=decides)
    rec.check("T3 detected the harness version", bool(rec.facts["t3_detected_provider"].get("version")), rec.facts["t3_detected_provider"].get("version"), decides=decides)
    ws.save_native_sessions("sessions", optional=not decides)
    sess = ws.sessions()
    if h.name == "pi":
        models = [m for s in sess for m in s["models"]]
        levels = [lv for s in sess for lv in s["thinking_levels"]]
        rec.check("harness session switched to the requested model", bool(models) and models[-1] == h.model.split("/", 1)[1], models, decides=decides)
        rec.check("harness session uses the requested option (thinking)", bool(levels) and levels[-1] == h.options["thinking"], levels, decides=decides)
    else:
        models = [m for s in sess for m in s["models"]]
        efforts = [e for s in sess for e in s["efforts"]]
        rec.check("harness API requests use the requested model", bool(models) and all(m == h.model for m in models), models, decides=decides)
        rec.check("harness API requests use the requested option (effort)", bool(efforts) and all(e == h.options["effort"] for e in efforts), efforts, decides=decides)
        api_errors = [e for s in sess for e in s.get("api_errors", [])]
        if api_errors:
            rec.facts["harness_api_errors"] = api_errors[:5]
            try:
                rec.facts["entrypoint_comparison"] = _claude_entrypoint_comparison(ws)
            except Exception as exc:  # an observation; it decides nothing
                rec.facts["entrypoint_comparison"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    inj = native.injected_material(h.name, {"sessions": sess})
    if inj:
        rec.save_json("injected-material.json", inj)
        if inj.get("t3_instructions"):
            rec.save_text("injected-t3-instructions.txt", inj["t3_instructions"])
        rec.save_json("injected-tool-names.json", inj.get("tools", []))
        tools = inj.get("tools", [])
        rec.facts["injected"] = {
            "t3_instructions_utf8_bytes": inj.get("t3_instructions_utf8_bytes"),
            "tools": len(tools),
            "tools_mcp__t3_code__": sum(1 for t in tools if str(t).startswith("mcp__t3_code__")),
            "tools_mcp__t3-code__": sum(1 for t in tools if str(t).startswith("mcp__t3-code__")),
            "other_tools": [t for t in tools if not str(t).startswith(("mcp__t3_code__", "mcp__t3-code__"))],
            **({k: inj[k] for k in ("direct_tools",)} if "direct_tools" in inj else {}),
            **({"deferred_t3": len(inj["deferred_t3_tools"]), "deferred_other": len(inj["deferred_other_tools"])} if "deferred_t3_tools" in inj else {}),
        }


def _claude_entrypoint_comparison(ws: Workspace) -> list[dict[str, Any]]:
    """Observation only: `claude -p` by hand through the gateway, with the
    entrypoint T3 sets and with the SDK default, on a prompt T3 never sends."""
    h = ws.harness
    results = []
    for entrypoint in ("t3-code", "sdk-ts"):
        before = len(ws.gateway_records())
        proc = ws.c.exec(
            ["sh", "-c", 'cd /tmp && CLAUDE_CODE_ENTRYPOINT="$0" claude -p "$1" --model "$2" --effort "$3" --max-turns 1 2>&1', entrypoint, P_DIAG, h.model, h.options["effort"]],
            check=False,
            timeout=180,
        )
        new = [r for r in ws.gateway_records()[before:] if gatewaylog.is_inference(r)]
        results.append({"CLAUDE_CODE_ENTRYPOINT": entrypoint, "exit": proc.returncode, "output": proc.stdout.strip()[:300], "gateway": [{k: r.get(k) for k in ("status", "error_type", "model")} for r in new]})
    return results


# ---------------------------------------------------------------------------
# 2. Turn messaging (and send_to_user)
# ---------------------------------------------------------------------------


def op_messaging(ws: Workspace, rec: Recorder) -> None:
    first = ws.launch(P_READY, "chat")
    tid = first.thread_id
    rec.facts["thread_id"] = tid
    o1 = ws.turn_outcome(tid, first, "first turn")
    rec.check("first turn completed", o1["completed"], {k: v for k, v in o1.items() if k != "snap"})

    follow = ws.send(tid, P_SUM, kind="follow-up")
    o2 = ws.turn_outcome(tid, follow, "follow-up")
    st = ws.state(tid, follow.run_id)
    shell_pending = ws.shell_pending(tid)
    rec.check("follow-up: run completed, final assistant message is 42", o2["completed"] and _exact(o2["reply"], "42"), {"run": follow.run_id, "status": o2["status"], "reply": o2["reply"]})
    rec.check(
        "follow-up: finished state is distinguishable (run terminal, no pending request in projection or shell)",
        st["kind"] == "finished" and st["run_status"] == "completed" and shell_pending is None,
        {"kind": st["kind"], "run_status": st["run_status"], "shell_pendingRuntimeRequest": shell_pending},
    )

    stu = ws.send(tid, P_SEND_TO_USER, kind="follow-up (send_to_user)")
    o3 = ws.turn_outcome(tid, stu, "send_to_user turn")
    items = [i for i in ws.items(o3["snap"], stu.run_id) if i["type"] in ("dynamic_tool", "mcp_tool_call") and str(i.get("toolName", "")).endswith("send_to_user")]
    full = [ws.turn_item(tid, i["id"]) for i in items]
    rec.save_json("send-to-user-items.json", {"snapshot_items": items, "getTurnItem": full})
    delivered = []
    for item in full:
        out = (item or {}).get("output") or {}
        sent_message = (out.get("structuredContent") or {}).get("sentMessage") if isinstance(out, dict) else None
        delivered.append({"itemId": (item or {}).get("id"), "status": (item or {}).get("status"), "toolName": (item or {}).get("toolName"), "input_message": ((item or {}).get("input") or {}).get("message"), "result_sentMessage": sent_message})
    rec.facts["send_to_user_in_export"] = delivered
    norm = lambda t: str(t or "").strip().rstrip(".").strip()  # noqa: E731
    good = [d for d in delivered if d["status"] == "completed" and norm(d["result_sentMessage"]) == norm(SEND_TO_USER_TEXT)]
    rec.facts["send_to_user_exact_copy"] = [d["result_sentMessage"] == SEND_TO_USER_TEXT for d in good]
    rec.check("send_to_user: a completed tool item whose result (structuredContent.sentMessage) carries the text", o3["completed"] and bool(good), {"status": o3["status"], "items": delivered})


# ---------------------------------------------------------------------------
# 2b. Pending requests: a question and a secret, answered from a script
# ---------------------------------------------------------------------------


def _answer_for(question: dict[str, Any], want: str) -> str:
    for opt in question.get("options", []):
        if want in (str(opt.get("value", "")).lower(), str(opt.get("label", "")).lower()):
            return opt.get("value") or opt.get("label")
    return want


def _file(ws: Workspace, name: str) -> str | None:
    r = ws.c.exec(["cat", f"{dk.WORKSPACE}/{name}"], check=False)
    return r.stdout if r.returncode == 0 else None


def _question_round(ws: Workspace, rec: Recorder, title: str, tag: str) -> dict[str, Any]:
    """Launch the question prompt in a clean workspace and answer the pending request if one appears."""
    ws.c.exec(["rm", "-f", f"{dk.WORKSPACE}/color.txt"])
    sent = ws.launch(P_QUESTION, title)
    tid = sent.thread_id
    st = ws.wait(tid, sent.run_id, until=("question", "secret", "finished"), timeout_s=180)
    result: dict[str, Any] = {"thread": tid, "run": sent.run_id, "state_before_answer": st["kind"], "run_status_before_answer": st["run_status"]}
    if st["kind"] == "finished":
        ws.require_completed(st, f"{tag} question turn")
        result["reply"] = ws.reply(st["snap"], sent.run_id)
        result["tools_called"] = [i.get("toolName") or i.get("title") for i in ws.items(st["snap"], sent.run_id) if i["type"] in ("dynamic_tool", "mcp_tool_call")]
        return result
    if st["kind"] != "question":
        result["unexpected"] = st["kind"]
        return result
    item = st["questions"][0]
    shell_pending = ws.shell_pending(tid)
    proj_req = next((r for r in st["snap"]["projection"]["runtimeRequests"] if r["id"] == item["requestId"]), {})
    # The call that opened the request: a running tool item in the same provider
    # turn whose input carries the question text. Exactly one must match.
    # Compared JSON-escaped, the way the text sits inside the serialised input.
    qtexts = [json.dumps(qq["question"], ensure_ascii=False)[1:-1] for qq in item.get("questions", []) if qq.get("question")]
    candidates = [
        i for i in ws.items(st["snap"], sent.run_id)
        if i["type"] in ("dynamic_tool", "mcp_tool_call") and i.get("status") == "running"
        and i.get("providerTurnId") == proj_req.get("providerTurnId")
        and qtexts and all(t in json.dumps((ws.turn_item(tid, i["id"]) or i).get("input"), ensure_ascii=False) for t in qtexts)
    ]
    tool_item = candidates[0] if len(candidates) == 1 else None
    result["originating_call_candidates"] = [c["id"] for c in candidates]
    color_before = _file(ws, "color.txt")
    result.update(
        {
            "shell_pending": shell_pending,
            "ids_joined": bool(shell_pending) and shell_pending.get("id") == item["requestId"] == proj_req.get("id") and proj_req.get("kind") == "user_input" and proj_req.get("status") == "pending",
            "question_tool_item": (tool_item or {}).get("id"),
            "question_tool": (tool_item or {}).get("toolName"),
            "color.txt_before_answer": color_before,
        }
    )
    rec.save_json(f"{tag}-pending-item.json", item)
    answers = {qq["id"]: _answer_for(qq, "blue") for qq in item.get("questions", [])}
    result["answers"] = answers
    ws.respond_user_input(tid, item["requestId"], answers)
    done = ws.wait(tid, sent.run_id, timeout_s=180)
    completed = ws.require_completed(done, f"{tag} question turn after the answer")
    req = next((r for r in done["snap"]["projection"].get("runtimeRequests", []) if r["id"] == item["requestId"]), {})
    # The harness's own record of the answer: the question tool's result, in this run's session.
    ws.save_native_sessions(f"after-{tag}")
    sessions = ws.sessions()
    session = next((native.session_for_native_id(sessions, n) for n in ws.native_ids(done["snap"], sent.run_id) if native.session_for_native_id(sessions, n)), None)
    native_id = ((tool_item or {}).get("nativeItemRef") or {}).get("nativeId")
    tool_result = next((r for r in (session or {}).get("tool_results", []) if r["toolCallId"] == native_id), None)
    result.update(
        {
            "completed": completed,
            "run_status": done["run_status"],
            "request_status": req.get("status"),
            "color.txt": _file(ws, "color.txt"),
            "reply": ws.reply(done["snap"], sent.run_id),
            "native_session": (session or {}).get("file"),
            "native_tool_call": native_id,
            "native_tool_result": (tool_result or {}).get("text"),
        }
    )
    rec.save_json(f"{tag}-thread-snapshot.json", done["snap"])
    return result


def op_pending(ws: Workspace, rec: Recorder) -> None:
    h = ws.harness
    stock = _question_round(ws, rec, "colors", "stock")
    rec.facts["question_stock_harness"] = stock
    if h.extension_dir:
        rec.check("stock Pi: no tool puts a question to the user, so the turn ends without a pending request", stock["state_before_answer"] == "finished", stock, decides=False)
        # DIAGNOSTIC: not part of the reference harness. Load the dialog
        # extension, run the same prompt in a new thread, then remove it.
        path = f"{h.extension_dir}/{PI_DIALOG_EXTENSION_NAME}"
        ws.c.exec(["sh", "-c", 'mkdir -p "$0" && cat > "$1"', h.extension_dir, path], input_text=PI_DIALOG_EXTENSION)
        rec.event("diagnostic dialog extension installed", path=path)
        try:
            names_check(ws, rec, "dialog extension installed")
            q = _question_round(ws, rec, "colors-2", "diagnostic")
        finally:
            ws.c.exec(["rm", "-f", path], check=False)
            rec.event("diagnostic dialog extension removed", path=path)
        rec.facts["question_diagnostic_extension"] = q
        label = "Pi with the diagnostic dialog extension"
        answer_ok = q.get("native_tool_result") == "blue"
    else:
        q = stock
        label = f"{h.name} ({h.native_question_tool})"
        answer_ok = "blue" in (q.get("native_tool_result") or "") and "[T3 Code:" not in (q.get("native_tool_result") or "")
    rec.facts["question_route_judged"] = label
    rec.check(
        f"question ({label}): waiting state, with the shell, projection and item request ids joined",
        q["state_before_answer"] == "question" and q.get("ids_joined") is True and not (q.get("shell_pending") or {}).get("userOnly"),
        {k: q.get(k) for k in ("state_before_answer", "shell_pending", "ids_joined")},
    )
    rec.check(
        f"question ({label}): answered from a script; color.txt absent before the answer and 'blue' after; request resolved; run completed",
        q.get("completed") is True and q.get("request_status") == "resolved" and q.get("color.txt_before_answer") is None and (q.get("color.txt") or "").strip() == "blue" and "blue" in (q.get("reply") or "").lower(),
        q,
    )
    rec.check(f"question ({label}): the request is bound to exactly one originating tool call, and that call's result in the run's harness session is the answer, without a T3 note", answer_ok and bool(q.get("native_tool_call")) and len(q.get("originating_call_candidates") or []) == 1, {k: q.get(k) for k in ("question_tool", "originating_call_candidates", "native_tool_call", "native_session", "native_tool_result")})

    # Secret request.
    pin = str(rec.fixtures.number("pin", 1000, 9999))
    ws.c.exec(["rm", "-f", f"{dk.WORKSPACE}/pin.txt"])
    sent = ws.launch(P_SECRET, "pin")
    tid = sent.thread_id
    st = ws.wait(tid, sent.run_id, until=("question", "secret", "finished"), timeout_s=180)
    if st["kind"] == "finished":
        ws.require_completed(st, "secret turn")
    shell_pending = ws.shell_pending(tid) if st["kind"] != "finished" else None
    item = st["secrets"][0] if st["kind"] == "secret" else {}
    rec.check(
        "secret: waiting state distinguishable (secret_request pending, shell pending userOnly true, same id)",
        st["kind"] == "secret" and bool((shell_pending or {}).get("userOnly")) and (shell_pending or {}).get("id") == item.get("id"),
        {"kind": st["kind"], "run_status": st["run_status"], "shell_pending": shell_pending, "item": item.get("id")},
    )
    secret_result: dict[str, Any] = {"state_before_answer": st["kind"]}
    if st["kind"] == "secret":
        rec.save_json("secret-pending-item.json", item)
        secret_result["pin.txt_before_answer"] = _file(ws, "pin.txt")
        ws.answer_secret(tid, item, pin)
        done = ws.wait(tid, sent.run_id, timeout_s=180)
        completed = ws.require_completed(done, "secret turn after the answer")
        after = next((i for i in ws.items(done["snap"], sent.run_id, "secret_request") if i["id"] == item["id"]), {})
        secret_result.update({"completed": completed, "secretStatus": after.get("secretStatus"), "pin.txt_matches": (_file(ws, "pin.txt") or "").strip() == pin, "reply": ws.reply(done["snap"], sent.run_id), "fields": [f.get("name") for f in item.get("fields") or []]})
        rec.save_json("secret-thread-snapshot.json", done["snap"])
    rec.facts["secret"] = secret_result
    rec.check(
        "secret: answered from a script through secrets.answerRequest; pin.txt absent before and the exact value after",
        secret_result.get("completed") is True and secret_result.get("secretStatus") == "saved" and secret_result.get("pin.txt_before_answer") is None and secret_result.get("pin.txt_matches") is True,
        secret_result,
    )


# ---------------------------------------------------------------------------
# 3. Trajectory export
# ---------------------------------------------------------------------------

EXPORT_TURNS = 24  # past the bounded window's 10 user turns, 75 items and 1 MiB
EXPORT_LINES = 1000  # 49,012 bytes per output, under Pi's 50 KB bash cap
BIG_LINES = 3000  # one output past Pi's cap
TRANSCRIPT_LIMIT = 50 * 1024 * 1024


def _export_cmd(tag: str, lines: int) -> str:
    return f"seq -f 'row %05g {tag} abcdefghijklmnopqrstuvwxyz0123' 1 {lines}; printf '%s-%s\\n' END {tag}"


def _db_items(con: sqlite3.Connection, tid: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "select t.turn_item_id, t.type, t.parent_item_id, t.run_id, p.ordinal, t.payload_json from orchestration_v2_projection_turn_items t "
        "left join orchestration_v2_turn_item_positions p on p.thread_id = t.thread_id and p.turn_item_id = t.turn_item_id where t.thread_id = ? order by p.ordinal, t.ordinal",
        (tid,),
    ).fetchall()
    return [{"id": r[0], "type": r[1], "parentItemId": r[2], "runId": r[3], "ordinal": r[4], "payload": json.loads(r[5])} for r in rows]


def _ordered(items: list[dict[str, Any]]) -> list[tuple]:
    return [(i["id"], i["type"], i.get("parentItemId"), i.get("runId")) for i in sorted(items, key=lambda i: i.get("ordinal") or 0)]


def _db_compare(rec: Recorder, db_path: str, export: dict[str, Any], label: str) -> None:
    """Ordered item identity against T3's own database, for one exported thread."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    db_items = _db_items(con, export["thread"]["id"])
    con.close()
    a, b = _ordered(export["items"]), _ordered(db_items)
    rec.check(f"{label}: ordered items (id, type, parent, run) equal the SQLite backup's", a == b and bool(b), {"export": len(a), "db": len(b), "first_difference": next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)})
    db_by_id = {i["id"]: i["payload"] for i in db_items}
    payload_diff = [i["id"] for i in export["items"] if i["type"] in TOOL_TYPES and not _is_t3_only(i) and tool_text(db_by_id.get(i["id"], {}).get("output")) != tool_text(i.get("output"))]
    rec.check(f"{label}: every tool output in the export equals the SQLite backup's payload", not payload_diff, payload_diff[:10])


def op_export(ws: Workspace, rec: Recorder) -> None:
    fx = rec.fixtures
    tags = [f"T{n:02d}{fx.tag(f'export-{n}')}" for n in range(EXPORT_TURNS)]
    cmds = [_export_cmd(tags[n], BIG_LINES if n == 1 else EXPORT_LINES) for n in range(EXPORT_TURNS)]
    prompt = "Run this exact shell command once and then reply with only the last line it printed: {cmd}"
    t0 = time.time()
    first = ws.launch(prompt.format(cmd=cmds[0]), "listing")
    tid = first.thread_id
    rec.facts["thread_id"] = tid
    sends = [first]
    for n in range(EXPORT_TURNS):
        if n > 0:
            sends.append(ws.send(tid, prompt.format(cmd=cmds[n]), kind="fresh-context" if n > 1 else "follow-up", fresh=n > 1))
        o = ws.turn_outcome(tid, sends[n], f"export turn {n}")
        rec.check(f"export turn {n}: run completed and replied with its end marker", o["completed"] and f"END-{tags[n]}" in (o["reply"] or ""), {"status": o["status"], "reply": o["reply"]})
    rec.facts["trajectory_seconds"] = round(time.time() - t0, 1)

    ws.save_native_sessions("sessions")
    sessions = ws.sessions()
    rec.facts["native_sessions"] = len(sessions)

    # The payload-bearing export: every tool item in full, from getTurnItem.
    export = thread_export(ws, tid)
    rec.save_json("export-trajectory.json", {"threads": [export]})
    items = export["items"]
    rec.facts["export_counts"] = {"items": len(items), "runs": len(export["runs"]), "by_type": {t: sum(1 for i in items if i["type"] == t) for t in sorted({i["type"] for i in items})}}
    cmp = compare_to_native(export, sessions)
    rec.facts["native_comparison"] = cmp
    rec.check("export vs the harness's own records, item by item: user text, assistant text, tool inputs and outputs", cmp["ok"], cmp)

    snap = ws.snapshot(tid)
    rec.save_json("http-thread-snapshot.json", snap)
    proj = snap["projection"]
    rec.facts["snapshot_output_omitted"] = sum(1 for i in proj["turnItems"] if i.get("outputOmitted"))
    tr = ws.transcript_raw(tid)
    tbytes = len(tr.body.encode())
    too_large = tr.status == 400 and "thread_transcript_too_large" in tr.body
    rec.facts["transcript_limit"] = {"limit_bytes": TRANSCRIPT_LIMIT, "status": tr.status, "utf8_bytes": tbytes, "crossed": too_large, "body_if_error": tr.body[:300] if tr.status != 200 else None}
    if tr.status == 200:
        t_items = {e["item"]["id"]: e["item"] for e in tr.json()["items"]}
        diff = [i["id"] for i in items if i["type"] == "command_execution" and not _is_t3_only(i) and (t_items.get(i["id"]) or {}).get("output") != i.get("output")]
        rec.check("HTTP transcript: command outputs equal the export's", not diff, {"differences": diff[:5], "items": len(t_items)}, decides=False)
    history = _read_history(ws, rec, tid, proj)
    rec.facts["history"] = history
    rec.check("HTTP bounded snapshot plus history pages reach every item with advancing cursors", history["problem"] is None and history["missing"] == 0 and history["expected"] == len(items), history)

    db = ws.sqlite_backup("final")
    if not db.get("path"):
        rec.check("SQLite backup taken and copied", False, db)
    else:
        con = sqlite3.connect(f"file:{db['path']}?mode=ro&immutable=1", uri=True)
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        con.close()
        rec.check("SQLite backup passes integrity_check", integrity == "ok", integrity)
        _db_compare(rec, db["path"], export, "parent thread")
        rec.facts["sqlite_wal_left_on_host"] = [p.name for p in Path(db["path"]).parent.iterdir() if p.name.endswith(("-wal", "-shm"))]

    turns = proj.get("providerTurns", [])
    rec.check("HTTP snapshot: token counts on every provider turn", len(turns) >= EXPORT_TURNS and all((t.get("tokenUsage") or {}).get("outputTokens") is not None for t in turns), {"provider_turns": len(turns)})
    rec.check("HTTP snapshot: every export run has an attempt naming its harness session", all(any(a["runId"] == s.run_id and a.get("nativeThreadId") for a in export["attempts"]) for s in sends), {"attempts": len(export["attempts"])})
    big = next((i for i in items if i["type"] == "command_execution" and tags[1] in (i.get("input") or "")), None)
    rec.facts["harness_side_truncation_tail"] = (big.get("output") or "")[-200:] if big else None
    _export_subagent(ws, rec)
    # A measurement: a refusal, a timeout or an evidence error here is recorded
    # as its own informational failure and cannot change the verdict above.
    try:
        _measure_large_file_change(ws, rec)
    except Exception as exc:
        rec.facts["large_file_change"] = {"error": f"{type(exc).__name__}: {exc}"[:300], **({"blocked": exc.detail} if isinstance(exc, Blocked) else {})}
        rec.save_text("error-large-file-measurement.txt", traceback.format_exc())
        rec.check("large file write measured (a file over 32 KiB written in one file-write call)", False, rec.facts["large_file_change"], decides=False)


def _measure_large_file_change(ws: Workspace, rec: Recorder) -> None:
    """Measurement, not a verdict: one file write above 32 KiB, and what the
    workspace, T3's export, T3's SQLite backup and the harness session hold."""
    # Informational throughout: its message check and its session copy must
    # not be able to change the export verdict.
    sent = ws.launch(P_BIGFILE, "bigfile", informational=True)
    tid = sent.thread_id
    o = ws.turn_outcome(tid, sent, "large file turn", timeout_s=420)
    size = ws.c.exec(["stat", "-c", "%s", f"{dk.WORKSPACE}/big.txt"], check=False).stdout.strip()
    export = thread_export(ws, tid)
    rec.save_json("export-bigfile.json", {"threads": [export]})
    fc = [i for i in export["items"] if i["type"] == "file_change"]
    ws.save_native_sessions("bigfile", optional=True)
    sessions = ws.sessions()
    calls = {c["id"]: c for sx in sessions for c in sx["tool_calls"]}
    db = ws.sqlite_backup("bigfile")
    db_items = {}
    if db.get("path"):
        con = sqlite3.connect(f"file:{db['path']}?mode=ro&immutable=1", uri=True)
        db_items = {i["id"]: i["payload"] for i in _db_items(con, tid)}
        con.close()
    rows = []
    for item in fc:
        nid = (item.get("nativeItemRef") or {}).get("nativeId")
        args = (calls.get(nid) or {}).get("arguments") or {}
        native_text = args.get("content")
        db_new = (db_items.get(item["id"]) or {}).get("newStr")
        dbi = db_items.get(item["id"]) or {}
        b = lambda v: len(v.encode()) if isinstance(v, str) else None  # noqa: E731
        rows.append({
            "fileName": item.get("fileName"),
            "status": item.get("status"),
            "harness_session_content_bytes": b(native_text),
            "getTurnItem_newStr_bytes": b(item.get("newStr")),
            "getTurnItem_diffStr_bytes": b(item.get("diffStr")),
            "getTurnItem_newStr_ends": (item.get("newStr") or "")[-40:],
            "sqlite_newStr_bytes": b(db_new),
            "sqlite_diffStr_bytes": b(dbi.get("diffStr")),
            "sqlite_newStr_ends": (db_new or "")[-40:],
            "export_equals_harness": item.get("newStr") == native_text,
            "sqlite_equals_harness": db_new == native_text,
        })
    other_tools = [i["type"] for i in export["items"] if i["type"] in TOOL_TYPES and i["type"] != "file_change"]
    rec.facts["large_file_change"] = {"run_status": o["status"], "workspace_file_bytes": int(size) if size.isdigit() else None, "file_changes": rows, "other_tool_items": other_tools}
    rec.check("large file write measured (a file over 32 KiB written in one file-write call)", o["completed"] and bool(rows) and size.isdigit() and int(size) > 32768, rec.facts["large_file_change"], decides=False)


def _read_history(ws: Workspace, rec: Recorder, tid: str, proj: dict[str, Any]) -> dict[str, Any]:
    """Bounded snapshot, then history pages by cursor. Fails on a missing or repeated cursor."""
    bounded = ws.api.get(f"/api/orchestration/threads/{q_(tid)}/bounded")
    rec.save_json("http-bounded-snapshot.json", {k: v for k, v in bounded.items() if k != "projection"} | {"turnItems": len((bounded.get("projection") or {}).get("turnItems", []))})
    seen = {i["id"] for i in (bounded.get("projection") or {}).get("turnItems", [])}
    seen |= {r["item"]["id"] for r in (bounded.get("projection") or {}).get("visibleTurnItems", [])}
    first_window = len(seen)
    cursor, has_more = bounded.get("historyCursor"), bounded.get("hasMoreHistory")
    pages, cursors, problem = 0, [], None
    while has_more:
        if not cursor:
            problem = "hasMoreHistory without a cursor"
            break
        if cursor in cursors:
            problem = "cursor did not advance"
            break
        cursors.append(cursor)
        page = ws.api.get(f"/api/orchestration/threads/{q_(tid)}/history?cursor={q_(cursor)}")
        pages += 1
        before = len(seen)
        for row in page.get("items", []):
            seen.add((row.get("item") or row)["id"])
        if len(seen) == before:
            problem = "page added no items"
            break
        cursor, has_more = page.get("nextCursor"), page.get("hasMoreHistory")
        if pages > 200:
            problem = "more than 200 pages"
            break
    expected = {i["id"] for i in proj["turnItems"]}
    return {"first_window_items": first_window, "pages": pages, "items_seen": len(seen & expected), "expected": len(expected), "missing": len(expected - seen), "problem": problem}


def tool_text(output: Any) -> str | None:
    """A tool item's output as the text the harness recorded: a command's
    output string, or the joined text parts of a dynamic tool's result."""
    if output is None or isinstance(output, str):
        return output
    if isinstance(output, dict) and isinstance(output.get("content"), list):
        return "".join(p.get("text", "") for p in output["content"] if isinstance(p, dict) and p.get("type") == "text")
    return json.dumps(output)


SUBAGENT_MAX_DEPTH = 8


def _export_subagent(ws: Workspace, rec: Recorder) -> None:
    sent = ws.launch(P_SUBAGENT, "lines")
    tid = sent.thread_id
    o = ws.turn_outcome(tid, sent, "subagent turn", timeout_s=360)
    rec.check("subagent turn completed with the reply 1", o["completed"] and _exact(o["reply"], "1"), {"status": o["status"], "reply": o["reply"]})
    deadline = time.time() + 120
    while True:
        snap = ws.snapshot(tid)
        subs = snap["projection"].get("subagents", [])
        if (subs and all(s["status"] in ("completed", "failed", "cancelled", "interrupted") for s in subs)) or time.time() > deadline:
            break
        time.sleep(3)
    ws.save_native_sessions("after-subagent")
    sessions = ws.sessions()
    parent = thread_export(ws, tid)
    threads = [parent]
    info: dict[str, Any] = {"parent_thread": tid, "parent_run": sent.run_id, "tools_called": [i.get("toolName") for i in parent["items"] if i["type"] == "dynamic_tool"], "subagents": []}
    rec.check("subagent: T3 recorded at least one child", bool(subs), len(subs))
    # Recursive: every descendant thread, not only direct children, to a depth
    # limit, and each thread exported once. A child past the limit or listed a
    # second time is recorded as a problem, which fails its check below.
    todo = [(parent, s, 1) for s in subs]
    exported = {tid}
    while todo:
        owner, s, depth = todo.pop(0)
        entry = {k: s.get(k) for k in ("id", "threadId", "runId", "parentNodeId", "origin", "driver", "childThreadId", "nativeTaskRef", "status", "model")}
        entry["depth"] = depth
        entry["owner_thread"] = owner["thread"]["id"]
        if not s.get("childThreadId"):
            entry["problem"] = "no child thread"
            info["subagents"].append(entry)
            continue
        if s["childThreadId"] in exported:
            entry["problem"] = "child thread listed a second time (a cycle or a repeat); not exported again"
            info["subagents"].append(entry)
            continue
        if depth > SUBAGENT_MAX_DEPTH:
            entry["problem"] = f"deeper than {SUBAGENT_MAX_DEPTH} levels; not exported"
            info["subagents"].append(entry)
            continue
        exported.add(s["childThreadId"])
        child = thread_export(ws, s["childThreadId"])
        threads.append(child)
        entry["child_lineage"] = child["thread"].get("lineage")
        entry["child_runs"] = [{"id": r["id"], "status": r["status"]} for r in child["runs"]]
        entry["timeline"] = [i["type"] for i in child["items"]]
        entry["native_comparison"] = compare_to_native(child, sessions)
        info["subagents"].append(entry)
        todo += [(child, gs, depth + 1) for gs in child.get("subagents", [])]
    info["max_depth"] = max((e["depth"] for e in info["subagents"]), default=0)
    parent_cmp = compare_to_native(parent, sessions)
    info["parent_native_comparison"] = parent_cmp
    rec.facts["subagent"] = info
    rec.save_json("export-subagent-trajectory.json", {"threads": threads})
    rec.check("subagent parent thread vs its harness session, item by item", parent_cmp["ok"], parent_cmp)
    for e in info["subagents"]:
        ok = (
            not e.get("problem") and e["id"] and e["threadId"] == e["owner_thread"] and (e["depth"] > 1 or e["runId"] == sent.run_id) and e["parentNodeId"] and e["status"] == "completed"
            and (e.get("child_lineage") or {}).get("parentThreadId") == e["owner_thread"] and (e.get("child_lineage") or {}).get("relationshipToParent") == "subagent"
            and e.get("child_runs") and all(r["status"] == "completed" for r in e["child_runs"])
        )
        rec.check(f"subagent {e['id'][-12:]}: completed, linked to the parent thread, run and node", ok, {k: v for k, v in e.items() if k != "native_comparison"})
        rec.check(f"subagent {e['id'][-12:]}: child thread vs the child's own harness session, item by item", bool(e.get("native_comparison", {}).get("ok")), e.get("native_comparison"))
    db = ws.sqlite_backup("after-subagent")
    if not db.get("path"):
        rec.check("SQLite backup after the subagent taken and copied", False, db)
    else:
        for n, t in enumerate(threads):
            _db_compare(rec, db["path"], t, "subagent parent" if n == 0 else f"subagent child {n}")


# ---------------------------------------------------------------------------
# 4. Mid-run steering
# ---------------------------------------------------------------------------


def op_steering(ws: Workspace, rec: Recorder) -> None:
    steps = " ".join(f"`sleep 6; echo step{n}`," for n in range(1, 7)).rstrip(",")
    first = ws.launch(
        "Run these six shell commands one at a time, each as its own separate tool call, waiting for "
        f"each to finish before starting the next: {steps}. After the last one, reply with the list of steps you ran.",
        "steps",
    )
    tid, run0 = first.thread_id, first.run_id
    rec.facts["thread_id"] = tid
    deadline = time.time() + 180
    done_steps, st = 0, None
    while time.time() < deadline:
        st = ws.state(tid, run0)
        done_steps = len([i for i in ws.items(st["snap"], run0, "command_execution") if re.search(r"echo step\d", i.get("input") or "") and i["status"] == "completed"])
        if done_steps >= 2 or st["kind"] == "finished":
            break
        time.sleep(1.0)
    rec.event("steps completed before steering", steps=done_steps, run_status=st and st["run_status"])
    if st is None or st["kind"] == "finished" or done_steps < 2:
        if st is not None:
            ws.require_completed(st, "steering turn")
        rec.check("turn was running with two steps done when the message was sent", False, {"steps": done_steps, "run_status": st and st["run_status"]})
        return
    caps = next((s.get("capabilities", {}).get("turns", {}) for s in st["snap"]["projection"].get("providerSessions", [])), {})
    rec.facts["provider_turn_capabilities"] = caps
    mode = "steer" if caps.get("supportsActiveSteering") else ("restart" if caps.get("supportsSteeringByInterruptRestart") else "interrupt-then-queue")
    rec.facts["steering_mode"] = mode
    if mode == "interrupt-then-queue":
        ws.interrupt(tid, run0)
        sent = ws.send(tid, STEER_TEXT, kind="steer", mode="queue")
    else:
        sent = ws.send(tid, STEER_TEXT, kind="steer", mode=mode, target_run=run0)
    final_run = sent.run_id
    st_final = ws.wait(tid, final_run, timeout_s=240)
    completed = ws.require_completed(st_final, "steered turn")
    snap = ws.snapshot(tid)
    transcript = ws.transcript(tid)
    rec.save_json("http-transcript.json", transcript)
    rec.save_json("http-thread-snapshot.json", snap)
    runs = {r["id"]: r for r in snap["projection"]["runs"]}
    items = [e["item"] for e in transcript["items"]]
    msg_item = next((i for i in items if i["type"] == "user_message" and i["id"].endswith(sent.message_id)), None)
    step_cmds = [i for i in items if i["type"] == "command_execution" and re.search(r"echo step\d", i.get("input") or "")]
    msg_time = (msg_item or {}).get("startedAt")
    # Every step and the message need both timestamps; without them the order
    # below cannot be read.
    times_ok = bool(msg_time) and bool((msg_item or {}).get("completedAt")) and bool(step_cmds) and all(c.get("startedAt") and c.get("completedAt") for c in step_cmds)
    after = [c for c in step_cmds if times_ok and c["startedAt"] > msg_time]
    rec.facts["timeline"] = [{"type": i["type"], "run": i.get("runId", "")[-10:], "startedAt": i.get("startedAt"), "completedAt": i.get("completedAt"), "text": (i.get("input") or i.get("text") or "")[:70]} for i in items]
    stopped = (_file(ws, "stopped.txt") or "").strip()
    reply = ws.reply(snap, final_run)
    rec.check("correction message is in the timeline by its message id", msg_item is not None and msg_item.get("text") == STEER_TEXT, {"message_id": sent.message_id, "found": msg_item is not None, "run": final_run})
    if mode == "steer":
        rec.check("native steer: correction delivered into the running run (same run id)", final_run == run0 and (msg_item or {}).get("runId") == run0, {"original_run": run0, "message_run": final_run})
    else:
        rec.check(
            "restart: original run interrupted, correction started a new run",
            runs.get(run0, {}).get("status") in ("interrupted", "cancelled") and final_run != run0 and final_run in runs,
            {"original_run": run0, "original_status": runs.get(run0, {}).get("status"), "message_run": final_run},
        )
    rec.check(
        "agent acted on it: run completed, timestamps present, stopped early, at most one step started after the message, did what it asked",
        completed and times_ok and stopped == "halted" and len(step_cmds) < 6 and len(after) <= 1 and "STEERED" in (reply or ""),
        {"run_status": st_final["run_status"], "timestamps_present": times_ok, "stopped.txt": stopped, "step_commands": len(step_cmds), "steps_started_after_message": len(after), "reply": reply},
    )


# ---------------------------------------------------------------------------
# 5. Context reset (fresh context in the same thread)
# ---------------------------------------------------------------------------


def history_access(ws: Workspace, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls and commands that read thread history or T3 or harness state,
    including inside subagent child timelines."""
    hits = []
    for i in items:
        if i["type"] == "subagent" or (i["type"] == "dynamic_tool" and "delegate" in str(i.get("toolName"))):
            hits.append({"type": i["type"], "runId": i.get("runId"), "toolName": i.get("toolName"), "note": "subagent started after the reset"})
        if i["type"] in ("command_execution", "dynamic_tool", "mcp_tool_call", "file_search", "file_read", "file_change", "web_search", "subagent"):
            blob = json.dumps({k: i.get(k) for k in ("input", "toolName", "title", "pattern", "prompt", "fileName", "path", "filePath")})
            if HISTORY_ACCESS.search(blob):
                hits.append({"type": i["type"], "runId": i.get("runId"), "toolName": i.get("toolName"), "input": str(i.get("input"))[:300]})
    return hits


def _post_reset_items(ws: Workspace, tid: str, run_ids: list[str | None]) -> list[dict[str, Any]]:
    snap = ws.snapshot(tid)
    items = [i for i in snap["projection"]["turnItems"] if i.get("runId") in run_ids]
    for sub in snap["projection"].get("subagents", []):
        if sub.get("runId") in run_ids and sub.get("childThreadId"):
            items += ws.snapshot(sub["childThreadId"])["projection"]["turnItems"]
    return items


def op_reset(ws: Workspace, rec: Recorder) -> None:
    word = rec.fixtures.word("ZEBRA", "reset-codeword")
    rec.facts["codeword"] = word
    rec.facts["mechanism"] = "WebSocket message.dispatch with freshContext: true (the composer's /clear)"
    s1 = ws.launch(P_REMEMBER.format(word=word), "notes")
    tid = s1.thread_id
    o1 = ws.turn_outcome(tid, s1, "codeword turn")
    rec.check("codeword turn completed", o1["completed"], {k: o1[k] for k in ("status", "reply")})
    s2 = ws.send(tid, P_ASK, kind="follow-up")
    o2 = ws.turn_outcome(tid, s2, "control turn")
    rec.check("control run completed and replied exactly the codeword", o2["completed"] and _exact(o2["reply"], word), o2["reply"])
    rec.check("control run continued the first run's harness session", bool(o2["native_ids"]) and o2["native_ids"] == o1["native_ids"], {"first": o1["native_ids"], "control": o2["native_ids"]})
    old_ids = sorted(set(o1["native_ids"] + o2["native_ids"]))
    ws.save_native_sessions("before-reset")
    ctrl_files = [native.session_for_native_id(ws.sessions(), i) for i in o2["native_ids"]]
    rec.check("request context: the control session holds the codeword", all(ctrl_files) and all(word in f["raw_text"] for f in ctrl_files), [f and f["file"] for f in ctrl_files])
    moved = _move_out(ws, rec, _native_paths(ws, old_ids), "moved-out")
    rec.facts["moved_out_at_reset"] = moved
    rec.check("old harness session files moved out of the container at the reset", moved["all_moved"], moved)
    s3 = ws.send(tid, P_ASK, kind="fresh-context", fresh=True)
    o3 = ws.turn_outcome(tid, s3, "reset turn")
    runs = {r["id"]: r for r in o3["snap"]["projection"]["runs"]}
    rec.check("reset run completed and replied exactly UNKNOWN", o3["completed"] and _exact(o3["reply"], "UNKNOWN"), o3["reply"])
    rec.check("the reset run carries freshContext and the earlier runs do not", runs.get(s3.run_id, {}).get("freshContext") is True and not any(runs.get(s.run_id, {}).get("freshContext") for s in (s1, s2)), {s.kind: runs.get(s.run_id, {}).get("freshContext") for s in (s1, s2, s3)})
    rec.check("reset run used a new harness session, distinct and nonempty", bool(o3["native_ids"]) and all(r and r not in o2["native_ids"] for r in o3["native_ids"]), {"control": o2["native_ids"], "reset": o3["native_ids"]})
    ws.save_native_sessions("after-reset")
    reset_files = [native.session_for_native_id(ws.sessions(), i) for i in o3["native_ids"]]
    rec.check("request context: the reset session holds the question and not the codeword", all(reset_files) and all(word not in f["raw_text"] and P_ASK in f["user_texts"] for f in reset_files), [f and f["file"] for f in reset_files])
    rec.check("no context handoff summary attached to the reset run", not [h for h in o3["snap"]["projection"].get("contextHandoffs", []) if h.get("targetRunId") == s3.run_id], o3["snap"]["projection"].get("contextHandoffs"))
    s4 = ws.send(tid, P_AFTER, kind="follow-up")
    o4 = ws.turn_outcome(tid, s4, "turn after the reset")
    rec.check("thread still works after the old session file was moved out", o4["completed"] and _exact(o4["reply"], "5"), o4["reply"])
    runs = {r["id"]: r for r in o4["snap"]["projection"]["runs"]}
    rec.facts["runs"] = [{"kind": s.kind, "run": s.run_id, "status": o["status"], "reply": o["reply"], "native_ids": o["native_ids"], "freshContext": runs.get(s.run_id, {}).get("freshContext")} for s, o in ((s1, o1), (s2, o2), (s3, o3), (s4, o4))]
    rec.save_json("http-thread-snapshot.json", o4["snap"])
    rec.save_json("http-transcript.json", ws.transcript(tid))
    hits = history_access(ws, _post_reset_items(ws, tid, [s3.run_id, s4.run_id]))
    rec.facts["history_access_after_reset"] = hits
    rec.check("after the reset: no tool call, command or subagent touched thread history, T3's state or the harness session dir", not hits, hits)


def _native_paths(ws: Workspace, ids: list[str]) -> list[str]:
    paths = []
    for i in ids:
        if i.startswith("/"):
            paths.append(i)
        else:
            out = ws.c.sh(f"find {ws.harness.native_session_dir} -name '{i}.jsonl' 2>/dev/null; true", check=False)
            paths += [line for line in out.splitlines() if line.strip()]
    return paths


def _move_out(ws: Workspace, rec: Recorder, paths: list[str], tag: str) -> dict[str, Any]:
    """Copy each path to the evidence dir (scanned), then delete it in the container."""
    rows = []
    for p in paths:
        res = dk.copy_out_scanned(ws.c.name, p, rec.raw_dir / tag / ws.label, ws.key)
        for f in res["files"]:
            rec.register(f)
        removed = False
        if res["ok"]:
            rm = ws.c.exec(["rm", "-rf", p], check=False)
            removed = rm.returncode == 0 and ws.c.exec(["test", "!", "-e", p], check=False).returncode == 0
        rows.append({"path": p, "copied": res["ok"], "files": len(res["files"]), "copy_error": res["error"], "removed": removed})
    return {"files": rows, "all_moved": bool(rows) and all(r["copied"] and r["removed"] for r in rows)}


# ---------------------------------------------------------------------------
# 5b. Controlled reset by a fresh container with a workspace handoff (D-010)
# ---------------------------------------------------------------------------

P_ROT_MARK = "Create a file named scratch.txt in the project containing exactly {marker}, and a file named lib/scratch.txt in the project containing exactly {inner}. Reply only with OK."
P_ROT_DELETE = "Delete the files scratch.txt and lib/scratch.txt in the project. Reply only with OK."
P_SPILL = "Run this exact shell command once and then reply with only the word done: {cmd}"

# T3's checkpoint refs, in the shapes its own cleanup deletes
# (apps/server/src/storageCleanup.ts, listCheckpointRefs and deleteCheckpointRefs;
# CHECKPOINT_REFS_PREFIX and `ordinal/<n>` in orchestration-v2/CheckpointService.ts).
T3_CHECKPOINT_PREFIXES = ("refs/t3/orchestration-v2/checkpoints/", "refs/t3/checkpoints/")
T3_CHECKPOINT_REF = re.compile(r"^refs/t3/orchestration-v2/checkpoints/[^/]+/ordinal/(0|[1-9][0-9]*)$|^refs/t3/checkpoints/[^/]+/turn/(0|[1-9][0-9]*)$")
HOLD_PREFIX = "refs/hold/"

# Workspace entries a tar must carry exactly: a non-ASCII name, a component
# over 100 bytes, a path over 255 bytes, a symlink and a pair of hard links.
LONG_NAME = "long-" + "x" * 110 + ".txt"
DEEP_PATH = "deep/" + "/".join(["p" * 60] * 5) + "/leaf.txt"
TAR_FIXTURE = ["résumé.txt", LONG_NAME, DEEP_PATH, "run.sh", "link-to-resume", "hard-a.txt", "hard-b.txt"]


def make_tar_fixture(ws: Workspace, tag: str) -> None:
    script = (
        'cd "$0" && printf "%s-r\\n" "$1" > "$2" && chmod 640 "$2"'
        ' && printf "%s-l\\n" "$1" > "$3" && chmod 600 "$3"'
        ' && mkdir -p "$(dirname "$4")" && printf "%s-d\\n" "$1" > "$4"'
        ' && printf "#!/bin/sh\\necho %s\\n" "$1" > run.sh && chmod 755 run.sh'
        ' && ln -s "$2" link-to-resume'
        ' && printf "%s-h\\n" "$1" > hard-a.txt && chmod 600 hard-a.txt && ln hard-a.txt hard-b.txt'
    )
    ws.c.exec(["sh", "-c", script, dk.WORKSPACE, tag, "résumé.txt", LONG_NAME, DEEP_PATH])


def tar_fixture_state(ws: Workspace) -> dict[str, Any]:
    """Type, mode, link count, inode, symlink target and content hash of each fixture entry."""
    script = (
        'cd "$0" || exit 2; for p in "$@"; do'
        ' if [ -L "$p" ] || [ -e "$p" ]; then'
        '  t=$(stat -c "%F|%a|%h|%i" -- "$p");'
        '  l=$( [ -L "$p" ] && readlink -- "$p" || printf "-");'
        '  h=$( [ -f "$p" ] && [ ! -L "$p" ] && sha256sum -- "$p" | cut -d" " -f1 || printf "-");'
        '  printf "%s\\t%s\\t%s\\t%s\\n" "$p" "$t" "$l" "$h";'
        ' else printf "%s\\tmissing\\t-\\t-\\n" "$p"; fi; done'
    )
    out = ws.c.exec(["sh", "-c", script, dk.WORKSPACE, *TAR_FIXTURE], check=False).stdout
    rows = {}
    for line in out.splitlines():
        p, meta, link, sha = line.split("\t")
        kind, mode, nlink, inode = (meta.split("|") + ["", "", ""])[:4]
        rows[p] = {"type": kind, "mode": mode, "nlink": nlink, "inode": inode, "link": link, "sha256": sha, "bytes": len(p.encode())}
    return rows


def git_repositories(ws: Workspace) -> dict[str, Any]:
    """Every Git repository under the workspace: the root, nested repositories
    and submodule checkouts (a `.git` directory or file). Each must answer
    `rev-parse --git-dir`; one that does not is unhandled and fails the op."""
    proc = ws.c.exec(["find", dk.WORKSPACE, "-name", ".git", "-prune", "-print"], check=False)
    found = sorted({str(PurePosixPath(p).parent) for p in proc.stdout.splitlines() if p.strip()})
    repos, unhandled = [], []
    for path in found:
        r = ws.c.exec(["git", "-C", path, "rev-parse", "--git-dir"], check=False)
        if r.returncode == 0:
            repos.append(path)
        else:
            unhandled.append({"path": path, "error": r.stderr.strip()[:200]})
    return {"find_exit": proc.returncode, "repositories": repos, "unhandled": unhandled, "ok": proc.returncode == 0 and not unhandled and dk.WORKSPACE in repos}


def git_object_scan(ws: Workspace, needles: dict[str, bytes], repo: str = dk.WORKSPACE) -> dict[str, Any]:
    """Every object in one repository, loose or packed, reachable or not,
    decompressed by `git cat-file --batch-all-objects --batch`."""
    res = dk.scan_command(["exec", ws.c.name, "git", "-C", repo, "cat-file", "--batch-all-objects", "--batch"], needles)
    refs = ws.c.exec(["git", "-C", repo, "for-each-ref", "--format=%(refname)"], check=False)
    count = ws.c.exec(["git", "-C", repo, "count-objects", "-v"], check=False).stdout
    return {**res, "repo": repo, "refs": refs.stdout.split(), "refs_exit": refs.returncode, "count_objects": dict(l.split(": ", 1) for l in count.splitlines() if ": " in l)}


def _unreachable(ws: Workspace, repo: str) -> tuple[int, list[str], str]:
    p = ws.c.exec(["git", "-C", repo, "fsck", "--unreachable", "--no-progress"], check=False)
    return p.returncode, sorted({l.split()[2] for l in p.stdout.splitlines() if l.startswith("unreachable ")}), p.stderr.strip()[:200]


def git_drop_t3_refs(ws: Workspace, repo: str) -> dict[str, Any]:
    """Delete only T3's checkpoint refs (its own prefixes and shapes, with
    --no-deref, so a symbolic checkpoint ref goes and its target stays), and
    only the objects that deletion left unreachable. No reflog is expired.
    Objects already unreachable before are held by temporary refs while
    `repack -a -d` and `prune --expire=now` run, then released and shown present."""
    steps: list[dict[str, Any]] = []

    def step(name: str, args: list[str], stdin: str | None = None) -> Any:
        p = ws.c.exec(["git", "-C", repo, *args], input_text=stdin, check=False)
        steps.append({"step": name, "exit": p.returncode, "stderr": p.stderr.strip()[:200]})
        return p

    listed = step("for-each-ref (T3 checkpoint prefixes)", ["for-each-ref", "--format=%(refname)", *T3_CHECKPOINT_PREFIXES])
    names = listed.stdout.split()
    refs = [r for r in names if T3_CHECKPOINT_REF.match(r)]
    fsck_exit, before, fsck_err = _unreachable(ws, repo)
    steps.append({"step": "fsck --unreachable (before)", "exit": fsck_exit, "stderr": fsck_err})
    if refs:
        step("update-ref --no-deref: delete T3 checkpoint refs", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"delete {r}\0\0" for r in refs))
    # The holds live in a namespace made for this invocation, so a task's own
    # refs/hold/ refs are neither touched nor mistaken for leftovers.
    hold_prefix = f"{HOLD_PREFIX.rstrip('/')}-{secrets.token_hex(6)}/"
    holds = [(f"{hold_prefix}{sha}", sha) for sha in before]
    held = True
    if holds:
        made = step("update-ref: hold the objects unreachable before", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"create {h}\0{sha}\0" for h, sha in holds))
        held = made.returncode == 0
    if fsck_exit == 0 and held:
        # Nothing is pruned unless the earlier unreachable set is known and held.
        step("repack -a -d", ["repack", "-a", "-d", "-q"])
        step("prune --expire=now", ["prune", "--expire=now"])
    else:
        steps.append({"step": "repack and prune skipped: earlier unreachable objects not held", "exit": 1, "stderr": ""})
    if holds:
        step("update-ref --no-deref: release the holds", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"delete {h}\0\0" for h, _ in holds))
    missing: list[str] = []
    if before:
        chk = step("cat-file --batch-check (objects unreachable before)", ["cat-file", "--batch-check"], "".join(f"{sha}\n" for sha in before))
        missing = [l.split()[0] for l in chk.stdout.splitlines() if l.endswith(" missing")]
    left = ws.c.exec(["git", "-C", repo, "for-each-ref", "--format=%(refname)", *T3_CHECKPOINT_PREFIXES, hold_prefix], check=False)
    left_refs = [r for r in left.stdout.split() if T3_CHECKPOINT_REF.match(r) or r.startswith(hold_prefix)]
    ok = all(st["exit"] == 0 for st in steps) and left.returncode == 0 and not missing and not left_refs
    return {
        "repo": repo,
        "t3_refs_deleted": refs,
        "other_refs_under_t3_prefixes_kept": [r for r in names if r not in refs],
        "unreachable_before": before,
        "unreachable_before_missing_after": missing,
        "refs_left": left_refs,
        "steps": steps,
        "all_steps_ok": ok,
    }


def workspace_listing(ws: Workspace) -> list[str]:
    """Every path in the workspace with type, mode, owner, mtime to the second
    (a tar keeps no more), symlink target, size and link count for non-directories,
    and a content hash for files."""
    meta = ws.c.exec(["sh", "-c", f"cd {dk.WORKSPACE} && find . -printf '%P|%y|%m|%u|%g|%Ts|%l|' \\( -type d -printf '-\\n' -o -printf '%s %n\\n' \\) | sort"], check=False).stdout.splitlines()
    sums = ws.c.exec(["sh", "-c", f"cd {dk.WORKSPACE} && find . -type f -print0 | sort -z | xargs -0 sha256sum"], check=False).stdout.splitlines()
    return meta + sums


def files_holding(ws: Workspace, needles: list[str]) -> dict[str, Any]:
    """Evidence only (it decides what nothing is moved): files outside the
    workspace that hold any needle, found as root."""
    proc = ws.c.exec(["grep", "-rlF", "-D", "skip", "-f", "-", "/home", "/tmp", "/root", "/var/tmp"], input_text="\n".join(needles), check=False, user="0")
    return {"exit": proc.returncode, "complete": proc.returncode in (0, 1), "files": [l for l in proc.stdout.splitlines() if l.strip()]}


def _stop_server(ws: Workspace) -> dict[str, Any]:
    pids = ws.c.exec(["pgrep", "-f", "t3 serve"], check=False).stdout.split()
    ws.c.exec(["kill", *pids], check=False)
    deadline = time.time() + 30
    left = pids
    while time.time() < deadline:
        left = ws.c.exec(["pgrep", "-f", "t3 serve"], check=False).stdout.split()
        if not left:
            return {"stopped": True, "pids": pids}
        time.sleep(0.3)
    ws.c.exec(["kill", "-9", *left], check=False)
    return {"stopped": not ws.c.exec(["pgrep", "-f", "t3 serve"], check=False).stdout.split(), "pids": pids, "killed_9": True}


def op_handoff(ws: Workspace, rec: Recorder) -> None:
    fx = rec.fixtures
    word, note = fx.word("HERON", "handoff-codeword"), fx.word("NOTE", "handoff-note")
    spill_tag, ckpt, draft = fx.tag("handoff-spill"), fx.word("MARK", "handoff-checkpoint"), fx.word("DRAFT", "handoff-draft")
    inner, dangle, keep = fx.word("INNER", "handoff-nested"), fx.word("LOOSE", "handoff-dangling"), fx.word("KEEP", "handoff-task-ref")
    spill = f"SPILL-{spill_tag}"
    rec.facts.update({"codeword": word, "note": note, "spill_marker": spill, "checkpoint_marker": ckpt, "draft_marker": draft, "nested_marker": inner, "dangling_marker": dangle, "task_ref_marker": keep})
    rec.facts["mechanism"] = "export the thread, run the container's end checks, stop T3, drop T3's checkpoint refs in every repository of the workspace, copy the workspace out as a tar, remove the container, start a fresh container from the same image on the same network and gateway with the workspace copied in, launch a new thread"
    nested = f"{dk.WORKSPACE}/lib"
    markers = {"checkpoint": ckpt.encode(), "nested": inner.encode(), "draft": draft.encode(), "dangling": dangle.encode(), "keep": keep.encode()}

    # Fixtures in the workspace: tar edge cases and a nested repository.
    make_tar_fixture(ws, fx.tag("handoff-tar"))
    ws.c.exec(["sh", "-c", 'mkdir -p "$0" && cd "$0" && git init -q && printf "lib\\n" > README && git add README && git commit -qm init', nested])

    # Segment 1.
    s1 = ws.launch(P_ROT_FIRST.format(word=word, note=note), "notes")
    tid1 = s1.thread_id
    o1 = ws.turn_outcome(tid1, s1, "codeword turn")
    rec.check("codeword turn completed and notes.txt holds the note", o1["completed"] and (_file(ws, "notes.txt") or "").strip() == note, {"status": o1["status"]})
    # The spill marker is the first line of a 3,001-line output; Pi keeps the
    # last 50 KB for the model and writes the whole output to /tmp.
    cmd = f"printf '%s-%s\\n' SPILL {spill_tag}; seq -f 'row %05g abcdefghijklmnopqrstuvwxyz0123456789' 1 3000"
    s2 = ws.send(tid1, P_SPILL.format(cmd=cmd), kind="follow-up")
    o2 = ws.turn_outcome(tid1, s2, "spill turn")
    rec.check("spill turn completed", o2["completed"] and _exact(o2["reply"], "done"), {"status": o2["status"], "reply": o2["reply"]})
    s3 = ws.send(tid1, P_ROT_MARK.format(marker=ckpt, inner=inner), kind="follow-up")
    o3 = ws.turn_outcome(tid1, s3, "checkpoint-marker turn")
    rec.check("checkpoint-marker turn completed; scratch.txt and lib/scratch.txt hold their markers", o3["completed"] and (_file(ws, "scratch.txt") or "").strip() == ckpt and (_file(ws, "lib/scratch.txt") or "").strip() == inner, {"status": o3["status"]})
    s4 = ws.send(tid1, P_ROT_DELETE, kind="follow-up")
    o4 = ws.turn_outcome(tid1, s4, "delete turn")
    rec.check("delete turn completed; scratch.txt and lib/scratch.txt are gone", o4["completed"] and _file(ws, "scratch.txt") is None and _file(ws, "lib/scratch.txt") is None, {"status": o4["status"]})

    # Task-owned Git state T3's cleanup must leave alone: a commit reachable
    # only from the reflog; a loose object nothing reaches; a ref under refs/t3/
    # outside T3's checkpoint prefix; a branch with a symbolic ref to it that
    # has the shape of a checkpoint ref (deleted, the branch kept).
    script = (
        f"cd {dk.WORKSPACE} && printf '%s\\n' {draft} > draft.txt && git add draft.txt && git commit -qm draft && git rev-parse HEAD && git reset -q --hard HEAD~1"
        f" && printf '%s\\n' {dangle} | git hash-object -w --stdin"
        f" && b=$(printf '%s\\n' {keep} | git hash-object -w --stdin) && t=$(printf '100644 blob %s\\tkeep.txt\\n' $b | git mktree) && c=$(git commit-tree $t -m keep) && git update-ref refs/t3/task/saved $c && echo $c"
        " && git branch side HEAD && git rev-parse refs/heads/side"
        " && git symbolic-ref refs/t3/orchestration-v2/checkpoints/c2lkZQ/ordinal/9 refs/heads/side"
    )
    sha, dangle_sha, keep_sha, side_sha = ws.c.sh(script).split()[:4]
    rec.facts["task_git"] = {"draft_commit": sha, "dangling_blob": dangle_sha, "task_ref": {"refs/t3/task/saved": keep_sha}, "side_branch": side_sha, "symbolic_ref": "refs/t3/orchestration-v2/checkpoints/c2lkZQ/ordinal/9 -> refs/heads/side"}

    # Every repository of the workspace, found while T3 still runs.
    repos = git_repositories(ws)
    rec.facts["repositories_before"] = repos
    rec.check("every Git repository under the workspace found and readable (root and nested)", repos["ok"] and nested in repos["repositories"], repos)
    if not repos["ok"]:
        return
    fixture_before = tar_fixture_state(ws)
    scans_before = {r: git_object_scan(ws, markers, r) for r in repos["repositories"]}
    t3_refs_before = {r: [x for x in v["refs"] if T3_CHECKPOINT_REF.match(x)] for r, v in scans_before.items()}
    unreachable_before = _unreachable(ws, dk.WORKSPACE)[1]
    before = {
        "files_holding": files_holding(ws, [spill, word, ckpt]),
        "export_scan": dk.scan_container_export(ws.c.name, ws.key, {"spill": spill.encode(), "codeword": word.encode(), "checkpoint": ckpt.encode(), "nested": inner.encode()}),
        "git": {r: {k: v[k] for k in ("hits", "refs", "refs_exit", "count_objects")} for r, v in scans_before.items()},
        "draft_in_reflog": sha in ws.c.exec(["git", "-C", dk.WORKSPACE, "reflog", "--format=%H"], check=False).stdout,
        "dangling_blob_unreachable": dangle_sha in unreachable_before,
    }
    rec.facts["before_handoff"] = before
    root_b, nest_b = scans_before[dk.WORKSPACE]["hits"], scans_before[nested]["hits"]
    rec.check(
        "positive controls before the handoff: spill marker outside the workspace; checkpoint marker in a root object and nested marker in a nested-repository object, both with T3 checkpoint refs; draft commit in the reflog only; the loose object unreachable; the task ref and the symbolic ref present",
        before["export_scan"]["hits"]["spill"] > 0 and root_b["checkpoint"] > 0 and nest_b["nested"] > 0 and bool(t3_refs_before[dk.WORKSPACE]) and bool(t3_refs_before[nested])
        and root_b["draft"] > 0 and before["draft_in_reflog"] and before["dangling_blob_unreachable"] and root_b["keep"] > 0
        and "refs/t3/task/saved" in scans_before[dk.WORKSPACE]["refs"] and "refs/t3/orchestration-v2/checkpoints/c2lkZQ/ordinal/9" in scans_before[dk.WORKSPACE]["refs"],
        {"hits": {r: v["hits"] for r, v in scans_before.items()}, "t3_refs": {r: len(v) for r, v in t3_refs_before.items()}, "draft_in_reflog": before["draft_in_reflog"], "dangling_blob_unreachable": before["dangling_blob_unreachable"], "spill_export_hits": before["export_scan"]["hits"]["spill"]},
    )
    # Export segment 1 with payloads, its backup and its sessions.
    seg1 = thread_export(ws, tid1)
    rec.save_json("segment-1-transcript.json", ws.transcript(tid1))
    ws.save_native_sessions("segment-1")
    db1 = ws.sqlite_backup("segment-1")
    sess_before = ws.sessions()
    inj_before = native.injected_material(ws.harness.name, {"sessions": sess_before})
    # The first container gets the same end checks as any other before it goes.
    container_end(ws, rec, phase="segment-1")

    # The handoff.
    t0 = time.time()
    stop = _stop_server(ws)
    cleanup = {r: git_drop_t3_refs(ws, r) for r in repos["repositories"]}
    listing_old = workspace_listing(ws)
    tar = dk.workspace_tar(ws.c.name, ws.key)
    t_copied = time.time()
    ws.retire_container("segment-1")
    ws.boot(ws.image, add_project=True, workspace_tar=tar, phase="segment-2")
    t_ready = time.time()
    listing_new = workspace_listing(ws)
    rec.facts["handoff"] = {"stop": stop, "git": cleanup, "workspace_tar_bytes": len(tar), "seconds_to_workspace_copied": round(t_copied - t0, 2), "seconds_to_new_container_ready": round(t_ready - t0, 2)}
    rec.check(
        "handoff: T3 stopped; in every repository only T3's checkpoint refs deleted (--no-deref), every object unreachable before still present, no reflog expired; old container removed, new container ready",
        stop["stopped"] and all(c["all_steps_ok"] for c in cleanup.values()),
        {"stop": stop, "git": {r: {k: v for k, v in c.items() if k != "unreachable_before"} | {"unreachable_before": len(c["unreachable_before"])} for r, c in cleanup.items()}},
    )
    diff = sorted(set(listing_old) ^ set(listing_new))
    rec.check("the workspace in the new container equals the handed-over one (paths, types, modes, owners, sizes, link counts, symlink targets, mtimes to the second, contents)", not diff and bool(listing_old), {"entries": len(listing_old), "differences": diff[:20]})
    fixture_after = tar_fixture_state(ws)
    same = {p: all(fixture_before.get(p, {}).get(k) == fixture_after.get(p, {}).get(k) for k in ("type", "mode", "nlink", "link", "sha256")) and fixture_after.get(p, {}).get("type") not in (None, "missing") for p in TAR_FIXTURE}
    hard_ok = fixture_after.get("hard-a.txt", {}).get("inode") == fixture_after.get("hard-b.txt", {}).get("inode") and fixture_after.get("hard-a.txt", {}).get("nlink") == "2"
    rec.facts["tar_fixture"] = {"before": fixture_before, "after": fixture_after, "same": same, "hard_links_share_an_inode": hard_ok}
    rec.check("tar fixture arrived exactly: non-ASCII name, component over 100 bytes, path over 255 bytes, executable, symlink target, hard links sharing one inode", all(same.values()) and hard_ok, rec.facts["tar_fixture"])
    stray = ws.c.exec(["sh", "-c", "for p in /tmp/project /tmp/.w; do [ -e \"$p\" ] && echo \"$p\"; done; true"], check=False).stdout.split()
    rec.check("the new container has no stray /tmp/project or /tmp/.w", not stray, stray)

    # After the handoff: every repository again.
    repos_after = git_repositories(ws)
    scans_after = {r: git_object_scan(ws, markers, r) for r in repos_after["repositories"]}
    rec.facts["git_after_handoff"] = {r: {k: v[k] for k in ("hits", "refs", "refs_exit", "count_objects", "bytes")} for r, v in scans_after.items()}
    rec.check("the same repositories after the handoff", repos_after["ok"] and repos_after["repositories"] == repos["repositories"], repos_after)
    clean = {
        r: v["exit"] == 0 and v["bytes"] > 0 and v["refs_exit"] == 0 and v["hits"]["checkpoint"] == 0 and v["hits"]["nested"] == 0 and not [x for x in v["refs"] if T3_CHECKPOINT_REF.match(x)]
        for r, v in scans_after.items()
    }
    rec.check("neither the checkpoint marker nor the nested marker is in any object of any repository, and no T3 checkpoint ref remains", bool(clean) and all(clean.values()), rec.facts["git_after_handoff"])
    g = lambda *a: ws.c.exec(["git", "-C", dk.WORKSPACE, *a], check=False).stdout.strip()  # noqa: E731
    root_a = scans_after.get(dk.WORKSPACE, {}).get("hits", {})
    draft_type, draft_reflog = g("cat-file", "-t", sha), sha in g("reflog", "--format=%H")
    rec.check("the task's reflog-only commit survived the handoff (object present, still in the reflog)", draft_type == "commit" and draft_reflog and root_a.get("draft", 0) > 0, {"sha": sha, "type": draft_type, "in_reflog": draft_reflog})
    dangle_type = g("cat-file", "-t", dangle_sha)
    rec.check("the task's loose unreachable object survived the handoff", dangle_type == "blob" and root_a.get("dangling", 0) > 0, {"sha": dangle_sha, "type": dangle_type})
    task_ref, side_now = g("rev-parse", "--verify", "-q", "refs/t3/task/saved"), g("rev-parse", "--verify", "-q", "refs/heads/side")
    symref_left = g("for-each-ref", "--format=%(refname)", "refs/t3/orchestration-v2/checkpoints/c2lkZQ/")
    rec.check(
        "the task ref outside T3's prefix survived with its object; the symbolic checkpoint-shaped ref is gone and the branch it pointed to is unchanged",
        task_ref == keep_sha and root_a.get("keep", 0) > 0 and side_now == side_sha and not symref_left,
        {"refs/t3/task/saved": task_ref, "expected": keep_sha, "refs/heads/side": side_now, "expected_side": side_sha, "symbolic_ref_left": symref_left},
    )
    scan = dk.scan_container_export(ws.c.name, ws.key, {"spill": spill.encode(), "codeword": word.encode(), "checkpoint": ckpt.encode(), "nested": inner.encode()})
    rec.check("the new container's whole filesystem holds neither the spill marker, the codeword, the checkpoint marker nor the nested marker", all(dk.scan_ok(scan, n) for n in ("spill", "codeword", "checkpoint", "nested")), scan)

    s5 = ws.launch(P_ASK, "notes")
    tid2 = s5.thread_id
    o5 = ws.turn_outcome(tid2, s5, "first turn after the handoff")
    rec.check("after the handoff: the agent does not know the codeword (run completed, reply exactly UNKNOWN)", o5["completed"] and _exact(o5["reply"], "UNKNOWN"), o5["reply"])
    s6 = ws.send(tid2, P_ROT_NOTE, kind="follow-up")
    o6 = ws.turn_outcome(tid2, s6, "note turn after the handoff")
    rec.check("after the handoff: notes.txt is readable by the runner and quoted by the agent", o6["completed"] and (_file(ws, "notes.txt") or "").strip() == note and _exact(o6["reply"], note), {"reply": o6["reply"]})
    shell_ids = [t["id"] for t in ws.shell().get("threads", [])]
    old_read = ws.api.get_raw(f"/api/orchestration/threads/{q_(tid1)}")
    db2 = ws.sqlite_backup("segment-2")
    db_rows = None
    if db2.get("path"):
        con = sqlite3.connect(f"file:{db2['path']}?mode=ro&immutable=1", uri=True)
        db_rows = con.execute("select count(*) from orchestration_v2_projection_turn_items where thread_id = ?", (tid1,)).fetchone()[0] + con.execute("select count(*) from orchestration_v2_projection_threads where thread_id = ?", (tid1,)).fetchone()[0]
        con.close()
    rec.check("after the handoff: the earlier thread is not listable or readable (shell, HTTP read, database)", shell_ids == [tid2] and old_read.status == 404 and db_rows == 0, {"shell_threads": shell_ids, "old_thread_http_status": old_read.status, "old_thread_db_rows": db_rows})
    seg2 = thread_export(ws, tid2)
    t1, t2 = seg1["thread"], seg2["thread"]
    keys = ("providerInstanceId", "modelSelection", "runtimeMode", "interactionMode")
    rec.check("after the handoff: the new thread reports the same harness, model and settings", all(t1.get(k) == t2.get(k) for k in keys), {"before": {k: t1.get(k) for k in keys}, "after": {k: t2.get(k) for k in keys}})
    joined = {"segments": [{"segment": 1, **seg1}, {"segment": 2, **seg2}], "handoff": rec.facts["handoff"]}
    rec.save_json("joined-trajectory.json", joined)
    last1 = max((i.get("completedAt") or i.get("startedAt") or "") for i in seg1["items"])
    first2 = min((i.get("startedAt") or "") for i in seg2["items"])
    shared = {i["id"] for i in seg1["items"]} & {i["id"] for i in seg2["items"]}
    rec.check("the joined trajectory has disjoint ids and segment 1 ends before segment 2 starts", bool(seg1["items"]) and bool(seg2["items"]) and not shared and last1 < first2, {"segment_1_items": len(seg1["items"]), "segment_2_items": len(seg2["items"]), "last_1": last1, "first_2": first2})
    ws.save_native_sessions("segment-2")
    sess_all = ws.sessions()
    for n, (seg, db) in enumerate(((seg1, db1), (seg2, db2)), 1):
        cmp = compare_to_native(seg, sess_all)
        rec.check(f"segment {n} vs its harness session, item by item", cmp["ok"], cmp)
        if db.get("path"):
            _db_compare(rec, db["path"], seg, f"segment {n}")
        else:
            rec.check(f"segment {n}: SQLite backup taken and copied", False, db)
    sess_after = [x for x in sess_all if x["file"] not in {b_["file"] for b_ in sess_before}]
    inj_after = native.injected_material(ws.harness.name, {"sessions": sess_after})
    rec.facts["first_start_differences"] = {
        "t3_instructions_equal": (inj_before or {}).get("t3_instructions") == (inj_after or {}).get("t3_instructions"),
        "tool_list_equal": (inj_before or {}).get("tools") == (inj_after or {}).get("tools"),
        "first_user_texts_after": [x["user_texts"][:2] for x in sess_after],
    }
    hits = history_access(ws, _post_reset_items(ws, tid2, [s5.run_id, s6.run_id]))
    rec.facts["history_access_after_handoff"] = hits
    rec.check("after the handoff: no tool call, command or subagent touched thread history or state", not hits, hits)


# ---------------------------------------------------------------------------
# 6. Snapshot and resume
# ---------------------------------------------------------------------------


def _ask(ws: Workspace, tid: str, text: str, kind: str, informational: bool = False) -> dict[str, Any]:
    sent = ws.send(tid, text, kind=kind, informational=informational)
    o = ws.turn_outcome(tid, sent, kind, timeout_s=300)
    return {"sent": sent, **{k: v for k, v in o.items() if k != "snap"}, "items": _post_reset_items(ws, tid, [sent.run_id])}


def _image_checks(rec: Recorder, image: str, key: dk.KeySource, tag: str, cleanup: list[dict[str, Any]]) -> bool:
    """Key checks on a snapshot image, in order; the first failure stops them.
    Returns False if any check failed or was incomplete: the caller must not
    start a copy from the image."""
    clean = not dk.key_in_inspect(image, key, kind="image") and not dk.key_in_image_history(image, key)
    rec.check(f"snapshot image ({tag}) config and history do not hold the key", clean, {"image": image}, hygiene=True)
    rec.save_text(f"snapshot-{tag}-history.txt", dk.image_history(image))
    if not clean:
        return False
    saved = dk.scan_image_save(image, key)
    ok = dk.scan_ok(saved)
    rec.check(f"snapshot image ({tag}): real key in no layer (docker save, layers decompressed)", ok, saved, hygiene=True)
    if not ok:
        return False
    registry: list[tuple[str, str]] = []
    ok = False
    try:
        c = dk.start_harness(image, None, label=f"nonet-{tag}", cleanup=registry)
        scan = dk.scan_container_export(c.name, key)
        ok = dk.scan_ok(scan)
        rec.check(f"snapshot image ({tag}) started with no network: real key nowhere (docker export scan)", ok, scan, hygiene=True)
    finally:
        for res in dk.remove_registered(registry):
            cleanup.append(res)
            rec.check(f"cleanup: no-network container ({tag}) removed and confirmed absent", res["confirmed_absent"], res, hygiene=True)
    return ok


def recall_violations(ws: Workspace, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A recall prompt needs no tools: any tool call or delegation in the recall
    run is a violation, and so is any history access (the reset op's audit)."""
    tools = [{"type": i["type"], "toolName": i.get("toolName"), "input": str(i.get("input"))[:200]} for i in items if i["type"] in TOOL_TYPES and "workspace-preparation" not in i["id"]]
    return tools + history_access(ws, items)


def _recall_checks(rec: Recorder, ws: Workspace, label: str, ask: dict[str, Any], word: str, seed_native: list[str], decides: bool) -> None:
    rec.check(f"{label}: run completed and answered exactly the codeword", ask["completed"] and _exact(ask["reply"], word), {k: ask.get(k) for k in ("run_id", "status", "reply")}, decides=decides)
    rec.check(f"{label}: continued the codeword run's harness session (same native session id)", bool(seed_native) and ask["native_ids"] == seed_native, {"seed": seed_native, "this": ask["native_ids"]}, decides=decides)
    v = recall_violations(ws, ask["items"])
    rec.check(f"{label}: no tool call, delegation or history access in the recall run", not v, v, decides=decides)


def _branch_local(rec: Recorder, orig: Workspace, cp: Workspace, tid: str, o_sent: Sent, c_sent: Sent, tag: str, decides: bool) -> None:
    o_msgs = {m["id"]: m["text"] for m in orig.snapshot(tid)["projection"]["messages"]}
    c_msgs = {m["id"]: m["text"] for m in cp.snapshot(tid)["projection"]["messages"]}
    rec.check(
        f"{tag}: histories are branch-local (each container holds its own question and not the other's)",
        o_sent.message_id in o_msgs and c_sent.message_id in c_msgs and c_sent.message_id not in o_msgs and o_sent.message_id not in c_msgs and c_sent.text not in o_msgs.values() and o_sent.text not in c_msgs.values(),
        {"original_has_copy_question": c_sent.message_id in o_msgs, "copy_has_original_question": o_sent.message_id in c_msgs},
        decides=decides,
    )


def _wait_item_running(ws: Workspace, tid: str, run_id: str | None, needle: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        st = ws.state(tid, run_id)
        cmds = [i for i in ws.items(st["snap"], run_id, "command_execution") if needle in (i.get("input") or "")]
        running = [i for i in cmds if i["status"] == "running"]
        last = {"run_status": st["run_status"], "items": [{"id": i["id"], "status": i["status"]} for i in cmds]}
        if st["run_status"] == "running" and len(cmds) == 1 and running:
            return {"ok": True, "run": run_id, "item": running[0]["id"], **last}
        if st["kind"] == "finished":
            break
        time.sleep(0.5)
    return {"ok": False, "run": run_id, **last}


def _busy_variant(ws: Workspace, rec: Recorder, tid: str, word: str, seed_native: list[str], tag: str, continuation: bool, images: list[str], cleanup: list[dict[str, Any]], copies: list[Workspace], images_registry: list[tuple[str, str]]) -> dict[str, Any]:
    """Characterisation only (D-009). Every outcome is recorded in the returned
    block; nothing here is a deciding check. Key and cleanup checks stay hygiene."""
    barrier = f"/tmp/go-{rec.fixtures.tag(f'barrier-{tag}')}"
    result: dict[str, Any] = {"continuation": continuation, "barrier": barrier}
    try:
        if not continuation:
            out = ws.api.rpc("server.updateSettings", {"patch": {"continueThreadsAfterServerUpdate": False}})
            result["setting_after_patch"] = (out or {}).get("continueThreadsAfterServerUpdate")
        busy = ws.send(tid, P_BUSY.format(tag=barrier.rsplit("-", 1)[1]), kind=f"follow-up (busy, {tag})", informational=True)
        active = _wait_item_running(ws, tid, busy.run_id, barrier, timeout_s=90)
        result["gate"] = active
        if not active["ok"]:
            result["outcome"] = "the barrier command never ran"
            return result
        img = dk.commit_snapshot(ws.c, tag, images_registry)
        images.append(img)
        still = _wait_item_running(ws, tid, busy.run_id, barrier, timeout_s=5)
        result["still_running_after_commit"] = still["ok"] and still.get("item") == active["item"]
        ws.c.exec(["touch", barrier])
        if not _image_checks(rec, img, ws.key, tag, cleanup):
            result["outcome"] = "image key check failed or was incomplete; no copy started"
            return result
        cp = Workspace(ws.harness, ws.image, ws.key, rec, label=f"copy-{tag}", gateway_image=ws.gateway_image)
        copies.append(cp)
        cp.start(from_image=img, add_project=False)
        time.sleep(3)
        proj = cp.snapshot(tid)["projection"]
        orig_run = next((r for r in proj["runs"] if r["id"] == busy.run_id), {})
        item = next((i for i in proj["turnItems"] if i["id"] == active["item"]), {})
        result["copy_initial"] = {"busy_run_status": orig_run.get("status"), "item_status": item.get("status"), "item_present": bool(item)}
        cp.c.exec(["touch", barrier])
        deadline = time.time() + 240
        later: list[dict[str, Any]] = []
        while time.time() < deadline:
            proj = cp.snapshot(tid)["projection"]
            orig_run = next((r for r in proj["runs"] if r["id"] == busy.run_id), {})
            later = [r for r in proj["runs"] if r["ordinal"] > orig_run.get("ordinal", 1e9)]
            if orig_run.get("status") in TERMINAL and all(r["status"] in TERMINAL for r in later):
                break
            time.sleep(2)
        replays = [i for i in proj["turnItems"] if i["type"] == "command_execution" and barrier in (i.get("input") or "") and i["id"] != active["item"]]
        result["copy_after_recovery"] = {"busy_run_status": orig_run.get("status"), "later_runs": [{"id": r["id"][-12:], "status": r["status"]} for r in later], "replayed_commands": len(replays)}
        ask = _ask(cp, tid, P_ASK_COPY_2 if continuation else P_ASK_COPY_3, f"follow-up (copy, {tag})", informational=True)
        result["copy_answer"] = {k: ask.get(k) for k in ("status", "completed", "reply", "native_ids")}
        result["copy_answer_exact"] = ask["completed"] and _exact(ask["reply"], word)
        result["copy_answer_same_session"] = ask["native_ids"] == seed_native
        result["copy_answer_violations"] = recall_violations(cp, ask["items"])
        busy_done = ws.wait(tid, busy.run_id, timeout_s=240)
        result["original_busy"] = {"status": busy_done["run_status"], "completed": busy_done["run_status"] == "completed", "reply": ws.reply(busy_done["snap"], busy.run_id)}
        ask_o = _ask(ws, tid, P_ASK_ORIG_2 if continuation else P_ASK_ORIG_3, f"follow-up (original, {tag})", informational=True)
        result["original_answer"] = {k: ask_o.get(k) for k in ("status", "completed", "reply", "native_ids")}
        result["original_answer_exact"] = ask_o["completed"] and _exact(ask_o["reply"], word)
        result["original_answer_same_session"] = ask_o["native_ids"] == seed_native
        _branch_local(rec, ws, cp, tid, ask_o["sent"], ask["sent"], f"busy {tag} (characterisation)", decides=False)
        container_end(cp, rec, decides=False)
        result["outcome"] = "completed"
    except Exception as exc:  # Blocked included: a characterisation never decides the op
        result["outcome"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        if not continuation:
            try:
                ws.api.rpc("server.updateSettings", {"patch": {"continueThreadsAfterServerUpdate": True}})
            except Exception as exc:
                result["setting_restore_error"] = str(exc)[:200]
    return result


IDLE_SHELL_STATUSES = ("idle", "completed")


def _idle_gate(ws: Workspace, tid: str, seed_run: str | None) -> dict[str, Any]:
    """A turn boundary, failing closed. Every thread in the tree (the thread and,
    recursively, its subagent children) must be readable; none may have a run
    that is not terminal, a pending runtime request, or a pending background
    task on a provider thread. The root's shell row must exist, with status
    idle or completed, no activity run, no pending request, no background task
    and no pending guest message. Every child's shell row is checked the same
    way, and a missing child row fails the gate: the shell route is read
    without `omitIdleSubagents=1` (orchestration-v2/http.ts), so T3 lists idle
    children too. The seed run must be completed."""
    problems: list[str] = []
    threads: dict[str, dict[str, Any]] = {}
    todo, seen = [tid], set()
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        try:
            proj = ws.snapshot(t)["projection"]
        except Exception as exc:
            problems.append(f"thread {t[-24:]} unreadable: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        active = [{"run": r["id"][-16:], "status": r["status"]} for r in proj["runs"] if r["status"] not in TERMINAL]
        pending = [r["id"][-24:] for r in proj.get("runtimeRequests", []) if r.get("status") == "pending"]
        background = [b for pt in proj.get("providerThreads", []) for b in (pt.get("pendingBackgroundTasks") or [])]
        threads[t] = {"runs_not_terminal": active, "pending_requests": pending, "provider_background_tasks": background}
        if active or pending or background:
            problems.append(f"thread {t[-24:]} not idle: {threads[t]}")
        if t == tid:
            seed = next((r for r in proj["runs"] if r["id"] == seed_run), {})
            threads[t]["seed_status"] = seed.get("status")
            if seed.get("status") != "completed":
                problems.append(f"seed run status {seed.get('status')}")
        todo += [x["childThreadId"] for x in proj.get("subagents", []) if x.get("childThreadId")]
    try:
        shell_rows = {r["id"]: r for r in ws.shell().get("threads", [])}
    except Exception as exc:
        shell_rows = None
        problems.append(f"shell unreadable: {type(exc).__name__}: {str(exc)[:120]}")
    shells = {}
    for t in threads:
        row = (shell_rows or {}).get(t)
        if row is None:
            shells[t] = None
            if shell_rows is not None:
                problems.append("no shell row for the thread" if t == tid else f"no shell row for child thread {t[-24:]}")
            continue
        shells[t] = {k: row.get(k) for k in ("status", "activityRunStatus", "pendingRuntimeRequest", "pendingBackgroundTasks", "pendingGuestMessageCount", "blockedOnThreadWait")}
        if row.get("status") not in IDLE_SHELL_STATUSES or row.get("activityRunStatus") or row.get("pendingRuntimeRequest") or row.get("pendingBackgroundTasks") or row.get("pendingGuestMessageCount") or row.get("blockedOnThreadWait"):
            problems.append(f"shell row of {t[-24:]} not idle: {shells[t]}")
    return {"threads": threads, "shell_rows": shells, "problems": problems, "ok": not problems}


def op_snapshot(ws: Workspace, rec: Recorder) -> None:
    word = rec.fixtures.word("OTTER", "snapshot-codeword")
    rec.facts["codeword"] = word
    rec.facts["mechanism"] = "docker commit of the running harness container at a turn boundary, docker run of the image behind a new gateway, WebSocket send to the same thread id"
    s1 = ws.launch(P_REMEMBER.format(word=word), "notes")
    tid = s1.thread_id
    o1 = ws.turn_outcome(tid, s1, "codeword turn")
    rec.check("seed turn completed", o1["completed"], {k: o1[k] for k in ("status", "reply", "native_ids")})
    rec.facts["thread_id"] = tid
    seed_native = o1["native_ids"]
    gate = _idle_gate(ws, tid, s1.run_id)
    rec.facts["idle_gate"] = gate
    rec.check("turn boundary before the commit: seed run completed; every thread in the tree readable, with no unfinished run, pending request or background task; the shell row present and idle", gate["ok"], gate)
    if not gate["ok"]:
        return
    copies: list[Workspace] = []
    images: list[str] = []
    images_registry: list[tuple[str, str]] = []
    cleanup: list[dict[str, Any]] = []
    try:
        # Decides the verdict: a snapshot at a turn boundary (D-009).
        img_a = dk.commit_snapshot(ws.c, "idle", images_registry)
        images.append(img_a)
        rec.event("snapshot committed", image=img_a, variant="turn boundary")
        if not _image_checks(rec, img_a, ws.key, "idle", cleanup):
            raise dk.KeyLeak("snapshot image key check failed or was incomplete; no copy started")
        copy_a = Workspace(ws.harness, ws.image, ws.key, rec, label="copy-idle", gateway_image=ws.gateway_image)
        copies.append(copy_a)
        copy_a.start(from_image=img_a, add_project=False)
        ask_c = _ask(copy_a, tid, P_ASK_COPY, "follow-up (copy)")
        _recall_checks(rec, copy_a, "idle copy", ask_c, word, seed_native, True)
        copy_a.save_native_sessions("recall")
        cs = next((native.session_for_native_id(copy_a.sessions(), n) for n in ask_c["native_ids"]), None)
        rec.check(
            "idle copy: its harness session holds the codeword before the copy's question (request context)",
            bool(cs) and word in cs["raw_text"] and P_ASK_COPY in cs["user_texts"] and cs["raw_text"].find(word) < cs["raw_text"].find(P_ASK_COPY),
            cs and cs["file"],
        )
        ask_o = _ask(ws, tid, P_ASK_ORIG, "follow-up (original)")
        _recall_checks(rec, ws, "idle original", ask_o, word, seed_native, True)
        _branch_local(rec, ws, copy_a, tid, ask_o["sent"], ask_c["sent"], "idle", decides=True)
        rec.save_json("copy-idle-snapshot.json", copy_a.snapshot(tid))
        container_end(copy_a, rec)
        copy_a.finish()

        busy: dict[str, Any] = {}
        for tag, continuation in (("busy", True), ("busy-nocontinue", False)):
            busy[tag] = _busy_variant(ws, rec, tid, word, seed_native, tag, continuation, images, cleanup, copies, images_registry)
        rec.facts["busy_characterisation"] = busy
    finally:
        for cp in copies:
            try:
                cp.finish()
            except Exception as exc:
                rec.check(f"cleanup: copy {cp.label} finished", False, str(exc), hygiene=True)
        for res in dk.remove_registered(images_registry):
            cleanup.append(res)
            rec.check(f"cleanup: snapshot image {res.get('image', '?').split(':')[-1]} removed and confirmed absent", res["confirmed_absent"], res, hygiene=True)
        rec.facts["cleanup"] = cleanup
    rec.save_json("original-snapshot.json", ws.snapshot(tid))


OPS: dict[str, OpFn] = {
    "startup": op_startup,
    "messaging": op_messaging,
    "pending": op_pending,
    "export": op_export,
    "steering": op_steering,
    "reset": op_reset,
    "handoff": op_handoff,
    "snapshot": op_snapshot,
}
