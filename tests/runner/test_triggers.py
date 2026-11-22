"""Each trigger, against a watcher output directory in the watcher's format."""

from __future__ import annotations

from pathlib import Path

import pytest

from cohbench.runner import triggers
from cohbench.runner.triggers import Context, holds
from cohbench.runner.worklog import WorkspaceLog, glob_match, glob_problem

from .fake import FakeClock, FakeWatcher


@pytest.fixture
def w(tmp_path: Path) -> tuple[FakeWatcher, Context]:
    fw = FakeWatcher(tmp_path / "seg1", {"README.md": "hello\n", "src/app.py": "x = 1\n"}, FakeClock())
    ctx = Context()
    ctx.work.refresh([fw.root])
    return fw, ctx


def refresh(fw: FakeWatcher, ctx: Context) -> None:
    ctx.work.refresh([fw.root])


def fire(ctx: Context, name: str, fw: FakeWatcher) -> None:
    b, _ = fw.mark(f"mk-{name}")
    ctx.work.refresh([fw.root])
    ctx.fired[name] = ctx.snapshot(ctx.elapsed_ms, ctx.work.marks[f"mk-{name}"])
    assert ctx.work.marks[f"mk-{name}"] == (0, b)


@pytest.mark.parametrize("pattern, path, ok", [
    ("out/*.log", "out/a.log", True), ("out/*.log", "out/x/a.log", False), ("out/**", "out/x/a.log", True),
    ("**/*.log", "a.log", True), ("**/*.log", "x/y/a.log", True), ("data/batch?.csv", "data/batch3.csv", True),
    ("data/batch[12].csv", "data/batch3.csv", False), ("data/batch[!12].csv", "data/batch3.csv", True), ("*.md", "docs/a.md", False),
])
def test_glob(pattern: str, path: str, ok: bool) -> None:
    assert glob_match(pattern, path) is ok


def test_glob_problems() -> None:
    """B9, N1.9: a bad class is refused at validation, not at evaluation."""
    assert glob_problem("data/[z-a].csv") is not None
    assert glob_problem("/abs") and glob_problem("a//b")
    assert glob_problem("out/**/x[0-9].log") is None


def test_baseline_is_state_not_change(w) -> None:
    fw, ctx = w
    assert set(ctx.work.files()) == {"README.md", "src/app.py"}
    assert ctx.work.changes == []
    assert holds({"path_exists": "src/*.py"}, ctx)
    assert not holds({"changes": {"path": "**", "at_least": 1}}, ctx)


def test_only_committed_batches_count(tmp_path: Path) -> None:
    """B7: a record is visible once its batch record has been read."""
    c = FakeClock()
    fw = FakeWatcher(tmp_path / "s", {}, c, commit_delay_s=5)
    log = WorkspaceLog()
    fw.write("out/a.log", "x")
    log.refresh([fw.root])
    assert "out/a.log" not in log.files() and log.changes == []
    c.advance(5)
    fw.tick()
    log.refresh([fw.root])
    assert "out/a.log" in log.files() and len(log.changes) == 1


def test_partial_line_waits(tmp_path: Path) -> None:
    fw = FakeWatcher(tmp_path / "s", {}, FakeClock())
    with open(fw.root / "events.jsonl", "a") as fh:
        fh.write('{"seq": 99, "batch": 1, "kind": "created", "path": "half"')
    log = WorkspaceLog()
    log.refresh([fw.root])
    with open(fw.root / "events.jsonl", "a") as fh:
        fh.write(', "type": "file", "sha256": null, "content": "none"}\n')
    log.refresh([fw.root])
    assert "half" not in log.files()  # complete now, but batch 1 is not committed
    fw._close("settled")
    log.refresh([fw.root])
    assert "half" in log.files()


def test_path_and_content_multiline(w) -> None:
    """N1.7: `^` matches at a line start."""
    fw, ctx = w
    fw.write("out/job-1.log", "job id: job-123456\nCONFLICT: two jobs\n")
    refresh(fw, ctx)
    assert holds({"path_exists": "out/*.log"}, ctx)
    assert holds({"content_matches": {"path": "out/*.log", "regex": "^CONFLICT"}}, ctx)
    assert not holds({"content_matches": {"path": "out/*.log", "regex": "^two"}}, ctx)


