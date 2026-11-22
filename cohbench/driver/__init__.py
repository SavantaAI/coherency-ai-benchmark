"""Episode-level operations on T3 Code for the runner. See README.md."""

import hashlib as _hashlib
from pathlib import Path as _Path

DRIVER_FORMAT = 3


def driver_version() -> str:
    """`cohbench-driver <format>+<hash>`: the hash is of this package's and
    cohbench/env's source files, so two runs on the same code agree."""
    h = _hashlib.sha256()
    root = _Path(__file__).resolve().parent
    for p in sorted([*root.glob("*.py"), *(root.parent / "env").glob("*.py")]):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return f"cohbench-driver {DRIVER_FORMAT}+{h.hexdigest()[:12]}"

from .episode import FINISH_S, Clock, Delivery, DriverFailure, Episode, EpisodeError, ProbeResult, Sent, TaskInfo, TurnNotRunning, TurnResult, install_sigterm
from .export import ExportFailed
from .harness import HARNESSES, Harness, harness
from .turns import Question, SecretRequest

__all__ = ["FINISH_S", "HARNESSES", "Clock", "Delivery", "DriverFailure", "Episode", "EpisodeError", "ExportFailed", "Harness", "ProbeResult", "Question", "SecretRequest", "Sent", "TaskInfo", "TurnNotRunning", "TurnResult", "driver_version", "harness", "install_sigterm"]
