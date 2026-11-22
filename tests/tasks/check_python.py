"""Run the workspace code of every Routing and Steering instance of the
development suite under a given Python, and compare what it prints with the
recorded outputs in `workspace_outputs.json`.

    python tests/tasks/check_python.py --python /path/to/python3.11
    python tests/tasks/check_python.py --python python3.12 --record   # after a deliberate change to the generators

The suite is generated with the interpreter running this script (3.12 or
later); only the workspace code runs under `--python`. For each Routing
instance: `add` on every input file, `resolve` with the answer key's winner,
`list`, and the register file. For each Steering instance: `bin/profile`, the
package tests' exit status (their timing line is not compared), and the
metric. The agent's container has Python 3.11 and the watcher's 3.12, so
both should print the same as the recording. `test_workspace_outputs.py`
runs this with the test interpreter, which keeps the recording current.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDED = Path(__file__).with_name("workspace_outputs.json")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    home = cwd.parent / (cwd.name + "-home")
    home.mkdir(exist_ok=True)
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state")})


def instance_outputs(inst: Path, python: str, work: Path) -> list[str]:
    info = json.loads((inst / "scripted" / "info.json").read_text())
    task = json.loads((inst / "task.json").read_text())
    w = work / inst.name
    shutil.copytree(inst / "workspace", w)
    out: list[str] = []
    if info["family"] == "routing":
        tool = f"bin/{info['tool']}"
        for f in info["files"]:
            r = _run([python, tool, "add", f], w)
            out.append(f"{r.returncode}\n{r.stdout}{r.stderr}")
        win = task["scoring"]["answer_key"].get("winner_id")
        if win:
            r = _run([python, tool, "resolve", win], w)
            out.append(f"{r.returncode}\n{r.stdout}{r.stderr}")
        r = _run([python, tool, "list"], w)
        out.append(f"{r.returncode}\n{r.stdout}{r.stderr}")
        out.append((w / "out" / "register.json").read_text())
    elif info["family"] == "steering":
        r = _run([python, "bin/profile"], w)
        out.append(f"{r.returncode}\n{r.stdout}{r.stderr}")
        r = _run([python, "-m", "unittest", "discover", "-s", "tests"], w)
        out.append(str(r.returncode))
        r = _run([python, *[arg.replace("/opt/task", str(inst / "metric")) for arg in task["watcher"]["metric"]["command"][1:]]], w)
        out.append(f"{r.returncode}\n{r.stdout}{r.stderr}")
    return out


def outputs(python: str) -> dict[str, str]:
    """Instance name -> sha256 of everything its workspace code printed."""
    with tempfile.TemporaryDirectory() as tmp:
        suite = Path(tmp) / "suite"
        subprocess.run([sys.executable, "-m", "cohbench.tasks", "suite", "--out", str(suite)], cwd=ROOT, check=True, capture_output=True)
        work = Path(tmp) / "work"
        work.mkdir()
        res = {}
        for inst in sorted(p for p in suite.iterdir() if p.is_dir() and not p.name.startswith("explanation-")):
            res[inst.name] = hashlib.sha256("\0".join(instance_outputs(inst, python, work)).encode()).hexdigest()
        return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True, help="the interpreter to run the workspace code with")
    ap.add_argument("--record", action="store_true", help="write the outputs as the new recording")
    a = ap.parse_args(argv)
    got = outputs(a.python)
    if a.record:
        RECORDED.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        print(f"recorded {len(got)} instances")
        return 0
    want = json.loads(RECORDED.read_text())
    bad = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
    for k in bad:
        print(f"differs: {k}")
    version = subprocess.run([a.python, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"{version}: {len(got) - len(bad)} of {len(want)} instances print what is recorded")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
