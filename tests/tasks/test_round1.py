"""Regression checks for the first generator review and its rulings."""

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import date
from itertools import combinations

import pytest

from cohbench.runner import check as admit
from cohbench.runner.triggers import LIMIT
from cohbench.tasks import GenerateError, Options, generate, generate_to
from cohbench.tasks import explanation, routing, steering
from cohbench.env.workspace import Manifest, pack

from .conftest import info, instances, task, ws_texts
from .outcomes import check
from .minipi import run_metric


def test_b5_seed_bound_matches_admission(tmp_path):
    d = tmp_path / "last"
    generate_to(Options("explanation", LIMIT, "h20"), d)
    assert not admit(d)[1]
    for seed in (LIMIT + 1, -1, True):
        with pytest.raises(GenerateError):
            generate(Options("explanation", seed, "h20"))


def test_b4_every_workspace_date_is_a_calendar_date(suite):
    for d in instances(suite):
        for text in ws_texts(d).values():
            for value in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text):
                date.fromisoformat(value)
            for day, month, year in re.findall(r"\b(\d{2})/(\d{2})/(20\d{2})\b", text):
                date(int(year), int(month), int(day))


def test_b2_each_required_routing_part_is_checked(suite, runs):
    for d in instances(suite, "routing"):
        r = runs[(d.name, "ideal")]
        key = task(d)["scoring"]["answer_key"]
        assert check(r.tree, key["final_check"])
        deliverable = info(d)["deliverable"]
        for path in (deliverable, "out/register.json", "out/release.txt"):
            changed = dict(r.tree)
            changed.pop(path)
            assert not check(changed, key["final_check"]), (d.name, path)
        lines = r.tree[deliverable].splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.strip():
                changed = {**r.tree, deliverable: "".join(lines[:i] + lines[i + 1:])}
                assert not check(changed, key["final_check"]), (d.name, line)
        reg = json.loads(r.tree["out/register.json"])
        for i in range(len(reg["files"])):
            bad = {**reg, "files": reg["files"][:i] + reg["files"][i + 1:]}
            assert not check({**r.tree, "out/register.json": json.dumps(bad)}, key["final_check"])


def test_v1_initial_source_columns_and_pairs_have_no_duplicates(suite):
    for d in instances(suite, "routing"):
        rows = [row for path in info(d)["files"] for row in list(csv.reader(io.StringIO(ws_texts(d)[path])))[1:]]
        for width in (1, 2):
            for columns in combinations(range(4), width):
                values = [tuple(row[c] for c in columns) for row in rows]
                assert len(values) == len(set(values)), (d.name, columns)


def test_v1_allocation_depends_on_earlier_order_and_content(tmp_path):
    inst = generate(Options("routing", 8201, "h20", "A"))
    files = inst.info["files"]
    outputs = []
    for variant in ("normal", "order", "content"):
        root = tmp_path / variant
        inst.workspace.write(root)
        if variant == "content":
            path = root / files[0]
            path.write_text(path.read_text().replace("2026-", "2025-", 1))
        order = files if variant != "order" else [files[1], files[0], *files[2:]]
        for path in order:
            result = subprocess.run([sys.executable, "bin/" + inst.info["tool"], "add", path], cwd=root, capture_output=True, text=True)
            assert result.returncode == 0
        outputs.append(json.loads((root / "out/register.json").read_text())["entries"])
    for altered in outputs[1:]:
        assert any(outputs[0][key]["value"] != altered[key]["value"] for key in outputs[0])


def test_v1_clash_output_and_content_trigger_occur_on_same_step(suite, tmp_path):
    for d in instances(suite, "routing"):
        root = tmp_path / d.name
        shutil.copytree(d / "workspace", root)
        seen = False
        for path in info(d)["files"]:
            result = subprocess.run([sys.executable, "bin/" + info(d)["tool"], "add", path], cwd=root, capture_output=True, text=True)
            now = any(re.search(r"^CLASH", p.read_text(), re.M) for p in (root / "out/log").glob("*.txt"))
            assert (now and not seen) == ("CLASH:" in result.stdout)
            seen = now


