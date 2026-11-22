from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from cohbench.runner import RunConfig, ScriptedPolicy, load, run_episode
from cohbench.runner.eventlog import read as read_log
from cohbench.runner.policy import register_policy_kind

from .fake import Act, FakeClock, FakeEpisode

FIX = Path(__file__).parent / "fixtures"


def integration_selected(expr: str) -> bool:
    """Whether a `-m` expression selects the integration tests: `integration`
    as a positive term (`not integration` and the like do not)."""
    import re

    return any(not re.search(r"\bnot\s*$", expr[: m.start()]) for m in re.finditer(r"\bintegration\b", expr))


def pytest_collection_modifyitems(config, items):
    """Integration tests run only when selected with `-m integration`, and only
    when Docker answers (as in tests/driver)."""
    marked = [i for i in items if "integration" in i.keywords]
    if not marked:
        return
    if not integration_selected(config.getoption("-m") or ""):
        for i in marked:
            i.add_marker(pytest.mark.skip(reason="integration test: select with -m integration"))
        return
    ok = shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    if not ok:
        for i in marked:
            i.add_marker(pytest.mark.skip(reason="Docker is not available"))
DEMO = FIX / "demo"
# An Explanation answer key's buried control (the trajectory unit asks for 80 words of filler).
BURIED = {"filler": " ".join(["The logs on the build host rotate every night and nobody reads them."] * 7), "sentence": "The disk on m3 is full."}


def demo_doc() -> dict[str, Any]:
    return json.loads((DEMO / "task.json").read_text())


def write_task(tmp: Path, edit: Callable[[dict[str, Any]], None] | None = None, name: str = "task") -> Path:
    d = tmp / name
    shutil.copytree(DEMO, d)
    doc = demo_doc()
    demo_names = {e["name"] for e in doc["events"]}
    if edit:
        edit(doc)
    # A test that removes a demo event also removes the card's link to it.
    names = {e["name"] for e in doc.get("events", []) if isinstance(e, dict)}
    for key in ("facts", "decisions"):
        for item in doc["user"]["card"][key]:
            if item.get("available_after") in demo_names - names:
                del item["available_after"]
    (d / "task.json").write_text(json.dumps(doc, indent=1))
    return d


# The test policy kind: the factory registered for a file by the Run that wrote it.
TEST_POLICIES: dict[str, Any] = {}
register_policy_kind("test", lambda path: TEST_POLICIES[str(Path(path).resolve())])


def policy_doc(rules: list[dict[str, Any]] | None = None, default: str = "no_request", awareness: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"format": "cohbench-scripted-policy", "version": 1, "rules": rules or [], "default": default,
            "awareness": awareness or {"rules": [], "default": "unclear"}}


def policy(rules: list[dict[str, Any]] | None = None, default: str = "no_request", awareness: dict[str, Any] | None = None):
    """A policy factory, as run_episode takes."""
    doc = policy_doc(rules, default, awareness)
    f = ScriptedPolicy.factory(doc)
    f.doc = doc  # type: ignore[attr-defined]  # written to a host file by Run, as run_episode requires one
    return f


class Run:
    """One fake episode run, with everything a test looks at."""

    def __init__(self, tmp: Path, acts: list[Act], *, edit: Callable[[dict[str, Any]], None] | None = None, pol: Any = None,
                 poll_s: float = 5.0, stall_s: float = 3600.0, episode: str = "ep-1", factory_error: Exception | None = None, **fake_kw: Any):
        self.task_dir = write_task(tmp, edit, name=f"task-{episode}")
        self.task = load(self.task_dir)
        self.clock = FakeClock()
        self.out = tmp / "out"
        self.fake: FakeEpisode | None = None

        def factory(spec):
            if factory_error:
                raise factory_error
            self.spec = spec
            self.fake = FakeEpisode(acts, tmp / f"driver-{episode}", self.clock, collect=spec.collect, **fake_kw)
            return self.fake

        self.factory = factory
        self.cfg = RunConfig(episode_id=episode, harness="pi", model="claude-haiku-5-5", upstream=("scripted", tmp / "script.json"),
                             mode="default", poll_s=poll_s, stall_s=stall_s)
        self.policy = pol or policy()
        # The host-only files the run opens: the policy file and the model script. Tests use a
        # policy kind of their own whose factory is the one given (a wrapper, a stand-in class).
        self.policy_file = tmp / f"policy-{episode}.json"
        self.policy_file.write_text(json.dumps(getattr(self.policy, "doc", policy_doc())))
        (tmp / "script.json").write_text(json.dumps({"version": 1, "handlers": []}))
        self.policy_kind = "test"

    def go(self, sleep: Callable[[float], None] | None = None) -> "Run":
        TEST_POLICIES[str(self.policy_file.resolve())] = self.policy
        self.outcome = run_episode(self.task, self.cfg, self.factory, (self.policy_kind, self.policy_file), self.out,
                                   sleep=sleep or self.clock.sleep, utcnow=self.clock.utcnow)
        self.dir = self.outcome.path
        self.log = list(read_log(self.dir / "runner" / "events.jsonl"))
        self.run_json = json.loads((self.dir / "run.json").read_text())
        rec = self.dir / "episode-record.json"
        self.record = json.loads(rec.read_text()) if rec.exists() else None
        return self

    @property
    def h(self):
        assert self.fake is not None and self.fake.handoff is not None
        return self.fake.handoff

    def events(self, type: str) -> list[dict[str, Any]]:
        return [e for e in self.h.runner_events if e["type"] == type]

    def sends(self) -> list[dict[str, Any]]:
        return [r for r in self.log if r["type"] == "send"]

    def planted(self, name: str) -> list[str]:
        return [e["fields"]["phase"] for e in self.events("planted") if e["fields"]["name"] == name]


@pytest.fixture
def run_factory(tmp_path: Path) -> Callable[..., Run]:
    def make(acts: list[Act], **kw: Any) -> Run:
        return Run(tmp_path, acts, **kw)
    return make


def _drop_card_links(doc: dict[str, Any]) -> None:
    names = {e["name"] for e in doc["events"]}
    for key in ("facts", "decisions"):
        for item in doc["user"]["card"][key]:
            if item.get("available_after") not in (None, *names):
                del item["available_after"]


def no_events(doc: dict[str, Any]) -> None:
    doc["events"] = []
    _drop_card_links(doc)


def only(*names: str) -> Callable[[dict[str, Any]], None]:
    def edit(doc: dict[str, Any]) -> None:
        doc["events"] = [e for e in doc["events"] if e["name"] in names]
        _drop_card_links(doc)
    return edit


def chain(*edits: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def edit(doc: dict[str, Any]) -> None:
        for e in edits:
            e(doc)
    return edit


def deep(doc: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(doc)
