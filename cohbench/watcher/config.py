"""Watcher configuration: a JSON file, command-line flags, or both."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .ignore import DEFAULT_NAME_FORMS

MIB = 1024 * 1024


@dataclass
class Config:
    root: str
    out: str
    # Glob patterns relative to the root (see ignore.py). Default: none.
    ignore: list[str] = field(default_factory=list)
    # Basenames never recorded at any depth (decision D-026): the harness's
    # instruction files. Tasks may not contain files with these names.
    ignore_names: list[str] = field(default_factory=lambda: ["CLAUDE.md", "AGENTS.md"])
    # Temporary-file forms of those names that are ignored too (fnmatch over
    # one component, `{name}` for the name); see ignore.DEFAULT_NAME_FORMS.
    ignore_name_forms: list[str] = field(default_factory=lambda: list(DEFAULT_NAME_FORMS))
    # Git ref namespaces left out of the coarse git state (D-026).
    ignore_git_refs: list[str] = field(default_factory=lambda: ["refs/t3/"])
    # Files above this size are hashed but not stored.
    max_blob_bytes: int = 5 * MIB
    # A path settles once it has been quiet this long.
    quiet_ms: int = 100
    # A path that never goes quiet (or stays open for writing) is read anyway
    # once its first unsettled event is this old.
    max_delay_ms: int = 2000
    # argv run on a copy of every committed batch. None: no metrics.
    metric_cmd: list[str] | None = None
    metric_cmd_file: str | None = None
    metric_timeout_s: float = 30.0
    # At shutdown, how long the metric queue is drained (plus the run in
    # progress, at most metric_timeout_s); what is left is recorded as not
    # measured, reason backlog, for the offline command to complete.
    metric_drain_s: float = 45.0
    # The mtime (Unix seconds) of every entry in a metric copy.
    metric_copy_mtime: int = 946684800
    metric_stdout_max: int = 1 * MIB
    metric_stderr_max: int = 4096
    heartbeat_s: float = 1.0
    # Private directory for the metric command's copies of the tree. None: a
    # new temporary directory (in the container, /tmp).
    work_dir: str | None = None
    # Where the agent sees the workspace; metric copies are built there.
    workspace_path: str = "/work/project"
    # users: start as root, run metrics as a separate user that cannot reach
    # the live tree or the output (the container image). none: run them as
    # the watcher's own user in work_dir (tests). auto: users when started
    # as root, none otherwise.
    metric_isolation: str = "auto"
    watcher_user: str = "1000:1000"
    metric_user: str = "1500:1500"

    def validate(self) -> None:
        root = Path(self.root)
        if not root.is_dir():
            raise ValueError(f"root is not a directory: {self.root}")
        r = os.path.realpath(self.root)
        o = os.path.realpath(self.out)
        if o == r or o.startswith(r.rstrip("/") + "/"):
            raise ValueError("the output directory must be outside the watched root")
        if self.max_blob_bytes < 0:
            raise ValueError("max_blob_bytes must be >= 0")
        if self.quiet_ms < 0 or self.max_delay_ms < self.quiet_ms:
            raise ValueError("need 0 <= quiet_ms <= max_delay_ms")
        if self.metric_cmd_file is not None:
            if not isinstance(self.metric_cmd_file, str) or not self.metric_cmd_file:
                raise ValueError("metric_cmd_file must be a path")
            if self.metric_cmd is not None:
                raise ValueError("choose metric_cmd or metric_cmd_file")
        if self.metric_cmd is not None:
            if not isinstance(self.metric_cmd, list) or not self.metric_cmd or not all(isinstance(a, str) for a in self.metric_cmd):
                raise ValueError("metric_cmd must be a non-empty list of strings")
        if self.metric_timeout_s <= 0:
            raise ValueError("metric_timeout_s must be > 0")
        if not isinstance(self.metric_drain_s, (int, float)) or isinstance(self.metric_drain_s, bool) or self.metric_drain_s < 0:
            raise ValueError("metric_drain_s must be >= 0")
        if not isinstance(self.metric_copy_mtime, int) or isinstance(self.metric_copy_mtime, bool) or self.metric_copy_mtime < 0:
            raise ValueError("metric_copy_mtime must be a non-negative integer")
        if self.heartbeat_s <= 0:
            raise ValueError("heartbeat_s must be > 0")
        if not isinstance(self.ignore, list) or not all(isinstance(p, str) for p in self.ignore):
            raise ValueError("ignore must be a list of strings")
        for key in ("ignore_names", "ignore_git_refs"):
            v = getattr(self, key)
            if not isinstance(v, list) or not all(isinstance(p, str) and p for p in v):
                raise ValueError(f"{key} must be a list of non-empty strings")
        f = self.ignore_name_forms
        if not isinstance(f, list) or not all(isinstance(x, str) and "{name}" in x and "/" not in x for x in f) or "{name}" not in f:
            raise ValueError("ignore_name_forms must be a list of names without a slash, each containing {name}, and must include {name} itself")
        for ns in self.ignore_git_refs:
            if not ns.startswith("refs/"):
                raise ValueError(f"ignored git ref namespace must start with refs/: {ns!r}")
        if self.metric_isolation not in ("auto", "users", "none"):
            raise ValueError("metric_isolation must be auto, users or none")
        if not self.workspace_path.startswith("/"):
            raise ValueError("workspace_path must be absolute")
        if self.work_dir is not None:
            w = os.path.realpath(self.work_dir)
            if w == r or w.startswith(r.rstrip("/") + "/"):
                raise ValueError("work_dir must be outside the watched root")

    def to_json(self) -> dict:
        return asdict(self)


def load(path: str | None, overrides: dict, validate: bool = True) -> Config:
    """Merge a JSON config file with overrides (None values are skipped)."""
    data: dict = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config file must hold a JSON object")
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for k, v in overrides.items():
        if v is not None:
            data[k] = v
    missing = {"root", "out"} - set(data)
    if missing:
        raise ValueError(f"missing config keys: {sorted(missing)}")
    cfg = Config(**data)
    if validate:
        cfg.validate()
    return cfg
