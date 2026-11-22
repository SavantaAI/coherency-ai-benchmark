"""Real two-container check of the watcher.

  uv run python tests/watcher/two_container_check.py --build [--out DIR] [--keep]

1. Creates a Docker volume and a user-defined network.
2. Starts a writer container: node:24.22.3-bookworm-slim plus inotify-tools
   (image wdev-writer:dev, nothing project specific), uid 1000, the volume
   read-write. In it, inotifywait subscribes to IN_OPEN, IN_ACCESS and
   IN_CLOSE_NOWRITE on the whole workspace for the entire run: what an agent
   that went looking would see. The writer seeds files, runs a probe series
   with the watcher off, waits, runs the same probe series with the watcher
   on, then a scripted series of changes, each logged with a timestamp, and
   finally inspects its process list, files, mount table and access times.
3. While the writer waits, starts the watcher image on the same volume,
   read-only, with no network, no capabilities and a read-only root
   filesystem, and does control reads through other containers.
4. Compares the recorded batches with what the writer did and with an
   independent walk, takes a mark from the host, and measures delays.

Every container, network, volume and image it creates is named wdev-*;
containers, network and volume are removed at the end (unless --keep).
Exits non-zero if any check fails. The report is printed and written to
<out>/report.json, the writer's log to <out>/writer.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cohbench.watcher import read_events, state_at  # noqa: E402

NODE = "node:24.22.3-bookworm-slim"
WRITER_IMAGE = "wdev-writer:dev"
PY = "python:3.12.16-slim-bookworm"
WS = "/work/project"
TELLING = re.compile(r"benchmark|coherency|eval|test|probe|proof|watch|monitor|measure|score", re.IGNORECASE)

WRITER_DOCKERFILE = f"""FROM {NODE}
RUN apt-get update && apt-get install -y --no-install-recommends inotify-tools && rm -rf /var/lib/apt/lists/*
"""

WRITER_SCRIPT = r"""
set -eu
cd /work/project
mark() { printf 'STEP %s %s\n' "$(date +%s.%N)" "$1"; }
# The subscriber an agent could run: every open, read and read-only close.
# adir is excluded only because inotifywait -r lists every new directory it
# watches, which would itself change adir's atime (the atime check below).
inotifywait -m -r -q -e open,access,close_nowrite --exclude '/adir[0-9]' --format '%w%f|%e' /work/project 2>/dev/null | while IFS= read -r l; do printf 'INO %s %s\n' "$(date +%s.%N)" "$l"; done &
sleep 1
# Phase A: seed, before the watcher exists.
printf 'seed\n' > seed.txt
printf 'control\n' > control.txt
printf 'ro check\n' > rocheck.txt
printf 'b one\n' > b.txt
printf 'old\n' > data
ln -s /work/project/data shortcut
(umask 077; printf 'secret\n' > secret600)
mkdir -m 700 dir700
(umask 077; printf 'inner\n' > dir700/inner)
probe() {  # the same small series twice: watcher off, then on
  printf 'p\n' > "$1.txt"; printf 'q\n' >> "$1.txt"; mkdir "$1.d"; printf 'r\n' > "$1.d/f"
}
mark A0; probe pa; sleep 1.5; mark A1
cat /proc/self/mountinfo > /tmp/mountinfo.before
echo "ATIME_BEFORE"; stat -c '%x %n' seed.txt control.txt rocheck.txt dir700 dir700/inner
echo READY
sleep 12
mark B0; probe pb; sleep 1.5; mark B1
mark M0; probe pm; sleep 1.5; mark M1
# Directory atime under test: a new directory, its atime pushed into the
# past before the watcher lists it.
# Three times, because Docker Desktop walks every directory of the volume
# whenever any container starts or is exec'ed into (the image has no health
# check, but the check itself starts helper containers).
for i in 1 2 3; do
  mark "ADIR$i"; mkdir "adir$i"; touch -a -d '2020-01-01 00:00:00' "adir$i"; printf 'in adir\n' > "adir$i/f"; sleep 0.6
  echo "ATIME_ADIR$i"; stat -c '%x %n' "adir$i"; mark "ADIREND$i"
done
mark 1; printf 'hello\n' > a.txt; sleep 0.6
mark 2; printf 'more\n' >> a.txt; sleep 0.6
mark 3; printf 'rewritten\n' > a.txt; sleep 0.6
mark 4; printf 'b two\n' > .b.tmp; mv .b.tmp b.txt; sleep 0.6
mark 5; mkdir -p d1/d2; printf 'f\n' > d1/d2/f; sleep 0.6
mark 6; mv d1 d3; sleep 0.6
mark 7; chmod 600 a.txt; sleep 0.6
mark 8; ln -s a.txt link; sleep 0.6
mark 9; ln a.txt hard; sleep 0.6
mark 10; mkdir burst; i=0; while [ $i -lt 200 ]; do printf '%s\n' $i > burst/f$i; i=$((i+1)); done; sleep 0.6
mark 11; rm a.txt; sleep 0.6
mark 12; rm -rf d3; mkdir d3; printf 'y\n' > d3/y; sleep 0.6
mark 13; (umask 077; printf 'private\n' > private.txt); sleep 0.6
mark 14; head -c 6291456 /dev/urandom > big.bin; sleep 0.6
mark 15; mv b.txt /tmp/b-elsewhere.txt; sleep 0.6
mark 16; printf 'new\n' > data; sleep 1.5
mark end
# Phase C: what can this container see? Access times first, before this
# script's own find reads anything.
echo "ATIME_AFTER"; stat -c '%x %n' seed.txt control.txt rocheck.txt dir700 dir700/inner

mark C0
echo "PROCS_BEGIN"
for p in /proc/[0-9]*; do
  if [ -r "$p/cmdline" ]; then printf '%s ' "${p#/proc/}"; tr '\0\n' '  ' < "$p/cmdline" | cut -c1-120; fi
done
echo "PROCS_END"
echo "FIND_ARTIFACTS_BEGIN"
find / \( -path /proc -o -path /sys -o -path /dev \) -prune -o \( -name 'events.jsonl' -o -name 'metrics.jsonl' -o -name 'heartbeat.json' -o -name 'blobs' -o -name '*.jsonl' \) -print 2>/dev/null || true
echo "FIND_ARTIFACTS_END"
echo "VOLUME_BEGIN"
find /work/project -mindepth 1 -printf '%y %m %u %P\n' | sort -k4
echo "VOLUME_END"
echo "MOUNTINFO_BEGIN"
cat /proc/self/mountinfo
echo "MOUNTINFO_END"
echo "MOUNTINFO_BEFORE_BEGIN"
cat /tmp/mountinfo.before
echo "MOUNTINFO_BEFORE_END"
echo "INOTIFY_FDS_BEGIN"
for p in /proc/[0-9]*; do
  n=$(ls -l "$p/fd" 2>/dev/null | grep -ci inotify || true)
  if [ "$n" != "0" ]; then echo "${p#/proc/} $n"; fi
done
echo "INOTIFY_FDS_END"
sleep 1
echo DONE
"""

METRIC = r"""
import json, os, time
time.sleep(0.8)  # so live changes land while a reading of an earlier batch runs
n = 0
for d, _, fs in os.walk("."):
    n += sum(1 for f in fs if not os.path.islink(os.path.join(d, f)))
def rd(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError as e:
        return "ERR " + type(e).__name__
def ls(p):
    try:
        return sorted(os.listdir(p))
    except OSError as e:
        return "ERR " + type(e).__name__
print(json.dumps({"files": n, "data": rd("data"), "shortcut": rd("shortcut"), "live": rd("/srv/w/live/data"),
                  "live_dir": ls("/srv/w/live"), "srv": ls("/srv/w"), "out": rd("/srv/w/out/events.jsonl"),
                  "uid": os.getuid(), "cwd": os.getcwd(), "ws": os.path.realpath("/work/project")}))
"""

WALK = r"""
import hashlib, json, os, stat, sys
root = sys.argv[1]
out = {}
for d, dirs, files in os.walk(root):
    for name in sorted(dirs) + sorted(files):
        full = os.path.join(d, name)
        rel = os.path.relpath(full, root)
        st = os.lstat(full)
        mode = "%04o" % stat.S_IMODE(st.st_mode)
        if stat.S_ISDIR(st.st_mode):
            out[rel] = ["dir", None, mode, st.st_uid, st.st_ino]
        elif stat.S_ISLNK(st.st_mode):
            out[rel] = ["symlink", hashlib.sha256(os.fsencode(os.readlink(full))).hexdigest(), mode, st.st_uid, st.st_ino]
        else:
            with open(full, "rb") as f:
                out[rel] = ["file", hashlib.sha256(f.read()).hexdigest(), mode, st.st_uid, st.st_ino]
print(json.dumps(out))
"""

# (step, kind, path) the watcher must record. Within one batch the order is
# the batch's own (deletions, moves, creations, then changes).
EXPECTED = [
    ("B", "created", "pb.d"),
    ("B", "created", "pb.txt"),
    ("B", "created", "pb.d/f"),
    ("B", "created", "pm.d"),
    ("B", "created", "pm.txt"),
    ("B", "created", "pm.d/f"),
    *[("B", "created", f"adir{i}") for i in (1, 2, 3)],
    *[("B", "created", f"adir{i}/f") for i in (1, 2, 3)],
    (1, "created", "a.txt"),
    (2, "modified", "a.txt"),
    (3, "modified", "a.txt"),
    (4, "modified", "b.txt"),
    (5, "created", "d1"),
    (5, "created", "d1/d2"),
    (5, "created", "d1/d2/f"),
    (6, "moved", "d3"),
    (7, "attrib", "a.txt"),
    (8, "created", "link"),
    (9, "created", "hard"),
    (10, "created", "burst"),
    *[(10, "created", f"burst/f{i}") for i in range(200)],
    (11, "deleted", "a.txt"),
    (12, "deleted", "d3/d2/f"),
    (12, "deleted", "d3/d2"),
    (12, "created", "d3/y"),
    (13, "created", "private.txt"),
    (14, "created", "big.bin"),
    (15, "deleted", "b.txt"),
    (16, "modified", "data"),
]
SEEDED = {"seed.txt", "control.txt", "rocheck.txt", "b.txt", "data", "shortcut", "secret600", "dir700", "dir700/inner", "pa.txt", "pa.d", "pa.d/f"}
FINAL_PATHS = (SEEDED - {"b.txt"}) | {"pm.txt", "pm.d", "pm.d/f", "pb.txt", "pb.d", "pb.d/f", *{f"adir{i}" for i in (1, 2, 3)}, *{f"adir{i}/f" for i in (1, 2, 3)}, "link", "hard", "burst", *{f"burst/f{i}" for i in range(200)}, "d3", "d3/y", "private.txt", "big.bin", "rootonly"}


def sh(*args: str, check: bool = True, timeout: float = 300, input: str | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:6])}... failed ({p.returncode}): {p.stderr.strip()[:500]}")
    return p


def section(log: str, name: str) -> list[str]:
    m = re.search(rf"^{name}_BEGIN\n(.*?)^{name}_END$", log, re.S | re.M)
    if not m:
        raise RuntimeError(f"writer output has no {name} section")
    return [line for line in m.group(1).splitlines() if line.strip() and not line.startswith(("INO ", "STEP "))]


def atimes(log: str, label: str, n: int) -> dict[str, str]:
    lines = [x for x in log.splitlines() if not x.startswith("INO ")]
    try:
        i = lines.index(label)
    except ValueError:
        raise RuntimeError(f"writer output has no {label} block") from None
    out = {}
    for line in lines[i + 1 : i + 1 + n]:
        parts = line.rsplit(" ", 1)
        out[parts[1]] = parts[0]
    return out


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def docker_time(s: str) -> float:
    # 2026-11-22T00:01:02.123456789Z: keep microseconds
    m = re.match(r"(.*\.\d{6})\d*Z", s)
    return epoch((m.group(1) if m else s.rstrip("Z")) + "Z")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="wdev-watcher:dev")
    ap.add_argument("--build", action="store_true", help="build the watcher and writer images first")
    ap.add_argument("--out", help="host output directory for the watcher (default: a new temp dir)")
    ap.add_argument("--keep", action="store_true", help="leave containers, network and volume in place")
    args = ap.parse_args()

    if args.build:
        sh("docker", "build", "-q", "-f", str(REPO / "docker/watcher/Dockerfile"), "-t", args.image, str(REPO), timeout=600)
    if args.build or sh("docker", "image", "inspect", WRITER_IMAGE, check=False).returncode != 0:
        sh("docker", "build", "-q", "-t", WRITER_IMAGE, "-", input=WRITER_DOCKERFILE, timeout=600)

    tag = secrets.token_hex(3)
    vol = f"wdev-{tag}"
    net = f"wdev-{tag}-n"
    writer = f"wdev-{tag}-w"
    watcher = f"wdev-{secrets.token_hex(6)}"
    helpers = [f"wdev-{tag}-h{i}" for i in range(5)]
    out = Path(args.out or tempfile.mkdtemp(prefix="wdev-out-"))
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wdev-aux-", dir=out.parent))
    (work / "count.py").write_text(METRIC)
    (work / "walk.py").write_text(WALK)
    os.chmod(work, 0o755)
    for f in ("count.py", "walk.py"):
        os.chmod(work / f, 0o644)
    checks: list[tuple[str, bool]] = []
    report: dict = {"names": {"volume": vol, "network": net, "writer": writer, "watcher": watcher}, "out": str(out)}

    def check(name: str, ok: bool, detail: object = None) -> None:
        checks.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + (f": {str(detail)[:1500]}" if detail is not None and not ok else ""))

    created: list[tuple[str, str]] = []
    try:
        sh("docker", "volume", "create", vol)
        created.append(("volume", vol))
        sh("docker", "network", "create", net)
        created.append(("network", net))
        # A fresh named volume is root-owned; hand it to uid 1000 as an image
        # with a node-owned project directory would. Later mounts use
        # volume-nocopy, since Docker otherwise copies the ownership of the
        # image's mount point onto an empty volume.
        sh("docker", "run", "--rm", "--user", "0", "--network", "none", "--mount", f"type=volume,src={vol},dst={WS}", NODE, "chown", "1000:1000", WS)

        sh("docker", "run", "-d", "--name", writer, "--network", net, "--user", "1000:1000", "--mount", f"type=volume,src={vol},dst={WS},volume-nocopy", WRITER_IMAGE, "sh", "-c", WRITER_SCRIPT)
        created.append(("container", writer))
        deadline = time.time() + 40
        while "READY" not in sh("docker", "logs", writer).stdout:
            if time.time() > deadline:
                raise RuntimeError("writer did not get ready: " + sh("docker", "logs", writer, check=False).stderr[-500:])
            time.sleep(0.2)

        metric_cmd = json.dumps(["python", "/opt/aux/count.py"])
        sh(
            "docker", "run", "-d", "--name", watcher,
            "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m",
            "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--mount", f"type=volume,src={vol},dst=/srv/w/live,readonly,volume-nocopy",
            "--mount", f"type=bind,src={out},dst=/srv/w/out",
            "--mount", f"type=bind,src={work},dst=/opt/aux,readonly",
            args.image, "run", "--root", "/srv/w/live", "--out", "/srv/w/out", "--ignore", ".git/**", "--metric-cmd", metric_cmd,
        )
        created.append(("container", watcher))
        deadline = time.time() + 30
        while True:
            try:
                hb = json.loads((out / "heartbeat.json").read_text())
                if hb["status"] == "running":
                    break
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                pass
            if time.time() > deadline:
                raise RuntimeError("watcher did not start: " + sh("docker", "logs", watcher, check=False).stderr[-800:])
            time.sleep(0.1)
        insp = json.loads(sh("docker", "inspect", watcher).stdout)[0]
        report["watcher_started"] = insp["State"]["StartedAt"]
        report["resolve"] = hb.get("resolve")
        check("watcher resolves paths with openat2", hb.get("resolve") == "openat2", hb.get("resolve"))
        m0 = [json.loads(x) for x in (out / "metrics.jsonl").read_text().splitlines()]
        check("baseline metric reading done before status running", len(m0) == 1 and m0[0]["reason"] == "baseline" and m0[0]["batch"] == 0 and m0[0]["ok"], m0)

        # Controls, through other containers: a read through a read-write
        # mount, and a plain cat (no O_NOATIME) through a read-only mount.
        time.sleep(1.1)
        sh("docker", "run", "--name", helpers[0], "--network", "none", "--user", "1000:1000", "--mount", f"type=volume,src={vol},dst=/w,volume-nocopy", NODE, "cat", "/w/control.txt")
        sh("docker", "run", "--name", helpers[1], "--network", "none", "--user", "1000:1000", "--mount", f"type=volume,src={vol},dst=/w,readonly,volume-nocopy", NODE, "cat", "/w/rocheck.txt")
        control_windows = []
        for h in helpers[:2]:
            hi = json.loads(sh("docker", "inspect", h).stdout)[0]["State"]
            control_windows.append((docker_time(hi["StartedAt"]) - 0.5, docker_time(hi["FinishedAt"]) + 0.5))
            sh("docker", "rm", h)

        report["watcher_inspect"] = {
            "NetworkMode": insp["HostConfig"]["NetworkMode"],
            "ReadonlyRootfs": insp["HostConfig"]["ReadonlyRootfs"],
            "CapDrop": insp["HostConfig"]["CapDrop"],
            "CapAdd": insp["HostConfig"]["CapAdd"],
            "User": insp["Config"]["User"],
            "Mounts": [{k: m.get(k) for k in ("Type", "Name", "Destination", "RW")} for m in insp["Mounts"]],
        }
        net_probe = (
            "import json, os, socket\n"
            "ifs = [i for i in os.listdir('/sys/class/net') if os.path.isdir(f'/sys/class/net/{i}')]\n"
            "up = sorted(i for i in ifs if int(open(f'/sys/class/net/{i}/flags').read(), 16) & 1)\n"
            "routes = open('/proc/net/route').read().splitlines()[1:]\n"
            "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2); conn = 'connected'\n"
            "except OSError as e:\n    conn = type(e).__name__ + ': ' + str(e)\n"
            "print(json.dumps({'up': up, 'all': sorted(ifs), 'routes': routes, 'connect': conn}))\n"
        )
        netinfo = json.loads(sh("docker", "exec", watcher, "python", "-c", net_probe).stdout)
        report["watcher_network"] = netinfo
        check("watcher has no network (NetworkMode none, only lo up, no routes, connect fails)", report["watcher_inspect"]["NetworkMode"] == "none" and netinfo["up"] == ["lo"] and not netinfo["routes"] and netinfo["connect"] != "connected", netinfo)
        src_mount = [m for m in insp["Mounts"] if m.get("Destination") == "/srv/w/live"]
        check("watcher mounts the volume read-only", len(src_mount) == 1 and src_mount[0]["RW"] is False, src_mount)

        # A mark while the writer runs its third probe series.
        deadline = time.time() + 120
        while not re.search(r"^STEP \S+ M0$", sh("docker", "logs", writer).stdout, re.M):
            if time.time() > deadline:
                raise RuntimeError("writer never reached the mark probe")
            time.sleep(0.05)
        pm = subprocess.run([sys.executable, "-m", "cohbench.watcher", "mark", str(out), "probe-m", "--timeout", "60"], cwd=REPO, capture_output=True, text=True, timeout=90)
        report["probe_mark_ack"] = json.loads(pm.stdout) if pm.stdout.strip().startswith("{") else {"raw": pm.stdout + pm.stderr}

        code = int(sh("docker", "wait", writer, timeout=240).stdout.strip())
        wlog = sh("docker", "logs", writer).stdout
        (out / "writer.log").write_text(wlog)
        check("writer script exited 0", code == 0, wlog[-2000:])

        # A root-owned mode 600 file: the watcher (uid 1000) must say it cannot read it.
        sh("docker", "run", "--rm", "--name", helpers[2], "--network", "none", "--user", "0", "--mount", f"type=volume,src={vol},dst=/w,volume-nocopy", NODE, "sh", "-c", "printf 'root only\\n' > /w/rootonly && chmod 600 /w/rootonly")
        # A mark from the host, through the control directory.
        mk = subprocess.run([sys.executable, "-m", "cohbench.watcher", "mark", str(out), "after-writer", "--timeout", "60"], cwd=REPO, capture_output=True, text=True, timeout=90)
        try:
            ack = json.loads(mk.stdout)
        except json.JSONDecodeError:
            ack = {"ok": False, "raw": mk.stdout + mk.stderr}
        report["mark_ack"] = ack
        check("mark from the host acknowledged with coverage (its metric is null or the reading of its batch, never waited for)",
              ack.get("ok") and ack.get("accepted") is True and ack.get("covered_utc") and "straddled" in ack and ack.get("at") and (ack.get("metric") is None or ack["metric"].get("batch") == ack.get("batch")), ack)

        dns = sh("docker", "run", "--rm", "--name", helpers[3], "--network", net, NODE, "getent", "hosts", watcher, check=False)
        report["dns_lookup_of_watcher_from_writer_network"] = {"exit": dns.returncode, "stdout": dns.stdout.strip()}
        check("watcher name does not resolve on the writer's network", dns.returncode != 0 and not dns.stdout.strip(), report["dns_lookup_of_watcher_from_writer_network"])

        ins = json.loads(sh("docker", "inspect", watcher).stdout)[0]
        alive = subprocess.run([sys.executable, "-m", "cohbench.watcher", "alive", str(out), "--max-age", "10"], cwd=REPO, capture_output=True, text=True, timeout=30)
        check("no image health check (nothing execs into the watcher on a timer); the host reads the heartbeat as running",
              not (ins["Config"].get("Healthcheck") or {}).get("Test") and not ins["State"].get("Health") and alive.returncode == 0, {"healthcheck": ins["Config"].get("Healthcheck"), "alive": alive.stdout + alive.stderr})
        sh("docker", "stop", "-t", "90", watcher, timeout=150)
        wexit = json.loads(sh("docker", "inspect", watcher).stdout)[0]["State"]["ExitCode"]
        hb = json.loads((out / "heartbeat.json").read_text())
        check("watcher exited 0 on SIGTERM, heartbeat stopped, nothing still changing", wexit == 0 and hb["status"] == "stopped" and hb["still_changing"] is False, {"exit": wexit, "hb": hb, "logs": sh("docker", "logs", watcher, check=False).stderr[-1000:]})

        walk = json.loads(sh("docker", "run", "--rm", "--network", "none", "--user", "0", "--mount", f"type=volume,src={vol},dst=/w,readonly,volume-nocopy", "--mount", f"type=bind,src={work},dst=/opt/aux,readonly", PY, "python", "/opt/aux/walk.py", "/w").stdout)

        # ---- compare ---------------------------------------------------
        events = read_events(str(out))
        bats = [r for r in events if r["kind"] == "batch"]
        check("every batch passed its self-check", all(b["check"] == "ok" for b in bats), [b for b in bats if b["check"] != "ok"][:3])
        recs = [r for r in events if r["kind"] not in ("batch", "mark")]
        base = [r for r in recs if r["batch"] == 0]
        live = [r for r in recs if r["batch"] > 0]
        check("baseline recorded the seeded files", {e["path"] for e in base} == SEEDED, sorted(e["path"] for e in base))
        b_secret = next(e for e in base if e["path"] == "secret600")
        check("mode 600 file of uid 1000 read and hashed", b_secret["mode"] == "0600" and b_secret["uid"] == 1000 and b_secret["content"] == "stored" and b_secret["sha256"], b_secret)

        steps: dict[str, float] = {}
        subs: list[tuple[float, str, str]] = []
        for line in wlog.splitlines():
            if line.startswith("STEP "):
                _, ts, n = line.split()
                steps[n] = float(ts)
            elif line.startswith("INO "):
                _, ts, rest = line.split(" ", 2)
                path, _, ev = rest.rpartition("|")
                subs.append((float(ts), path.replace(WS + "/", "", 1) if path.startswith(WS + "/") else path, ev))
        step_names = [k for k in steps if k.isdigit()]

        def step_of(e: dict) -> object:
            w = epoch(e["wall"])
            if steps["B0"] <= w < steps["1"]:
                return "B"
            n = 0
            for k in step_names:
                if steps[k] <= w:
                    n = max(n, int(k))
            return n

        got = [(step_of(e), e["kind"], e["path"]) for e in live if e["path"] != "rootonly"]
        rootonly = [e for e in live if e["path"] == "rootonly"]
        missing = [x for x in EXPECTED if x not in got]
        # rm -rf d3 && mkdir d3: ext4 may hand the new directory the old
        # inode number, and then the entry is identical and records nothing.
        optional = {(12, "deleted", "d3"), (12, "created", "d3")}
        report["step12_directory_recreated_records"] = [x for x in got if x in optional]
        unexpected = [x for x in got if x not in EXPECTED and x not in optional]
        check("every scripted change recorded, in its step", not missing, missing[:20])
        check("no unexpected records", not unexpected, unexpected[:20])
        steps_in_order = [x[0] for x in got]
        rank = lambda s: -1 if s == "B" else s
        check("records in scripted step order", all(rank(a) <= rank(b) for a, b in zip(steps_in_order, steps_in_order[1:])))
        check("root-owned mode 600 file recorded as unreadable", len(rootonly) == 1 and rootonly[0]["content"] == "unreadable" and rootonly[0]["error"] == "EACCES", rootonly)
        by = {(e["kind"], e["path"]): e for e in live}
        check("temp-then-rename is a modification of the target", by[("modified", "b.txt")].get("via_rename") == ".b.tmp", by.get(("modified", "b.txt")))
        check("6 MiB file hashed, not stored", by[("created", "big.bin")]["content"] == "oversize" and by[("created", "big.bin")]["size"] == 6291456)
        check("symlink recorded as a link", by[("created", "link")]["type"] == "symlink" and by[("created", "link")]["target"] == "a.txt")
        check("private.txt (umask 077) read", by[("created", "private.txt")]["mode"] == "0600" and by[("created", "private.txt")]["content"] == "stored")

        st = state_at(str(out), "last")
        replay = {p: [e.type, e.sha256, f"{e.mode:04o}", e.uid, e.ino] for p, e in st.entries.items()}
        diff_paths = sorted(p for p in set(replay) | set(walk) if replay.get(p) != walk.get(p))
        check("state_at(last) equals an independent walk (rootonly differs only by its unknown hash)", diff_paths == ["rootonly"] and replay["rootonly"][1] is None, diff_paths[:20])
        check("final tree is exactly what the writer left", set(walk) == FINAL_PATHS, sorted(set(walk) ^ FINAL_PATHS)[:20])
        mark_b = ack.get("batch")
        if isinstance(mark_b, int):
            check("the mark's batch holds rootonly (written before the mark)", "rootonly" in state_at(str(out), mark_b).entries)

        metrics = [json.loads(line) for line in (out / "metrics.jsonl").read_text().splitlines()]
        report["metric_runs"] = len(metrics)
        final = metrics[-1]
        n_files_final = sum(1 for e in st.entries.values() if e.type == "file" and e.content == "stored")
        check("last metric reading is of the last batch and counts its stored files", final.get("measured") and final["ok"] and final["batch"] == st.batch and final["metrics"]["files"] == n_files_final, final)
        big_b = by[("created", "big.bin")]["batch"]
        check("readings lack only the oversize big.bin, from its batch on: complete false there, true before",
              all((x["complete"] is False and x["lacking"] == ["big.bin"]) if x["batch"] >= big_b else x["complete"] is True for x in metrics if x.get("measured")),
              [(x["batch"], x.get("complete"), x.get("lacking")) for x in metrics][:40])
        check("the host mark's batch has a measured, ok reading in metrics.jsonl", [x.get("ok") for x in metrics if x.get("batch") == ack.get("batch") and x.get("measured")] == [True], ack.get("batch"))
        check("every metric reading ok and attributed to a batch", all(x.get("ok") and isinstance(x.get("batch"), int) for x in metrics), [x for x in metrics if not x.get("ok")][:3])
        iso_bad = []
        for x in metrics:
            mm = x.get("metrics") or {}
            stx = state_at(str(out), x["batch"])
            e = stx.entries.get("data")
            want = (out / "blobs" / e.sha256[:2] / e.sha256).read_text() if e is not None else None
            problems = []
            if x.get("isolation") != "users":
                problems.append("isolation " + str(x.get("isolation")))
            if mm.get("data") != want or mm.get("shortcut") != want:
                problems.append(f"data {mm.get('data')!r} shortcut {mm.get('shortcut')!r} want {want!r}")
            for k in ("live", "live_dir", "srv", "out"):
                if not str(mm.get(k, "")).startswith("ERR"):
                    problems.append(f"{k} readable: {str(mm.get(k))[:80]}")
            if mm.get("uid") != 1500 or mm.get("cwd") != mm.get("ws"):
                problems.append(f"uid {mm.get('uid')} cwd {mm.get('cwd')} ws {mm.get('ws')}")
            if problems:
                iso_bad.append((x["run"], x["batch"], problems))
        report["metric_isolation_sample"] = metrics[-1].get("metrics")
        check("every reading: data and the absolute link shortcut read the batch's bytes; live tree and output unreachable; metric uid 1500", not iso_bad and len(metrics) > 3, iso_bad[:5])
        changed = [x for x in metrics if (x.get("metrics") or {}).get("data") == "old\n" and x["batch"] >= 1]
        check("some reading of an old batch ran after the live file changed (the race was exercised)", any(epoch(x["end"]) > steps["16"] for x in changed), [(x["batch"], x["start"], x["end"]) for x in changed[-3:]])
        accounted = sorted({x["batch"] for x in metrics if x.get("measured")})
        check("every committed batch has a measured reading, in batch order", accounted == list(range(0, st.batch + 1)) and [x["batch"] for x in metrics if isinstance(x.get("batch"), int)] == accounted, accounted)

        # ---- offline completion, in the image ------------------------------
        # A copy of the output with the last three readings removed, completed
        # by `complete-metrics` in the watcher image: the same executor, user,
        # layout and environment as the live worker, so the same values.
        out2 = Path(tempfile.mkdtemp(prefix="wdev-completion-", dir=out.parent))
        shutil.copytree(out / "blobs", out2 / "blobs")
        for name in ("events.jsonl", "config.json", "heartbeat.json"):
            shutil.copy(out / name, out2 / name)
        lines = (out / "metrics.jsonl").read_text().splitlines(keepends=True)
        dropped = [json.loads(x) for x in lines[-3:]]
        (out2 / "metrics.jsonl").write_text("".join(lines[:-3]))
        for d, _, fs in os.walk(out2):
            os.chmod(d, 0o777)
            for f in fs:
                os.chmod(os.path.join(d, f), 0o666)
        refused = subprocess.run([sys.executable, "-m", "cohbench.watcher", "complete-metrics", str(out2)], cwd=REPO, capture_output=True, text=True, timeout=60)
        check("complete-metrics refuses to run outside the watcher image", refused.returncode == 2 and "only in the watcher image" in refused.stdout, refused.stdout + refused.stderr)
        comp = sh("docker", "run", "--rm", "--name", helpers[4], "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m",
                  "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
                  "--mount", f"type=bind,src={out2},dst=/srv/w/out", "--mount", f"type=bind,src={work},dst=/opt/aux,readonly",
                  args.image, "complete-metrics", "/srv/w/out", check=False, timeout=300)
        after = [json.loads(x) for x in (out2 / "metrics.jsonl").read_text().splitlines()]
        added = {x["batch"]: x for x in after[len(lines) - 3:]}
        report["offline_completion"] = {"exit": comp.returncode, "stdout": comp.stdout.strip()[-500:], "stderr": comp.stderr.strip()[-800:], "batches": sorted(added)}
        same = all(b["batch"] in added and added[b["batch"]].get("metrics") == b.get("metrics") and added[b["batch"]].get("offline") is True and added[b["batch"]].get("isolation") == "users" for b in dropped)
        runs_ok = [x["run"] for x in after if x.get("run") is not None] == sorted({x["run"] for x in after if x.get("run") is not None})
        check("complete-metrics in the image remeasures the removed readings with the same values, isolated, run numbers continuing", comp.returncode == 0 and same and runs_ok, report["offline_completion"])

        # ---- delays ----------------------------------------------------
        bmap = {b["batch"]: b for b in bats}
        delays = []
        for n in range(1, 16):
            firsts = [e for e in live if step_of(e) == n]
            if not firsts:
                continue
            e = firsts[0]
            b = bmap[e["batch"]]
            delays.append({"step": n, "path": e["path"], "kind": e["kind"], "write_to_record_ms": round((epoch(e["wall"]) - steps[str(n)]) * 1000, 1), "write_to_first_inotify_read_ms": round((epoch(b["first_event"]) - steps[str(n)]) * 1000, 1) if b.get("first_event") else None, "scan_ms": b["scan_ms"]})
        report["delays"] = delays
        simple = [d["write_to_record_ms"] for d in delays if d["step"] in (1, 2, 3, 4, 7, 8, 9, 11, 13)]
        inot = [d["write_to_first_inotify_read_ms"] for d in delays if d["step"] in (1, 2, 3, 4, 7, 8, 9, 11, 13)]
        report["delay_summary_ms"] = {"steps": "single small writes (1,2,3,4,7,8,9,11,13)", "write_to_record": {"min": min(simple), "median": statistics.median(simple), "max": max(simple)}, "write_to_first_inotify_read": {"min": min(inot), "median": statistics.median(inot), "max": max(inot)}}

        # ---- what an inotify subscriber in the writer saw ------------------
        started = docker_time(report["watcher_started"])
        base_end = epoch(bats[0]["wall"])

        def window(a: float, b: float) -> list[str]:
            return [f"{t:.6f} {p} {e}" for t, p, e in subs if a <= t < b]

        def in_controls(t: float) -> bool:
            return any(a <= t < b for a, b in control_windows)

        report["inotify_off_probe"] = window(steps["A0"], steps["A1"])
        report["inotify_watcher_start"] = window(started - 0.5, base_end + 0.5)
        report["inotify_on_probe"] = window(steps["B0"], steps["B1"])
        later = [(t, p, e) for t, p, e in subs if steps["1"] <= t < steps["C0"]]
        summary: dict[str, int] = {}
        for t, p, e in later:
            summary[e] = summary.get(e, 0) + 1
        report["inotify_scripted_phase_counts"] = summary
        report["inotify_scripted_phase_sample"] = [f"{t:.6f} {p} {e}" for t, p, e in later[:60]]
        report["inotify_control_windows"] = [f"{t:.6f} {p} {e}" for t, p, e in subs if in_controls(t)]
        off = {(p.replace("pa", "P"), e) for p, e in (x.split(" ", 1)[1].rsplit(" ", 1) for x in report["inotify_off_probe"])}
        on = {(p.replace("pb", "P"), e) for p, e in (x.split(" ", 1)[1].rsplit(" ", 1) for x in report["inotify_on_probe"])}
        report["inotify_on_minus_off"] = sorted(f"{p} {e}" for p, e in on - off)
        from collections import Counter

        def counted(a: str, b: str, tag: str) -> dict:
            c = Counter(e for t, p, e in subs if steps[a] <= t < steps[b])
            per_path = Counter((p.replace(tag, "P"), e) for t, p, e in subs if steps[a] <= t < steps[b])
            return {"total": sum(c.values()), "by_kind": dict(sorted(c.items())), "by_path": {f"{p} {e}": n for (p, e), n in sorted(per_path.items())}}

        report["inotify_counts"] = {"watcher_off": counted("A0", "A1", "pa"), "watcher_on": counted("B0", "B1", "pb"), "watcher_on_with_mark": counted("M0", "M1", "pm")}

        # ---- what the writer saw -------------------------------------------
        procs = section(wlog, "PROCS")
        report["writer_processes"] = procs
        progs = sorted({p.split()[1].rsplit("/", 1)[-1] for p in procs if len(p.split()) > 1})
        check("writer's process list holds only its own shell, tools and its subscriber", set(progs) <= {"sh", "tr", "cut", "inotifywait", "date"}, procs)
        art = section(wlog, "FIND_ARTIFACTS")
        check("no watcher file anywhere the writer can read", not art, art)
        vol_paths = {line.split(" ", 3)[3] for line in section(wlog, "VOLUME")}
        check("the volume holds only the writer's own files (at the time of its check)", vol_paths == FINAL_PATHS - {"rootonly"}, sorted(vol_paths ^ (FINAL_PATHS - {"rootonly"}))[:20])
        mi = section(wlog, "MOUNTINFO")
        mi_before = section(wlog, "MOUNTINFO_BEFORE")
        ws_lines = [line for line in mi if f" {WS} " in line]
        report["writer_mountinfo_volume_line"] = ws_lines
        check("mount table unchanged after the watcher started (line for line)", bool(mi) and mi_before == mi, {"removed": [x for x in mi_before if x not in mi], "added": [x for x in mi if x not in mi_before]})
        check("volume mount line has no telling word", len(ws_lines) == 1 and not TELLING.search(ws_lines[0]), ws_lines)
        check("no telling word anywhere in the writer's mount table", not any(TELLING.search(line) for line in mi))
        ino_fds = section(wlog, "INOTIFY_FDS")
        report["writer_inotify_fds"] = ino_fds
        sub = {p.split()[0] for p in procs if len(p.split()) > 1 and p.split()[1].rsplit("/", 1)[-1] == "inotifywait"}
        report["writer_subscriber_pids"] = sorted(sub)
        check("inotify descriptors in the writer belong only to its own subscriber", bool(sub) and all(line.split()[0] in sub for line in ino_fds), {"fds": ino_fds, "subscriber": sorted(sub)})
        a0, a1 = atimes(wlog, "ATIME_BEFORE", 5), atimes(wlog, "ATIME_AFTER", 5)
        d1 = {}
        for i in (1, 2, 3):
            d1.update(atimes(wlog, f"ATIME_ADIR{i}", 1))
        report["atime"] = {"before_watcher": a0, "after": a1, "adir_after": d1}
        check("file atime unchanged by the watcher's reads (seed.txt, dir700/inner)", all(a0[k] == a1[k] for k in ("seed.txt", "dir700/inner")), report["atime"])
        walks = [t for t, p, e in subs if p == "" and e.startswith("OPEN,ISDIR")]
        adir_result = {}
        for i in (1, 2, 3):
            kept = d1[f"adir{i}"].startswith("2020-01-01")
            walked = any(steps[f"ADIR{i}"] <= t < steps[f"ADIREND{i}"] for t in walks)
            adir_result[f"adir{i}"] = {"atime_kept": kept, "docker_walk_in_window": walked}
        report["adir_atime"] = adir_result
        check("directory atime unchanged by the watcher listing new directories (every change explained by a Docker Desktop walk, at least one window without one)", all(v["atime_kept"] or v["docker_walk_in_window"] for v in adir_result.values()) and any(v["atime_kept"] and not v["docker_walk_in_window"] for v in adir_result.values()), adir_result)
        check("control: a read through a read-write mount changes atime", a0["control.txt"] != a1["control.txt"], report["atime"])
        check("a plain read through a read-only mount (no O_NOATIME) leaves atime unchanged", a0["rocheck.txt"] == a1["rocheck.txt"], report["atime"])
    finally:
        if not args.keep:
            for kind, name in reversed(created):
                if kind == "container":
                    sh("docker", "rm", "-f", name, check=False)
                elif kind == "network":
                    sh("docker", "network", "rm", name, check=False)
                elif kind == "volume":
                    sh("docker", "volume", "rm", "-f", name, check=False)
            for h in helpers:
                sh("docker", "rm", "-f", h, check=False)
    report["checks"] = [{"name": n, "ok": ok} for n, ok in checks]
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("names", "delay_summary_ms", "writer_mountinfo_volume_line", "inotify_off_probe", "inotify_watcher_start", "inotify_on_probe", "inotify_on_minus_off", "inotify_scripted_phase_counts") if k in report}, indent=1))
    print(f"report: {out / 'report.json'}")
    failed = [n for n, ok in checks if not ok]
    print(f"{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
