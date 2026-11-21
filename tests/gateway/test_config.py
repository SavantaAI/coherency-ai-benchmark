"""Configuration loading and validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

ALLOW = [{"method": "POST", "path": "/v1/messages"}]


def test_shipped_config():
    cfg = load_config(DEFAULT_CONFIG_PATH, env={})
    routes = {r.prefix: r for r in cfg.routes}
    assert set(routes) == {"/anthropic", "/openai"}
    a, o = routes["/anthropic"], routes["/openai"]
    assert (a.upstream, a.credential_header, a.hosts) == ("https://api.anthropic.com", "x-api-key", ["api.anthropic.com"])
    assert (o.upstream, o.credential_header, o.credential_format) == ("https://api.openai.com", "authorization", "Bearer {key}")
    # Transparent mode on; the plain listener is loopback-only health.
    assert cfg.transparent and not cfg.prefix_routes and cfg.host == "127.0.0.1"
    assert cfg.store_bodies is False
    # The key files do not exist on a development machine: nothing is active.
    assert cfg.active_routes() == [] and cfg.transparent_hosts() == []
    # The Anthropic allowlist is what the two harnesses were seen to call.
    assert a.allows("POST", "/v1/messages")
    assert a.allows("POST", "/v1/messages/count_tokens")
    assert a.allows("GET", "/api/claude_code/settings")
    assert a.allows("GET", "/api/claude_code/policy_limits")
    for method, path in [("GET", "/v1/models"), ("POST", "/v1/files"), ("GET", "/v1/messages"), ("POST", "/v1/messages/batches")]:
        assert not a.allows(method, path), (method, path)
    allowed = [
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/responses"),
        ("GET", "/v1/responses/resp_67ccd2bed1ec8190"),
        ("DELETE", "/v1/responses/resp_1"),
        ("POST", "/v1/responses/resp_1/cancel"),
        ("GET", "/v1/responses/resp_1/input_items"),
    ]
    refused = [
        ("POST", "/v1/files"),
        ("POST", "/v1/responses/resp_1"),
        ("GET", "/v1/responses"),
        ("GET", "/v1/responses/resp_1/cancel"),
        ("POST", "/v1/responses/resp_1/cancel/again"),
        ("GET", "/v1/responses/a.b"),
        ("POST", "/v1/responses/compact"),
        ("POST", "/v1/responses/input_tokens"),
    ]
    assert all(o.allows(m, p) for m, p in allowed)
    assert not any(o.allows(m, p) for m, p in refused), [x for x in refused if o.allows(*x)]
    assert cfg.single_provider is True


def test_env_overrides(tmp_path):
    key = tmp_path / "k"
    key.write_text("sk-made-up-key\n")
    env = {
        "GATEWAY_PORT": "9999",
        "GATEWAY_LOG_DIR": str(tmp_path / "logs"),
        "GATEWAY_STORE_BODIES": "1",
        "GATEWAY_UPSTREAM_TIMEOUT": "60",
        "GATEWAY_KEY_FILE_ANTHROPIC": str(key),
    }
    cfg = load_config(DEFAULT_CONFIG_PATH, env=env)
    assert (cfg.port, cfg.log_dir, cfg.store_bodies, cfg.upstream_timeout) == (9999, str(tmp_path / "logs"), True, 60.0)
    routes = {r.name: r for r in cfg.routes}
    assert routes["anthropic"].enabled
    assert routes["anthropic"].credential_value() == "sk-made-up-key"
    assert not routes["openai"].enabled
    assert "not found" in routes["openai"].disabled_reason
    assert cfg.secrets() == ["sk-made-up-key"]
    # Only the active route's host gets a certificate.
    assert cfg.transparent_hosts() == ["api.anthropic.com"]


@pytest.mark.parametrize(
    "content,reason",
    [("  \n", "empty"), ("sk-has space-inside", "printable ASCII"), ("short", "too short")],
)
def test_bad_key_file_disables_route(tmp_path, content, reason):
    key = tmp_path / "k"
    key.write_text(content)
    cfg = load_config(DEFAULT_CONFIG_PATH, env={"GATEWAY_KEY_FILE_ANTHROPIC": str(key)})
    route = next(r for r in cfg.routes if r.name == "anthropic")
    assert not route.enabled
    assert reason in route.disabled_reason


def test_non_utf8_key_file(tmp_path):
    key = tmp_path / "k"
    key.write_bytes(b"\xff\xfe\x00")
    cfg = load_config(DEFAULT_CONFIG_PATH, env={"GATEWAY_KEY_FILE_ANTHROPIC": str(key)})
    assert "UTF-8" in next(r for r in cfg.routes if r.name == "anthropic").disabled_reason


def write(tmp_path, doc) -> Path:
    path = tmp_path / "c.json"
    path.write_text(json.dumps(doc))
    return path


def base(route=None, **top):
    route = {"prefix": "/a", "upstream": "https://x.example", "credential": {"header": "x-api-key"}, "allow": ALLOW, **(route or {})}
    return {"listen": {"prefix_routes": True}, "routes": [route], **top}


@pytest.mark.parametrize(
    "doc",
    [
        base({"prefix": "anthropic"}),
        base({"prefix": "/a/"}),
        base({"prefix": "/a%2e"}),
        base({"upstream": "ftp://x"}),
        base({"upstream": "http://x.example"}),
        base({"upstream": "https://user:pw@x.example"}),
        base({"upstream": "https://x.example:99999"}),
        base({"upstream": "https://x.example/?q=1"}),
        base({"credential": {"header": "x-api-key", "format": "Bearer"}}),
        base({"credential": {"header": "bad header"}}),
        base({"prefix": "/healthz"}),
        base({"allow": None}),
        base({"allow": [{"method": "FETCH", "path": "/v1"}]}),
        base({"allow": [{"method": "GET", "path": "/v1/%2e%2e"}]}),
        base({"allow": [{"method": "GET", "path": "/v1", "match": "glob"}]}),
        base({"hosts": ["api.example.com:443"]}),
        base({"hosts": ["bad_host!"]}),
        base(store_bodies="false"),
        base(max_request_bytes="lots"),
        base(max_request_bytes=0),
        base(upstream_timeout_seconds=True),
        base(max_connections=10**9),
        {"listen": {"host": "0.0.0.0"}, "transparent": {"enabled": True}, "routes": base()["routes"]},
        {"listen": {"prefix_routes": False}, "routes": base()["routes"]},
        base(transparent={"enabled": True, "ca_cert_file": "../x.crt"}),
        base(transparent={"enabled": True, "ca_name": "Bad/Name"}),
        [1, 2],
    ],
)
def test_invalid_config_rejected(tmp_path, doc):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, doc), env={})


def test_plain_http_upstream_needs_loud_opt_in(tmp_path):
    cfg = load_config(write(tmp_path, base({"upstream": "http://x.example"}, insecure_allow_http_upstream=True)), env={})
    assert cfg.routes[0].scheme == "http"


@pytest.mark.parametrize("env", [{"GATEWAY_STORE_BODIES": "maybe"}, {"GATEWAY_PORT": "x"}, {"GATEWAY_HOST": "0.0.0.0"}])
def test_invalid_env_rejected(env):
    with pytest.raises(ConfigError):
        load_config(DEFAULT_CONFIG_PATH, env=env)


def test_bad_config_exits_2(tmp_path):
    path = write(tmp_path, base(store_bodies="false"))
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-m", "gateway", "--config", str(path)], capture_output=True, text=True, cwd=root)
    assert out.returncode == 2
    assert "store_bodies must be a JSON boolean" in out.stderr


def test_errors_never_echo_the_upstream_url(tmp_path):
    secret_url = "https://user:hunter2-secret@x.example:99999/"
    for url in (secret_url, "http://hunter2-secret.example", "https://x.example:notaport-hunter2-secret"):
        with pytest.raises(ConfigError) as caught:
            load_config(write(tmp_path, base({"upstream": url})), env={})
        assert "hunter2-secret" not in str(caught.value)


def test_config_io_errors_are_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path, env={})  # a directory
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError):
        load_config(bad, env={})


def test_id_pattern_in_allow_paths(tmp_path):
    allow = [{"method": "GET", "path": "/v1/things/{id}/parts"}]
    cfg = load_config(write(tmp_path, base({"allow": allow})), env={})
    route = cfg.routes[0]
    assert route.allows("GET", "/v1/things/abc_1-2/parts")
    assert not route.allows("GET", "/v1/things/abc/parts/x")
    assert not route.allows("GET", "/v1/things/a.b/parts")
    assert not route.allows("GET", "/v1/things/" + "a" * 129 + "/parts")
