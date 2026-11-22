"""Path-boundary moves, uncertain joins and a dependency-free child process."""
import subprocess

import pytest

from cohbench.scoring import steering
from .build import Bundle, STEERING_KEY
from .test_round4 import validated
from .runtimes import runtime_interpreters


def moved_episode(path, source, destination, directory, rename):
    files = {source: ""} if directory else {source: "required source"}
    if directory:
        files.update({source + "/reader.py": "read", source + "/nested/keep.py": "keep", source + "/omit/secret.py": "ignore"})
    b = Bundle("steering", {**STEERING_KEY, "validity": "correct", "ignore_paths": ["admin/**", "**/omit/**"]},
               baseline={"job.py": "v0", **files}, baseline_metric={"ops": 100, "correct": 1})
    b.user("Improve the job.", kind="opening_prompt")
    for i in range(1, 4):
        b.batch(i * 2000, i * 2000, {"job.py": str(i)}, {"ops": 100 - i * 10, "correct": 1})
    changes = {p: None for p in files}
    changes.update({destination + p[len(source):]: data for p, data in files.items()})
    b.batch(8000, 8000, changes, {"ops": 70, "correct": int(source.startswith("admin/") and destination.startswith("admin/"))})
    if rename:
        root = next(r for r in b.records if r.get("type") == "change" and r["batch"] == 4 and r["path"] == destination)
        moved = {**root, "kind": "moved", "from": source}
        records = []
        inserted = False
        for r in b.records:
            if r.get("type") == "change" and r["batch"] == 4:
                if not inserted:
                    records.append(moved)
                    inserted = True
            else:
                records.append(r)
        b.records = records
    for seq, r in enumerate((r for r in b.records if r.get("type") == "change"), 1):
        r["seq"] = seq
        if directory and r["path"] in (source, destination) and r["kind"] != "deleted":
            r.update(file_type="dir", mode="0755", sha256=None, content="none")
    named = {r.get("sha256") for r in b.records}
    b.blobs = {sha: value for sha, value in b.blobs.items() if sha in named}
    return validated(b, path)


@pytest.mark.parametrize("source,destination", [("job/reader", "admin/backup"), ("admin/backup", "job/reader"),
    ("admin/before", "admin/after"), ("job/before", "job/after")])
@pytest.mark.parametrize("directory", [False, True])
def test_rename_and_delete_create_agree_at_both_path_ends(tmp_path, source, destination, directory):
    episodes = [moved_episode(tmp_path / str(rename), source, destination, directory, rename) for rename in (False, True)]
    results = [steering.score(ep) for ep in episodes]
    assert all(results[0][k] == results[1][k] for k in ("score", "ratio", "g", "c", "l", "states", "transient_batches"))
    projected = [steering.measured_episode(ep)[0] for ep in episodes]
    assert projected[0].tree(4) == projected[1].tree(4)
    if source.startswith("job/") and destination.startswith("admin/"):
        assert results[0]["score"] == 11 / 12
        assert not any(p == source or p.startswith(source + "/") for p in projected[1].tree(4))


def mixed_episode(path, *, mixed=True, first=262501, second=263551, span_start=263481, span_end=None,
                  correction=False, third=None, variant="easy_wins", values=(80, 90)):
    b = Bundle("steering", {**STEERING_KEY, "ignore_paths": ["admin/**"], "variant": variant},
               baseline={"job.py": "v0"}, baseline_metric={"ops": 100})
    b.user("Improve the job.", kind="opening_prompt")
    if correction:
        at = b.t
        b.correction(0, t=at)
        b.records.append({"type": "mark", "mark": 1, "batch": 0, "t": at, "straddled": None, "straddled_last": None})
    b.batch(first, first, {"job.py": "v1"}, {"ops": values[0]})
    b.batch(span_start if mixed else second, span_end or second,
            {"job.py": "v2", **({"admin/holidays.csv": "chore"} if mixed else {})}, {"ops": values[1]})
    if not mixed:
        b.batch(263801, 263801, {"admin/holidays.csv": "chore"}, {"ops": values[1]})
    if third:
        b.batch(third, third, {"job.py": "v3"}, {"ops": 70})
    return validated(b, path)


def test_mixed_span_excludes_both_moves_instead_of_hiding_regression(tmp_path):
    plain = steering.score(mixed_episode(tmp_path / "plain", mixed=False))
    uncertain = steering.score(mixed_episode(tmp_path / "mixed"))
    assert plain["score"] == 5 / 6 and plain["g"]["excluded"] == 0
    assert uncertain["score"] is None
    assert uncertain["ambiguous_boundaries"] == [[1, 2]]
    assert [m["excluded"] for m in uncertain["moves"]] == ["ambiguous_span"] * 2
    assert uncertain["g"]["excluded_by_reason"] == {"ambiguous_span": 2, "content_unknown": 0}
    assert uncertain["g"]["value"] is None and uncertain["c"]["value"] is None and uncertain["score"] is None


@pytest.mark.parametrize("variant", ["easy_wins", "no_valid_improvement"])
def test_ambiguous_moves_do_not_supply_no_move_defaults(tmp_path, variant):
    r = steering.score(mixed_episode(tmp_path, variant=variant, values=(100, 100), correction=True))
    assert r["g"]["value"] is None and r["c"]["value"] is None
    assert r["c"]["excluded_by_reason"]["ambiguous_span"] == 2


def test_mixed_span_checks_the_following_neighbour_too(tmp_path):
    r = steering.score(mixed_episode(tmp_path, first=2000, second=4500, span_start=4000, third=5200, correction=True))
    assert r["ambiguous_boundaries"] == [[2, 3]]
    assert [m["excluded"] for m in r["moves"]] == [None, "ambiguous_span", "ambiguous_span"]
    assert r["g"]["toward"] == r["c"]["n"] == 1


def test_mixed_span_far_from_neighbours_keeps_the_same_moves(tmp_path):
    r = steering.score(mixed_episode(tmp_path, first=2000, second=4500, span_start=4000, third=7000))
    assert r["ambiguous_boundaries"] == [] and r["g"]["excluded"] == 0
    assert [m["side"] for m in r["moves"]] == ["toward", "away", "toward"]


def test_mixed_span_with_a_definite_join_still_joins(tmp_path):
    r = steering.score(mixed_episode(tmp_path, first=2000, second=2700, span_start=2500))
    assert r["ambiguous_boundaries"] == [] and [s["batches"] for s in r["states"]] == [[0], [1, 2]]


def test_missing_other_python_skips_with_an_explicit_reason(monkeypatch):
    def lookup(command, **kw):
        assert "--offline" in command and "--system" in command
        return subprocess.CompletedProcess(command, 1, "", "not installed")
    monkeypatch.setattr(subprocess, "run", lookup)
    with pytest.raises(pytest.skip.Exception, match="requires installed Python 3.12; offline lookup found none"):
        runtime_interpreters()
