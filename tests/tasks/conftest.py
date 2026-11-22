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


def _runner_has_d046() -> bool:
    """Whether the runner admits D-046's Explanation form (the follow-up at
    the first hand-back since the reset). The runner's change that delivers a
    reset notice into the running turn lands with it; until then the tests
    that need either are skipped with WAITS."""
    import tempfile

    from cohbench.runner import check
    from cohbench.tasks import Options, generate_to

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "x"
        generate_to(Options("explanation", 1, "h80"), d)
        _t, problems, _w = check(d)
        return not problems


RUNNER_D046 = _runner_has_d046()
WAITS = "waits on the runner's D-046 change: a reset notice into the running turn, the follow-up since the reset"


def long_form(d: Path) -> bool:
    """An instance that needs the runner's D-046 behaviour: every h80 instance."""
    return task(d)["grouping"]["horizon"] == "h80"


def _run_all(dirs: list[Path], base: Path, whos: tuple[str, ...], fn: Any) -> dict[tuple[str, str], Any]:
    jobs = [(d, who) for d in dirs for who in whos if (d / "scripted" / f"{who}-run.json").exists()]

    def one(job: tuple[Path, str]) -> tuple[tuple[str, str], Any]:
        d, who = job
        t = base / d.name / who
        t.mkdir(parents=True)
        return (d.name, who), fn(d, who, t)

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, jobs))


@pytest.fixture(scope="session")
def runs(suite: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    """The ideal and worst scripts (and at h80 the grace variant) run through
    the runner with its fake episode and MiniPi: every h20 instance, and the
    h80 ones once the runner has D-046. Threads: each run has its own fake
    clock, policy, engine and directory; the time goes to subprocesses."""
    from .harness import run_script

    dirs = [d for d in instances(suite) if RUNNER_D046 or not long_form(d)]
    return _run_all(dirs, tmp_path_factory.mktemp("runs"), ("ideal", "worst", "ideal-grace"), run_script)


@pytest.fixture(scope="session")
def long_runs(suite: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    """Every h80 instance's three scripts under the D-046 stand-in (d046.py)."""
    from .d046 import D046Run

    dirs = [d for d in instances(suite) if long_form(d)]
    return _run_all(dirs, tmp_path_factory.mktemp("long"), ("ideal", "worst", "ideal-grace"), lambda d, w, t: D046Run(d, w, t).run())


@pytest.fixture(scope="session")
def legacy_explanation(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """The Explanation h80 instances in the earlier form (`SINCE_RESET`
    False: no reset, the follow-up at the first hand-back), which the current
    runner admits and runs."""
    from cohbench.tasks import Options, explanation, generate_to
    from cohbench.tasks.suite import CONTROL_SEEDS, PAIRS

    base = tmp_path_factory.mktemp("legacy")
    old = explanation.SINCE_RESET
    explanation.SINCE_RESET = False
    try:
        out = []
        for seed, control in [(s, False) for s in range(1, PAIRS + 1)] + [(CONTROL_SEEDS, True)]:
            o = Options("explanation", seed, "h80", control=control)
            generate_to(o, base / o.name())
            out.append(base / o.name())
        return out
    finally:
        explanation.SINCE_RESET = old
