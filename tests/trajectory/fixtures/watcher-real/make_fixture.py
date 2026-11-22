"""Make the real watcher fixture: run `python -m cohbench.watcher` over a short
scripted sequence inside a Linux container and keep its output verbatim.

    uv run python tests/trajectory/fixtures/watcher-real/make_fixture.py

On the host it builds the watcher's Linux test image
(`tests/watcher/linux.Dockerfile`) under its own tag, runs one container
named `ctraj-fixture-<pid>` with no network, as uid 1000, the repository
mounted read-only, and removes that container by name at the end. Inside
the container (`--inside OUT`) it creates a workspace, starts a watcher on
it, performs the steps below at fixed offsets from the episode start, stops
the watcher, copies the tree, starts a second watcher on the copy (the
two-segment case of a controlled reset), performs more steps, and stops it.
The host then replaces `segment-1/`, `segment-2/` and `sequence.json` next to
this script.

Segment 1, seconds after the episode start (`sequence.json` has the UTC time
of every step):

  baseline  data/a.csv, README.md, CLAUDE.md (not recorded, D-026), a git
            repository with refs/heads/main and a driver ref under refs/t3/
            (not recorded)
  3.5       tmp/c0ffee12ab.bin created
  6.1       out/summary.txt created
  7.0       out/.report.txt.tmp written; renamed to out/report.txt once the
            watcher has committed it (a temp file across two batches), and
            a driver ref is added (not recorded)
  9.0       mark m-1
  11.0      data/a.csv appended in place
  13.0      out/summary.txt rewritten with the same bytes
  15.0      out/report.txt replaced through the reused temp name
            out/.report.txt.tmp within one batch
  17.0      the same again, the rename waiting until the temp file is
            committed (across two batches, over an existing file)
  20.0      tmp/c0ffee12ab.bin deleted, out/summary.txt moved to
            out/final.txt, big.bin (over the blob limit) created, CLAUDE.md
            modified, then rewritten through .CLAUDE.md.tmp, and a
            CLAUDE.md~ backup written (none recorded: names containing an
            ignored name are ignored too)
  22.0      mark reset-1, then the handoff: the watcher stops, the tree is
            copied, a second watcher starts on the copy

Segment 2, seconds after the handoff ends:

  2.0       c.txt created; data/a.csv rewritten in place
  4.0       p.txt and q.txt created
  6.0       p.txt and q.txt swapped through a temp name in one batch (a
            rename cycle: the watcher writes a resync)
  8.0       locked/f created and locked/ made mode 000 (inaccessible)
  10.0      mark end-1 (the episode end); then post.txt is written after
            the end, and the watcher stops
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
IMAGE = "ctraj-fixture-watcher:dev"
# D-026: the watcher leaves the harness's instruction files (CLAUDE.md,
# AGENTS.md at any depth) and git refs under refs/t3/ out by its own default
# ignore rules; the script writes both, to show they never reach the log.
METRIC = """import json, os
files = size = 0
for d, ds, fs in os.walk("."):
    ds[:] = sorted(x for x in ds if x != ".git")
    for f in fs:
        files += 1
        size += os.lstat(os.path.join(d, f)).st_size
print(json.dumps({"files": files, "bytes": size}))
"""


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# Inside the container -------------------------------------------------------


class Watcher:
    def __init__(self, root: Path, out: Path, work: Path) -> None:
        self.out = out
        cmd = [sys.executable, "-m", "cohbench.watcher", "run", "--root", str(root), "--out", str(out), "--work-dir", str(work),
               "--max-blob-bytes", "4096", "--metric-cmd", json.dumps([sys.executable, "/tmp/task/metric.py"])]
        self.p = subprocess.Popen(cmd, cwd="/src", stdout=subprocess.DEVNULL, stderr=open(out.parent / f"{out.name}.stderr", "w"))
        deadline = time.monotonic() + 60
        while self.status() != "running":
            if self.p.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"watcher did not start: {(out.parent / f'{out.name}.stderr').read_text()[-2000:]}")
            time.sleep(0.05)

    def status(self) -> str | None:
        try:
            return json.loads((self.out / "heartbeat.json").read_text()).get("status")
        except (OSError, ValueError):
            return None

    def mark(self, label: str) -> dict:
        r = subprocess.run([sys.executable, "-m", "cohbench.watcher", "mark", str(self.out), label, "--timeout", "60"], cwd="/src", capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise RuntimeError(f"mark {label} failed: {r.stderr[-1000:]}")
        return json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"stdout": r.stdout}

    def seq(self) -> int:
        return len([x for x in (self.out / "events.jsonl").read_text().splitlines() if x.strip()])

    def committed(self, path: str, after: int) -> None:
        """Wait until a batch that records `path` after record `after` is closed."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            recs = [json.loads(x) for x in (self.out / "events.jsonl").read_text().splitlines() if x.strip()][after:]
            b = next((r["batch"] for r in recs if r.get("path") == path and r["kind"] != "batch"), None)
            if b is not None and any(r["kind"] == "batch" and r["batch"] == b for r in recs):
                return
            time.sleep(0.02)
        raise RuntimeError(f"{path} was never committed")

    def stop(self) -> None:
        self.p.send_signal(signal.SIGTERM)
        if self.p.wait(timeout=90) != 0:
            raise RuntimeError(f"watcher exited {self.p.returncode}")


