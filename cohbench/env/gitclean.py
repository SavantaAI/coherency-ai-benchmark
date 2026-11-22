"""Removing T3 Code's checkpoint refs from every repository of the workspace.

T3 checkpoints the workspace repository, nested repositories and submodules
as refs named `refs/t3/orchestration-v2/checkpoints/<scope>/ordinal/<n>`
(`CHECKPOINT_REFS_PREFIX`, apps/server/src/orchestration-v2/CheckpointService.ts
at the pinned T3). Its own cleanup (apps/server/src/storageCleanup.ts) lists
that prefix and `refs/t3/checkpoints/`, keeps refs of the exact checkpoint
shape with a numeric last segment, and deletes them with `update-ref
--no-deref`. This does the same in each repository, and removes only the
objects that deletion left unreachable: objects already unreachable before
are held by temporary refs while `repack -a -d` and `prune` run, then
released and shown to be present. No reflog is expired. Ported from the
committed driver proof's final version.
"""

from __future__ import annotations

import re
import secrets
from pathlib import PurePosixPath
from typing import Any

from .docker import Container, scan_command

T3_CHECKPOINT_PREFIXES = ("refs/t3/orchestration-v2/checkpoints/", "refs/t3/checkpoints/")
T3_CHECKPOINT_REF = re.compile(r"^refs/t3/orchestration-v2/checkpoints/[^/]+/ordinal/(0|[1-9][0-9]*)$|^refs/t3/checkpoints/[^/]+/turn/(0|[1-9][0-9]*)$")
HOLD_PREFIX = "refs/hold/"


def repositories(c: Container, workspace: str) -> dict[str, Any]:
    """Every Git repository under the workspace: each `.git` directory or file
    (so nested repositories and submodule checkouts). Each must answer
    `rev-parse --git-dir`; one that does not makes `ok` false."""
    # NUL-delimited: a directory name holding a newline is still one path.
    proc = c.exec(["find", workspace, "-name", ".git", "-prune", "-print0"], check=False, timeout=300)
    found = sorted({str(PurePosixPath(p).parent) for p in proc.stdout.split("\0") if p})
    repos, unhandled = [], []
    for path in found:
        r = c.exec(["git", "-C", path, "rev-parse", "--git-dir"], check=False)
        if r.returncode == 0:
            repos.append(path)
        else:
            unhandled.append({"path": path, "error": r.stderr.strip()[:200]})
    return {"find_exit": proc.returncode, "repositories": repos, "unhandled": unhandled, "ok": proc.returncode == 0 and not unhandled}


def object_scan(c: Container, repo: str, needles: dict[str, bytes]) -> dict[str, Any]:
    """Every object in one repository, loose or packed, reachable or not."""
    res = scan_command(["exec", c.name, "git", "-C", repo, "cat-file", "--batch-all-objects", "--batch"], needles)
    refs = c.exec(["git", "-C", repo, "for-each-ref", "--format=%(refname)"], check=False)
    return {**res, "repo": repo, "refs": refs.stdout.split(), "refs_exit": refs.returncode}


def unreachable(c: Container, repo: str) -> tuple[int, list[str], str]:
    p = c.exec(["git", "-C", repo, "fsck", "--unreachable", "--no-progress"], check=False, timeout=600)
    return p.returncode, sorted({line.split()[2] for line in p.stdout.splitlines() if line.startswith("unreachable ")}), p.stderr.strip()[:200]


def drop_t3_refs(c: Container, repo: str) -> dict[str, Any]:
    """The committed driver proof's final procedure (driver_proof/ops.py,
    git_drop_t3_refs at af9be88), on a container that holds the repository."""
    steps: list[dict[str, Any]] = []

    def step(name: str, args: list[str], stdin: str | None = None) -> Any:
        p = c.exec(["git", "-C", repo, *args], input_text=stdin, check=False, timeout=600)
        steps.append({"step": name, "exit": p.returncode, "stderr": p.stderr.strip()[:200]})
        return p

    listed = step("for-each-ref (T3 checkpoint prefixes)", ["for-each-ref", "--format=%(refname)", *T3_CHECKPOINT_PREFIXES])
    names = listed.stdout.split()
    refs = [r for r in names if T3_CHECKPOINT_REF.match(r)]
    if listed.returncode == 0 and not refs:
        # Nothing to remove, so nothing to repack or prune: the repository is
        # left exactly as it was.
        return {"repo": repo, "t3_refs_deleted": [], "other_refs_under_t3_prefixes_kept": names, "unreachable_before": None,
                "unreachable_before_missing_after": [], "refs_left": [], "steps": steps, "skipped": "no T3 checkpoint refs", "ok": True}
    fsck_exit, before, fsck_err = unreachable(c, repo)
    steps.append({"step": "fsck --unreachable (before)", "exit": fsck_exit, "stderr": fsck_err})
    if refs:
        step("update-ref --no-deref: delete T3 checkpoint refs", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"delete {r}\0\0" for r in refs))
    # The holds live in a namespace made for this invocation, so a task's own
    # refs/hold/ refs are neither touched nor mistaken for leftovers.
    hold_prefix = f"{HOLD_PREFIX.rstrip('/')}-{secrets.token_hex(6)}/"
    holds = [(f"{hold_prefix}{sha}", sha) for sha in before]
    held = True
    if holds:
        made = step("update-ref: hold the objects unreachable before", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"create {h}\0{sha}\0" for h, sha in holds))
        held = made.returncode == 0
    if fsck_exit == 0 and held:
        # Nothing is pruned unless the earlier unreachable set is known and held.
        step("repack -a -d", ["repack", "-a", "-d", "-q"])
        step("prune --expire=now", ["prune", "--expire=now"])
    else:
        steps.append({"step": "repack and prune skipped: earlier unreachable objects not held", "exit": 1, "stderr": ""})
    if holds:
        step("update-ref --no-deref: release the holds", ["update-ref", "--no-deref", "-z", "--stdin"], "".join(f"delete {h}\0\0" for h, _ in holds))
    missing: list[str] = []
    if before:
        chk = step("cat-file --batch-check (objects unreachable before)", ["cat-file", "--batch-check"], "".join(f"{sha}\n" for sha in before))
        missing = [line.split()[0] for line in chk.stdout.splitlines() if line.endswith(" missing")]
    left = c.exec(["git", "-C", repo, "for-each-ref", "--format=%(refname)", *T3_CHECKPOINT_PREFIXES, hold_prefix], check=False)
    left_refs = [r for r in left.stdout.split() if T3_CHECKPOINT_REF.match(r) or r.startswith(hold_prefix)]
    ok = all(st["exit"] == 0 for st in steps) and left.returncode == 0 and not missing and not left_refs
    return {
        "repo": repo,
        "t3_refs_deleted": refs,
        "other_refs_under_t3_prefixes_kept": [r for r in names if r not in refs],
        "unreachable_before": len(before),
        "unreachable_before_missing_after": missing,
        "refs_left": left_refs,
        "steps": steps,
        "ok": ok,
    }
