"""Every generated instance is admitted by the runner with each of its
scripts and policies, its host-only files stay out of the workspace, its
files are what D-031 and D-032 require, its Python runs on 3.11, and its
answer key carries what the scoring unit reads."""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from cohbench.runner import ScriptedPolicy, check
from cohbench.runner.task import INSPECT_MAX
from cohbench.scoring import checks
from cohbench.scoring.controls import KEYS as CONTROL_KEYS
from cohbench.scripted import load_script
from cohbench.tasks.common import BUDGETS, MAX_REOPENS, RETURN_DELAY_S, workspace_mtime, Options

import pytest

from cohbench.tasks.common import task_digest
from cohbench.tasks.suite import write_suite

from .conftest import RUNNER_D046, WAITS, info, instances, task


def test_the_suite_has_what_the_brief_asks_for(suite):
    rows = json.loads((suite / "suite.json").read_text())["instances"]
    assert len(rows) == len(instances(suite))
    for fam in ("routing", "explanation", "steering"):
        for h in ("h20", "h80"):
            cell = [r for r in rows if r["family"] == fam and r["horizon"] == h]
            ctl = [r for r in cell if r["control"]]
            assert len(ctl) == 6
            scored = [r for r in cell if not r["control"]]
            if fam == "routing":
                pairs = {r["pair"] for r in scored if r["pair"]}
                assert len(pairs) == 6 and all(sum(1 for r in scored if r["pair"] == p) == 2 for p in pairs)
                none = [r for r in scored if r["pair"] is None]
                assert len(none) * 4 == len({r["seed"] for r in scored})  # a quarter of the seeds
            else:
                assert len(scored) == 6
    steer = [task(d)["scoring"]["answer_key"]["variant"] for d in instances(suite, "steering") if not task(d)["grouping"]["control"]]
    assert steer.count("easy_wins") == steer.count("no_valid_improvement")


def _waits(d: Path) -> bool:
    """Explanation at h80 uses the follow-up form the runner admits once its D-046 change lands."""
    return not RUNNER_D046 and task(d)["grouping"]["family"] == "explanation" and task(d)["grouping"]["horizon"] == "h80"


def _scripts(d: Path) -> list[str]:
    return sorted(p.name[: -len("-run.json")] for p in (d / "scripted").glob("*-run.json"))


def test_every_instance_is_admitted_with_each_script_and_policy(suite):
    for d in instances(suite):
        if _waits(d):
            continue
        assert _scripts(d) == (["ideal", "ideal-grace", "worst"] if task(d)["grouping"]["horizon"] == "h80" else ["ideal", "worst"])
        for who in _scripts(d):
            hosts = {"user policy": d / "scripted" / f"{who}-policy.json", "model script": d / "scripted" / f"{who}-run.json"}
            t, problems, _warnings = check(d, hosts)
            assert t is not None and not problems, (d.name, who, problems)


@pytest.mark.skipif(RUNNER_D046, reason="the runner admits the D-046 form now; the test above covers it")
def test_explanation_h80_is_refused_today_only_for_the_follow_up_form(suite):
    """While the runner lacks D-046, its admission refuses these instances for
    the follow-up's trigger and nothing else."""
    for d in instances(suite, "explanation"):
        if task(d)["grouping"]["horizon"] != "h80":
            continue
        _t, problems, _w = check(d, {"user policy": d / "scripted" / "ideal-policy.json", "model script": d / "scripted" / "ideal-run.json"})
        assert problems and all("follow_up" in p for p in problems), problems


@pytest.mark.skipif(not RUNNER_D046, reason=WAITS)
def test_explanation_h80_is_admitted_once_the_runner_has_d046(suite):
    for d in instances(suite, "explanation"):
        if task(d)["grouping"]["horizon"] == "h80":
            for who in _scripts(d):
                _t, problems, _w = check(d, {"user policy": d / "scripted" / f"{who}-policy.json", "model script": d / "scripted" / f"{who}-run.json"})
                assert not problems, (d.name, who, problems)


def test_the_check_task_command_admits_an_instance_of_each_family(suite):
    for fam in ("routing", "explanation", "steering"):
        d = instances(suite, fam)[0]
        r = subprocess.run([sys.executable, "-m", "cohbench.runner", "check-task", str(d), "--policy", str(d / "scripted" / "ideal-policy.json"),
                            "--script", str(d / "scripted" / "ideal-run.json")], capture_output=True, text=True, cwd=Path(__file__).parents[2])
        assert r.returncode == 0, r.stdout + r.stderr


def test_every_script_loads_in_the_scripted_model_and_every_policy_is_valid(suite):
    for d in instances(suite):
        for who in _scripts(d):
            doc = json.loads((d / "scripted" / f"{who}-run.json").read_text())
            assert doc["format"] == "cohbench-scripted-run" and doc["version"] == 1
            load_script(doc["script"])
            pol = json.loads((d / "scripted" / f"{who}-policy.json").read_text())
            assert ScriptedPolicy.check(pol) == [], (d.name, who)


def test_host_only_files_are_outside_the_workspace(suite):
    for d in instances(suite):
        ws = (d / "workspace").resolve()
        for p in [d / "task.json", d / "workspace.manifest.json", *sorted((d / "scripted").iterdir())]:
            assert not p.resolve().is_relative_to(ws)
        if (d / "metric").exists():
            assert not (d / "metric").resolve().is_relative_to(ws)
        names = {p.name for p in (d / "workspace").rglob("*")}
        assert not names & {"task.json", "info.json", "suite.json"} and not any(n.endswith(("-run.json", "-policy.json")) for n in names)


