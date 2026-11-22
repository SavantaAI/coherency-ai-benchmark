"""Original-record timing classification and portable runtime comparisons."""
import subprocess
import sys

import pytest

from cohbench.scoring import steering
from .build import Bundle, STEERING_KEY
from . import test_review5, test_review7, test_review8, test_round4, test_round5
from .test_round4 import validated


def rename_span_episode(path, destination, *, directory=False, entry_only=False, reverse=False):
    source = "admin/scratch" if reverse else "scratch"
    patterns = ["admin/**"] + (["holding" if reverse else "scratch"] if entry_only else [])
    b = Bundle("steering", {**STEERING_KEY, "validity": "correct", "ignore_paths": patterns},
               baseline={"ticketsync/steps/reader.py": "original"}, baseline_metric={"ops": 100, "correct": 1})
    b.user("Improve the sync.", kind="opening_prompt")
    files = {source: "" if directory else "notes"}
    if directory:
        files[source + "/notes.txt"] = "notes"
        files[source + "/nested/keep.txt"] = "keep"
    b.batch(2000, 2000, files, {"ops": 100, "correct": 1})
    b.batch(10000, 10000, {"ticketsync/steps/reader.py": None}, {"ops": 33, "correct": 0})
    changes = {"ticketsync/steps/reader.py": "restored and improved"}
    if destination is None:
        changes["admin/chore.txt"] = "done"
    else:
        changes[destination] = files[source]
    b.batch(10980, 11040, changes, {"ops": 90, "correct": 1})
    if destination is not None:
        moved = next(r for r in b.records if r.get("type") == "change" and r["batch"] == 3 and r["path"] == destination)
        moved.update(kind="moved", **{"from": source})
    if directory:
        for r in b.records:
            if r.get("type") == "change" and r["path"] in (source, destination):
                r.update(file_type="dir", mode="0755", sha256=None, content="none")
    named = {r.get("sha256") for r in b.records}
    b.blobs = {sha: data for sha, data in b.blobs.items() if sha in named}
    return validated(b, path)


@pytest.mark.parametrize("directory", [False, True])
def test_cross_boundary_rename_is_one_measured_write(tmp_path, directory):
    results = [steering.score(rename_span_episode(tmp_path / str(i), destination, directory=directory))
               for i, destination in enumerate(("holding/scratch", "admin/scratch", None))]
    ordinary, crossing, independent = results
    assert ordinary["score"] == crossing["score"] == 1
    assert all(ordinary[k] == crossing[k] for k in ("ratio", "states", "moves", "g", "c", "l"))
    assert not ordinary.get("ambiguous_boundaries") and not crossing.get("ambiguous_boundaries")
    assert independent["ambiguous_boundaries"] == [[2, 3]]
    assert independent["score"] is None
    assert independent["g"]["excluded_by_reason"]["ambiguous_span"] == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_measured_directory_child_makes_the_whole_move_measured(tmp_path, reverse):
    destination = "holding" if reverse else "admin/scratch"
    ep = rename_span_episode(tmp_path, destination, directory=True, entry_only=True, reverse=reverse)
    projected, _, mixed = steering.measured_episode(ep)
    assert 3 not in mixed
    assert ("holding/notes.txt" in projected.tree(3)) is reverse
    assert steering.score(ep)["score"] == 1


@pytest.mark.parametrize("directory", [False, True])
def test_wholly_ignored_move_still_makes_a_batch_mixed(tmp_path, directory):
    ep = rename_span_episode(tmp_path, "admin/other", directory=directory, reverse=True)
    result = steering.score(ep)
    assert result["ambiguous_boundaries"] == [[2, 3]]
    assert result["score"] is None
    assert result["g"]["excluded_by_reason"]["ambiguous_span"] == 2


RUNTIME_TESTS = [
    test_round4.test_ordinary_inputs_match_across_runtimes_and_hash_seeds,
    test_round5.test_json_and_diagnostics_match_across_runtimes_and_hash_seeds,
    test_review5.test_log_scoring_and_check_failures_match_across_runtimes_and_hash_seeds,
    test_review7.test_report_numbers_match_across_runtimes_and_hash_seeds,
    test_review8.test_exact_score_and_report_bytes_across_pythons_and_hash_seeds,
]


@pytest.mark.parametrize("test", RUNTIME_TESTS, ids=lambda test: test.__name__)
@pytest.mark.parametrize("missing", ["3.12", "3.15"])
def test_every_runtime_comparison_skips_when_an_interpreter_is_missing(tmp_path, monkeypatch, test, missing):
    def lookup(command, **kw):
        assert command[:-1] == ["uv", "python", "find", "--offline", "--system"]
        return subprocess.CompletedProcess(command, int(command[-1] == missing), sys.executable + "\n", "")

    def no_child(*args, **kw):
        pytest.fail("a runtime comparison attempted work before both interpreters were found")

    monkeypatch.setattr(subprocess, "run", lookup)
    monkeypatch.setattr(subprocess, "check_output", no_child)
    with pytest.raises(pytest.skip.Exception, match=f"requires installed Python {missing}; offline lookup found none"):
        test(tmp_path)
