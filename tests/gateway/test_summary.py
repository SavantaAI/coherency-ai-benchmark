"""The usage summary for budgets and cost reports."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gateway.summary import aggregate, episode, outcome, summarize

USAGE_A = {"input_tokens": 2, "cache_creation_input_tokens": 6093, "cache_read_input_tokens": 11967, "output_tokens": 4}
USAGE_B = {"input_tokens": 5, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 18000, "output_tokens": 40}


def record(i, method, path, ua, size, usage=None, status="complete", fmt="anthropic", upstream_base="", **extra):
    route_path = path.split("?", 1)[0]
    return {
        "time": f"2026-11-21T15:00:0{i}.000Z",
        "id": f"id-{ua}-{i}",
        "mode": "transparent",
        "route": fmt,
        "usage_format": fmt,
        "host": "api.example.com",
        "method": method,
        "path": path,
        "route_path": route_path,
        "upstream_path": upstream_base + path,
        "status": 200,
        "request_bytes": size,
        "response_bytes": size // 10,
        "request_body_file": f"id-{ua}-{i}.request",
        "usage": usage,
        "usage_status": status if usage else "unavailable",
        "duration_ms": 100.0 * (i + 1),
        **extra,
    }


def claude_log():
    return [
        record(0, "GET", "/api/claude_code/settings", "claude-code", 0),
        record(1, "GET", "/api/claude_code/policy_limits", "claude-code", 0),
        record(2, "POST", "/v1/messages?beta=true", "claude-cli", 52704, {**USAGE_A, "service_tier": "standard"}),
        record(3, "POST", "/v1/messages?beta=true", "claude-cli", 53000, {**USAGE_B, "speed": "standard"}),
    ]


def pi_log():
    return [
        record(2, "POST", "/v1/messages?beta=true", "pi", 6185, dict(USAGE_A)),
        record(3, "POST", "/v1/messages", "pi", 6400, {**USAGE_B, "server_tool_use": None}),
        {"time": "2026-11-21T15:00:09.000Z", "id": "x", "mode": "transparent", "gateway_error": "tls_unknown_host"},
    ]


def test_explicit_identifiers_are_absent():
    # The rows hold no path, route, host, size or file name, so two logs with
    # the same timing and counts give the same rows. Real harnesses differ in
    # their counts, so the rows still tell them apart: this is for budgets
    # and cost, never for scoring.
    a = summarize(claude_log())
    assert a == summarize(pi_log())
    assert a[0] == {
        "index": 0,
        "start": "2026-11-21T15:00:02.000Z",
        "duration_ms": 300.0,
        "input_tokens": 2,
        "cached_input_tokens": 11967,
        "cache_write_tokens": 6093,
        "output_tokens": 4,
        "usage_status": "complete",
        "outcome": "ok",
        "inference": True,
        "server_tool_use": False,
    }
    text = json.dumps(a)
    for leak in ("claude", "pi", "/v1", "api.example.com", "52704", "6185", "request_body_file", "route"):
        assert leak not in text


def test_episode_aggregate():
    rows = summarize(claude_log())
    rows[1]["usage_status"] = "partial"
    rows[1]["server_tool_use"] = True
    assert aggregate(rows) == {
        "calls": 2,
        "input_tokens": 7,
        "cached_input_tokens": 29967,
        "cache_write_tokens": 6103,
        "output_tokens": 44,
        "calls_not_complete": 1,
        "calls_outcome_unknown": 0,
        "duration_ms": 700.0,
        "server_tool_use": True,
        "usage_status": "partial",
    }
    assert aggregate([])["usage_status"] == "unavailable"


def test_missing_counters_stay_null():
    log = [record(0, "POST", "/v1/messages", "x", 1, {"input_tokens": 3, "output_tokens": 1}, status="partial")]
    row = summarize(log)[0]
    assert (row["cached_input_tokens"], row["cache_write_tokens"], row["usage_status"]) == (None, None, "partial")
    assert aggregate([row])["cache_write_tokens"] is None


def test_bad_nested_details_do_not_raise():
    log = [record(0, "POST", "/v1/chat/completions", "x", 1, {"prompt_tokens": 9, "completion_tokens": 1, "prompt_tokens_details": 7}, fmt="openai")]
    row = summarize(log)[0]
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (None, None, 1)


def test_route_with_upstream_base_path_is_counted():
    log = [record(0, "POST", "/v1/chat/completions", "x", 1, {"prompt_tokens": 9, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": 4}}, fmt="openai", upstream_base="/base")]
    rows = summarize(log)
    assert len(rows) == 1 and rows[0]["input_tokens"] == 5


def test_include_all_marks_non_inference_calls():
    rows = summarize(claude_log(), include_all=True)
    assert [r["inference"] for r in rows] == [False, False, True, True]


def test_cli(tmp_path):
    log = tmp_path / "requests.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in claude_log()) + "\n")
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-m", "gateway", "summary", str(log)], capture_output=True, text=True, cwd=root, check=True)
    assert [json.loads(x) for x in out.stdout.splitlines()] == summarize(claude_log())
    out = subprocess.run([sys.executable, "-m", "gateway", "summary", "--episode", str(log)], capture_output=True, text=True, cwd=root, check=True)
    assert json.loads(out.stdout) == episode(claude_log())


def test_records_without_route_path_or_inference_fields():
    # Records written before route_path and inference existed.
    old = record(0, "POST", "/v1/messages?beta=true", "x", 1, dict(USAGE_A))
    del old["route_path"]
    assert len(summarize([old])) == 1


def test_error_responses_are_not_inference_calls():
    log = [
        record(0, "POST", "/v1/messages", "x", 1, None, status_code=429),
        record(1, "POST", "/v1/messages", "x", 1, dict(USAGE_A)),
        record(2, "POST", "/v1/messages", "x", 1, None, status_code=500),
        record(3, "POST", "/v1/messages", "x", 1, None, status_code=400),
    ]
    for r in log:
        if "status_code" in r:
            r["status"] = r.pop("status_code")
    rows = summarize(log)
    assert len(rows) == 1 and rows[0]["usage_status"] == "complete"
    totals = episode(log)
    assert (totals["calls"], totals["calls_not_complete"], totals["usage_status"]) == (1, 0, "complete")
    assert totals["errors"] == {"4xx": 2, "5xx": 1, "other": 0}


def test_outcome_classes():
    base = {"upstream_path": "/v1/messages", "method": "POST"}
    assert outcome({**base, "status": 200}) == "ok"
    assert outcome({**base, "status": 200, "gateway_error": "credential_in_response"}) == "ok"
    # Disconnect or timeout after the request went upstream: unknown.
    assert outcome({**base, "status": None, "client_disconnected": True, "upstream_sent": True}) == "unknown"
    assert outcome({**base, "status": 504, "gateway_error": "upstream_timeout", "upstream_sent": True}) == "unknown"
    # The gateway replaced a 2xx it did not relay: unknown; a replaced 429 stays an error.
    assert outcome({**base, "status": 502, "upstream_status": 200, "gateway_error": "credential_in_response"}) == "unknown"
    assert outcome({**base, "status": 502, "upstream_status": 429}) == "4xx"
    assert outcome({**base, "status": 502, "gateway_error": "upstream_unreachable", "upstream_sent": None}) == "not_sent"


def test_bool_is_not_an_int():
    usage = {"input_tokens": True, "cache_read_input_tokens": 1, "cache_creation_input_tokens": 1, "output_tokens": False}
    row = summarize([record(0, "POST", "/v1/messages", "x", 1, usage)])[0]
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)
    bad = summarize([record(0, "POST", "/v1/messages", "x", 1, dict(USAGE_A), tokens={"input_tokens": True, "cached_input_tokens": 1, "cache_write_tokens": 1, "output_tokens": 1})])[0]
    assert bad["input_tokens"] == 2  # invalid stored tokens are recomputed from usage
    assert aggregate([{"input_tokens": True, "duration_ms": True}])["input_tokens"] is None
