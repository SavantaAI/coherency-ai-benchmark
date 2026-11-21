"""Check, from inside a running agent container, that the gateway is its only way out.

Runs on the host with the standard library and the docker CLI:

    python -m gateway verify <gateway container> <agent container>

The checks run inside the agent container with `docker exec`, using node or
python3, whichever the container has. They never send a request that the
gateway would forward upstream, so they cost nothing.

Asserted:
- every network of the agent container is internal, and its only peer on
  each of them is the gateway;
- every such network was created with no host address on its bridge
  (`com.docker.network.bridge.gateway_mode_ipv4=isolated`, Docker Engine 28.0
  and later), and from inside the agent container the bridge address does
  not answer at all: connecting to it fails with "no route to host" or
  "network unreachable", not with refused ports. A network with IPv6
  enabled must have both families isolated, and both are probed. On older
  Docker, where the option does not exist, `--allow-flushed-bridge` waives
  the option check for a bridge whose address was removed by hand; then the
  bridge interface on the Docker host (or VM) is also inspected with a
  short-lived host-network helper (`alpine:3.20`, `ip -o addr show dev`) and
  must have no address. The reachability assertion still applies;
- each of the gateway's active hosts (read from the gateway container)
  resolves, inside the agent container, to the gateway, completes a verified
  TLS handshake there and gets the gateway's own answer to `GET *`, which it
  refuses before any routing;
- an unconfigured name is rejected by the gateway with an explicit TLS
  `unrecognized_name` alert.

Sampled (a fixed list, not exhaustive):
- outside names do not resolve;
- internet addresses (TCP and UDP) are unreachable;
- the Docker host, through the bridge address, host.docker.internal and
  gateway.docker.internal, does not answer on common ports or on any port a
  running container publishes;
- the gateway answers on no other sampled port.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys

NODE_CHECKS = r"""
const checks = JSON.parse(process.argv[1]);
const net = require('net'), tls = require('tls'), dns = require('dns'), dgram = require('dgram');
const err = e => (e && (e.code || e.message)) || String(e);
function tcp(h, p, ms) { return new Promise(r => { const s = net.connect({host: h, port: p, timeout: ms || 3000});
  s.on('connect', () => { s.destroy(); r('connected'); }); s.on('timeout', () => { s.destroy(); r('timeout'); });
  s.on('error', e => r(err(e))); }); }
function lookup(n) { return new Promise(r => dns.lookup(n, {all: true}, (e, a) =>
  r(e ? err(e) : 'resolved ' + a.map(x => x.address).join(',')))); }
function tlsget(ip, sni) { return new Promise(r => { let buf = '';
  const s = tls.connect({host: ip, port: 443, servername: sni, timeout: 5000}, () =>
    s.write('GET * HTTP/1.1\r\nHost: ' + sni + '\r\nConnection: close\r\n\r\n'));
  s.on('data', d => buf += d); s.on('end', () => r('http ' + buf.split('\r\n')[0]));
  s.on('timeout', () => { s.destroy(); r('timeout'); }); s.on('error', e => r(err(e))); }); }
function udp(h, p) { return new Promise(r => { const s = dgram.createSocket('udp4');
  const t = setTimeout(() => { s.close(); r('no reply'); }, 3000);
  s.on('message', () => { clearTimeout(t); s.close(); r('reply'); });
  s.on('error', e => { clearTimeout(t); try { s.close(); } catch (x) {} r(err(e)); });
  s.send(Buffer.from('000001000001000000000000037879780000010001', 'hex'), p, h, e => {
    if (e) { clearTimeout(t); try { s.close(); } catch (x) {} r(err(e)); } }); }); }
function run(c) {
  if (c.kind === 'dns') return lookup(c.name);
  if (c.kind === 'tcp') return tcp(c.host, c.port, c.timeout);
  if (c.kind === 'udp') return udp(c.host, c.port);
  return tlsget(c.host, c.sni);
}
// Concurrently: an unreachable address takes seconds per probe.
Promise.all(checks.map(run)).then(out => console.log(JSON.stringify(out)));
"""

PYTHON_CHECKS = r"""
import errno, json, os, socket, ssl, sys
from concurrent.futures import ThreadPoolExecutor
checks = json.loads(sys.argv[1])
def err(e):
    code = getattr(e, "errno", None)
    return errno.errorcode.get(code, e.__class__.__name__) if code else e.__class__.__name__
