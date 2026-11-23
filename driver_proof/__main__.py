"""Run the driver operations against T3 Code in Docker and record verdicts.

    python -m driver_proof run --harness pi --op all
    python -m driver_proof run --harness claude --op reset
    python -m driver_proof summary

The API key file comes from --key-file or DRIVER_PROOF_KEY_FILE. It must sit
outside the repository. Only the gateway container mounts it (container.py).

Every run writes under runs/driver-proof/<run id>/ and, at the end, points
runs/driver-proof/latest at it (written atomically).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import signal
import sys
import traceback
import uuid
from pathlib import Path

from . import container as dk
from .evidence import Blocked, Recorder, now_iso
from .fixtures import Fixtures
from .harness import HARNESSES
from .ops import OPS, container_end
from .session import Workspace

DEFAULT_OUT = dk.REPO_ROOT / "runs" / "driver-proof"
def recorded_pin() -> str | None:
    """The pinned T3 version (decision D-003)."""
    return dk.DEFAULT_T3_VERSION


def collect_versions(ws: Workspace, images: dict[str, str], t3_version: str) -> dict:
    versions = {
        "t3_requested": t3_version,
        "harness": ws.harness.name,
        "base_image": dk.DEFAULT_NODE_IMAGE,
        "base_image_digest": dk.base_image_digest(),
        "image_ids": images,
        "docker_server": dk.run(["version", "--format", "{{.Server.Version}}"], check=False).stdout.strip(),
        "host": f"{platform.system()} {platform.machine()}, python {platform.python_version()}",
        "build_args": dk.HARNESS_BUILD_ARGS[ws.harness.name],
    }
    if ws.c is not None:
        versions["t3_cli"] = ws.c.exec(["t3", "--version"], check=False).stdout.strip()
        versions["harness_cli"] = ws.harness_version()
        versions["t3_detected_harness"] = ws.t3_detected_provider().get("version")
        versions["node"] = ws.c.exec(["node", "--version"], check=False).stdout.strip()
    return versions


def run_op(harness_name: str, op: str, *, key: dk.KeySource, images: dict[str, str], t3_version: str, run_dir: Path, run_id: str, seed: str, pin: str | None) -> dict:
    harness = HARNESSES[harness_name]
    out_dir = (run_dir / harness_name).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = Fixtures(seed)  # fresh per op: values depend only on the seed and the name
    rec = Recorder(harness=harness_name, op=op, out_dir=out_dir, key=key, fixtures=fixtures)
    rec.facts["run_id"] = run_id
    ws = Workspace(harness, images["harness"], key, rec, label=harness_name, gateway_image=images["gateway"])
    versions: dict = {"t3_requested": t3_version}
    try:
        try:
            ws.start()
            versions = collect_versions(ws, images, t3_version)
            rec.check(f"installed T3 is the requested version {t3_version}", versions.get("t3_cli") == f"t3 v{t3_version}", versions.get("t3_cli"), hygiene=True)
            rec.check("the T3 version is the D-003 pin", pin is not None and t3_version == pin, {"requested": t3_version, "pin": pin}, hygiene=True)
            OPS[op](ws, rec)
        except Blocked as exc:
            rec.block(exc.cause, exc.detail)
        except Exception as exc:  # recorded as a failed operation with the traceback
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.save_text("error-traceback.txt", traceback.format_exc())
        try:
            container_end(ws, rec)
        except Exception as exc:
            rec.check("container end checks ran", False, f"{type(exc).__name__}: {exc}", hygiene=True)
            rec.save_text("error-container-end.txt", traceback.format_exc())
        if ws.api is not None:
            rec.facts["rpc_calls"] = ws.api.calls
        rec.facts["messages_sent"] = [s.record() for s in ws.sent]
    finally:
        try:
            ws.finish()
        except Exception as exc:
            rec.check("cleanup: container and topology removal ran", False, f"{type(exc).__name__}: {exc}", hygiene=True)
    rec.facts["fixtures"] = dict(fixtures.used)
    scan = dk.scan_host_tree_for_key(rec.raw_dir, key)
    for leak in scan["files"]:
        Path(leak).unlink()
    rec.check("evidence directory (gateway logs included) holds no copy of the key (every file read)", not scan["files"] and not scan["errors"], {"deleted": scan["files"], "read_errors": scan["errors"]}, hygiene=True)
    path = rec.write(versions)
    verdict = rec.verdict()
    print(f"{harness_name:7} {op:10} {verdict:8} {path}", flush=True)
    for c in rec.checks:
        if not c.passed:
            print(f"    NO {'' if c.kind == 'decides' else '(' + c.kind + ') '}{c.name}", flush=True)
    if rec.blocked:
        print(f"    blocked: {rec.blocked['cause']}", flush=True)
    if rec.error:
        print(f"    error: {rec.error}", flush=True)
    return {"harness": harness_name, "op": op, "verdict": verdict, "blocked": rec.blocked, "result_file": str(path.relative_to(run_dir)), "finished_at": now_iso()}


def write_summary(run_dir: Path, manifest: dict) -> dict:
    table: dict = {}
    for r in manifest["results"]:
        table.setdefault(r["op"], {})[r["harness"]] = {"verdict": r["verdict"], "blocked": (r["blocked"] or {}).get("cause"), "result_file": r["result_file"]}
    label = "the recorded proof (T3 version equals the D-003 pin)" if manifest["t3_version_is_recorded_pin"] else "NOT the recorded proof: the T3 version differs from the D-003 pin"
    summary = {"written_at": now_iso(), "run_id": manifest["run_id"], "label": label, "table": table}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def write_latest(out_root: Path, run_id: str) -> None:
    tmp = out_root / f".latest-{uuid.uuid4().hex[:6]}"
    tmp.write_text(run_id + "\n")
    os.replace(tmp, out_root / "latest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m driver_proof")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run operations")
    run.add_argument("--harness", choices=[*HARNESSES, "all"], default="all")
    run.add_argument("--op", choices=[*OPS, "all"], default="all")
    run.add_argument("--key-file", help="file holding the Anthropic API key (or DRIVER_PROOF_KEY_FILE)")
    run.add_argument("--t3-version", default=dk.DEFAULT_T3_VERSION)
    run.add_argument("--seed", help="fixture seed (default: random, recorded in the manifest)")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT)
    run.add_argument("--rebuild", action="store_true", help="rebuild the base once and each harness image once, before any op")
    run.add_argument("--allow-unpinned", action="store_true", help="allow a T3 version other than the D-003 pin (the run then fails its pin check)")
    build = sub.add_parser("build", help="build the harness images only")
    build.add_argument("--harness", choices=[*HARNESSES, "all"], default="all")
    build.add_argument("--t3-version", default=dk.DEFAULT_T3_VERSION)
    build.add_argument("--rebuild", action="store_true")
    summ = sub.add_parser("summary", help="print the summary of a run (default: latest)")
    summ.add_argument("--out", type=Path, default=DEFAULT_OUT)
    summ.add_argument("--run-id")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        for i, name in enumerate(HARNESSES if args.harness == "all" else [args.harness]):
            print(dk.build_images(name, args.t3_version, rebuild_base=args.rebuild and i == 0, rebuild_harness=args.rebuild))
        return 0
    if args.cmd == "summary":
        run_id = args.run_id or (args.out / "latest").read_text().strip()
        print((args.out / run_id / "summary.json").read_text())
        return 0

    # A SIGTERM must leave through the cleanup scopes below, not past them.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    key = dk.load_key(args.key_file)
    pin = recorded_pin()
    if args.t3_version != pin and not args.allow_unpinned:
        raise SystemExit(f"--t3-version {args.t3_version} is not the D-003 pin ({pin}); pass --allow-unpinned to run it anyway")
    harnesses = list(HARNESSES) if args.harness == "all" else [args.harness]
    ops = list(OPS) if args.op == "all" else [args.op]
    run_id = f"{now_iso()[:19].replace(':', '')}-{uuid.uuid4().hex[:6]}"
    args.out = args.out.resolve()
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True)
    seed = args.seed or secrets.token_hex(8)
    gateway_tag = f"{dk.GATEWAY_REPO}:{run_id.lower()}"
    manifest = {
        "run_id": run_id,
        "started_at": now_iso(),
        "seed": seed,
        "t3_version": args.t3_version,
        "t3_version_recorded_pin": pin,
        "t3_version_is_recorded_pin": pin == args.t3_version,
        "network_mode": dk.network_mode(),
        "ops": ops,
        "harnesses": harnesses,
        "results": [],
    }
    try:
        # One cleanup scope: the gateway tag is registered before it is built.
        manifest["gateway_image_tag"] = gateway_tag
        gateway_id = dk.build_gateway(run_id)
        manifest["gateway_image_id"] = gateway_id
        if manifest["network_mode"]["flush_via"] and manifest["network_mode"]["flush_via"].startswith("privileged helper"):
            dk.run(["pull", "-q", dk.FLUSH_HELPER_IMAGE], timeout=600)
        # Images are settled before any op runs, and every op of a harness runs
        # on the image ids recorded here, even if a tag moves meanwhile.
        images: dict[str, dict] = {}
        for i, name in enumerate(harnesses):
            built = dk.build_images(name, args.t3_version, rebuild_base=args.rebuild and i == 0, rebuild_harness=args.rebuild)
            images[name] = {**built, "base_id": dk.image_info(built["base"])["id"], "harness_id": dk.image_info(built["harness"])["id"]}
        manifest["images"] = images
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        for name in harnesses:
            img = {"base": images[name]["base_id"], "harness": images[name]["harness_id"], "gateway": gateway_id}
            for op in ops:
                manifest["results"].append(run_op(name, op, key=key, images=img, t3_version=args.t3_version, run_dir=run_dir, run_id=run_id, seed=seed, pin=pin))
                (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    finally:
        manifest["finished_at"] = now_iso()
        manifest["gateway_image_removed"] = dk.remove_image(gateway_tag)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        write_summary(run_dir, manifest)
        write_latest(args.out, run_id)
    return 0 if all(r["verdict"] == "pass" for r in manifest["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
