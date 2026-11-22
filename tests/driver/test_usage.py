"""Accounting for calls canceled by the runner's confirmed interrupts."""

from copy import deepcopy

import pytest

from cohbench.driver.usage import TOKEN_FIELDS, settled_usage


WINDOW = {"start": "2026-11-22T12:00:35Z", "end": "2026-11-22T12:00:37Z"}


def call(name="cancelled", *, start="2026-11-22T12:00:30Z", duration_ms=6000, status="unavailable", outcome="unknown", tokens=None):
    return {"id": name, "start": start, "duration_ms": duration_ms, "usage_status": status, "outcome": outcome,
            **{k: tokens for k in TOKEN_FIELDS}}


def snapshot(*calls):
    bad = sum(c["usage_status"] != "complete" for c in calls)
    return {"calls": list(calls), "summary": {"calls": len(calls), "calls_not_complete": bad,
            "usage_status": "unavailable" if bad else "complete",
            **{k: sum(c[k] or 0 for c in calls) if any(c[k] is not None for c in calls) else None for k in TOKEN_FIELDS}}}


@pytest.mark.parametrize("tokens", [None, 23])
def test_cancelled_call_is_settled_with_its_recorded_tokens(tokens):
    raw = snapshot(call(tokens=tokens))
    before = deepcopy(raw)
    effective = settled_usage(raw, [WINDOW])
    assert effective["calls_not_complete"] == 0 and effective["usage_status"] == "complete"
    assert all(effective[k] == (tokens or 0) for k in TOKEN_FIELDS)
    assert effective["recorded"] == before["summary"] and raw == before
    assert effective["runner_cancelled_calls"][0]["id"] == "cancelled"
    assert effective["interrupts"] == [WINDOW]


@pytest.mark.parametrize("other", [
    call("earlier", start="2026-11-22T12:00:20Z", duration_ms=5000),
    call("later", start="2026-11-22T12:00:38Z"),
    call("completed", outcome="ok", status="partial"),
    call("unknown-time", duration_ms=None),
])
def test_unrelated_incomplete_calls_still_need_usage(other):
    raw = snapshot(call(tokens=7), other)
    effective = settled_usage(raw, [WINDOW])
    assert effective["calls_not_complete"] == 1 and effective["usage_status"] == "unavailable"
    assert [r["id"] for r in effective["runner_cancelled_calls"]] == ["cancelled"]
    assert all(effective[k] == 7 for k in TOKEN_FIELDS)


def test_interrupt_does_not_change_complete_usage_totals():
    raw = snapshot(call(tokens=7), call("complete", status="complete", outcome="ok", tokens=23))
    effective = settled_usage(raw, [WINDOW])
    assert effective["usage_status"] == "complete" and effective["calls_not_complete"] == 0
    assert all(effective[k] == raw["summary"][k] == 30 for k in TOKEN_FIELDS)


def test_unknown_outcome_without_an_interrupt_is_not_settled():
    effective = settled_usage(snapshot(call()), [])
    assert effective["calls_not_complete"] == 1 and effective["usage_status"] == "unavailable"
    assert effective["runner_cancelled_calls"] == []