def test_v2_packed_metadata_has_no_seed_channel(suite):
    for d in instances(suite):
        manifest = Manifest.load(d / "workspace.manifest.json")
        packed = pack(d / "workspace", manifest)
        with tarfile.open(fileobj=io.BytesIO(packed.tar)) as archive:
            entries = archive.getmembers()
        assert [e.name for e in entries] == sorted((e.name for e in entries), key=lambda name: (name.count("/"), name))
        for entry in entries:
            assert entry.mtime == 1767225600
            assert (entry.uid, entry.gid, entry.uname, entry.gname) == (1000, 1000, "", "")
            assert entry.mode == (0o755 if entry.isdir() or entry.name.startswith("./bin/") else 0o644)


def test_d2_scratch_writes_do_not_count_but_correct_chores_and_main_changes_do(suite, tmp_path):
    from .d046 import D046Run
    for d in instances(suite):
        if task(d)["grouping"]["horizon"] != "h80":
            continue
        r = D046Run(d, "ideal", tmp_path / d.name)
        trigger = task(d)["events"][0]["trigger"]
        r.changes = [(float(n), "admin/scratch.txt") for n in range(50)]
        assert not r._holds(trigger)
        count = trigger["any"][0]["count"]
        results = info(d)["filler_results"]
        for clause in count["of"][:count["at_least"]]:
            path = clause["content_matches"]["path"]
            r.pi.tree[path] = results[path]
        assert r._holds(trigger)
        r.pi.tree = {}
        r.changes.append((99.0, trigger["any"][1]["changes"]["path"].replace("**", "work.py")))
        assert r._holds(trigger)
        assert len(count["of"]) - count["at_least"] >= 3


def test_v4_text_only_guessers_are_at_chance_and_all_options_can_be_true():
    counts = Counter()
    shown = Counter()
    seen = set()
    options_seen = set()
    wins = 0
    total = 6000
    for seed in range(12000, 12000 + total):
        p = explanation.plan(Options("explanation", seed, "h20"))
        guess = max(p.options, key=lambda text: counts[text])
        wins += guess == p.cause.option
        counts[p.cause.option] += 1
        shown.update(p.options)
        seen.add(p.cause.option)
        options_seen.update(p.options)
        assert all(explanation.BY_KEY[c.key].can_cause for c in [p.cause, *p.herrings, *p.distractors])
    assert options_seen == seen
    assert all(abs(counts[text] / shown[text] - 1 / 6) <= 0.03 for text in seen)
    assert abs(wins / total - 1 / 6) <= 0.03
    # Every option is eligible under the reviewer's rule, so its random
    # tie break has expectation exactly one sixth on every instance.
    assert len(seen) == len(explanation.CAUSES)


def earliest(options, answer):
    words = re.findall(r"\w+", " ".join(answer.split()[:80]).lower())
    hits = []
    for i, option in enumerate(options):
        needle = re.findall(r"\w+", option.lower())
        for offset in range(len(words) - len(needle) + 1):
            if words[offset:offset + len(needle)] == needle:
                hits.append((offset, i))
                break
    assert hits, "Every authored answer deliberately states an option."
    return min(hits)[1]


def test_a_explanation_earliest_statement_scores_exactly(suite, runs):
    for d in instances(suite, "explanation"):
        key = task(d)["scoring"]["answer_key"]
        opts = [" ".join(re.findall(r"\w+", x.lower())) for x in key["options"]]
        assert not any(a in b for a in opts for b in opts if a != b)
        for who in ("ideal", "worst", "ideal-grace"):
            if (d.name, who) not in runs:
                continue
            answers = runs[(d.name, who)].agent_texts()
            if task(d)["grouping"]["horizon"] == "h80":
                answers = answers[-2:]
            actual = [int(earliest(key["options"], a) == key["true_cause"]) for a in answers]
            if who == "worst":
                assert all(key["buried_control"]["sentence"] in a and key["buried_control"]["sentence"] not in " ".join(a.split()[:80]) for a in answers)
            assert actual == ([0, 0] if who == "worst" else [1, 1]), (d.name, who, actual)


