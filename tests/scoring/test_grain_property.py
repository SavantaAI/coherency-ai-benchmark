"""Grain invariance within the log's stated limits, and the three exceptions."""
from cohbench.scoring import steering
from .timeline import generate, render, no_mixed_unchanged_writes, no_mixed_transient_runs
from .test_round4 import validated


def test_seeded_grain_property_200_timelines(tmp_path):
    checked = []
    for seed in range(2000):
        timeline = generate(seed)
        if not no_mixed_unchanged_writes(timeline) or not no_mixed_transient_runs(timeline):
            continue
        results = [steering.score(render(timeline, merge).load(tmp_path / f"{seed}-{merge}")) for merge in (False, True)]
        assert all(results[0][part] == results[1][part] for part in ("g", "c", "l", "score")), (seed, timeline, results)
        checked.append(seed)
        if len(checked) == 200:
            break
    assert len(checked) == 200


def test_seed7_unchanged_write_extends_only_the_merged_span(tmp_path):
    timeline = generate(7)
    assert timeline == [
        {"t": 862, "kind": "write", "path": "pkg/core.py", "content": "v2"},
        {"t": 880, "kind": "attempt", "fp": "f1"},
        {"t": 985, "kind": "write", "path": "pkg/core.py", "content": "v2"},
    ]
    assert not no_mixed_unchanged_writes(timeline)
    episodes = [render(timeline, merge).load(tmp_path / str(merge)) for merge in (False, True)]
    assert [len(ep.batch_records()) for ep in episodes] == [2, 2]
    states = [steering.attempt_states(ep, steering.groups(ep))[1] for ep in episodes]
    assert states[0] is not None and states[1] is None
    assert [steering.score(ep)["l"]["unplaced_attempts"] for ep in episodes] == [0, 1]


def test_seed114_transient_sharing_a_batch_extends_only_the_merged_group(tmp_path):
    timeline = [
        {"t": 3686, "kind": "write", "path": "pkg/core.py", "content": "v2"},
        {"t": 3794, "kind": "write", "path": "pkg/tmp.txt", "content": "x"},
        {"t": 3940, "kind": "attempt", "fp": "f1"},
        {"t": 4444, "kind": "write", "path": "pkg/tmp.txt", "content": None},
    ]
    assert no_mixed_unchanged_writes(timeline) and not no_mixed_transient_runs(timeline)
    episodes = [render(timeline, merge).load(tmp_path / str(merge)) for merge in (False, True)]
    assert [steering.spans(ep, steering.groups(ep))[-1] for ep in episodes] == [(3686, 3686), (3686, 4444)]
    assert steering.attempt_states(episodes[0], steering.groups(episodes[0]))[1] is not None
    assert steering.attempt_states(episodes[1], steering.groups(episodes[1]))[1] is None
    assert [steering.score(ep)["l"]["unplaced_attempts"] for ep in episodes] == [0, 1]


def test_render_omits_batches_whose_writes_net_to_nothing(tmp_path):
    timeline = [{"t": 1000, "kind": "write", "path": "pkg/tmp.txt", "content": "x"},
                {"t": 1100, "kind": "write", "path": "pkg/tmp.txt", "content": None}]
    assert len(render(timeline, True).load(tmp_path / "merged").batch_records()) == 1
    assert len(render(timeline, False).load(tmp_path / "split").batch_records()) == 3


def test_seed271_omitted_transient_run_does_not_change_attempt_placement(tmp_path):
    timeline = generate(271)
    assert no_mixed_unchanged_writes(timeline) and no_mixed_transient_runs(timeline)
    episodes = [render(timeline, merge).load(tmp_path / str(merge)) for merge in (False, True)]
    assert [len(steering.transient_batches(ep)) for ep in episodes] == [2, 0]
    states = [steering.attempt_states(ep, steering.groups(ep)) for ep in episodes]
    assert states[0] == states[1] and states[0][1] is not None
    a, b = [steering.score(ep) for ep in episodes]
    assert all(a[part] == b[part] for part in ("g", "c", "l", "score"))


def test_omitted_empty_batch_breaks_the_link_between_lasting_changes(tmp_path):
    timeline = [
        {"t": 1000, "kind": "write", "path": "pkg/core.py", "content": "v1"},
        {"t": 1800, "kind": "write", "path": "tmp", "content": "x"},
        {"t": 2000, "kind": "write", "path": "tmp", "content": None},
        {"t": 2800, "kind": "write", "path": "pkg/core.py", "content": "v0"},
    ]
    split, merged = [steering.score(validated(render(timeline, merge), tmp_path / str(merge))) for merge in (False, True)]
    assert split["transient_batches"] == [1, 2, 3, 4] and split["score"] == 2 / 3
    assert merged["transient_batches"] == [] and merged["score"] == 5 / 6