def test_workspace_files_are_readable_single_named_small_and_as_the_manifest_says(suite):
    for d in instances(suite):
        m = json.loads((d / "workspace.manifest.json").read_text())
        o = Options(task(d)["grouping"]["family"], task(d)["grouping"]["seed"], task(d)["grouping"]["horizon"])
        assert m["mtime"] == workspace_mtime(o)
        rows = {r["path"]: r for r in m["entries"]}
        on_disk = {p.relative_to(d / "workspace").as_posix(): p for p in (d / "workspace").rglob("*")}
        assert set(rows) == set(on_disk)
        for rel, p in on_disk.items():
            st = p.lstat()
            assert not stat.S_ISLNK(st.st_mode)
            assert f"{stat.S_IMODE(st.st_mode):04o}" == rows[rel]["mode"]
            if stat.S_ISREG(st.st_mode):
                assert st.st_nlink == 1, rel
                assert st.st_size <= INSPECT_MAX, rel
                assert os.access(p, os.R_OK), rel
                assert rows[rel]["mode"] in ("0644", "0755"), rel
            else:
                assert rows[rel]["type"] == "dir" and rows[rel]["mode"] == "0755"


def _python_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and (p.suffix == ".py" or p.read_bytes().startswith(b"#!/usr/bin/env python3")):
            out.append(p)
    return out


def test_every_python_file_in_a_workspace_or_metric_parses_as_python_3_11(suite):
    seen = 0
    for d in instances(suite):
        for root in (d / "workspace", d / "metric"):
            if root.exists():
                for p in _python_files(root):
                    ast.parse(p.read_text(encoding="utf-8"), filename=str(p), feature_version=(3, 11))
                    seen += 1
    assert seen > 100


def test_budgets_return_delay_and_reopens_are_the_provisional_numbers(suite):
    delays = set()
    for d in instances(suite):
        t = task(d)
        assert t["budgets"] == BUDGETS[t["grouping"]["horizon"]]
        assert t["reachability"]["max_reopens"] == MAX_REOPENS
        delay = t["user"]["return_delay_ms"]
        assert RETURN_DELAY_S[0] * 1000 <= delay <= RETURN_DELAY_S[1] * 1000 and delay % 1000 == 0
        delays.add(delay)
    assert len(delays) > 5  # varied by seed


def test_the_user_card_always_says_the_user_cannot_see_the_files(suite):
    for d in instances(suite):
        facts = task(d)["user"]["card"]["facts"]
        assert any(f["id"] == "no_files" and "can't see your files" in f["text"] for f in facts), d.name


def test_answer_keys_carry_what_the_scoring_unit_reads(suite):
    for d in instances(suite):
        t = task(d)
        k, g = t["scoring"]["answer_key"], t["grouping"]
        fam = g["family"]
        if fam == "routing":
            checks.parse(k["final_check"])
            if k["conflict"]:
                assert k["conflict_event"] == "conflict" and any(e["name"] == "conflict" for e in t["events"])
                decision = next(x for x in t["user"]["card"]["decisions"] if x["id"] == "keeps")
                assert k["decision_reply"] == decision["answer"]
                assert k["settled_by_workspace"] is (g["sibling"] == "A")
                assert k["decision_reply"] in k["card_texts"]
            else:
                assert "conflict_event" not in k and not any(e["name"] == "conflict" for e in t["events"])
        if fam == "routing":
            card = t["user"]["card"]
            assert k["card_texts"] == [f["text"] for f in card["facts"]] + [x["answer"] for x in card["decisions"]]
            fixed = [t["prompt"]["opening"], *t["user"]["replies"].values()]
            for e in t["events"]:
                fixed += [e["action"][x] for x in ("text", "notice", "continuation") if x in e["action"]] + ([e["reopen"]] if e.get("reopen") else [])
            assert sorted(k["fixed_messages"]) == sorted(fixed)
        if fam == "explanation" and g["horizon"] == "h80":
            assert k["answers_after"] == "handoff"
        if fam == "steering":
            assert k["variant"] in ("easy_wins", "no_valid_improvement")
            assert (k["variant"] == "easy_wins") is info(d)["easy"]
        c = t["scoring"]["control"]
        assert (c is not None) is g["control"]
        if c:
            assert set(c["expected"]) <= set(CONTROL_KEYS), (d.name, c)
            if "final" in c["expected"]:
                checks.parse(c["expected"]["final"])


def test_routing_decision_text_occurs_in_no_other_card_text_and_no_fixed_message(suite):
    """The scorer finds the decision in a composed reply by its text; it must
    not also be inside another card item or a fixed message (the scoring
    unit's review)."""
    for d in instances(suite, "routing"):
        k = task(d)["scoring"]["answer_key"]
        if not k["conflict"]:
            continue
        item = k["decision_reply"]
        assert [x for x in k["card_texts"] if item in x] == [item], d.name
        assert not [x for x in k["fixed_messages"] if item in x], d.name


def test_the_suite_manifest_lists_every_directory_with_the_runners_digest(suite):
    rows = json.loads((suite / "suite.json").read_text())["instances"]
    assert sorted(r["dir"] for r in rows) == sorted(d.name for d in instances(suite))
    for r in rows:
        d = suite / r["dir"]
        g = task(d)["grouping"]
        assert {k: r[k] for k in ("family", "seed", "pair", "sibling", "control", "horizon")} == g
        assert r["digest"] == task_digest(d)
        if not _waits(d):
            t, problems, _w = check(d)
            assert not problems and t.digest == r["digest"], (r["dir"], problems)


def test_regenerating_the_suite_gives_the_same_manifest(suite, tmp_path):
    write_suite(tmp_path / "again")
    assert (tmp_path / "again" / "suite.json").read_bytes() == (suite / "suite.json").read_bytes()
