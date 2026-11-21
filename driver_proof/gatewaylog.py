"""Read a gateway's request log (gateway/README.md) for one harness container.

Each harness container has its own gateway, so every record in a log belongs
to that container. Records are tied to a T3 run by time: a request belongs to
a run if it started inside the run's [startedAt, completedAt] window.

The log is operator-only (D-008). The driver proof reads it for three things:
whether the model API refused a run's requests, which models were called, and
token counts for spend.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from .container import REPO_ROOT

# The model API refused the credential or the account: a precondition outside
# the driver. Decided from the upstream's structured error body as the gateway
# stored it: the error type, and for invalid_request_error the provider's
# credit-balance message (Anthropic reports an empty balance with that type).
REFUSAL_TYPES = {"billing_error", "authentication_error", "permission_error"}
CREDIT_MESSAGE = "credit balance is too low"


def is_refusal(status: int | None, error_type: str | None, message: str | None) -> bool:
    if not isinstance(status, int) or status not in (400, 401, 402, 403):
        return False
    if error_type in REFUSAL_TYPES:
        return True
    return error_type == "invalid_request_error" and CREDIT_MESSAGE in (message or "").lower()


# US dollars per million tokens for claude-haiku-5-5 (Anthropic first-party
# list price, prompts up to 100K tokens). Cache reads at 0.1x and cache
# writes at 1.25x of input, the usual Anthropic multipliers.
HAIKU_PRICE = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load(log_dir: Path) -> list[dict[str, Any]]:
    path = log_dir / "requests.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            rec["error_type"], rec["error_message"] = _error(log_dir, rec)
            records.append(rec)
    return records


def _error(log_dir: Path, rec: dict[str, Any]) -> tuple[str | None, str | None]:
    if not isinstance(rec.get("status"), int) or rec["status"] < 400 or not rec.get("id"):
        return None, None
    body = log_dir / "bodies" / f"{rec['id']}.response"
    try:
        data = json.loads(body.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, None
    return err.get("type"), err.get("message")


def is_inference(rec: dict[str, Any]) -> bool:
    return rec.get("method") == "POST" and (rec.get("upstream_path") or rec.get("path") or "").split("?", 1)[0] == "/v1/messages"


def in_window(records: list[dict[str, Any]], start: str | None, end: str | None, slack_s: float = 0.2) -> list[dict[str, Any]]:
    s, e = _ts(start), _ts(end)
    if s is None or e is None:
        return []
    out = []
    for rec in records:
        t = _ts(rec.get("time"))
        if t is not None and s - dt.timedelta(seconds=slack_s) <= t <= e + dt.timedelta(seconds=slack_s):
            out.append(rec)
    return out


def request_body(log_dir: Path, rec: dict[str, Any]) -> str | None:
    try:
        return (log_dir / "bodies" / f"{rec['id']}.request").read_text()
    except (OSError, KeyError):
        return None


def last_user_blocks(body: str | None) -> list[str] | None:
    """The text blocks of the last user message in a decoded Anthropic request body."""
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


def bind_to_run(log_dir: Path, records: list[dict[str, Any]], run: dict[str, Any], other_runs: list[dict[str, Any]], message_texts: list[str], slack_s: float = 0.2) -> dict[str, Any]:
    """Inference requests that belong to one T3 run.

    The run needs startedAt and completedAt. A request belongs to it if it
    started inside that window (small slack) and inside no other run's window
    in the same container; another run with startedAt but no completedAt has an
    open window that overlaps everything after its start. A request inside two
    windows is ambiguous. A bound request is confirmed when the last user
    message in its decoded body is one of the run's user messages (a text
    block equal to it)."""
    start, end = run.get("startedAt"), run.get("completedAt")
    if not start or not end:
        return {"bound": [], "ambiguous": [], "confirmed": [], "reason": "run has no startedAt and completedAt"}
    own = [r for r in in_window(records, start, end, slack_s) if is_inference(r)]
    ambiguous, bound = [], []
    for r in own:
        t = _ts(r.get("time"))
        overlaps = []
        for o in other_runs:
            os_, oe = _ts(o.get("startedAt")), _ts(o.get("completedAt"))
            if os_ is None:
                continue
            if t is not None and t >= os_ and (oe is None or t <= oe):
                overlaps.append(o.get("id"))
        (ambiguous if overlaps else bound).append(r)
    confirmed = []
    for r in bound:
        blocks = last_user_blocks(request_body(log_dir, r))
        if blocks and any(t in blocks for t in message_texts if t):
            confirmed.append(r)
    return {"bound": bound, "ambiguous": ambiguous, "confirmed": confirmed, "reason": None}


def refusals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: r.get(k) for k in ("time", "id", "path", "status", "error_type", "error_message", "model")}
        for r in records
        if is_inference(r) and is_refusal(r.get("status"), r.get("error_type"), r.get("error_message"))
    ]


def model_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per inference call: the request's model and the response's model."""
    return [{"id": r.get("id"), "model": r.get("model"), "response_model": r.get("response_model"), "status": r.get("status")} for r in records if is_inference(r)]


def models(records: list[dict[str, Any]]) -> dict[str, int]:
    seen: dict[str, int] = {}
    for row in model_rows(records):
        key = str(row["model"])
        seen[key] = seen.get(key, 0) + 1
    return seen


def destinations(records: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        key = f"{r.get('host')} {r.get('method')} {(r.get('upstream_path') or r.get('path') or '').split('?', 1)[0]} -> {r.get('status')}"
        out[key] = out.get(key, 0) + 1
    return out


def usage_summary(log_dir: Path) -> dict[str, Any]:
    """Token totals from the gateway's summary, plus a cost at haiku list prices.
    For budgets and the cost report only; never a scorer input (D-008)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from gateway.summary import read_log, summarize  # noqa: PLC0415

    path = log_dir / "requests.jsonl"
    rows = summarize(read_log(str(path))) if path.exists() else []
    tot = {"calls": len(rows), "input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "usage_not_complete": 0}
    for row in rows:
        for k in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"):
            tot[k] += row.get(k) or 0
        if row.get("usage_status") != "complete":
            tot["usage_not_complete"] += 1
    tot["cost_usd_at_haiku_list_price"] = round(
        (tot["input_tokens"] * HAIKU_PRICE["input"] + tot["output_tokens"] * HAIKU_PRICE["output"] + tot["cached_input_tokens"] * HAIKU_PRICE["cache_read"] + tot["cache_write_tokens"] * HAIKU_PRICE["cache_write"]) / 1e6,
        6,
    )
    return tot
