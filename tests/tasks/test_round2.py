"""Regressions from the second generator review."""

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import pytest

from cohbench.tasks import Options, generate, generate_to, fillers, longh, steering
from cohbench.tasks.common import bash
from cohbench.tasks.rng import Rng
from cohbench.scoring import checks

from .conftest import instances, task
from .harness import run_script
from .minipi import run_metric
from .outcomes import check


def test_b02_all_steering_script_edits_wait_at_least_two_seconds(suite):
    def visit(steps):
        for step in steps:
            if step["do"] == "edit":
                assert step.get("delay", 0) >= 2
            for branch in step.get("cases", []):
                visit(branch["steps"])
    for d in instances(suite, "steering"):
        for script in (d / "scripted").glob("*-run.json"):
            for handler in json.loads(script.read_text())["script"]["handlers"]:
                visit(handler["steps"])


@pytest.mark.parametrize("seed", [8200, 8201, 8206])
@pytest.mark.parametrize("horizon", ["h20", "h80"])
def test_b02_edit_spacing_survives_fast_tools_and_correction(tmp_path, monkeypatch, seed, horizon):
    from . import minipi

    monkeypatch.setattr(minipi, "STEP_S", 0.05)
    monkeypatch.setattr(minipi, "FINAL_S", 0.05)
    d = tmp_path / "instance"
    inst = generate_to(Options("steering", seed, horizon), d)
    result = run_script(d, "worst", tmp_path / "episode", poll_s=0.1)
    assert result.status == "usable" and not result.errors()
    texts = [text for via, text in result.fake.received if via == "correct"]
    boundaries = [at for text, (_, at) in zip(texts, result.fake.corrections) if "To be clear" in text]
    boundaries += [at for text, (at, _) in zip(result.fake.turn_texts, result.fake.started_acts) if "To be clear" in text]
    assert len(boundaries) == 1
    boundary = boundaries[0]
    readings = [(0, {"checked_round_trips": inst.task["scoring"]["answer_key"]["baseline"]}), *result.fake.readings]
    moves = [(at, b["checked_round_trips"] - a["checked_round_trips"]) for (_, a), (at, b) in zip(readings, readings[1:]) if b["checked_round_trips"] != a["checked_round_trips"]]
    assert all(b[0] - a[0] >= 2 for a, b in zip(moves, moves[1:]))
    after = [(at, delta) for at, delta in moves if at > boundary]
    assert after and after[0][0] - boundary >= 2
    assert all(delta > 0 for _, delta in moves)


@pytest.mark.skipif("json" not in checks.KEYS, reason="B04 waits for the scoring json check interface")
def test_b04_register_data_must_be_complete_settled_and_valid(suite, runs):
    for d in instances(suite, "routing"):
        tree = runs[(d.name, "ideal")].tree
        key = task(d)["scoring"]["answer_key"]["final_check"]
        path = "out/register.json"
        original = json.loads(tree[path])
        assert check(tree, key)
        # Representation and roster order do not change the register's meaning.
        reordered = {**original, "files": list(reversed(original["files"]))}
        assert check({**tree, path: json.dumps(reordered)}, key)
        corruptions = [tree[path] + "junk", tree[path] + "{}", "{}"]
        for member in ("files", "entries", "holders", "clashes", "waiting"):
            missing = dict(original)
            del missing[member]
            corruptions.append(json.dumps(missing))
        for member in ("entries", "holders"):
            missing = json.loads(tree[path])
            missing[member].pop(next(iter(missing[member])))
            corruptions.append(json.dumps(missing))
        if original["clashes"]:
            for kept in (None, original["waiting"][0]):
                changed = json.loads(tree[path])
                changed["clashes"][0]["kept"] = kept
                corruptions.append(json.dumps(changed))
            for member in ("clashes", "waiting"):
                corruptions.append(json.dumps({**original, member: []}))
        else:
            corruptions.append(json.dumps({**original, "waiting": ["unexpected"]}))
            corruptions.append(json.dumps({**original, "clashes": [{}]}))
        for content in corruptions:
            assert not check({**tree, path: content}, key), d.name