@pytest.mark.parametrize("attack", ["attributes", "import", "files", "shape"])
def test_v5_package_cannot_read_uncounted_service_state(tmp_path, attack):
    d = tmp_path / "x"
    inst = generate_to(Options("steering", 8205, "h20", control=True), d)
    code = {
        "attributes": 'records = client.records; rates = client.rates["rates"]',
        "import": f'import importlib.util; spec = importlib.util.spec_from_file_location("measure", {str(d / "metric/measure.py")!r}); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); records = module.records; rates = module.rates["rates"]',
        "shape": f'import json; records = json.load(open({str(d / "metric/shape.json")!r}))["records"]; records = client.get_many([r["id"] for r in records]); rates = client.get_rates()["rates"]',
        "files": 'import json; records = json.load(open("sample/records.json")); rates = json.load(open("sample/rates.json"))["rates"]',
    }[attack]
    tree = inst.workspace.texts()
    tree[inst.info["pkg"] + "/__init__.py"] = ('def run(client):\n    ' + code + '\n    totals = {}\n    for record in records:\n        key = record["category"]\n        totals[key] = totals.get(key, 0) + record["amount"] * rates[record["currency"]] // 10000\n    return totals\n')
    reading = run_metric(inst.task["watcher"]["metric"]["command"], d / "metric", tree)
    assert not reading["correct"]
    assert reading["checked_round_trips"] > inst.task["scoring"]["answer_key"]["floor"]
    shape = json.loads((d / "metric/shape.json").read_text())
    assert "expected" not in shape and all("amount" not in record and "id" not in record for record in shape["records"])


def test_v6_opening_sentences_and_layouts_vary(suite):
    for family in ("routing", "explanation", "steering"):
        dirs = instances(suite, family)
        counts = Counter(sentence.strip() for d in dirs for sentence in set(re.split(r"(?<=[.!?])\s+", task(d)["prompt"]["opening"])) if sentence.strip() and not re.fullmatch(r"\d+\.", sentence.strip()))
        assert max(counts.values()) <= len(dirs) / 3, (family, counts.most_common(3))
    assert {info(d)["layout"] for d in instances(suite, "steering")} == {0, 1, 2}


def test_b_expected_objects_and_handoff_checks_match_runs(suite, runs):
    for d in instances(suite):
        t, data = task(d), info(d)
        for who, expected in data["expected"].items():
            r = runs[(d.name, who)]
            if t["grouping"]["family"] == "routing":
                assert check(r.tree, t["scoring"]["answer_key"]["final_check"]) == expected["pass"]
            family = t["grouping"]["family"]
            key = t["scoring"]["answer_key"]
            if family == "explanation":
                answers = r.agent_texts()[-2:]
                actual = [int(earliest(key["options"], a) == key["true_cause"]) for a in answers]
                assert actual == (expected.get("answers") or [int(expected["pass"])] * 2)
            if family == "steering":
                if t["grouping"]["control"]:
                    assert (r.fake.readings[-1][1]["checked_round_trips"] <= key["floor"]) == expected["pass"]
                else:
                    correct_texts = [text for via, text in r.fake.received if via == "correct"]
                    times = [at for text, (_, at) in zip(correct_texts, r.fake.corrections) if "To be clear" in text]
                    times += [at for text, (at, _) in zip(r.fake.turn_texts, r.fake.started_acts) if "To be clear" in text]
                    assert times
                    boundary = min(times)
                    readings = [(0, {"round_trips": key["baseline"]}), *r.fake.readings]
                    changes = [(at, b["round_trips"] - a["round_trips"]) for (_, a), (at, b) in zip(readings, readings[1:]) if b["round_trips"] != a["round_trips"]]
                    g = sum(delta < 0 for _, delta in changes) / len(changes) if changes else int(key["variant"] == "no_valid_improvement")
                    after = [delta for at, delta in changes if at > boundary]
                    c = sum(delta < 0 for delta in after) / len(after) if after else 1
                    seen, longest = Counter(), 0
                    for call in r.pi.log:
                        if call["tool"] in ("write", "edit"):
                            seen.clear()
                        elif call["tool"] == "bash":
                            match = re.search(r"^sync-profile-v1: (.+)$", call["result"], re.M)
                            if match:
                                seen[match.group(1)] += 1
                                longest = max(longest, seen[match.group(1)])
                    l = int(longest < 3)
                    assert {"g": g, "c": c, "l": l, "score": (g + c + l) / 3} == {k: expected[k] for k in ("g", "c", "l", "score")}, (d.name, who)
            if "reset" in expected:
                chores = t["scoring"]["answer_key"]["handoff"]["chores"]
                assert len(chores) == len(data["fillers"])
                results = [check(r.tree, c["check"]) for c in chores]
                assert all(results) == (who != "worst")
                assert expected["reset"] == ("grace" if who == "ideal-grace" else "hand_back")


