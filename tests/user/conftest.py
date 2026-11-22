"""Shared fixtures. Every test runs against `fake_server.Fake`; no test calls
a real endpoint."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from cohbench.user import prompts
from cohbench.user.config import Config, parse

from .fake_server import Fake

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "cohbench" / "user" / "configs" / "openrouter-qwen3.6-27b.json"
DEV = ROOT / "calibration" / "classifier" / "dev"
KEY = "sk-test-0123456789abcdef"  # letters, digits and dashes: it would pass the metadata pattern


class Clock:
    """A clock the tests move by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def config_doc(base_url: str = "http://127.0.0.1:9/v1", *, context: dict[str, Any] | None = None,
               limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """The shipped config, pointed at the fake endpoint."""
    doc = copy.deepcopy(json.loads(SHIPPED.read_text()))
    e = doc["endpoint"]
    e["base_url"] = base_url
    e["model"] = e["expect"]["model"] = "test/model-1"
    e["provider"]["order"] = ["testprovider"]
    e["expect"]["provider"] = "TestProvider"
    doc["prompt"].update(version=prompts.VERSION, template=prompts.TEMPLATE, sha256=prompts.digest())
    doc["context"].update(context or {})
    doc["limits"].update(limits or {})
    return doc


@pytest.fixture
def fake():
    with Fake() as f:
        yield f


@pytest.fixture
def cfg(fake) -> Config:
    return parse(config_doc(fake.base_url))


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    p = tmp_path / "key"
    p.write_text(KEY + "\n")
    return p