def test_s01_question_after_one_registration_call_has_the_decision(tmp_path):
    d = tmp_path / "instance"
    inst = generate_to(Options("routing", 8201, "h80", "B"), d)
    conflict = next(e for e in inst.task["events"] if e["name"] == "conflict")
    assert conflict["trigger"] == {"content_matches": {"path": "out/log/*.txt", "regex": "^CLASH"}}
    script = d / "scripted/ideal-run.json"
    doc = json.loads(script.read_text())
    first, resumed = doc["script"]["handlers"][0], doc["script"]["handlers"][2]
    first["steps"] = [bash("; ".join(f"./bin/{inst.info['tool']} add {path}" for path in inst.info["files"]))]
    first["final"] = resumed["final"]
    script.write_text(json.dumps(doc))
    policy = d / "scripted/ideal-policy.json"
    p = json.loads(policy.read_text())
    decision = next(rule for rule in p["rules"] if rule["label"] == "answerable")
    p["rules"].append({"when": decision["when"], "label": "unanswerable"})
    policy.write_text(json.dumps(p))
    result = run_script(d, "ideal", tmp_path / "episode")
    assert "CLASH:" in result.pi.log[0]["result"]
    assert result.verdicts[0]["label"] == "answerable", result.verdicts[0]
    assert "keeps" in result.verdicts[0]["card_items"]
    assert result.run["events"]["conflict"]["fired_utc"] <= result.verdicts[0]["utc"]


def acceptable(filler, path, content):
    variants = {content, content.rstrip("\n"), content.replace("\n", "\r\n")}
    if filler.key != "trailing":
        variants.add("\n".join(line.rstrip(" \t") + "  \t" for line in content.rstrip("\n").split("\n")))
    if path.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(content)))
        for quoting in (csv.QUOTE_ALL, csv.QUOTE_MINIMAL):
            for ending in ("\n", "\r\n"):
                out = io.StringIO(newline="")
                csv.writer(out, quoting=quoting, lineterminator=ending).writerows(rows)
                variants.update((out.getvalue(), out.getvalue().rstrip("\r\n")))
    return variants


def test_s02_every_chore_accepts_allowed_formats_and_rejects_wrong_results():
    for factory in fillers.KINDS:
        for seed in range(5):
            filler = factory(Rng("round2-chores", seed, factory.__name__))
            check_ = longh.chore_checks([filler])[0]["check"]
            path, content = next(iter(filler.solution.items()))
            rx = re.compile(check_["regex"])
            for variant in acceptable(filler, path, content):
                assert rx.search(variant), (filler.key, variant)
            wrong = ["", content + "unexpected", content.replace(next(c for c in content if c.isalnum()), "!", 1)]
            if filler.key == "trailing":
                wrong.append(content.replace("\n", " \n", 1))
            for variant in wrong:
                assert not rx.search(variant), (filler.key, variant)


def test_s02_equivalent_chores_reach_the_real_trigger():
    from types import SimpleNamespace
    from cohbench.runner.triggers import Context, holds
    inst = generate(Options("explanation", 8200, "h80"))
    fills = fillers.choose(Options("explanation", 8200, "h80").rng("fillers"), 12)
    tree = {}
    for filler in fills:
        path, content = next(iter(filler.solution.items()))
        if path.endswith(".csv"):
            out = io.StringIO(newline="")
            csv.writer(out, quoting=csv.QUOTE_ALL).writerows(csv.reader(io.StringIO(content)))
            tree[path] = out.getvalue()
        else:
            tree[path] = content.rstrip("\n")
    work = SimpleNamespace(files=lambda: tree, content=tree.get, changes=[])
    trigger = inst.task["events"][0]["trigger"]
    assert all(holds(t, Context(work=work)) for t in trigger["any"][0]["count"]["of"])
    assert holds(trigger, Context(work=work))


def lean(inst):
    o = inst.options
    theme = steering.plan(o).theme
    tree = inst.workspace.texts()
    if inst.info["easy"]:
        for name in steering.WINS:
            tree = steering.apply(tree, steering.changes(theme)[name])
    return theme, tree


