"""Docker operations, the gateway topology, and everything that touches the API key.

The real key exists only in the gateway container (gateway/, D-005, D-007).
Each harness container sits on an internal Docker network whose one peer is
its own gateway. It resolves api.anthropic.com to the gateway through an
`--add-host` entry, trusts the gateway's per-start CA through
NODE_EXTRA_CA_CERTS, and holds a random placeholder key shaped like a real
one. The gateway drops the client's credential and adds the real one.

Everything here that reads the real key is in this module: `load_key`, the
gateway's key mount, and the scanners. The scanners stream bytes (a
`docker export`, a `docker save`, a `docker cp` tar, an evidence file) and
look for the key's bytes across chunk boundaries. The key is never put in
argv or env.

Everything here shells out to the `docker` CLI so the package needs nothing
beyond the standard library.
"""

from __future__ import annotations

import tempfile
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"

DEFAULT_T3_VERSION = "0.1.12-nightly.20261121.5051"
DEFAULT_NODE_IMAGE = "node:24.22.3-bookworm-slim"
HARNESS_BUILD_ARGS = {
    "pi": {"PI_VERSION": "1.6.0"},
    "claude": {"CLAUDE_CODE_VERSION": "2.1.330"},
}

# Inside the harness container. Neutral names only.
CONTAINER_HOSTNAME = "workstation"
WORKSPACE = "/work/project"
T3_BASE_DIR = "/home/node/.t3"
T3_PORT = 3773
CA_PATH_IN_HARNESS = "/usr/local/share/ca-certificates/local-ca.crt"
API_HOST = "api.anthropic.com"

# Inside the gateway container.
GATEWAY_KEY_PATH = "/run/secrets/anthropic_key"
GATEWAY_CA = "/run/gateway-tls/local-ca.crt"
GATEWAY_LOG = "/var/log/gateway"

# Host-side names. These never reach the inside of a harness container,
# except the gateway's container name, which its embedded DNS answers.
CONTAINER_PREFIX = "t3drv"
SNAPSHOT_REPO = "t3drv-snapshot"
GATEWAY_REPO = "t3drv-gateway"


class DockerError(RuntimeError):
    pass


class KeyLeak(RuntimeError):
    """The key was found where it must not be. The message never holds the key."""