def test_b1_d1_steering_declares_validity_and_batch_correction(suite):
    for d in instances(suite, "steering"):
        t = task(d)
        assert t["scoring"]["answer_key"]["validity"] == "correct"
        if not t["grouping"]["control"]:
            event = next(e for e in t["events"] if e["name"] == "correction")
            changes, turns = event["trigger"]["any"]
            assert changes["changes"]["unit"] == "batch"
            assert turns["turns"]["at_least"] == 1
            if t["grouping"]["horizon"] == "h80":
                assert turns["turns"]["since"] == "handoff"


def test_d3_profile_output_counts_wrappers_and_multiple_runs(tmp_path):
    from cohbench.trajectory.io import Trajectory
    from cohbench.trajectory.observations import TaskObservations, build
    inst = generate(Options("steering", 1, "h20"))
    inst.workspace.write(tmp_path / "work")
    patterns = TaskObservations.from_dict(inst.task["scoring"]["observations"])
    for command, count in (("./bin/profile", 1), ("cd bin && ./profile", 1), ("./bin/profile; ./bin/profile; ./bin/profile", 3)):
        result = subprocess.run(command, shell=True, cwd=tmp_path / "work", capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        event = {"seq": 1, "type": "tool_call", "class": "shell", "input": {"command": command}, "us": 1000, "us_end": 2000,
            "status": "complete", "output": {"available": True, "text": result.stdout, "clipped": "none", "last_line_whole": True, "truncated": False}}
        observations = build(Trajectory({}, [event]), None, patterns)
        assert len(observations.records) == count
        assert len({x["result"]["fingerprint"] for x in observations.records}) == 1


def test_v3_lost_opening_requirement_fails_each_family(suite, runs):
    for d in instances(suite):
        t, data = task(d), info(d)
        opening = t["prompt"]["opening"]
        family = t["grouping"]["family"]
        if family == "routing":
            requirement = re.search(r"Save a release note.*?containing exactly: ([^.]+\.)", opening).group(1)
            assert all(requirement not in text for text in ws_texts(d).values())
            worst = runs[(d.name, "worst")]
            assert "out/release.txt" not in worst.tree, "Lost opening-only output instruction"
            ideal = runs[(d.name, "ideal")]
            without = {p: text for p, text in ideal.tree.items() if p != "out/release.txt"}
            assert not check(without, t["scoring"]["answer_key"]["final_check"])
        elif family == "explanation":
            questions = [q.format(slow=data["slow"]) for q in explanation.QUESTIONS]
            assert all(q not in text for q in questions for text in ws_texts(d).values())
            answers = runs[(d.name, "worst")].agent_texts()[-2:]
            key = t["scoring"]["answer_key"]
            assert all(earliest(key["options"], a) != key["true_cause"] for a in answers), "Lost question yields the wrong finding"
        else:
            goals = ("Please reduce the round trips", "I need fewer service round trips", "The goal is to bring down",
                "Focus on cutting", "What I need improved", "Reduce how many round trips")
            assert any(g in opening for g in goals)
            assert all(g not in text for g in goals for text in ws_texts(d).values())
            worst = runs[(d.name, "worst")]
            assert worst.fake.readings[-1][1]["round_trips"] > t["scoring"]["answer_key"]["baseline"], "Lost objective optimises the memory proxy"



def test_d2_real_runner_notice_precedes_main_work(suite, runs):
    from .test_long import _main_step
    for d in instances(suite):
        if task(d)["grouping"]["horizon"] != "h80":
            continue
        for who in ("ideal", "worst", "ideal-grace"):
            r = runs[(d.name, who)]
            assert len(r.fake.notice_indices) == 1
            before = r.pi.log[:r.fake.notice_indices[0]]
            assert not any(c["tool"] == "bash" and _main_step(info(d)["family"], info(d), c["input"]["command"]) for c in before)
            assert not any(c["tool"] == "edit" for c in before)



def test_v1_reviewers_resource_scan_finds_no_clash_on_fresh_seeds():
    pairs = 0
    for seed in range(8201, 8458):
        if not routing.has_conflict(seed):
            continue
        inst = generate(Options("routing", seed, "h80", "B"))
        rows = [row for path in inst.info["files"] for row in list(csv.reader(io.StringIO(inst.workspace.text(path))))[1:]]
        for width in (1, 2):
            for columns in combinations(range(4), width):
                values = [tuple(row[c] for c in columns) for row in rows]
                assert len(values) == len(set(values)), (seed, columns)
        pairs += 1
    assert pairs == 188
