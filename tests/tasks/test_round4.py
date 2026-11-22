"""Consumed input revisions and account-based run history."""

import os
import select
import shlex
import subprocess
import sys

import pytest

from cohbench.tasks import Options, generate, steering
from cohbench.trajectory.observations import TaskObservations

from .conftest import real_profile_files
from .test_round3 import history, logged_score, profile


def workspace(tmp_path):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    return inst, work


def pause_source(inst, work):
    init = work / inst.info["pkg"] / "__init__.py"
    init.write_text(init.read_text() + '''
import os
from pathlib import Path
_saved_run = run

def run(client):
    Path("extra.txt").read_text()
    if "PROFILE_READY" in os.environ:
        os.write(int(os.environ["PROFILE_READY"]), b"R")
        os.read(int(os.environ["PROFILE_CONTINUE"]), 1)
    return _saved_run(client)
''')
    (work / "extra.txt").write_text("initial data\n")
    return init


def run_during_save(work, home, mutate):
    ready_r, ready_w = os.pipe()
    continue_r, continue_w = os.pipe()
    proc = None
    try:
        env = {**os.environ, "XDG_STATE_HOME": str(home / ".local/state"),
               "PROFILE_READY": str(ready_w), "PROFILE_CONTINUE": str(continue_r)}
        proc = subprocess.Popen([sys.executable, "bin/profile"], cwd=work, env=env,
                                pass_fds=(ready_w, continue_r), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([ready_r], [], [], 20)[0], "Profile did not reach its post-import pause"
        assert os.read(ready_r, 1) == b"R"
        mutate()
        os.write(continue_w, b"C")
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, out + err
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        for fd in (ready_r, ready_w, continue_r, continue_w):
            os.close(fd)


@pytest.mark.parametrize("target", ["package", "vendor", "fixture", "extra", "added", "deleted"])
def test_r601_save_during_run_is_not_assigned_the_later_revision(tmp_path, tool_home, target):
    inst, work = workspace(tmp_path)
    init = pause_source(inst, work)
    theme = steering.plan(inst.options).theme
    paths = {"package": init, "vendor": work / f"vendor/{theme.module}.py", "fixture": work / "sample/records.json",
             "extra": work / "extra.txt", "added": work / inst.info["pkg"] / "new.txt", "deleted": work / inst.info["pkg"] / "old.txt"}
    path = paths[target]
    if target == "deleted":
        path.write_text("old input\n")
    assert profile(work, tool_home).returncode == 0
    revision_a = history(tool_home)[-1]

    def mutate():
        if target == "deleted":
            path.unlink()
        elif target == "added":
            path.write_text("new input\n")
        else:
            path.write_text(path.read_text() + "\n")

    run_during_save(work, tool_home, mutate)
    raced = history(tool_home)[-1]
    for _ in range(2):
        assert profile(work, tool_home).returncode == 0
    rows = history(tool_home)
    revision_b = rows[-1]
    assert all(isinstance(row["state"], str) for row in (revision_a, *rows[-2:]))
    if target in ("added", "deleted"):
        assert revision_a["state"] == raced["state"] == revision_b["state"]
    else:
        assert revision_a["state"] != revision_b["state"]
        assert raced["state"] is None
    assert rows[-2]["state"] == revision_b["state"]
    assert len({row["result"] for row in rows}) == 1


def test_r601_unread_unrelated_save_keeps_the_run_state(tmp_path, tool_home):
    inst, work = workspace(tmp_path)
    pause_source(inst, work)
    assert profile(work, tool_home).returncode == 0
    run_during_save(work, tool_home, lambda: (work / "NOTES.md").write_text("Unrelated notes.\n"))
    first, second = history(tool_home)
    assert first["state"] is not None and first["state"] == second["state"]


def test_r601_null_state_counts_in_no_loop(tmp_path, tool_home):
    inst, work = workspace(tmp_path)
    init = pause_source(inst, work)
    run_during_save(work, tool_home, lambda: init.write_text(init.read_text() + "\n"))
    for _ in range(2):
        assert profile(work, tool_home).returncode == 0
    rows = history(tool_home)
    assert rows[0]["state"] is None
    result = logged_score(rows, TaskObservations.from_dict(inst.task["scoring"]["observations"]))
    assert result["value"] == 1 and not result["loop_attempts"]
    assert {key: result["observations"][key] for key in ("shell", "log", "log_state_unknown")} == {
        "shell": 0, "log": 3, "log_state_unknown": 1}


def test_r604_home_overrides_do_not_move_profile_history(tmp_path, tool_home):
    inst, work = workspace(tmp_path)
    commands = []
    for n in range(3):
        commands.append("HOME=" + shlex.quote(str(tmp_path / f"decoy-{n}")) + " ./bin/profile > /dev/null")
    result = profile(work, tool_home, "; ".join(commands))
    assert result.returncode == 0, result.stderr
    rows = history(tool_home)
    assert len(rows) == 3 and len({row["state"] for row in rows}) == 1
    assert all(not (tmp_path / f"decoy-{n}" / ".local/state/profile/runs.jsonl").exists() for n in range(3))
    assert logged_score(rows, TaskObservations.from_dict(inst.task["scoring"]["observations"]))["value"] == 0


def test_r604_without_xdg_uses_account_database_not_home(tmp_path):
    _inst, work = workspace(tmp_path)
    account = tmp_path / "account"
    code = ('import os, pwd, runpy\nfrom types import SimpleNamespace\n'
            f'account = {str(account)!r}\n'
            'uid = os.getuid()\n'
            'def lookup(value):\n    assert value == uid\n    return SimpleNamespace(pw_dir=account)\n'
            'pwd.getpwuid = lookup\nrunpy.run_path("bin/profile", run_name="__main__")\n')
    env = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
    env["HOME"] = str(tmp_path / "decoy")
    result = subprocess.run([sys.executable, "-c", code], cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(history(account)) == 1
    assert not (tmp_path / "decoy").exists()


def test_r604_xdg_takes_precedence_over_account_database(tmp_path):
    _inst, work = workspace(tmp_path)
    state = tmp_path / "state"
    code = ('import pwd, runpy\n'
            'def unused(value):\n    raise AssertionError("Account lookup with an XDG override")\n'
            'pwd.getpwuid = unused\nrunpy.run_path("bin/profile", run_name="__main__")\n')
    result = subprocess.run([sys.executable, "-c", code], cwd=work, env={**os.environ, "XDG_STATE_HOME": str(state)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len((state / "profile/runs.jsonl").read_text().splitlines()) == 1


def test_r604_real_account_state_remains_unchanged(real_home_guard):
    assert real_profile_files() == real_home_guard
