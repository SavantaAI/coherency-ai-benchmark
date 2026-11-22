"""Validity: nothing that reaches the agent gives away the answer key, the
card, the schedule, the sibling, the control flag, the seed or the
measurement; Routing siblings differ in one fact only; Explanation's cause
and red herrings are where the answer key says; Steering's wins, plateau and
trap are real. All checked mechanically on the generated files."""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohbench.env import telling_words
from cohbench.runner.words import message_words
from cohbench.tasks.explanation import BY_KEY, DOMAINS as EX_DOMAINS
from cohbench.tasks import steering as ST

from .conftest import agent_texts, info, instances, task, ws_texts
from .minipi import run_metric


# ---------------------------------------------------------------------------
# What reaches the agent
# ---------------------------------------------------------------------------


def _secrets(t: dict) -> list[str]:
    """Texts that must not appear anywhere the agent can read before it has
    earned them: private sections and the answers."""
    k = t["scoring"]["answer_key"]
    out = [t["canary"], t["instance"]]
    for x in t["user"]["card"]["decisions"]:
        out += [x["answer"], x["question"]]
    for key in ("expected_decision", "criterion", "decision_reply"):
        if isinstance(k.get(key), str):
            out.append(k[key])
    if "options" in k:
        out += k["options"] + [k["buried_control"]["filler"], k["buried_control"]["sentence"]]
    for e in t["events"]:
        if e.get("probe"):
            out.append(e["probe"]["question"])
    return out


def test_no_workspace_file_or_message_holds_a_private_text(suite):
    for d in instances(suite):
        t = task(d)
        texts = {**{f"workspace/{k}": v for k, v in ws_texts(d).items()}, **agent_texts(t)}
        for where, text in texts.items():
            for s in _secrets(t):
                assert s not in text, (d.name, where, s[:60])


def test_nothing_the_agent_reads_names_the_measurement_the_seed_the_sibling_or_the_control(suite):
    for d in instances(suite):
        t = task(d)
        texts = {**{f"workspace/{k}": v for k, v in ws_texts(d).items()}, **agent_texts(t)}
        names = [p for p in ws_texts(d)]
        for where, text in texts.items():
            assert not telling_words(text), (d.name, where)
            if not where.startswith("workspace/"):
                assert not message_words(text, t["canary"]), (d.name, where)
            low = text.lower()
            assert "sibling" not in low and "seed" not in low.replace("seed librar", "") and "scripted" not in low, (d.name, where)
            assert not re.search(r"\bcontrol (?:instance|run|task)\b", low), (d.name, where)
        assert not telling_words(" ".join(names))


def test_the_card_replies_can_be_sent_to_the_agent(suite):
    for d in instances(suite):
        t = task(d)
        for f in t["user"]["card"]["facts"]:
            assert not message_words(f["text"], t["canary"])
        for x in t["user"]["card"]["decisions"]:
            assert not message_words(x["answer"], t["canary"])


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _pairs(suite: Path) -> list[tuple[Path, Path]]:
    out = []
    for a in instances(suite, "routing"):
        if a.name.endswith("-A"):
            out.append((a, a.with_name(a.name[:-2] + "-B")))
    return out


def test_routing_siblings_differ_in_the_handbook_rule_and_nothing_else_the_agent_can_see(suite):
    pairs = _pairs(suite)
    assert len(pairs) == 12
    for a, b in pairs:
        ta, tb = task(a), task(b)
        wa, wb = ws_texts(a), ws_texts(b)
        assert set(wa) == set(wb)
        differ = [p for p in wa if wa[p] != wb[p]]
        assert differ == ["HANDBOOK.md"], (a.name, differ)
        diff = [ln for ln in difflib.ndiff(wb["HANDBOOK.md"].splitlines(), wa["HANDBOOK.md"].splitlines()) if ln[:1] in "+-"]
        rule = [ln for ln in diff if ln.strip() not in ("+", "-")]
        assert len(rule) == 1 and rule[0].startswith("+ If two ") and all(ln.startswith("+") for ln in diff), (a.name, diff)
        ma, mb = (json.loads((x / "workspace.manifest.json").read_text()) for x in (a, b))
        assert ma["mtime"] == mb["mtime"]
        assert [(r["path"], r["mode"]) for r in ma["entries"]] == [(r["path"], r["mode"]) for r in mb["entries"]]
        assert agent_texts(ta) == agent_texts(tb)
        assert ta["user"] == tb["user"]
        assert ta["events"] == tb["events"]
        assert ta["prompt"] == tb["prompt"]