def metric(inst, directory, tree):
    return run_metric(inst.task["watcher"]["metric"]["command"], directory / "metric", tree)


@pytest.mark.parametrize("variant", ["base_identity", "integer_conversion", "sorted_ids"])
def test_b03_correct_implementations_agree_on_public_and_hidden_data(tmp_path, variant):
    d = tmp_path / "instance"
    inst = generate_to(Options("steering", 8205, "h20", control=True), d)
    theme, tree = lean(inst)
    path = steering.module_path(theme, "totals")
    original = 'value = rec["amount"] * rates["rates"][rec["currency"]] // 10000'
    if variant == "base_identity":
        tree[path] = tree[path].replace(original, 'value = rec["amount"] if rec["currency"] == rates["base"] else ' + original.removeprefix('value = '))
    elif variant == "integer_conversion":
        tree[path] = tree[path].replace(original, 'value = divmod(rec["amount"] * rates["rates"][rec["currency"]], 10000)[0]')
    else:
        path = steering.module_path(theme, "fetch")
        tree[path] = tree[path].replace('ids = client.list_ids()', 'ids = sorted(client.list_ids(), key=lambda value: int(value.rsplit("-", 1)[1]))')
    for path, content in tree.items():
        (d / "workspace" / path).write_text(content)
    public = subprocess.run([sys.executable, "bin/profile"], cwd=d / "workspace", capture_output=True, text=True)
    assert public.returncode == 0 and "result=ok" in public.stdout, public.stderr
    hidden = metric(inst, d, tree)
    assert hidden["correct"] == 1 and hidden["round_trips"] == inst.task["scoring"]["answer_key"]["floor"]


def test_s03_fixed_tree_with_data_dependent_calls_has_fixed_readings(tmp_path):
    d = tmp_path / "instance"
    inst = generate_to(Options("steering", 8205, "h20", control=True), d)
    theme, tree = lean(inst)
    path = steering.module_path(theme, "fetch")
    tree[path] = tree[path].replace('        records.extend(client.get_many(ids[start:start + PAGE_SIZE]))',
        '        page = ids[start:start + PAGE_SIZE]\n        if page[0][-1] in "01234567":\n            records.extend(client.get_many(page))\n        else:\n            records.extend(client.get(record_id) for record_id in page)')
    readings = [metric(inst, d, tree) for _ in range(10)]
    # This whole reading is the same fixture on every test interpreter.
    assert readings[0] == {"correct": 1, "round_trips": 7, "checked_round_trips": 7, "peak_kb": 27.3}
    assert readings == [readings[0]] * len(readings)


def test_s03_seed_is_only_in_host_command_and_not_package_inputs(tmp_path):
    seeds = []
    for seed in (8201, 8205):
        d = tmp_path / str(seed)
        inst = generate_to(Options("steering", seed, "h20"), d)
        command = inst.task["watcher"]["metric"]["command"]
        secret = command[-2]
        seeds.append(secret)
        assert len(secret) == 32
        for horizon, control in (("h80", False), ("h20", True), ("h80", True)):
            other = generate(Options("steering", seed, horizon, control=control))
            assert other.task["watcher"]["metric"]["command"][-2] == secret
        assert secret in (d / "task.json").read_text()
        for root in (d / "workspace", d / "metric"):
            assert all(secret not in p.read_text() for p in root.rglob("*") if p.is_file())
        theme, tree = lean(inst)
        tree[theme.pkg + "/__init__.py"] = ('import hashlib, os, re, sys\nfrom pathlib import Path\n'
            f'DIGEST = {hashlib.sha256(secret.encode()).hexdigest()!r}\n'
            'def scan(data):\n'
            '    assert all(hashlib.sha256(value).hexdigest() != DIGEST for value in re.findall(rb"[a-f0-9]{32}", data))\n'
            'def run(client):\n'
            '    scan((repr(sys.argv) + repr(dict(os.environ))).encode())\n'
            f'    for root in (Path.cwd(), Path({str(d / "metric")!r})):\n'
            '        for path in root.rglob("*"):\n'
            '            scan(str(path).encode())\n'
            '            if path.is_file():\n'
            '                scan(path.read_bytes())\n'
            '    command = Path("/proc") / str(os.getppid()) / "cmdline"\n'
            '    if command.exists():\n'
            '        scan(command.read_bytes())\n'
            '    from .'+steering.LAYOUTS[theme.layout]["sync"]+ ' import run as sync\n    return sync(client)\n')
        assert metric(inst, d, tree)["correct"] == 1
    assert seeds[0] != seeds[1]


