"""Check the actual Dockerfiles and ignore files, including live run output."""

from __future__ import annotations

import shlex
import shutil
import tempfile
from pathlib import Path

import pytest

from cohbench.env import images
from cohbench.env.build_inputs import _context_paths, _ignore_rules, inputs_digest

ROOT = Path(__file__).resolve().parents[2]
ROLES = ("base", "pi", "claude", "gateway", "scripted", "watcher")


def recipe(root, role):
    dockerfile = root / "docker" / role / "Dockerfile"
    context = root / "docker" if role in ("base", "pi", "claude") else root
    args = {}
    parents = {}
    if role == "base":
        args = {"T3_VERSION": images.T3_VERSION, "NODE_IMAGE": images.NODE_IMAGE}
    elif role in ("pi", "claude"):
        base = f"t3-harness-base:{images.T3_VERSION}"
        args = {"BASE_IMAGE": base, **images.HARNESS_BUILD_ARGS[role]}
        parents = {base: fingerprint(root, "base")}
    return dockerfile, context, args, parents


def fingerprint(root, role):
    dockerfile, context, args, parents = recipe(root, role)
    return inputs_digest(dockerfile, context, args, parents)


def copied_files(root, role):
    dockerfile, context, _, _ = recipe(root, role)
    paths = set()
    for line in dockerfile.read_text().replace("\\\n", "").splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == "COPY":
            for pattern in words[1:-1]:
                if not pattern.startswith("--"):
                    matches = list(context.glob(pattern))
                    assert matches, pattern
                    assert all(path.is_file() for path in matches)
                    paths.update(matches)
    return paths


@pytest.fixture
def layout(tmp_path):
    # Copy only build controls and actual COPY sources, never run or key files.
    root = tmp_path / "repository"
    for role in ROLES:
        dockerfile = ROOT / "docker" / role / "Dockerfile"
        for path in [
            dockerfile,
            dockerfile.with_suffix(".dockerignore"),
            *copied_files(ROOT, role),
        ]:
            dest = root / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    return root


@pytest.mark.parametrize("role", ROLES)
def test_real_context_contains_only_copy_sources(role):
    dockerfile, context, _, _ = recipe(ROOT, role)
    rules = _ignore_rules(dockerfile.with_suffix(".dockerignore"))
    sent_files = {path for path in _context_paths(context, rules) if path.is_file()}
    assert sent_files == copied_files(ROOT, role)


@pytest.mark.parametrize("role", ROLES)
def test_real_repository_ignores_created_changed_and_deleted_run_output(role):
    before = fingerprint(ROOT, role)
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="image-inputs-", dir=runs) as scratch:
        path = Path(scratch) / "output.txt"
        path.write_text("first")
        assert fingerprint(ROOT, role) == before
        path.write_text("changed")
        assert fingerprint(ROOT, role) == before
        path.unlink()
        assert fingerprint(ROOT, role) == before
    assert fingerprint(ROOT, role) == before


@pytest.mark.parametrize("role", ROLES)
def test_other_unused_directories_do_not_change_image_digest(layout, role):
    before = fingerprint(layout, role)
    for directory in (
        "runs",
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "out",
        "build",
        "dist",
        "docs",
        "tests",
        "cohbench/scoring",
        "cohbench/scripted/__pycache__",
        "cohbench/watcher/__pycache__",
        "gateway/__pycache__",
        f"docker/{role}/cache",
    ):
        path = layout / directory / "unused.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("created")
        assert fingerprint(layout, role) == before, directory
        path.write_text("changed")
        assert fingerprint(layout, role) == before, directory
        path.unlink()
        assert fingerprint(layout, role) == before, directory


@pytest.mark.parametrize("role", ROLES)
def test_each_copied_file_changes_image_digest(layout, role):
    before = fingerprint(layout, role)
    for path in copied_files(layout, role):
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        assert fingerprint(layout, role) != before, path.relative_to(layout)
        path.write_bytes(original)
        assert fingerprint(layout, role) == before


def test_ignored_run_tree_is_not_even_statted(monkeypatch):
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        assert path != ROOT / "runs" and ROOT / "runs" not in path.parents
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    for role in ROLES:
        fingerprint(ROOT, role)