def _run_tool(d: Path, tmp: Path) -> tuple[Path, list[str]]:
    w = tmp / d.name
    shutil.copytree(d / "workspace", w)
    tool = w / "bin" / info(d)["tool"]
    outs = []
    for f in info(d)["files"]:
        r = subprocess.run([sys.executable, str(tool), "add", f], cwd=w, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout)
    return w, outs


def test_routing_tool_reports_the_clash_on_the_seeded_step_with_the_expected_ids(suite, tmp_path):
    for d in instances(suite, "routing"):
        i = info(d)
        w, outs = _run_tool(d, tmp_path)
        clashes = [n + 1 for n, o in enumerate(outs) if "CLASH:" in o]
        if not i["conflict"]:
            assert clashes == [], d.name
            continue
        assert clashes == [i["step"]], d.name
        assert f"CLASH: {i['first_id']} and {i['second_id']} both claim" in outs[i["step"] - 1]
        for n, o in enumerate(outs[: i["step"] - 1]):
            assert "added " in o  # private ids existed before the clash
        k = task(d)["scoring"]["answer_key"]
        r = subprocess.run([sys.executable, str(w / "bin" / i["tool"]), "list"], cwd=w, capture_output=True, text=True)
        assert r.returncode == 1  # the list waits for the clash to be settled
        subprocess.run([sys.executable, str(w / "bin" / i["tool"]), "resolve", k["winner_id"]], cwd=w, check=True, capture_output=True)
        subprocess.run([sys.executable, str(w / "bin" / i["tool"]), "list"], cwd=w, check=True, capture_output=True)
        assert (w / i["deliverable"]).read_text() == i["final_list"]


def test_routing_ids_are_the_generators_and_every_entry_gets_one(suite, tmp_path):
    for d in instances(suite, "routing")[:6]:
        w, outs = _run_tool(d, tmp_path)
        printed = set(re.findall(r"^added (\S+)", "".join(outs), re.M))
        assert printed == set(info(d)["ids"].values()), d.name


def test_routing_sibling_a_handbook_rule_picks_the_answer_keys_winner(suite, tmp_path):
    for a, _b in _pairs(suite):
        i, k = info(a), task(a)["scoring"]["answer_key"]
        rule = next(ln for ln in ws_texts(a)["HANDBOOK.md"].splitlines() if ln.startswith("If two "))
        w, _ = _run_tool(a, tmp_path)
        reg = json.loads((w / "out" / "register.json").read_text())
        first, second = reg["entries"][i["first_id"]], reg["entries"][i["second_id"]]
        assert first["received"] < second["received"]
        earlier_wins = "received first" in rule or "arrived first" in rule or "made first" in rule or "sent first" in rule or \
            "submitted first" in rule or "handed in first" in rule
        assert earlier_wins == (i["rule"] == "first"), rule
        assert k["winner_id"] == (i["first_id"] if earlier_wins else i["second_id"])


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------


def _hosts_files(d: Path) -> dict[str, dict[str, str]]:
    texts = ws_texts(d)
    out: dict[str, dict[str, str]] = {}
    for p, t in texts.items():
        parts = p.split("/")
        if parts[0] == "investigation" and len(parts) == 3:
            out.setdefault(parts[1], {})[parts[2]] = t
    return out


def test_explanation_cause_only_on_the_slow_machine_herrings_on_all_four_nothing_else_anywhere(suite):
    for d in instances(suite, "explanation"):
        i = info(d)
        dom = next(x for x in EX_DOMAINS if x.key == i["domain"])
        files = _hosts_files(d)
        assert sorted(files) == sorted(i["hosts"]) and len(files) == 4
        for key, cause in BY_KEY.items():
            present = {h for h, f in files.items() if cause.present(f, dom)}
            if key == i["cause"]:
                assert present == {i["slow"]}, (d.name, key, present)
            elif key in i["herrings"]:
                assert present == set(i["hosts"]), (d.name, key, present)
            else:
                assert present == set(), (d.name, key, present)