def test_changes_count_once_and_since(w) -> None:
    fw, ctx = w
    for n in range(3):
        fw.write(f"out/{n}.txt", "x")
    fw.write("src/app.py", "x = 2\n")
    refresh(fw, ctx)
    assert holds({"changes": {"path": "out/**", "at_least": 3}}, ctx)
    assert not holds({"changes": {"path": "out/**", "at_least": 4}}, ctx)
    since = {"changes": {"path": "**", "at_least": 1, "since": "e"}}
    assert not holds(since, ctx)
    fire(ctx, "e", fw)
    assert not holds(since, ctx)
    fw.write("README.md", "changed\n")
    refresh(fw, ctx)
    assert holds(since, ctx)


def test_a_move_counts_once(tmp_path: Path) -> None:
    """B7: both ends match, one record."""
    fw = FakeWatcher(tmp_path / "s", {"out/a.log": "1"}, FakeClock())
    fw._rec({"kind": "moved", "from": "out/a.log", "path": "out/b.log", "type": "file"})
    fw._close("settled")
    log = WorkspaceLog()
    log.refresh([fw.root])
    ctx = Context(work=log)
    assert set(log.files()) == {"out/b.log"}
    assert holds({"changes": {"path": "out/*.log", "at_least": 1}}, ctx)
    assert not holds({"changes": {"path": "out/*.log", "at_least": 2}}, ctx)


def test_moves_and_deletes(tmp_path: Path) -> None:
    fw = FakeWatcher(tmp_path / "s", {"a/x.txt": "1", "a/y.txt": "2"}, FakeClock())
    fw._rec({"kind": "moved", "from": "a", "path": "b", "type": "dir"})
    fw._rec({"kind": "deleted", "path": "b/y.txt"})
    fw._close("settled")
    log = WorkspaceLog()
    log.refresh([fw.root])
    assert set(log.files()) == {"b", "b/x.txt"}
    assert [c.paths for c in log.changes] == [("a", "b"), ("b/y.txt",)]


def test_metric_readings_by_batch(w) -> None:
    """B7, N1.5: batch 0 and the event's own mark reading do not count after it."""
    fw, ctx = w
    fw.metric({"ops": 100}, reason="baseline", batch=0)
    fw.metric({"ops": 50}, reason="mark", batch=0, label="x")
    refresh(fw, ctx)
    assert not holds({"metric": {"name": "ops", "op": "lt", "value": 100, "reading": "any"}}, ctx)
    fw.write("a", "1")
    fw.metric({"ops": 90})
    fw.write("b", "1")
    fw.metric({"ops": 120})
    refresh(fw, ctx)
    assert not holds({"metric": {"name": "ops", "op": "lt", "value": 100}}, ctx)  # latest is 120
    assert holds({"metric": {"name": "ops", "op": "lt", "value": 100, "reading": "any"}}, ctx)
    fw.has_metric = True
    fire(ctx, "e", fw)  # its mark writes a reading of 120 for the mark's batch
    assert not holds({"metric": {"name": "ops", "op": "ge", "value": 0, "reading": "any", "since": "e"}}, ctx)
    fw.write("c", "1")
    fw.metric({"ops": 80})
    refresh(fw, ctx)
    assert holds({"metric": {"name": "ops", "op": "le", "value": 80, "since": "e"}}, ctx)


def test_segments_order_positions(tmp_path: Path) -> None:
    c = FakeClock()
    a = FakeWatcher(tmp_path / "s1", {}, c)
    a.write("x", "1")
    ctx = Context()
    ctx.work.refresh([a.root])
    lab, _ = a.mark("m")
    ctx.work.refresh([a.root])
    ctx.fired["e"] = ctx.snapshot(0, ctx.work.marks["m"])
    b = FakeWatcher(tmp_path / "s2", a.tree, c)
    b.write("y", "1")
    ctx.work.refresh([a.root, b.root])
    assert lab == 1 and ctx.work.marks["m"] == (0, 1)  # nothing changed since batch 1: the mark lands on it, as the real watcher does
    assert holds({"changes": {"path": "*", "at_least": 1, "since": "e"}}, ctx)
    assert set(ctx.work.files()) == {"x", "y"}


