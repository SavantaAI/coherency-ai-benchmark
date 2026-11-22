"""The normalised change log: watcher records reduced to task state, on the
episode clock, cut at the episode end."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from cohbench import watcher
from cohbench.trajectory import changelog, io

from .conftest import REAL, real_watcher

START = "2026-11-21T10:00:00.000Z"
W = lambda s: f"2026-11-21T10:00:{s}Z"  # noqa: E731


def f(seq, s, kind, path, batch, **k):
    return {"seq": seq, "wall": W(s), "kind": kind, "path": path, "batch": batch, "type": k.pop("type", "file"), "mode": "0644",
            "sha256": k.pop("sha256", str(seq % 10) * 64), "content": k.pop("content", "stored"), **k}


def b(seq, s, batch, **k):
    return {"seq": seq, "wall": W(s), "kind": "batch", "batch": batch, **k}


def real_log(**kw):
    w1, seq = real_watcher(1)
    w2, _ = real_watcher(2)
    kw.setdefault("keep_marks", {"m-1", "reset-1"})  # as if a correction and a probe referred to them
    return changelog.join([(w1.events, w1.metrics), (w2.events, w2.metrics)], seq["start_utc"],
                          excluded=seq["excluded"], end_mark=seq["end_mark"], marks=seq["marks"], **kw)


@pytest.fixture(scope="module")
def log():
    return real_log()


def raw(segment):
    return [json.loads(x) for x in (REAL / f"segment-{segment}" / "events.jsonl").read_text().splitlines()]


def test_the_real_watcher_output_projects_to_task_state(log):
    assert changelog.validate(log) == []
    assert log.header == {"type": "header", "format": "cohbench-changelog", "version": 1, "records": 61, "changes": 27, "batches": 15, "marks": 3, "metrics": 10, "no_metric": 5}
    recs = log.records
    assert [r["t"] for r in recs if "t" in r] == sorted(r["t"] for r in recs if "t" in r)
    assert len(log.wall_us) == len(recs) and log.wall_us == sorted(log.wall_us)
    assert [r["seq"] for r in recs if r["type"] == "change"] == list(range(1, 28))
    assert [r["batch"] for r in recs if r["type"] == "batch"] == list(range(15))
    # Marks carry neutral numbers; the labels stay in memory for the audit record.
    assert [r["mark"] for r in recs if r["type"] == "mark"] == [1, 2, 3] and log.mark_ids == {"m-1": 1, "reset-1": 2, "end-1": 3}
    created = next(r for r in recs if r["type"] == "change" and r["path"] == "tmp/c0ffee12ab.bin")
    assert {k: created[k] for k in ("kind", "from", "content", "file_type", "mode", "target", "batch")} == {
        "kind": "created", "from": None, "content": "stored", "file_type": "file", "mode": "0644", "target": None, "batch": 1}
    assert created["sha256"] == hashlib.sha256(bytes(range(64))).hexdigest()
    moved = next(r for r in recs if r.get("kind") == "moved" and r["path"] == "out/final.txt")
    assert moved["from"] == "out/summary.txt"
    assert next(r for r in recs if r.get("path") == "big.bin")["content"] == "oversize"
    assert next(r for r in recs if r.get("path") == "out/summary.txt" and r["kind"] == "rewritten")
    # The baseline came before the opening prompt: on the episode clock it is at 0.
    assert recs[0]["t"] == 0


def test_the_projected_tree_is_the_watchers_own_replay(log):
    def watcher_tree(segment, batch):
        st = watcher.state_at(str(REAL / f"segment-{segment}"), batch)
        return {p: (e.type, f"{e.mode:04o}", e.sha256, e.target) for p, e in st.entries.items()}

    marks = {label: next(r["batch"] for r in log.records if r["type"] == "mark" and r["mark"] == n) for label, n in log.mark_ids.items()}
    assert log.tree(marks["m-1"]) == watcher_tree(1, watcher.mark_batch(str(REAL / "segment-1"), "m-1"))
    assert log.tree(marks["reset-1"]) == watcher_tree(1, "last")
    assert log.tree(marks["end-1"]) == watcher_tree(2, watcher.mark_batch(str(REAL / "segment-2"), "end-1"))


def test_the_source_leaves_out_the_instruction_file_and_driver_refs(log):
    """D-026: the watcher never records them, nor names that contain one
    (the fixture rewrites CLAUDE.md through .CLAUDE.md.tmp and writes a
    CLAUDE.md~ backup); nothing here filters them."""
    for n in (1, 2):
        text = (REAL / f"segment-{n}" / "events.jsonl").read_text()
        assert "CLAUDE.md" not in text and "refs/t3" not in text
        stored = [p.read_bytes() for p in (REAL / f"segment-{n}" / "blobs").rglob("*") if p.is_file()]
        assert not any(b"Instructions" in x for x in stored)
    assert any("CLAUDE.md~" in s["step"] for s in json.loads((REAL / "sequence.json").read_text())["steps"])
    assert "CLAUDE.md" not in log.dumps() and "refs/t3" not in log.dumps()


def test_post_run_activity_is_cut_at_the_end_mark(log):
    assert any(r.get("path") == "post.txt" for r in raw(2))
    assert "post.txt" not in log.dumps()


def test_a_temp_file_living_across_two_batches_is_two_changes(log):
    """D-028: nothing is merged across batches. The fixture's temp name lived
    across two batches twice: each is a creation, then a move. Inside one
    batch the watcher already wrote the net result, the target's own record."""
    r1 = raw(1)
    inside = [r for r in r1 if r.get("via_rename") == "out/.report.txt.tmp"]
    assert [(r["kind"], r["path"]) for r in inside] == [("modified", "out/report.txt")]
    writes = [(r["kind"], r["path"], r["from"]) for r in log.records if r["type"] == "change" and "report" in r["path"]]
    assert writes == [("created", "out/.report.txt.tmp", None), ("moved", "out/report.txt", "out/.report.txt.tmp"), ("modified", "out/report.txt", None),
                      ("created", "out/.report.txt.tmp", None), ("deleted", "out/report.txt", None), ("moved", "out/report.txt", "out/.report.txt.tmp")]
    assert "via_rename" not in log.dumps()


def test_unreferenced_marks_are_dropped_and_the_rest_renumbered():
    log = real_log(keep_marks=())
    assert [r["mark"] for r in log.records if r["type"] == "mark"] == [1] and log.mark_ids == {"end-1": 1}
    assert changelog.validate(log) == []
    assert "m-1" not in log.dumps() and "reset-1" not in log.dumps()


def test_every_reading_describes_the_tree_of_the_batch_it_names(log):
    """The fixture's metric counts files and bytes. One reading per batch
    whose copy held the whole tree; it agrees with the projected tree of its
    batch and says nothing of why or when it was taken. From batch 10 on the
    tree holds big.bin, over the fixture's blob limit, so every copy left it
    out for size: no reading of those trees counts (D-036)."""
    rows = [r for r in log.records if r["type"] == "metric"]
    assert [r["batch"] for r in rows] == list(range(10))
    assert [(r["batch"], r["reason"]) for r in log.records if r["type"] == "no_metric"] == [(b, "incomplete") for b in range(10, 15)]
    assert all(log.no_reading[b] == {"detail": "incomplete", "lacked": ["big.bin"]} for b in range(10, 15))
    assert all(set(r) == {"type", "batch", "ok", "metrics", "absent", "absent_count"} for r in rows)
    m1 = [json.loads(x) for x in (REAL / "segment-1" / "metrics.jsonl").read_text().splitlines()]
    # The watcher's fifth round: one reading per committed batch, no mark readings.
    assert {m["reason"] for m in m1} == {"baseline", "change"} and all("complete" in m for m in m1)
    for r in rows:
        tree = log.tree(r["batch"])
        files = [p for p, v in tree.items() if v[0] == "file" and p not in r["absent"]]
        assert r["metrics"]["files"] == len(files), (r, files)


def test_the_two_segments_join_across_the_handoff(log):
    # The second watcher's baseline and its reading fall inside the handoff and are bookkeeping.
    assert [r["batch"] for r in log.records if r["type"] == "batch" and r["baseline"]] == [0]
    reset = next(r for r in log.records if r["type"] == "mark" and r["mark"] == log.mark_ids["reset-1"])
    after = [r for r in log.records if r["type"] == "change" and r["t"] > reset["t"]]
    assert [r["path"] for r in after][:2] == ["c.txt", "data/a.csv"] and after[0]["batch"] == reset["batch"] + 1
    # At most one reading per batch; the handoff's batch holds big.bin, so no copy of it counts (D-036).
    assert sum(1 for r in log.records if r["type"] in ("metric", "no_metric") and r["batch"] == reset["batch"]) == 1
    # On the episode clock the handoff's length is left out.
    w2, seq = real_watcher(2)
    us = lambda u: changelog._us(u, changelog.datetime.fromisoformat(seq["start_utc"].replace("Z", "+00:00")), "t")  # noqa: E731
    h = seq["excluded"][0]
    # A change dates from its batch's first write, not the batch's commit.
    first = next(r for r in w2.events if r["kind"] == "batch" and r["batch"] == next(x for x in w2.events if x.get("path") == "c.txt")["batch"])["first_write"]
    wall = us(first)
    assert after[0]["t"] == (wall - (us(h["end_utc"]) - us(h["start_utc"]))) // 1000


