"""Readers for T3 Code's records of a thread.

Three sources give the same record shapes (checked on run
2026-11-21T210951-74da66: the SQLite payloads of thread, runs, attempts,
messages and subagents equal the export's, and so do the turn items except
that the export's `subagent` items carry a `progress` key the database copy
lacks; see cohbench/trajectory/README.md):

- the SQLite backup (`statev2` copy), the primary source;
- the payload-bearing export (`thread_export` in `driver_proof/ops.py`), the
  same records without runtime requests;
- the HTTP thread snapshot, where tool items carry `outputOmitted` and no
  output, and file changes carry no text. The HTTP transcript fills both;
  outputs can also come from the harness session (see `normalise`).

Nothing here interprets the records; it only reads them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Thread:
    thread: dict[str, Any]
    runs: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    subagents: list[dict[str, Any]]
    items: list[dict[str, Any]]  # in T3's position order
    runtime_requests: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""

    @property
    def id(self) -> str:
        return self.thread["id"]


def _by_ordinal(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: (i.get("ordinal") is None, i.get("ordinal") or 0))


def from_sqlite(path: str | Path) -> dict[str, Thread]:
    """Every thread in a T3 SQLite backup, opened read-only and immutable."""
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro&immutable=1", uri=True)
    try:
        def rows(table: str, tid: str, order: str = "") -> list[dict[str, Any]]:
            q = f"select payload_json from {table} where thread_id = ?" + (f" order by {order}" if order else "")
            return [json.loads(r[0]) for r in con.execute(q, (tid,))]

        out: dict[str, Thread] = {}
        for (tid, payload) in con.execute("select thread_id, payload_json from orchestration_v2_projection_threads order by created_at"):
            items = [
                json.loads(r[0])
                for r in con.execute(
                    "select t.payload_json from orchestration_v2_projection_turn_items t left join orchestration_v2_turn_item_positions p "
                    "on p.thread_id = t.thread_id and p.turn_item_id = t.turn_item_id where t.thread_id = ? order by p.ordinal, t.ordinal",
                    (tid,),
                )
            ]
            out[tid] = Thread(
                thread=json.loads(payload),
                runs=sorted(rows("orchestration_v2_projection_runs", tid), key=lambda r: r["ordinal"]),
                attempts=rows("orchestration_v2_projection_run_attempts", tid, "attempt_ordinal"),
                messages=rows("orchestration_v2_projection_messages", tid, "created_at"),
                subagents=rows("orchestration_v2_projection_subagents", tid),
                items=items,
                runtime_requests=rows("orchestration_v2_projection_runtime_requests", tid, "created_at"),
                source=f"sqlite:{Path(path).name}",
            )
        return out
    finally:
        con.close()


def _thread_from_export(t: dict[str, Any], source: str) -> Thread:
    return Thread(
        thread=t["thread"],
        runs=sorted(t["runs"], key=lambda r: r["ordinal"]),
        attempts=t.get("attempts", []),
        messages=t.get("messages", []),
        subagents=t.get("subagents", []),
        items=_by_ordinal(t["items"]),
        runtime_requests=t.get("runtimeRequests", []),
        source=source,
    )


def from_export(obj: dict[str, Any] | str | Path) -> dict[str, Thread]:
    """`{"threads": [...]}` as `export-trajectory.json` holds it."""
    name = "export"
    if not isinstance(obj, dict):
        name = f"export:{Path(obj).name}"
        obj = json.loads(Path(obj).read_text())
    return {t["thread"]["id"]: _thread_from_export(t, name) for t in obj["threads"]}


def from_joined(obj: dict[str, Any] | str | Path) -> list[dict[str, Thread]]:
    """`joined-trajectory.json` of a handoff: one dict of threads per segment."""
    name = "joined"
    if not isinstance(obj, dict):
        name = f"joined:{Path(obj).name}"
        obj = json.loads(Path(obj).read_text())
    segs = []
    for s in obj["segments"]:
        threads = s.get("threads") or [s]
        segs.append({t["thread"]["id"]: _thread_from_export(t, f"{name}#segment{s.get('segment')}") for t in threads})
    return segs


def from_snapshot(obj: dict[str, Any] | str | Path, transcript: dict[str, Any] | str | Path | None = None) -> dict[str, Thread]:
    """An HTTP thread snapshot (`{"projection": ...}`). Tool outputs are
    omitted there; a transcript (`{"items": [{"item": ...}]}`) fills those it holds."""
    name = "snapshot"
    if not isinstance(obj, dict):
        name = f"snapshot:{Path(obj).name}"
        obj = json.loads(Path(obj).read_text())
    proj = obj["projection"]
    items = _by_ordinal(proj["turnItems"])
    if transcript is not None:
        if not isinstance(transcript, dict):
            transcript = json.loads(Path(transcript).read_text())
        full = {e["item"]["id"]: e["item"] for e in transcript.get("items", []) if isinstance(e.get("item"), dict)}
        merged = []
        for i in items:
            f = full.get(i["id"])
            if f is not None:
                # The transcript's item is the fuller record: fields the snapshot
                # leaves out (a command's output, a file change's newStr) come from it.
                extra = {k: v for k, v in f.items() if k not in i}
                if i.get("outputOmitted") and "output" in f and not f.get("outputOmitted"):
                    extra.update(output=f["output"], outputOmitted=False, _output_source="transcript")
                i = {**i, **extra}
            merged.append(i)
        items = merged
    t = Thread(
        thread=proj["thread"],
        runs=sorted(proj["runs"], key=lambda r: r["ordinal"]),
        attempts=proj.get("attempts", []),
        messages=proj.get("messages", []),
        subagents=proj.get("subagents", []),
        items=items,
        runtime_requests=proj.get("runtimeRequests", []),
        source=name,
    )
    return {t.id: t}


def tool_text(output: Any) -> str | None:
    """A tool item's output as text: a command's string, or the joined text
    parts of a dynamic tool's result."""
    if output is None or isinstance(output, str):
        return output
    if isinstance(output, dict) and isinstance(output.get("content"), list):
        return "".join(p.get("text", "") for p in output["content"] if isinstance(p, dict) and p.get("type") == "text")
    return json.dumps(output, sort_keys=True)
