"""Reading a turn's state from T3's records. Pure functions, no Docker.

How a script tells the states apart (docs/driver-proof.md, "Telling the
three states apart"):

* question: a `user_input_request` item in the run with status `waiting`,
  whose `requestId` is a `runtimeRequests` entry with kind `user_input` and
  status `pending`;
* secret: a `secret_request` item in the run with `secretStatus: pending`;
* finished: the run's status is terminal.

Refusal by the model API is read from the gateway log as in the proof
(`driver_proof/gatewaylog.py`): the run is terminal with both times, at
least one inference request lies in its window and no other run's, the last
user message of a bound request is the run's message, every bound request
is a refusal, and none is ambiguous.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "rolled_back"}
TOOL_TYPES = ("command_execution", "dynamic_tool", "mcp_tool_call", "file_change", "file_read", "file_search", "web_search", "subagent")
SEND_TO_USER = "send_to_user"


@dataclass
class Question:
    """A pending question, in whichever thread of the tree asked it."""

    text: str
    options: list[str]
    thread_id: str
    item_id: str
    request_id: str
    question_ids: list[str]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class SecretRequest:
    """A pending secret request, in whichever thread of the tree asked it."""

    label: str
    reason: str | None
    fields: list[dict[str, Any]]
    thread_id: str
    item_id: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def run_by_id(proj: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    return next((r for r in proj.get("runs", []) if r["id"] == run_id), None)


def classify(proj: dict[str, Any], run_id: str) -> dict[str, Any]:
    """The state of one run. A terminal run is finished, whatever items it
    left behind; otherwise a pending question or secret request in the run,
    or running."""
    run = run_by_id(proj, run_id)
    if run is None:
        return {"kind": "running", "run_status": None, "questions": [], "secrets": [], "run": None, "reason": "run not in the projection yet"}
    status = run.get("status")
    items = [i for i in proj.get("turnItems", []) if i.get("runId") == run_id]
    q, s = pending_requests(proj, items)
    if status in TERMINAL:
        kind = "finished"
    elif q:
        kind = "question"
    elif s:
        kind = "secret"
    else:
        kind = "running"
    return {"kind": kind, "run_status": status, "questions": q, "secrets": s, "run": run}


def pending_requests(proj: dict[str, Any], items: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pending question items (with their runtime request pending) and pending
    secret items among `items` (default: every item of the projection) whose
    run is not terminal."""
    items = proj.get("turnItems", []) if items is None else items
    live_runs = {r["id"] for r in proj.get("runs", []) if r.get("status") not in TERMINAL}
    pending = {r["id"]: r for r in proj.get("runtimeRequests", []) if r.get("status") == "pending"}
    questions = [i for i in items if i["type"] == "user_input_request" and i.get("status") == "waiting" and i.get("runId") in live_runs and pending.get(i.get("requestId"), {}).get("kind") == "user_input"]
    secrets_ = [i for i in items if i["type"] == "secret_request" and i.get("secretStatus") == "pending" and i.get("runId") in live_runs]
    return questions, secrets_


def question_of(item: dict[str, Any], thread_id: str) -> Question:
    qs = item.get("questions") or []
    lines, options = [], []
    for q in qs:
        lines.append(q.get("question") or q.get("header") or "")
        options += [str(o.get("value") or o.get("label")) for o in q.get("options") or []]
    return Question(
        text="\n".join(x for x in lines if x) or (item.get("title") or ""),
        options=options,
        thread_id=thread_id,
        item_id=item["id"],
        request_id=item.get("requestId") or "",
        question_ids=[q.get("id") for q in qs if q.get("id")],
        raw=item,
    )


def secret_of(item: dict[str, Any], thread_id: str) -> SecretRequest:
    return SecretRequest(label=item.get("label") or item.get("title") or "", reason=item.get("reason"), fields=list(item.get("fields") or []), thread_id=thread_id, item_id=item["id"], raw=item)


