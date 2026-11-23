"""Hosted compatibility and accounting, using only the local HTTP stand-in."""

import json

import pytest

from cohbench.user.__main__ import main
from cohbench.user.api import Client, EndpointError, ServedMismatch
from cohbench.user.config import ConfigError, parse
from cohbench.user.ledger import Ledger

from .conftest import DEV, KEY, config_doc


def direct(base_url):
    doc = config_doc(base_url)
    doc["endpoint"].update(protocol="openai", provider=None)
    return doc


def test_direct_keeps_identity_check_without_sending_routing(fake):
    cfg = parse(direct(fake.base_url))
    client = Client(cfg, KEY)
    result = client.choose([], ["A"], client.clock() + 10)
    assert result["served"]["provider"] == "TestProvider"
    assert "provider" not in fake.requests[0]
    fake.mode = "wrong_model"
    with pytest.raises(ServedMismatch):
        client.choose([], ["A"], client.clock() + 10)
    fake.mode = "no_provider"
    with pytest.raises(ServedMismatch):
        client.choose([], ["A"], client.clock() + 10)


def test_direct_still_requires_pinned_provider(fake):
    doc = direct(fake.base_url)
    doc["endpoint"]["expect"]["provider"] = None
    with pytest.raises(ConfigError):
        parse(doc)


def test_failed_answers_count_tokens_and_cap_survives_new_client(fake, tmp_path):
    path = tmp_path / "calls.jsonl"
    cfg = parse(direct(fake.base_url))
    fake.mode = "echo_answer"
    client = Client(cfg, KEY, ledger=Ledger(path, 1))
    with pytest.raises(EndpointError):
        client.choose([], ["A"], client.clock() + 10)
    client.flush()
    entries = [
        json.loads(line)
        for line in path.read_text().splitlines()
        if json.loads(line)["event"] != "cap"
    ]
    assert entries[0]["event"] == "start"
    assert entries[1]["outcome"] == "invalid_response"
    assert entries[1]["usage"]["prompt_tokens"] == 0
    assert entries[1]["usage"]["completion_tokens"] > 0
    assert KEY not in path.read_text()
    client = Client(cfg, KEY, ledger=Ledger(path, 1))
    with pytest.raises(EndpointError, match="budget"):
        client.choose([], ["A"], client.clock() + 10)
    assert len(fake.requests) == 1


def test_retries_share_cap(fake, tmp_path):
    fake.mode = "http429"
    client = Client(
        parse(direct(fake.base_url)), KEY, ledger=Ledger(tmp_path / "calls.jsonl", 1)
    )
    with pytest.raises(EndpointError, match="budget"):
        client.choose([], ["A"], client.clock() + 10)
    assert len(fake.requests) == 1


def test_check_sends_one_real_question_and_selection_resumes(fake, tmp_path, key_file):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(direct(fake.base_url)))
    args = ["--config", str(cfg), "--key-file", str(key_file)]
    assert main(["check", str(DEV / "items"), *args]) == 0
    assert len(fake.requests) == 1
    fake.decide = lambda messages, keys: "B"
    out = tmp_path / "verdicts.json"
    run = ["run", str(DEV / "items"), *args, "--out", str(out)]
    assert main([*run, "--select", "dev-e1-01"]) == 0
    assert len(json.loads(out.read_text())["verdicts"]) == 1
    assert main([*run, "--select", "dev-e1-01", "dev-e1-02"]) == 0
    assert len(json.loads(out.read_text())["verdicts"]) == 2
    assert len(fake.requests) == 3