def test_a_resync_batch_is_deletions_and_creations(log):
    b = next(r for r in log.records if r["type"] == "batch" and r["resync"])
    recs = [(r["kind"], r["path"]) for r in log.records if r["type"] == "change" and r["batch"] == b["batch"]]
    assert recs == [("deleted", "p.txt"), ("deleted", "q.txt"), ("created", "p.txt"), ("created", "q.txt")]
    before, now = log.tree(b["batch"] - 1), log.tree(b["batch"])
    assert (now["p.txt"][2], now["q.txt"][2]) == (before["q.txt"][2], before["p.txt"][2])


def test_an_inaccessible_directory_and_the_readings_absent_paths(log):
    locked = next(r for r in log.records if r.get("path") == "locked")
    assert (locked["file_type"], locked["content"]) == ("dir", "inaccessible")
    # The inaccessible directory is an omission no copy can avoid; big.bin,
    # left out for size, is what makes those copies partial (D-036).
    assert log.no_reading[locked["batch"]]["lacked"] == ["big.bin"]
    assert all(r["absent_count"] == len(r["absent"]) for r in log.records if r["type"] == "metric")


def test_free_form_strings_are_gone(log):
    text = log.dumps()
    for word in ("/tmp/task", "request", "mono", "ino", "uid", "via_rename", "first_event", "check", "renames", ".swap.tmp", "skipped_count", "stderr"):
        assert word not in text


def test_a_watcher_that_did_not_stop_cleanly_needs_the_end_mark(tmp_path):
    d = tmp_path / "w"
    shutil.copytree(REAL / "segment-2", d)
    hb = json.loads((d / "heartbeat.json").read_text())
    (d / "heartbeat.json").write_text(json.dumps({**hb, "status": "failed", "error": "boom"}))
    _, seq = real_watcher(2)
    with pytest.raises(changelog.ChangeLogError, match="'failed', not 'stopped'"):
        changelog.build_from_dir(d, seq["start_utc"])
    assert changelog.validate(changelog.build_from_dir(d, seq["start_utc"], end_mark="end-1")) == []


def test_a_directory_move_is_never_collapsed():
    """The draft-to-published case: a directory and its file created in one
    batch, the directory renamed in the next. The tree keeps the file under
    its new name."""
    recs = [f(1, "00.0", "created", "README.md", 0), b(2, "00.1", 0),
            f(3, "01.0", "created", "draft", 1, type="dir", content="none", sha256=None), f(4, "01.0", "created", "draft/result", 1, sha256="7" * 64), b(5, "01.1", 1),
            f(6, "02.0", "moved", "published", 2, type="dir", content="none", sha256=None, **{"from": "draft"}), b(7, "02.1", 2)]
    log = changelog.build(recs, START)
    assert changelog.validate(log) == []
    assert sorted(log.tree(2)) == ["README.md", "published", "published/result"]
    assert [(r["kind"], r["path"]) for r in log.records if r["type"] == "change"][-1] == ("moved", "published")


@pytest.mark.parametrize("why, recs", [
    ("another directory", [f(3, "01.0", "created", "tmp/a.part", 1), b(4, "01.1", 1), f(5, "02.0", "moved", "out/a", 2, **{"from": "tmp/a.part"}), b(6, "02.1", 2)]),
    ("from the baseline", [f(3, "00.0", "created", "a.part", 0), b(4, "00.1", 0), f(5, "02.0", "moved", "a", 1, **{"from": "a.part"}), b(6, "02.1", 1)]),
    ("named in between", [f(3, "01.0", "created", "a.part", 1), b(4, "01.1", 1), f(5, "01.5", "attrib", "a.part", 2, mode="0600", changed=["mode"]),
                          f(6, "01.5", "moved", "a", 2, **{"from": "a.part"}), b(7, "01.6", 2)]),
    ("two batches before", [f(3, "01.0", "created", "a.part", 1), b(4, "01.1", 1), f(5, "01.5", "created", "z", 2), b(6, "01.6", 2),
                            f(7, "02.0", "moved", "a", 3, **{"from": "a.part"}), b(8, "02.1", 3)]),
    ("another identity", [f(3, "01.0", "created", "a.part", 1, ino=5), b(4, "01.1", 1), f(5, "02.0", "moved", "a", 2, ino=6, **{"from": "a.part"}), b(6, "02.1", 2)]),
])
def test_a_move_without_positive_evidence_is_recorded_as_it_was(why, recs):
    base = [f(1, "00.0", "created", "README.md", 0), b(2, "00.1", 0)] if recs[1]["batch"] != 0 else [f(1, "00.0", "created", "README.md", 0)]
    recs = [{**r, "seq": n + 1} for n, r in enumerate(base + recs)]
    log = changelog.build(recs, START)
    assert any(r.get("kind") == "moved" for r in log.records), why


def test_a_temp_file_across_two_batches_is_recorded_alike_on_both_sides():
    """Two harnesses that write through a temp name slower than one batch
    both show a creation and a move: the same changes, never merged."""
    one = [f(1, "00.0", "created", "a.txt", 0, sha256="1" * 64), b(2, "00.1", 0),
           f(3, "01.0", "created", ".a.tmp", 1, sha256="3" * 64, ino=7), b(4, "01.0", 1),
           f(5, "01.1", "deleted", "a.txt", 2, sha256="1" * 64), f(6, "01.1", "moved", "a.txt", 2, sha256="3" * 64, ino=7, **{"from": ".a.tmp"}), b(7, "01.1", 2)]
    two = [{**r, "ino": 9} if r.get("ino") else r for r in one]
    two = [{**r, "path": "a.txt.part"} if r.get("path") == ".a.tmp" else {**r, "from": "a.txt.part"} if r.get("from") == ".a.tmp" else r for r in two]
    kinds = lambda log: [(r["kind"], r["path"] == "a.txt", r["batch"]) for r in log.records if r["type"] == "change"]  # noqa: E731
    a, c = changelog.build(one, START), changelog.build(two, START)
    assert changelog.validate(a) == changelog.validate(c) == []
    assert kinds(a) == kinds(c) == [("created", True, 0), ("created", False, 1), ("deleted", True, 2), ("moved", True, 2)]


def test_a_rename_of_task_output_in_a_later_batch_is_a_move():
    """draft.txt written, then renamed final.txt a batch later: both stay."""
    recs = [f(1, "00.0", "created", "README.md", 0), b(2, "00.1", 0), f(3, "01.0", "created", "draft.txt", 1, ino=4), b(4, "01.1", 1),
            f(5, "02.0", "moved", "final.txt", 2, ino=4, **{"from": "draft.txt"}), b(6, "02.1", 2)]
    metrics = [{"run": n, "batch": n - 1, "reason": "change", "end": W(f"0{n}.5"), "ok": True, "metrics": {"n": n}} for n in (1, 2, 3)]
    log = changelog.build(recs, START, metrics)
    assert [(r["kind"], r["path"]) for r in log.records if r["type"] == "change"] == [("created", "README.md"), ("created", "draft.txt"), ("moved", "final.txt")]
    assert [r["batch"] for r in log.records if r["type"] == "metric"] == [0, 1, 2]


def test_an_identity_or_owner_change_alone_is_a_rewrite():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "01.0", "attrib", "a", 1, sha256="1" * 64, changed=["owner", "identity"]), b(4, "01.1", 1),
            f(5, "02.0", "attrib", "a", 2, sha256="1" * 64, mode="0600", changed=["mode"]), b(6, "02.1", 2)]
    assert [r["kind"] for r in changelog.build(recs, START).records if r["type"] == "change"] == ["created", "rewritten", "attribute"]


