"""Networks without a host-side bridge address, and the checks every harness
container passes before a message is sent.

Two rules, kept apart:

- Everything this project creates around the agent (container, network and
  volume names, mounts, environment variable names and values, process
  arguments, reverse DNS, and the files it places outside the workspace) is
  checked against the broad `TELLING` pattern, here.
- The task workspace is the task's: it is checked once, on its manifest,
  before population, against a narrow pattern (cohbench/env/workspace.py).
  Nothing the agent writes is ever gated.

The container checks, each failing closed, run after T3 is ready and the
harness settings are written, on every segment and every probe copy:

- `python -m gateway verify` against the gateway and the harness container;
- the bridge address answers nothing from inside the container;
- reverse DNS of the container's addresses and the gateway's names nothing
  telling, and `/proc/self/mountinfo` holds no host path or telling name;
- no telling word in the hostname, any process's arguments, environment
  variable names or values;
- no telling word in the names under the harness's own directories, at any
  depth, nor in the names or the text of the files this project places in
  the container.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from .docker import Container, DockerError, bounded_subprocess, image_exists, run

REPO_ROOT = Path(__file__).resolve().parents[2]

# Words that would tell the agent it is being measured, or how. Applied to
# everything the agent can read about its own container.
TELLING = re.compile(
    r"benchmark|coherency|\beval|\btest|probe|proof|watch|monitor|measur|score|snapshot|scripted|cohbench|harness|driver|gateway",
    re.IGNORECASE,
)

FLUSH_HELPER_IMAGE = "alpine:3.20"
_MODE: dict | None = None


def network_mode() -> dict:
    """How internal networks lose their host-side bridge address on this Docker.

    Engine 28.0 or later: `com.docker.network.bridge.gateway_mode_ipv4=isolated`.
    Before 28.0 the network is created plainly, and before any container
    attaches, IPv6 is turned off on the bridge and its address is flushed
    (gateway/README.md, "Docker before 28.0"). On Docker Desktop that runs in
    a privileged helper container on the VM's host network."""
    global _MODE
    if _MODE is None:
        version = run(["version", "--format", "{{.Server.Version}}"], timeout=60).stdout.strip()
        major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else 0
        desktop = "Docker Desktop" in run(["info", "--format", "{{.OperatingSystem}}"], check=False, timeout=60).stdout
        _MODE = {
            "docker_server": version,
            "path": "gateway_mode_ipv4=isolated" if major >= 28 else "flush bridge address",
            "flush_via": None if major >= 28 else ("privileged helper " + FLUSH_HELPER_IMAGE if desktop else "sudo -n"),
            "helper_image_present_before": image_exists(FLUSH_HELPER_IMAGE) if major < 28 and desktop else None,
        }
    return _MODE


