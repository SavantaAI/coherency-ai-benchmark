"""Command line.

    python -m cohbench.runner run --task DIR --harness NAME --model ID \\
        --upstream scripted:SCRIPT | real:KEY_FILE --mode default|coached --out DIR \\
        --policy scripted:RULES [--episode ID] [--poll-s S]
    python -m cohbench.runner check-task DIR [--policy RULES] [--script SCRIPT]

`run` exits 0 when the episode is usable (finished or cut off by a budget,
every required event fired, export accepted), 3 when it finished but a
required event never fired, 1 when it was published as failed, and 2 on a
usage error.
The key file is passed to the driver by path and never opened here. The
policy file and a scripted run's model script are host-only documents: the
task check refuses a workspace that holds either (D-031).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Callable

from .policy import POLICY_KINDS
from .run import Factory, run_episode
from .runner import RunConfig
from .task import check, load


def _upstream(v: str) -> tuple[str, Path]:
    kind, sep, path = v.partition(":")
    if not sep or kind not in ("scripted", "real") or not path:
        raise argparse.ArgumentTypeError("scripted:<script> or real:<key file>")
    return kind, Path(path)


# `--policy KIND:FILE`: the kinds are `policy.POLICY_KINDS`; another package
# adds one with `policy.register_policy_kind` (the classifier, from
# `cohbench.user`). The file is a host-only document whatever the kind, and the
# run builds its factory from it.
try:  # registers `classifier` when the classifier unit is present; the CLI works without it
    from cohbench.user import kind as _classifier_kind
except ImportError:
    _classifier_kind = None
if _classifier_kind is not None and hasattr(_classifier_kind, "register"):
    _classifier_kind.register()


def _default_factory() -> Factory | None:
    try:
        from .adapter import open_episode  # stage 2
    except ImportError:
        return None
    return open_episode


def main(argv: list[str] | None = None, factory: Factory | None = None, sleep: Callable[[float], None] | None = None,
         utcnow: Callable[[], Any] | None = None) -> int:
    """`factory`, `sleep` and `utcnow` are for tests: a stand-in episode and its clock."""
    ap = argparse.ArgumentParser(prog="python -m cohbench.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one episode")
    r.add_argument("--task", required=True, type=Path)
    r.add_argument("--harness", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--upstream", required=True, type=_upstream)
    r.add_argument("--mode", required=True, choices=("default", "coached"))
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--policy", required=True, help="KIND:<file>, KIND one of " + ", ".join(sorted(POLICY_KINDS)))
    r.add_argument("--episode", default=None, help="opaque episode id; random when absent")
    r.add_argument("--poll-s", type=float, default=5.0)
    c = sub.add_parser("check-task", help="validate a task instance")
    c.add_argument("dir", type=Path)
    c.add_argument("--policy", type=Path, default=None, help="the user policy file the run will use")
    c.add_argument("--script", type=Path, default=None, help="the scripted run's model script the run will use")
    a = ap.parse_args(argv)

    if a.cmd == "check-task":
        hosts = {k: p for k, p in (("user policy", a.policy), ("model script", a.script)) if p is not None}
        task, problems, warnings = check(a.dir, hosts)
        for w in warnings:
            print(f"warning: {w}")
        if problems:
            for p in problems:
                print(f"error: {p}")
            return 1
        assert task is not None
        print(json.dumps({"instance": task.instance, "family": task.grouping.family, "digest": task.digest,
                          "files": len(task.workspace.entries), "events": [e.name for e in task.events]}, indent=1))
        return 0

    kind, _, path = a.policy.partition(":")
    if kind not in POLICY_KINDS or not path:
        print(f"error: --policy KIND:<file>, KIND one of {sorted(POLICY_KINDS)}", file=sys.stderr)
        return 2
    factory = factory or _default_factory()
    if factory is None:
        print("error: the driver adapter (cohbench/runner/adapter.py) could not be imported", file=sys.stderr)
        return 2
    if not (math.isfinite(a.poll_s) and a.poll_s > 0):
        print("error: --poll-s must be a positive number of seconds", file=sys.stderr)
        return 2
    episode = a.episode if a.episode is not None else f"e{secrets.token_hex(6)}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", episode):
        print("error: --episode: 1 to 64 letters, digits, - and _", file=sys.stderr)
        return 2
    try:
        hosts: dict[str, Path] = {"user policy": Path(path)}
        if a.upstream[0] == "scripted":
            hosts["model script"] = a.upstream[1]  # never the key file, which is not opened here
        task = load(a.task, hosts)
        POLICY_KINDS[kind](path)  # the file must load before anything opens
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cfg = RunConfig(episode_id=episode, harness=a.harness, model=a.model, upstream=a.upstream, mode=a.mode, poll_s=a.poll_s)
    kw: dict[str, Any] = {"sleep": sleep} if sleep else {}
    if utcnow:
        kw["utcnow"] = utcnow
    try:
        out = run_episode(task, cfg, factory, (kind, Path(path)), a.out, **kw)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"episode": cfg.episode_id, "path": str(out.path), "status": out.status, "end": out.end_reason, "failure": out.failure,
                      "detail": out.detail}, indent=1))
    return {"usable": 0, "not_reached": 3}.get(out.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
