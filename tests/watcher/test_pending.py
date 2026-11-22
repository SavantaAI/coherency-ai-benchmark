from cohbench.watcher.pending import Pending

Q, MAX = 0.1, 2.0


def test_quiet_then_due():
    p = Pending(Q, MAX)
    assert p.deadline() is None and not p.due(5.0)
    p.touch("f", 1.0, 0, modify=True)
    p.touch("f", 1.05, 0, close_write=True)
    assert not p.due(1.1)
    assert p.due(1.16)
    t = p.take(1.16)
    assert t.dirty == {"f"} and t.final_writes() == {"f"} and not t.forced
    assert not p


def test_open_writer_holds_until_close_or_max_delay():
    p = Pending(Q, MAX)
    p.touch("f", 1.0, 0, modify=True)
    assert abs(p.deadline() - 3.0) < 1e-9
    assert not p.due(2.9)
    assert p.due(3.0)
    assert p.take(3.0).forced


def test_deadline_never_in_the_past_without_being_due():
    p = Pending(Q, MAX)
    p.touch("d", 1.0, 0)
    for i in range(50):
        p.touch(f"d/c{i}", 1.0 + i * 0.01, 0)
    d = p.deadline()
    assert p.due(d)  # the moment the deadline passes, the batch is due: no spin


def test_continuous_activity_is_cut_at_max_delay():
    p = Pending(Q, MAX)
    t = 1.0
    while t < 3.5:
        p.touch("log", t, 0, modify=True, close_write=True)
        t += 0.05
        if p.due(t):
            break
    assert t <= 3.05


def test_renames_are_kept_in_order_and_carry_open_writes():
    p = Pending(Q, MAX)
    p.touch("a", 1.0, 0, modify=True)
    p.rename("a", "b", 1.01, 0)
    p.rename("b", "c", 1.02, 0)
    assert p.renames == [("a", "b"), ("b", "c")]
    assert set(p.open_writes) == {"c"}
    p.touch("c", 1.03, 0, close_write=True)
    assert p.due(1.2)


def test_silent_open_writer_stops_holding_batches():
    p = Pending(Q, MAX)
    p.touch("f", 1.0, 0, modify=True)
    p.take(3.0)  # still open, carried over
    p.touch("g", 3.5, 0)
    # f's last write is more than max_delay old by now, so quiet applies.
    assert p.due(3.61)


def test_writes_follow_only_later_renames():
    p = Pending(Q, MAX)
    p.rename("a", "b", 1.0, 0)
    p.rename("b", "a", 1.01, 0)
    p.touch("b", 1.02, 0, modify=True)  # a new b, written after both renames
    p.touch("a", 1.0, 0)
    p.rename("a", "c", 1.03, 0)
    t = p.take(2.0)
    assert t.final_writes() == {"b"}


def test_merge_offsets_write_positions():
    p = Pending(Q, MAX)
    p.rename("x", "y", 1.0, 0)
    t = p.take(2.0)
    p.touch("y", 2.1, 0, modify=True)
    p.rename("y", "z", 2.2, 0)
    t.merge(p.take(3.0))
    assert t.renames == [("x", "y"), ("y", "z")] and t.final_writes() == {"z"}