def test_a_reading_inside_an_excluded_interval_still_measures_its_tree():
    """M2 of round 8: a retained batch's reading that finished during a
    probe copy is the batch's reading; the tree it measured is what matters.
    It carries no time, so nothing is dated inside the copy."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "04.9", "created", "x", 1), b(4, "04.95", 1)]
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "metrics": {"n": 1}},
               {"run": 2, "batch": 1, "reason": "change", "end": W("05.2"), "ok": True, "metrics": {"n": 2}}]
    log = changelog.build(recs, START, metrics, excluded=[{"t": 5000, "t_end": 7000, "reason": "probe"}])
    assert changelog.validate(log) == []
    assert [(r["type"], r["batch"], r.get("metrics")) for r in log.records if r["type"] in ("metric", "no_metric")] == [("metric", 0, {"n": 1}), ("metric", 1, {"n": 2})]
    assert all("t" not in r for r in log.records if r["type"] == "metric")


def test_marks_on_a_later_segments_first_batch_survive_the_join():
    """N1: marks issued before the second segment's first change."""
    logs = two_logs()
    log2, m2 = logs[1]
    mark = {"seq": 0, "wall": W("04.9"), "kind": "mark", "label": "c-1", "batch": 0}
    end = {"seq": 0, "wall": W("04.95"), "kind": "mark", "label": "end-1", "batch": 0}
    log2 = log2[:4] + [mark, end] + log2[4:]
    log2 = [{**r, "seq": n + 1} for n, r in enumerate(log2)]
    log = changelog.join([logs[0], (log2, m2)], START, excluded=HANDOFF, end_mark="end-1", keep_marks={"c-1"})
    assert changelog.validate(log) == []
    assert [(r["mark"], r["batch"]) for r in log.records if r["type"] == "mark"] == [(1, 1), (2, 1)]
    assert log.mark_ids == {"c-1": 1, "end-1": 2}
    assert [r["path"] for r in log.records if r["type"] == "change"] == ["a", "b"]


def test_duplicate_mark_ids_are_an_error():
    recs = [f(1, "00.0", "created", "a", 0), {"seq": 2, "wall": W("00.05"), "kind": "mark", "label": "m-1", "batch": 0},
            {"seq": 3, "wall": W("00.06"), "kind": "mark", "label": "m-1", "batch": 0}, b(4, "00.1", 0)]
    with pytest.raises(changelog.ChangeLogError, match="appears twice"):
        changelog.build(recs, START)


def test_the_episode_end_selects_the_final_state():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "05.0", "created", "b", 1), b(4, "05.1", 1),
            {"seq": 5, "wall": W("06.0"), "kind": "mark", "label": "end-1", "batch": 1}, f(6, "09.0", "created", "c", 2), b(7, "09.1", 2)]
    metrics = [{"run": 1, "batch": 1, "reason": "mark", "label": "end-1", "end": W("10.0"), "ok": True, "metrics": {"x": 1}},
               {"run": 2, "batch": 2, "reason": "change", "end": W("10.0"), "ok": True, "metrics": {"x": 2}}]
    by_time = changelog.build(recs, START, metrics, end_t=7000)
    assert [r["path"] for r in by_time.records if r["type"] == "change"] == ["a", "b"]
    # Batch 1's only reading, requested by a mark and finished after the end,
    # measured batch 1's tree: it is batch 1's. Batch 2 is not retained.
    assert [(r["type"], r["batch"]) for r in by_time.records if r["type"] in ("metric", "no_metric")] == [("no_metric", 0), ("metric", 1)]
    by_mark = changelog.build(recs, START, metrics, end_t=7000, end_mark="end-1")
    assert [r["path"] for r in by_mark.records if r["type"] == "change"] == ["a", "b"]
    assert [r["mark"] for r in by_mark.records if r["type"] == "mark"] == [1]
    with pytest.raises(changelog.ChangeLogError, match="end mark"):
        changelog.build(recs, START, end_mark="nope")


def test_the_end_mark_and_the_end_time_must_agree():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "05.0", "created", "b", 1), b(4, "05.1", 1),
            {"seq": 5, "wall": W("06.0"), "kind": "mark", "label": "end-1", "batch": 1}, f(6, "09.0", "created", "c", 2), b(7, "09.1", 2)]
    # A later batch closed before the end: the mark is not the end.
    with pytest.raises(changelog.ChangeLogError, match="the mark and the end disagree"):
        changelog.build(recs, START, end_t=9500, end_mark="end-1")
    # The mark's batch closed after the end by more than the settling window.
    with pytest.raises(changelog.ChangeLogError, match="beyond the settling window"):
        changelog.build(recs, START, end_t=4900, end_mark="end-1")
    # Within the window (100 ms) it is the same boundary.
    assert changelog.validate(changelog.build(recs, START, end_t=5050, end_mark="end-1")) == []


