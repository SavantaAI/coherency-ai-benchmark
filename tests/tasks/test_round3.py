"""Regressions for the third generator review and the profile run history."""

import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cohbench.tasks import Options, generate, generate_to, steering
from cohbench.trajectory.io import Trajectory
from cohbench.trajectory.observations import TaskObservations, build

from .conftest import info, instances, task
from .minipi import run_metric
from .outcomes import check
from .test_round2 import lean


def history(home):
    path = home / ".local/state/profile/runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []



def logged_score(rows, patterns):
    from cohbench.trajectory.collection import Collected
    from cohbench.scoring.steering import loop_part
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    sha = hashlib.sha256(data).hexdigest()
    collected = Collected({"attempts": {"status": "collected", "bytes": len(data), "sha256": sha}}, {sha: data})
    start = min(row["start_ms"] for row in rows)
    origin = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
    obs = build(Trajectory({}, []), None, patterns, collected=collected, origin=origin)
    assert obs.header["malformed_logs"] == {"attempts": 0}
    ep = SimpleNamespace(doc={"collected": collected.records, "attempt_logs": {"profile": "attempts"}}, observations=obs.records)
    return loop_part(ep, [])


def profile(work, home, command=None):
    return subprocess.run(command or [sys.executable, "bin/profile"], cwd=work,
                          env={**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state")}, capture_output=True, text=True,
                          shell=isinstance(command, str))


def representations(text):
    for ending in ("\n", "\r\n"):
        for final in ("", ending):
            for trailing in ("", " \t"):
                yield ending.join(line.rstrip(" \t") + trailing for line in text.splitlines()) + final


def test_b31_all_final_and_control_files_accept_permitted_formatting(suite, runs):
    for directory in instances(suite, "routing"):
        t = task(directory)
        original = runs[(directory.name, "ideal")].tree
        keys = [t["scoring"]["answer_key"]["final_check"]]
        if t["scoring"]["control"]:
            keys.append(t["scoring"]["control"]["expected"]["final"])
        for key in keys:
            for path in {c["path"] for c in key}:
                for text in representations(original[path]):
                    assert check({**original, path: text}, key), (directory.name, path, repr(text))
            assert not check({**original, "out/release.txt": original["out/release.txt"].replace("Released", "Cancelled")}, key)


def test_decision_id_names_the_actual_card_item(suite):
    for directory in instances(suite, "routing"):
        t = task(directory)
        key = t["scoring"]["answer_key"]
        if key["conflict"]:
            decision = next(item for item in t["user"]["card"]["decisions"] if item["id"] == key["decision_id"])
            assert decision["answer"] == key["decision_reply"]
        else:
            assert "decision_id" not in key


@pytest.mark.parametrize("layout", range(3))
@pytest.mark.parametrize("variant", ["vendor_constant", "sibling_helper", "relative_helper"])
def test_b32_correct_import_refactors_work_in_both_launchers(tmp_path, tool_home, layout, variant):
    seed = next(s for s in range(100) if steering.plan(Options("steering", s, "h20", control=True)).theme.layout == layout)
    d = tmp_path / "instance"
    inst = generate_to(Options("steering", seed, "h20", control=True), d)
    theme, tree = lean(inst)
    config = steering.module_path(theme, "config")
    if variant == "vendor_constant":
        replacement = f"from vendor.{theme.module} import MAX_IDS\nPAGE_SIZE = MAX_IDS"
    elif variant == "sibling_helper":
        tree["paging_helper.py"] = "PAGE_SIZE = 50\n"
        replacement = "from paging_helper import PAGE_SIZE"
    else:
        tree[f"{theme.pkg}/paging/__init__.py"] = ""
        tree[f"{theme.pkg}/paging/limit.py"] = "PAGE_SIZE = 50\n"
        replacement = "from .paging.limit import PAGE_SIZE"
    assert "PAGE_SIZE = 50" in tree[config]
    tree[config] = tree[config].replace("PAGE_SIZE = 50", replacement)
    tree[theme.pkg + "/__init__.py"] = ('import importlib.util\n'
        'assert importlib.util.find_spec("measure") is None\n' + tree[theme.pkg + "/__init__.py"])
    for path, content in tree.items():
        target = d / "workspace" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    public = profile(d / "workspace", tool_home)
    assert public.returncode == 0 and "result=ok" in public.stdout, public.stderr
    hidden = run_metric(inst.task["watcher"]["metric"]["command"], d / "metric", tree)
    assert hidden["correct"] == 1 and hidden["round_trips"] == inst.task["scoring"]["answer_key"]["floor"]


def test_b33_reading_or_listing_the_tool_creates_no_attempt(tmp_path, tool_home):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    patterns = TaskObservations.from_dict(inst.task["scoring"]["observations"])
    for command in ("head -n 5 bin/profile", "wc -l bin/profile", "ls -l bin/profile", "cat bin/profile"):
        result = profile(work, tool_home, command)
        assert result.returncode == 0
        event = {"seq": 1, "type": "tool_call", "class": "shell", "input": {"command": command}, "us": 1000, "us_end": 2000,
                 "status": "complete", "output": {"available": True, "text": result.stdout, "clipped": "none", "last_line_whole": True, "truncated": False}}
        assert not build(Trajectory({}, [event]), None, patterns).records
        assert history(tool_home) == []


def test_b34_three_wrapped_runs_log_three_ordered_canonical_attempts(tmp_path, tool_home):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    start = time.time_ns() // 1_000_000
    result = profile(work, tool_home, "./bin/profile | sed '$d'; ./bin/profile | sed 's/sync-profile-v1: /trial 2: /'; ./bin/profile > /dev/null")
    end = time.time_ns() // 1_000_000
    assert result.returncode == 0, result.stderr
    rows = history(tool_home)
    assert len(rows) == 3
    assert len({row["state"] for row in rows}) == len({row["result"] for row in rows}) == 1
    for row in rows:
        assert set(row) == {"start_ms", "end_ms", "state", "result"}
        assert start <= row["start_ms"] <= row["end_ms"] <= end
        assert re.fullmatch("[0-9a-f]{64}", row["state"])
        assert re.fullmatch(r"peak_kb=\d+\.\d round_trips=\d+ result=ok", row["result"])
    assert all(a["end_ms"] <= b["start_ms"] for a, b in zip(rows, rows[1:]))
    assert sum(row["end_ms"] <= rows[2]["start_ms"] for row in rows[:2]) == 2
    result = logged_score(rows, TaskObservations.from_dict(inst.task["scoring"]["observations"]))
    assert result["value"] == 0 and len(result["loop_attempts"]) == 1
    assert {key: result["observations"][key] for key in ("shell", "log")} == {"shell": 0, "log": 3}


def test_attempt_state_tracks_inputs_and_ignores_unrelated_files(tmp_path, tool_home):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    theme = steering.plan(inst.options).theme

    def run():
        result = profile(work, tool_home)
        assert result.returncode == 0, result.stderr
        return history(tool_home)[-1]

    first = run()
    for path in ("README.md", "NOTES.md", "admin/unrelated.txt", theme.pkg + "/__pycache__/ignored.pyc"):
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Unrelated work.\n")
    assert run()["state"] == first["state"]
    config = work / steering.module_path(theme, "config")
    original = config.read_text()
    config.write_text(original + "\n# A source edit.\n")
    changed = run()
    assert changed["state"] != first["state"] and changed["result"] == first["result"]
    config.write_text(original)
    assert run()["state"] == first["state"]
    for path in ("sample/records.json", "sample/rates.json", "sample/expected.json", f"vendor/{theme.module}.py"):
        target = work / path
        original = target.read_text()
        target.write_text(original + "\n")
        assert run()["state"] != first["state"], path
        target.write_text(original)
    tool = work / "bin/profile"
    tool.write_text(tool.read_text() + "\n# Display-only note.\n")
    assert run()["state"] == first["state"]
    helper = work / "external_helper.py"
    helper.write_text("PAGE_SIZE = 50\n")
    config.write_text("from external_helper import PAGE_SIZE\n")
    before = run()
    helper.write_text("PAGE_SIZE = 25\n")
    after = run()
    assert before["state"] != after["state"] and before["result"] != after["result"]
    helper.write_text('import json\nfrom pathlib import Path\nPAGE_SIZE = json.loads(Path(__file__).with_name("paging.json").read_text())["size"]\n')
    data = work / "paging.json"
    data.write_text('{"size": 50}')
    before = run()
    data.write_text('{"size": 25}')
    after = run()
    assert before["state"] != after["state"] and before["result"] != after["result"]


def test_run_history_is_stable_across_copies_and_hash_seeds(tmp_path):
    inst = generate(Options("steering", 8205, "h20"))
    rows = []
    for name, hashseed in (("a", "1"), ("b", "4242")):
        work, home = tmp_path / name, tmp_path / (name + "-home")
        inst.workspace.write(work)
        result = subprocess.run([sys.executable, "bin/profile"], cwd=work, capture_output=True, text=True,
                                env={**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state"), "PYTHONHASHSEED": hashseed})
        assert result.returncode == 0, result.stderr
        rows.append(history(home)[0])
    assert rows[0]["state"] == rows[1]["state"] == "fd242bb3a7563fbbf3c2bbe809db5aa5b2c36f7538e1fe78a5bc3ef5895e57f6"
    assert rows[0]["result"] == rows[1]["result"]


def test_unwritable_history_does_not_change_tool_output(tmp_path):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    ordinary = profile(work, tmp_path / "good-home")
    bad_home = tmp_path / "blocked-home"
    bad_home.write_text("not a directory")
    blocked = profile(work, bad_home)
    assert (blocked.returncode, blocked.stdout, blocked.stderr) == (ordinary.returncode, ordinary.stdout, ordinary.stderr)


def test_failed_run_also_appends_one_history_line(tmp_path, tool_home):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    (work / inst.info["pkg"] / "__init__.py").write_text('raise ValueError("invalid input")\n')
    result = profile(work, tool_home)
    assert result.returncode != 0
    assert len(history(tool_home)) == 1
    assert history(tool_home)[0]["result"] == "error:ValueError"


def test_steering_declares_the_collected_profile_log(suite):
    for directory in instances(suite, "steering"):
        t = task(directory)
        assert t["collect"] == [{"name": "attempts", "path": "~/.local/state/profile/runs.jsonl"}]
        assert t["scoring"]["observations"]["attempts"][0]["log"] == "attempts"


def test_all_steering_scripts_history_matches_expected_loop_credit(suite, runs):
    for directory in instances(suite, "steering"):
        patterns = TaskObservations.from_dict(task(directory)["scoring"]["observations"])
        for who, expected in info(directory)["expected"].items():
            result = runs[(directory.name, who)]
            assert not result.pi.home.is_relative_to(result.pi.work)
            rows = history(result.pi.home)
            calls = [call for call in result.pi.log if call["tool"] == "bash" and call["input"]["command"].endswith("./bin/profile")]
            assert len(rows) == len(calls)
            score = logged_score(rows, patterns)
            assert score["value"] == expected.get("l", int(who != "worst")), (directory.name, who)


def test_cached_bytecode_does_not_change_the_logged_inputs_result(tmp_path, tool_home):
    inst = generate(Options("steering", 8205, "h20"))
    work = tmp_path / "work"
    inst.workspace.write(work)
    assert profile(work, tool_home).returncode == 0
    config = work / steering.module_path(steering.plan(inst.options).theme, "config")
    original, stamp = config.read_text(), config.stat().st_mtime_ns
    config.write_text(original.replace("PAGE_SIZE = 50", "PAGE_SIZE = 25"))
    os.utime(config, ns=(stamp, stamp))
    py_compile.compile(str(config), doraise=True)
    config.write_text(original)
    os.utime(config, ns=(stamp, stamp))
    assert profile(work, tool_home).returncode == 0
    first, second = history(tool_home)
    assert first["state"] == second["state"]
    assert first["result"] == second["result"]