def run(args: list[str], *, input_text: str | None = None, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc


# ---------------------------------------------------------------------------
# The key and the scanners
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySource:
    path: Path
    secret: str = field(repr=False)

    def __repr__(self) -> str:  # never print the key
        return f"KeySource(path={str(self.path)!r})"


def load_key(flag_value: str | None) -> KeySource:
    """Resolve and validate the key file. Refuses paths inside the repository,
    unreadable files, and files whose stripped content is empty or multi-line."""
    raw = flag_value or os.environ.get("DRIVER_PROOF_KEY_FILE")
    if not raw:
        raise SystemExit("Set DRIVER_PROOF_KEY_FILE or pass --key-file: a file holding the Anthropic API key.")
    path = Path(raw).expanduser().resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise SystemExit(f"key file {path} is inside the repository; keep it outside {REPO_ROOT}")
    if not path.is_file():
        raise SystemExit(f"key file {path} does not exist or is not a regular file")
    try:
        secret = path.read_text().strip()
    except OSError as exc:
        raise SystemExit(f"key file {path} is not readable: {exc.strerror}") from None
    if not secret:
        raise SystemExit(f"key file {path} is empty")
    if any(ch in secret for ch in "\r\n\t "):
        raise SystemExit(f"key file {path} must hold the key alone on one line")
    return KeySource(path, secret)


def placeholder_key() -> str:
    """A random key shaped like an Anthropic API key. The gateway replaces it."""
    return "sk-ant-api03-" + secrets.token_urlsafe(70)[:93] + "AA"


class StreamScanner:
    """Counts occurrences of needles in a byte stream fed in chunks."""

    def __init__(self, needles: dict[str, bytes]):
        self.needles = {k: v for k, v in needles.items() if v}
        self.keep = max((len(v) for v in self.needles.values()), default=1) - 1
        self.tail = b""
        self.hits = {k: 0 for k in self.needles}
        self.bytes = 0

    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        data = self.tail + chunk
        for name, needle in self.needles.items():
            start = 0
            while (i := data.find(needle, start)) >= 0:
                # A match lying wholly inside the kept tail was counted already.
                if i + len(needle) > len(self.tail):
                    self.hits[name] += 1
                start = i + 1
        self.tail = data[-self.keep :] if self.keep else b""


def scan_command(args: list[str], needles: dict[str, bytes], *, timeout: float = 1800) -> dict:
    """Stream `docker <args>` stdout through a scanner. Never stores the stream."""
    scanner = StreamScanner(needles)
    started = time.time()
    proc = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    while chunk := proc.stdout.read(1 << 20):
        scanner.feed(chunk)
        if time.time() - started > timeout:
            proc.kill()
            break
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    code = proc.wait()
    return {"command": " ".join(args[:2]), "exit": code, "stderr": err.strip()[:300], "bytes": scanner.bytes, "hits": scanner.hits, "seconds": round(time.time() - started, 1)}


def scan_container_export(name: str, key: KeySource, extra: dict[str, bytes] | None = None) -> dict:
    """The whole filesystem of a container, through `docker export`."""
    return scan_command(["export", name], {"key": key.secret.encode(), **(extra or {})})


def scan_image_save(image: str, key: KeySource) -> dict:
    """Every layer of an image: `docker save` read as a tar stream, each member's
    bytes scanned, gzip members decompressed into the scanner. A member in an
    encoding the standard library cannot decode (zstd) makes the scan incomplete."""
    import zlib

    started = time.time()
    # stderr goes to a temporary file: an unread pipe could fill and stall the
    # producer while this reads stdout.
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=errfile)
    assert proc.stdout is not None
    hits = 0
    members = gz = raw_bytes = decoded_bytes = 0
    undecodable: list[str] = []
    error = None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                members += 1
                f = tar.extractfile(member)
                if f is None:
                    continue
                scanner = StreamScanner({"key": key.secret.encode()})
                head = f.read(4)
                raw_bytes += len(head)
                if head[:2] == b"\x1f\x8b":
                    gz += 1
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    chunk = head
                    while True:
                        out = d.decompress(chunk)
                        decoded_bytes += len(out)
                        scanner.feed(out)
                        if d.eof and d.unused_data:
                            # Concatenated gzip members.
                            rest = d.unused_data
                            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                            chunk = rest
                            continue
                        chunk = f.read(1 << 20)
                        if not chunk:
                            tail = d.flush()
                            decoded_bytes += len(tail)
                            scanner.feed(tail)
                            if not d.eof:
                                undecodable.append(f"{member.name}: gzip stream ended early")
                            break
                        raw_bytes += len(chunk)
                elif head == b"\x28\xb5\x2f\xfd":
                    undecodable.append(f"{member.name}: zstd")
                    while chunk := f.read(1 << 20):
                        raw_bytes += len(chunk)
                else:
                    scanner.feed(head)
                    decoded_bytes += len(head)
                    while chunk := f.read(1 << 20):
                        raw_bytes += len(chunk)
                        decoded_bytes += len(chunk)
                        scanner.feed(chunk)
                hits += scanner.hits["key"]
    except BaseException as exc:
        # Stop `docker save` before waiting on it: a writer blocked on a full
        # pipe that nobody reads would never exit.
        proc.kill()
        if not isinstance(exc, Exception):
            proc.wait()
            raise
        error = f"{type(exc).__name__}: {exc}"
    finally:
        proc.stdout.close()
    code = proc.wait()
    errfile.seek(0)
    err = errfile.read(4096).decode(errors="replace")
    errfile.close()
    return {
        "command": f"save {image}",
        "exit": code,
        "stderr": err.strip()[:300],
        "error": error,
        "members": members,
        "gzip_members": gz,
        "undecodable": undecodable,
        "bytes": raw_bytes,
        "decoded_bytes": decoded_bytes,
        "hits": {"key": hits},
        "seconds": round(time.time() - started, 1),
    }


def scan_ok(result: dict, name: str = "key") -> bool:
    """A scan passes only if it completed, read bytes, decoded everything, and found nothing."""
    return result["exit"] == 0 and result["bytes"] > 0 and not result.get("error") and not result.get("undecodable") and result["hits"].get(name, 0) == 0


def key_in_inspect(target: str, key: KeySource, *, kind: str = "container") -> bool:
    return key.secret in run([kind, "inspect", target]).stdout