def test_decisions_are_made_on_microseconds():
    """A batch closing 400 us after the end is not retained, though both
    round to the same millisecond."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            {**f(3, "05.0004", "created", "b", 1)}, {**b(4, "05.0004", 1)}]
    log = changelog.build(recs, START, end_us=5_000_000)
    assert [r["path"] for r in log.records if r["type"] == "change"] == ["a"]
    assert [r["path"] for r in changelog.build(recs, START, end_us=5_000_400).records if r["type"] == "change"] == ["a", "b"]


def test_the_episode_clock_and_excluded_intervals():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), b(3, "03.0", 1), f(4, "08.0", "created", "b", 2), b(5, "08.1", 2)]
    log = changelog.build(recs, START, excluded=[{"t": 2000, "t_end": 5000, "reason": "handoff"}])
    assert [(r["type"], r["t"]) for r in log.records if r["type"] in ("change", "batch")] == [("change", 0), ("batch", 100), ("change", 5000), ("batch", 5100)]
    # The start and end of an interval are outside it.
    assert changelog.validate(changelog.build([*recs[:2], f(3, "02.0", "created", "x", 1), b(4, "02.0", 1)], START, excluded=[{"t": 2000, "t_end": 5000, "reason": "handoff"}])) == []
    with pytest.raises(changelog.ChangeLogError, match="inside the excluded handoff"):
        changelog.build([*recs, f(6, "04.0", "created", "x", 1)], START, excluded=[{"t": 2000, "t_end": 5000, "reason": "handoff"}])


def test_a_symlink_keeps_its_target():
    log = changelog.build([f(1, "00.0", "created", "link", 0, type="symlink", target="data/a.csv", content="none", sha256="a" * 64), b(2, "00.1", 0)], START)
    assert next(r for r in log.records if r["type"] == "change")["target"] == "data/a.csv"


def test_marks_must_be_runner_issued_ids():
    rec = {"seq": 1, "wall": W("00.0"), "kind": "mark", "label": "turn end, the agent seemed stuck", "batch": 0}
    with pytest.raises(changelog.ChangeLogError, match="runner-issued id"):
        changelog.build([rec], START)
    with pytest.raises(changelog.ChangeLogError, match="not one the runner issued"):
        changelog.build([{**rec, "label": "m-9"}], START, marks={"m-1"})
    with pytest.raises(changelog.ChangeLogError, match="not one the runner issued"):
        changelog.build([], START, [{"run": 1, "batch": 0, "reason": "mark", "label": "m-9", "end": W("00.0")}], marks={"m-1"})


def test_a_record_without_its_batch_is_an_error():
    with pytest.raises(changelog.ChangeLogError, match="missing 'batch'"):
        changelog.build([{"seq": 1, "wall": W("00.0"), "kind": "created", "path": "a"}], START)


@pytest.mark.parametrize("bad, message", [
    ({"seq": 1, "wall": W("00.0"), "kind": "renamed", "path": "a", "batch": 0}, "unknown kind"),
    ({"seq": 1, "wall": W("00.0"), "kind": "created", "path": "/abs", "batch": 0}, "clean relative path"),
    ({"seq": 1, "kind": "created", "path": "a", "batch": 0}, "missing 'wall'"),
    ({"seq": 1, "wall": "2026-11-21T10:00:00", "kind": "created", "path": "a", "batch": 0}, "without a zone"),
    ({"seq": "1", "wall": W("00.0"), "kind": "created", "path": "a", "batch": 0}, "not an integer"),
    ({"seq": 1, "wall": W("00.0"), "kind": "created", "path": "a", "batch": [1]}, "not an integer"),
    ({"seq": 1, "wall": W("00.0"), "kind": "created", "path": "a", "batch": 0, "content": "maybe"}, "content"),
    ({"seq": 1, "wall": W("00.0"), "kind": "created", "path": "a", "batch": 0, "sha256": "wrong"}, "SHA-256"),
])
def test_fails_loudly(bad, message):
    with pytest.raises(changelog.ChangeLogError, match=message):
        changelog.build([bad], START)


@pytest.mark.parametrize("change, message", [
    (lambda r: r[0].__setitem__("t", True), "integers"),
    (lambda r: next(x for x in r if x["type"] == "metric").__setitem__("reason", "whim"), "reason"),
    (lambda r: next(x for x in r if x["type"] == "change").__setitem__("batch", 99), "no batch record"),
    (lambda r: [r.remove(x) for x in [x for x in r if x["type"] == "metric" and x["batch"] == 1]], "neither a metric reading"),
    (lambda r: next(x for x in r if x["type"] == "metric").__setitem__("batch", [1]), "integers"),
    (lambda r: next(x for x in r if x["type"] == "metric").__setitem__("run", 9), "differ from the schema"),
    (lambda r: r.insert(r.index(next(x for x in r if x["type"] == "metric")) + 1, {"type": "no_metric", "batch": 0, "reason": "not_recorded"}), "exactly one"),
    (lambda r: r.insert(r.index(next(x for x in r if x["type"] == "metric" and x["batch"] == 2)) + 1, {**next(x for x in r if x["type"] == "metric" and x["batch"] == 2), "batch": 1}), "exactly one"),
    (lambda r: next(x for x in r if x["type"] == "batch" and x["batch"] == 3).__setitem__("baseline", True), "only batch 0 is the baseline"),
    (lambda r: r.insert(r.index(next(x for x in r if x["type"] == "batch" and x["batch"] == 1)) + 1,
                        {**next(x for x in r if x["type"] == "change" and x["batch"] == 1), "seq": 99,
                         "t": next(x for x in r if x["type"] == "batch" and x["batch"] == 1)["t"]}), "after that batch closed"),
    (lambda r: next(x for x in r if x["type"] == "mark").__setitem__("mark", 5), "marks are numbered"),
    (lambda r: next(x for x in r if x["type"] == "mark").__setitem__("mark", "m-1"), "neutral number"),
    (lambda r: next(x for x in r if x["type"] == "metric").__setitem__("absent_count", -1), "absent_count"),
])
def test_validator(log, change, message):
    import copy

    bad = copy.deepcopy(log)
    change(bad.records)
    assert any(message in e for e in changelog.validate(bad)), changelog.validate(bad)


def test_partial_last_line_is_left_out(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "events.jsonl").write_text('{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "a", "batch": 0}\n'
                                    '{"seq": 2, "wall": "2026-11-21T10:00:01Z", "kind": "batch", "batch": 0}\n{"seq": 3, "wall": "2026-11-21T10:00')
    assert sum(r["type"] == "change" for r in changelog.build_from_dir(d, START).records) == 1


def test_cli(tmp_path):
    _, seq = real_watcher(1)
    io.write_manifest({"format": "cohbench-run-manifest", "episode_start_utc": seq["start_utc"]}, tmp_path / "manifest.json")
    (tmp_path / "excluded.json").write_text(json.dumps(seq["excluded"]))
    out = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "changelog", str(REAL / "segment-1"), str(REAL / "segment-2"), "--manifest", str(tmp_path / "manifest.json"),
                          "--end-mark", "end-1", "--excluded", str(tmp_path / "excluded.json"), "--out", str(tmp_path / "changes.jsonl")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["changes"] == 27
    assert changelog.read(tmp_path / "changes.jsonl").dumps() == real_log(keep_marks=()).dumps()


# One watcher log per context segment (a controlled reset, D-010) -------------------


def two_logs(drop_b=False):
    g = lambda seq, s, batch, refs: {"seq": seq, "wall": W(s), "kind": "git", "path": ".git", "batch": batch, "head": "ref: refs/heads/main", "refs": refs}  # noqa: E731
    log1 = [f(1, "00.0", "created", "a", 0, sha256="1" * 64), g(2, "00.0", 0, {"refs/heads/main": "x"}), b(3, "00.1", 0),
            f(4, "02.0", "created", "b", 1, sha256="2" * 64), b(5, "02.1", 1)]
    base2 = [f(1, "04.0", "created", "a", 0, sha256="1" * 64)] + ([] if drop_b else [f(2, "04.0", "created", "b", 0, sha256="2" * 64)])
    log2 = base2 + [g(3, "04.0", 0, {"refs/heads/main": "x"}), b(4, "04.1", 0), f(5, "06.0", "created", "c", 1, sha256="3" * 64), b(6, "06.1", 1)]
    log2 = [{**r, "seq": n + 1} for n, r in enumerate(log2)]
    m1 = [{"run": 1, "batch": 1, "reason": "change", "end": W("02.5"), "ok": True, "metrics": {"x": 1}}]
    m2 = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("04.5"), "ok": True, "metrics": {"x": 1}},
          {"run": 2, "batch": 1, "reason": "change", "end": W("06.5"), "ok": True, "metrics": {"x": 2}}]
    return [(log1, m1), (log2, m2)]


HANDOFF = [{"t": 3000, "t_end": 4800, "reason": "handoff"}]


def test_two_watcher_logs_join_into_one_sequence():
    log = changelog.join(two_logs(), START, excluded=HANDOFF)
    assert changelog.validate(log) == []
    assert [(r["seq"], r["path"], r["batch"]) for r in log.records if r["type"] == "change"] == [(1, "a", 0), (2, "b", 1), (3, "c", 2)]
    assert [r["batch"] for r in log.records if r["type"] == "batch"] == [0, 1, 2]
    # The second log's baseline reading is the handoff's own bookkeeping and is dropped.
    assert [(r["batch"], r["metrics"]) for r in log.records if r["type"] == "metric"] == [(1, {"x": 1}), (2, {"x": 2})]
    assert [r["batch"] for r in changelog.join(two_logs(), START).records if r["type"] == "metric"] == [1, 2]
    # On the episode clock: c at 6 s wall is 4.2 s once the 1.8 s handoff is left out.
    assert next(r for r in log.records if r.get("path") == "c")["t"] == 4200


def test_a_baseline_that_differs_from_the_previous_state_fails_naming_the_path():
    with pytest.raises(changelog.ChangeLogError, match=r"\['b'\]"):
        changelog.join(two_logs(drop_b=True), START, excluded=HANDOFF)


def test_skipped_batches_are_numbered_across_the_join():
    logs = two_logs()
    log2, m2 = logs[1]
    # The second watcher's batch 1 is skipped by a later reading of its batch 2.
    log2 = log2 + [{"seq": 7, "wall": W("07.0"), "kind": "created", "path": "d", "batch": 2, "type": "file", "mode": "0644", "sha256": "4" * 64, "content": "stored"},
                   {"seq": 8, "wall": W("07.1"), "kind": "batch", "batch": 2}]
    m2 = [m2[0], {"run": 2, "batch": 2, "reason": "change", "end": W("07.5"), "ok": True, "metrics": {"x": 3}, "skipped": [1]}]
    log = changelog.join([logs[0], (log2, m2)], START, excluded=HANDOFF)
    assert changelog.validate(log) == []
    # The bundle says only that none was taken; the audit record says why.
    assert [(r["batch"], r["reason"]) for r in log.records if r["type"] == "no_metric"] == [(0, "none_taken"), (2, "none_taken")]
    assert {b: d["detail"] for b, d in log.no_reading.items()} == {0: "not_recorded", 2: "skipped"}


# Round 7 ------------------------------------------------------------------------


def test_a_change_dates_from_its_batchs_first_event_and_the_tree_from_its_last():
    """N3: the watcher commits about 100 ms after the write. A created path
    exists from the batch's first event (never before the previous batch's
    commit); the batch's tree is in effect from its last event."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "02.2", "created", "notes.md", 1), b(4, "02.2", 1, first_event=W("02.088"), last_event=W("02.095")),
            f(5, "03.2", "created", "late", 2), b(6, "03.2", 2, first_event=W("01.0"), last_event=W("03.1"))]
    log = changelog.build(recs, START)
    ch = {r["path"]: r["t"] for r in log.records if r["type"] == "change"}
    bt = {r["batch"]: r["t"] for r in log.records if r["type"] == "batch"}
    assert (ch["notes.md"], bt[1]) == (2088, 2095)
    assert ch["late"] == 2200  # clamped to the previous batch's commit
    assert log.batch_at_us(2_090_000) == 0 and log.batch_at_us(2_095_000) == 1
    # Which side of the end a batch falls on: its last event.
    assert [r["path"] for r in changelog.build(recs, START, end_t=2100).records if r["type"] == "change"] == ["a", "notes.md"]


def test_an_unsettled_batch_is_kept_and_flagged():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "01.0", "created", "b", 1), b(4, "01.1", 1, settled=False, unsettled=["b"])]
    log = changelog.build(recs, START)
    assert [(r["batch"], r["settled"]) for r in log.records if r["type"] == "batch"] == [(0, True), (1, False)]
    assert log.unsettled == {1} and changelog.validate(log) == []
    assert "unsettled" not in log.dumps().replace('"settled"', "")


def test_skipped_ranges_and_an_earlier_segments_final_reading():
    """The watcher's skipped batches as inclusive ranges; the join keeps an
    earlier segment's final reading, its skipped list included."""
    logs = two_logs()
    log1, m1 = logs[0]
    log1 = log1 + [f(6, "02.5", "created", "z", 2, sha256="5" * 64), b(7, "02.6", 2)]
    m1 = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "metrics": {"x": 0}},
          {"run": 2, "batch": 2, "reason": "final", "end": W("02.9"), "ok": True, "metrics": {"x": 1}, "skipped": [[1, 1]]}]
    log2 = [{**r, "batch": r["batch"]} for r in logs[1][0]]
    base_extra = [f(0, "04.0", "created", "z", 0, sha256="5" * 64)]
    log2 = [{**r, "seq": n + 1} for n, r in enumerate(log2[:2] + base_extra + log2[2:])]
    log = changelog.join([(log1, m1), (log2, logs[1][1])], START, excluded=HANDOFF)
    assert changelog.validate(log) == []
    assert log.no_reading[1]["detail"] == "skipped" and next(r["reason"] for r in log.records if r["type"] == "no_metric" and r["batch"] == 1) == "none_taken"
    # The shutdown reading measured batch 2's tree, before the next log's baseline did.
    assert next(r["metrics"] for r in log.records if r["type"] == "metric" and r["batch"] == 2) == {"x": 1}


