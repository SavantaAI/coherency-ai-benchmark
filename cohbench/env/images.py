"""Image provisioning: a separate, explicit step, done once and reused.

    images, record = provision("pi", record_path=Path("runs/images.json"))
    ... any number of episodes with `images` ...
    remove(record)      # only on request: the shared images an episode never removes

An episode removes its own containers, networks, volumes and snapshot images
(cohbench/env/environment.py). The images here are shared across episodes:
the harness image (docker/base and docker/<harness>, as the driver proof
builds them), the gateway, scripted server and watcher images built from the
working tree, and on Docker before 28.0 the `alpine:3.20` helper the bridge
flush and `gateway verify` use. The record says which of them this step
built or pulled, so `remove` takes away only those.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .docker import DockerError, image_exists, image_info, remove_image, run

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / "docker"

T3_VERSION = "0.1.12-nightly.20261121.5051"
NODE_IMAGE = "node:24.22.3-bookworm-slim"
HARNESS_BUILD_ARGS = {"pi": {"PI_VERSION": "1.6.0"}, "claude": {"CLAUDE_CODE_VERSION": "2.1.330"}}
FLUSH_HELPER_IMAGE = "alpine:3.20"


def _build(dockerfile: Path, tag: str, build_args: dict[str, str], context: Path) -> None:
    args = ["docker", "build", "-q", "-f", str(dockerfile), "-t", tag]
    for k, v in build_args.items():
        args += ["--build-arg", f"{k}={v}"]
    args.append(str(context))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise DockerError(f"build of {tag} failed:\n{proc.stderr[-4000:]}")


@dataclass(frozen=True)
class Images:
    harness: str
    gateway: str
    scripted: str
    watcher: str

    def ids(self) -> dict[str, str]:
        return {role: image_info(tag)["id"] for role, tag in (("harness", self.harness), ("gateway", self.gateway), ("scripted", self.scripted), ("watcher", self.watcher))}


def harness_image(harness: str, *, t3_version: str = T3_VERSION, rebuild: bool = False) -> tuple[str, list[str]]:
    base = f"t3-harness-base:{t3_version}"
    tag = f"t3-harness-{harness}:{t3_version}"
    built = []
    if rebuild or not image_exists(base):
        _build(DOCKER_DIR / "base/Dockerfile", base, {"T3_VERSION": t3_version, "NODE_IMAGE": NODE_IMAGE}, DOCKER_DIR)
        built.append(base)
    if rebuild or built or not image_exists(tag):
        _build(DOCKER_DIR / f"{harness}/Dockerfile", tag, {"BASE_IMAGE": base, **HARNESS_BUILD_ARGS[harness]}, DOCKER_DIR)
        built.append(tag)
    return tag, built


def provision(harness: str, *, tag: str = "local", rebuild_harness: bool = False, rebuild_services: bool = True, record_path: Path | None = None) -> tuple[Images, dict[str, Any]]:
    """Build or reuse every image an episode needs, and say which this call
    built or pulled. The service images are rebuilt from the working tree by
    default (Docker's cache makes an unchanged rebuild quick)."""
    record: dict[str, Any] = {"format": "cohbench-images", "built": [], "pulled": [], "reused": []}
    htag, built = harness_image(harness, rebuild=rebuild_harness)
    record["built"] += built
    if not built:
        record["reused"].append(htag)
    svc = {}
    for role, dockerfile in (("gateway", "docker/gateway/Dockerfile"), ("scripted", "docker/scripted/Dockerfile"), ("watcher", "docker/watcher/Dockerfile")):
        t = f"cohbench-{role}:{tag}"
        existed = image_exists(t)
        if rebuild_services or not existed:
            _build(REPO_ROOT / dockerfile, t, {}, REPO_ROOT)
            if not existed:
                record["built"].append(t)
            else:
                record["reused"].append(t)
        else:
            record["reused"].append(t)
        svc[role] = t
    from .checks import network_mode

    if network_mode()["path"] != "gateway_mode_ipv4=isolated" and network_mode()["flush_via"] and network_mode()["flush_via"].startswith("privileged helper"):
        if not image_exists(FLUSH_HELPER_IMAGE):
            run(["pull", FLUSH_HELPER_IMAGE], timeout=600)
            record["pulled"].append(FLUSH_HELPER_IMAGE)
        else:
            record["reused"].append(FLUSH_HELPER_IMAGE)
    images = Images(harness=htag, gateway=svc["gateway"], scripted=svc["scripted"], watcher=svc["watcher"])
    record["images"] = asdict(images)
    record["ids"] = images.ids()
    if record_path is not None:
        Path(record_path).write_text(json.dumps(record, indent=1))
    return images, record


def remove(record: dict[str, Any]) -> list[dict]:
    """Remove the images a `provision` call built or pulled, by exact tag."""
    return [remove_image(t) for t in [*record.get("built", []), *record.get("pulled", [])]]


def ensure(harness: str, **kw: Any) -> Images:
    """`provision` without the record, for scripts and tests."""
    return provision(harness, **kw)[0]
