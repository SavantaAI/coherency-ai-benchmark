"""Offline interpreter lookup for parent test processes."""
import subprocess

import pytest


def runtime_interpreters():
    executables = []
    for version in ("3.12", "3.15"):
        result = subprocess.run(["uv", "python", "find", "--offline", "--system", version], capture_output=True, text=True)
        if result.returncode:
            pytest.skip(f"cross-runtime comparison requires installed Python {version}; offline lookup found none")
        executables.append(result.stdout.strip())
    return executables