def inside(dest: Path) -> None:
    os.environ["PYTHONPATH"] = "/src"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tmp = Path(tempfile.mkdtemp(prefix="fx-", dir="/tmp"))
    Path("/tmp/task").mkdir()
    Path("/tmp/task/metric.py").write_text(METRIC)
    ws1, ws2 = tmp / "ws1", tmp / "ws2"
    out1, out2 = tmp / "segment-1", tmp / "segment-2"
    steps: list[dict] = []

    def step(name: str) -> None:
        steps.append({"utc": utc(), "step": name})

    def git(*a: str, cwd: Path) -> None:
        subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *a], cwd=cwd, check=True, capture_output=True)

    def write(p: Path, data: bytes, mode: str = "wb") -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, mode) as f:
            f.write(data)

    # The baseline.
    ws1.mkdir()
    write(ws1 / "data/a.csv", b"id,value\n1,10\n")
    write(ws1 / "README.md", b"# Fixture task\n")
    write(ws1 / "CLAUDE.md", b"Instructions for the harness.\n")
    git("init", "-q", "-b", "main", cwd=ws1)
    git("add", "-A", cwd=ws1)
    git("commit", "-q", "-m", "baseline", cwd=ws1)
    git("update-ref", "refs/t3/orchestration-v2/checkpoints/ZjA4/ordinal/0", "HEAD", cwd=ws1)

    w = Watcher(ws1, out1, tmp / "work-1")
    time.sleep(1.0)
    start = time.monotonic()
    start_utc = utc()

    def at(s: float, base: float | None = None) -> None:
        delay = (base if base is not None else start) + s - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    at(3.5)
    write(ws1 / "tmp/c0ffee12ab.bin", bytes(range(64)))
    step("tmp/c0ffee12ab.bin created")
    at(6.1)
    write(ws1 / "out/summary.txt", b"rows: 1\n")
    step("out/summary.txt created")
    at(7.0)
    n = w.seq()
    write(ws1 / "out/.report.txt.tmp", b"report v1\n")
    step("out/.report.txt.tmp written")
    w.committed("out/.report.txt.tmp", n)
    os.replace(ws1 / "out/.report.txt.tmp", ws1 / "out/report.txt")
    git("update-ref", "refs/t3/orchestration-v2/checkpoints/ZjA4/ordinal/1", "HEAD", cwd=ws1)
    step("out/.report.txt.tmp renamed to out/report.txt; a driver ref added")
    at(9.0)
    step("mark m-1 requested")
    steps[-1]["ack"] = w.mark("m-1")
    at(11.0)
    write(ws1 / "data/a.csv", b"2,20\n", "ab")
    step("data/a.csv appended in place")
    at(13.0)
    write(ws1 / "out/summary.txt", b"rows: 1\n")
    step("out/summary.txt rewritten with the same bytes")
    at(15.0)
    write(ws1 / "out/.report.txt.tmp", b"report v2\n")
    os.replace(ws1 / "out/.report.txt.tmp", ws1 / "out/report.txt")
    step("out/report.txt replaced through out/.report.txt.tmp within one batch")
    at(17.0)
    n = w.seq()
    write(ws1 / "out/.report.txt.tmp", b"report v3\n")
    w.committed("out/.report.txt.tmp", n)
    os.replace(ws1 / "out/.report.txt.tmp", ws1 / "out/report.txt")
    step("out/report.txt replaced through out/.report.txt.tmp across two batches")
    at(20.0)
    os.unlink(ws1 / "tmp/c0ffee12ab.bin")
    os.replace(ws1 / "out/summary.txt", ws1 / "out/final.txt")
    write(ws1 / "big.bin", b"x" * 8192)
    write(ws1 / "CLAUDE.md", b"Instructions, edited by the agent.\n", "ab")
    # A temp-file write of an instruction file, and an editor's backup: names
    # that contain an ignored name are ignored too, so neither leaves a trace.
    write(ws1 / ".CLAUDE.md.tmp", b"Instructions, rewritten.\n")
    os.replace(ws1 / ".CLAUDE.md.tmp", ws1 / "CLAUDE.md")
    write(ws1 / "CLAUDE.md~", b"Instructions, edited by the agent.\n")
    step("tmp/c0ffee12ab.bin deleted, out/summary.txt moved to out/final.txt, big.bin created, CLAUDE.md modified, "
         "then rewritten through .CLAUDE.md.tmp, with a CLAUDE.md~ backup")
    at(22.0)
    step("mark reset-1 requested")
    steps[-1]["ack"] = w.mark("reset-1")
    handoff_start = utc()
    w.stop()
    subprocess.run(["cp", "-a", str(ws1), str(ws2)], check=True)
    w = Watcher(ws2, out2, tmp / "work-2")
    handoff_end = utc()
    base2 = time.monotonic()

    at(2.0, base2)
    write(ws2 / "c.txt", b"segment two\n")
    with open(ws2 / "data/a.csv", "r+b") as f:
        f.write(b"ID")
    step("c.txt created; data/a.csv rewritten in place")
    at(4.0, base2)
    write(ws2 / "p.txt", b"p\n")
    write(ws2 / "q.txt", b"q\n")
    step("p.txt and q.txt created")
    at(6.0, base2)
    os.replace(ws2 / "p.txt", ws2 / ".swap.tmp")
    os.replace(ws2 / "q.txt", ws2 / "p.txt")
    os.replace(ws2 / ".swap.tmp", ws2 / "q.txt")
    step("p.txt and q.txt swapped through .swap.tmp")
    at(8.0, base2)
    write(ws2 / "locked/f", b"hidden\n")
    os.chmod(ws2 / "locked", 0)
    step("locked/f created; locked/ made mode 000")
    at(10.0, base2)
    step("mark end-1 requested")
    steps[-1]["ack"] = w.mark("end-1")
    end_utc = utc()
    # Post-run activity: a write after the episode end (the awareness
    # exchange's), which no scorer input may hold.
    time.sleep(0.5)
    n = w.seq()
    write(ws2 / "post.txt", b"after the end\n")
    step("post.txt written after the end")
    w.committed("post.txt", n)
    w.stop()
    os.chmod(ws2 / "locked", 0o755)

    for src in (out1, out2):
        shutil.copytree(src, dest / src.name)
    (dest / "sequence.json").write_text(json.dumps({
        "start_utc": start_utc, "end_utc": end_utc, "end_mark": "end-1", "marks": ["m-1", "reset-1", "end-1"],
        "excluded": [{"start_utc": handoff_start, "end_utc": handoff_end, "reason": "handoff"}],
        "steps": steps, "kernel": os.uname().release, "python": sys.version.split()[0],
    }, indent=1, sort_keys=True) + "\n")


