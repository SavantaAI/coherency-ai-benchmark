"""The same seed and options give byte-identical output: across processes
with different hash seeds, locales and working directories, and wherever the
output is written (a validity rule of this unit)."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

from cohbench.tasks import Options, generate
from cohbench.tasks.common import dumps

ROOT = Path(__file__).parents[2]


def digest_tree(root: Path) -> dict[str, tuple[str, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        mode = f"{stat.S_IMODE(p.lstat().st_mode):04o}"
        out[rel] = (mode, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
    return out


def _run(args: list[str], cwd: Path, hashseed: str, lang: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env.update(PYTHONHASHSEED=hashseed, LANG=lang, LC_ALL=lang, PYTHONPATH=str(ROOT), TZ="Pacific/Auckland" if hashseed == "1" else "UTC")
    r = subprocess.run([sys.executable, "-m", "cohbench.tasks", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_suite_is_byte_identical_across_processes_hash_seeds_locales_and_paths(tmp_path):
    a, b = tmp_path / "one" / "suite", tmp_path / "two" / "deeper" / "other-name"
    (tmp_path / "cwd-a").mkdir()
    (tmp_path / "cwd-b").mkdir()
    _run(["suite", "--out", str(a)], tmp_path / "cwd-a", "1", "C")
    _run(["suite", "--out", str(b)], tmp_path / "cwd-b", "4242", "en_US.UTF-8")
    da, db = digest_tree(a), digest_tree(b)
    assert da == db
    assert len(da) > 1000


def test_one_instance_from_the_command_line_equals_its_suite_copy(tmp_path, suite):
    out = tmp_path / "x"
    _run(["generate", "--family", "routing", "--seed", "1", "--horizon", "20", "--sibling", "b", "--out", str(out)], tmp_path, "7", "C")
    assert digest_tree(out) == digest_tree(suite / "routing-h20-s1-B")


def test_generating_twice_in_one_process_gives_equal_documents():
    for o in (Options("routing", 3, "h80", "A"), Options("explanation", 2, "h20"), Options("steering", 5, "h80", control=True)):
        x, y = generate(o), generate(o)
        assert dumps(x.task) == dumps(y.task)
        assert x.workspace.items == y.workspace.items
        assert x.scripts == y.scripts


def test_no_generated_file_names_its_output_path_or_the_machine(tmp_path, suite):
    home = str(Path.home())
    for p in suite.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert str(suite).encode() not in data and home.encode() not in data, p


# The digest of the whole development suite: names, modes and contents. The
# same value on every interpreter the tests run on (3.12 and 3.15) is the
# cross-version check; a deliberate change to a generator changes it here.
SUITE_DIGEST = "41313946388fae441c7a59abb2f69dd0b59567f054859315be1f4b5b054e4e70"


def suite_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + f"{stat.S_IMODE(p.lstat().st_mode):04o}".encode() + b"\0")
        if p.is_file():
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def test_the_suite_is_the_same_on_every_python_the_tests_run_on(suite):
    assert suite_digest(suite) == SUITE_DIGEST
