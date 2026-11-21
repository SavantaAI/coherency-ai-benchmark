"""Gateway configuration: a JSON file plus environment variable overrides.

Every validation failure raises ConfigError, which the entry point turns into
exit code 2.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")

# The one path the plain listener always answers. Route prefixes may not shadow it.
HEALTH_PATH = "/healthz"

HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "*"}
LOOPBACK_NAMES = {"localhost"}


class ConfigError(ValueError):
    pass


ID_SEGMENT = "{id}"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class Allow:
    """One allowed (method, path).

    `exact` matches the path, where a `{id}` segment matches one segment of
    letters, digits, `_` or `-` (up to 128). `prefix` also matches every path
    below it at a `/` boundary; it exists for tests and local experiments.
    """

    method: str
    path: str
    match: str  # "exact" or "prefix"

    def allows(self, method: str, path: str) -> bool:
        if self.method != "*" and self.method != method:
            return False
        if self.match == "prefix":
            base = self.path.rstrip("/")
            return path == self.path or path.startswith(base + "/")
        want = self.path.split("/")
        got = path.split("/")
        if len(want) != len(got):
            return False
        return all(w == g or (w == ID_SEGMENT and ID_RE.match(g)) for w, g in zip(want, got, strict=True))


@dataclass
class Route:
    """One upstream with one credential, reachable by path prefix and by hostname."""

    name: str
    prefix: str
    upstream: str
    credential_header: str
    credential_format: str
    key_file: str | None
    usage_format: str  # "anthropic", "openai" or "none"
    allow: list[Allow]
    # Hostnames this route answers for in transparent mode.
    hosts: list[str] = field(default_factory=list)
    key: str | None = field(default=None, repr=False)
    disabled_reason: str | None = None

    @property
    def enabled(self) -> bool:
        """A route is active only when its key file was read."""
        return self.key is not None

    @property
    def error_shape(self) -> str:
        return "openai" if self.usage_format == "openai" else "anthropic"

    @property
    def scheme(self) -> str:
        return urlsplit(self.upstream).scheme

    @property
    def upstream_host(self) -> str:
        return urlsplit(self.upstream).hostname or ""

    @property
    def upstream_port(self) -> int:
        parts = urlsplit(self.upstream)
        if parts.port is not None:
            return parts.port
        return 443 if parts.scheme == "https" else 80

    @property
    def upstream_host_header(self) -> str:
        return urlsplit(self.upstream).netloc

    @property
    def upstream_base_path(self) -> str:
        return urlsplit(self.upstream).path.rstrip("/")

    def credential_value(self) -> str:
        assert self.key is not None
        return self.credential_format.replace("{key}", self.key)

    def matches(self, path: str) -> bool:
        """`path` is the request path without its query string."""
        return path == self.prefix or path.startswith(self.prefix + "/")

    def allows(self, method: str, path: str) -> bool:
        """`path` is the upstream-relative path without its query string."""
        return any(a.allows(method, path) for a in self.allow)


@dataclass
class Config:
    # The plain HTTP listener. Without prefix_routes it binds loopback and
    # serves only /healthz.
    host: str = "127.0.0.1"
    port: int = 8080
    prefix_routes: bool = False
    log_dir: str = "/var/log/gateway"
    store_bodies: bool = False
    # Longest silence allowed on the upstream socket, in seconds. There is no
    # limit on the total duration of a response.
    upstream_timeout: float = 900.0
    # Longest silence on the client socket.
    client_idle_timeout: float = 60.0
    # Time allowed for one request's line, headers and body to arrive, counted
    # from the request's first byte. An idle kept-alive connection is closed
    # after client_idle_timeout instead.
    client_read_timeout: float = 300.0
    # Time allowed for the upstream TCP connect and TLS handshake.
    upstream_connect_timeout: float = 15.0
    max_request_bytes: int = 32 * 1024 * 1024
    max_connections: int = 256
    # A non-streaming response body is buffered whole and screened; above
    # this it cannot be screened and is refused.
    max_response_body_bytes: int = 16 * 1024 * 1024
    # One server-sent event is held whole and screened; above this the
    # connection is cut.
    max_sse_event_bytes: int = 16 * 1024 * 1024
    # Cap on one stored body (request or response).
    max_stored_body_bytes: int = 16 * 1024 * 1024
    # Bytes held in request and response buffers across all connections.
    max_buffered_bytes: int = 512 * 1024 * 1024
    # Startup fails unless exactly one route is active.
    single_provider: bool = True
    insecure_allow_http_upstream: bool = False
    routes: list[Route] = field(default_factory=list)
    # Transparent mode: a TLS listener that answers as the upstream hosts.
    transparent: bool = False
    tls_host: str = "0.0.0.0"
    tls_port: int = 443
    tls_dir: str = "/run/gateway-tls"
    cert_days: int = 7
    ca_name: str = "Local Root CA"
    ca_cert_file: str = "local-ca.crt"

    def secrets(self) -> list[str]:
        return [r.key for r in self.routes if r.key]

    def active_routes(self) -> list[Route]:
        return [r for r in self.routes if r.enabled]

    def transparent_hosts(self) -> list[str]:
        """Hostnames of active routes only; inactive routes get no certificate."""
        return [h for r in sorted(self.active_routes(), key=lambda r: r.prefix) for h in r.hosts]

    def route_for_host(self, host: str) -> Route | None:
        for route in self.active_routes():
            if host in route.hosts:
                return route
        return None


# -- typed readers ---------------------------------------------------------------


# Type errors name the field and the expected type, never the value: a
# misplaced value could be a credential.


def _bool(value, what: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{what} must be a JSON boolean (true or false)")


def _env_bool(value: str, what: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{what} must be 1 or 0")


def _int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"{what} must be an integer")
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{what} must be an integer") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{what} must be between {lo} and {hi}")
    return number


def _float(value, what: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{what} must be a number")
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f"{what} must be a number") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{what} must be between {lo} and {hi}")
    return number


def _str(value, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} must be a non-empty string")
    return value


def _obj(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be an object")
    return value


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def valid_path(path: str) -> bool:
    """Origin-form path with no empty, dot or percent-encoded segments."""
    if path == "/":
        return True
    if not path.startswith("/"):
        return False
    segments = path[1:].split("/")
    return all(SEGMENT_RE.match(s) and s not in (".", "..") for s in segments)


def read_key(path: str | None) -> tuple[str | None, str | None]:
    """Return (key, reason_if_missing). The key is never logged."""
    if not path:
        return None, "no key file configured"
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"key file {path} not found"
    except UnicodeDecodeError:
        return None, f"key file {path} is not UTF-8 text"
    except OSError as exc:
        return None, f"key file {path} unreadable ({exc.__class__.__name__})"
    key = raw.strip()
    if not key:
        return None, f"key file {path} is empty"
    if not all(33 <= ord(ch) <= 126 for ch in key):
        return None, f"key file {path} holds characters other than printable ASCII"
    if len(key) < 8:
        return None, f"key file {path} is too short to be a key"
    return key, None


def valid_allow_path(path: str) -> bool:
    if path == "/":
        return True
    if not path.startswith("/"):
        return False
    return all(s == ID_SEGMENT or (SEGMENT_RE.match(s) and s not in (".", "..")) for s in path[1:].split("/"))


def _parse_allow(raw, index: int) -> list[Allow]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"route {index}: allow must be a non-empty list of {{method, path, match}}")
    out = []
    for item in raw:
        item = _obj(item, f"route {index}: allow entry")
        method = _str(item.get("method"), f"route {index}: allow method").upper()
        if method not in METHODS:
            raise ConfigError(f"route {index}: an allow method is not one of {sorted(METHODS)}")
        path = _str(item.get("path"), f"route {index}: allow path")
        if not valid_allow_path(path):
            raise ConfigError(f"route {index}: an allow path is not a plain path")
        match = item.get("match", "exact")
        if match not in ("exact", "prefix"):
            raise ConfigError(f"route {index}: match must be exact or prefix")
        out.append(Allow(method, path, match))
    return out


def _parse_route(raw, index: int, allow_http: bool) -> Route:
    raw = _obj(raw, f"route {index}")
    prefix = _str(raw.get("prefix"), f"route {index}: prefix")
    upstream = _str(raw.get("upstream"), f"route {index}: upstream")
    cred = _obj(raw.get("credential"), f"route {index}: credential")
    header = _str(cred.get("header"), f"route {index}: credential header").lower()
    if not TOKEN_RE.match(header):
        raise ConfigError(f"route {index}: credential header is not a header name")
    if not prefix.startswith("/") or prefix.endswith("/") or not valid_path(prefix):
        raise ConfigError(f"route {index}: prefix must look like /name")
    if prefix == HEALTH_PATH or HEALTH_PATH.startswith(prefix + "/"):
        raise ConfigError(f"route {index}: prefix shadows {HEALTH_PATH}")
    # The URL value is never echoed in an error: it may hold credentials.
    if "@" in upstream.split("?", 1)[0].split("#", 1)[0]:
        raise ConfigError(f"route {index}: upstream may not contain user information")
    try:
        parts = urlsplit(upstream)
        port = parts.port
    except ValueError:
        raise ConfigError(f"route {index}: upstream is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"route {index}: upstream must be an https URL")
    if parts.scheme == "http" and not allow_http:
        raise ConfigError(
            f"route {index}: an http upstream would receive the key in cleartext; "
            "use https, or set insecure_allow_http_upstream for local experiments"
        )
    if parts.query or parts.fragment:
        raise ConfigError(f"route {index}: upstream may not carry a query or fragment")
    if parts.path and parts.path != "/" and not valid_path(parts.path.rstrip("/")):
        raise ConfigError(f"route {index}: upstream path is not a plain path")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"route {index}: upstream port out of range")
    fmt = cred.get("format", "{key}")
    if not isinstance(fmt, str) or "{key}" not in fmt or any(c in fmt for c in "\r\n"):
        raise ConfigError(f"route {index}: credential format must be one line containing {{key}}")
    key_file = cred.get("key_file")
    if key_file is not None and not isinstance(key_file, str):
        raise ConfigError(f"route {index}: key_file must be a string")
    usage = raw.get("usage", "none")
    if usage not in ("anthropic", "openai", "none"):
        raise ConfigError(f"route {index}: usage must be anthropic, openai or none")
    hosts = raw.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and HOSTNAME_RE.match(h.lower()) for h in hosts):
        raise ConfigError(f"route {index}: hosts must be a list of hostnames")
    name = raw.get("name") or prefix.strip("/")
    if not isinstance(name, str) or not re.match(r"^[a-z0-9_]+$", name):
        raise ConfigError(f"route {index}: name must be lower-case letters, digits or _")
    return Route(
        name=name,
        prefix=prefix,
        upstream=upstream.rstrip("/"),
        credential_header=header,
        credential_format=fmt,
        key_file=key_file,
        usage_format=usage,
        allow=_parse_allow(raw.get("allow"), index),
        hosts=[h.lower() for h in hosts],
    )


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    """Load the JSON config, apply environment overrides, and read key files.

    Environment variables (all optional):
      GATEWAY_CONFIG             path of the JSON config file
      GATEWAY_PREFIX_ROUTES      1 serves path-prefix routes on the plain listener
      GATEWAY_HOST, GATEWAY_PORT plain listener address
      GATEWAY_LOG_DIR            directory for requests.jsonl and stored bodies
      GATEWAY_STORE_BODIES       1/0, store request and response bodies
      GATEWAY_UPSTREAM_TIMEOUT   seconds of upstream silence before giving up
      GATEWAY_KEY_FILE_<NAME>    key file for the route named NAME (upper case)
      GATEWAY_TRANSPARENT        1/0, run the TLS listener for transparent mode
      GATEWAY_TLS_PORT           port of that listener
      GATEWAY_TLS_DIR            where the CA and leaf certificates are written
    """
    env = dict(os.environ if env is None else env)
    config_path = Path(path or env.get("GATEWAY_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file {config_path} not found") from None
    except UnicodeDecodeError:
        raise ConfigError(f"config file {config_path} is not UTF-8") from None
    except OSError as exc:
        raise ConfigError(f"config file {config_path} unreadable ({exc.__class__.__name__})") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {config_path}: invalid JSON at line {exc.lineno}") from None
    raw = _obj(raw, "config")

    cfg = Config()
    listen = _obj(raw.get("listen", {}), "listen")
    if "host" in listen:
        cfg.host = _str(listen["host"], "listen.host")
    if "port" in listen:
        cfg.port = _int(listen["port"], "listen.port", 0, 65535)
    if "prefix_routes" in listen:
        cfg.prefix_routes = _bool(listen["prefix_routes"], "listen.prefix_routes")
    if "log_dir" in raw:
        cfg.log_dir = _str(raw["log_dir"], "log_dir")
    if "store_bodies" in raw:
        cfg.store_bodies = _bool(raw["store_bodies"], "store_bodies")
    if "upstream_timeout_seconds" in raw:
        cfg.upstream_timeout = _float(raw["upstream_timeout_seconds"], "upstream_timeout_seconds", 1, 86400)
    if "client_idle_timeout_seconds" in raw:
        cfg.client_idle_timeout = _float(raw["client_idle_timeout_seconds"], "client_idle_timeout_seconds", 0.1, 86400)
    if "client_read_timeout_seconds" in raw:
        cfg.client_read_timeout = _float(raw["client_read_timeout_seconds"], "client_read_timeout_seconds", 0.1, 86400)
    if "max_request_bytes" in raw:
        cfg.max_request_bytes = _int(raw["max_request_bytes"], "max_request_bytes", 1, 1 << 34)
    if "max_connections" in raw:
        cfg.max_connections = _int(raw["max_connections"], "max_connections", 1, 100000)
    for name in ("max_response_body_bytes", "max_sse_event_bytes", "max_stored_body_bytes"):
        if name in raw:
            setattr(cfg, name, _int(raw[name], name, 1024, 1 << 31))
    if "max_buffered_bytes" in raw:
        cfg.max_buffered_bytes = _int(raw["max_buffered_bytes"], "max_buffered_bytes", 1 << 20, 1 << 40)
    if "upstream_connect_timeout_seconds" in raw:
        cfg.upstream_connect_timeout = _float(raw["upstream_connect_timeout_seconds"], "upstream_connect_timeout_seconds", 0.1, 600)
    if "single_provider" in raw:
        cfg.single_provider = _bool(raw["single_provider"], "single_provider")
    if "insecure_allow_http_upstream" in raw:
        cfg.insecure_allow_http_upstream = _bool(raw["insecure_allow_http_upstream"], "insecure_allow_http_upstream")

    tls = _obj(raw.get("transparent", {}), "transparent")
    if "enabled" in tls:
        cfg.transparent = _bool(tls["enabled"], "transparent.enabled")
    if "host" in tls:
        cfg.tls_host = _str(tls["host"], "transparent.host")
    if "port" in tls:
        cfg.tls_port = _int(tls["port"], "transparent.port", 0, 65535)
    if "tls_dir" in tls:
        cfg.tls_dir = _str(tls["tls_dir"], "transparent.tls_dir")
    if "cert_days" in tls:
        cfg.cert_days = _int(tls["cert_days"], "transparent.cert_days", 1, 397)
    if "ca_name" in tls:
        cfg.ca_name = _str(tls["ca_name"], "transparent.ca_name")
        if not re.match(r"^[A-Za-z0-9 .,_-]{1,64}$", cfg.ca_name):
            raise ConfigError("transparent.ca_name may hold letters, digits, spaces and .,_- only")
    if "ca_cert_file" in tls:
        cfg.ca_cert_file = _str(tls["ca_cert_file"], "transparent.ca_cert_file")
        if not re.match(r"^[A-Za-z0-9._-]{1,64}$", cfg.ca_cert_file) or cfg.ca_cert_file.startswith("."):
            raise ConfigError("transparent.ca_cert_file must be a plain file name")

    if "GATEWAY_PREFIX_ROUTES" in env:
        cfg.prefix_routes = _env_bool(env["GATEWAY_PREFIX_ROUTES"], "GATEWAY_PREFIX_ROUTES")
    if "GATEWAY_HOST" in env:
        cfg.host = _str(env["GATEWAY_HOST"], "GATEWAY_HOST")
    if "GATEWAY_PORT" in env:
        cfg.port = _int(env["GATEWAY_PORT"], "GATEWAY_PORT", 0, 65535)
    if "GATEWAY_LOG_DIR" in env:
        cfg.log_dir = _str(env["GATEWAY_LOG_DIR"], "GATEWAY_LOG_DIR")
    if "GATEWAY_STORE_BODIES" in env:
        cfg.store_bodies = _env_bool(env["GATEWAY_STORE_BODIES"], "GATEWAY_STORE_BODIES")
    if "GATEWAY_UPSTREAM_TIMEOUT" in env:
        cfg.upstream_timeout = _float(env["GATEWAY_UPSTREAM_TIMEOUT"], "GATEWAY_UPSTREAM_TIMEOUT", 1, 86400)
    if "GATEWAY_TRANSPARENT" in env:
        cfg.transparent = _env_bool(env["GATEWAY_TRANSPARENT"], "GATEWAY_TRANSPARENT")
    if "GATEWAY_TLS_PORT" in env:
        cfg.tls_port = _int(env["GATEWAY_TLS_PORT"], "GATEWAY_TLS_PORT", 0, 65535)
    if "GATEWAY_TLS_DIR" in env:
        cfg.tls_dir = _str(env["GATEWAY_TLS_DIR"], "GATEWAY_TLS_DIR")

    if not cfg.prefix_routes and not is_loopback(cfg.host):
        raise ConfigError(
            f"the plain listener on {cfg.host} would be reachable from other containers; "
            "it binds loopback unless listen.prefix_routes (GATEWAY_PREFIX_ROUTES=1) opts in to path-prefix mode"
        )
    if not cfg.prefix_routes and not cfg.transparent:
        raise ConfigError("neither transparent mode nor path-prefix routes are enabled; nothing to serve")

    routes = raw.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ConfigError("config needs a non-empty routes list")
    seen: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(routes):
        route = _parse_route(item, index, cfg.insecure_allow_http_upstream)
        if route.prefix in seen or route.name in names:
            raise ConfigError(f"duplicate route prefix or name {route.prefix}")
        seen.add(route.prefix)
        names.add(route.name)
        override = env.get(f"GATEWAY_KEY_FILE_{route.name.upper()}")
        if override is not None:
            route.key_file = override or None
        route.key, route.disabled_reason = read_key(route.key_file)
        cfg.routes.append(route)
    all_hosts = [h for r in cfg.routes for h in r.hosts]
    if len(all_hosts) != len(set(all_hosts)):
        raise ConfigError("a hostname appears in more than one route")
    if cfg.transparent and not all_hosts:
        raise ConfigError("transparent mode is on but no route lists hosts")
    if cfg.max_request_bytes > cfg.max_buffered_bytes:
        raise ConfigError("max_request_bytes may not exceed max_buffered_bytes")
    # Longest prefix first, so /a/b wins over /a.
    cfg.routes.sort(key=lambda r: len(r.prefix), reverse=True)
    return cfg


def check_startup(cfg: Config) -> None:
    """Conditions that need the key files: raised as ConfigError at startup."""
    active = cfg.active_routes()
    if not active:
        raise ConfigError("no route has a key file; nothing to serve")
    if cfg.single_provider and len(active) != 1:
        names = ", ".join(sorted(r.name for r in active))
        raise ConfigError(
            f"single_provider is on but {len(active)} routes are active ({names}); "
            "mount exactly one key file, or set single_provider to false"
        )
    if cfg.transparent and not cfg.transparent_hosts():
        raise ConfigError("transparent mode is on but no active route lists hosts")
