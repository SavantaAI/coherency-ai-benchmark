"""delta.plan, delta.resync and the strict replay rules, on hand-made manifests."""

from __future__ import annotations

import pytest

from cohbench.watcher.delta import check, plan, resync
from cohbench.watcher.manifest import Entry, Manifest, ReplayError, apply_record


def F(ino, sha="s", mode=0o644):
    return Entry(type="file", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=1, sha256=sha * 64 if len(sha) == 1 else sha, content="stored")


def D(ino, mode=0o755):
    return Entry(type="dir", mode=mode, uid=1000, gid=1000, dev=1, ino=ino, size=None, sha256=None, content="none")


def L(ino, target):
    return Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=ino, size=len(target), sha256="c" * 64, content="stored", target=target)


def M(**kw):
    return Manifest.of({k.replace("__", "/"): v for k, v in kw.items()})


def run(old, new, renames=(), written=()):
    recs = plan(old, new, list(renames), set(written))
    assert recs is not None, "no legal order"
    err = check(old, new, recs)
    assert err is None, err
    return [(r["kind"], r.get("from"), r["path"]) if r["kind"] == "moved" else (r["kind"], r["path"]) for r in recs]


def test_rm_rf_dst_then_mv_src_dst():
    old = M(src=D(1), src__c=F(2), dst=D(3), dst__child=F(4))
    new = M(dst=D(1), dst__c=F(2))
    assert run(old, new, [("src", "dst")]) == [("deleted", "dst/child"), ("deleted", "dst"), ("moved", "src", "dst")]


def test_mkdir_then_mv_into_it():
    old = M(a=F(5))
    new = M(d=D(6), d__b=F(5))
    assert run(old, new, [("a", "d/b")]) == [("created", "d"), ("moved", "a", "d/b")]


def test_rm_rf_mkdir_mv_into_recreated_directory():
    old = M(d=D(1), d__old=F(2), a=F(5))
    new = M(d=D(7), d__b=F(5))
    assert run(old, new, [("a", "d/b")]) == [("deleted", "d/old"), ("deleted", "d"), ("created", "d"), ("moved", "a", "d/b")]


def test_two_renames_onto_one_name():
    old = M(a=F(1, "a"), c=F(2, "c"))
    new = M(b=F(2, "c"))
    assert run(old, new, [("a", "b"), ("c", "b")]) == [("deleted", "a"), ("moved", "c", "b")]


def test_rename_chain_with_a_write():
    old = M(a=F(1, "a"))
    new = M(c=F(1, "z"))
    assert run(old, new, [("a", "b"), ("b", "c")]) == [("moved", "a", "c"), ("modified", "c")]


def test_new_file_in_moved_directory():
    old = M(d=D(1), d__sub=D(2))
    new = M(e=D(1), e__sub=D(2), e__sub__new=F(3))
    assert run(old, new, [("d", "e")]) == [("moved", "d", "e"), ("created", "e/sub/new")]


def test_creations_are_shallowest_first_regardless_of_name():
    old = M()
    new = M(z=F(1), a=F(2), m=D(3), m__x=F(4))
    assert run(old, new) == [("created", "a"), ("created", "m"), ("created", "z"), ("created", "m/x")]


def test_identity_without_rename_is_delete_and_create():
    old = M(a=F(1))
    new = M(b=F(1))
    assert run(old, new) == [("deleted", "a"), ("created", "b")]


def test_hard_link_moved():
    old = M(a=F(1), b=F(1))
    new = M(a=F(1), c=F(1))
    assert run(old, new, [("b", "c")]) == [("moved", "b", "c")]


def test_type_changes():
    old = M(f=F(1), d=D(2), d__x=F(3), g=F(4))
    new = M(f=L(5, "t"), d=F(6), g=D(7))
    got = run(old, new)
    assert ("attrib", "f") in got
    assert got.index(("deleted", "d/x")) < got.index(("deleted", "d")) < got.index(("created", "d"))
    assert ("deleted", "g") in got and ("created", "g") in got


def test_rewritten_only_when_written_and_unchanged():
    old = M(f=F(1, "a"), g=F(2, "b"))
    new = M(f=F(1, "a"), g=F(2, "b"))
    assert run(old, new, written={"f"}) == [("rewritten", "f")]
    assert run(old, new) == []


def test_swap_cycle_cannot_be_ordered_and_resync_handles_it():
    old = M(a=F(1, "a"), b=F(2, "b"))
    new = M(a=F(2, "b"), b=F(1, "a"))
    recs = plan(old, new, [("a", "t"), ("b", "a"), ("t", "b")])
    if recs is not None:
        assert check(old, new, recs) is None
    rs = resync(old, new)
    assert check(old, new, rs) is None
    assert {r["kind"] for r in rs} == {"deleted", "created"}


def test_git_changes_are_records():
    old = Manifest.of({}, git={"head": "ref: refs/heads/main", "refs": {}, "backend": "files"})
    new = Manifest.of({}, git={"head": "ref: refs/heads/main", "refs": {"refs/heads/main": "a" * 40}, "backend": "files"})
    assert run(old, new) == [("git", ".git")]


def rec(kind, path, e=None, **kw):
    r = {"kind": kind, "path": path, "seq": 1}
    if e is not None:
        r.update(e.fields())
    r.update(kw)
    return r


@pytest.mark.parametrize(
    "start,bad",
    [
        ({}, rec("created", "d/x", F(1))),  # parent missing
        ({"d": D(1), "d/x": F(2)}, rec("deleted", "d", D(1))),  # still has children
        ({"a": F(1)}, rec("created", "a", F(2))),  # exists
        ({"a": F(1), "b": F(2)}, rec("moved", "b", F(1), **{"from": "a"})),  # onto existing
        ({"a": F(1)}, rec("moved", "b", F(9), **{"from": "a"})),  # identity changes
        ({"d": D(1)}, rec("moved", "d/e", D(1), **{"from": "d"})),  # into itself
        ({"a": F(1)}, rec("modified", "a", D(1))),  # file to dir in place
        ({"a": F(1, "a")}, rec("rewritten", "a", F(1, "b"))),  # rewritten with a change
        ({}, rec("modified", "nope", F(1))),
        ({}, rec("frobbed", "x", F(1))),
        ({}, rec("created", "../x", F(1))),
    ],
)
def test_apply_record_is_strict(start, bad):
    m = Manifest.of(start)
    with pytest.raises(ReplayError):
        apply_record(m, bad)
