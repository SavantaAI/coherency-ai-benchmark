"""The running watcher: inotify, the core, the metric hook, the control
channel and the heartbeat."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import tempfile
import threading
import time
import traceback

from .blobs import BlobStore
from .config import Config
from .core import Core
from .inotify import Inotify
from .metrics import LocalExecutor, MetricRunner
from .records import CONFIG, HEARTBEAT, EventLog, MetricsLog, parse_utc, utc_from_epoch, utc_now, write_json_atomic

CONTROL = "control"
REQUEST = ".request.json"
ACK = ".ack.json"
CONTROL_POLL_S = 0.25
MARK_LIMIT_S = 10.0  # default limit for answering a mark; above the hold limit


class Watcher:
    def __init__(self, cfg: Config, backend=None, executor=None) -> None:
        cfg.validate()
        self.cfg = cfg
        os.makedirs(cfg.out, exist_ok=True)
        self.out = cfg.out
        self.control = os.path.join(cfg.out, CONTROL)
        os.makedirs(self.control, exist_ok=True)
        self.events = EventLog(cfg.out)
        self.blobs = BlobStore(cfg.out)
        self.inotify = backend if backend is not None else Inotify()
        self.core = Core(cfg, self.inotify, self.events, self.blobs)
        self.metrics: MetricRunner | None = None
        self._temp_work: str | None = None
        self.metrics_log: MetricsLog | None = None
        self.metric_isolation = None
        if cfg.metric_cmd:
            if executor is None:
                if cfg.metric_isolation == "users":
                    raise RuntimeError("metric_isolation is users but no isolated executor was set up (start as root)")
                work = cfg.work_dir
                if work is None:
                    work = self._temp_work = tempfile.mkdtemp(prefix="w-")  # removed at shutdown
                os.makedirs(work, mode=0o700, exist_ok=True)
                executor = LocalExecutor(work, self.blobs, cfg.workspace_path, copy_mtime=cfg.metric_copy_mtime)
            else:
                executor.blobs = self.blobs
                executor.copy_mtime = cfg.metric_copy_mtime
            self.metric_isolation = executor.isolation
            self.metrics_log = MetricsLog(cfg.out)
            self.metrics = MetricRunner(cfg.metric_cmd, cfg.metric_timeout_s, cfg.metric_stdout_max, cfg.metric_stderr_max, self.metrics_log, executor, self.blobs)

            def queue_batch(batch: int, rec: dict) -> None:
                # The snapshot is this batch's own manifest, never changed
                # afterwards (the next commit makes a new one).
                snap = self.core.take_snapshot()
                if snap is None or snap[0] != batch:
                    raise RuntimeError(f"internal: no snapshot of batch {batch}")
                self.metrics.enqueue(batch, snap[1])

            self.core.on_commit = queue_batch
        # config.json records the isolation the metric actually runs with.
        pub = public_config(cfg)
        if self.metric_isolation is not None:
            pub["metric_isolation"] = self.metric_isolation
        write_json_atomic(os.path.join(cfg.out, CONFIG), pub)
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._next_heartbeat = 0.0
        self._next_control = 0.0
        self.status = "starting"
        self.started = utc_now()
        self.error: str | None = None
        self.late_events: int | None = None
        self.late_overflow = False
        self.late_capped = False
        self.quiescent = True
        self.final_settled: bool | None = None
        self.marks = 0
        self._marks: list[dict] = []  # registered, not yet answered
        self.core.on_read = self._on_read

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Baseline, then the baseline metric reading, then status running."""
        self.heartbeat(force=True)
        self.core.baseline()
        # The control directory is polled (every CONTROL_POLL_S), not
        # watched: a watch on it would share the workspace's inotify queue,
        # so a mark request would wake the loop and cause a workspace read
        # (D-037: a mark is a pure observer).
        if self.metrics is not None:
            # The baseline commit queued batch 0; its reading comes first,
            # before the heartbeat says running.
            self.metrics.finish_one()
            self.metrics.start()
        self.status = "running"
        self.heartbeat(force=True)

    def stop(self) -> None:
        self._stop.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def install_signals(self) -> None:
        def handler(signum, frame):
            self.stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self) -> int:
        try:
            self.start()
            while not self._stop.is_set():
                self.step()
                if self.core.fatal:
                    self.error = self.core.fatal
                    break
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}"
        finally:
            self.shutdown()
        return 1 if self.error else 0

    def _read_raw(self) -> int:
        return self.core._read_raw()

    def step(self, max_wait: float | None = None) -> list[dict]:
        now = time.monotonic()
        wait = min(self.cfg.heartbeat_s, CONTROL_POLL_S)
        d = self.core.pending.deadline()
        if self.core.overflowed or self.core.recover_full:
            wait = 0.0
        elif d is not None:
            wait = min(wait, max(0.0, d - now))
        wait = min(wait, max(0.0, self._next_heartbeat - now))
        if max_wait is not None:
            wait = min(wait, max_wait)
        fds = [self._wake_r]
        fd = getattr(self.inotify, "fd", None)
        if isinstance(fd, int) and fd >= 0:
            fds.append(fd)
        try:
            ready, _, _ = select.select(fds, [], [], wait)
        except InterruptedError:
            ready = []
        if self._wake_r in ready:
            try:
                while os.read(self._wake_r, 4096):
                    pass
            except BlockingIOError:
                pass
        out: list[dict] = []
        # A batch already due is committed before the next read, so events
        # read after its deadline never join it.
        if self.core.due():
            out.extend(self.core.tick())
        self._read_raw()
        if self.core.control_hint or time.monotonic() >= self._next_control:
            self.core.control_hint = False
            self._next_control = time.monotonic() + CONTROL_POLL_S
            out.extend(self.process_control())
        if self.core.due():
            out.extend(self.core.tick())
        if self._marks:
            out.extend(self.service_marks())
        if os.getpid() == 1 and self.metric_isolation != "none":
            # Only with isolated metrics: the local executor waits for its own
            # children, and reaping them here would race it.
            self._reap()
        self.heartbeat()
        return out

    @staticmethod
    def _reap() -> None:
        """As PID 1 (the image runs without --init: an init running as root
        without CAP_KILL could not forward signals to the unprivileged
        watcher), collect any orphan that was re-parented here."""
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return

    def drain(self, rounds: int = 100, reason: str | None = None) -> list[dict]:
        """Read everything queued and commit it now, without waiting for it
        to settle. Always scans at least once, so the lstat sweep and the
        validation pass run even with nothing pending. Sets
        self.quiescent: whether the queue was empty when it stopped."""
        out: list[dict] = []
        self.quiescent = False
        for _ in range(rounds):
            n = self._read_raw()
            out.extend(self.core.tick(force=True, reason=reason))
            if not n and not self.core.pending and not self.core.overflowed:
                self.quiescent = True
                break
        return out

    # ---- control channel -------------------------------------------------

    def process_control(self) -> list[dict]:
        """Take every queued mark request: parse it and register the mark."""
        try:
            names = sorted(n for n in os.listdir(self.control) if n.endswith(REQUEST))
        except FileNotFoundError:
            return []
        out: list[dict] = []
        for n in names:
            rid = n[: -len(REQUEST)]
            path = os.path.join(self.control, n)
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
                label = req.get("label")
                if not isinstance(label, str):
                    raise ValueError("request needs a string label")
                at = req.get("at")
                if at is not None:
                    if not isinstance(at, str):
                        raise ValueError("at must be a UTC instant in ISO 8601")
                    at = parse_utc(at)
                limit = req.get("limit_s", MARK_LIMIT_S)
                if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not limit > self.cfg.max_delay_ms / 1000.0:
                    raise ValueError(f"limit_s must be a number of seconds above the hold limit ({self.cfg.max_delay_ms / 1000.0:g} s)")
                unknown = set(req) - {"label", "at", "limit_s"}
                if unknown:
                    raise ValueError(f"unknown request fields {sorted(unknown)}")
            except (OSError, ValueError) as e:
                self._ack(rid, {"id": rid, "ok": False, "accepted": False, "error": f"bad request: {e}"})
                os.replace(path, path + ".rejected")
                continue
            os.replace(path, os.path.join(self.control, rid + ".taken.json"))
            out.extend(self.request_mark(label, rid, at=at, limit_s=float(limit)))
        return out

    # ---- marks (D-037) -----------------------------------------------------

    def request_mark(self, label: str, rid: str | None = None, at: float | None = None, limit_s: float = None) -> list[dict]:
        """Register a mark for the instant `at` (wall seconds; default the
        time of the request). A mark is a pure observer: registering it reads
        nothing, scans nothing and commits nothing. It is answered from the
        regular reader's coverage: once a regular queue read that began at
        or after `at` has returned (and did not stop at its cap) and no open
        batch holds a hint from that read or an earlier one (D-037)."""
        limit_s = MARK_LIMIT_S if limit_s is None else limit_s
        now = self.core.wallclock()
        m = {"label": label, "rid": rid, "at": now if at is None else at, "received": now, "deadline": self.core.clock() + limit_s, "limit_s": limit_s, "read": None}
        self._marks.append(m)
        return []

    def mark(self, label: str, at: float | None = None, limit_s: float | None = None) -> dict:
        """In-process: register a mark and run the loop until it is
        answered. Returns its mark record."""
        out = self.request_mark(label, None, at=at, limit_s=limit_s)
        while True:
            got = [r for r in out if r.get("kind") == "mark" and r.get("label") == label]
            if got:
                return got[-1]
            out = self.step(max_wait=0.02)

    def _on_read(self, start_wall: float, epoch: int) -> None:
        for m in self._marks:
            if m["read"] is None and start_wall >= m["at"]:
                m["read"] = epoch

    def _covered(self, m: dict) -> bool:
        if m["read"] is None:
            return False
        p = self.core.pending
        return not p or (p.first_epoch is not None and p.first_epoch > m["read"])

    def service_marks(self, final: bool = False) -> list[dict]:
        """Answer every registered mark that is covered (all writes up to its
        instant in committed batches), and refuse those past their limit."""
        out: list[dict] = []
        now = self.core.clock()
        for m in list(self._marks):
            # The limit runs from the request to when coverage first held: a
            # mark first found covered after its limit is not accepted.
            if now > m["deadline"]:
                self._marks.remove(m)
                out.extend(self._answer(m, False))
            elif self._covered(m):
                self._marks.remove(m)
                out.extend(self._answer(m, True))
            elif final:
                self._marks.remove(m)
                out.extend(self._answer(m, False))
        return out

    def _named(self, at: float) -> tuple[int, int | None, int | None]:
        """(batch, straddled, straddled_last) for the instant `at`, by the
        rule replay checks (replay.answer_for) over every committed batch's
        bounds: lo is first_write_after (every write of the batch came after
        it), hi is last_write or, without known write times, the commit
        time."""
        from .replay import answer_for

        return answer_for(at, {b: (i["lo"], i["hi"]) for b, i in self.core.batch_info.items()})

    def _answer(self, m: dict, accepted: bool) -> list[dict]:
        rid, label = m["rid"], m["label"]
        covered = self.core.wallclock() if accepted else None
        at = parse_utc(utc_from_epoch(m["at"]))  # as the record holds it
        batch, straddled, straddled_last = self._named(at) if accepted else (None, None, None)
        settled = bool(self.core.batch_info.get(batch, {}).get("settled")) if accepted else False
        rec = {"kind": "mark", "label": label, "request": rid, "accepted": accepted, "at": utc_from_epoch(m["at"]), "batch": batch, "straddled": straddled, "straddled_last": straddled_last, "covered_utc": utc_from_epoch(covered) if covered else None, "settled": settled}
        self.events.stamp(rec)
        self.events.write([rec])
        self.marks += 1
        ack = {"id": rid, "ok": accepted, "accepted": accepted, "label": label, "at": rec["at"], "batch": batch, "straddled": straddled, "straddled_last": straddled_last, "covered_utc": rec["covered_utc"], "settled": settled, "mark_seq": rec["seq"], "wall": rec["wall"], "metric": None}
        if not accepted:
            ack["error"] = f"not covered within limit_s ({m['limit_s']:g} s): a batch holding writes from before the instant was still open"
        if accepted and self.metrics is not None:
            # The mark's reading is the reading of the batch it names
            # (D-036). The acknowledgement never waits for it: it carries the
            # reading only if it is already written; otherwise null, and the
            # reading is found by batch in metrics.jsonl.
            ack["metric"] = self.metrics.reading(batch)
        if rid is not None:
            self._ack(rid, ack)
        return [rec]

    def _ack(self, rid: str, obj: dict) -> None:
        write_json_atomic(os.path.join(self.control, rid + ACK), obj)

    # ---- shutdown ----------------------------------------------------------

    def shutdown(self) -> None:
        """Final scan and batch, final metric reading, then the heartbeat
        says whether events were still arriving."""
        self.status = "stopping"
        try:
            self.heartbeat(force=True)
            if self.core.batch >= 0:
                self.process_control()  # outstanding marks first
                # The segment's end: the one forced close (D-037).
                self.drain(reason="final")
                self.final_settled = bool(self.core.last_commit.get("settled")) and self.quiescent
                self.service_marks(final=True)
                if self.metrics is not None:
                    self.metrics.finish(self.cfg.metric_drain_s)
                self.late_events = self._count_late()
                if self.core.fatal and not self.error:
                    self.error = self.core.fatal
        except Exception as e:
            msg = f"during shutdown: {type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}"
            self.error = msg if self.error is None else self.error + "\n" + msg
        finally:
            self.status = "failed" if self.error else "stopped"
            try:
                self.heartbeat(force=True)
            finally:
                self.events.close()
                if self.metrics_log is not None:
                    self.metrics_log.close()
                if self.metrics is not None:
                    self.metrics.executor.close()
                if self._temp_work is not None:
                    from .metrics import clear_dir

                    try:
                        clear_dir(self._temp_work)
                        os.rmdir(self._temp_work)
                    except OSError:
                        pass
                close = getattr(self.inotify, "close", None)
                if close:
                    close()
                self.core.close()
                for fd in (self._wake_r, self._wake_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _count_late(self, max_reads: int = 16) -> int:
        """Tree events (overflow included) that arrived after the final batch
        was closed, reading at most max_reads buffers so continued writes
        cannot hold shutdown. A fatal root event among them is recorded."""
        from . import inotify as ino

        n = 0
        for _ in range(max_reads):
            raws = self.inotify.read()
            if not raws:
                return n
            for ev in raws:
                if ev.mask & ino.IN_Q_OVERFLOW:
                    self.late_overflow = True
                    n += 1
                    continue
                info = self.core.wd_info.get(ev.wd)
                if info is None or info[2] == "control":
                    continue
                if info[0] == "" and info[2] == "tree" and not ev.name and ev.mask & (ino.IN_DELETE_SELF | ino.IN_MOVE_SELF | ino.IN_UNMOUNT | ino.IN_IGNORED):
                    self.core.fatal = "the root was deleted, moved or unmounted (seen during shutdown)"
                    n += 1
                    continue
                rel = f"{info[0]}/{ev.name}" if info[0] else ev.name
                if ev.name and (self.core.excluded(rel) or (info[2] == "git" or self.core.is_git(rel)) and not self.core.git_relevant(rel)):
                    continue  # ignored activity is not "still changing"
                n += 1
        self.late_capped = True
        return n

    # ---- heartbeat ---------------------------------------------------------

    def heartbeat(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_heartbeat:
            return
        self._next_heartbeat = now + self.cfg.heartbeat_s
        hb = {
            "status": self.status,
            "pid": os.getpid(),
            "started": self.started,
            "wall": utc_now(),
            "mono": round(now, 6),
            "batch": self.core.batch,
            "last_seq": self.events.written_seq,
            "records": self.events.count,
            "entries": len(self.core.manifest.entries),
            "watches": len(self.core.wd_info),
            "pending_paths": len(self.core.pending.dirty),
            "overflows": self.core.overflows,
            "resyncs": self.core.resyncs,
            "marks": self.marks,
            "unwatched": dict(self.core.unwatched),
            "metric_runs": self.metrics.runs if self.metrics else 0,
            "metric_failures": self.metrics.failures if self.metrics else 0,
            "metric_reused": self.metrics.reused if self.metrics else 0,
            "metric_queue": self.metrics.queued() if self.metrics else 0,
            "metric_backlog": len(self.metrics.backlog) if self.metrics else 0,
            "resolve": self.core.root.method,
            "late_events": self.late_events,
            "late_overflow": self.late_overflow,
            "late_capped": self.late_capped,
            "still_changing": (self.late_events > 0) if self.late_events is not None else None,
            "final_settled": self.final_settled,
            "metric_isolation": self.metric_isolation,
            "event_log_broken": self.events.broken,
            "error": self.error,
        }
        write_json_atomic(os.path.join(self.out, HEARTBEAT), hb)


def public_config(cfg: Config) -> dict:
    """The effective configuration as written to config.json. The ignored
    names and ref namespaces appear only as hashes (D-026: nothing the
    watcher writes names them)."""
    import hashlib

    d = cfg.to_json()
    for key in ("ignore_names", "ignore_git_refs"):
        vals = d.pop(key)
        d[key + "_sha256"] = sorted(hashlib.sha256(v.encode()).hexdigest() for v in vals)
    return d


def main_run(cfg: Config) -> int:
    executor = None
    mode = cfg.metric_isolation
    if mode == "auto":
        mode = "users" if os.geteuid() == 0 else "none"
    if os.geteuid() == 0:
        if mode != "users":
            raise RuntimeError("started as root: metric_isolation must be users (or start as the watcher user)")
        from .isolate import launch

        executor = launch(cfg.root, cfg.out, bool(cfg.metric_cmd or cfg.metric_cmd_file), cfg.watcher_user, cfg.metric_user, cfg.workspace_path)
    elif mode == "users":
        raise RuntimeError("metric_isolation users needs the watcher to start as root with SETUID and SETGID")
    if cfg.metric_cmd_file is not None:
        path = cfg.metric_cmd_file
        try:
            with open(path, encoding="utf-8") as f:
                cfg.metric_cmd = json.load(f)
        finally:
            os.unlink(path)
        cfg.metric_cmd_file = None
    cfg.metric_isolation = mode
    w = Watcher(cfg, executor=executor)
    w.install_signals()
    code = w.run()
    if w.error:
        print(f"watcher: {w.error}", file=sys.stderr)
    return code