def test_wall_us_at_an_interval_boundary():
    from cohbench.trajectory.clock import Clock

    c = Clock([{"us": 2_000_000, "us_end": 5_000_000, "reason": "probe"}])
    assert c.wall_us(2000) == 2_000_000  # the interval's start, not its end
    assert c.wall_us(2001) == 5_001_000 and c.wall_us(1999) == 1_999_000


def test_a_file_of_unknown_content_is_flagged_and_left_out_of_net_changes():
    """D-032 (A6 of round 8): a file the watcher cannot read is unknown from
    its first record on, through metadata changes and renames, until it is
    deleted. Every change to it carries `content_unknown`, and no net change
    counts it; a change to another path still does."""
    U = {"content": "unreadable", "sha256": None}
    recs = [f(1, "00.0", "created", "a", 0, **U, epoch=1), f(2, "00.0", "created", "k", 0), b(3, "00.1", 0),
            f(4, "01.0", "modified", "a", 1, **U, epoch=2, content_unknown=True), b(5, "01.1", 1),
            f(6, "02.0", "attrib", "a", 2, **U, mode="0600", changed=["mode"]), b(7, "02.1", 2),
            f(8, "03.0", "moved", "b", 3, **U, **{"from": "a"}), b(9, "03.1", 3),
            f(10, "04.0", "modified", "k", 4, changed=["content"]), b(11, "04.1", 4),
            f(12, "05.0", "deleted", "b", 5, **U), b(13, "05.1", 5),
            f(14, "06.0", "created", "b", 6), b(15, "06.1", 6)]
    log = changelog.build(recs, START)
    assert changelog.validate(log) == []
    flags = [(r["kind"], r["path"], r["content_unknown"]) for r in log.records if r["type"] == "change"]
    assert flags == [("created", "a", True), ("created", "k", False), ("modified", "a", True), ("attribute", "a", True), ("moved", "b", True),
                     ("modified", "k", False), ("deleted", "b", True), ("created", "b", False)]
    assert log.unknown(0) == {"a"} and log.unknown(3) == {"b"} and log.unknown(6) == set()
    assert [log.net_change(n) for n in range(7)] == [False, False, False, False, True, False, True]
    # A copy that drops the flag on a later change to the file is refused.
    bad = changelog.ChangeLog(log.header, [dict(r, content_unknown=False) if r["type"] == "change" and r["kind"] == "attribute" else r for r in log.records])
    assert any("content_unknown stays true" in e for e in changelog.validate(bad))
    bad = changelog.ChangeLog(log.header, [dict(r, content_unknown="yes") if r["type"] == "change" else r for r in log.records])
    assert any("content_unknown is a boolean" in e for e in changelog.validate(bad))


def test_an_end_batch_whose_writes_all_came_after_the_end_is_not_the_end_state():
    """B5: post.txt was created 10 ms after the end, its batch closed 20 ms
    after and the end mark came 30 ms after. Every write of that batch is
    later than the end (its first event is), so the end state is the batch
    before it, and the end mark moves there."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "05.0", "created", "b", 1), b(4, "05.1", 1, first_event=W("05.0"), last_event=W("05.0")),
            f(5, "10.01", "created", "post.txt", 2), b(6, "10.02", 2, first_event=W("10.01"), last_event=W("10.01")),
            {"seq": 7, "wall": W("10.03"), "kind": "mark", "label": "end-1", "batch": 2}]
    log = changelog.build(recs, START, end_t=10_000, end_mark="end-1")
    assert changelog.validate(log) == []
    assert "post.txt" not in log.dumps() and set(log.tree(1)) == {"a", "b"}
    assert [(r["batch"], r["mark"]) for r in log.records if r["type"] == "mark"] == [(1, 1)]
    # A batch whose first write came before the end but whose writes went on
    # past it is outside the episode too (R3 of round 9), listed for audit.
    early = [dict(r, first_event=W("09.99")) if r.get("kind") == "batch" and r["batch"] == 2 else r for r in recs]
    cut = changelog.build(early, START, end_t=10_000, end_mark="end-1")
    assert "post.txt" not in cut.dumps() and cut.outside_end == [{"batch": 2, "first_write_us": 9_990_000, "last_write_us": 10_010_000, "straddles": True}]


def test_a_batch_measured_only_by_a_marks_reading_keeps_it():
    """M2: the watcher served a queued mark first and counted its reading as
    measuring the batch, so no change reading of it exists."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "01.0", "created", "b", 1), b(4, "01.1", 1),
            {"seq": 5, "wall": W("01.2"), "kind": "mark", "label": "turn-1", "batch": 1}]
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "metrics": {"n": 0}},
               {"run": 2, "batch": 1, "reason": "mark", "label": "turn-1", "end": W("01.4"), "ok": True, "metrics": {"n": 1}},
               {"run": 3, "batch": 1, "reason": "final", "end": W("02.0"), "ok": True, "metrics": {"n": 1}}]
    log = changelog.build(recs, START, metrics)
    assert changelog.validate(log) == []
    assert [(r["type"], r["batch"], r.get("metrics")) for r in log.records if r["type"] in ("metric", "no_metric")] == [("metric", 0, {"n": 0}), ("metric", 1, {"n": 1})]
    assert log.reading_sources[1]["reason"] == "mark"


def test_the_earliest_successful_reading_wins_over_an_earlier_failure():
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0)]
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": False, "metrics": None},
               {"run": 2, "batch": 0, "reason": "final", "end": W("03.0"), "ok": True, "metrics": {"n": 7}},
               {"run": 3, "batch": 0, "reason": "mark", "label": "m", "end": W("04.0"), "ok": True, "metrics": {"n": 7}}]
    log = changelog.build(recs, START, metrics)
    assert [(r["ok"], r["metrics"]) for r in log.records if r["type"] == "metric"] == [(True, {"n": 7})]
    # A failed reading (here the watcher's copy check) is never the one kept.
    only_failed = changelog.build(recs, START, [{**metrics[0], "error": "internal: copy verification failed: a.txt"}])
    assert [(r["type"], r.get("reason")) for r in only_failed.records if r["type"] in ("metric", "no_metric")] == [("no_metric", "failed")]


