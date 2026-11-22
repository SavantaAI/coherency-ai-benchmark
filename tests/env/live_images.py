"""Opt-in pytest plugin recording real image builds during an integration run.

Load with PYTEST_PLUGINS=live_images and put tests/env on PYTHONPATH.
IMAGE_AUDIT_PATH selects a host JSON record. IMAGE_EXPECT_BUILDS optionally
contains a JSON list of exactly the tags the run should build.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_build_contexts import ROOT, fingerprint

from cohbench.env import images

BUILDS = []
ORIGINAL_BUILD = images._build


def pytest_sessionstart(session):
    def build(dockerfile, tag, build_args, context, digest):
        ORIGINAL_BUILD(dockerfile, tag, build_args, context, digest)
        BUILDS.append(tag)

    images._build = build


def pytest_sessionfinish(session, exitstatus):
    images._build = ORIGINAL_BUILD
    tags = {
        "base": f"t3-harness-base:{images.T3_VERSION}",
        "pi": f"t3-harness-pi:{images.T3_VERSION}",
        "gateway": "cohbench-gateway:local",
        "scripted": "cohbench-scripted:local",
        "watcher": "cohbench-watcher:local",
    }
    record = {"test_exitstatus": int(exitstatus), "built": BUILDS, "images": {}}
    for role, tag in tags.items():
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                '{{index .Config.Labels "cohbench.inputs"}}',
                tag,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        label = result.stdout.strip()
        info = images.image_info(tag)
        record["images"][role] = {"tag": tag, "id": info["id"], "inputs_digest": label}
        if label != fingerprint(ROOT, role):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            record.setdefault("errors", []).append(f"input label mismatch: {tag}")
    expected = os.environ.get("IMAGE_EXPECT_BUILDS")
    if expected is not None and sorted(BUILDS) != sorted(json.loads(expected)):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        record.setdefault("errors", []).append("build list differs from expectation")
    Path(os.environ["IMAGE_AUDIT_PATH"]).write_text(json.dumps(record, indent=2) + "\n")
    session.config.pluginmanager.get_plugin("terminalreporter").write_line(
        "Image build audit: " + json.dumps(record)
    )