def key_in_image_history(image: str, key: KeySource) -> bool:
    return key.secret in run(["history", "--no-trunc", image]).stdout


def text_has_key(text: str | bytes, key: KeySource) -> bool:
    needle = key.secret.encode() if isinstance(text, bytes) else key.secret
    return needle in text


def scan_host_tree_for_key(root: Path, key: KeySource) -> dict:
    """Files under root holding the key. Read errors are reported, not skipped."""
    hits: list[str] = []
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            scanner = StreamScanner({"key": key.secret.encode()})
            with path.open("rb") as f:
                while chunk := f.read(1 << 20):
                    scanner.feed(chunk)
            if scanner.hits["key"]:
                hits.append(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror}")
    return {"files": hits, "errors": errors}


def copy_out_scanned(container: str, src: str, dest_dir: Path, key: KeySource) -> dict:
    """`docker cp container:src -` into memory, scan the tar, and only then
    extract it under dest_dir. Nothing reaches disk if the key is in it."""
    proc = subprocess.run(["docker", "cp", f"{container}:{src}", "-"], capture_output=True)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode(errors="replace").strip()[:300], "files": []}
    scanner = StreamScanner({"key": key.secret.encode()})
    scanner.feed(proc.stdout)
    if scanner.hits["key"]:
        return {"ok": False, "error": "the key was in the copied bytes; nothing written", "key_hits": scanner.hits["key"], "files": []}
    dest_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar.getmembers():
            target = (dest_dir / member.name).resolve()
            if dest_dir.resolve() not in target.parents and target != dest_dir.resolve():
                return {"ok": False, "error": f"unsafe path in tar: {member.name}", "files": files}
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                target.write_bytes(fobj.read() if fobj else b"")
                files.append(target)
    return {"ok": True, "error": None, "files": files, "bytes": scanner.bytes}


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def image_tag(harness: str, t3_version: str) -> str:
    return f"t3-harness-{harness}:{t3_version}"


def base_tag(t3_version: str) -> str:
    return f"t3-harness-base:{t3_version}"


def image_exists(tag: str) -> bool:
    """True if present, False if Docker says it is absent; raises on any other error."""
    proc = run(["image", "inspect", tag], check=False, timeout=60)
    if proc.returncode == 0:
        return True
    if "No such image" in proc.stderr or "not found" in proc.stderr.lower():
        return False
    raise DockerError(f"docker image inspect {tag} failed: {proc.stderr.strip()[:500]}")


def build_images(harness: str, t3_version: str, *, rebuild_base: bool = False, rebuild_harness: bool = False) -> dict[str, str]:
    base = base_tag(t3_version)
    tag = image_tag(harness, t3_version)
    built = []
    if rebuild_base or not image_exists(base):
        _build(DOCKER_DIR / "base/Dockerfile", base, {"T3_VERSION": t3_version, "NODE_IMAGE": DEFAULT_NODE_IMAGE}, DOCKER_DIR)
        built.append(base)
    if rebuild_harness or base in built or not image_exists(tag):
        _build(DOCKER_DIR / f"{harness}/Dockerfile", tag, {"BASE_IMAGE": base, **HARNESS_BUILD_ARGS[harness]}, DOCKER_DIR)
        built.append(tag)
    return {"base": base, "harness": tag, "built": built}


def build_gateway(run_id: str) -> str:
    """Build the gateway image once for a run, from the repository root. Returns its id."""
    tag = f"{GATEWAY_REPO}:{run_id.lower()}"
    _build(DOCKER_DIR / "gateway/Dockerfile", tag, {}, REPO_ROOT)
    return image_info(tag)["id"]