def _flush_bridge(network: str, registry) -> dict:
    net_id = run(["network", "inspect", "-f", "{{.Id}}", network], timeout=60).stdout.strip()
    bridge = "br-" + net_id[:12]
    script = f"sysctl -qw net.ipv6.conf.{bridge}.disable_ipv6=1 && ip addr flush dev {bridge}"
    mode = network_mode()
    if mode["flush_via"] and mode["flush_via"].startswith("privileged helper"):
        from .docker import opaque_name

        name = registry.add("container", opaque_name())
        try:
            proc = run(["run", "--rm", "--name", name, "--privileged", "--net=host", FLUSH_HELPER_IMAGE, "sh", "-c", script], check=False, timeout=180)
        finally:
            registry.remove("container", name)
    else:
        proc = bounded_subprocess(["sudo", "-n", "sh", "-c", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise DockerError(f"could not flush {bridge}: {proc.stderr.strip()[:300]}")
    return {"bridge": bridge, "flushed": True}


def create_agent_network(name: str, registry) -> dict:
    """An internal network whose bridge has no host-side address. Returns the
    subnet, the gateway container's address on it (.10) and the bridge's."""
    mode = network_mode()
    for _ in range(40):
        n = secrets.randbelow(150) + 100
        subnet = f"172.31.{n}.0/24"
        args = ["network", "create", "--internal", "--subnet", subnet]
        if mode["path"] == "gateway_mode_ipv4=isolated":
            args += ["-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated"]
        proc = run([*args, name], check=False, timeout=120)
        if proc.returncode == 0:
            info: dict[str, Any] = {"mode": mode["path"], "subnet": subnet, "gateway_ip": f"172.31.{n}.10", "bridge_ip": f"172.31.{n}.1"}
            if mode["path"] != "gateway_mode_ipv4=isolated":
                info.update(_flush_bridge(name, registry))
            return info
        if "overlap" not in proc.stderr.lower():
            raise DockerError(f"network create: {proc.stderr.strip()[:300]}")
    raise DockerError("no free 172.31.x.0/24 subnet")


def verify(gateway: str, harness: str, *, flushed: bool) -> dict:
    """`python -m gateway verify` from the repository root."""
    flag = ["--allow-flushed-bridge"] if flushed else []
    proc = bounded_subprocess([sys.executable, "-m", "gateway", "verify", *flag, gateway, harness], cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
    lines = proc.stdout.strip().splitlines()
    return {
        "ok": proc.returncode == 0 and not [line for line in lines if line.startswith("FAIL")],
        "exit": proc.returncode,
        "summary": lines[-1] if lines else "",
        "failures": [line for line in lines if line.startswith("FAIL")],
        "waived": [line for line in lines if line.startswith("WAIVED")],
        "output": proc.stdout[-8000:],
        "stderr": proc.stderr[-600:],
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


def node(c: Container, program: str, req: dict, *, timeout: float = 120) -> subprocess.CompletedProcess:
    """A one-shot `node -` with its program and data on stdin: nothing is
    written in the container and the process's argv is only `node -`."""
    return c.exec(["node", "-"], input_text="const REQ = " + json.dumps(req) + ";\n" + program, check=False, timeout=timeout)


def bridge_unreachable(c: Container, bridge_ip: str) -> dict:
    req = {"ip": bridge_ip, "ports": [22, 53, 80, 443, 2375, 3773, 8080], "timeoutMs": 8000}
    proc = node(c, _BRIDGE_JS, req)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    no_host = all(v in ("EHOSTUNREACH", "ENETUNREACH") for v in data["ports"].values())
    arp_incomplete = all(len(line.split()) >= 4 and line.split()[2] == "0x0" for line in data["arp"])
    return {"ok": no_host and arp_incomplete, "bridge_ip": bridge_ip, **data}


_IDENTITY_JS = r"""
const dns=require('dns').promises;const os=require('os');const fs=require('fs');
(async()=>{
  const own=Object.values(os.networkInterfaces()).flat().filter(a=>a&&a.family==='IPv4'&&!a.internal).map(a=>a.address);
  const out={own,reverse:{}};
  for(const ip of [...own,REQ.gateway]){
    try{out.reverse[ip]=await dns.reverse(ip);}catch(e){out.reverse[ip]='error '+(e.code||String(e));}
  }
  out.mountinfo=fs.readFileSync('/proc/self/mountinfo','utf8').split('\n').filter(Boolean);
  const procs=[];
  for(const pid of fs.readdirSync('/proc').filter(n=>/^\d+$/.test(n))){
    try{
      const argv=fs.readFileSync('/proc/'+pid+'/cmdline','utf8').split('\0').filter(Boolean);
      let env=[];try{env=fs.readFileSync('/proc/'+pid+'/environ','utf8').split('\0').filter(Boolean).map(e=>{const i=e.indexOf('=');return i<0?[e,'']:[e.slice(0,i),e.slice(i+1)];});}catch(e){env=null;}
      procs.push({pid,argv,env});
    }catch(e){}
  }
  out.procs=procs;
  out.hostname=os.hostname();
  process.stdout.write(JSON.stringify(out));
})();
"""


# T3 Code's own process names, not this project's: the pinned runtime ships a
# helper called `t3-resource-monitor`. Matched on the exact runtime path.
T3_OWN_ARGV = re.compile(r"^/home/node/\.t3/runtime/versions/[^/]+/resource-monitor/[^/]+/t3-resource-monitor$")


def host_markers() -> list[str]:
    home = str(Path.home())
    return [str(REPO_ROOT), "/Users/", home, "/private/", "runs/"]


def _named_fields(line: str) -> str:
    # root, mount point, and the source after the " - fstype " separator.
    left, _, right = line.partition(" - ")
    f, r = left.split(), right.split()
    return " ".join(f[3:5] + r[1:2])


def identity(c: Container, gateway_ip: str) -> dict:
    """From inside the container: reverse DNS, the mount table, process
    arguments and environment variable names, and the hostname."""
    proc = node(c, _IDENTITY_JS, {"gateway": gateway_ip}, timeout=60)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    names = [n for v in data["reverse"].values() if isinstance(v, list) for n in v]
    markers = host_markers()
    # Host paths anywhere in a mount line; telling words in what a mount names.
    # Docker Desktop's overlay options name its own VM paths, which are Docker's.
    bad_mounts = [line for line in data["mountinfo"] if any(h in line for h in markers) or TELLING.search(_named_fields(line))]
    def argv_text(p: dict) -> str:
        return " ".join(a for a in p["argv"] if not T3_OWN_ARGV.match(a))

    bad_args = [p for p in data["procs"] if TELLING.search(argv_text(p)) or any(h in " ".join(p["argv"]) for h in markers)]
    t3_own = sorted({a for p in data["procs"] for a in p["argv"] if T3_OWN_ARGV.match(a)})
    bad_env = sorted({k for p in data["procs"] for k, _ in (p["env"] or []) if TELLING.search(k)})
    bad_values = sorted({k for p in data["procs"] for k, v in (p["env"] or []) if TELLING.search(v) or any(h in v for h in markers)})
    unreadable_env = [p["pid"] for p in data["procs"] if p["env"] is None]
    result = {
        "ok_dns": not any(TELLING.search(n) for n in names),
        "ok_mounts": bool(data["mountinfo"]) and not bad_mounts,
        "ok_processes": bool(data["procs"]) and not bad_args and not bad_env and not bad_values and not unreadable_env,
        "ok_hostname": not TELLING.search(data["hostname"]),
        "reverse": data["reverse"],
        "bad_mount_lines": bad_mounts,
        "bad_process_args": bad_args[:10],
        "bad_env_names": bad_env,
        "env_names_with_bad_values": bad_values,
        "env_unreadable_pids": unreadable_env,
        "mount_points": sorted({line.split()[4] for line in data["mountinfo"] if len(line.split()) > 4}),
        "processes": len(data["procs"]),
        "t3_own_process_names_allowed": t3_own,
    }
    result["ok"] = result["ok_dns"] and result["ok_mounts"] and result["ok_processes"] and result["ok_hostname"]
    return result


def _scannable(text: str) -> str:
    """The words a file says. In a JSON settings file the keys are the
    schema of T3 or the harness (T3's settings name a `driver` per provider
    instance), so only the values are checked."""
    try:
        doc = json.loads(text)
    except ValueError:
        return text
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            out.append(v)

    walk(doc)
    return "\n".join(out)


def infrastructure_names(c: Container, dirs: list[str], files: list[str], text_files: list[str], *, baseline: frozenset[str] | None = None,
                         text_baseline: dict[str, str] | None = None) -> dict:
    """The names this project creates around the agent, outside the workspace:
    every name under `dirs` at any depth (the harness's own directories),
    with each symlink's target string, and the names of `files`; plus the
    text of `text_files` (the settings and scripts this project writes). A
    failed listing or read fails.

    With `baseline` (the `listing` of the same container's startup check),
    only entries present at startup are judged: what the agent wrote under
    those directories since is its own content and outside this gate.
    Returns `listing`, every entry seen, for use as a later baseline."""
    problems, bad, count = [], [], 0
    seen: set[str] = set()
    skipped = 0
    for root in dirs:
        if c.exec(["test", "-d", root], check=False).returncode != 0:
            continue
        # NUL-delimited, so a name holding a newline is one entry; a symlink's
        # target follows its name.
        r = c.exec(["find", root, "-printf", "%p\\0%l\\0"], check=False, timeout=300)
        if r.returncode != 0:
            problems.append(f"find {root} exited {r.returncode}: {r.stderr.strip()[:120]}")
        parts = r.stdout.split("\0")
        for i in range(0, len(parts) - 1, 2):
            entry, target = parts[i], parts[i + 1]
            seen.add(entry)
            if baseline is not None and entry not in baseline:
                skipped += 1
                continue
            count += 1
            if TELLING.search(entry) or (target and TELLING.search(target)):
                bad.append(entry if not target else f"{entry} -> {target}")
    for f in files:
        if TELLING.search(f):
            bad.append(f)
    texts_bad, texts, changed = [], {}, []
    for f in text_files:
        r = c.exec(["cat", f], check=False)
        if r.returncode != 0:
            if text_baseline is not None and f in text_baseline and c.exec(["test", "-e", f], check=False).returncode == 1:
                # Gone since start: the agent deleted it; its change, outside the gate.
                changed.append(f)
                continue
            problems.append(f"could not read {f}")
            continue
        texts[f] = r.stdout
        if text_baseline is not None and f in text_baseline:
            if r.stdout != text_baseline[f]:
                # The agent changed a settings file after start: its content,
                # outside this gate (the startup check passed the original).
                changed.append(f)
            continue
        text = _scannable(r.stdout)
        if TELLING.search(text) or any(h in text for h in host_markers()):
            texts_bad.append(f)
    return {"ok": not problems and not bad and not texts_bad, "problems": problems, "telling_names": bad[:20], "telling_text_in": texts_bad, "entries": count,
            "written_since_start": skipped if baseline is not None else None, "settings_changed_since_start": changed if text_baseline is not None else None,
            "dirs": dirs, "files": files + text_files, "listing": frozenset(seen), "texts": texts}