def answers_for(question: Question, answer: str | dict[str, str] | list[str]) -> dict[str, str]:
    """T3's `runtime-request.respond` answers: one value per question id. A
    single string answers a prompt with one question; a prompt with several
    takes a dict keyed by question id or a list in question order. A value
    that matches an option's label or value (case-insensitive) is sent as
    that option's value."""
    qs = question.raw.get("questions") or []
    if isinstance(answer, str):
        if len(qs) != 1:
            raise ValueError(f"the prompt has {len(qs)} questions; answer each (a dict by question id or a list)")
        given = {qs[0]["id"]: answer}
    elif isinstance(answer, list):
        if len(answer) != len(qs):
            raise ValueError(f"{len(answer)} answers for {len(qs)} questions")
        given = {q["id"]: a for q, a in zip(qs, answer)}
    else:
        given = dict(answer)
        missing = [q["id"] for q in qs if q["id"] not in given]
        if missing:
            raise ValueError(f"no answer for question(s) {missing}")
    out = {}
    for q in qs:
        value = given[q["id"]]
        for opt in q.get("options") or []:
            if value.strip().lower() in (str(opt.get("value", "")).lower(), str(opt.get("label", "")).lower()):
                value = opt.get("value") or opt.get("label")
        out[q["id"]] = value
    return out


def answers_match(stored: Any, sent: dict[str, str]) -> bool:
    """Whether T3's recorded answers (`runtimeRequests[].answers`, a record
    keyed by question id whose values may be a string, a list or an object
    holding them) are the ones sent: each sent value found under its
    question id."""
    if not isinstance(stored, dict):
        return False

    def strings(v: Any) -> list[str]:
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [x for i in v for x in strings(i)]
        if isinstance(v, dict):
            return [x for i in v.values() for x in strings(i)]
        return []

    return all(qid in stored and value in strings(stored[qid]) for qid, value in sent.items())


FOLLOWING = set(TOOL_TYPES) | {"user_input_request", "secret_request"}


