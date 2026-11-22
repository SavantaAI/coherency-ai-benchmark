"""One episode's container topology.

    env = Environment(images, Upstream.scripted(script), workspace=Path("task/ws"), manifest=Path("task/ws.json"),
                      out_dir=Path("runs/x/env"), prepare=callback, harness_paths=HarnessPaths(...))
    env.start()          # workspace checks, volume, site, watcher, harness container, prepare, checks
    env.harness          # the running harness container
    env.mark("turn-3")   # a watcher batch boundary, acknowledged
    env.handoff()        # D-010: fresh container on a cloned and cleaned volume, same site
    copy = env.probe_copy(upstream)   # D-009: paused capture, own site; raises ProbeUnavailable
    copy.close()
    env.close()          # removes every container, network, volume and image this environment made

Every container, network and volume gets an opaque random name, registered
before Docker is asked to create it; `names` maps them to roles on the host
only. The harness container has no bind mount: the CA certificate is copied
in before its first start, and the workspace is a Docker volume populated
from exactly the files the task's manifest lists. The watcher mounts the
same volume read-only, has no network, and starts first.

`prepare(container, role)` is the caller's hook, run once per harness
container (role "segment" or "copy") after it starts and before the checks:
the driver writes the harness settings and waits for T3 there, so the checks
see the container as the agent will.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import checks, gitclean, volume, workspace as wsp
from .docker import (Container, DockerError, DockerTimeout, GateFailed, KeyLeak, Registry, bounded, bounded_subprocess, container_state, cp_in, key_hits,
                     no_deadline, one_file_tar, deadline as docker_deadline, opaque_name, require_stopped, run, scan_command, scan_image_save, scan_ok, tar_member_hits, write_scanned)
from .images import Images
from .site import REPO_ROOT, Site, Upstream, start_site

WORKSPACE = "/work/project"
CONTAINER_HOSTNAME = "workstation"
CA_PATH = "/usr/local/share/ca-certificates/local-ca.crt"
# The watcher image's paths (docker/watcher/Dockerfile).
WATCHER_LIVE = "/srv/w/live"
WATCHER_OUT = "/srv/w/out"
# Files this project places in every harness image (docker/base, docker/pi, docker/claude).
IMAGE_FILES = ("/etc/t3-settings.json", "/usr/local/bin/workspace-entrypoint")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class WatcherSpec:
    """The watcher's settings (cohbench/watcher/README.md, Configuration)."""

    # The root `.git` is recorded coarsely by the watcher (HEAD and refs). A
    # nested repository's `.git` is not task content either: T3 writes its
    # checkpoint objects there and the handoff's cleanup removes them.
    ignore: tuple[str, ...] = (".git/**", "**/.git/**")
    metric_cmd: tuple[str, ...] | None = None
    task_dir: Path | None = None  # mounted read-only at /opt/task for the metric command
    quiet_ms: int = 100
    stop_timeout_s: int = 90
    tmp_size: str = "256m"  # the watcher's /tmp, where a metric run rebuilds the tree
    mark_timeout_s: float = 120.0
    # The watcher's limit on one metric run (`--metric-timeout-s`); None
    # leaves the watcher's default.
    metric_timeout_s: float | None = None
    # D-026: basenames never recorded at any depth (every harness's
    # instruction file, so the log never shows which one is absent) and git
    # ref namespaces never recorded. None leaves the watcher's own defaults;
    # the driver always passes both from its harness descriptions.
    ignore_names: tuple[str, ...] | None = None
    ignore_git_refs: tuple[str, ...] | None = None
    # Where the agent sees the workspace (the metric's copy is built there),
    # and the metric's isolation: `users` (the metric server as its own
    # user, which needs the root start below) is required, not `auto`, so a
    # start that cannot isolate fails instead of running unisolated.
    workspace_path: str = WORKSPACE
    metric_isolation: str = "users"


@dataclass(frozen=True)
class HarnessPaths:
    """What the checks look at outside the workspace: the harness's own
    directories (every name, any depth) and the files this project writes
    there (names and text)."""

    dirs: tuple[str, ...] = ()
    text_files: tuple[str, ...] = ()


@dataclass
class Segment:
    number: int
    container: Container
    volume: str
    watcher: str | None
    watcher_out: Path | None
    started_utc: str
    ended_utc: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    watcher_stop: dict[str, Any] | None = None
    # Every entry under the harness directories at the startup check: the
    # baseline a probe copy's name check is judged against.
    name_baseline: frozenset[str] = frozenset()
    # The text of each file this project writes, at the startup check: a
    # probe copy's check passes a file whose text is unchanged and leaves a
    # changed one (the agent's edit) outside the gate.
    text_baseline: dict[str, str] = field(default_factory=dict)


class HandoffFailed(RuntimeError):
    """A handoff stopped part way. `stage` names the step (stop server, stop
    container, final mark, key scan, capture, server log, remove container,
    stop watcher, clone, git cleanup, new segment); `cause` is the error."""

    def __init__(self, stage: str, cause: BaseException):
        super().__init__(f"handoff failed at {stage}: {type(cause).__name__}: {cause}"[:800])
        self.stage = stage
        self.cause = cause


class MainNotResumed(RuntimeError):
    """The main harness container stayed paused after a probe capture. The
    episode cannot go on: the driver ends it as a driver failure."""


class ProbeUnavailable(RuntimeError):
    def __init__(self, reason: str, detail: Any = None):
        super().__init__(f"{reason}: {json.dumps(detail, default=str)[:600]}")
        self.reason = reason
        self.detail = detail


