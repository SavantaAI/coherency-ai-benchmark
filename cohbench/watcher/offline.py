"""Complete the metric readings of a finished log, after the episode.

Every committed batch without a measured reading (a `backlog` record, a
failed copy, or no record at all) is measured from its replayed manifest
and the blob store with the same runner, digest and executor the live
watcher uses, and appended to metrics.jsonl with `offline: true`, run
numbers continuing after the last one. A batch whose reuse key (tree digest
and run settings) equals one already measured reuses that reading.

`complete(out, executor)` takes the executor the live run used. The command
line (`complete-metrics`) builds only the isolated executor, so it runs only
in the watcher image, started as root like `run`, with the output directory
at /srv/w/out: the same user, layout, path, environment and link handling
as the live metric worker. Anywhere else it refuses.
"""

from __future__ import annotations

import json
import os

from .blobs import BlobStore
from .manifest import Manifest, apply_record
from .metrics import MetricRunner
from .records import CONFIG, HEARTBEAT, METRICS, MetricsLog
from .replay import ReplayError, read_events


def _lines(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        data = f.read()
    if data and not data.endswith(b"\n"):
        raise ReplayError(f"{path} ends with a partial line")
    return [json.loads(x) for x in data.splitlines() if x]


def _measured(r: dict) -> bool:
    if "measured" in r:
        return bool(r["measured"])
    # A reading written before the `measured` field: the command ran.
    return r.get("run") is not None and (r.get("exit") is not None or bool(r.get("timed_out")))


REQUIRED = ("metric_cmd", "metric_timeout_s", "metric_stdout_max", "metric_stderr_max", "workspace_path", "metric_copy_mtime", "watcher_user", "metric_user", "metric_isolation")
IMAGE_USERS = ("1000:1000", "1500:1500")
IMAGE_WORKSPACE = "/work/project"


def complete(out_dir: str, executor, users: tuple[str, str] | None = None) -> dict:
    """Measure every committed batch that has no measured reading, with
    `executor`, every setting taken from the run's recorded config.json
    (which must be readable): the command, timeout and output limits for
    the runner, the copy mtime for the executor; the executor's workspace
    path (and, with `users`, the users it was launched with) must equal the
    recorded ones. Returns {"measured": [...], "reused": [...], "already": n}."""
    hb_path = os.path.join(out_dir, HEARTBEAT)
    if os.path.exists(hb_path):
        with open(hb_path, encoding="utf-8") as f:
            status = json.load(f).get("status")
        if status in ("starting", "running", "stopping"):
            raise RuntimeError(f"the watcher is {status}; complete the readings after it stopped")
    cfg = load_config(out_dir)
    if not cfg["metric_cmd"]:
        raise RuntimeError("the run had no metric command")
    if cfg["metric_isolation"] != executor.isolation:
        raise RuntimeError(f"the run's metric isolation was {cfg['metric_isolation']!r}; this completion runs with {executor.isolation!r}")
    if getattr(executor, "workspace", None) != cfg["workspace_path"]:
        raise RuntimeError(f"the run's workspace path was {cfg['workspace_path']!r}; this executor builds copies at {getattr(executor, 'workspace', None)!r}")
    if users is not None and (cfg["watcher_user"], cfg["metric_user"]) != tuple(users):
        raise RuntimeError(f"the run's users were {cfg['watcher_user']} and {cfg['metric_user']}; this completion runs as {users[0]} and {users[1]}")
    executor.copy_mtime = int(cfg["metric_copy_mtime"])
    existing = _lines(os.path.join(out_dir, METRICS))
    events = read_events(out_dir)
    blobs = BlobStore(out_dir)
    executor.blobs = blobs
    runs = [r["run"] for r in existing if isinstance(r.get("run"), int) and not isinstance(r.get("run"), bool)]
    runner = MetricRunner(list(cfg["metric_cmd"]), float(cfg["metric_timeout_s"]), int(cfg["metric_stdout_max"]), int(cfg["metric_stderr_max"]), MetricsLog(out_dir, append=True), executor, blobs, offline=True, first_run=max(runs, default=0) + 1)
    done: set[int] = set()
    for r in existing:
        b = r.get("batch")
        if not isinstance(b, int) or not _measured(r):
            continue
        done.add(b)
        if r.get("tree_sha256") and not r.get("timed_out") and r.get("reused_from") is None:
            # The digest includes the run settings: a reading taken another
            # way (another mtime, path or isolation) never matches.
            runner.by_digest.setdefault(r["tree_sha256"], r)
    result = {"measured": [], "reused": [], "already": len(done)}
    m = Manifest()
    try:
        for rec in events:
            if rec["kind"] == "mark":
                continue
            if rec["kind"] != "batch":
                apply_record(m, rec)
                continue
            b = rec["batch"]
            if b in done:
                continue
            got = runner.measure(b, m.copy())
            (result["reused"] if got.get("reused_from") is not None else result["measured"]).append(b)
    finally:
        runner.log.close()
    return result


def load_config(out_dir: str) -> dict:
    """The run's recorded config.json. Raises RuntimeError when it cannot be
    read or lacks a setting the completion needs: no defaults are taken."""
    path = os.path.join(out_dir, CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot read the run's config ({path}): {e}") from e
    missing = [k for k in REQUIRED if not isinstance(cfg, dict) or k not in cfg]
    if missing:
        raise RuntimeError(f"the run's config ({path}) lacks {missing}")
    return cfg


def main_complete(out_dir: str) -> dict:
    """The command line: only in the watcher image, as root, with the
    isolated executor. Root without capabilities cannot read the output
    directory, so the image's own users and workspace path are used to
    launch, and the recorded config, read after the drop, must match them."""
    from .isolate import launch

    if os.geteuid() != 0:
        raise RuntimeError("complete-metrics runs only in the watcher image, started as root like `run` "
                           "(docker run ... <image> complete-metrics /srv/w/out), so the metric runs exactly as the live worker ran it")
    ex = launch("/srv/w/live", out_dir, True, IMAGE_USERS[0], IMAGE_USERS[1], IMAGE_WORKSPACE)
    try:
        return complete(out_dir, ex, users=IMAGE_USERS)
    finally:
        ex.close()
