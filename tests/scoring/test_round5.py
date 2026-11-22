"""JSON workspace checks, calls spanning writes, and conflict/reset ordering."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from cohbench.scoring import checks, report, steering
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode
from . import synth as S
from .build import Bundle, ROUTING_KEY
from .test_round4 import validated


def document_episode(path, text, **kw):
    b = Bundle("routing", baseline={"data.json": text}, **kw)
    b.user("Check the register.", kind="opening_prompt")
    b.out("Done.")
    return load(b.write(path), validate=True)


def check_document(ep, assertions, path="data.json"):
    return checks.run(ep, ep.final_batch(), {"path": path, "json": assertions})["held"]


@pytest.mark.parametrize("pointer, value, expected", [
    ("", {"name": "Ada", "jobs": [1, 2], "nested": {"ok": True}, "empty": None}, True),
    ("/name", "Ada", True), ("/jobs/0", 1, True), ("/nested/ok", True, True),
    ("/empty", None, True), ("/missing", None, False), ("/jobs/2", None, False),
    ("/jobs/01", 2, False), ("/jobs/-", None, False), ("/jobs/+1", 2, False),
    ("/jobs/١", 2, False), ("/jobs/1/x", None, False), ("/name", "Grace", False),
])
def test_json_equals_resolves_rfc6901_pointers(tmp_path, pointer, value, expected):
    ep = document_episode(tmp_path, '{"name":"Ada","jobs":[1,2],"nested":{"ok":true},"empty":null}')
    assert check_document(ep, [{"pointer": pointer, "equals": value}]) is expected


@pytest.mark.parametrize("pointer, value", [("/a~1b/~0key", "value"), ("/~01", 2), ("/", 3), ("/01", 4), ("/é", 5)])
def test_json_pointer_escapes_and_empty_keys(tmp_path, pointer, value):
    ep = document_episode(tmp_path, '{"a/b":{"~key":"value"},"~1":2,"":3,"01":4,"é":5}')
    assert check_document(ep, [{"pointer": pointer, "equals": value}]) is True


@pytest.mark.parametrize("text, value, expected", [
    ("1", 1.0, True), ("1.0", 1, True), ("1e0", 1, True), ("-0.0", 0, True),
    ("true", 1, False), ("1", True, False), ("false", 0, False),
    ('{"a":[true]}', {"a": [1]}, False), ("1.0000000000000000001", 1, False),
])
def test_json_numbers_do_not_coerce_booleans_or_round_decimals(tmp_path, text, value, expected):
    ep = document_episode(tmp_path, text)
    assert check_document(ep, [{"pointer": "", "equals": value}]) is expected


@pytest.mark.parametrize("value, expected", [
    ([2, 1, 1], True), ([1.0, 2, 1], True), ([1, 2], False), ([1, 2, 2], False),
    ([True, 2, 1], False),
])
def test_json_set_equals_is_a_multiset(tmp_path, value, expected):
    ep = document_episode(tmp_path, '{"values":[1,1,2]}')
    assert check_document(ep, [{"pointer": "/values", "set_equals": value}]) is expected


def test_json_set_equals_supports_nested_values_and_requires_a_list(tmp_path):
    ep = document_episode(tmp_path, '{"values":[{"b":2,"a":1},[1,2],null],"scalar":1}')
    assert check_document(ep, [{"pointer": "/values", "set_equals": [None, [1, 2], {"a": 1, "b": 2}]}]) is True
    assert check_document(ep, [{"pointer": "/values", "set_equals": [None, [2, 1], {"a": 1, "b": 2}]}]) is False
    assert check_document(ep, [{"pointer": "/scalar", "set_equals": [1]}]) is False


@pytest.mark.parametrize("pointer, expected", [("/missing", True), ("/nested/missing", True), ("/a/1", True), ("/a/0", False), ("/null", False), ("", False)])
def test_json_absent_distinguishes_missing_from_null(tmp_path, pointer, expected):
    ep = document_episode(tmp_path, '{"a":[0],"null":null}')
    assert check_document(ep, [{"pointer": pointer, "absent": True}]) is expected


@pytest.mark.parametrize("text", ['{"x":1} junk', '{"x":1}\n{}', '{"x":', '', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
def test_json_invalid_document_or_trailing_junk_fails(tmp_path, text):
    ep = document_episode(tmp_path, text)
    assert check_document(ep, [{"pointer": "/x", "equals": 1}]) is False
    assert check_document(ep, [{"pointer": "/unused", "absent": True}]) is False


def test_json_requires_one_existing_document_and_all_assertions(tmp_path):
    ep = document_episode(tmp_path, ' \n {"x":1,"y":2}\r\n ')
    assertions = [{"pointer": "/x", "equals": 1}, {"pointer": "/y", "equals": 2}]
    assert check_document(ep, assertions) is True
    assert check_document(ep, [*assertions, {"pointer": "/x", "equals": 0}]) is False
    assert check_document(ep, [{"pointer": "/x", "absent": True}], path="missing.json") is False


@pytest.mark.parametrize("bad", [None, [], {}, [1], [{"equals": 1}], [{"pointer": None, "equals": 1}],
    [{"pointer": "x", "equals": 1}], [{"pointer": "/~2", "equals": 1}], [{"pointer": "/~", "equals": 1}],
    [{"pointer": "", "absent": False}], [{"pointer": "", "absent": 1}], [{"pointer": "", "set_equals": 1}],
    [{"pointer": "", "equals": 1, "absent": True}], [{"pointer": "", "unknown": True}],
    [{"pointer": "", "equals": float("nan")}], [{"pointer": "", "equals": (1,)}],
    [{"pointer": "", "equals": {1: "x"}}],
])
def test_malformed_json_assertion_is_refused_by_check_name(bad):
    with pytest.raises(checks.CheckError, match="json check 'data.json'"):
        checks.parse({"path": "data.json", "json": bad})


@pytest.mark.parametrize("extra", [{"regex": "x"}, {"absent": True}, {"negate": False}, {"ignore_case": False}, {"unknown": 1}, {"path": None}])
def test_json_check_refuses_other_check_fields(extra):
    with pytest.raises(checks.CheckError, match="json check"):
        checks.parse({"path": "data.json", "json": [{"pointer": "", "equals": 1}], **extra})


@pytest.mark.parametrize("unknown, oversize", [(True, False), (False, True)])
def test_json_unknown_content_stays_undecided(tmp_path, unknown, oversize):
    b = Bundle("routing")
    b.batch(2000, 2000, {"data.json": '{"x":1}'}, unknown=("data.json",) if unknown else (), oversize=("data.json",) if oversize else ())
    ep = validated(b, tmp_path)
    assert check_document(ep, [{"pointer": "/x", "equals": 1}]) is None


def test_json_register_corruption_fails_routing(tmp_path):
    key = {**ROUTING_KEY, "settled_by_workspace": True, "final_check": {"path": "data.json", "json": [
        {"pointer": "/roster", "set_equals": ["a", "b"]}, {"pointer": "/clash", "equals": "settled"},
        {"pointer": "/waiting", "equals": ["b"]}, {"pointer": "/assigned", "equals": ["a"]},
    ]}}
    for name, contents, expected in [
        ("valid", '{"roster":["b","a"],"clash":"settled","waiting":["b"],"assigned":["a"]}', True),
        ("unresolved", '{"roster":["a","b"],"clash":"open","waiting":["b"],"assigned":["a"]}', False),
        ("junk", '{"roster":["a","b"],"clash":"settled","waiting":["b"],"assigned":["a"]} junk', False),
    ]:
        b = Bundle("routing", key, baseline={"data.json": contents}, pair="p1", sibling="A")
        b.user("Complete the register.", kind="opening_prompt")
        b.fired()
        b.out("Done.")
        assert score_episode(validated(b, tmp_path / name))["score"]["passed"] is expected


def call_bundle(start=1000, end=3000, write=2000, transient=False):
    b = Bundle("steering", baseline={"code.py": "v0"}, baseline_metric={"ops": 100})
    b.user("Improve progress.", kind="opening_prompt")
    b.batch(write, write, {"code.py": "v1"}, {"ops": 100})
    if transient:
        b.batch(write + 100, write + 100, {"code.py": "v0"}, {"ops": 100})
    for _ in range(3):
        b.attempt("profile", None, "a" * 64 if end is not None else None, status="complete" if end is not None else "unfinished", t=start)
        b.attempts[-1]["t_end"] = end
    return b


def test_profile_edit_profile_twice_in_one_call_leaves_all_attempts_unplaced(tmp_path):
    ep = validated(call_bundle(), tmp_path)
    result = steering.score(ep)
    assert steering.attempt_states(ep, steering.groups(ep)) == {1: None, 2: None, 3: None}
    assert result["l"]["unplaced_attempts"] == 3 and result["l"]["loop_attempts"] == [] and result["l"]["value"] == 1
    assert result["l"]["unplaced"] == [{"seq": i, "reason": "spans_change"} for i in (1, 2, 3)]


@pytest.mark.parametrize("start, end, write, expected", [(1000, 2000, 1000, 0), (1000, 2000, 2000, 3), (1000, 2000, 2001, 0), (1000, None, 1500, 0)])
def test_attempt_spanning_write_boundaries_and_unfinished_calls(tmp_path, start, end, write, expected):
    ep = validated(call_bundle(start, end, write), tmp_path)
    result = steering.score(ep)
    assert result["l"]["unplaced_attempts"] == expected
    assert all((state is None) == bool(expected) for state in steering.attempt_states(ep, steering.groups(ep)).values())


def test_dropped_transient_during_call_remains_invisible(tmp_path):
    ep = validated(call_bundle(transient=True), tmp_path)
    result = steering.score(ep)
    assert result["transient_batches"] == [1, 2] and result["l"]["unplaced_attempts"] == 0
    assert all(state is not None for state in steering.attempt_states(ep, steering.groups(ep)).values())


def test_unplaced_reasons_distinguish_start_inside_group_from_spanning_change(tmp_path):
    b = call_bundle(start=2100, end=2400)
    b.batch(2300, 2300, {"code.py": "v2"}, {"ops": 90})
    result = steering.score(validated(b, tmp_path))
    assert result["l"]["unplaced_attempts"] == 3
    assert {r["reason"] for r in result["l"]["unplaced"]} == {"starts_inside_group"}


def ordered_routing(path, order, *, explicit=False, episode="a-one", event="conflict"):
    b = Bundle("routing", {**ROUTING_KEY, "settled_by_workspace": True, "conflict_event": event},
               horizon="h80", pair="p1", sibling="A", episode=episode)
    b.user("Run the register.", kind="opening_prompt")
    b.planted = [{"name": "handoff", "scheduled": {"action": "reset", "required": True}},
                 {"name": event, "scheduled": {"required": True}}]
    b.user("Leave notes.", kind="reset_notice")
    if order in ("before", "no_reset"):
        b.fired(event)
    if order != "no_reset":
        b.fired("handoff")
        seq = b.user("Continue.", kind="reset")
        b._ev(type="controlled_reset", target=seq, t=b.events[seq]["t"])
    if order == "after":
        b.fired(event)
    if order in ("no_reset", "no_conflict"):
        b.reach = {"status": "not_reached", "not_reached": ["handoff" if order == "no_reset" else event]}
    b.batch(b.t, b.t, {"out/summary.txt": "kept: big"})
    b.out("Done.")
    ep = validated(b, path)
    if explicit:
        doc = json.loads((path / "episode.json").read_text())
        doc["resets"] = [{"event": "handoff", "reached": order != "no_reset", "fired": order != "no_reset", "performed": order != "no_reset",
                          "reason": "finished_before_reset" if order == "no_reset" else None}]
        (path / "episode.json").write_text(json.dumps(doc))
        ep = load(path, validate=True)
    return ep


@pytest.mark.parametrize("order", ["before", "after", "no_reset", "no_conflict"])
@pytest.mark.parametrize("explicit", [False, True])
def test_long_routing_records_conflict_order_against_performed_reset(tmp_path, order, explicit):
    score = score_episode(ordered_routing(tmp_path, order, explicit=explicit, event="clash"))
    assert score["conflict_vs_reset"] == (order if order in ("before", "after") else None)
    if order == "no_reset":
        assert score["unjudged_reason"] == "no reset"
    elif order == "no_conflict":
        assert score["score"]["passed"] is False
    else:
        assert score["score"]["passed"] is True


def test_conflict_reset_split_is_only_a_per_cell_diagnostic(tmp_path):
    scores = [score_episode(ordered_routing(tmp_path / order, order, episode=f"{prefix}-one"))
              for prefix, order in zip(("a", "b", "c", "d"), ("before", "after", "no_reset", "no_conflict"))]
    for i, score in enumerate(scores):
        score["task"]["instance"]["seed"] = score["grouping"]["seed"] = i
        score["task"]["sha256"] = str(i) * 64
    configs = {p: {"model": "m", "harness": "h"} for p in ("a", "b", "c", "d")}
    root = S.write(tmp_path / "results", scores, configs)
    before = report.build(root, resamples=0)
    row = before["sections"][0]["tables"][0]["rows"][0]
    assert row["conflict_vs_reset"] == {"before": 1, "after": 1, "not_measured": 2,
        "episodes": {"before": ["a-one"], "after": ["b-one"], "not_measured": ["c-one", "d-one"]}}
    assert "conflict before reset 1, after reset 1, not measured 2" in report.render(before, False)
    for score in scores:
        score.pop("conflict_vs_reset")
    after = report.build(S.write(tmp_path / "old", scores, configs), resamples=0)
    old_row = after["sections"][0]["tables"][0]["rows"][0]
    assert old_row["conflict_vs_reset"]["not_measured"] == 4
    assert all(row[k] == old_row[k] for k in ("R", "E", "S", "headline", "eligibility", "capability"))


def test_json_and_diagnostics_match_across_runtimes_and_hash_seeds(tmp_path):
    code = '''
import json, sys
from pathlib import Path
from cohbench.scoring import checks, steering, report
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps
from tests.scoring import synth as S
root = Path(sys.argv[1])
a = load(root / 'document')
b = load(root / 'attempts')
c = score_episode(load(root / 'ordering'))
c.pop('python_version')
rep = report.build(root / 'results', resamples=10)
rep['python_versions'] = ['runtime']
print(dumps({'checks': [checks.run(a, a.final_batch(), check) for check in json.loads((root / 'checks.json').read_text())],
             'attempts': steering.score(b), 'ordering': c, 'report': rep, 'text': report.render(rep, False)}))
'''
    document_episode(tmp_path / "document", '{"values":[1,true,1.0],"a/b":{"~x":1.0000000000000000001}}')
    validated(call_bundle(), tmp_path / "attempts")
    score = score_episode(ordered_routing(tmp_path / "ordering", "before"))
    S.write(tmp_path / "results", [score], {"a": {"model": "m", "harness": "h"}})
    (tmp_path / "checks.json").write_text(json.dumps([
        {"path": "data.json", "json": [{"pointer": "/values", "set_equals": [True, 1, 1]}]},
        {"path": "data.json", "json": [{"pointer": "/a~1b/~0x", "equals": 1}]},
    ]))
    outputs = []
    for version in ("3.12", "3.15"):
        executable = subprocess.check_output(["uv", "python", "find", "--offline", version], text=True).strip()
        for seed in ("1", "777"):
            outputs.append(subprocess.check_output([executable, "-c", code, str(tmp_path)], cwd=Path(__file__).resolve().parents[2],
                           env={**os.environ, "PYTHONHASHSEED": seed}))
    assert len(set(outputs)) == 1


def test_spanning_calls_cannot_supply_prior_equivalent_results(tmp_path):
    b = call_bundle()
    b.batch(4000, 4000, {"code.py": "v0"}, {"ops": 100})
    for t in (5000, 5100):
        b.attempt("profile", None, "a" * 64, t=t)
    result = steering.score(validated(b, tmp_path))
    assert result["l"]["unplaced_attempts"] == 3
    assert result["l"]["value"] == 1 and result["l"]["loop_attempts"] == []