# On the host -----------------------------------------------------------------


def host() -> int:
    subprocess.run(["docker", "build", "-q", "-f", str(REPO / "tests/watcher/linux.Dockerfile"), "-t", IMAGE, str(REPO / "tests/watcher")], check=True, stdout=subprocess.DEVNULL)
    name = f"ctraj-fixture-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="ctraj-fixture-") as d:
        out = Path(d) / "out"
        out.mkdir()
        os.chmod(out, 0o777)
        try:
            subprocess.run(["docker", "run", "--name", name, "--network", "none", "--user", "1000:1000",
                            "--mount", f"type=bind,src={REPO},dst=/src,readonly", "--mount", f"type=bind,src={out},dst=/out",
                            "--tmpfs", "/tmp:rw,exec,size=256m", "-e", "HOME=/tmp", IMAGE,
                            "python", "/src/tests/trajectory/fixtures/watcher-real/make_fixture.py", "--inside", "/out"], check=True, timeout=300)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        for sub in ("segment-1", "segment-2"):
            shutil.rmtree(HERE / sub, ignore_errors=True)
            shutil.copytree(out / sub, HERE / sub)
        shutil.copy(out / "sequence.json", HERE / "sequence.json")
    print(f"wrote {HERE.relative_to(REPO)}/segment-1, segment-2 and sequence.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inside", type=Path)
    a = ap.parse_args()
    if a.inside:
        inside(a.inside)
        sys.exit(0)
    sys.exit(host())