def test_a_batch_that_straddles_the_end_is_outside_the_episode():
    """R3 of round 9: its first write before the end, its last after it. The
    bundle cannot date its changes to before the end, so none of them, its
    reading or its paths enter the bundle; the last tree is the batch before
    it, the end mark moves there, and the batch is listed for audit."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "09.99", "created", "b", 1), b(4, "10.06", 1, first_write=W("09.99"), last_write=W("10.04")),
            {"seq": 5, "wall": W("10.07"), "kind": "mark", "label": "end-1", "batch": 1}]
    metrics = [{"run": 1, "batch": 1, "reason": "change", "end": W("10.2"), "ok": True, "metrics": {"n": 2}}]
    log = changelog.build(recs, START, metrics, end_t=10_000, end_mark="end-1")
    assert changelog.validate(log) == []
    assert [r["batch"] for r in log.records if r["type"] == "batch"] == [0]
    assert "\"b\"" not in log.dumps() and not any(r["type"] == "metric" for r in log.records)
    assert [(r["batch"], r["mark"]) for r in log.records if r["type"] == "mark"] == [(0, 1)]
    assert log.outside_end == [{"batch": 1, "first_write_us": 9_990_000, "last_write_us": 10_040_000, "straddles": True}]
    assert "straddles_end" not in log.dumps()
    whole = changelog.build([*recs[:2], f(3, "09.90", "created", "b", 1), b(4, "09.96", 1, first_write=W("09.90"), last_write=W("09.95")), recs[4]],
                            START, end_t=10_000, end_mark="end-1")
    assert [r["batch"] for r in whole.records if r["type"] == "batch"] == [0, 1] and whole.outside_end == []


def test_a_reading_of_an_incomplete_copy_is_not_the_batchs():
    """A3 and D-036: an earlier successful mark reading whose copy lacked
    out/report.txt (a missing blob) loses to a later reading of the whole
    tree. Only what no copy can hold may be absent (the git state, a file
    unreadable in the workspace, an inaccessible directory); a file left out
    for size makes the copy partial. The watcher's own `complete: false`
    decides where it is given. A batch with only incomplete readings has
    none, the reason in the audit."""
    recs = [f(1, "00.0", "created", "secret.bin", 0, content="unreadable", sha256=None), b(2, "00.1", 0),
            f(3, "01.0", "created", "out/report.txt", 1), b(4, "01.1", 1),
            {"seq": 5, "wall": W("01.2"), "kind": "mark", "label": "m-1", "batch": 1}]
    allowed = {".git": "coarse git state", "secret.bin": "file, unreadable"}
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "metrics": {"n": 0}, "absent": allowed},
               {"run": 2, "batch": 1, "reason": "mark", "label": "m-1", "end": W("01.3"), "ok": True, "metrics": {"n": 9},
                "absent": {**allowed, "out/report.txt": "missing blob"}},
               {"run": 3, "batch": 1, "reason": "change", "end": W("01.5"), "ok": True, "metrics": {"n": 1}, "absent": allowed}]
    log = changelog.build(recs, START, metrics)
    assert changelog.validate(log) == []
    assert [(r["batch"], r["metrics"], r["absent"]) for r in log.records if r["type"] == "metric"] == [(0, {"n": 0}, [".git", "secret.bin"]), (1, {"n": 1}, [".git", "secret.bin"])]
    only = changelog.build(recs, START, metrics[:2])
    assert [(r["batch"], r["reason"]) for r in only.records if r["type"] == "no_metric"] == [(1, "incomplete")]
    assert only.no_reading[1] == {"detail": "incomplete", "lacked": ["out/report.txt"]}
    # A file left out for size: not a reading of the tree (D-036).
    big = [f(1, "00.0", "created", "big.bin", 0, content="oversize"), b(2, "00.1", 0)]
    sized = changelog.build(big, START, [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "metrics": {"n": 0},
                                          "absent": {"big.bin": "file, content oversize"}}])
    assert sized.no_reading[0] == {"detail": "incomplete", "lacked": ["big.bin"]}
    # The watcher's own verdict, where it gives one, decides.
    flagged = changelog.build(recs, START, [{**metrics[0], "complete": False}, {**metrics[2], "complete": True, "absent": {"out/x": "?"}}])
    assert [(r["type"], r["batch"]) for r in flagged.records if r["type"] in ("metric", "no_metric")] == [("no_metric", 0), ("metric", 1)]

def test_write_times_come_from_first_write_and_last_write():
    """Watcher round 4: `first_write` and `last_write` count only change
    notifications; a sweep-only batch has both null and falls back to its
    commit time. `first_event` and `last_event` serve older output only."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "01.5", "created", "b", 1), b(4, "02.0", 1, first_event=W("01.0"), last_event=W("01.9"), first_write=W("01.4"), last_write=W("01.6")),
            f(5, "03.0", "modified", "a", 2, changed=["content"]), b(6, "03.5", 2, first_event=W("02.9"), last_event=W("03.1"), first_write=None, last_write=None)]
    log = changelog.build(recs, START)
    at = {(r["type"], r.get("path"), r["batch"]): u for r, u in zip(log.records, log.wall_us)}
    assert at[("change", "b", 1)] == 1_400_000 and at[("batch", None, 1)] == 1_600_000
    assert at[("change", "a", 2)] == at[("batch", None, 2)] == 3_500_000  # sweep-only: the commit time
    assert log.spans[1] == (1_400_000, 1_600_000)


def test_a_mark_the_watcher_did_not_accept_names_no_state():
    """Watcher round 4: `accepted: false` took no reading and resolves no
    boundary: it is never a kept mark, and an end mark so recorded fails."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "01.0", "created", "b", 1), b(4, "01.1", 1),
            {"seq": 5, "wall": W("01.2"), "kind": "mark", "label": "c-1", "batch": 1, "accepted": False},
            {"seq": 6, "wall": W("01.3"), "kind": "mark", "label": "end-1", "batch": 1}]
    log = changelog.build(recs, START, keep_marks={"c-1"}, end_mark="end-1", end_t=1200)
    assert log.mark_ids == {"end-1": 1}
    bad = [*recs[:5], {**recs[5], "accepted": False}]
    with pytest.raises(changelog.ChangeLogError, match="not accepted"):
        changelog.build(bad, START, end_mark="end-1", end_t=1200)


def test_the_watchers_own_unknown_content_flag_is_taken_from_the_record():
    """Watcher round 4 flags every record of an entry whose content is
    unknown, on every name; the change log takes it as given."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "01.0", "attrib", "a", 1, content="unreadable", sha256=None, changed=["mode"], content_unknown=True), b(4, "01.1", 1)]
    log = changelog.build(recs, START)
    assert [r["content_unknown"] for r in log.records if r["type"] == "change"] == [False, True]
    assert log.unknown(1) == {"a"} and not log.net_change(1)


def test_a_mark_names_an_instant_and_the_batch_around_it():
    """D-037: a mark no longer closes a batch. It carries the instant it
    stands for (`at`), the batch in effect there and the batch that
    straddles it; an older log is resolved the same way by time."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            f(3, "01.0", "created", "b", 1), b(4, "02.1", 1, first_write=W("01.0"), last_write=W("02.0")),
            {"seq": 5, "wall": W("02.3"), "kind": "mark", "label": "c-1", "batch": 0, "at": W("01.5"), "straddled": 1, "accepted": True}]
    log = changelog.build(recs, START, keep_marks={"c-1"})
    assert changelog.validate(log) == []
    m = next(r for r in log.records if r["type"] == "mark")
    assert (m["batch"], m["straddled"], m["t"]) == (0, 1, 1500)
    # An older log: no `at` or `straddled`; resolved from the write times.
    old = [*recs[:4], {"seq": 5, "wall": W("01.5"), "kind": "mark", "label": "c-1", "batch": 0}]
    m = next(r for r in changelog.build(old, START, keep_marks={"c-1"}).records if r["type"] == "mark")
    assert (m["batch"], m["straddled"]) == (0, 1)
    bad = changelog.ChangeLog(log.header, [dict(r, straddled=0) if r["type"] == "mark" else r for r in log.records])
    assert any("straddled is null or a batch after" in e for e in changelog.validate(bad))


def test_a_straddled_batch_after_the_reset_keeps_its_global_number():
    """J1 of round 10: segment 1 ends at batch 1; segment 2's mark names its
    local batch 0 in effect and local batch 1 straddling. Across the join
    both numbers go through the same mapping: batch 1 and straddled 2."""
    logs = two_logs()
    log2, m2 = logs[1]
    log2 = [dict(r, first_write=W("05.9"), last_write=W("06.0")) if r["kind"] == "batch" and r["batch"] == 1 else r for r in log2]
    mark = {"seq": len(log2) + 1, "wall": W("06.3"), "kind": "mark", "label": "c-1", "batch": 0, "at": W("05.95"), "straddled": 1, "accepted": True}
    log = changelog.join([logs[0], (log2 + [mark], m2)], START, excluded=HANDOFF, keep_marks={"c-1"})
    assert changelog.validate(log) == []
    m = next(r for r in log.records if r["type"] == "mark")
    assert (m["batch"], m["straddled"]) == (1, 2)
    assert next(r for r in log.records if r["type"] == "change" and r["path"] == "c")["batch"] == 2


def test_a_mark_is_inside_the_episode_by_its_instant():
    """J2 of round 10: a mark standing for 3.4 s, acknowledged at 6.3 s, in
    an episode that ended at 4 s: inside, because its instant is."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "02.9", "created", "b", 1), b(4, "03.0", 1, first_write=W("02.9"), last_write=W("02.9")),
            {"seq": 5, "wall": W("06.3"), "kind": "mark", "label": "c-1", "batch": 1, "at": W("03.4"), "straddled": None, "accepted": True}]
    log = changelog.build(recs, START, keep_marks={"c-1"}, end_t=4000)
    assert [(r["batch"], r["t"]) for r in log.records if r["type"] == "mark"] == [(1, 3400)]
    # Without `at`, the record time decides (an older log).
    old = [*recs[:4], {k: v for k, v in recs[4].items() if k not in ("at", "straddled")}]
    assert not any(r["type"] == "mark" for r in changelog.build(old, START, keep_marks={"c-1"}, end_t=4000).records)