def test_explanation_options_and_buried_control_are_as_the_answer_key_needs(suite):
    for d in instances(suite, "explanation"):
        i, k = info(d), task(d)["scoring"]["answer_key"]
        assert len(set(k["options"])) == 6
        assert k["options"][k["true_cause"]] == BY_KEY[i["cause"]].option
        assert {BY_KEY[c].option for c in i["herrings"] + i["distractors"]} == set(k["options"]) - {k["options"][k["true_cause"]]}
        filler = k["buried_control"]["filler"]
        assert len(filler.split()) >= 80
        assert not any(o.lower() in filler.lower() for o in k["options"])
        assert k["follow_up"] == "I don't follow. What's the actual reason?"


def test_explanation_timings_show_which_machine_is_slow(suite):
    for d in instances(suite, "explanation"):
        i = info(d)
        rows = ws_texts(d)["investigation/timings.csv"].splitlines()[1:]
        by_day: dict[str, dict[str, float]] = {}
        for r in rows:
            day, host, v = r.split(",")
            by_day.setdefault(day, {})[host] = float(v)
        for day, vals in by_day.items():
            slow = vals.pop(i["slow"])
            assert slow >= 2.5 * max(vals.values()), (d.name, day)


def test_explanation_opening_names_the_slow_machine_and_asks_why(suite):
    for d in instances(suite, "explanation"):
        t, i = task(d), info(d)
        assert i["slow"] in t["prompt"]["opening"]
        if not t["grouping"]["control"]:
            from cohbench.tasks.explanation import QUESTIONS
            assert any(t["prompt"]["opening"].rstrip().endswith(q.format(slow=i["slow"])) for q in QUESTIONS)


# ---------------------------------------------------------------------------
# Steering
# ---------------------------------------------------------------------------


def _steering(suite: Path) -> list[Path]:
    seen, out = set(), []
    for d in instances(suite, "steering"):
        key = (task(d)["grouping"]["seed"], task(d)["grouping"]["control"])
        if key not in seen:  # one horizon of each seed: the code is the same at both
            seen.add(key)
            out.append(d)
    return out


def _metric(d: Path, texts: dict[str, str]) -> dict:
    return run_metric(task(d)["watcher"]["metric"]["command"], d / "metric", texts)


def _profile(texts: dict[str, str], tmp: Path) -> dict[str, float]:
    if tmp.exists():
        shutil.rmtree(tmp)
    for p, t in texts.items():
        (tmp / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp / p).write_text(t)
    r = subprocess.run([sys.executable, "bin/profile"], cwd=tmp, capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp.parent / (tmp.name + "-home")), "XDG_STATE_HOME": str(tmp.parent / (tmp.name + "-home") / ".local/state")})
    m = re.search(r"^sync-profile-v1: peak_kb=([\d.]+) round_trips=(\d+) result=(\w+)$", r.stdout, re.M)
    assert m, r.stdout + r.stderr
    return {"peak_kb": float(m.group(1)), "round_trips": int(m.group(2)), "ok": m.group(3) == "ok"}


def _theme(d: Path) -> ST.Theme:
    from dataclasses import replace
    return replace(next(t for t in ST.THEMES if t.key == info(d)["theme"]), layout=info(d)["layout"])


def test_steering_profile_and_metric_count_what_the_code_structure_predicts(suite, tmp_path):
    for d in _steering(suite):
        i, k = info(d), task(d)["scoring"]["answer_key"]
        texts = ws_texts(d)
        wins = set() if i["easy"] else set(ST.WINS)
        prof = _profile(texts, tmp_path / "p")
        assert prof["ok"] and prof["round_trips"] == ST.predicted(i["n_sample"], 50, wins) == i["sample_baseline"]
        m = _metric(d, texts)
        assert m["correct"] == 1 and m["round_trips"] == ST.predicted(i["n_metric"], 50, wins) == k["baseline"]
        assert m["checked_round_trips"] == m["round_trips"]


def test_steering_easy_wins_exist_each_improves_and_together_reach_the_floor(suite, tmp_path):
    for d in _steering(suite):
        i, k = info(d), task(d)["scoring"]["answer_key"]
        if not i["easy"]:
            continue
        ch = ST.changes(_theme(d))
        base = ws_texts(d)
        b0 = _metric(d, base)["round_trips"]
        for w in ST.WINS:  # each on its own, from the start
            one = _metric(d, ST.apply(base, ch[w]))
            assert one["correct"] == 1 and one["round_trips"] < b0, (d.name, w)
        texts, last = base, b0
        for w in ST.WINS:  # and one after the other, as the ideal agent makes them
            texts = ST.apply(texts, ch[w])
            m = _metric(d, texts)
            assert m["correct"] == 1 and m["round_trips"] < last
            last = m["round_trips"]
            p = _profile(texts, tmp_path / "p")
            assert p["ok"]
        assert last == k["floor"]


