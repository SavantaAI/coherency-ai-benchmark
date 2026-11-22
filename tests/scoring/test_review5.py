"""Regressions for the review of scoring commit fd84489."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from cohbench.scoring import checks, report, routing, steering
from cohbench.scoring.__main__ import main
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode
from cohbench.trajectory.observations import tree_digest
from . import synth as S
from .build import Bundle, ROUTING_KEY
from .test_review4 import full_config
from .test_round2 import plan_file
from .test_round4 import full_scores, validated


@pytest.mark.parametrize("extra, reason", [
    ("1e9999999999999999999999", "number"),
    ("[" * 10000 + "0" + "]" * 10000, "nesting"),
    ('"' + "x" * (1024 * 1024) + '"', "1 MiB"),
    ("not-json", "parse"),
])
def test_agent_json_parse_failure_writes_a_failed_score(tmp_path, extra, reason):
    check = {"path": "register.json", "json": [{"pointer": "/ok", "equals": True}]}
    b = Bundle("routing", {**ROUTING_KEY, "conflict": False, "final_check": check},
               baseline={"register.json": '{"ok": true, "extra": ' + extra + "}"})
    b.user("Write the register.", kind="opening_prompt")
    root = b.write(tmp_path / "bundle")
    out = tmp_path / "score.json"
    assert main(["score", str(root), "--out", str(out)]) == 0
    result = json.loads(out.read_text())["score"]
    assert result["passed"] is False
    assert reason in result["final_check"]["results"][0]["why"]


@pytest.mark.parametrize("depth, expected", [(64, True), (65, False)])
def test_json_depth_boundary_and_brackets_inside_strings(tmp_path, depth, expected):
    text = "[" * depth + json.dumps('escaped " and \\ and ' + "[" * 100) + "]" * depth
    ep = Bundle("routing", baseline={"data.json": text}).load(tmp_path)
    result = checks.run(ep, ep.final_batch(), {"path": "data.json", "json": [{"pointer": "/missing", "absent": True}]})
    assert result["held"] is expected


@pytest.mark.parametrize("kind", ["regex", "negate", "absent"])
def test_deep_agent_path_does_not_abort_any_check_kind(tmp_path, kind):
    path = "/".join(["nested"] * 1500) + "/result.txt"
    check = {"path": path, "absent": True} if kind == "absent" else {"path": path, "regex": "ok", **({"negate": True} if kind == "negate" else {})}
    b = Bundle("routing", {**ROUTING_KEY, "conflict": False, "final_check": check}, baseline={path: "ok"})
    ep = b.load(tmp_path)
    result = score_episode(ep)["score"]
    assert result["passed"] is (kind == "regex")


@pytest.mark.parametrize("supplied_plan", [False, True])
def test_report_without_required_plan_has_values_but_no_release_bands(tmp_path, supplied_plan):
    scores = full_scores() + full_scores("ac", mode="coached")
    for score in scores:
        score["python_version"] = "3.12"
    root = S.write(tmp_path / "results", scores, {"a": full_config(), "ac": full_config()})
    plan = plan_file(tmp_path, scores)
    ordinary = report.build(root, resamples=10, plan=plan if supplied_plan else None)
    release = report.build(root, resamples=10, plan=plan, require_plan=True)
    assert ordinary["not_release"] and not release["not_release"]
    assert "plan was not required" in report.render(ordinary, False)
    for unplanned, planned in zip(ordinary["sections"][0]["tables"], release["sections"][0]["tables"]):
        a, b = unplanned["rows"][0], planned["rows"][0]
        assert a["headline"]["band"] is None and b["headline"]["band"] is not None
        assert a["headline"]["ranked"] == b["headline"]["ranked"]
        for measure in ("R", "E", "S", "headline"):
            assert a[measure]["value"] == b[measure]["value"]
            assert a[measure]["interval"] == b[measure]["interval"]
    assert ordinary["sections"][0]["coached_gap"] == release["sections"][0]["coached_gap"]


def test_release_runtime_has_no_cli_or_build_override(tmp_path):
    scores = full_scores()
    for score in scores:
        score["python_version"] = "3.15"
    root = S.write(tmp_path / "results", scores, {"a": full_config()})
    plan = plan_file(tmp_path, scores)
    with pytest.raises(SystemExit) as exc:
        main(["report", str(root), "--out", str(tmp_path / "out"), "--plan", str(plan),
              "--require-plan", "--release-python", "3.15", "--resamples", "10"])
    assert exc.value.code == 2 and not (tmp_path / "out").exists()
    with pytest.raises(TypeError, match="release_python"):
        report.build(root, plan=plan, require_plan=True, release_python="3.15")
    assert report.build(root, plan=plan, require_plan=True, resamples=10)["not_release"]


def decision_episode(tmp_path, answer_ids, reply, *, decision_id=True, final=True, bad_request=False):
    key = {**ROUTING_KEY, **({"decision_id": "keeps"} if decision_id else {})}
    b = Bundle("routing", key, pair="p1", sibling="B", baseline={"out/summary.txt": "kept: big" if final else "kept: small"})
    b.user("Resolve the conflict.", kind="opening_prompt")
    b.fired()
    seq = b.out("Which one keeps it?", label="answerable", reply=reply)
    if answer_ids is not None:
        next(e for e in b.events if e["type"] == "verdict")["answer_ids"] = answer_ids
    if bad_request:
        b.out("And private-job-123?", label="unanswerable", reply="I cannot see that.")
    return load(b.write(tmp_path), validate=True), seq


@pytest.mark.parametrize("ids, reply, expected", [
    (["keeps"], None, True), (["keeps"], "The deadline is Friday.", True),
    (["deadline"], ROUTING_KEY["decision_reply"], False), ([], ROUTING_KEY["decision_reply"], False),
])
def test_classifier_selected_ids_decide_routing_even_without_reply(tmp_path, ids, reply, expected):
    ep, seq = decision_episode(tmp_path, ids, reply)
    result = routing.score(ep)
    assert result["passed"] is expected
    assert routing.output_right(ep, seq) is expected
    request = result["requests_after_conflict"][0]
    assert request["answer_ids"] == ids and request["reply_delivered"] is (reply is not None)


@pytest.mark.parametrize("ids, decision_id, field", [(None, True, "answer_ids"), (["keeps"], False, "decision_id"), (None, False, "answer_ids")])
def test_undelivered_reply_without_id_evidence_is_unjudged(tmp_path, ids, decision_id, field):
    ep, seq = decision_episode(tmp_path, ids, None, decision_id=decision_id)
    result = routing.score(ep)
    assert result["passed"] is None and field in " ".join(result["reasons"])
    assert routing.output_right(ep, seq) is None


@pytest.mark.parametrize("final, bad_request", [(False, False), (True, True)])
def test_missing_selection_cannot_hide_known_routing_failure(tmp_path, final, bad_request):
    ep, _ = decision_episode(tmp_path, None, None, final=final, bad_request=bad_request)
    assert routing.score(ep)["passed"] is False


def log_episode(tmp_path, *, status="collected", records=None, shell=True, shell_mode="one", collection=True, declaration=True, change=False):
    b = Bundle("steering", baseline={"code.py": "v0"}, baseline_metric={"ops": 100})
    b.user("Measure progress.", kind="opening_prompt")
    records = records if records is not None else [(1000, 1010, "measure", "a" * 64, "same"),
                                                  (1100, 1110, "measure", "a" * 64, "same"),
                                                  (1200, 1210, "measure", "a" * 64, "same")]
    if shell:
        for i in range(3):
            start = 900 if shell_mode == "one" else 900 + i * 100
            fp = hashlib.sha256(str(i).encode()).hexdigest() if shell_mode == "labels" else "b" * 64
            b.attempt("measure", None, fp, t=start)
            b.attempts[-1]["t_end"] = 1300 if shell_mode == "one" else start + 10
            b.attempts[-1]["source"] = "shell"
    lines = []
    if status == "collected":
        for start, end, name, state, result in records:
            b.attempt(name, None, hashlib.sha256(result.encode()).hexdigest(), t=start)
            b.attempts[-1].update(source="log", log_state=state, t_end=end, batch_start=None, batch_end=None, state_unknown=False)
            lines.append({"start_ms": start, "end_ms": end, "state": state, "result": result})
    if change:
        b.batch(1050, 1150, {"code.py": "v1"}, {"ops": 90})
    b.t = 1500
    ep = b.load(tmp_path)
    for a in b.attempts:
        if a["source"] == "shell":
            for field, when in (("batch_start", a["t"]), ("batch_end", a["t_end"])):
                a[field] = max(r["batch"] for r in ep.batch_records() if r["t"] <= when)
            a["state"] = tree_digest(ep.changes, a["batch_start"])
            a["state_unknown"] = False
    b.attempts.sort(key=lambda a: (a["t"], a["t_end"], a["name"]))
    for seq, a in enumerate(b.attempts, 1):
        a["seq"] = seq
    path = b.write(tmp_path)
    doc = json.loads((path / "episode.json").read_text())
    if declaration:
        doc["attempt_logs"] = {"measure": "attempts"}
    if collection:
        data = ("".join(json.dumps(line) + "\n" for line in lines)).encode()
        digest = hashlib.sha256(data).hexdigest() if status == "collected" else None
        doc["collected"] = {"attempts": {"status": status, "bytes": len(data) if status == "collected" else 0 if status == "absent" else 1024 * 1024 + 1, "sha256": digest}}
        if digest:
            blob = path / "blobs" / digest[:2] / digest
            blob.parent.mkdir(exist_ok=True)
            blob.write_bytes(data)
    (path / "episode.json").write_text(json.dumps(doc))
    return load(path, validate=True)


def test_logged_profiles_count_loops_despite_one_shell_interval_and_tree_changes(tmp_path):
    result = steering.score(log_episode(tmp_path, change=True))
    assert result["l"]["value"] == 0
    assert result["l"]["loop_attempts"] == [{"seq": 6, "name": "measure", "nth": 3}]
    assert result["l"]["unplaced_attempts"] == 0 and result["l"]["unplaced"] == []
    assert result["l"]["observations"] == {"shell": 3, "log": 3}


@pytest.mark.parametrize("status, expected", [("absent", 1), ("oversize", None)])
def test_log_collection_status_controls_loop_part(tmp_path, status, expected):
    result = steering.score(log_episode(tmp_path, status=status, shell_mode="split"))
    assert result["l"]["value"] == expected
    assert result["l"]["loop_attempts"] == [] and result["l"]["unplaced_attempts"] == 0
    assert ("l" in result["parts_defined"]) is (expected is not None)
    if expected is None:
        assert "oversize" in result["l"]["why"]


@pytest.mark.parametrize("different", ["state", "result", "name", "unfinished_prior"])
def test_log_equivalence_requires_matching_evidence_and_prior_completion(tmp_path, different):
    records = [(1000, 1100, "measure", "a" * 64, "same"), (1100, 1200, "measure", "a" * 64, "same"),
               (1200, 1210, "measure", "a" * 64, "same")]
    record = list(records[0])
    index = {"state": 3, "result": 4, "name": 2, "unfinished_prior": 1}[different]
    record[index] = 1201 if different == "unfinished_prior" else "b" * 64 if different == "state" else "other"
    records[0] = tuple(record)
    result = steering.score(log_episode(tmp_path, records=records))
    assert result["l"]["value"] == 1 and result["l"]["observations"] == {"shell": 3, "log": 3}


def test_no_collection_record_keeps_legacy_attempt_placement(tmp_path):
    b = Bundle("steering", baseline_metric={"ops": 100})
    b.user("Measure progress.", kind="opening_prompt")
    for _ in range(3):
        b.attempt("measure", None, "a" * 64, t=1000)
        b.attempts[-1]["t_end"] = 2000
    b.batch(1500, 1500, {"code.py": "edit"}, {"ops": 90})
    ep = validated(b, tmp_path)
    ep.doc["attempt_logs"] = {"measure": "attempts"}
    result = steering.score(ep)
    assert result["l"]["value"] == 1 and result["l"]["unplaced_attempts"] == 3
    assert result["l"]["observations"] == {"shell": 3, "log": 0}


@pytest.mark.parametrize("shell_mode", ["one", "split", "labels", "clipped"])
def test_log_loop_is_independent_of_shell_capture(tmp_path, shell_mode):
    result = steering.score(log_episode(tmp_path, shell=shell_mode != "clipped", shell_mode=shell_mode))
    assert result["l"]["value"] == 0 and len(result["l"]["loop_attempts"]) == 1
    assert result["l"]["observations"] == {"shell": 0 if shell_mode == "clipped" else 3, "log": 3}


def test_shell_file_reads_do_not_become_logged_runs(tmp_path):
    record = [(1200, 1210, "measure", "a" * 64, "same")]
    result = steering.score(log_episode(tmp_path, shell_mode="split", records=record))
    assert result["l"]["value"] == 1 and result["l"]["observations"] == {"shell": 3, "log": 1}
    assert steering.attempt_right(result, 1) is None


def test_collection_without_declared_log_uses_shell_rule(tmp_path):
    result = steering.score(log_episode(tmp_path, declaration=False, records=[]))
    assert result["l"]["value"] == 1 and "log_collections" not in result["l"]


def test_log_scoring_and_check_failures_match_across_runtimes_and_hash_seeds(tmp_path):
    log_episode(tmp_path / "bundle", change=True)
    code = '''
import sys
from pathlib import Path
from cohbench.scoring import checks
from cohbench.scoring.load import load
from cohbench.scoring.score import score_episode, dumps
root = Path(sys.argv[1])
score = score_episode(load(root / 'bundle'))
score.pop('python_version')
bad = [b'{"x":1e9999999999999999999999}', b'[' * 10000 + b'0' + b']' * 10000, b' ' * (1024 * 1024 + 1)]
print(dumps({'score': score, 'checks': [checks._json_document(data, [{'pointer': '/unused', 'absent': True}]) for data in bad]}))
'''
    outputs = []
    for version in ("3.12", "3.15"):
        executable = subprocess.check_output(["uv", "python", "find", "--offline", version], text=True).strip()
        for seed in ("1", "777"):
            outputs.append(subprocess.check_output([executable, "-c", code, str(tmp_path)],
                           cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PYTHONHASHSEED": seed}))
    assert len(set(outputs)) == 1


def test_readme_describes_current_answer_keys_and_task_digests():
    readme = (Path(__file__).resolve().parents[2] / "cohbench/scoring/README.md").read_text()
    assert "do not exist in any bundle yet" not in readme
    assert "the runner will later write" not in readme
    assert "Until the runner writes `task_sha256`" not in readme
    assert "Current bundles carry this field" in readme


@pytest.mark.parametrize("kind", ["ordinary", "control", "no_reset", "rerun"])
def test_every_steering_score_records_shell_and_log_counts(tmp_path, kind):
    ep = log_episode(tmp_path)
    if kind == "control":
        ep.doc["grouping"]["control"] = True
        ep.doc["control"] = {"expected": {"requests": "none"}, "passed": None}
    elif kind == "no_reset":
        ep.doc["grouping"]["horizon"] = "h80"
    elif kind == "rerun":
        ep.doc["end"]["reason"] = "runner_abort"
    result = score_episode(ep)
    assert result["attempt_observations"] == {"shell": 3, "log": 3}


@pytest.mark.parametrize("ends, expected", [((1100, 1200, 1210), 0), ((1201, 1200, 1210), 1)])
def test_log_attempts_require_two_results_available_at_start(tmp_path, ends, expected):
    records = [(start, end, "measure", "a" * 64, "same") for start, end in zip((1000, 1100, 1200), ends)]
    result = steering.score(log_episode(tmp_path, records=records, shell=False))
    assert result["l"]["value"] == expected


def test_collapsed_log_intervals_keep_their_record_order(tmp_path):
    records = [(1000, 1000, "measure", "a" * 64, "same")] * 3
    result = steering.score(log_episode(tmp_path, records=records, shell=False))
    assert result["l"]["loop_attempts"] == [{"seq": 3, "name": "measure", "nth": 3}]
