"""One harness container's network surroundings: its gateway and its upstream.

A site is an internal agent network whose bridge has no host address, a
gateway container on it at `.10` holding the one provider route, and the
gateway's way up:

- real: a plain egress network, and the key snapshot's bytes copied into
  the created gateway container (uid 10001, mode 0400) before it starts,
  never into anything else and never to a host file;
- scripted: a second internal network shared by the gateway and a scripted
  model server (cohbench/scripted), with a random placeholder key in the
  gateway, since a route is active only when its key file exists. The route
  points at `http://<server>:8080` with `insecure_allow_http_upstream`.

Both present the same thing to the harness container: `api.anthropic.com`
resolving to the gateway, which answers TLS for it with a CA made at start.
The main run and every probe copy get their own site.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import checks
from .docker import Container, DockerError, KeySource, KeyLeak, Registry, copy_bytes, copy_out, deadline_passed, cp_in, extract, key_hits, one_file_tar, opaque_name, placeholder_key, run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_CA = "/run/gateway-tls/local-ca.crt"
GATEWAY_KEY = "/run/secrets/anthropic_key"
GATEWAY_UID = 10001  # the gateway image's user (docker/gateway/Dockerfile)
GATEWAY_LOG = "/var/log/gateway"
SCRIPTED_LOG = "/var/log/scripted"


@dataclass(frozen=True)
class Provider:
    """One model API as the gateway and the harness container see it."""

    name: str  # the gateway route (gateway/config.json)
    api_host: str  # the name the harness resolves to the gateway
    upstream_url: str
    key_env: str  # the placeholder credential's variable in the harness container
    key_prefix: str
    key_length: int = 93
    key_suffix: str = "AA"

    def placeholder(self) -> str:
        return placeholder_key(self.key_prefix, self.key_length, self.key_suffix)


# Only Anthropic is exercised. Another vendor needs its own entry here, a
# gateway route of that name, and the harness's model slug for it.
ANTHROPIC = Provider("anthropic", "api.anthropic.com", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "sk-ant-api03-")


@dataclass(frozen=True)
class Upstream:
    """Where the gateway sends model calls. Build with `real` or `scripted`."""

    kind: str  # "real" or "scripted"
    key: KeySource | None = None
    script: dict | None = None
    provider: Provider = ANTHROPIC
    # scripted only: a scripted server state to import before the first
    # request (`admin import`), and the script that answers what comes next.
    seed_state: dict | None = None
    next_script: dict | str | None = None

    @staticmethod
    def real(key_file: str | Path, provider: Provider = ANTHROPIC) -> "Upstream":
        from .docker import load_key

        return Upstream("real", key=load_key(key_file, repo_root=REPO_ROOT), provider=provider)

    @staticmethod
    def scripted(script: dict | None = None, *, seed_state: dict | None = None, next_script: dict | str | None = None) -> "Upstream":
        """The scripted server speaks the Anthropic Messages API only."""
        return Upstream("scripted", script=script, seed_state=seed_state, next_script=next_script)

    def __repr__(self) -> str:
        return f"Upstream({self.kind!r}, {self.provider.name!r})"


@dataclass
class Site:
    label: str
    upstream: Upstream
    agent_network: str
    up_network: str
    gateway: Container
    gateway_ip: str
    bridge: dict
    ca_pem: bytes
    log_dir: Path
    scripted: Container | None = None
    scripted_log: Path | None = None
    names: dict[str, str] = field(default_factory=dict)

    @property
    def flushed(self) -> bool:
        return self.bridge.get("mode") != "gateway_mode_ipv4=isolated"

    # ----- the scripted server's admin interface, through docker exec ---------

    def _admin(self, args: list[str], stdin: str | None = None) -> str:
        if self.scripted is None:
            raise RuntimeError("this site has the real upstream; there is no scripted server")
        proc = self.scripted.exec(["python", "-m", "cohbench.scripted", "admin", *args], input_text=stdin, check=False, timeout=60)
        if proc.returncode != 0:
            raise DockerError(f"scripted admin {args[0]} failed: {proc.stdout.strip()[:800]} {proc.stderr.strip()[:800]}")
        return proc.stdout

    def load_script(self, script: dict) -> None:
        self._admin(["load", "-"], json.dumps(script))

    def export_state(self) -> dict:
        """The scripted server's whole episode (`admin export`)."""
        return json.loads(self._admin(["export"]))

    def import_state(self, state: dict, next_script: dict | str | None = None) -> dict:
        """Seed a server that has answered nothing: `POST /import` on its admin
        listener, from inside its own container (the listener binds there)."""
        if self.scripted is None:
            raise RuntimeError("this site has the real upstream; there is no scripted server")
        body: dict[str, Any] = {"state": state}
        if next_script is not None:
            # The server takes the script as JSON text, passed through unparsed.
            body["next_script"] = next_script if isinstance(next_script, str) else json.dumps(next_script)
        prog = (
            "import sys,urllib.request,urllib.error\n"
            "req=urllib.request.Request('http://127.0.0.1:8081/import',data=sys.stdin.buffer.read(),headers={'content-type':'application/json'},method='POST')\n"
            "try:\n r=urllib.request.urlopen(req,timeout=60); sys.stdout.write(r.read().decode()); sys.exit(0)\n"
            "except urllib.error.HTTPError as e:\n sys.stdout.write(e.read().decode()); sys.exit(1)\n"
        )
        proc = self.scripted.exec(["python", "-c", prog], input_text=json.dumps(body), check=False, timeout=120)
        if proc.returncode != 0:
            raise DockerError(f"scripted import refused: {proc.stdout.strip()[:800]} {proc.stderr.strip()[:400]}")
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"raw": proc.stdout[:400]}

    def scripted_state(self) -> dict:
        return json.loads(self._admin(["state"]))

    def scripted_requests(self) -> list[dict]:
        """The scripted server's request log, read from the host side of its
        bind mount: a test and diagnosis helper (on native Linux the host user
        may not read it; the export copies it through Docker instead)."""
        if self.scripted_log is None:
            return []
        path = self.scripted_log / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def scripted_system_prompts(self) -> dict[str, str]:
        if self.scripted_log is None:
            return {}
        return {p.name: p.read_text() for p in sorted(self.scripted_log.glob("system-*.txt"))}

    def gateway_log(self) -> Path:
        """The host side of the gateway's log bind mount. Written by the
        gateway's uid 10001; on native Linux the host user may not read it,
        so nothing in this project reads it there: see `usage` and
        `copy_logs`."""
        return self.log_dir

    def usage(self) -> dict:
        """`python -m gateway summary --episode`, run by the gateway itself
        inside its (running) container, so it reads its own files."""
        proc = self.gateway.exec(["sh", "-c", 'test -e "$0" || { echo NOLOG; exit 0; }; exec python -m gateway summary "$0" --episode', f"{GATEWAY_LOG}/requests.jsonl"],
                                 check=False, timeout=60)
        if proc.returncode != 0:
            raise DockerError(f"gateway summary failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        if proc.stdout.strip() == "NOLOG":
            from .environment import gateway_episode_usage

            return gateway_episode_usage(Path("/nonexistent"))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def copy_logs(self, role: str, dest: Path, keys: list) -> dict:
        """A service's log directory (role `gateway` or `scripted`), copied
        out of its container through Docker into memory, scanned for every
        key, then unpacked under `dest` (created mode 0700). Never reads the
        host side of the bind mount. Raises KeyLeak on a hit (nothing
        written) and DockerError when the copy fails."""
        c, path = (self.gateway, GATEWAY_LOG) if role == "gateway" else (self.scripted, SCRIPTED_LOG)
        if c is None:
            raise DockerError(f"this site has no {role}")
        data, err = copy_bytes(c.name, path)
        if data is None:
            raise DockerError(f"the {role} log was not copied: {err}")
        for k in keys:
            if k is not None and key_hits(data, k):
                raise KeyLeak(f"the {role} log held the key; nothing written")
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        res = extract(data, dest)
        if not res["ok"]:
            raise DockerError(f"the {role} log could not be unpacked: {res.get('error')}")
        # docker cp of a directory unpacks as <dest>/<basename>.
        inner = dest / PurePosixPath(path).name
        return {"dir": inner if inner.is_dir() else dest, "bytes": len(data)}


def _host_dir(path: Path) -> Path:
    """A host directory a container user other than ours can write to."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)
    return path.resolve()


def _gateway_config(provider: Provider, upstream_url: str, *, http_upstream: bool) -> dict:
    cfg = json.loads((REPO_ROOT / "gateway" / "config.json").read_text())
    cfg["routes"] = [r for r in cfg["routes"] if r["name"] == provider.name]
    if len(cfg["routes"]) != 1:
        raise ValueError(f"gateway/config.json has no route {provider.name!r}")
    cfg["routes"][0]["upstream"] = upstream_url
    cfg["routes"][0]["credential"]["key_file"] = GATEWAY_KEY
    cfg["insecure_allow_http_upstream"] = http_upstream
    cfg["store_bodies"] = True
    return cfg


def start_site(label: str, upstream: Upstream, out_dir: Path, registry: Registry, *, gateway_image: str, scripted_image: str) -> Site:
    """Create the networks, the upstream and the gateway; return once the
    gateway's CA exists. Every name is registered before creation."""
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    agent_net = registry.add("network", opaque_name())
    names["agent_network"] = agent_net
    net = checks.create_agent_network(agent_net, registry)
    up_net = registry.add("network", opaque_name())
    names["upstream_network"] = up_net
    if upstream.kind == "scripted":
        run(["network", "create", "--internal", up_net], timeout=120)
    else:
        run(["network", "create", up_net], timeout=120)

    scripted = None
    scripted_log = None
    if upstream.kind == "scripted":
        sname = registry.add("container", opaque_name())
        names["scripted_server"] = sname
        scripted_log = _host_dir(out_dir / "scripted")
        run(["run", "-d", "--name", sname, "--network", up_net, "--read-only", "--tmpfs", "/tmp:rw,size=16m",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "-v", f"{scripted_log}:/var/log/scripted", scripted_image], timeout=120)
        scripted = Container(sname, scripted_image)
        upstream_url = f"http://{sname}:8080"
    else:
        upstream_url = upstream.provider.upstream_url

    gdir = out_dir / "gateway"
    log_dir = _host_dir(gdir / "log")
    cfg_path = gdir / "config.json"
    cfg_path.write_text(json.dumps(_gateway_config(upstream.provider, upstream_url, http_upstream=upstream.kind == "scripted"), indent=1))
    os.chmod(cfg_path, 0o644)
    if upstream.kind == "real":
        assert upstream.key is not None
        secret = upstream.key.secret
    else:
        secret = upstream.provider.placeholder()
    gw = registry.add("container", opaque_name())
    names["gateway"] = gw
    run(["create", "--name", gw, "--network", up_net,
         "-v", f"{log_dir}:/var/log/gateway",
         "-v", f"{cfg_path.resolve()}:/etc/gw/config.json:ro",
         "-e", "GATEWAY_CONFIG=/etc/gw/config.json",
         gateway_image], timeout=120)
    # The credential goes into the created container as a file the
    # gateway's own uid owns, mode 0400, from memory: readable by the
    # gateway whatever the host user and uid mapping, and no copy of it
    # is left on the host. With the real upstream these are exactly the
    # snapshot's bytes, the ones every scan uses.
    cp_in(gw, str(PurePosixPath(GATEWAY_KEY).parent), one_file_tar(PurePosixPath(GATEWAY_KEY).name, secret.encode(), mode=0o400, uid=GATEWAY_UID, gid=GATEWAY_UID))
    run(["start", gw], timeout=120)
    gateway = Container(gw, gateway_image)
    run(["network", "connect", "--ip", net["gateway_ip"], agent_net, gw], timeout=120)

    deadline = time.monotonic() + 45
    ca = b""
    while True:
        res = copy_out(gw, GATEWAY_CA, gdir / "ca", key=upstream.key, timeout=max(0.1, deadline - time.monotonic()))
        if res.get("key_hits"):
            raise KeyLeak("the gateway's CA certificate held the key")
        if res["ok"] and res["files"] and res["files"][0].stat().st_size > 0:
            ca = res["files"][0].read_bytes()
            break
        if time.monotonic() > deadline or deadline_passed():
            raise DockerError(f"gateway wrote no CA certificate: {res['error']}\n{gateway.logs()[-1500:]}")
        time.sleep(max(0.0, min(0.5, deadline - time.monotonic())))
    site = Site(label, upstream, agent_net, up_net, gateway, net["gateway_ip"], net, ca, log_dir, scripted, scripted_log, names)
    if scripted is not None:
        _wait_scripted(site)
        if upstream.seed_state is not None:
            site.import_state(upstream.seed_state, upstream.next_script)
        elif upstream.script is not None:
            site.load_script(upstream.script)
    return site


def _wait_scripted(site: Site, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            site.scripted_state()
            return
        except DockerError as exc:
            if deadline_passed():
                raise
            last = str(exc)
        time.sleep(max(0.0, min(0.5, deadline - time.monotonic())))
    raise DockerError(f"scripted server did not answer its admin interface: {last}")


