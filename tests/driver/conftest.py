"""Integration tests need Docker and build images. They run only when
selected: `uv run pytest -m integration tests/driver`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _selects_integration(expr: str) -> bool:
    """Whether a `-m` expression names the integration mark as wanted: true
    for an item marked only `integration`, false for an item with no marks.
    Parsed by pytest's own mark-expression parser, so "not integration" or
    a mark whose name merely contains the word does not select it."""
    if not expr.strip():
        return False
    from _pytest.mark.expression import Expression

    e = Expression.compile(expr)
    return e.evaluate(lambda name, **_: name == "integration") and not e.evaluate(lambda name, **_: False)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    selected = _selects_integration(config.getoption("-m") or "")
    marked = [item for item in items if "integration" in item.keywords]
    if not marked:
        return
    if not selected:
        for item in marked:
            item.add_marker(pytest.mark.skip(reason="integration test: select with -m integration"))
        return
    # Docker is asked only when an integration test is selected.
    docker = shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    if not docker:
        for item in marked:
            item.add_marker(pytest.mark.skip(reason="Docker is not available"))


@pytest.fixture(scope="session")
def images():
    from cohbench.env import provision

    return provision("pi")[0]


@pytest.fixture
def manifest(workspace: Path):
    """The test workspace's manifest, made from the directory as a task author would."""
    from cohbench.env import manifest_for

    return manifest_for(workspace)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A small task workspace: a Git repository with one commit."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# project\n\nA small project.\n")
    env = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@localhost", "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@localhost", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-qm", "initial commit"]):
        subprocess.run(cmd, cwd=ws, check=True, env=env)
    return ws
