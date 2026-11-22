from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from cohbench.tasks.suite import write_suite


@pytest.fixture(scope="session")
def suite(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("suite") / "suite"
    write_suite(d)
    return d


def instances(root: Path, family: str | None = None) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (family is None or p.name.startswith(family + "-")))


def task(d: Path) -> dict[str, Any]:
    return json.loads((d / "task.json").read_text())


def info(d: Path) -> dict[str, Any]:
    return json.loads((d / "scripted" / "info.json").read_text())


def ws_texts(d: Path) -> dict[str, str]:
    root = d / "workspace"
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def agent_texts(t: dict[str, Any]) -> dict[str, str]:
    """Every text of the task document that the runner can send to the agent
    as the user's words, apart from the card's replies (which answer a
    request) and the coached sentence (which states the rules on purpose)."""
    out = {"opening": t["prompt"]["opening"]}
    for e in t["events"]:
        a = e["action"]
        for k in ("text", "notice", "continuation"):
            if k in a:
                out[f"{e['name']}.{k}"] = a[k]
        if e.get("reopen"):
            out[f"{e['name']}.reopen"] = e["reopen"]
    return out


@pytest.fixture(scope="session")
def runs(suite: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    """Every instance's ideal and worst script, run through the runner with
    the fake episode and MiniPi. Runs in threads: each run is its own fake
    clock, policy, engine and directory, and the time goes to subprocesses."""
    from .harness import run_script

    base = tmp_path_factory.mktemp("runs")
    jobs = [(d, who) for d in instances(suite) for who in ("ideal", "worst")]

    def one(job: tuple[Path, str]) -> tuple[tuple[str, str], Any]:
        d, who = job
        t = base / d.name / who
        t.mkdir(parents=True)
        return (d.name, who), run_script(d, who, t)

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, jobs))
