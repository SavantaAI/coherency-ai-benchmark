"""Write a task instance's manifests: `workspace.manifest.json` for
`workspace/`, and `metric.manifest.json` for `metric/` when it exists. Each
lists every directory with its mode, and every file and symlink (a file with
mode, size and SHA-256). A generator does the same when it writes an
instance.

    python tests/runner/fixtures/build_manifest.py tests/runner/fixtures/demo
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


MTIME = 1767225600  # 2026-01-01T00:00:00Z: the one modification time every packed entry gets (a generator derives it from the seed)


def build_dir(d: Path, mtime: int | None = MTIME) -> dict:
    rows = []
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames.sort()
        for name in sorted(filenames) + sorted(dirnames):
            p = Path(dirpath) / name
            rel = p.relative_to(d).as_posix()
            st = p.lstat()
            if stat.S_ISDIR(st.st_mode):
                rows.append({"path": rel, "type": "dir", "mode": f"{stat.S_IMODE(st.st_mode):04o}"})
            elif stat.S_ISLNK(st.st_mode):
                rows.append({"path": rel, "type": "symlink", "target": os.readlink(p)})
            elif stat.S_ISREG(st.st_mode):
                rows.append({"path": rel, "type": "file", "mode": f"{stat.S_IMODE(st.st_mode):04o}", "size": st.st_size,
                             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    out: dict = {"entries": sorted(rows, key=lambda r: r["path"])}
    if mtime is not None:
        out["mtime"] = mtime
    return out


def build(instance: Path) -> dict:
    return build_dir(instance / "workspace")


def write(instance: Path) -> None:
    (instance / "workspace.manifest.json").write_text(json.dumps(build_dir(instance / "workspace"), indent=1) + "\n")
    if (instance / "metric").is_dir():
        (instance / "metric.manifest.json").write_text(json.dumps(build_dir(instance / "metric"), indent=1) + "\n")


if __name__ == "__main__":
    write(Path(sys.argv[1]))