def _build(dockerfile: Path, tag: str, build_args: dict[str, str], context: Path) -> None:
    args = ["build", "-f", str(dockerfile), "-t", tag]
    for key, value in build_args.items():
        args += ["--build-arg", f"{key}={value}"]
    args.append(str(context))
    proc = subprocess.run(["docker", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise DockerError(f"build of {tag} failed:\n{proc.stderr[-4000:]}")


def image_info(tag: str) -> dict:
    data = json.loads(run(["image", "inspect", tag]).stdout)[0]
    return {"tag": tag, "id": data["Id"], "created": data.get("Created"), "size_bytes": data.get("Size"), "env_names": [e.split("=", 1)[0] for e in data.get("Config", {}).get("Env", [])]}


def base_image_digest() -> str | None:
    proc = run(["image", "inspect", DEFAULT_NODE_IMAGE, "--format", "{{json .RepoDigests}}"], check=False)
    if proc.returncode != 0:
        return None
    digests = json.loads(proc.stdout or "[]")
    return digests[0] if digests else None


# ---------------------------------------------------------------------------
# Containers and the topology
# ---------------------------------------------------------------------------


@dataclass
class Container:
    name: str
    image: str
    started_at: float = field(default_factory=time.time)

    def exec(self, cmd: list[str], *, input_text: str | None = None, check: bool = True, timeout: float | None = 600, user: str | None = None, detach: bool = False) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if detach:
            args.append("-d")
        if input_text is not None:
            args.append("-i")
        if user is not None:
            args += ["-u", user]
        args += [self.name, *cmd]
        return run(args, input_text=input_text, check=check, timeout=timeout)

    def sh(self, script: str, *, check: bool = True, timeout: float | None = 600, user: str | None = None) -> str:
        return self.exec(["sh", "-c", script], check=check, timeout=timeout, user=user).stdout

    def logs(self) -> str:
        proc = run(["logs", self.name], check=False)
        return proc.stdout + proc.stderr

    def inspect(self) -> dict:
        return json.loads(run(["inspect", self.name]).stdout)[0]


@dataclass
class Topology:
    """One harness container's network: an internal network, its gateway, and an egress network."""

    label: str
    internal: str
    egress: str
    subnet: str
    gateway_ip: str
    gateway: Container
    log_dir: Path
    ca_file: Path
    placeholder: str = field(repr=False)
    removed: dict = field(default_factory=dict)
    bridge_ip: str = ""
    bridge: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)


FLUSH_HELPER_IMAGE = "alpine:3.20"
_NETWORK_MODE: dict | None = None


def network_mode() -> dict:
    """How internal networks lose their host-side bridge address on this Docker.

    Engine 28.0 or later: `com.docker.network.bridge.gateway_mode_ipv4=isolated`.
    Before 28.0 that option is rejected, so the network is created plainly and
    the bridge's address is flushed before any harness container starts
    (gateway/README.md, "Docker before 28.0"). On Docker Desktop the flush runs
    in a pinned, privileged helper container on the VM's host network; on a
    Linux host it runs with `sudo -n ip addr flush`."""
    global _NETWORK_MODE
    if _NETWORK_MODE is None:
        version = run(["version", "--format", "{{.Server.Version}}"]).stdout.strip()
        major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else 0
        desktop = "Docker Desktop" in run(["info", "--format", "{{.OperatingSystem}}"], check=False).stdout
        _NETWORK_MODE = {
            "docker_server": version,
            "path": "gateway_mode_ipv4=isolated" if major >= 28 else "flush bridge address",
            "flush_via": None if major >= 28 else ("privileged helper " + FLUSH_HELPER_IMAGE if desktop else "sudo -n sh -c 'sysctl ... disable_ipv6=1 && ip addr flush'"),
            "steps": None if major >= 28 else ["sysctl -qw net.ipv6.conf.<bridge>.disable_ipv6=1", "ip addr flush dev <bridge>"],
            "helper_image_present_before": image_exists(FLUSH_HELPER_IMAGE) if major < 28 and desktop else None,
        }
    return _NETWORK_MODE


def _flush_bridge(network: str) -> dict:
    net_id = run(["network", "inspect", "-f", "{{.Id}}", network]).stdout.strip()
    bridge = "br-" + net_id[:12]
    mode = network_mode()
    # IPv6 off first: a flush alone leaves the kernel to give the bridge an
    # IPv6 link-local address again when the first container attaches.
    script = f"sysctl -qw net.ipv6.conf.{bridge}.disable_ipv6=1 && ip addr flush dev {bridge}"
    if mode["flush_via"] and mode["flush_via"].startswith("privileged helper"):
        proc = run(["run", "--rm", "--privileged", "--net=host", FLUSH_HELPER_IMAGE, "sh", "-c", script], check=False, timeout=180)
    else:
        proc = subprocess.run(["sudo", "-n", "sh", "-c", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise DockerError(f"could not flush {bridge}: {proc.stderr.strip()[:300]}")
    return {"bridge": bridge, "flushed": True}


def opaque_name() -> str:
    """A random name with no meaning. The agent can resolve container names and
    reverse-resolve its own address to <container>.<network> through Docker's
    embedded DNS, so every harness container, gateway and network gets one.
    The mapping to labels is kept host-side in the evidence."""
    return "n" + secrets.token_hex(6)


def _create_internal_network(name: str) -> tuple[str, str, str, dict]:
    mode = network_mode()
    for _ in range(40):
        n = secrets.randbelow(150) + 100
        subnet = f"172.31.{n}.0/24"
        args = ["network", "create", "--internal", "--subnet", subnet]
        if mode["path"] == "gateway_mode_ipv4=isolated":
            args += ["-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated"]
        proc = run([*args, name], check=False)
        if proc.returncode == 0:
            info = {"mode": mode["path"]}
            if mode["path"] != "gateway_mode_ipv4=isolated":
                info.update(_flush_bridge(name))
            return subnet, f"172.31.{n}.10", f"172.31.{n}.1", info
        if "overlap" not in proc.stderr.lower():
            raise DockerError(f"network create {name}: {proc.stderr.strip()[:300]}")
    raise DockerError("no free 172.31.x.0/24 subnet")


def start_topology(label: str, gateway_image: str, key: KeySource, log_dir: Path, cleanup: list[tuple[str, str]]) -> Topology:
    """Networks and a started gateway with body storage on, its CA copied to the host.

    Every resource name is appended to `cleanup` before Docker is asked to
    create it, so a failure at any point leaves a complete list to remove."""
    internal, egress, gw_name = opaque_name(), opaque_name(), opaque_name()
    log_dir = log_dir.resolve()
    cleanup.append(("network", internal))
    subnet, gw_ip, bridge_ip, bridge_info = _create_internal_network(internal)
    cleanup.append(("network", egress))
    run(["network", "create", egress])
    log_dir.mkdir(parents=True, exist_ok=True)
    cleanup.insert(0, ("container", gw_name))
    run([
        "run", "-d", "--name", gw_name, "--network", egress,
        "-v", f"{key.path}:{GATEWAY_KEY_PATH}:ro",
        "-v", f"{log_dir}:{GATEWAY_LOG}",
        "-e", "GATEWAY_STORE_BODIES=1",
        gateway_image,
    ])
    gateway = Container(gw_name, gateway_image)
    run(["network", "connect", "--ip", gw_ip, internal, gw_name])
    # The CA certificate is screened in memory before it is written (copy_out_scanned).
    ca_dir = log_dir.parent / f"{label}-ca"
    deadline = time.time() + 30
    while True:
        res = copy_out_scanned(gw_name, GATEWAY_CA, ca_dir, key)
        if res["ok"] and res["files"] and res["files"][0].stat().st_size > 0:
            ca_file = res["files"][0]
            break
        if res.get("key_hits"):
            raise KeyLeak("the gateway's CA certificate held the key")
        if time.time() > deadline:
            raise DockerError(f"gateway {gw_name} wrote no CA certificate: {res['error']}\n{gateway.logs()[-1500:]}")
        time.sleep(0.5)
    names = {"internal_network": internal, "egress_network": egress, "gateway_container": gw_name}
    return Topology(label, internal, egress, subnet, gw_ip, gateway, log_dir, ca_file, placeholder_key(), bridge_ip=bridge_ip, bridge=bridge_info, names=names)


def remove_registered(cleanup: list[tuple[str, str]]) -> list[dict]:
    """Remove every registered resource, containers first, then images, then
    networks. Each is removed independently: an error or timeout on one is
    recorded and the loop goes on. Each removal is confirmed and retried once."""
    out = []
    order = {"container": 0, "image": 1, "network": 2}
    for kind, name in sorted(cleanup, key=lambda x: order.get(x[0], 3)):
        res: dict = {kind: name, "confirmed_absent": False}
        for attempt in range(2):
            try:
                res = {"container": remove_container, "network": remove_network, "image": remove_image}[kind](name)
            except Exception as exc:  # recorded; the other resources are still removed
                res = {kind: name, "confirmed_absent": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            res["attempt"] = attempt + 1
            if res["confirmed_absent"]:
                break
            time.sleep(2)
        out.append(res)
    return out


def _ca_tar(ca_file: Path) -> bytes:
    """The CA certificate as a one-file tar owned by root, so nothing of the
    host (its user id, its path) reaches the harness container."""
    data = ca_file.read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(Path(CA_PATH_IN_HARNESS).name)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, 0, 0, 0
        info.uname = info.gname = "root"
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def start_harness(image: str, topo: Topology | None, *, label: str, cleanup: list[tuple[str, str]], workspace_tar: bytes | None = None) -> Container:
    """Create a harness container, copy files in, then start it. No bind mounts.

    On the topology's internal network: the CA certificate is copied in with
    `docker cp`, and a workspace tar (a handoff) replaces /work/project before
    the first start. The name is registered before creation, and any
    exception, a signal-driven SystemExit included, removes what was created."""
    name = opaque_name()
    cleanup.insert(0, ("container", name))
    args = ["create", "--init", "--name", name, "--hostname", CONTAINER_HOSTNAME]
    if topo is None:
        args += ["--network", "none"]
    else:
        args += [
            "--network", topo.internal,
            "--add-host", f"{API_HOST}:{topo.gateway_ip}",
            "-e", f"NODE_EXTRA_CA_CERTS={CA_PATH_IN_HARNESS}",
            "-e", f"ANTHROPIC_API_KEY={topo.placeholder}",
        ]
    try:
        run([*args, image])
        if topo is not None:
            _cp_in(name, str(Path(CA_PATH_IN_HARNESS).parent), _ca_tar(topo.ca_file))
        if workspace_tar is not None:
            # Staged in /tmp/.w; the entrypoint replaces /work/project with it
            # before T3 starts, so the image's own workspace does not merge in.
            _cp_in(name, "/tmp", _staged(workspace_tar))
        run(["start", name])
    except BaseException:
        remove_container(name)
        raise
    return Container(name=name, image=image)


STAGE_ROOT = ".w"


def _staged(workspace_tar: bytes) -> bytes:
    """The workspace tar re-rooted under .w/, with .w owned by the workspace user.

    A PAX `path` or `linkpath` header overrides the member's name when the
    archive is read, so both are dropped and tarfile writes new ones from the
    new name where it needs them (non-ASCII names, long components, long paths).
    The result is read back and checked before use (`verify_staged`)."""
    with tarfile.open(fileobj=io.BytesIO(workspace_tar)) as src:
        members = src.getmembers()
        top = next((m for m in members if m.isdir() and "/" not in m.name.strip("/")), None)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as out:
            w = tarfile.TarInfo(STAGE_ROOT)
            w.type, w.mode = tarfile.DIRTYPE, 0o755
            w.uid, w.gid = (top.uid, top.gid) if top else (1000, 1000)
            w.uname, w.gname = (top.uname, top.gname) if top else ("node", "node")
            w.mtime = int(time.time())
            out.addfile(w)
            for m in members:
                data = src.extractfile(m) if m.isfile() else None
                m.pax_headers = {k: v for k, v in m.pax_headers.items() if k not in ("path", "linkpath")}
                m.name = f"{STAGE_ROOT}/{m.name}"
                if m.islnk():
                    m.linkname = f"{STAGE_ROOT}/{m.linkname}"
                out.addfile(m, data)
    staged = buf.getvalue()
    problems = verify_staged(staged, len(members))
    if problems:
        raise DockerError(f"staged workspace archive failed its check: {problems[:5]}")
    return staged


def verify_staged(staged: bytes, expected_members: int) -> list[str]:
    """Every member path, and every hard-link target, is under the staging
    root, relative, with no `..`; the member count is the source's plus the root."""
    problems = []
    with tarfile.open(fileobj=io.BytesIO(staged)) as tar:
        members = tar.getmembers()
    if len(members) != expected_members + 1:
        problems.append(f"{len(members)} members, expected {expected_members + 1}")

    def inside(path: str) -> bool:
        parts = path.split("/")
        return not path.startswith("/") and ".." not in parts and (path == STAGE_ROOT or path.startswith(STAGE_ROOT + "/"))

    for m in members:
        if not inside(m.name):
            problems.append(f"member outside {STAGE_ROOT}: {m.name!r}")
        if m.islnk() and not inside(m.linkname):
            problems.append(f"hard-link target outside {STAGE_ROOT}: {m.name!r} -> {m.linkname!r}")
    return problems


def _cp_in(name: str, dest_dir: str, tar_bytes: bytes) -> None:
    proc = subprocess.run(["docker", "cp", "-a", "-", f"{name}:{dest_dir}"], input=tar_bytes, capture_output=True)
    if proc.returncode != 0:
        raise DockerError(f"docker cp into {name}:{dest_dir} failed: {proc.stderr.decode(errors='replace')[:300]}")


def workspace_tar(name: str, key: KeySource) -> bytes:
    """The workspace directory as a tar stream from `docker cp` (modes, owners and
    mtimes to the second), scanned for the key in memory."""
    proc = subprocess.run(["docker", "cp", f"{name}:{WORKSPACE}", "-"], capture_output=True)
    if proc.returncode != 0:
        raise DockerError(f"docker cp of the workspace from {name} failed: {proc.stderr.decode(errors='replace')[:300]}")
    scanner = StreamScanner({"key": key.secret.encode()})
    scanner.feed(proc.stdout)
    if scanner.hits["key"]:
        raise KeyLeak("the workspace held the key; handoff refused")
    return proc.stdout


_IDENTITY_JS = r"""
const dns=require('dns').promises;const os=require('os');const fs=require('fs');
(async()=>{
  const own=Object.values(os.networkInterfaces()).flat().filter(a=>a&&a.family==='IPv4'&&!a.internal).map(a=>a.address);
  const out={own,reverse:{}};
  for(const ip of [...own,REQ.gateway]){
    try{out.reverse[ip]=await dns.reverse(ip);}catch(e){out.reverse[ip]='error '+(e.code||String(e));}
  }
  out.mountinfo=fs.readFileSync('/proc/self/mountinfo','utf8').split('\n').filter(Boolean);
  process.stdout.write(JSON.stringify(out));
})();
"""

TELLING = __import__("re").compile(r"benchmark|coherency|\beval|\btest|probe|proof|snapshot|copy|idle|busy|t3drv|driver|harness|edge|gateway|nonet", __import__("re").IGNORECASE)


def identity_checks(harness: Container, topo: Topology) -> dict:
    """From inside the harness container: what reverse DNS says about its own
    address and the gateway's, and what /proc/self/mountinfo shows."""
    proc = harness.exec(["node", "-"], input_text="const REQ = " + json.dumps({"gateway": topo.gateway_ip}) + ";\n" + _IDENTITY_JS, check=False, timeout=60)
    if proc.returncode != 0:
        return {"ok_dns": False, "ok_mounts": False, "error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    names = [n for v in data["reverse"].values() if isinstance(v, list) for n in v]
    host_markers = [str(REPO_ROOT), "/Users/", "/home/berg", "runs/driver", "driver-proof", "/private/"]
    def named_fields(line: str) -> str:
        # root, mount point, and the source after the " - fstype " separator.
        left, _, right = line.partition(" - ")
        f, r = left.split(), right.split()
        return " ".join(f[3:5] + r[1:2])

    # Host paths anywhere in the line; telling words in what a mount names.
    # Docker Desktop's overlay options name its own VM paths
    # (/var/lib/desktop-containerd/...snapshotter...); those are Docker's.
    bad_mounts = [l for l in data["mountinfo"] if any(h in l for h in host_markers) or TELLING.search(named_fields(l))]
    return {
        "ok_dns": not any(TELLING.search(n) for n in names),
        "ok_mounts": not bad_mounts and bool(data["mountinfo"]),
        "reverse": data["reverse"],
        "bad_mount_lines": bad_mounts,
        "mount_sources": sorted({l.split()[3] for l in data["mountinfo"] if len(l.split()) > 4}),
    }


_BRIDGE_JS = r"""
const net=require('net');const fs=require('fs');
(async()=>{
  const out={};
  await Promise.all(REQ.ports.map(p=>new Promise(r=>{const s=net.connect({host:REQ.ip,port:p,timeout:REQ.timeoutMs});
      s.on('connect',()=>{s.destroy();out[p]='connected';r();});s.on('timeout',()=>{s.destroy();out[p]='timeout';r();});
      s.on('error',e=>{out[p]=e.code||String(e);r();});})));
  const arp=fs.readFileSync('/proc/net/arp','utf8').split('\n').filter(l=>l.startsWith(REQ.ip+' '));
  process.stdout.write(JSON.stringify({ports:out,arp}));
})();
"""


def bridge_unreachable(harness: Container, bridge_ip: str) -> dict:
    """From inside the harness container: the network's gateway address must not
    answer at all. Every port must give no route or no host (EHOSTUNREACH or
    ENETUNREACH); a refusal or a timeout fails. ARP must find no host there."""
    req = {"ip": bridge_ip, "ports": [22, 53, 80, 443, 2375, 3773, 8080], "timeoutMs": 8000}
    proc = harness.exec(["node", "-"], input_text="const REQ = " + json.dumps(req) + ";\n" + _BRIDGE_JS, check=False, timeout=120)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    no_host = all(v in ("EHOSTUNREACH", "ENETUNREACH") for v in data["ports"].values())
    arp_incomplete = all(len(l.split()) >= 4 and l.split()[2] == "0x0" for l in data["arp"])
    return {"ok": no_host and arp_incomplete, "bridge_ip": bridge_ip, **data, "ports_tested_are_a_sample": True}


def verify_topology(topo: Topology, harness: Container) -> dict:
    """`python -m gateway verify` from the repository root."""
    flag = [] if topo.bridge.get("mode") == "gateway_mode_ipv4=isolated" else ["--allow-flushed-bridge"]
    proc = subprocess.run([sys.executable, "-m", "gateway", "verify", *flag, topo.gateway.name, harness.name], cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
    lines = proc.stdout.strip().splitlines()
    return {"exit": proc.returncode, "flags": flag, "summary": lines[-1] if lines else "", "failures": [l for l in lines if l.startswith("FAIL")], "waived": [l for l in lines if l.startswith("WAIVED")], "output": proc.stdout[-8000:], "stderr": proc.stderr[-600:]}


def remove_container(name: str) -> dict:
    proc = run(["rm", "-f", name], check=False, timeout=120)
    gone = run(["container", "inspect", name], check=False, timeout=60)
    absent = gone.returncode != 0 and "No such" in gone.stderr
    return {"container": name, "rm_exit": proc.returncode, "rm_stderr": proc.stderr.strip()[:300], "confirmed_absent": absent}


def remove_network(name: str) -> dict:
    proc = run(["network", "rm", name], check=False, timeout=120)
    gone = run(["network", "inspect", name], check=False, timeout=60)
    absent = gone.returncode != 0 and ("No such" in gone.stderr or "not found" in gone.stderr)
    return {"network": name, "rm_exit": proc.returncode, "rm_stderr": proc.stderr.strip()[:300], "confirmed_absent": absent}


def remove_topology(topo: Topology) -> list[dict]:
    """Remove the gateway, then both networks. Each removal is confirmed; a failed one is retried once."""
    out = []
    for kind, target in (("container", topo.gateway.name), ("network", topo.internal), ("network", topo.egress)):
        if topo.removed.get(target):
            continue
        for attempt in range(2):
            res = remove_container(target) if kind == "container" else remove_network(target)
            res["attempt"] = attempt + 1
            if res["confirmed_absent"]:
                topo.removed[target] = True
                break
            time.sleep(2)
        out.append(res)
    return out


def commit_snapshot(container: Container, label: str, cleanup: list[tuple[str, str]]) -> str:
    """The tag is registered before the commit, so a commit that fails or is
    interrupted halfway still leaves it on the removal list."""
    tag = f"{SNAPSHOT_REPO}:{label}-{uuid.uuid4().hex[:8]}"
    cleanup.append(("image", tag))
    run(["commit", container.name, tag], timeout=600)
    return tag


def remove_image(tag: str) -> dict:
    proc = run(["rmi", "-f", tag], check=False, timeout=300)
    try:
        exists = image_exists(tag)
        err = None
    except DockerError as exc:
        exists, err = None, str(exc)
    return {"image": tag, "rmi_exit": proc.returncode, "rmi_stderr": proc.stderr.strip()[:300], "confirmed_absent": exists is False, "inspect_error": err}


def image_history(tag: str) -> str:
    return run(["history", "--no-trunc", tag]).stdout