def final_item(proj: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """The final message's item (see `final_message`)."""
    run = run_by_id(proj, run_id)
    if run is None or run.get("status") != "completed":
        return None
    items = [i for i in proj.get("turnItems", []) if i.get("runId") == run_id and (i["type"] == "assistant_message" or i["type"] in FOLLOWING)]
    items.sort(key=lambda i: (i.get("ordinal") is None, i.get("ordinal") or 0))
    if not items or items[-1]["type"] != "assistant_message":
        return None
    return items[-1]


def final_message(proj: dict[str, Any], run_id: str) -> str | None:
    """What the user sees when the turn ends: the run's last assistant
    message, only when the run completed and no tool call, question, secret
    request or subagent start follows it in the run (the trajectory unit's
    rule). Items without an ordinal sort last."""
    item = final_item(proj, run_id)
    return item.get("text") if item else None


def is_send_to_user(item: dict[str, Any], names: tuple[str, ...]) -> bool:
    return item.get("type") == "dynamic_tool" and item.get("toolName") in names


def sent_message(full_item: dict[str, Any]) -> str | None:
    """The text T3 showed the user for a send_to_user call: the result's
    `structuredContent.sentMessage`, not the input, for a completed call only."""
    if full_item.get("status") != "completed":
        return None
    out = full_item.get("output") or {}
    if not isinstance(out, dict):
        return None
    sc = out.get("structuredContent") or {}
    msg = sc.get("sentMessage") if isinstance(sc, dict) else None
    return msg if isinstance(msg, str) else None


def run_errors(proj: dict[str, Any], run_id: str) -> list[str]:
    out = []
    for i in proj.get("turnItems", []):
        if i["type"] == "error" and i.get("runId") == run_id:
            failure = i.get("failure") or {}
            out.append(" | ".join(str(x) for x in (i.get("title"), failure.get("code"), failure.get("message"), i.get("text")) if x))
    return out


def turn_capabilities(proj: dict[str, Any]) -> dict[str, Any]:
    return next((s.get("capabilities", {}).get("turns", {}) for s in proj.get("providerSessions", []) if s.get("capabilities")), {})


def steering_mechanism(caps: dict[str, Any]) -> str:
    """Native steering when the harness reports it; otherwise interrupt and
    restart (SPEC section 3: such runs are marked)."""
    if caps.get("supportsActiveSteering"):
        return "native_steer"
    if caps.get("supportsSteeringByInterruptRestart"):
        return "interrupt_restart"
    return "interrupt_then_queue"


# ---------------------------------------------------------------------------
# The whole tree (D-029): one projection decides hand-back and interruption
# ---------------------------------------------------------------------------


def _ordinal(run: dict[str, Any]) -> tuple:
    return (run.get("ordinal") is None, run.get("ordinal") or 0)


def pending_continuations(proj: dict[str, Any]) -> list[str]:
    """Runs of this thread with a delegated completion T3 has reserved and
    not yet dispatched: an open cohort with a delivery whose message is not
    in the thread yet. T3's own idleness check reads the same fields
    (Orchestrator.ts: `delegatedCompletion.disposition === "open"`, a
    delivery, and no message with its id); when the child ends, T3 queues
    that message and starts a new parent run with it."""
    have = {m["id"] for m in proj.get("messages", [])}
    out = []
    for r in proj.get("runs", []):
        dc = r.get("delegatedCompletion") or {}
        delivery = dc.get("delivery")
        if dc.get("disposition") == "open" and delivery and delivery.get("messageId") not in have:
            out.append(r["id"])
    return out


def started_by(proj: dict[str, Any], run: dict[str, Any]) -> str:
    """`user` when the run's message came from the user (createdBy user),
    `t3` when T3 sent it itself (a delegated completion, createdBy agent)."""
    mid = run.get("userMessageId")
    msg = next((m for m in proj.get("messages", []) if m["id"] == mid), None) if mid else None
    if msg is None:
        return "unknown"
    return "user" if msg.get("createdBy") in (None, "user") and not msg.get("delegatedCompletion") else "t3"


def busy_state(tree: dict[str, dict[str, Any] | Exception], root: str) -> dict[str, Any]:
    """The one definition of busy, over one projection of every thread,
    used by every caller (wait, interrupt, reply, correct, the reset and
    probe gate, a probe copy's wait):

    * `requests`: every pending question or secret request whose own run
      has not ended, in delivery order (start time, then ordinal), each as
      (kind, thread, Question or SecretRequest);
    * `live`: runs not terminal and not queued, as (thread, run, status);
    * `queued`: runs T3 queued and has not started, as (thread, run);
    * `continuations`: delegated completions T3 reserved and has not
      dispatched (it will start a parent run), as (thread, run);
    * `background`: provider background work still pending, as (thread,
      count), whatever the status of the run that owns it;
    * `unreadable`: threads whose read failed, as (thread, reason): neither
      idle nor busy, the tree's state is unknown;
    * `latest`: the main thread's latest run; `root_runs` all of them;
    * `idle`: the main thread's latest run terminal, nothing pending, live,
      queued, reserved or in the background, every thread read;
    * `signature`: every thread's runs with their statuses and its item
      count, for telling whether anything changed between two reads."""
    rp = tree.get(root)
    unreadable = [(t, f"{type(p).__name__}: {p}"[:200]) for t, p in tree.items() if not isinstance(p, dict)]
    live, queued, conts, background, requests = [], [], [], [], []
    sig = []
    for t in [root] + sorted(t for t in tree if t != root):
        p = tree.get(t)
        if not isinstance(p, dict):
            continue
        for r in p.get("runs", []):
            if r.get("status") == "queued":
                queued.append((t, r["id"]))
            elif r.get("status") not in TERMINAL:
                live.append((t, r["id"], r.get("status")))
        conts += [(t, rid) for rid in pending_continuations(p)]
        n_bg = sum(len(pt.get("pendingBackgroundTasks") or []) for pt in p.get("providerThreads", []))
        if n_bg:
            background.append((t, n_bg))
        qs, ss = pending_requests(p)
        requests += [("question", t, question_of(i, t), i) for i in qs] + [("secret", t, secret_of(i, t), i) for i in ss]
        sig.append((t, tuple((r["id"], r.get("status")) for r in p.get("runs", [])), len(p.get("turnItems", [])), len(p.get("messages", []))))
    requests.sort(key=lambda x: (str(x[3].get("startedAt") or ""), x[3].get("ordinal") or 0))
    runs = sorted(rp.get("runs", []), key=_ordinal) if isinstance(rp, dict) else []
    latest = runs[-1] if runs else None
    idle = (isinstance(rp, dict) and latest is not None and latest.get("status") in TERMINAL and not requests and not live and not queued
            and not conts and not background and not unreadable)
    return {"requests": [(k, t, obj) for k, t, obj, _ in requests], "live": live, "queued": queued, "continuations": conts, "background": background,
            "unreadable": unreadable, "latest": latest, "root_runs": runs, "idle": idle, "signature": tuple(sig)}


def busy_summary(st: dict[str, Any]) -> str:
    return (f"live {[r[-12:] for _, r, _ in st['live']]}, queued {len(st['queued'])}, continuations {len(st['continuations'])}, "
            f"background {sum(n for _, n in st['background'])}, requests {len(st['requests'])}, unreadable {[t[-16:] for t, _ in st['unreadable']]}")


# ---------------------------------------------------------------------------
# The idle gate (D-009): a turn boundary, failing closed
# ---------------------------------------------------------------------------

# A shell row's status is its latest run's status, or idle. Any terminal one
# is a boundary (an interrupted run is a stopped run); activity and pending
# work are judged from the row's other fields and from the projections.
IDLE_SHELL_STATUSES = ("idle", *sorted(TERMINAL))


def shell_rows(shell: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every row of T3's shell snapshot by thread id: `threads` and
    `archivedThreads` (a finished subagent's thread can be archived)."""
    rows: dict[str, dict[str, Any]] = {}
    for key in ("archivedThreads", "threads"):
        for r in shell.get(key) or []:
            rows[r["id"]] = {**r, "_archived": key == "archivedThreads"}
    return rows


def idle_problems(threads: dict[str, dict[str, Any] | Exception], shell_rows: dict[str, dict[str, Any]] | Exception, root: str, run_id: str | None) -> list[str]:
    """Problems that make a thread tree not idle; empty means a turn boundary.
    The projection part is `busy_state`, the definition every caller uses;
    on top of it, the gate fails closed on T3's shell: a row for every thread
    (archived rows count) with an idle or terminal status and no activity or
    pending work, and the named run present in the main thread and terminal."""
    problems: list[str] = []
    st = busy_state(threads, root)
    root_proj = threads.get(root)
    if isinstance(root_proj, dict):
        run = run_by_id(root_proj, run_id) if run_id else None
        if run is None:
            problems.append(f"run {str(run_id)[-24:]} is not in the thread")
        elif run.get("status") not in TERMINAL:
            problems.append(f"run {run_id[-24:]} is {run.get('status')}")
    for t, reason in st["unreadable"]:
        problems.append(f"thread {t[-24:]} unreadable: {reason[:80]}")
    if st["requests"] or st["live"] or st["queued"] or st["continuations"] or st["background"]:
        problems.append(f"the tree is busy: {busy_summary(st)}")
    if isinstance(shell_rows, Exception):
        problems.append(f"shell unreadable: {type(shell_rows).__name__}")
        return problems
    for tid in threads:
        row = shell_rows.get(tid)
        if row is None:
            problems.append(f"no shell row for thread {tid[-24:]}")
            continue
        busy = {k: row.get(k) for k in ("activityRunStatus", "pendingRuntimeRequest", "pendingBackgroundTasks", "pendingGuestMessageCount", "blockedOnThreadWait") if row.get(k)}
        if row.get("status") not in IDLE_SHELL_STATUSES or busy:
            problems.append(f"shell row of {tid[-24:]} not idle: status {row.get('status')}, {sorted(busy)}")
    return problems


# ---------------------------------------------------------------------------
# Refusals in the gateway log
# ---------------------------------------------------------------------------

REFUSAL_TYPES = {"billing_error", "authentication_error", "permission_error"}
CREDIT_MESSAGE = "credit balance is too low"


def is_refusal(status: Any, error_type: str | None, message: str | None) -> bool:
    if not isinstance(status, int) or status not in (400, 401, 402, 403):
        return False
    if error_type in REFUSAL_TYPES:
        return True
    return error_type == "invalid_request_error" and CREDIT_MESSAGE in (message or "").lower()


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class ScanDeadline(RuntimeError):
    """The refusal scan ran out of time."""


def _check(deadline: float | None) -> None:
    import time as _time

    if deadline is not None and _time.monotonic() >= deadline:
        raise ScanDeadline("the gateway log scan ran out of time")


def load_gateway_log(log_dir: Path, *, deadline: float | None = None) -> list[dict[str, Any]]:
    path = Path(log_dir) / "requests.jsonl"
    if not path.exists():
        return []
    out = []
    # Streamed, line by line, the deadline checked at each.
    with open(path, encoding="utf-8") as fh:
        lines = (line.rstrip("\n") for line in fh)
        out = _gateway_records(lines, log_dir, deadline)
    return out


def _gateway_records(lines: Any, log_dir: Path, deadline: float | None) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        _check(deadline)
        if not line.strip():
            continue
        rec = json.loads(line)
        rec["error_type"], rec["error_message"] = None, None
        if isinstance(rec.get("status"), int) and rec["status"] >= 400 and rec.get("id"):
            try:
                err = json.loads((Path(log_dir) / "bodies" / f"{rec['id']}.response").read_text()).get("error")
                if isinstance(err, dict):
                    rec["error_type"], rec["error_message"] = err.get("type"), err.get("message")
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        out.append(rec)
    return out


def is_inference(rec: dict[str, Any]) -> bool:
    return rec.get("method") == "POST" and (rec.get("upstream_path") or rec.get("path") or "").split("?", 1)[0] == "/v1/messages"


def _last_user_blocks(body: str | None) -> list[str] | None:
    try:
        data = json.loads(body or "")
    except json.JSONDecodeError:
        return None
    msgs = [m for m in data.get("messages", []) if isinstance(m, dict) and m.get("role") == "user"] if isinstance(data, dict) else []
    if not msgs:
        return None
    content = msgs[-1].get("content")
    if isinstance(content, str):
        return [content]
    return [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"] if isinstance(content, list) else None


def refusal(log_dir: Path, run: dict[str, Any], other_runs: list[dict[str, Any]], message_texts: list[str], *, unreadable_threads: list[str] | None = None, slack_s: float = 0.2,
            deadline: float | None = None) -> dict[str, Any]:
    """Whether the model API refused this run, from a copy of the gateway
    log. `deadline` (monotonic) is checked at every record; ScanDeadline when
    it passes."""
    records = load_gateway_log(log_dir, deadline=deadline)
    start, end = _ts(run.get("startedAt")), _ts(run.get("completedAt"))
    if start is None or end is None or run.get("status") not in TERMINAL:
        return {"refused": False, "reason": "run not terminal with both times"}
    own = []
    for r in records:
        _check(deadline)
        t = _ts(r.get("time"))
        if is_inference(r) and t is not None and start - dt.timedelta(seconds=slack_s) <= t <= end + dt.timedelta(seconds=slack_s):
            own.append(r)
    bound, ambiguous = [], []
    for r in own:
        t = _ts(r.get("time"))
        overlap = False
        for o in other_runs:
            _check(deadline)
            os_, oe = _ts(o.get("startedAt")), _ts(o.get("completedAt"))
            if os_ is not None and t is not None and t >= os_ and (oe is None or t <= oe):
                overlap = True
        (ambiguous if overlap else bound).append(r)
    confirmed = []
    for r in bound:
        _check(deadline)
        try:
            body = (Path(log_dir) / "bodies" / f"{r['id']}.request").read_text()
        except (OSError, KeyError):
            body = None
        blocks = _last_user_blocks(body)
        if blocks and any(t in blocks for t in message_texts if t):
            confirmed.append(r)
    # Refused: the last bound inference request is a refusal and no request
    # after it succeeded (a turn whose first call worked and whose second was
    # refused for credit balance is refused), and a bound request carries the
    # run's message.
    bound.sort(key=lambda r: _ts(r.get("time")) or start)
    refused = [r for r in bound if is_refusal(r.get("status"), r.get("error_type"), r.get("error_message"))]
    last = bound[-1] if bound else None
    last_refused = last is not None and is_refusal(last.get("status"), last.get("error_type"), last.get("error_message"))
    ok = last_refused and not ambiguous and bool(confirmed) and not unreadable_threads
    return {
        "refused": ok,
        "bound": len(bound),
        "ambiguous": len(ambiguous),
        "confirmed": len(confirmed),
        "unreadable_threads": unreadable_threads or [],
        "refusals": [{k: r.get(k) for k in ("time", "status", "error_type", "error_message")} for r in refused[:5]],
    }
