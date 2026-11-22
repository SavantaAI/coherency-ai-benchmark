"""Rebuild the fixtures in this directory from a saved driver-proof run.

    python tests/trajectory/fixtures/build_fixtures.py runs/driver-proof/2026-11-21T210951-74da66

The run directory is gitignored; the fixtures are committed. Each fixture is
cut down from the run: large command outputs keep their last three lines
(and Pi's truncation notice), large tool results that the format withholds
are shortened, system prompts and tool definitions are dropped from session
files, and snapshot projections keep only the records the reader uses. The
same cut is applied to T3's copy and the harness session's copy of a text,
so the cross-check still compares like with like. The saved runs use only
made-up codewords, markers and a made-up PIN.

`--interrupt CAPTURE` builds `pi-interrupt-child-question.json` instead,
from a driver integration capture (a `state.sqlite` and a `sessions/`
directory): a budget cutoff interrupting a tree whose child had a pending
question, made on 2026-11-22 with a scripted upstream (no model).

Not built here: `watcher-real/` (real watcher output, made by its own
`make_fixture.py` in a Linux container) and `identifiers-task.json` (written
by hand).
"""

from __future__ import annotations

import glob
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
KEEP_PROJECTION = ("thread", "runs", "attempts", "messages", "subagents", "turnItems", "runtimeRequests")
NOTICE = re.compile(r"\n\n\[Showing lines \d+-\d+ of \d+[^\]]*\]\Z")


def cut_text(s: str, lines: int = 3, limit: int = 600) -> str:
    if not isinstance(s, str) or len(s) <= limit:
        return s
    m = NOTICE.search(s)
    tail = m.group(0) if m else ""
    body = s[: m.start()] if m else s
    return "\n".join(body.split("\n")[-lines:]) + tail


def cut_item(i: dict) -> dict:
    i = dict(i)
    if i["type"] == "command_execution" and isinstance(i.get("output"), str):
        i["output"] = cut_text(i["output"])
    if i["type"] == "dynamic_tool" and isinstance(i.get("output"), dict) and isinstance(i["output"].get("content"), list):
        name = i.get("toolName") or ""
        if name.endswith("orchestrator_capabilities") or name == "tool_search":
            i["output"] = {"content": [{"type": "text", "text": c["text"][:400] + " [cut for the fixture]"} for c in i["output"]["content"] if c.get("type") == "text"]}
    return i


def cut_session(path: str, out: Path) -> None:
    """A Pi session with its system message dropped and long tool results cut the same way."""
    rows = []
    for line in Path(path).read_text().splitlines():
        e = json.loads(line)
        if e.get("type") == "message":
            msg = e["message"]
            if msg.get("role") == "system":
                continue
            msg.pop("usage", None)
            msg.pop("details", None)  # Pi keeps a second copy of long output here; the reader does not use it
            if msg.get("role") == "toolResult":
                for c in msg.get("content") or []:
                    if c.get("type") == "text":
                        if msg.get("toolName") == "tool_search":
                            c["text"] = c["text"][:400] + " [cut for the fixture]"
                        else:
                            c["text"] = cut_text(c["text"])
                if msg.get("toolName", "").endswith("orchestrator_capabilities"):
                    for c in msg.get("content") or []:
                        c["text"] = c["text"][:400] + " [cut for the fixture]"
        elif e.get("type") not in ("session", "model_change", "thinking_level_change"):
            continue
        rows.append(json.dumps(e, ensure_ascii=False))
    out.write_text("\n".join(rows) + "\n")


def projection(snap: dict) -> dict:
    p = snap["projection"]
    return {"projection": {k: p.get(k, []) for k in KEEP_PROJECTION}}


