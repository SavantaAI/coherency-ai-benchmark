"""The classifier's pinned configuration, for a hosted OpenAI-compatible
chat completions endpoint.

A config file names everything the classifier controls about a verdict: the
endpoint's base URL, the model id, the provider routing (for OpenRouter: one
provider, no fallbacks, a required quantization), what the response must
report as having served it, the prompt version and the hash of its texts,
the sampling settings, the response schema, the time limits and the context
budget. Its digest goes into the record of every verdict.

The API key is never in the config. It is read from a file named by the
`COHBENCH_CLASSIFIER_KEY_FILE` environment variable or a command-line
option (`api.read_key`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import prompts

FORMAT = "cohbench-classifier-config"
VERSION = 2


class ConfigError(ValueError):
    """The config file is malformed, or something does not match it."""


def freeze(v: Any) -> Any:
    """A read-only deep copy: mappings become mapping proxies, lists tuples."""
    if isinstance(v, Mapping):
        return MappingProxyType({k: freeze(x) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return tuple(freeze(x) for x in v)
    return v


def thaw(v: Any) -> Any:
    """A plain, mutable deep copy of a frozen value, for building a request."""
    if isinstance(v, Mapping):
        return {k: thaw(x) for k, x in v.items()}
    if isinstance(v, tuple):
        return [thaw(x) for x in v]
    return v


@dataclass(frozen=True)
class Config:
    doc: Mapping[str, Any]  # frozen: read-only, so it always matches the digest
    digest: str  # sha256 of the canonical JSON of the whole document
    path: Path | None = None

    @property
    def endpoint(self) -> Mapping[str, Any]:
        return self.doc["endpoint"]

    @property
    def sampling(self) -> Mapping[str, Any]:
        return self.doc["sampling"]

    @property
    def schema(self) -> Mapping[str, Any]:
        return self.doc["schema"]

    @property
    def limits(self) -> Mapping[str, Any]:
        return self.doc["limits"]

    @property
    def context(self) -> Mapping[str, Any]:
        return self.doc["context"]


def canonical(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _obj(doc: Mapping[str, Any], key: str, where: str, fields: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:  # type: ignore[assignment]
    v = doc.get(key)
    if not isinstance(v, Mapping):
        raise ConfigError(f"{where}.{key}: an object")
    missing = fields - set(v)
    extra = set(v) - fields - optional
    if missing:
        raise ConfigError(f"{where}.{key}: missing {sorted(missing)}")
    if extra:
        raise ConfigError(f"{where}.{key}: unknown fields {sorted(extra)}")
    return v


def _num(v: Any, where: str, *, integer: bool = False, positive: bool = True) -> None:
    """A finite number: above zero, or with `positive` false at least zero."""
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if not ok or isinstance(v, bool) or not math.isfinite(v) or (v <= 0 if positive else v < 0):
        what = "positive" if positive else "non-negative"
        raise ConfigError(f"{where}: a finite {what} {'integer' if integer else 'number'}")


def _text(v: Any, where: str) -> None:
    if not isinstance(v, str) or not v:
        raise ConfigError(f"{where}: text")


def parse(doc: Any, path: Path | None = None) -> Config:
    if not isinstance(doc, Mapping):
        raise ConfigError("config: an object")
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        raise ConfigError(f"config.format and version: {FORMAT!r} and {VERSION}")
    extra = set(doc) - {"format", "version", "description", "endpoint", "prompt", "sampling", "schema", "limits", "context"}
    if extra:
        raise ConfigError(f"config: unknown fields {sorted(extra)}")
    e = _obj(doc, "endpoint", "config", {"base_url", "model", "provider", "expect"}, {"extra_body"})
    _text(e["base_url"], "config.endpoint.base_url")
    u = urllib.parse.urlsplit(e["base_url"])
    if not ((u.scheme == "https" and u.hostname) or (u.scheme == "http" and u.hostname == "127.0.0.1")) or u.username or u.password:
        raise ConfigError("config.endpoint.base_url: https, or http on 127.0.0.1 for a local stand-in, with no credentials")
    _text(e["model"], "config.endpoint.model")
    pv = e["provider"]
    if not isinstance(pv, Mapping):
        raise ConfigError("config.endpoint.provider: an object naming one provider")
    order = pv.get("order")
    if not (isinstance(order, list) and len(order) == 1 and isinstance(order[0], str) and order[0].strip()):
        raise ConfigError("config.endpoint.provider.order: exactly one provider")
    if pv.get("allow_fallbacks") is not False:
        raise ConfigError("config.endpoint.provider.allow_fallbacks: false")
    if "extra_body" in e and not isinstance(e["extra_body"], Mapping):
        raise ConfigError("config.endpoint.extra_body: an object")
    reserved = {"model", "messages", "response_format", "temperature", "seed", "max_tokens", "provider", "stream"}
    if set(e.get("extra_body", {})) & reserved:
        raise ConfigError(f"config.endpoint.extra_body: may not set {sorted(set(e['extra_body']) & reserved)}")
    x = _obj(e, "expect", "config.endpoint", {"model", "provider"})
    for k in ("model", "provider"):
        if not isinstance(x[k], str) or not x[k].strip():
            raise ConfigError(f"config.endpoint.expect.{k}: the name every response must report; it cannot be left out")
    p = _obj(doc, "prompt", "config", {"version", "template", "sha256"})
    for k in p:
        _text(p[k], f"config.prompt.{k}")
    sm = _obj(doc, "sampling", "config", {"temperature", "seed", "max_tokens"})
    if not isinstance(sm["temperature"], (int, float)) or isinstance(sm["temperature"], bool) or sm["temperature"] != 0:
        raise ConfigError("config.sampling.temperature: 0")
    _num(sm["seed"], "config.sampling.seed", integer=True, positive=False)
    _num(sm["max_tokens"], "config.sampling.max_tokens", integer=True)
    sc = _obj(doc, "schema", "config", {"name", "strict", "field"})
    if sc["strict"] is not True:
        raise ConfigError("config.schema.strict: true")
    if sc["field"] != prompts.FIELD:
        raise ConfigError(f"config.schema.field: {prompts.FIELD!r}")
    _text(sc["name"], "config.schema.name")
    lm = _obj(doc, "limits", "config", {"verdict_deadline_s", "request_timeout_s", "retries", "backoff_s"})
    _num(lm["verdict_deadline_s"], "config.limits.verdict_deadline_s")
    _num(lm["request_timeout_s"], "config.limits.request_timeout_s")
    _num(lm["backoff_s"], "config.limits.backoff_s", positive=False)
    if lm["retries"] not in (0, 1) or isinstance(lm["retries"], bool):
        raise ConfigError("config.limits.retries: 0 or 1")
    c = _obj(doc, "context", "config", {"max_input_tokens", "chars_per_token", "margin", "keep_message_tokens"})
    _num(c["max_input_tokens"], "config.context.max_input_tokens", integer=True)
    _num(c["chars_per_token"], "config.context.chars_per_token")
    _num(c["margin"], "config.context.margin", integer=True, positive=False)
    _num(c["keep_message_tokens"], "config.context.keep_message_tokens", integer=True)
    try:
        raw = canonical(doc)
    except ValueError as exc:  # a NaN or infinity elsewhere in the document
        raise ConfigError(f"config: not plain JSON ({exc})") from None
    return Config(doc=freeze(json.loads(raw)), digest=hashlib.sha256(raw).hexdigest(), path=path)


class _DuplicateKey(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for k, _ in pairs:
        if k in seen:
            raise _DuplicateKey(repr(k))
        seen.add(k)
    return dict(pairs)


def load(path: str | os.PathLike[str]) -> Config:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    except _DuplicateKey as exc:
        raise ConfigError(f"{p}: the key {exc} appears twice in one object") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{p}: {exc}") from None
    cfg = parse(doc, p)
    check_prompts(cfg)
    return cfg


def check_prompts(cfg: Config) -> None:
    """The prompt texts in this code are the ones the config was made for."""
    p = cfg.doc["prompt"]
    if p["version"] != prompts.VERSION:
        raise ConfigError(f"config names prompt version {p['version']!r}; the code has {prompts.VERSION!r}")
    if p["template"] != prompts.TEMPLATE:
        raise ConfigError(f"config names prompt template {p['template']!r}; the code renders {prompts.TEMPLATE!r}")
    if p["sha256"] != prompts.digest():
        raise ConfigError(f"the prompt texts changed: config has {p['sha256'][:12]}, the code has {prompts.digest()[:12]}; bump the version")
