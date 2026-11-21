"""Entry point.

    python -m gateway [--config PATH]          run the gateway
    python -m gateway summary LOG [--all]      harness-blind usage summary
    python -m gateway verify GATEWAY AGENT     check a running topology (host side)
    python -m gateway hosts [--config PATH]    print the active hosts as JSON (used by verify)
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from . import tls
from .config import ConfigError, check_startup, load_config
from .proxy import GatewayServer


def serve(config_path: str | None) -> int:
    try:
        config = load_config(config_path)
        for route in sorted(config.routes, key=lambda r: r.prefix):
            state = "active" if route.enabled else f"inactive: {route.disabled_reason}"
            print(f"gateway: route {route.name} -> {route.upstream_host} ({state})", file=sys.stderr)
        check_startup(config)
    except ConfigError as exc:
        print(f"gateway: {exc}", file=sys.stderr)
        return 2

    servers = []
    try:
        servers.append(GatewayServer(config))
        print(
            f"gateway: plain listener on {config.host}:{config.port} "
            f"({'path-prefix routes and /healthz' if config.prefix_routes else '/healthz only'})",
            file=sys.stderr,
        )
        if config.transparent:
            hosts = config.transparent_hosts()
            certs = tls.generate(
                config.tls_dir, hosts, days=config.cert_days, ca_name=config.ca_name, ca_cert_file=config.ca_cert_file
            )
            context = tls.server_context(certs["leaves"])
            servers.append(GatewayServer(config, tls_context=context, state=servers[0].state))
            print(
                f"gateway: transparent mode on {config.tls_host}:{config.tls_port} for {', '.join(hosts)}; "
                f"CA certificate {certs['ca.crt']}",
                file=sys.stderr,
            )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"gateway: cannot start: {servers[0].redactor.text(str(exc)) if servers else exc}", file=sys.stderr)
        return 2
    print(
        f"gateway: log {servers[0].request_log.path}, store_bodies={'on' if config.store_bodies else 'off'}",
        file=sys.stderr,
        flush=True,
    )

    def stop(signum, frame):  # noqa: ARG001
        for server in servers:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True) for s in servers[1:]]
    for thread in threads:
        thread.start()
    try:
        servers[0].serve_forever(poll_interval=0.5)
    finally:
        for thread in threads:
            thread.join(timeout=5)
        for server in servers:
            server.server_close()
    print("gateway: stopped", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "summary":
        from .summary import episode, read_log, summarize

        parser = argparse.ArgumentParser(prog="gateway summary", description="usage for budgets and cost reports")
        parser.add_argument("log", help="path of requests.jsonl")
        parser.add_argument("--all", action="store_true", help="per-call mode: include non-inference calls")
        parser.add_argument("--episode", action="store_true", help="one line of episode totals instead of one per call")
        args = parser.parse_args(argv[1:])
        records = read_log(args.log)
        if args.episode:
            print(json.dumps(episode(records), separators=(",", ":")))
        else:
            for row in summarize(records, include_all=args.all):
                print(json.dumps(row, separators=(",", ":")))
        return 0
    if argv and argv[0] == "hosts":
        parser = argparse.ArgumentParser(prog="gateway hosts")
        parser.add_argument("--config")
        args = parser.parse_args(argv[1:])
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"gateway: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"transparent": config.transparent, "hosts": config.transparent_hosts()}))
        return 0
    if argv and argv[0] == "verify":
        from .verify_topology import main as verify_main

        return verify_main(argv[1:])
    parser = argparse.ArgumentParser(prog="gateway", description="Model API gateway")
    parser.add_argument("--config", help="JSON config file (default: $GATEWAY_CONFIG or the bundled config.json)")
    args = parser.parse_args(argv)
    return serve(args.config)


if __name__ == "__main__":
    sys.exit(main())
