"""Hosted reader regressions. All transports are injected or local."""

import copy
import json
from pathlib import Path
import threading
import time

import pytest

from cohbench.scoring import reader as R
from .build import OPTIONS
from .test_explanation import completion, hosted, fake


def direct(**changes):
    raw = json.loads(
        Path("cohbench/scoring/configs/explabs-kimi-k3.1-pro.json").read_text()
    )
    raw.update(changes)
    return R.parse_config(raw)


def test_direct_protocol_is_blind_and_bounds_the_window():
    rd = R.make(direct(), key="secret", transport=fake({})[0])
    body = rd.body("Why?", "word " * 80 + "hidden cause", OPTIONS)
    assert "provider" not in body
    assert body["reasoning_effort"] == "none"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert len(body["messages"]) == 2
    assert "hidden cause" not in json.dumps(body["messages"])
    assert "kimi" not in json.dumps(body["messages"])
    assert "harness" not in json.dumps(body["messages"])
    assert "secret" not in json.dumps(body)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "25"])
def test_deadline_config_is_finite_positive(timeout):
    with pytest.raises(R.ReaderError):
        direct(timeout_s=timeout)


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "other"),
        ("extra_body", {"messages": []}),
        ("provider", {"order": ["x"]}),
    ],
)
def test_direct_config_refuses_ignored_or_overridden_fields(field, value):
    raw = copy.deepcopy(direct().raw)
    raw["endpoint"][field] = value
    with pytest.raises(R.ReaderError):
        R.parse_config(raw)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_uses_identical_bytes(status):
    calls = []

    def transport(*args):
        calls.append(args[4])
        return (
            (status, b"")
            if len(calls) == 1
            else (200, json.dumps(completion("B")).encode())
        )

    rd = R.make(hosted(), key="secret", transport=transport)
    rd.client.sleep = lambda _: None
    assert rd.choose("Why?", "text", OPTIONS) == 1
    assert len(calls) == 2 and calls[0] == calls[1]
    assert rd.last["attempts"] == 2


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404])
def test_final_http_failure_is_not_retried(status):
    transport, calls = fake({}, status)
    with pytest.raises(R.ReaderError):
        R.make(hosted(), key="secret", transport=transport).choose("q", "a", OPTIONS)
    assert len(calls) == 1


def test_stalled_transport_has_a_total_deadline():
    release = threading.Event()

    def transport(*args):
        release.wait(2)
        return 200, json.dumps(completion("A")).encode()

    started = time.monotonic()
    try:
        with pytest.raises(R.ReaderError):
            R.make(hosted(timeout_s=0.03), key="secret", transport=transport).choose(
                "q", "a", OPTIONS
            )
        assert time.monotonic() - started < 0.8
    finally:
        release.set()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"org/reader-model","model":"org/reader-model"}',
        b'{"usage":NaN}',
        json.dumps(completion("A"))
        .replace(
            '{\\"choice\\": \\"A\\"}', '{\\"choice\\": \\"A\\", \\"choice\\": \\"B\\"}'
        )
        .encode(),
    ],
)
def test_duplicate_and_nonstandard_json_rejected(raw):
    rd = R.make(hosted(), key="secret")
    with pytest.raises(R.ReaderError):
        rd.parse(raw, 6)


def test_cache_records_usage_without_repeating_calls_or_changing_score_records(
    tmp_path,
):
    reply = completion("C")
    reply["usage"] = {"prompt_tokens": 100, "completion_tokens": 6}
    transport, calls = fake(reply)
    hosted_reader = R.make(hosted(), key="secret", transport=transport)
    path = tmp_path / "cache.jsonl"
    rd = R.RecordedReader(hosted_reader, path)
    prefix = "word " * 80
    assert rd.choose("q", prefix + "tail one", OPTIONS) == 2
    assert rd.choose("q", prefix + "tail two", OPTIONS) == 2
    replay = R.RecordedReader(hosted_reader, path)
    assert replay.choose("q", prefix, OPTIONS) == 2
    assert len(calls) == 1
    row = json.loads(path.read_text())
    assert row["response"]["usage"] == reply["usage"]
    assert row["response"]["served"]["provider"] == "one-provider"
    assert "response" not in rd.used()[0]
    assert rd.used() == replay.used()
    assert "secret" not in path.read_text()
    assert rd.choose("q", prefix, list(reversed(OPTIONS))) == 2
    assert rd.choose("different question", prefix, OPTIONS) == 2
    other = R.RecordedReader(
        R.make(hosted(timeout_s=30), key="secret", transport=transport), path
    )
    other.choose("q", prefix, OPTIONS)
    assert len(calls) == 4


def test_classifier_revision_includes_shared_client(tmp_path, monkeypatch):
    from cohbench.user import revision

    unit = tmp_path / "user"
    unit.mkdir()
    (unit / "revision.py").write_text("classifier")
    for name in ["endpoint.py", "errors.py", "ledger.py"]:
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(revision, "__file__", str(unit / "revision.py"))
    revision.code_revision.cache_clear()
    try:
        first = revision.code_revision()
        (tmp_path / "endpoint.py").write_text("changed client")
        revision.code_revision.cache_clear()
        assert revision.code_revision() != first
    finally:
        revision.code_revision.cache_clear()


def test_shared_schema_preserves_classifier_key_validation():
    from cohbench import endpoint
    from cohbench.user import prompts

    assert endpoint.schema(["A", "B2"]) == prompts.schema(["A", "B2"])
    for schema in [endpoint.schema, prompts.schema]:
        with pytest.raises(ValueError):
            schema(["not an option key"])
