"""Compare rebuilt saved episodes with a git reference, without hosted calls.

Raw episodes stay outside the repository. The report contains relative episode
ids and file digests, never raw contents. Reference code is unpacked into a
temporary directory, not a git checkout. Both arms receive the same raw input.
"""

import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
WORKER = r"""
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cohbench.trajectory import bundle,io,replay
rows=[]
for item in json.load(sys.stdin):
    try:
        result,built=replay.rebuild(item['path'],item['task'])
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)
            bundle.write(built,dest/'bundle')
            io.write(result.trajectory,dest/'trajectory.jsonl')
            rows.append({'episode':item['episode'],'files':replay.digests(dest)})
    except Exception as exc:
        # No raw paths or item content in the published error record.
        rows.append({'episode':item['episode'],'error':type(exc).__name__})
print(json.dumps(rows))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=Path, required=True)
    parser.add_argument("--reference", default="main")
    parser.add_argument(
        "--fallback-task",
        type=Path,
        default=ROOT / "tests/runner/fixtures/demo/task.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from cohbench.trajectory import replay

    cases = []
    for manifest in sorted(args.episodes.glob("**/export/manifest.json")):
        path = manifest.parent.parent
        if not (path / "episode-record.json").is_file():
            continue
        cases.append(
            {
                "episode": path.relative_to(args.episodes).as_posix(),
                "path": str(path.resolve()),
                "task": str(replay.task_of(path) or args.fallback_task.resolve()),
            }
        )
    if not cases:
        parser.error("no saved episodes found")
    reference = subprocess.check_output(
        ["git", "rev-parse", args.reference], cwd=ROOT, text=True
    ).strip()
    archive = subprocess.check_output(
        ["git", "archive", reference, "cohbench"], cwd=ROOT
    )
    with tempfile.TemporaryDirectory(prefix="cohbench-reference-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as data:
            data.extractall(tmp, filter="data")

        def run(root):
            result = subprocess.run(
                [sys.executable, "-c", WORKER, str(root)],
                input=json.dumps(cases),
                text=True,
                capture_output=True,
                check=True,
                cwd=tmp,
            )
            return json.loads(result.stdout)

        baseline, current = run(tmp), run(ROOT)
    rows = []
    for before, after in zip(baseline, current, strict=True):
        assert before["episode"] == after["episode"]
        row = {"episode": before["episode"]}
        if "error" in before or "error" in after:
            row.update(
                reference_error=before.get("error"), current_error=after.get("error")
            )
        else:
            row["files"] = len(before["files"])
            row["differences"] = [
                p
                for p in sorted(before["files"].keys() | after["files"].keys())
                if before["files"].get(p) != after["files"].get(p)
            ]
            if row["differences"]:
                row["reference_digests"] = before["files"]
                row["current_digests"] = after["files"]
            else:
                row["identical_digests"] = before["files"]
        rows.append(row)
    report = {"reference": reference, "episodes": len(rows), "results": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    failed = [
        r["episode"] for r in rows if r.get("differences") or "reference_error" in r
    ]
    print(json.dumps({"episodes": len(rows), "different_or_unrebuildable": failed}))
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
