from __future__ import annotations

from pathlib import Path

import pytest

from cohbench.user import prompts
from cohbench.user.api import KEY_ENV, EndpointError, read_key
from cohbench.user.config import ConfigError, check_prompts, load, parse

from .conftest import KEY, SHIPPED, config_doc


def test_shipped_config_loads_and_matches_the_prompts():
    cfg = load(SHIPPED)
    e = cfg.endpoint
    assert e["provider"]["allow_fallbacks"] is False and len(e["provider"]["order"]) == 1
    assert cfg.sampling["temperature"] == 0 and cfg.schema["strict"] is True
    assert "key" not in SHIPPED.read_text().lower().replace("keep_message_tokens", "")


def test_digest_is_stable_and_covers_every_field():
    a = parse(config_doc())
    assert a.digest == parse(config_doc()).digest
    d = config_doc()
    d["sampling"]["seed"] += 1
    assert parse(d).digest != a.digest
    d = config_doc()
    d["endpoint"]["provider"]["quantizations"] = ["bf16"]
    assert parse(d).digest != a.digest


@pytest.mark.parametrize("change,match", [
    (lambda d: d.update(version=1), "version"),
    (lambda d: d["sampling"].update(temperature=0.7), "temperature"),
    (lambda d: d["sampling"].update(top_p=1), "sampling"),
    (lambda d: d["endpoint"].update(base_url="http://example.com/v1"), "https"),
    (lambda d: d["endpoint"]["provider"].update(order=["a", "b"]), "exactly one provider"),
    (lambda d: d["endpoint"]["provider"].update(allow_fallbacks=True), "allow_fallbacks"),
    (lambda d: d["endpoint"]["provider"].pop("allow_fallbacks"), "allow_fallbacks"),
    (lambda d: d["endpoint"].update(extra_body={"temperature": 1}), "may not set"),
    (lambda d: d["endpoint"]["expect"].pop("model"), "missing"),
    (lambda d: d["schema"].update(strict=False), "strict"),
    (lambda d: d["schema"].update(field="answer"), "field"),
    (lambda d: d["limits"].update(retries=3), "retries"),
    (lambda d: d["limits"].update(verdict_deadline_s=0), "deadline"),
    (lambda d: d["context"].pop("chars_per_token"), "missing"),
    (lambda d: d.update(api_key="x"), "unknown"),
    (lambda d: d["endpoint"].update(base_url="http://127.0.0.1.example.com/v1"), "https"),
    (lambda d: d["endpoint"].update(base_url="https://user:pw@example.com/v1"), "credentials"),
    # C5: the served-by check cannot be switched off.
    (lambda d: d["endpoint"].update(provider=None), "provider"),
    (lambda d: d["endpoint"]["expect"].update(provider=None), "expect.provider"),
    (lambda d: d["endpoint"]["expect"].update(provider=" "), "expect.provider"),
    (lambda d: d["endpoint"]["expect"].update(model=""), "expect.model"),
    (lambda d: d["endpoint"]["provider"].update(order=[""]), "exactly one provider"),
    # non-finite and negative numbers
    (lambda d: d["limits"].update(verdict_deadline_s=float("nan")), "finite"),
    (lambda d: d["limits"].update(request_timeout_s=float("inf")), "finite"),
    (lambda d: d["limits"].update(backoff_s=-0.5), "non-negative"),
    (lambda d: d["context"].update(margin=-1), "non-negative"),
    (lambda d: d["sampling"].update(seed=-1), "non-negative"),
    (lambda d: d["sampling"].update(temperature=float("nan")), "temperature"),
    (lambda d: d.update(description=float("nan")), "plain JSON"),
])
def test_bad_configs_are_refused(change, match):
    d = config_doc()
    change(d)
    with pytest.raises(ConfigError, match=match):
        parse(d)


def test_changed_prompt_text_is_refused(monkeypatch, tmp_path):
    cfg = parse(config_doc())
    check_prompts(cfg)
    copy = tmp_path / "prompts.py"
    copy.write_text(Path(prompts.__file__).read_text().replace("Question type 1 (request)", "Question type 1 (asks)"))
    monkeypatch.setattr(prompts, "__file__", str(copy))
    with pytest.raises(ConfigError, match="bump the version"):
        check_prompts(cfg)


def test_other_prompt_version_is_refused():
    d = config_doc()
    d["prompt"]["version"] = "p0"
    with pytest.raises(ConfigError, match="version"):
        check_prompts(parse(d))


def test_read_key(key_file, tmp_path, monkeypatch):
    assert read_key(key_file) == KEY
    monkeypatch.setenv(KEY_ENV, str(key_file))
    assert read_key() == KEY
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(EndpointError, match=KEY_ENV):
        read_key()
    bad = tmp_path / "bad"
    bad.write_text("two words\n")
    with pytest.raises(EndpointError) as e:
        read_key(bad)
    assert "two" not in str(e.value)
    with pytest.raises(EndpointError, match="cannot be read"):
        read_key(tmp_path / "missing")


def test_parsed_config_is_frozen():
    cfg = parse(config_doc())
    with pytest.raises(TypeError):
        cfg.doc["limits"]["verdict_deadline_s"] = 99  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        cfg.endpoint["provider"]["order"].append("other")  # type: ignore[union-attr]


def test_backoff_zero_is_allowed():
    d = config_doc()
    d["limits"]["backoff_s"] = 0
    assert parse(d).limits["backoff_s"] == 0


def test_key_file_that_is_not_text(tmp_path):
    p = tmp_path / "key"
    p.write_bytes(b"\xff\xfe" + KEY.encode())
    with pytest.raises(EndpointError, match="not UTF-8") as e:
        read_key(p)
    assert KEY not in str(e.value)
