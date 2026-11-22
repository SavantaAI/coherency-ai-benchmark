"""The command line: what it refuses and where it writes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cohbench.tasks.routing import has_conflict

ROOT = Path(__file__).parents[2]


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cohbench.tasks", *args], capture_output=True, text=True, cwd=ROOT)


def _seed(conflict: bool) -> int:
    return next(s for s in range(1, 100) if has_conflict(s) is conflict)


@pytest.mark.parametrize("args, why", [
    (["--family", "routing", "--seed", str(_seed(True)), "--horizon", "20"], "give --sibling"),
    (["--family", "routing", "--seed", str(_seed(False)), "--horizon", "20", "--sibling", "a"], "no siblings"),
    (["--family", "explanation", "--seed", "1", "--horizon", "20", "--sibling", "a"], "only Routing"),
    (["--family", "routing", "--seed", "1", "--horizon", "20", "--sibling", "a", "--control"], "no sibling"),
    (["--family", "steering", "--seed", "-1", "--horizon", "20"], "non-negative"),
])
def test_options_that_make_no_instance_are_refused(tmp_path, args, why):
    r = cli("generate", *args, "--out", str(tmp_path / "x"))
    assert r.returncode == 2 and why in r.stderr, r.stderr
    assert not (tmp_path / "x").exists()


def test_a_non_empty_output_directory_is_refused(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "keep.txt").write_text("mine\n")
    r = cli("generate", "--family", "steering", "--seed", "1", "--horizon", "80", "--out", str(tmp_path / "x"))
    assert r.returncode == 2 and (tmp_path / "x" / "keep.txt").read_text() == "mine\n"


def test_generate_writes_an_admitted_instance(tmp_path):
    r = cli("generate", "--family", "explanation", "--seed", "9", "--horizon", "80", "--control", "--out", str(tmp_path / "x"))
    assert r.returncode == 0, r.stderr
    d = tmp_path / "x"
    c = subprocess.run([sys.executable, "-m", "cohbench.runner", "check-task", str(d), "--policy", str(d / "scripted" / "worst-policy.json"),
                        "--script", str(d / "scripted" / "worst-run.json")], capture_output=True, text=True, cwd=ROOT)
    assert c.returncode == 0, c.stdout
