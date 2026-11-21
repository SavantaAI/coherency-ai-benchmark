"""Usage summary of a requests.jsonl, for the runner (budgets) and for cost reports.

Gateway usage is never a scorer input (decision D-008). The per-call rows
carry no path, route, host, size, header or file name, but they still differ
between harnesses: token counts alone tell Claude Code from Pi on the same
model.

An inference call is a request to a model inference endpoint whose outcome
is a 2xx response ("ok") or unknown ("unknown": the request reached the
upstream but no response was read in full, for example after a client
disconnect or a timeout, so the upstream may have generated output). Error
responses on inference endpoints are not inference calls; the episode
summary counts them by status class.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .usage import inference_kind, normalise

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
STATUS_RANK = {"complete": 0, "partial": 1, "unavailable": 2}


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _route_path(record: dict) -> str | None:
    path = record.get("route_path")
    if isinstance(path, str):
        return path
    # Records written before route_path existed: the upstream path without
    # its query (equal to the route path when the upstream has no base path).
    upstream = record.get("upstream_path")
    return upstream.split("?", 1)[0] if isinstance(upstream, str) else None


def _kind(record: dict) -> str | None:
    kind = record.get("inference")
    if isinstance(kind, str):
        return kind
    return inference_kind(record.get("usage_format"), record.get("method"), _route_path(record))


def _class(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def outcome(record: dict) -> str:
    """ok, 4xx, 5xx, other, unknown, or not_sent (never reached the upstream)."""
    upstream_status = _int(record.get("upstream_status"))
    if upstream_status is not None:
        # The gateway replaced the upstream's response. A 2xx it did not relay
        # may still have generated (billed) output that was never read.
        return "unknown" if 200 <= upstream_status < 300 else _class(upstream_status)
    error = record.get("gateway_error")
    status = _int(record.get("status"))
    if status is not None and error not in ("upstream_timeout", "upstream_unreachable"):
        if 200 <= status < 300 and record.get("relay_complete") is False:
            # A 2xx head arrived but the relay did not finish (client cancel,
            # upstream stall, cut): output may exist that was not all read.
            return "unknown"
        return _class(status)
    sent = record.get("upstream_sent")
    if sent is None:  # older records
        sent = error != "upstream_unreachable"
    return "unknown" if sent else "not_sent"


def _tokens(record: dict, kind: str | None) -> dict:
    tokens = record.get("tokens")
    if isinstance(tokens, dict) and set(tokens) == set(TOKEN_FIELDS) and all(
        v is None or _int(v) is not None for v in tokens.values()
    ):
        return tokens
    return normalise(kind, record.get("usage"))[0]


def _forwarded(records: Iterable[dict]) -> list[dict]:
    out = [r for r in records if isinstance(r, dict) and r.get("upstream_path") and r.get("method")]
    out.sort(key=lambda r: (str(r.get("time") or ""), str(r.get("id") or "")))
    return out


def summarize(records: Iterable[dict], include_all: bool = False) -> list[dict]:
    """One row per inference call; with include_all, one per forwarded request."""
    rows = []
    for record in _forwarded(records):
        kind = _kind(record)
        result = outcome(record)
        is_call = kind is not None and result in ("ok", "unknown")
        if not is_call and not include_all:
            continue
        tokens = _tokens(record, kind)
        status = record.get("usage_status")
        if status not in STATUS_RANK:
            status = "unavailable"
        duration = record.get("duration_ms")
        rows.append(
            {
                "index": len(rows),
                "start": record.get("time"),
                "duration_ms": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
                **{f: tokens.get(f) for f in TOKEN_FIELDS},
                "usage_status": status,
                "outcome": result,
                "inference": is_call,
                "server_tool_use": record.get("server_tool_use") is True,
            }
        )
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Totals over inference-call rows. A token total is null when no call reported that field."""
    out: dict = {"calls": len(rows)}
    for field in TOKEN_FIELDS:
        values = [v for v in (_int(r.get(field)) for r in rows) if v is not None]
        out[field] = sum(values) if values else None
    # Totals undercount by whatever these calls did not report.
    out["calls_not_complete"] = sum(1 for r in rows if r.get("usage_status") != "complete")
    out["calls_outcome_unknown"] = sum(1 for r in rows if r.get("outcome") == "unknown")
    durations = [r["duration_ms"] for r in rows if isinstance(r.get("duration_ms"), (int, float)) and not isinstance(r.get("duration_ms"), bool)]
    out["duration_ms"] = round(sum(durations), 1)
    out["server_tool_use"] = any(r.get("server_tool_use") is True for r in rows)
    statuses = [r.get("usage_status", "unavailable") for r in rows]
    out["usage_status"] = max(statuses, key=lambda s: STATUS_RANK.get(s, 2)) if statuses else "unavailable"
    return out


def episode(records: Iterable[dict]) -> dict:
    """Episode totals, plus error responses on inference endpoints by status class."""
    records = list(records)
    out = aggregate(summarize(records))
    errors = {"4xx": 0, "5xx": 0, "other": 0}
    for record in _forwarded(records):
        result = outcome(record)
        if _kind(record) is not None and result in errors:
            errors[result] += 1
    out["errors"] = errors
    return out


def read_log(path: str | Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out