def test_a_rejected_mark_is_an_audit_record():
    """J3 of round 10: the watcher writes `accepted: false, batch: null`. Such
    a mark never fails the build by its shape and names nothing; an end
    mark so recorded still fails where the end mark defines the last tree."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            {"seq": 3, "wall": W("01.0"), "kind": "mark", "label": "c-1", "batch": None, "accepted": False},
            {"seq": 4, "wall": W("01.1"), "kind": "mark", "label": None, "accepted": False}]
    log = changelog.build(recs, START, keep_marks={"c-1"})
    assert changelog.validate(log) == [] and log.mark_ids == {}
    assert [x.get("label") for x in log.rejected_marks] == ["c-1", None]
    with pytest.raises(changelog.ChangeLogError, match="not accepted"):
        changelog.build(recs, START, end_mark="c-1", end_t=2000)


def test_the_watchers_fifth_round_readings():
    """D-036 as the watcher writes it: a reading reused from an identical
    earlier tree is this batch's reading; one added offline counts; a
    `backlog` record (not measured) is no reading; `complete: false` with
    `lacking` makes a reading partial."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0), f(3, "01.0", "created", "b", 1), b(4, "01.1", 1),
            f(5, "02.0", "deleted", "b", 2), b(6, "02.1", 2), f(7, "03.0", "created", "c", 3), b(8, "03.1", 3), f(9, "04.0", "created", "d", 4), b(10, "04.1", 4)]
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "measured": True, "complete": True, "metrics": {"n": 1}, "tree_sha256": "t0"},
               {"run": 2, "batch": 1, "reason": "change", "end": W("01.5"), "ok": True, "measured": True, "complete": False, "lacking": ["b"], "metrics": {"n": 9}},
               {"run": None, "batch": 2, "reason": "change", "end": W("02.5"), "ok": True, "measured": True, "complete": True, "metrics": {"n": 1},
                "reused_from": 0, "reused_run": 1},
               {"run": 3, "batch": 3, "reason": "change", "end": W("09.0"), "ok": True, "measured": True, "complete": True, "metrics": {"n": 2}, "offline": True},
               {"run": None, "batch": 4, "reason": "backlog", "ok": False, "measured": False, "complete": False, "metrics": None}]
    log = changelog.build(recs, START, metrics)
    assert changelog.validate(log) == []
    assert {r["batch"]: r["metrics"] for r in log.records if r["type"] == "metric"} == {0: {"n": 1}, 2: {"n": 1}, 3: {"n": 2}}
    assert {r["batch"]: r["reason"] for r in log.records if r["type"] == "no_metric"} == {1: "incomplete", 4: "none_taken"}
    assert log.no_reading[1] == {"detail": "incomplete", "lacked": ["b"]} and log.no_reading[4] == {"detail": "backlog"}


def test_a_rejected_mark_crosses_the_join_and_a_straddle_names_a_logged_batch():
    """Boundary audit of round 11: a rejected mark (`batch: null`) in the
    second segment passes the join as an audit record; a mark whose
    `straddled` names no batch of the log fails validation."""
    logs = two_logs()
    log2, m2 = logs[1]
    rej = {"seq": len(log2) + 1, "wall": W("06.2"), "kind": "mark", "label": "c-9", "batch": None, "accepted": False}
    log = changelog.join([logs[0], (log2 + [rej], m2)], START, excluded=HANDOFF, keep_marks={"c-9"})
    assert changelog.validate(log) == [] and log.mark_ids == {} and [x["label"] for x in log.rejected_marks] == ["c-9"]
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
            {"seq": 3, "wall": W("00.5"), "kind": "mark", "label": "c-1", "batch": 0, "at": W("00.5"), "straddled": None, "accepted": True}]
    ok = changelog.build(recs, START, keep_marks={"c-1"})
    bad = changelog.ChangeLog(ok.header, [dict(r, straddled=99) if r["type"] == "mark" else r for r in ok.records])
    assert any("straddled batch 99 is not in the log" in e for e in changelog.validate(bad))


def test_a_special_entry_may_be_absent_from_a_complete_reading():
    """M3 of round 11: a fifo (no copy materialises it) is in `absent` of a
    complete reading; that is not a partial copy, on build or on readback."""
    recs = [f(1, "00.0", "created", "a", 0), f(2, "00.0", "created", "work.fifo", 0, type="fifo", content="none", sha256=None), b(3, "00.1", 0)]
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "measured": True, "complete": True,
                "metrics": {"n": 1}, "absent": {"work.fifo": "fifo: not materialised"}},]
    log = changelog.build(recs, START, metrics)
    assert changelog.validate(log) == []
    assert [r["absent"] for r in log.records if r["type"] == "metric"] == [["work.fifo"]]
    old = changelog.build(recs, START, [{k: v for k, v in metrics[0].items() if k not in ("complete", "measured")}])
    assert [r["type"] for r in old.records if r["type"] in ("metric", "no_metric")] == ["metric"]


def test_the_watchers_straddle_rule_from_its_round_6():
    """A batch straddles an instant in [first_write_after, last_write): the
    lower bound, not the first write's read time, opens the span. A batch
    without a recorded lower bound has no span (the watcher's own rule); a
    sweep-only (`none`) batch sits at its commit."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            f(3, "01.0", "created", "b", 1), b(4, "01.2", 1, write_times="known", first_write_after=W("00.8"), first_write=W("01.0"), last_write=W("01.1")),
            f(5, "03.0", "created", "c", 2), b(6, "03.5", 2, write_times="unknown", first_write=None, last_write=None),
            {"seq": 7, "wall": W("00.9"), "kind": "mark", "label": "m-a", "batch": 0},
            {"seq": 8, "wall": W("02.0"), "kind": "mark", "label": "m-b", "batch": 1}]
    log = changelog.build(recs, START, keep_marks={"m-a", "m-b"})
    assert changelog.validate(log) == []
    marks = {r["mark"]: (r["batch"], r["straddled"]) for r in log.records if r["type"] == "mark"}
    # m-a at 0.9 s: after batch 1's lower bound (0.8 s), before its first write's read (1.0 s): straddled.
    # m-b at 2.0 s: after batch 1's last write; the unknown batch records no
    # lower bound, so by the watcher's own rule (replay.answer_for) it has no
    # span and straddles nothing: batch 1 is in effect.
    assert marks == {log.mark_ids["m-a"]: (0, 1), log.mark_ids["m-b"]: (1, None)}
    assert log.spans[1] == (800_000, 1_100_000) and log.spans[2] == (3_500_000, 3_500_000) and log.spans[0] == (100_000, 100_000)


def test_a_complete_reading_with_a_capped_absent_list_is_kept():
    """T2 of round 12: 1,001 fifos, `complete: true`, 1,000 paths shown and
    `absent_count: 1001`. The watcher's `complete` is the evidence; the
    capped display is not a cut copy, and each shown path is still checked."""
    recs = [f(n + 1, "00.0", "created", f"p/f{n:04}", 0, type="fifo", content="none", sha256=None) for n in range(1001)]
    recs.append(b(1002, "00.1", 0))
    shown = {f"p/f{n:04}": "fifo: not materialised" for n in range(1000)}
    metrics = [{"run": 1, "batch": 0, "reason": "baseline", "end": W("00.5"), "ok": True, "measured": True, "complete": True,
                "metrics": {"n": 0}, "absent": shown, "absent_count": 1001}]
    log = changelog.build(recs, START, metrics)
    assert [r["absent_count"] for r in log.records if r["type"] == "metric"] == [1001]
    assert changelog.validate(log) == []
    bad = changelog.ChangeLog(log.header, [dict(r, absent=r["absent"][:-1] + ["p/readable.txt"]) if r["type"] == "metric" else r for r in log.records])
    assert any("a copy of batch 0" in e for e in changelog.validate(bad))


@pytest.mark.parametrize("kind", ["known", "none", "unknown"])
def test_resolving_an_instant_agrees_with_the_watchers_mark(kind):
    """T1 of round 12: with the watcher's recorded `first_write_after`, an
    instant resolved here gives the watcher's answer for every kind of batch:
    straddled from that bound up to the batch's last write (`known`) or its
    commit (`none`, `unknown`), never the previous batch's last write. Also
    across a segment join."""
    def batch1(**k):
        times = {"first_write": W("01.3"), "last_write": W("01.4")} if kind == "known" else {"first_write": None, "last_write": None}
        return b(4, "01.6", 1, write_times=kind, first_write_after=W("01.2"), **times, **k)
    upper = 1_400_000 if kind == "known" else 1_600_000
    # The watcher's rule, strict at both ends: at the lower bound (1.2 s) the
    # batch's writes all came after; at its last write (or commit) it is in effect.
    for at, want in ((1.15, None), (1.2, None), (1.21, 1), (1.35, 1), (upper / 1e6 - 0.001, 1), (upper / 1e6, None)):
        recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
                f(3, "01.3", "created", "b", 1), batch1(),
                {"seq": 5, "wall": W("02.0"), "kind": "mark", "label": "m", "batch": 0, "at": W(f"{at:06.3f}")}]
        log = changelog.build(recs, START, keep_marks={"m"})
        m = next(r for r in log.records if r["type"] == "mark")
        # A mark without `straddled` is resolved here; the watcher's rule says `want`.
        assert m["straddled"] == want, (kind, at, m)
        assert log.spans[1] == (1_200_000, upper)
    # Across the join: the second segment's batch keeps its own lower bound.
    logs = two_logs()
    log2, m2 = logs[1]
    log2 = [dict(r, write_times=kind, first_write_after=W("05.7"), **({"first_write": W("05.9"), "last_write": W("06.0")} if kind == "known" else {"first_write": None, "last_write": None}))
            if r["kind"] == "batch" and r["batch"] == 1 else r for r in log2]
    for at, want in (("05.700", None), ("05.800", 2)):
        mark = {"seq": len(log2) + 1, "wall": W("06.3"), "kind": "mark", "label": "c-1", "batch": 0, "at": W(at)}
        log = changelog.join([logs[0], (log2 + [mark], m2)], START, excluded=HANDOFF, keep_marks={"c-1"})
        assert next(r["straddled"] for r in log.records if r["type"] == "mark") == want, at


def test_a_null_git_head_is_read():
    """The watcher's last round: `git.head` is null when it points into an
    ignored namespace."""
    recs = [f(1, "00.0", "created", "a", 0), {"seq": 2, "wall": W("00.0"), "kind": "git", "path": ".git", "batch": 0, "head": None, "refs": {"refs/heads/main": "x"}},
            b(3, "00.1", 0)]
    log = changelog.build(recs, START)
    assert changelog.validate(log) == [] and next(r["head"] for r in log.records if r["type"] == "git") is None


def test_a_run_of_batches_can_straddle_one_instant():
    """The watcher's last pass: honest write ranges can overlap, so a run of
    batches straddles an instant; `straddled` is the first, `straddled_last`
    the last. The watcher's answer is carried; an older mark is resolved the
    same way; readback checks the pair."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            f(3, "01.0", "created", "b", 1), b(4, "01.6", 1, write_times="known", first_write_after=W("00.9"), first_write=W("01.0"), last_write=W("01.5")),
            f(5, "01.4", "created", "c", 2), b(6, "01.9", 2, write_times="known", first_write_after=W("01.1"), first_write=W("01.4"), last_write=W("01.8"))]
    told = {"seq": 7, "wall": W("02.0"), "kind": "mark", "label": "c-1", "batch": 0, "at": W("01.2"), "straddled": 1, "straddled_last": 2, "accepted": True}
    log = changelog.build([*recs, told], START, keep_marks={"c-1"})
    assert changelog.validate(log) == []
    assert next((r["batch"], r["straddled"], r["straddled_last"]) for r in log.records if r["type"] == "mark") == (0, 1, 2)
    old = {k: v for k, v in told.items() if k not in ("straddled", "straddled_last")}
    log = changelog.build([*recs, old], START, keep_marks={"c-1"})
    assert next((r["straddled"], r["straddled_last"]) for r in log.records if r["type"] == "mark") == (1, 2)
    # The watcher's own rule gives the same answer.
    from cohbench.watcher.replay import answer_for

    bounds = {0: (0.1, 0.1), 1: (0.9, 1.5), 2: (1.1, 1.8)}
    assert answer_for(1.2, bounds) == (0, 1, 2)
    bad = changelog.ChangeLog(log.header, [dict(r, straddled_last=None) if r["type"] == "mark" else r for r in log.records])
    assert any("both null, or a run" in e for e in changelog.validate(bad))