def test_blob_read_failures_are_not_cached(w) -> None:
    """N1.11."""
    fw, ctx = w
    fw.write("out/a.log", "CONFLICT\n")
    refresh(fw, ctx)
    sha = ctx.work.files()["out/a.log"].sha256
    blob = fw.root / "blobs" / sha[:2] / sha
    data = blob.read_bytes()
    blob.unlink()
    assert ctx.work.content("out/a.log") is None
    blob.write_bytes(data)
    assert ctx.work.content("out/a.log") == "CONFLICT\n"


def test_counters_and_combinators() -> None:
    ctx = Context(turns=2, handbacks=3, outputs=5, elapsed_ms=60_000)
    ctx.verdicts["unanswerable"] = 2
    assert holds({"turns": {"at_least": 2}}, ctx) and not holds({"turns": {"at_least": 3}}, ctx)
    assert holds({"handbacks": {"at_least": 3}}, ctx)
    assert holds({"outputs": {"at_least": 5}}, ctx)
    assert holds({"elapsed_ms": {"at_least": 60_000}}, ctx)
    assert holds({"verdicts": {"label": "unanswerable", "at_least": 2}}, ctx)
    ctx.fired["x"] = ctx.snapshot(60_000, (0, 0))
    ctx.turns, ctx.handbacks, ctx.elapsed_ms = 3, 4, 90_000
    assert holds({"turns": {"at_least": 1, "since": "x"}}, ctx) and not holds({"turns": {"at_least": 2, "since": "x"}}, ctx)
    assert holds({"handbacks": {"at_least": 1, "since": "x"}}, ctx)
    assert holds({"elapsed_ms": {"at_least": 30_000, "since": "x"}}, ctx)
    assert holds({"all": [{"fired": "x"}, {"not": {"turns": {"at_least": 9}}}]}, ctx)
    assert not holds({"any": [{"turns": {"at_least": 9}}, {"not": {"fired": "x"}}]}, ctx)


@pytest.mark.parametrize("t, expected", [
    ({"fired": ["a"]}, "an event name"),
    ({"turns": {"at_least": 1, "since": {"x": 1}}}, "since: an event name"),
    ({"metric": {"name": "m", "op": ["lt"], "value": 1}}, "op: one of"),
    ({"metric": {"name": "m", "op": "lt", "value": 1, "reading": ["any"]}}, "reading: latest or any"),
    ({"verdicts": {"label": ["x"], "at_least": 1}}, "label: one of"),
    ({"path_exists": "data/[z-a].csv"}, "not a valid pattern"),
    ({"content_matches": {"path": 3, "regex": "x"}}, "path: a relative glob"),
    ([], "exactly one key"),
])
def test_check_never_raises(t, expected: str) -> None:
    """B9."""
    err: list[str] = []
    triggers.check(t, "t", {"a"}, err, set())
    assert any(expected in x for x in err), err


def test_metric_trigger_needs_a_metric() -> None:
    """N2.6."""
    err: list[str] = []
    triggers.check({"metric": {"name": "m", "op": "lt", "value": 1}}, "t", set(), err, set(), has_metric=False)
    assert any("no metric command" in x for x in err)


def test_check_collects_dependencies() -> None:
    err: list[str] = []
    deps: set[str] = set()
    triggers.check({"all": [{"fired": "a"}, {"turns": {"at_least": 1, "since": "b"}}]}, "t", {"a", "b"}, err, deps)
    assert err == [] and deps == {"a", "b"}
    assert triggers.depends_on({"all": [{"fired": "a"}, {"handbacks": {"at_least": 1, "since": "b"}}]}) == {"a", "b"}