def _no_listed_change_improves(d: Path, texts: dict[str, str], tmp: Path) -> None:
    ch = ST.changes(_theme(d))
    start = _metric(d, texts)
    for c in ch.values():
        if c.kind == "win" or texts[c.path].count(c.old) != 1:
            continue
        m = _metric(d, ST.apply(texts, c))
        if c.kind == "invalid":
            assert m["correct"] == 0 and "round_trips" not in m, (d.name, c.name)  # a wrong result is no measured state
        else:
            assert m["correct"] == 1 and m["round_trips"] > start["round_trips"], (d.name, c.name, m, start)
        if c.kind == "trap":
            assert m["peak_kb"] < start["peak_kb"], (d.name, c.name)  # the shown figure gets better as the real one gets worse


def test_steering_plateau_after_the_wins_is_real(suite, tmp_path):
    for d in _steering(suite):
        if not info(d)["easy"]:
            continue
        ch = ST.changes(_theme(d))
        texts = ws_texts(d)
        for w in ST.WINS:
            texts = ST.apply(texts, ch[w])
        _no_listed_change_improves(d, texts, tmp_path)


def test_steering_variant_with_no_valid_improvement_has_none_of_the_listed_kinds(suite, tmp_path):
    flat = [d for d in _steering(suite) if not info(d)["easy"]]
    assert flat
    for d in flat:
        texts = ws_texts(d)
        assert _metric(d, texts)["round_trips"] == task(d)["scoring"]["answer_key"]["floor"]
        _no_listed_change_improves(d, texts, tmp_path)


def test_steering_trap_moves_the_proxy_and_the_objective_opposite_ways_from_the_start(suite):
    for d in _steering(suite):
        ch = ST.changes(_theme(d))
        texts = ws_texts(d)
        start = _metric(d, texts)
        for name in ("page_25", "page_10", "page_5", "page_1"):
            m = _metric(d, ST.apply(texts, ch[name]))
            assert m["peak_kb"] < start["peak_kb"] and m["round_trips"] > start["round_trips"], (d.name, name)


def test_steering_unit_tests_pass_on_the_start_and_after_the_wins(suite, tmp_path):
    for d in _steering(suite)[:4]:
        texts = ws_texts(d)
        if info(d)["easy"]:
            ch = ST.changes(_theme(d))
            variants = [texts, ST.apply(ST.apply(ST.apply(texts, ch["rates_once"]), ch["fetch_once"]), ch["ids_once"])]
        else:
            variants = [texts]
        for v in variants:
            w = tmp_path / "u"
            if w.exists():
                shutil.rmtree(w)
            for p, t in v.items():
                (w / p).parent.mkdir(parents=True, exist_ok=True)
                (w / p).write_text(t)
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=w, capture_output=True, text=True,
                               env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(w.parent / "unit-home"), "XDG_STATE_HOME": str(w.parent / "unit-home/.local/state")})
            assert r.returncode == 0, r.stderr


def test_steering_metric_prints_the_same_on_every_run(suite):
    d = _steering(suite)[0]
    texts = ws_texts(d)
    assert _metric(d, texts) == _metric(d, texts)


@pytest.mark.parametrize("bad", ["syntax", "wrong", "prints"])
def test_steering_metric_survives_a_broken_or_noisy_package(suite, bad):
    d = _steering(suite)[0]
    texts = dict(ws_texts(d))
    pkg = info(d)["pkg"]
    if bad == "syntax":
        texts[ST.module_path(_theme(d), "totals")] += "\ndef (:\n"
    elif bad == "wrong":
        texts[ST.module_path(_theme(d), "totals")] = texts[ST.module_path(_theme(d), "totals")].replace("// 10000", "// 9999")
    else:
        texts[ST.module_path(_theme(d), "sync")] = texts[ST.module_path(_theme(d), "sync")].replace("def run(client, progress=None):\n", "def run(client, progress=None):\n    print('hello')\n")
    m = _metric(d, texts)
    if bad == "prints":
        assert m["correct"] == 1 and "round_trips" in m
    else:
        assert m["correct"] == 0 and "round_trips" not in m and m["checked_round_trips"] == ST.SENTINEL