def sessions_for(threads: list[dict], dirs: list[Path], dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    names = {Path(a["nativeThreadId"]).name for t in threads for a in t["attempts"] if a.get("nativeThreadId")}
    written = []
    for name in sorted(names):
        src = None
        for d in dirs:
            hits = glob.glob(str(d / "**" / name), recursive=True)
            if hits:
                src = hits[0]
        if src:
            cut_session(src, dest / name)
            written.append(name)
    return written


def main(run: Path) -> None:
    pi = run / "pi"
    # 1. The 24-turn export, first three runs: a normal turn, Pi's truncated output, a fresh-context turn.
    con = sqlite3.connect(f"file:{next(pi.glob('export/t3-db-pi-final/*.sqlite'))}?mode=ro&immutable=1", uri=True)
    ex = json.loads((pi / "export/export-trajectory.json").read_text())["threads"][0]
    runs = ex["runs"][:3]
    ids = {r["id"] for r in runs}
    t = {**ex, "runs": runs, "attempts": [a for a in ex["attempts"] if a["runId"] in ids], "messages": [m for m in ex["messages"] if m.get("runId") in ids],
         "items": [cut_item(i) for i in ex["items"] if i.get("runId") in ids], "providerTurns": []}
    con.close()
    (HERE / "pi-export-three-turns.json").write_text(json.dumps({"threads": [t]}, indent=1, ensure_ascii=False))
    sessions_for([t], [pi / "export/native/pi/final"], HERE / "sessions/pi-export")

    # 2. The subagent export: parent and child threads.
    sub = json.loads((pi / "export/export-subagent-trajectory.json").read_text())
    for th in sub["threads"]:
        th["items"] = [cut_item(i) for i in th["items"]]
        th["providerTurns"] = []
    (HERE / "pi-subagent.json").write_text(json.dumps(sub, indent=1, ensure_ascii=False))
    sessions_for(sub["threads"], [pi / "export/native/pi/after-subagent"], HERE / "sessions/pi-subagent")

    # 3. The handoff: two segments from two containers.
    joined = json.loads((pi / "handoff/joined-trajectory.json").read_text())
    for s in joined["segments"]:
        s["items"] = [cut_item(i) for i in s["items"]]
        s["providerTurns"] = []
    joined["handoff"] = {k: joined["handoff"][k] for k in ("seconds_to_workspace_copied", "seconds_to_new_container_ready")}
    (HERE / "pi-handoff-joined.json").write_text(json.dumps(joined, indent=1, ensure_ascii=False))
    sessions_for(joined["segments"][:1], [pi / "handoff/native/pi/final-segment-1"], HERE / "sessions/pi-handoff-1")
    sessions_for(joined["segments"][1:], [pi / "handoff/native/pi/final"], HERE / "sessions/pi-handoff-2")

    # 4. Pending question (diagnostic ask_user) and secret request, as HTTP snapshots.
    for name in ("diagnostic", "secret"):
        snap = projection(json.loads((pi / f"pending/{name}-thread-snapshot.json").read_text()))
        (HERE / f"pi-pending-{name}-snapshot.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False))
        sessions_for([{"attempts": snap["projection"]["attempts"]}], [pi / "pending/native/pi/final"], HERE / f"sessions/pi-pending-{name}")

    # 5. Steering: snapshot plus the transcript that holds the command outputs.
    snap = projection(json.loads((pi / "steering/http-thread-snapshot.json").read_text()))
    (HERE / "pi-steering-snapshot.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False))
    tr = json.loads((pi / "steering/http-transcript.json").read_text())
    tr = {"items": [{"item": cut_item(e["item"])} for e in tr["items"]]}
    (HERE / "pi-steering-transcript.json").write_text(json.dumps(tr, indent=1, ensure_ascii=False))
    sessions_for([{"attempts": snap["projection"]["attempts"]}], [pi / "steering/native/pi/final"], HERE / "sessions/pi-steering")

    # 6. The send_to_user item as getTurnItem returned it (the messaging operation saved no thread record).
    st = json.loads((pi / "messaging/send-to-user-items.json").read_text())
    (HERE / "pi-send-to-user-item.json").write_text(json.dumps(st["getTurnItem"][0], indent=1, ensure_ascii=False))

    # 7. Claude Code's refused startup turn: the only Claude Code record there is.
    snap = projection(json.loads((run / "claude/startup/thread-snapshot.json").read_text()))
    (HERE / "claude-refused-startup-snapshot.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False))
    src = next(Path(p) for p in glob.glob(str(run / "claude/startup/native/claude/final/projects/-work-project/*.jsonl")))
    rows = []
    for line in src.read_text().splitlines():
        e = json.loads(line)
        if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict):
            keep = {k: e.get(k) for k in ("type", "uuid", "parentUuid", "timestamp", "isApiErrorMessage", "isMeta", "sessionId")}
            msg = e["message"]
            keep["message"] = {k: msg.get(k) for k in ("role", "content", "model")}
            rows.append(json.dumps(keep, ensure_ascii=False))
    d = HERE / "sessions/claude-refused"
    d.mkdir(parents=True, exist_ok=True)
    (d / src.name).write_text("\n".join(rows) + "\n")
    (HERE / "claude-tool-names.json").write_text(json.dumps(json.loads((run / "claude/startup/injected-tool-names.json").read_text()), indent=1))


def interrupt_capture(capture: Path) -> None:
    """Every thread of the capture's T3 backup, in export form, and its Pi
    sessions cut like the others."""
    sys.path.insert(0, str(HERE.parents[2]))
    from cohbench.trajectory import t3

    threads = t3.from_sqlite(capture / "state.sqlite")
    out = {"threads": [{"thread": t.thread, "runs": t.runs, "attempts": t.attempts, "messages": t.messages, "subagents": t.subagents,
                        "items": [cut_item(i) for i in t.items], "runtimeRequests": t.runtime_requests, "providerTurns": []} for t in threads.values()]}
    (HERE / "pi-interrupt-child-question.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    sessions_for(out["threads"], [capture / "sessions"], HERE / "sessions/pi-interrupt-child-question")


def queued_refusal(run_dir: Path) -> None:
    """The runner's secret-refusal episode with the refusal queued as an
    ordinary user message (`runs/runner-integration/2026-11-22T090323-secret`,
    whose export was rejected): its raw record trimmed into
    `queued-refusal/`, the T3 backup as export JSON (the test writes the
    SQLite file back), the Pi session cut like the others."""
    import shutil

    sys.path.insert(0, str(HERE.parents[2]))
    from cohbench.trajectory import t3

    src = run_dir / "export.rejected"
    out = HERE / "queued-refusal"
    if out.exists():
        shutil.rmtree(out)
    (out / "export" / "segment-1").mkdir(parents=True)
    threads = t3.from_sqlite(src / "segment-1" / "t3-state.sqlite")
    obj = {"threads": [{"thread": t.thread, "runs": t.runs, "attempts": t.attempts, "messages": t.messages, "subagents": t.subagents,
                        "items": [cut_item(i) for i in t.items], "runtimeRequests": t.runtime_requests, "providerTurns": []} for t in threads.values()]}
    (out / "export" / "segment-1" / "threads.json").write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    sessions_for(obj["threads"], [src / "segment-1" / "sessions"], out / "export" / "segment-1" / "sessions")
    shutil.copy(src / "manifest.json", out / "export" / "manifest.json")
    wsrc, wdst = src / "watcher" / "segment-1", out / "export" / "watcher" / "segment-1"
    wdst.mkdir(parents=True)
    for name in ("events.jsonl", "metrics.jsonl", "heartbeat.json", "config.json"):
        if (wsrc / name).exists():
            shutil.copy(wsrc / name, wdst / name)
    if (wsrc / "blobs").is_dir():
        shutil.copytree(wsrc / "blobs", wdst / "blobs")
    (out / "runner").mkdir()
    shutil.copy(run_dir / "runner" / "events.jsonl", out / "runner" / "events.jsonl")
    for name in ("run.json", "episode-record.json"):
        shutil.copy(run_dir / name, out / name)
    # The repository's own path, made relative to it (the test resolves it).
    run = out / "run.json"
    run.write_text(run.read_text().replace(str(HERE.parents[2]) + "/", ""))


if __name__ == "__main__":
    if sys.argv[1] == "--queued-refusal":
        queued_refusal(Path(sys.argv[2]))
    elif sys.argv[1] == "--interrupt":
        interrupt_capture(Path(sys.argv[2]))
    else:
        main(Path(sys.argv[1]))