class Environment:
    def __init__(
        self,
        images: Images,
        upstream: Upstream,
        *,
        workspace: Path | bytes,
        manifest: wsp.Manifest | Path | dict | list,
        out_dir: Path,
        watcher: WatcherSpec | None = WatcherSpec(),
        instruction: tuple[str, str] | None = None,
        prepare: Callable[[Container, str], None] | None = None,
        harness_paths: HarnessPaths = HarnessPaths(),
        workspace_path: str = WORKSPACE,
        key_guard: tuple[Path, ...] = (),
    ):
        if workspace_path != WORKSPACE:
            # The harness image's entrypoint starts T3 in /work/project.
            raise ValueError(f"the workspace path is fixed at {WORKSPACE}")
        if manifest is None:
            raise wsp.WorkspaceError("a workspace needs its manifest")
        self.images = images
        self.upstream = upstream
        self.workspace_source = workspace
        self.manifest = manifest if isinstance(manifest, wsp.Manifest) else wsp.Manifest.load(manifest)
        self.out_dir = Path(out_dir).resolve()
        self.watcher_spec = watcher
        self.instruction = instruction
        self.prepare = prepare
        self.harness_paths = harness_paths
        self.workspace_path = workspace_path
        # Further directories the key file may not lie in (the driver passes
        # the whole episode output root, D-019).
        self.key_guard = tuple(key_guard)
        self.registry = Registry()
        self.names: dict[str, str] = {}
        self.site: Site | None = None
        self.segments: list[Segment] = []
        self.copies: list["ProbeCopy"] = []
        self.started_utc: str | None = None
        self._t0: float | None = None
        self.events: list[dict[str, Any]] = []
        self.instruction_record: dict[str, Any] | None = None
        self.workspace_record: dict[str, Any] | None = None
        self.removal: list[dict] = []
        self.closed = False
        self._lock = threading.Lock()
        self.on_closed: list[Callable[["Environment"], None]] = []
        self.final_usage: dict[str, Any] | None = None

    # ----- clocks and usage --------------------------------------------------------

    @property
    def harness(self) -> Container:
        if not self.segments:
            raise RuntimeError("the environment is not started")
        return self.segments[-1].container

    def wall_elapsed(self) -> float:
        """Raw wall-clock seconds since `start` began (monotonic)."""
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    def usage(self) -> dict[str, Any]:
        """The main site's gateway usage, as `python -m gateway summary
        --episode` prints it, run inside the gateway's container (the host
        may not read the gateway's files). After the gateway was stopped, the
        summary of the logs the export copied out (`final_usage`), else an
        empty dict. For budgets and cost only (D-008)."""
        if self.site is None:
            return {}
        if self.final_usage is not None:
            return self.final_usage
        return self.site.usage()

    def note(self, what: str, **data: Any) -> None:
        self.events.append({"at": utc_now(), "what": what, **data})

    def keys(self) -> list:
        """Every key snapshot in play: the main upstream's and each probe
        copy's. Everything written to the host is scanned for all of them."""
        out = []
        for k in [self.upstream.key] + [c.site.upstream.key for c in self.copies if c.site is not None]:
            if k is not None and all(k.secret != o.secret for o in out):
                out.append(k)
        return out

    def write(self, path: Path, data: str | bytes) -> Path:
        """Write a host file only after scanning it for every key (KeyLeak,
        nothing written, on a hit); atomically."""
        return write_scanned(path, data, self.keys())

    # ----- start ---------------------------------------------------------------------

    def _check_key_path(self) -> None:
        key = self.upstream.key
        if key is None:
            return
        places = [self.out_dir, *[Path(p).resolve() for p in self.key_guard]]
        if isinstance(self.workspace_source, (str, Path)):
            places.append(Path(self.workspace_source).resolve())
        for place in places:
            if key.path == place or place in key.path.parents:
                raise KeyLeak(f"the key file lies inside {place}; keep it outside the workspace and the output directory")

    def start(self) -> None:
        self._t0 = time.monotonic()
        self.started_utc = utc_now()
        self._check_key_path()
        bad = wsp.check_names(self.manifest)
        if bad:
            raise GateFailed("task workspace names that name the measurement", bad[:20])
        taken = wsp.reserved(self.manifest, tuple(self.watcher_spec.ignore_names or ()) if self.watcher_spec else ())
        if taken:
            raise GateFailed("task files with a reserved name (D-026: harness instruction files)", taken[:20])
        packed = wsp.pack(self.workspace_source, self.manifest)
        tar = packed.tar
        if self.instruction is not None:
            tar, self.instruction_record = wsp.add_instruction_sentence(tar, *self.instruction, mtime=packed.newest_mtime)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_record = {"entries": packed.entries, "bytes": len(tar), "host_metadata_skipped": packed.host_metadata_skipped,
                                 "key_admission": self.admit(tar)}
        vol = volume.create(self.registry)
        self.names[vol] = "workspace volume, segment 1"
        volume.populate(vol, tar, self.images.harness, self.registry)
        self.note("volume populated", **self.workspace_record)
        self.site = start_site("main", self.upstream, self.out_dir / "site-main", self.registry, gateway_image=self.images.gateway, scripted_image=self.images.scripted)
        for role, name in self.site.names.items():
            self.names[name] = f"main {role.replace('_', ' ')}"
        self._start_segment(1, vol)

    def admit(self, tar: bytes) -> dict[str, Any] | None:
        """The key must not be in the workspace, as bytes or decoded: every
        file's bytes and its members one level down (gzip, bzip2, xz, zip,
        tar), and every object of every git repository in it, loose and
        packed, nested repositories included, read decoded by `git cat-file`
        in a throwaway container with no network. Content in any other
        encoding is not searched. Raises KeyLeak, or GateFailed when a
        repository cannot be read; nothing is populated then."""
        key = self.upstream.key
        if key is None:
            return None
        if key_hits(tar, key):
            raise KeyLeak("the workspace holds the key; nothing was populated")
        found = tar_member_hits(tar, key)
        if found:
            raise KeyLeak(f"the workspace holds the key, or a file that could not be decoded to check: {found[:10]}; nothing was populated")
        git = volume.scan_git_objects_tar(tar, self.images.harness, self.registry, {"key": key.secret.encode()})
        if any(git["hits"].values()):
            raise KeyLeak("a git object in the workspace holds the key; nothing was populated")
        if not git["ok"]:
            raise GateFailed("the workspace's git objects could not all be read for the key scan", git)
        return {"git_repositories": git["repositories"], "git_object_bytes": git["bytes"]}

    def _start_segment(self, number: int, vol: str) -> Segment:
        assert self.site is not None
        watcher_name, watcher_out = None, None
        if self.watcher_spec is not None:
            watcher_out = self.out_dir / "watcher" / f"segment-{number}"
            watcher_name = start_watcher(vol, watcher_out, self.watcher_spec, self.images.watcher, self.registry)
            self.names[watcher_name] = f"watcher, segment {number}"
        c = start_harness(self.images.harness, self.site, vol, self.registry, workspace_path=self.workspace_path)
        self.names[c.name] = f"harness container, segment {number}"
        seg = Segment(number, c, vol, watcher_name, watcher_out, utc_now())
        self.segments.append(seg)
        if self.prepare is not None:
            self.prepare(c, "segment")
        seg.checks = self.check(c, self.site)
        seg.name_baseline = seg.checks["names"]["listing"]
        seg.text_baseline = seg.checks["names"]["texts"]
        self.note("harness container ready", segment=number)
        return seg

    def check(self, c: Container, site: Site, *, baseline: frozenset[str] | None = None, text_baseline: dict[str, str] | None = None,
              extra_names: list[str] | None = None) -> dict[str, Any]:
        """The infrastructure checks (cohbench/env/checks.py). Raises GateFailed.
        `baseline` limits the name check under the harness directories to the
        entries present at the segment's startup (a probe copy)."""
        out: dict[str, Any] = {}
        out["verify"] = checks.verify(site.gateway.name, c.name, flushed=site.flushed)
        if not out["verify"]["ok"]:
            raise GateFailed("gateway verify", {k: v for k, v in out["verify"].items() if k != "output"})
        out["bridge"] = checks.bridge_unreachable(c, site.bridge["bridge_ip"])
        if not out["bridge"]["ok"]:
            raise GateFailed("the bridge address answers from the harness container", out["bridge"])
        out["identity"] = checks.identity(c, site.gateway_ip)
        if not out["identity"]["ok"]:
            raise GateFailed("identity check inside the harness container", out["identity"])
        names = [n for n in self.names if n in (c.name, site.gateway.name, site.agent_network, site.up_network)] + [s.volume for s in self.segments] + list(extra_names or [])
        if any(checks.TELLING.search(n) for n in names):
            raise GateFailed("a resource name is telling", names)
        out["names"] = checks.infrastructure_names(c, list(self.harness_paths.dirs), [CA_PATH], list(IMAGE_FILES) + list(self.harness_paths.text_files), baseline=baseline,
                                                   text_baseline=text_baseline)
        if not out["names"]["ok"]:
            raise GateFailed("telling names or text outside the workspace", out["names"])
        return out

    # ----- the watcher -------------------------------------------------------------------

    def mark(self, label: str, *, at: str | None = None, timeout_s: float | None = None, limit_s: float | None = None, final: bool = False,
             allow_straddle: bool = False) -> dict[str, Any]:
        """A mark naming the instant `at` (UTC; default the time of the
        request), D-037: `python -m cohbench.watcher mark --at`. A mark never
        closes an open batch; the watcher answers once everything written up
        to `at` is in a committed batch (within `limit_s`, the watcher's own
        limit), with `accepted`, `at`, `batch` (in effect at `at`),
        `straddled` (a batch that began before `at` and ended after it, or
        null) and `covered_utc`, plus `requested_utc` and `acked_utc` added
        here. A mark that is not accepted names no state: it raises. The
        whole subprocess ends within `timeout_s` (default the spec's
        mark_timeout_s), capped by the current deadline. `final` (a reset's
        and the export's mark, after the agent's container stopped)
        additionally refuses a straddled answer, unless `allow_straddle`: an
        elapsed cutoff's end, where the batch that straddles the deadline is
        outside the episode by decision (D-035)."""
        seg = self.segments[-1]
        if seg.watcher_out is None or self.watcher_spec is None:
            raise RuntimeError("no watcher in this environment")
        t0 = time.monotonic()
        requested = utc_now()
        limit = bounded(self.watcher_spec.mark_timeout_s if timeout_s is None else timeout_s)
        # The subprocess gets the whole limit; the watcher's own wait for the
        # acknowledgement gets it less a second for python's start and exit.
        inner = max(0.05, limit - 1.0)
        # The instant is always explicit: the caller's, else the request's own.
        at = at or requested
        args = [sys.executable, "-m", "cohbench.watcher", "mark", str(seg.watcher_out), label, "--timeout", str(round(inner, 3)), "--at", at]
        if limit_s is not None:
            args += ["--limit-s", str(min(limit_s, inner))]
        try:
            proc = bounded_subprocess(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=limit)
            out, code = proc.stdout, proc.returncode
        except DockerTimeout as exc:
            out, code = str(exc), -9
        try:
            ack = json.loads(out.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            ack = {"ok": False, "error": out[-300:]}
        ack["requested_utc"] = requested
        ack["acked_utc"] = utc_now()
        ack["seconds"] = round(time.monotonic() - t0, 3)
        self.note("watcher mark", segment=seg.number, label=label, mark_at=at, ok=ack.get("ok"), accepted=ack.get("accepted"), batch=ack.get("batch"))
        if code != 0 or not ack.get("ok") or ack.get("accepted") is False:
            raise DockerError(f"the watcher did not accept mark {label!r}: {ack}")
        if final and not allow_straddle and ack.get("straddled") is not None:
            raise DockerError(f"the final mark {label!r} came back straddled by batch {ack.get('straddled')}; the agent's container was stopped")
        return ack

    def stop_watcher(self, seg: Segment | None = None) -> dict[str, Any] | None:
        """`docker stop` the segment's watcher, which then records what is
        queued, runs its final metric and exits. Records whether it shut down
        cleanly: a successful inspect saying it exited with status 0, and its
        heartbeat saying stopped with nothing still changing. Idempotent once
        clean; an unclean stop is recorded and may be retried."""
        seg = seg or self.segments[-1]
        if seg.watcher is None or self.watcher_spec is None:
            return None
        if seg.watcher_stop is not None and seg.watcher_stop.get("clean"):
            return seg.watcher_stop
        rec: dict[str, Any] = {}
        try:
            proc = run(["stop", "-t", str(self.watcher_spec.stop_timeout_s), seg.watcher], check=False, timeout=self.watcher_spec.stop_timeout_s + 60)
            rec["stop_exit"] = proc.returncode
            ins = run(["inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", seg.watcher], check=False, timeout=60)
            parts = ins.stdout.split()
            rec["inspect_ok"] = ins.returncode == 0 and len(parts) == 2
            rec["running"] = parts[0] == "true" if rec["inspect_ok"] else None
            rec["exit_code"] = parts[1] if rec["inspect_ok"] else None
            assert seg.watcher_out is not None
            self.write(seg.watcher_out.parent / f"segment-{seg.number}.log", Container(seg.watcher, self.images.watcher).logs())
        except (DockerError, OSError) as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        hb: dict[str, Any] = {}
        try:
            hb = json.loads((seg.watcher_out / "heartbeat.json").read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        # `still_changing`: inotify events arrived after the final batch was
        # closed, so the log may miss the last writes. That is not a clean stop.
        rec.update({"heartbeat_status": hb.get("status"), "still_changing": hb.get("still_changing"), "late_events": hb.get("late_events"),
                    "late_overflow": hb.get("late_overflow"), "late_capped": hb.get("late_capped"), "final_settled": hb.get("final_settled"),
                    "metric_isolation": hb.get("metric_isolation")})
        rec["clean"] = (rec.get("inspect_ok") is True and rec.get("running") is False and rec.get("exit_code") == "0" and "error" not in rec
                        and hb.get("status") == "stopped" and not hb.get("still_changing") and not hb.get("late_overflow") and not hb.get("late_capped")
                        and hb.get("final_settled") is not False)
        if rec["clean"] or rec.get("running") is False:
            rec["removed"] = self.registry.remove("container", seg.watcher)["confirmed_absent"]
        seg.watcher_stop = rec
        return rec

    # ----- the T3 server inside the container ------------------------------------------

    def stop_server(self, c: Container | None = None, *, timeout_s: float = 30) -> dict[str, Any]:
        """Stop `t3 serve` (and the harness it runs) in a harness container:
        SIGTERM, then SIGKILL after `timeout_s`. `stopped` is True only when a
        `pgrep` that ran said nothing is left. Never raises for a failed
        command; the record says what happened."""
        c = c or self.harness
        rec: dict[str, Any] = {"stopped": False}
        try:
            def left() -> list[str] | None:
                p = c.exec(["pgrep", "-f", "t3 serve"], check=False, timeout=30)
                return p.stdout.split() if p.returncode in (0, 1) else None

            pids = left()
            rec["pids"] = pids
            if pids:
                c.exec(["kill", *pids], check=False, timeout=30)
            end = time.monotonic() + timeout_s
            while time.monotonic() < end:
                now = left()
                if now == []:
                    rec["stopped"] = True
                    return rec
                time.sleep(max(0.0, min(0.3, end - time.monotonic())))
            now = left()
            if now:
                c.exec(["kill", "-9", *now], check=False, timeout=30)
                rec["killed_9"] = True
            rec["stopped"] = left() == []
        except DockerError as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return rec

    def stop_container(self, c: Container | None = None) -> dict[str, Any]:
        """`docker stop` the harness container (every process in it ends) and
        confirm, by a successful inspect, that it is not running. Raises
        DockerError otherwise. It stays registered until removal."""
        c = c or self.harness
        proc = run(["stop", "-t", "10", c.name], check=False, timeout=60)
        return {"stop_exit": proc.returncode, **require_stopped(c.name)}

    def stop_site(self, site: Site | None = None) -> dict[str, Any]:
        """`docker stop` the site's gateway and scripted server, so their logs
        are final, each confirmed stopped by a successful inspect (DockerError
        otherwise). They stay registered until removal."""
        site = site or self.site
        assert site is not None
        out: dict[str, Any] = {}
        for role, c in (("gateway", site.gateway), ("scripted_server", site.scripted)):
            if c is None:
                continue
            proc = run(["stop", "-t", "10", c.name], check=False, timeout=60)
            out[role] = {"stop_exit": proc.returncode, **require_stopped(c.name)}
        return out

    # ----- handoff (D-010) ---------------------------------------------------------------

    def handoff(self, *, on_stopped: Callable[[Container], Any] | None = None) -> dict[str, Any]:
        """The caller has passed the idle gate. In order: stop T3; stop the
        container; ask the watcher for a final mark; key-scan the container;
        `on_stopped(container)` (the caller's capture from the stopped
        container, whose failure fails the handoff); remove the container;
        stop the watcher; clone the volume; remove T3's checkpoint refs in
        every repository of the clone only (the original volume stays as it
        was until close); start a watcher on the clone; start a fresh harness
        container on the same site; prepare; check."""
        old = self.segments[-1]
        rec: dict[str, Any] = {"from_segment": old.number}
        t0 = time.monotonic()
        stage = "stop server"
        try:
            rec["stop_server"] = self.stop_server(old.container)
            if not rec["stop_server"]["stopped"]:
                raise GateFailed("t3 serve did not stop", rec["stop_server"])
            # The watcher's contract: the agent's container is stopped before the
            # final mark, so no write can race it.
            stage = "stop container"
            rec["container_stopped"] = self.stop_container(old.container)
            if old.watcher is not None:
                stage = "final mark"
                rec["final_mark"] = self.mark(f"handoff-{old.number}", final=True)
            stage = "key scan"
            rec["key_scan"] = self.key_scan(old.container)
            if on_stopped is not None:
                stage = "capture"
                on_stopped(old.container)
            stage = "server log"
            rec["server_log"] = self.save_server_log(old.container, self.out_dir / f"server-log-segment-{old.number}.txt")
            stage = "remove container"
            rec["container_removed"] = self.registry.remove("container", old.container.name)
            if not rec["container_removed"]["confirmed_absent"]:
                raise DockerError(f"container {old.container.name} was not removed")
            old.ended_utc = utc_now()
            stage = "stop watcher"
            rec["watcher_stop"] = self.stop_watcher(old)
            if rec["watcher_stop"] is not None and not rec["watcher_stop"]["clean"]:
                raise GateFailed("the watcher did not shut down cleanly", rec["watcher_stop"])
            stage = "clone"
            new_vol = volume.create(self.registry)
            self.names[new_vol] = f"workspace volume, segment {old.number + 1}"
            volume.clone(old.volume, new_vol, self.images.harness, self.registry)
            rec["clone_equal"] = volume.listing(old.volume, self.images.harness, self.registry) == volume.listing(new_vol, self.images.harness, self.registry)
            if not rec["clone_equal"]:
                raise GateFailed("the cloned volume differs from the original", {})
            stage = "git cleanup"
            w = volume.worker(new_vol, self.images.harness, self.registry, path=self.workspace_path)
            try:
                repos = gitclean.repositories(w, self.workspace_path)
                rec["repositories"] = repos
                if not repos["ok"]:
                    raise GateFailed("a repository in the workspace could not be read", repos)
                rec["git"] = {r: gitclean.drop_t3_refs(w, r) for r in repos["repositories"]}
            finally:
                self.registry.remove("container", w.name)
            bad = {r: g for r, g in rec["git"].items() if not g["ok"]}
            if bad:
                raise GateFailed("T3 checkpoint cleanup failed", bad)
            rec["seconds_to_clone_cleaned"] = round(time.monotonic() - t0, 2)
            stage = "new segment"
            seg = self._start_segment(old.number + 1, new_vol)
        except Exception as exc:
            raise HandoffFailed(stage, exc) from exc
        rec["seconds_to_container_ready"] = round(time.monotonic() - t0, 2)
        rec["to_segment"] = seg.number
        self.note("handoff", **{k: rec[k] for k in ("from_segment", "to_segment", "seconds_to_clone_cleaned", "seconds_to_container_ready")})
        return rec

    # ----- key scans -----------------------------------------------------------------------

    def save_server_log(self, c: Container, path: Path) -> dict[str, Any]:
        """The container's `docker logs`, scanned for every key, then
        written. A KeyLeak propagates; a failed read is returned, not written."""
        try:
            text = c.logs()
        except DockerError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        self.write(path, text)
        return {"ok": True, "bytes": len(text)}

    def key_scan(self, c: Container, key=None) -> dict[str, Any] | None:
        """With a key (the real upstream): the harness container's filesystem
        (`docker export`), the workspace volume's content (`docker cp`, since an
        export leaves mounted volumes out) and `docker inspect` must not hold
        it. Raises KeyLeak. Nothing to scan for without a key."""
        key = key or self.upstream.key
        if key is None:
            return None
        needle = key.secret.encode()
        exp = scan_command(["export", c.name], {"key": needle})
        vol = scan_command(["cp", f"{c.name}:{self.workspace_path}", "-"], {"key": needle})
        inspect_hit = key.secret in run(["inspect", c.name], timeout=60).stdout
        out = {"container": c.name, "export_bytes": exp["bytes"], "export_ok": scan_ok(exp), "volume_bytes": vol["bytes"], "volume_ok": scan_ok(vol), "inspect_clean": not inspect_hit}
        self.note("key scan", **out)
        if not (out["export_ok"] and out["volume_ok"] and out["inspect_clean"]):
            raise KeyLeak(f"the key scan of {c.name} did not pass")
        return out

    # ----- probe copy (D-009) ----------------------------------------------------------------

    def probe_copy(self, *, upstream: Upstream) -> "ProbeCopy":
        """Capture the main harness container at a turn boundary the caller has
        checked, and start the copy on its own site.

        The main container is paused (`docker pause`, which freezes every
        process in it, detached ones included) while it is committed and its
        volume cloned, and the clone's listing must equal the original's
        taken in the same pause. Reading the volume for that listing hashes
        every file; the pause and those reads are the capture's observable
        effect on the main container (see the README). The unpause is
        checked and retried; a main container that stays paused raises
        MainNotResumed, the one failure here that is not a probe outcome.

        Before the copy starts, the committed image (`docker save`, every
        layer) and the cloned volume (every file, one decoding level down,
        and every git object) are scanned for each key in play. The copy has
        its own registry. Any other failure removes what the copy made, as
        far as removal succeeds (what is left stays registered and close()
        retries it), and raises ProbeUnavailable naming the stage."""
        assert self.site is not None
        n = len(self.copies) + 1
        copy = ProbeCopy(self, n)
        self.copies.append(copy)
        reg = copy.registry
        main = self.segments[-1]
        t0 = time.monotonic()
        stage = "capture"
        pause_attempted = False
        try:
            tag = reg.add("image", f"cohbench-copy:{opaque_name()}")
            copy.image = tag
            vol = volume.create(reg)
            copy.volume = vol
            self.names[vol] = f"probe copy {n} volume"
            # From here on, anything that does not end with the main container
            # confirmed running and unpaused raises MainNotResumed.
            pause_attempted = True
            try:
                run(["pause", main.container.name], timeout=60)
                run(["commit", "--pause=false", main.container.name, tag], timeout=600)
                volume.clone(main.volume, vol, self.images.harness, reg)
                same = volume.listing(main.volume, self.images.harness, reg) == volume.listing(vol, self.images.harness, reg)
            finally:
                # Recovery has its own limit, outside the call's deadline,
                # which the capture may have used up.
                with no_deadline():
                    copy.unpause = self._resume(main.container)
            copy.capture = {"paused": True, "clone_equal": same, "seconds": round(time.monotonic() - t0, 2), "unpause": copy.unpause}
            if not copy.unpause["resumed"]:
                # Nothing of the copy runs while the main container may be paused.
                copy.close_quietly()
                raise MainNotResumed(f"the main container is not confirmed running and unpaused after the capture: {copy.unpause}")
            if not same:
                raise ProbeUnavailable("capture", "the clone differs from the paused main volume")
            stage = "snapshot key scan"
            keys = [k for k in (self.upstream.key, upstream.key) if k is not None]
            keys = [k for i, k in enumerate(keys) if all(k.secret != o.secret for o in keys[:i])]
            copy.snapshot_scan = []
            for k in keys:
                img = scan_image_save(tag, k)
                files = volume.scan_files(vol, self.images.harness, reg, k)
                git = volume.scan_git_objects_volume(vol, self.images.harness, reg, {"key": k.secret.encode()})
                rec = {"image_ok": scan_ok(img) and not img["undecodable"] and img["error"] is None, "image_bytes": img["bytes"],
                       "files_ok": files["ok"], "files": files["members"], "git_ok": git["ok"], "git_repositories": git["repositories"]}
                copy.snapshot_scan.append(rec)
                if not (rec["image_ok"] and rec["files_ok"] and rec["git_ok"]):
                    leak = img["hits"]["key"] or files["found"] or any(git["hits"].values())
                    raise ProbeUnavailable("snapshot key scan", {"key_found": bool(leak), **rec})
            stage = "site"
            copy.site = start_site(f"copy-{n}", upstream, self.out_dir / f"site-copy-{n}", reg, gateway_image=self.images.gateway, scripted_image=self.images.scripted)
            for role, name in copy.site.names.items():
                self.names[name] = f"probe copy {n} {role.replace('_', ' ')}"
            stage = "container"
            copy.container = start_harness(tag, copy.site, vol, reg, workspace_path=self.workspace_path)
            self.names[copy.container.name] = f"probe copy {n} harness container"
            stage = "prepare"
            if self.prepare is not None:
                self.prepare(copy.container, "copy")
            stage = "checks"
            copy.checks = self.check(copy.container, copy.site, baseline=main.name_baseline, text_baseline=main.text_baseline, extra_names=[vol])
        except MainNotResumed:
            raise
        except BaseException as exc:
            copy.close_quietly()
            if pause_attempted and not (copy.unpause or {}).get("resumed"):
                raise MainNotResumed(f"the main container is not confirmed running and unpaused after the capture: {copy.unpause}") from exc
            if isinstance(exc, ProbeUnavailable):
                raise
            if isinstance(exc, Exception):
                raise ProbeUnavailable(f"{stage} failed", f"{type(exc).__name__}: {exc}") from exc
            raise
        copy.seconds_to_ready = round(time.monotonic() - t0, 2)
        self.note("probe copy ready", copy=n, seconds=copy.seconds_to_ready)
        return copy

    RESUME_S = 60.0

    def _resume(self, c: Container, attempts: int = 5) -> dict[str, Any]:
        """`docker unpause`, confirmed by a successful inspect saying running
        and not paused, retried; under its own limit (RESUME_S), never the
        caller's deadline. Never raises: `resumed` False says it could not
        confirm. Also right when the pause itself failed or its result is
        unknown: an unpause of a running container fails and the inspect
        decides."""
        last = ""
        end = time.monotonic() + self.RESUME_S
        for i in range(attempts):
            try:
                with no_deadline():
                    with docker_deadline(end):
                        proc = run(["unpause", c.name], check=False, timeout=30)
                        st = container_state(c.name)
                if st["running"] and not st["paused"]:
                    return {"resumed": True, "attempts": i + 1}
                last = (proc.stderr.strip() or str(st))[:200]
            except DockerError as exc:
                last = str(exc)[:200]
            if time.monotonic() + 1 >= end:
                break
            time.sleep(1)
        return {"resumed": False, "attempts": i + 1, "last": last}

    # ----- close ----------------------------------------------------------------------------

    def close(self) -> list[dict]:
        """Stop the harness containers first, so nothing writes after the
        watchers' last batch, then the watchers, then remove every registered
        resource (probe copies included) by exact name. Always safe to call."""
        with self._lock:
            again = self.closed
            self.closed = True
        if again:
            return self._retry_removal()
        with no_deadline():
            return self._close()

    def _close(self) -> list[dict]:
        leak: BaseException | None = None
        try:
            for seg in self.segments:
                if ("container", seg.container.name) in self.registry.items():
                    try:
                        run(["unpause", seg.container.name], check=False, timeout=60)
                        self.key_scan(seg.container)
                        self.save_server_log(seg.container, self.out_dir / f"server-log-segment-{seg.number}.txt")
                    except KeyLeak as exc:
                        leak = exc
                    except Exception as exc:
                        self.note("end of segment record incomplete", segment=seg.number, error=str(exc)[:200])
                    run(["stop", "-t", "10", seg.container.name], check=False, timeout=60)
            for seg in self.segments:
                try:
                    self.stop_watcher(seg)
                except KeyLeak as exc:
                    leak = exc
                except Exception as exc:
                    self.note("watcher stop failed", segment=seg.number, error=str(exc)[:200])
        finally:
            results: list[dict] = []
            with no_deadline():
                for copy in self.copies:
                    try:
                        results += copy.close()
                    except KeyLeak as exc:
                        leak = exc
                        results += copy.removal
                results += self.registry.remove_all()
            self.removal = results
            self.note("closed", removed=len(results), not_removed=[r for r in results if not r["confirmed_absent"]])
            try:
                self.write_record()
            except KeyLeak as exc:
                leak = exc
            except Exception as exc:
                self.note("environment record not written", error=str(exc)[:200])
            for cb in self.on_closed:
                try:
                    cb(self)
                except Exception as exc:
                    self.note("close callback failed", error=str(exc)[:200])
        if leak is not None:
            raise leak
        return results

    def _retry_removal(self) -> list[dict]:
        """A later close: retry whatever is still registered (probe copies'
        leftovers first), under no deadline."""
        results: list[dict] = []
        with no_deadline():
            for copy in self.copies:
                results += copy.close_quietly()
            results += self.registry.remove_all()
        self.removal += results
        try:
            self.write_record()
        except Exception as exc:
            self.note("environment record not written", error=str(exc)[:200])
        for cb in self.on_closed:  # republish the record where it was published
            try:
                cb(self)
            except Exception as exc:
                self.note("close callback failed", error=str(exc)[:200])
        return results

    @property
    def left_over(self) -> list[tuple[str, str]]:
        """Everything still registered, probe copies included."""
        return self.registry.items() + [x for c in self.copies for x in c.left_over]

    def record(self) -> dict[str, Any]:
        return {
            "started_utc": self.started_utc,
            "upstream": self.upstream.kind,
            "provider": self.upstream.provider.name,
            "workspace_path": self.workspace_path,
            "workspace": self.workspace_record,
            "images": {"harness": self.images.harness, "gateway": self.images.gateway, "scripted": self.images.scripted, "watcher": self.images.watcher},
            "network_mode": checks.network_mode(),
            "docker_names": dict(self.names),
            "instruction": self.instruction_record,
            "segments": [
                {"segment": s.number, "container": s.container.name, "volume": s.volume, "watcher": s.watcher, "watcher_out": str(s.watcher_out) if s.watcher_out else None,
                 "started_utc": s.started_utc, "ended_utc": s.ended_utc, "checks": _check_summary(s.checks), "watcher_stop": s.watcher_stop}
                for s in self.segments
            ],
            "copies": [c.record() for c in self.copies],
            "events": self.events,
            "removal": self.removal,
            "left_over": self.left_over,
            "closed": self.closed,
        }

    def write_record(self) -> Path:
        """environment.json: scanned for every key, written to a temporary
        file and renamed over the old one."""
        return self.write(self.out_dir / "environment.json", json.dumps(self.record(), indent=1, default=str))


def _check_summary(g: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "verify" in g:
        out["verify"] = g["verify"]["summary"]
    if "bridge" in g:
        out["bridge"] = g["bridge"].get("ports")
    if "identity" in g:
        out["identity"] = {k: g["identity"].get(k) for k in ("ok", "reverse", "mount_points", "processes", "t3_own_process_names_allowed")}
    if "names" in g:
        out["names"] = {k: g["names"].get(k) for k in ("ok", "entries", "written_since_start", "settings_changed_since_start", "dirs", "files")}
    return out


class ProbeCopy:
    """One probe copy: its own registry, site, container, volume and image."""

    def __init__(self, env: Environment, number: int):
        self.env = env
        self.number = number
        self.registry = Registry()
        self.site: Site | None = None
        self.container: Container | None = None
        self.volume: str | None = None
        self.image: str | None = None
        self.capture: dict[str, Any] = {}
        self.checks: dict[str, Any] = {}
        self.seconds_to_ready: float | None = None
        self.unpause: dict[str, Any] | None = None
        self.snapshot_scan: list[dict] | None = None
        self.closed = False
        self.removal: list[dict] = []
        self.close_problems: list[str] = []
        self.key_leak = False
        self._logged = False

    def close(self) -> list[dict]:
        """Key-scan the copy's container, save its server log (scanned), and
        remove everything the copy made, by name. Never raises except
        KeyLeak (after removal). What could not be removed stays registered:
        calling close again, as Environment.close does, retries it."""
        leak: KeyLeak | None = None
        if not self._logged:
            self._logged = True
            try:
                if self.container is not None and self.site is not None:
                    if self.site.upstream.key is not None:
                        self.env.key_scan(self.container, self.site.upstream.key)
                    self.env.save_server_log(self.container, self.env.out_dir / f"site-copy-{self.number}" / "server-log.txt")
            except KeyLeak as exc:
                leak = exc
            except Exception as exc:
                self.close_problems.append(f"{type(exc).__name__}: {exc}"[:300])
        try:
            results = self.registry.remove_all() if self.registry.items() else []
        except Exception as exc:  # remove_all records per item; this is a last guard
            results = []
            self.close_problems.append(f"removal: {type(exc).__name__}: {exc}"[:300])
        self.removal += results
        self.closed = not self.registry.items()
        if leak is not None:
            raise leak
        return results

    def close_quietly(self) -> list[dict]:
        """`close` outside any deadline, for error paths: a KeyLeak is
        recorded (`key_leak`) instead of raised."""
        with no_deadline():
            try:
                return self.close()
            except KeyLeak as exc:
                self.key_leak = True
                self.close_problems.append(f"KeyLeak: {exc}"[:300])
                return []

    @property
    def left_over(self) -> list[tuple[str, str]]:
        return self.registry.items()

    def record(self) -> dict[str, Any]:
        return {"copy": self.number, "container": self.container.name if self.container else None, "volume": self.volume, "image": self.image,
                "upstream": self.site.upstream.kind if self.site else None, "capture": self.capture, "snapshot_scan": self.snapshot_scan,
                "seconds_to_ready": self.seconds_to_ready, "checks": _check_summary(self.checks), "removal": self.removal,
                "close_problems": self.close_problems, "left_over": self.left_over}


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def start_harness(image: str, site: Site, vol: str, registry: Registry, *, workspace_path: str = WORKSPACE) -> Container:
    """Create the harness container on the site's agent network with the
    workspace volume at the project path, copy the CA in, then start it. No
    bind mounts. Any failure removes what was created."""
    name = registry.add("container", opaque_name())
    p = site.upstream.provider
    args = [
        "create", "--init", "--name", name, "--hostname", CONTAINER_HOSTNAME,
        "--network", site.agent_network,
        "--add-host", f"{p.api_host}:{site.gateway_ip}",
        "-e", f"NODE_EXTRA_CA_CERTS={CA_PATH}",
        "-e", f"{p.key_env}={p.placeholder()}",
        "--mount", f"type=volume,src={vol},dst={workspace_path},volume-nocopy",
        image,
    ]
    try:
        run(args, timeout=120)
        cp_in(name, str(Path(CA_PATH).parent), one_file_tar(Path(CA_PATH).name, site.ca_pem))
        run(["start", name], timeout=120)
    except BaseException:
        registry.remove("container", name)
        raise
    return Container(name, image)


def start_watcher(vol: str, out: Path, spec: WatcherSpec, image: str, registry: Registry, *, timeout_s: float = 90) -> str:
    """The watcher on the volume, as cohbench/watcher/README.md ("Docker")
    and docker/watcher/Dockerfile require: started as root with every
    capability dropped except SETUID and SETGID (its entry point becomes the
    reader 1000:1000 and forks the metric server as 1500:1500), no `--init`
    and no `--user`, no network, read-only root, `/tmp` a tmpfs with exec,
    the volume read-only at /srv/w/live with volume-nocopy, the output a host
    bind at /srv/w/out, the task's metric files read-only at /opt/task. The
    D-026 lists are passed explicitly. Returns once its heartbeat file says
    `running`, fresh (`watcher alive`), with no `event_log_broken` and the
    baseline written, within `timeout_s` capped by the current deadline. The
    image has no container health check (D-036)."""
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o777)
    name = registry.add("container", opaque_name())
    args = [
        "run", "-d", "--name", name, "--network", "none", "--read-only", "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={spec.tmp_size}",
        "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
        "--mount", f"type=volume,src={vol},dst={WATCHER_LIVE},readonly,volume-nocopy",
        "--mount", f"type=bind,src={out.resolve()},dst={WATCHER_OUT}",
    ]
    if spec.task_dir is not None:
        args += ["--mount", f"type=bind,src={Path(spec.task_dir).resolve()},dst=/opt/task,readonly"]
    args += [image, "run", "--root", WATCHER_LIVE, "--out", WATCHER_OUT, "--quiet-ms", str(spec.quiet_ms)]
    for pattern in spec.ignore:
        args += ["--ignore", pattern]
    if spec.ignore_names is None or spec.ignore_git_refs is None:
        raise ValueError("WatcherSpec.ignore_names and ignore_git_refs must be given (D-026); the driver fills them from its harness descriptions")
    args += ["--ignore-names", json.dumps(list(spec.ignore_names)), "--ignore-git-refs", json.dumps(list(spec.ignore_git_refs)),
             "--workspace-path", spec.workspace_path, "--metric-isolation", spec.metric_isolation]
    if spec.metric_cmd:
        args += ["--metric-cmd", json.dumps(list(spec.metric_cmd))]
    if spec.metric_timeout_s is not None:
        args += ["--metric-timeout-s", str(spec.metric_timeout_s)]
    try:
        run(args, timeout=120)
    except BaseException:
        registry.remove("container", name)
        raise
    end = time.monotonic() + bounded(timeout_s)
    last = ""
    while time.monotonic() < end:
        # `running` is written after the baseline and its metric reading, so
        # the agent's container starts only then. Readiness comes from the
        # heartbeat file and a successful inspect, never from Docker health.
        try:
            hb = json.loads((out / "heartbeat.json").read_text())
        except (OSError, json.JSONDecodeError):
            hb = {}
        status = hb.get("status")
        # D-036: no container health check (Docker's timed exec walks the
        # volume, which the agent could observe); the heartbeat file says it.
        left = max(0.1, end - time.monotonic())
        alive = bounded_subprocess([sys.executable, "-m", "cohbench.watcher", "alive", str(out), "--max-age", "5"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=left)
        ins = run(["inspect", "-f", "{{.State.Running}}", name], check=False, timeout=max(0.1, end - time.monotonic()))
        running = ins.stdout.strip() if ins.returncode == 0 else "unknown"
        # Ready: the container confirmed running by a successful inspect, and a
        # fresh heartbeat saying running, with the baseline written.
        if running == "true" and status == "running" and alive.returncode == 0 and not hb.get("event_log_broken") and _baseline_written(out / "events.jsonl"):
            if spec.metric_cmd and hb.get("metric_isolation") != spec.metric_isolation:
                raise DockerError(f"the watcher's metric isolation is {hb.get('metric_isolation')!r}, not {spec.metric_isolation!r}")
            return name
        if status == "failed" or running == "false" or hb.get("event_log_broken"):
            break
        last = f"heartbeat {status}, alive exit {alive.returncode}, running {running}, event_log_broken {hb.get('event_log_broken')}"
        time.sleep(max(0.0, min(0.5, end - time.monotonic())))
    try:
        logs = Container(name, image).logs()[-1500:] if time.monotonic() < end else "(not read: the time is up)"
    except DockerError:
        logs = "(not read)"
    raise DockerError(f"watcher did not come up: {last}\n{logs}")


def _baseline_written(events: Path) -> bool:
    """The watcher's baseline is complete: its log holds the closing baseline
    record (`kind: baseline`, or a `batch` record with `reason: baseline`)."""
    if not events.exists():
        return False
    for line in events.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line still being written
        if rec.get("kind") == "baseline" or (rec.get("kind") == "batch" and rec.get("reason") == "baseline"):
            return True
    return False


def gateway_episode_usage(log_dir: Path) -> dict[str, Any]:
    """`python -m gateway summary --episode` over a gateway log, as a dict."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from gateway.summary import episode, read_log  # noqa: PLC0415

    path = Path(log_dir) / "requests.jsonl"
    return episode(read_log(str(path))) if path.exists() else episode([])