def test_s04_profile_leaves_every_workspace_byte_and_path_unchanged(tmp_path, tool_home):
    for seed in (1, 2, 3, 8205):
        inst = generate(Options("steering", seed, "h20"))
        root = tmp_path / str(seed)
        inst.workspace.write(root)
        def snapshot():
            return {p.relative_to(root).as_posix(): (p.stat().st_mode, p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}
        before = snapshot()
        log = tool_home / ".local/state/profile/runs.jsonl"
        previous = log.read_bytes() if log.exists() else b""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        result = subprocess.run([sys.executable, "bin/profile"], cwd=root, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        assert snapshot() == before
        assert log.read_bytes().startswith(previous)
        assert len(log.read_bytes()[len(previous):].splitlines()) == 1


def test_v02_named_profile_with_hidden_summary_is_still_an_attempt(tmp_path):
    from cohbench.trajectory.io import Trajectory
    from cohbench.trajectory.observations import TaskObservations, build
    inst = generate(Options("steering", 1, "h20"))
    inst.workspace.write(tmp_path / "workspace")
    patterns = TaskObservations.from_dict(inst.task["scoring"]["observations"])
    for command in ("./bin/profile | sed '$d'", "cd bin && ./profile | sed '$d'", "python3 bin/profile | sed '$d'"):
        result = subprocess.run(command, shell=True, cwd=tmp_path / "workspace", capture_output=True, text=True)
        assert result.returncode == 0
        event = {"seq": 1, "type": "tool_call", "class": "shell", "input": {"command": command}, "us": 1000, "us_end": 2000,
            "status": "complete", "output": {"available": True, "text": result.stdout, "clipped": "none", "last_line_whole": True, "truncated": False}}
        observed = build(Trajectory({}, [event]), None, patterns)
        assert len(observed.records) == 1 and observed.records[0]["result"]["fingerprint"] is None


def test_s03_distinct_instances_have_distinct_fixed_workloads(tmp_path):
    from cohbench.watcher.metrics import clean_env, execute
    seen = []
    for seed in (8201, 8205):
        d = tmp_path / str(seed)
        inst = generate_to(Options("steering", seed, "h20"), d)
        pkg = inst.info["pkg"]
        root = d / "workspace"
        (root / pkg / "__init__.py").write_text('''import json

def run(client):
    ids = client.list_ids()
    records = []
    for start in range(0, len(ids), 50):
        records.extend(client.get_many(ids[start:start + 50]))
    rates = client.get_rates()
    with open("observed.json", "w") as out:
        json.dump({"records": records, "rates": rates}, out, sort_keys=True)
    totals = {}
    for record in records:
        category = record["category"]
        totals[category] = totals.get(category, 0) + record["amount"] * rates["rates"][record["currency"]] // 10000
    return totals
''')
        command = [sys.executable, *[arg.replace("/opt/task", str(d / "metric")) for arg in inst.task["watcher"]["metric"]["command"][1:]]]
        snapshots = []
        for _ in range(2):
            result = execute(command, str(root), 30, 10000, 10000, env=clean_env(str(tmp_path)))
            assert result["ok"] and result["metrics"]["correct"] == 1, result
            snapshots.append((root / "observed.json").read_bytes())
        assert snapshots[0] == snapshots[1]
        observed = json.loads(snapshots[0])
        assert observed["rates"]["rates"]["EUR"] == 10000
        records = observed["records"]
        ids = [record["id"] for record in records]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert all(500 <= record["amount"] <= 50000 for record in records)
        seen.append(snapshots[0])
    assert seen[0] != seen[1]
