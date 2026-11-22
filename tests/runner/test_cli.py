"""The command line."""

from __future__ import annotations

import json
from pathlib import Path

from cohbench.runner.__main__ import main

from .conftest import DEMO, write_task
from .fake import Act, FakeClock, FakeEpisode


def test_check_task(capsys, tmp_path: Path) -> None:
    assert main(["check-task", str(DEMO)]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == ["conflict", "format_change", "handoff", "check_in"]
    bad = write_task(tmp_path, lambda d: d["prompt"].__setitem__("opening", "Run the eval."))
    assert main(["check-task", str(bad)]) == 1
    assert "error: prompt.opening: reaches the agent and holds telling words ['eval']" in capsys.readouterr().out


def test_run_with_a_factory(capsys, tmp_path: Path) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"format": "cohbench-scripted-policy", "version": 1, "rules": []}))
    clock = FakeClock()
    made = {}

    def factory(spec):
        made["ep"] = FakeEpisode([Act(writes=[(2, "out/job-c.log", "CONFLICT: x\n")], final="Done.")], tmp_path / "drv", clock)
        made["spec"] = spec
        return made["ep"]

    args = ["run", "--task", str(DEMO), "--harness", "pi", "--model", "claude-haiku-5-5", "--upstream", "real:/nonexistent/key",
            "--mode", "coached", "--out", str(tmp_path / "out"), "--policy", f"scripted:{rules}", "--episode", "cli-1"]
    assert main(args, factory=factory, sleep=clock.sleep, utcnow=clock.utcnow) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "usable" and out["end"] == "finished" and Path(out["path"]).name == "cli-1"
    assert made["spec"].upstream == ("real", Path("/nonexistent/key")) and made["spec"].mode == "coached"


def test_run_usage_errors(capsys, tmp_path: Path) -> None:
    base = ["run", "--task", str(DEMO), "--harness", "pi", "--model", "m", "--upstream", "scripted:s.json", "--mode", "default",
            "--out", str(tmp_path)]
    assert main(base + ["--policy", "classifier:x"]) == 2
    assert main(base + ["--policy", "scripted:no-such-file.json"]) == 2
    assert "no-such-file.json" in capsys.readouterr().err
    fake = lambda spec: None  # noqa: E731 - never reached
    for bad in (["--poll-s", "0"], ["--poll-s", "nan"], ["--poll-s", "-1"], ["--episode", "has space"], ["--episode", "x" * 65], ["--episode", ""]):
        assert main(base + ["--policy", "scripted:x.json"] + bad, factory=fake) == 2, bad


def test_run_not_reached_exits_3(capsys, tmp_path: Path) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"format": "cohbench-scripted-policy", "version": 1, "rules": []}))
    clock = FakeClock()
    args = ["run", "--task", str(DEMO), "--harness", "pi", "--model", "m", "--upstream", f"scripted:{DEMO / 'scripted-run.json'}", "--mode", "default",
            "--out", str(tmp_path / "out"), "--policy", f"scripted:{rules}", "--episode", "nr"]
    assert main(args, factory=lambda spec: FakeEpisode([Act()] * 4, tmp_path / "drv", clock), sleep=clock.sleep, utcnow=clock.utcnow) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "not_reached"


def test_the_integration_marker_expression() -> None:
    """m8: `-m 'not integration'` must not start Docker."""
    from .conftest import integration_selected

    assert integration_selected("integration") and integration_selected("integration and not slow")
    assert not integration_selected("not integration") and not integration_selected("") and not integration_selected("slow and not  integration")


def test_the_classifier_kind_registers_when_present() -> None:
    """The CLI imports `cohbench.user.kind` when the classifier unit is there; it works without it."""
    import importlib.util

    from cohbench.runner.policy import POLICY_KINDS

    assert "scripted" in POLICY_KINDS
    if importlib.util.find_spec("cohbench.user") is not None and importlib.util.find_spec("cohbench.user.kind") is not None:
        assert "classifier" in POLICY_KINDS