def test_a_sweep_only_batch_row_and_its_marks_share_one_timestamp():
    """The checker's case at commit aea3e81: a sweep-only batch's row said
    1.600 s while the live upper bound was 1.602 s, and a mark at 1.601 s was
    answered with that batch straddling. Against the watcher's current code:
    its replay rule (answer_for) over the row's own times names that batch in
    effect at 1.601 s, and the change log, resolving the instant itself from
    the same row, agrees."""
    from datetime import datetime, timezone

    from cohbench.watcher.replay import answer_for

    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            f(3, "01.0", "created", "b", 1), b(4, "01.2", 1, write_times="known", first_write_after=W("00.9"), first_write=W("01.0"), last_write=W("01.1")),
            f(5, "01.5", "modified", "a", 2, changed=["content"]), b(6, "01.600", 2, write_times="none", first_write_after=W("01.300"), first_write=None, last_write=None),
            {"seq": 7, "wall": W("01.700"), "kind": "mark", "label": "m", "batch": 0, "at": W("01.601")}]
    log = changelog.build(recs, START, keep_marks={"m"})
    m = next(r for r in log.records if r["type"] == "mark")
    t = lambda s: datetime.fromisoformat(W(s).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()  # noqa: E731
    bounds = {0: (t("00.1"), t("00.1")), 1: (t("00.9"), t("01.1")), 2: (t("01.300"), t("01.600"))}
    assert answer_for(t("01.601"), bounds) == (2, None, None)
    assert (m["straddled"], m["straddled_last"]) == (None, None) and log.batch_at_us(1_601_000) == 2


def test_every_batch_carries_its_first_write():
    """D-042's input: `t_first` on every batch record, the batch's first
    write, so batches can be joined by their writes whatever the watcher
    netted away. A file written at 2.95 s and again at 3.4 s is one change
    record and the batch's first write is still 2.95 s; a file created and
    deleted in one batch leaves no change record and its batch keeps its
    first write. The baseline, a sweep-only batch and an `unknown` one have
    none. `t` is the last write (the commit with no write times), so
    `[t_first, t]` is the write span."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            f(3, "03.4", "modified", "a", 1, changed=["content"]),
            b(4, "03.6", 1, write_times="known", first_write_after=W("02.9"), first_write=W("02.95"), last_write=W("03.4")),
            b(5, "05.2", 2, write_times="known", first_write_after=W("04.8"), first_write=W("04.9"), last_write=W("05.0")),
            f(6, "07.0", "created", "c", 3), b(7, "07.1", 3, write_times="none", first_write=None, last_write=None),
            f(8, "08.0", "created", "d", 4), b(9, "08.5", 4, write_times="unknown", first_write=None, last_write=None)]
    log = changelog.build(recs, START)
    assert changelog.validate(log) == []
    spans = {r["batch"]: (r["t_first"], r["t"]) for r in log.records if r["type"] == "batch"}
    assert spans == {0: (None, 100), 1: (2950, 3400), 2: (4900, 5000), 3: (None, 7100), 4: (None, 8500)}
    assert [r["t"] for r in log.records if r["type"] == "change" and r["batch"] == 1] == [2950]
    # Older output without `write_times`: the first write it records, if any.
    old = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0),
           f(3, "01.5", "created", "b", 1), b(4, "02.0", 1, first_write=W("01.4"), last_write=W("01.6")),
           f(5, "03.0", "modified", "a", 2, changed=["content"]), b(6, "03.5", 2, first_write=None, last_write=None)]
    assert [(r["t_first"], r["t"]) for r in changelog.build(old, START).records if r["type"] == "batch"] == [(None, 100), (1400, 1600), (None, 3500)]


def test_the_first_write_is_checked_and_optional_on_readback():
    """`t_first` is null or an integer from the previous batch's `t` to its
    own; the baseline's is null. A change log from before the field reads
    back without it on every batch, never on some."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            f(3, "01.0", "created", "b", 1), b(4, "01.2", 1, write_times="known", first_write_after=W("00.8"), first_write=W("01.0"), last_write=W("01.1")),
            f(5, "02.0", "created", "c", 2), b(6, "02.2", 2, write_times="known", first_write_after=W("01.9"), first_write=W("02.0"), last_write=W("02.1"))]
    log = changelog.build(recs, START)
    assert changelog.validate(log) == []

    def edited(fn):
        return changelog.validate(changelog.ChangeLog(log.header, [fn(r) if r["type"] == "batch" else r for r in log.records]))

    assert edited(lambda r: {k: v for k, v in r.items() if k != "t_first"}) == []
    assert any("without t_first" in e for e in edited(lambda r: {k: v for k, v in r.items() if k != "t_first"} if r["batch"] == 1 else r))
    assert any("t_first is null or an integer" in e for e in edited(lambda r: dict(r, t_first=r["t"] + 1) if r["batch"] == 2 else r))
    assert any("t_first is null or an integer" in e for e in edited(lambda r: dict(r, t_first=1000) if r["batch"] == 2 else r))
    assert any("t_first is null or an integer" in e for e in edited(lambda r: dict(r, t_first="1000") if r["batch"] == 1 else r))
    assert any("the baseline has no first write" in e for e in edited(lambda r: dict(r, t_first=0) if r["batch"] == 0 else r))


def test_a_first_write_inside_an_excluded_interval_counts_at_its_start():
    """The episode clock stands still inside an excluded interval: a batch
    whose netted-away first write fell inside one has `t_first` at the
    interval's start (2,000 ms on the episode clock, as its end is)."""
    recs = [f(1, "00.0", "created", "a", 0), b(2, "00.1", 0, write_times="none"),
            b(3, "05.8", 1, write_times="known", first_write_after=W("03.9"), first_write=W("04.0"), last_write=W("05.5"))]
    log = changelog.build(recs, START, excluded=[{"t": 2000, "t_end": 5000, "reason": "handoff"}])
    assert changelog.validate(log) == []
    assert [(r["t_first"], r["t"]) for r in log.records if r["type"] == "batch"] == [(None, 100), (2000, 2500)]