def run(c):
    try:
        if c["kind"] == "dns":
            return "resolved " + ",".join(sorted({a[4][0] for a in socket.getaddrinfo(c["name"], 443)}))
        if c["kind"] == "tcp":
            socket.create_connection((c["host"], c["port"]), timeout=c.get("timeout", 3000) / 1000).close(); return "connected"
        if c["kind"] == "udp":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
            s.sendto(bytes.fromhex("000001000001000000000000037879780000010001"), (c["host"], c["port"]))
            s.recv(512); return "reply"
        cafile = os.environ.get("NODE_EXTRA_CA_CERTS") or os.environ.get("SSL_CERT_FILE")
        ctx = ssl.create_default_context(cafile=cafile)
        raw = socket.create_connection((c["host"], 443), timeout=5)
        s = ctx.wrap_socket(raw, server_hostname=c["sni"])
        s.sendall(("GET * HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % c["sni"]).encode())
        return "http " + s.recv(4096).decode("latin-1").split("\r\n")[0]
    except socket.timeout:
        return "timeout"
    except ssl.SSLError as e:
        return "ERR_SSL_" + str(e.reason or e.__class__.__name__)
    except Exception as e:
        return err(e)
with ThreadPoolExecutor(max_workers=64) as pool:
    print(json.dumps(list(pool.map(run, checks))))
"""

COMMON_PORTS = [22, 53, 80, 443, 2375, 2376, 3000, 5000, 5432, 6379, 8000, 8080, 8443, 9000]
GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4"
GATEWAY_MODE_V6 = "com.docker.network.bridge.gateway_mode_ipv6"
HELPER_IMAGE = "alpine:3.20"
ISOLATION_PORTS = [22, 80, 443]
UNREACHABLE = {"EHOSTUNREACH", "ENETUNREACH"}
OUTSIDE_NAMES = ["example.com", "github.com", "registry.npmjs.org", "pypi.org", "api.openai.com", "statsig.anthropic.com"]
INTERNET = [("1.1.1.1", 443), ("8.8.8.8", 443), ("160.79.104.10", 443)]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def inspect(kind: str, name: str) -> dict:
    return json.loads(docker(kind, "inspect", name))[0]


def published_ports() -> list[int]:
    ports = set()
    for line in docker("ps", "--format", "{{.Ports}}").splitlines():
        for part in line.split(","):
            part = part.strip()
            if "->" in part and ":" in part.split("->")[0]:
                host = part.split("->")[0].rsplit(":", 1)[1]
                low, _, high = host.partition("-")
                if low.isdigit() and (not high or high.isdigit()):
                    # Every port of a published range, not just its ends.
                    ports.update(range(int(low), int(high or low) + 1))
    return sorted(ports)


def bridge_addresses(net: dict) -> tuple[bool, str]:
    """Ask the Docker host (or Docker Desktop's VM) which addresses the bridge has.

    Runs a short-lived host-network helper; pulls the pinned image if it is
    missing and removes it again afterwards. Returns (has_no_address, detail).
    """
    name = (net.get("Options") or {}).get("com.docker.network.bridge.name") or "br-" + net["Id"][:12]
    had_image = subprocess.run(["docker", "image", "inspect", HELPER_IMAGE], capture_output=True).returncode == 0
    if not had_image:
        subprocess.run(["docker", "pull", "-q", HELPER_IMAGE], capture_output=True)
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--net=host", HELPER_IMAGE, "ip", "-o", "addr", "show", "dev", name],
            capture_output=True, text=True,
        )
    finally:
        if not had_image:
            subprocess.run(["docker", "image", "rm", HELPER_IMAGE], capture_output=True)
    if out.returncode != 0:
        return False, f"{name}: cannot inspect ({out.stderr.strip() or out.returncode})"
    addresses = [line.split()[3] for line in out.stdout.splitlines() if len(line.split()) > 3 and line.split()[2] in ("inet", "inet6")]
    return not addresses, f"{name}: addresses={addresses or 'none'}"


def gateway_hosts(gateway: str) -> list[str]:
    """The active hosts, read from inside the gateway container."""
    out = subprocess.run(["docker", "exec", gateway, "python", "-m", "gateway", "hosts"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"cannot read the gateway's active hosts: {out.stderr.strip()}")
    return json.loads(out.stdout)["hosts"]


def run_checks(agent: str, checks: list[dict]) -> list[str]:
    arg = json.dumps(checks)
    for exe, code in (("node", NODE_CHECKS), ("python3", PYTHON_CHECKS)):
        found = subprocess.run(["docker", "exec", agent, "sh", "-c", f"command -v {exe}"], capture_output=True)
        if found.returncode == 0:
            flag = "-e" if exe == "node" else "-c"
            out = subprocess.run(["docker", "exec", agent, exe, flag, code, arg], capture_output=True, text=True)
            if out.returncode != 0:
                raise SystemExit(f"in-container check failed: {out.stderr.strip()}")
            return json.loads(out.stdout.strip().splitlines()[-1])
    raise SystemExit("the agent container has neither node nor python3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway verify", description=__doc__.split("\n\n")[0])
    parser.add_argument("gateway", help="gateway container name")
    parser.add_argument("agent", help="agent container name (running)")
    parser.add_argument("--host", action="append", help="expected API host (default: the gateway's active hosts)")
    parser.add_argument(
        "--allow-flushed-bridge",
        action="store_true",
        help="Docker before 28.0: accept a network without gateway_mode_ipv4=isolated whose bridge "
        "address was removed by hand; the bridge address must still not answer",
    )
    args = parser.parse_args(argv)

    agent = inspect("container", args.agent)
    gateway = inspect("container", args.gateway)
    agent_nets = agent["NetworkSettings"]["Networks"]
    gateway_nets = gateway["NetworkSettings"]["Networks"]
    shared = [n for n in agent_nets if n in gateway_nets]
    if not shared:
        raise SystemExit("the agent and gateway share no network")
    gateway_ips = [gateway_nets[n]["IPAddress"] for n in shared]
    # Expected hosts come from the gateway's active routes, not from the
    # agent's mappings, so an omitted --add-host shows up as a failure.
    hosts = args.host or gateway_hosts(args.gateway)
    if not hosts:
        raise SystemExit("the gateway has no active host")
    gw_ip = gateway_ips[0]

    print(f"agent {args.agent} networks: {', '.join(agent_nets)}")
    bridge_gateways = []
    asserted: list[tuple[str, bool, str]] = []
    waived: list[tuple[str, str]] = []
    isolation: list[tuple[str, str]] = []
    agent_id, gateway_id = agent["Id"], gateway["Id"]
    for name in agent_nets:
        net = inspect("network", name)
        asserted.append((f"network {name} is internal", net.get("Internal") is True, f"internal={net.get('Internal')}"))
        peers = sorted(
            (c.get("Name") or cid[:12]) for cid, c in (net.get("Containers") or {}).items() if cid not in (agent_id, gateway_id)
        )
        has_gateway = gateway_id in (net.get("Containers") or {})
        asserted.append((f"network {name}: only peer is {args.gateway}", has_gateway and not peers,
                         f"other peers={peers or 'none'}, gateway attached={has_gateway}"))
        cfgs = net.get("IPAM", {}).get("Config") or []
        gws = []
        families = set()
        for c in cfgs:
            if not c.get("Subnet"):
                continue
            subnet = ipaddress.ip_network(c["Subnet"])
            families.add(subnet.version)
            # Per family: Docker leaves Gateway out of an internal network's
            # IPAM listing, but the host side of the bridge takes the first address.
            gws.append(c.get("Gateway") or str(next(subnet.hosts())))
        bridge_gateways += gws
        ipv6 = net.get("EnableIPv6") is True
        if ipv6 and 6 not in families:
            asserted.append((f"network {name}: IPv6 enabled with a subnet to check", False,
                             "IPv6 is enabled but no IPv6 subnet is listed, so it cannot be probed"))
        options = net.get("Options") or {}
        modes = [(GATEWAY_MODE, options.get(GATEWAY_MODE))]
        if ipv6:
            modes.append((GATEWAY_MODE_V6, options.get(GATEWAY_MODE_V6)))
        mode = modes[0][1]
        if all(m == "isolated" for _, m in modes):
            for option, _ in modes:
                asserted.append((f"network {name}: {option}=isolated", True, "no host address on the bridge"))
        elif args.allow_flushed_bridge:
            for option, value in modes:
                if value != "isolated":
                    waived.append((f"network {name}: {option}=isolated", f"not set ({value or 'default'}); "
                                   "--allow-flushed-bridge given, the host-side and reachability assertions apply"))
            empty, detail = bridge_addresses(net)
            asserted.append((f"network {name}: bridge interface on the Docker host has no address", empty, detail))
        else:
            for option, value in modes:
                asserted.append((f"network {name}: {option}=isolated", value == "isolated", f"set to {value or 'default (nat)'}"))
        for ip in gws:
            isolation.append((name, ip))
        print(f"  {name}: internal={net.get('Internal')} subnet={[c.get('Subnet') for c in cfgs]} "
              f"bridge address={gws} {GATEWAY_MODE}={mode or 'default'}")
    print(f"gateway {args.gateway} at {gw_ip}; API hosts {hosts}")

    checks: list[tuple[str, dict, str]] = []  # (label, check, expectation)
    for name, ip in isolation:
        # An address nobody holds fails ARP after about 3 s: allow 8 s.
        for port in ISOLATION_PORTS:
            checks.append((f"network {name}: bridge address {ip}:{port} does not answer",
                           {"kind": "tcp", "host": ip, "port": port, "timeout": 8000}, "unreachable"))
    for host in hosts:
        checks.append((f"DNS {host} resolves to the gateway", {"kind": "dns", "name": host}, "resolves:" + ",".join(gateway_ips)))
        checks.append((f"TLS {host}:443 by name", {"kind": "tls", "host": host, "sni": host}, "tls_ok"))
    for name in OUTSIDE_NAMES:
        if name not in hosts:
            checks.append((f"sample: DNS {name}", {"kind": "dns", "name": name}, "no_resolve"))
    for name in ("host.docker.internal", "gateway.docker.internal", args.gateway):
        checks.append((f"DNS {name}", {"kind": "dns", "name": name}, "info"))
    for ip, port in INTERNET:
        checks.append((f"sample: TCP {ip}:{port}", {"kind": "tcp", "host": ip, "port": port}, "no_connect"))
    checks.append(("sample: UDP 8.8.8.8:53", {"kind": "udp", "host": "8.8.8.8", "port": 53}, "no_connect"))
    host_ports = sorted(set(COMMON_PORTS) | set(published_ports()))
    for ip in bridge_gateways:
        for port in host_ports:
            checks.append((f"sample: TCP bridge {ip}:{port}", {"kind": "tcp", "host": ip, "port": port}, "no_connect"))
    for port in (22, 80, 8080, 8443, 3773):
        checks.append((f"sample: TCP gateway {gw_ip}:{port}", {"kind": "tcp", "host": gw_ip, "port": port}, "no_connect"))
    checks.append((f"TLS example.com via {gw_ip} rejected by name", {"kind": "tls", "host": gw_ip, "sni": "example.com"}, "tls_rejected"))

    results = run_checks(args.agent, [c for _, c, _ in checks])
    # host.docker.internal may resolve on Docker Desktop. If it does, its ports must not answer.
    extra_checks = []
    for (_label, check, _expect), result in zip(checks, results, strict=True):
        alias = check.get("name")
        if alias in ("host.docker.internal", "gateway.docker.internal") and result.startswith("resolved "):
            for ip in result.split(" ", 1)[1].split(","):
                for port in host_ports:
                    extra_checks.append((f"sample: TCP {alias} {ip}:{port}", {"kind": "tcp", "host": ip, "port": port}, "no_connect"))
    if extra_checks:
        checks += extra_checks
        results += run_checks(args.agent, [c for _, c, _ in extra_checks])

    failures = 0
    for label, ok, detail in asserted:
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")
    for label, detail in waived:
        print(f"WAIVED  {label}: {detail}")
    for (label, _check, expect), result in zip(checks, results, strict=True):
        if expect.startswith("resolves:"):
            want = set(expect.split(":", 1)[1].split(","))
            got = set(result.split(" ", 1)[1].split(",")) if result.startswith("resolved ") else set()
            ok = bool(got) and got <= want
        elif expect == "no_resolve":
            ok = not result.startswith("resolved")
        elif expect == "no_connect":
            ok = result not in ("connected", "reply")
        elif expect == "unreachable":
            # Refused means a host stack answered; a timeout could be a
            # firewall drop or a stale neighbour entry. Neither counts.
            ok = result in UNREACHABLE
        elif expect == "tls_ok":
            # "GET *" is refused by the gateway itself before any routing,
            # so this answer never comes from (or costs anything at) the upstream.
            ok = result.startswith("http HTTP/1.1 400")
        elif expect == "tls_rejected":
            ok = "UNRECOGNIZED_NAME" in result.upper()
        else:
            ok = None
        mark = "INFO" if ok is None else ("PASS" if ok else "FAIL")
        failures += ok is False
        print(f"{mark}  {label}: {result}")
    print(f"{len(asserted) + len(waived) + len(checks)} checks, {failures} failed, {len(waived)} waived "
          "(lines marked sample are a fixed sample, not exhaustive)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
