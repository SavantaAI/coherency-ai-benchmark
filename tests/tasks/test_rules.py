"""SPEC.md section 4, "Design rules for every task", one test (or more) per
rule, over the whole development suite and a wider range of seeds."""

from __future__ import annotations

import re
from pathlib import Path

from cohbench.runner.triggers import prerequisites
from cohbench.tasks import Options, generate
from cohbench.tasks import fillers as F
from cohbench.tasks.common import COACHED
from cohbench.tasks.phrases import PATTERNS, found
from cohbench.tasks.rng import Rng
from cohbench.tasks.routing import has_conflict
from cohbench.tasks.steering import has_easy_wins

from .conftest import agent_texts, info, instances, task, ws_texts

README = Path(__file__).parents[2] / "cohbench" / "tasks" / "README.md"


# Rule 1: easy atomic steps -------------------------------------------------


def test_rule1_every_family_documents_its_steps_and_every_filler_is_one_plain_chore():
    text = README.read_text()
    for fam in ("Routing", "Explanation", "Steering"):
        assert re.search(rf"^### {fam}\b.*?^#### Steps$", text, re.M | re.S), fam
    for k in F.KINDS:
        f = k(Rng("rule1", k.__name__))
        assert f.instruction.count(". ") == 0 and f.instruction.endswith("."), f.key  # one sentence
        assert set(f.look) <= set(f.planted)
        assert f.solution and all(c.endswith("\n") for c in f.solution.values()), f.key


# Rule 2: unprompted rules --------------------------------------------------


def test_rule2_the_pattern_list_catches_the_spec_examples_and_the_coached_sentence():
    assert found("Ask me only when you need a decision.")
    assert found("Keep your answer under 50 words.")
    assert found("Stop once nothing improves.")
    assert {"ask only", "lead with", "check progress"} <= set(found(COACHED))
    assert len(PATTERNS) >= 15


def test_rule2_no_prompt_message_reopen_or_workspace_file_states_the_tested_behaviour(suite):
    hits = []
    for d in instances(suite):
        t = task(d)
        texts = {**agent_texts(t), **{f"workspace/{k}": v for k, v in ws_texts(d).items()}}
        for where, text in texts.items():
            if found(text):
                hits.append((d.name, where, found(text)))
    assert not hits, hits[:10]


# Rule 3: late decisions ----------------------------------------------------


def test_rule3_the_user_only_decision_unlocks_at_the_conflict_after_private_ids_exist(suite):
    for d in instances(suite, "routing"):
        t, i = task(d), info(d)
        decisions = t["user"]["card"]["decisions"]
        if not i["conflict"]:
            assert decisions == []
            continue
        assert [x["available_after"] for x in decisions] == ["conflict"]
        assert i["step"] >= 2  # ids from the first file's run exist before the clash
        ev = next(e for e in t["events"] if e["name"] == "conflict")
        trig = ev["trigger"]
        clash = trig if "content_matches" in trig else next(x for x in trig["all"] if "content_matches" in x)
        assert clash["content_matches"]["regex"] == "^CLASH"
        if t["grouping"]["horizon"] == "h80":
            assert {"fired": "handoff"} in trig["all"]


def test_rule3_in_steering_and_explanation_nothing_is_decided_by_the_user(suite):
    for d in instances(suite, "steering") + instances(suite, "explanation"):
        assert task(d)["user"]["card"]["decisions"] == []


# Rule 4: unpredictable traps -----------------------------------------------


def test_rule4_trap_timing_varies_by_seed_and_some_seeds_have_no_trap():
    seeds = range(1, 201)
    none = [s for s in seeds if not has_conflict(s)]
    assert 0.18 <= len(none) / len(seeds) <= 0.32
    steps = {generate(Options("routing", s, "h20", "A")).info["step"] for s in seeds[:60] if has_conflict(s)}
    assert len(steps) >= 4
    ks = {generate(Options("steering", s, "h20")).info["k"] for s in seeds[:30]}
    assert ks == {2, 3, 4}
    easy = [has_easy_wins(s) for s in seeds]
    assert 0.35 <= sum(easy) / len(easy) <= 0.65
    slow = {(x.info["domain"], x.info["slow"], x.info["cause"]) for x in (generate(Options("explanation", s, "h20")) for s in seeds[:30])}
    assert len({c for _d, _s, c in slow}) >= 6 and len({s for _d, s, _c in slow}) >= 10


def test_rule4_the_clash_step_is_not_always_the_last_file():
    rel = [(generate(Options("routing", s, "h20", "B")).info["step"], len(generate(Options("routing", s, "h20", "B")).info["files"]))
           for s in range(1, 80) if has_conflict(s)]
    assert any(st < n for st, n in rel) and any(st == n for st, n in rel)


# Rule 5: guaranteed reachability -------------------------------------------


def test_rule5_every_required_event_has_a_plain_reopen_and_only_required_prerequisites(suite):
    for d in instances(suite):
        t = task(d)
        required = {e["name"] for e in t["events"] if e["required"]}
        for e in t["events"]:
            if not e["required"]:
                continue
            assert e["reopen"] and not found(e["reopen"]), (d.name, e["name"])
            assert prerequisites(e["trigger"]) <= required
            assert t["reachability"]["max_reopens"] >= 1


def test_rule5_each_family_trigger_is_reachable_from_its_reopen(suite):
    for d in instances(suite):
        t = task(d)
        fam = t["grouping"]["family"]
        for e in t["events"]:
            if e["name"] == "correction":
                # Any finished turn fires it, and a reopen always ends in a finished turn.
                assert any("turns" in x for x in e["trigger"]["any"])
            if e["name"] == "follow_up":
                since = {"since": "handoff"} if t["grouping"]["horizon"] == "h80" else {}
                assert e["trigger"] == {"handbacks": {"at_least": 1, **since}}
            if e["name"] == "conflict":
                # The reopen asks for the remaining files, and adding every file prints the clash.
                assert f"every file in {info(d)['files'][0].split('/')[0]}/" in e["reopen"]
            if e["name"] == "handoff":
                # The chores' results are counted; the reopen asks for the list from the first message, whose chores produce them.
                assert "count" in e["trigger"]["any"][0]
                assert "my first message" in e["reopen"]
        if fam == "explanation":
            assert [e["name"] for e in t["events"]] == (["handoff", "follow_up"] if t["grouping"]["horizon"] == "h80" else ["follow_up"])
