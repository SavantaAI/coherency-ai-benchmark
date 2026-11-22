"""state_at and diff over hand-written logs."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from cohbench.watcher import ReplayError, diff, mark_batch, state_at
from cohbench.watcher.blobs import BlobStore
from cohbench.watcher.manifest import Entry
from cohbench.watcher.replay import render


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Log:
    def __init__(self, out):
        self.out = str(out)
        os.makedirs(self.out, exist_ok=True)
        self.blobs = BlobStore(self.out)
        self.lines: list[dict] = []
        self.seq = 0
        self.batch = 0
        self.open: list[dict] = []
        self.ino = 100

    def entry(self, type="file", data=None, mode=0o644, content=None, ino=None, uid=1000, target=None):
        if ino is None:
            self.ino += 1
            ino = self.ino
        e = Entry(type=type, mode=mode, uid=uid, gid=1000, dev=1, ino=ino, size=None if data is None else len(data), sha256=None if data is None else sha(data), content=content or ("stored" if data is not None else "none"), target=target)
        if data is not None and e.content == "stored":
            self.blobs.put(sha(data), data)
        return e

    def add(self, kind, path, e=None, **kw):
        self.seq += 1
        r = {"kind": kind, "path": path, "seq": self.seq, "batch": self.batch}
        if e is not None:
            r.update(e.fields())
        r.update(kw)
        self.lines.append(r)
        self.open.append(r)
        return e

    def commit(self):
        self.seq += 1
        self.lines.append({"kind": "batch", "batch": self.batch, "records": len(self.open), "seq": self.seq})
        self.open = []
        self.batch += 1
        return self.batch - 1

    def mark(self, label):
        self.seq += 1
        self.lines.append({"kind": "mark", "label": label, "batch": self.batch - 1, "seq": self.seq})

    def write(self, tail=""):
        with open(os.path.join(self.out, "events.jsonl"), "w") as f:
            for r in self.lines:
                f.write(json.dumps(r) + "\n")
            f.write(tail)


def test_state_at_batch_boundaries(tmp_path):
    L = Log(tmp_path)
    d = L.add("created", "src", L.entry("dir", mode=0o755))
    a = L.add("created", "src/a.py", L.entry(data=b"1\n"))
    b0 = L.commit()
    a2 = L.entry(data=b"2\n", ino=a.ino)
    L.add("modified", "src/a.py", a2)
    L.add("moved", "src/b.py", a2, **{"from": "src/a.py"})
    b1 = L.commit()
    L.mark("after-move")
    L.add("deleted", "src/b.py", a2)
    b2 = L.commit()
    L.write()
    assert set(state_at(L.out, b0).entries) == {"src", "src/a.py"}
    assert set(state_at(L.out, b1).entries) == {"src", "src/b.py"}
    assert set(state_at(L.out, b2).entries) == {"src"}
    assert state_at(L.out, "last").batch == b2
    assert mark_batch(L.out, "after-move") == b1
    del d


def test_uncommitted_tail_is_left_out(tmp_path):
    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"a"))
    L.commit()
    L.add("created", "b", L.entry(data=b"b"))  # no batch record yet
    L.write(tail='{"kind": "crea')
    assert set(state_at(L.out, "last").entries) == {"a"}
    with pytest.raises(ReplayError):
        state_at(L.out, 1)


def test_count_mismatch_and_sequence_gap_raise(tmp_path):
    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"a"))
    L.commit()
    L.lines[-1]["records"] = 2
    L.write()
    with pytest.raises(ReplayError):
        state_at(L.out, "last")
    L2 = Log(tmp_path / "2")
    L2.add("created", "a", L2.entry(data=b"a"))
    L2.commit()
    L2.lines[-1]["seq"] = 9
    L2.write()
    with pytest.raises(ReplayError):
        state_at(L2.out, "last")


def test_no_baseline_raises(tmp_path):
    L = Log(tmp_path)
    L.write()
    with pytest.raises(ReplayError):
        state_at(L.out, "last")


def test_owner_links_and_git_backend_are_kept(tmp_path):
    L = Log(tmp_path)
    e = L.entry(data=b"x", uid=1001)
    L.add("created", "a", e)
    L.add("created", "b", e)
    L.add("git", ".git", head="ref: refs/heads/main", refs={}, backend="files")
    L.commit()
    L.write()
    st = state_at(L.out, 0)
    assert st.entries["a"].uid == 1001 and st.links() == [["a", "b"]]
    assert st.git["backend"] == "files"


def test_diff_reports_every_difference(tmp_path):
    L = Log(tmp_path)
    t = L.add("created", "t.txt", L.entry(data=b"one\ntwo\n"))
    bn = L.add("created", "bin", L.entry(data=b"\x00\x01"))
    big = L.add("created", "big", L.entry(data=b"B" * 10, content="oversize"))
    L.add("created", "wo", Entry(type="file", mode=0o200, uid=1000, gid=1000, dev=1, ino=500, size=1, sha256=None, content="unreadable", error="EACCES"))
    k = L.add("created", "keep", L.entry(data=b"same\n"))
    g = L.add("created", "gone", L.entry(data=b"bye\n"))
    o = L.add("created", "own", L.entry(data=b"o"))
    L.add("created", "idf", L.entry(data=b"i"))
    L.add("git", ".git", head="ref: refs/heads/main", refs={"refs/heads/main": "a" * 40}, backend="files")
    L.commit()
    L.add("modified", "t.txt", L.entry(data=b"one\nthree\n", ino=t.ino))
    L.add("modified", "bin", L.entry(data=b"\x00\x02", ino=bn.ino))
    L.add("modified", "big", L.entry(data=b"C" * 10, content="oversize", ino=big.ino))
    L.add("modified", "wo", Entry(type="file", mode=0o200, uid=1000, gid=1000, dev=1, ino=500, size=6, sha256=None, content="unreadable", error="EACCES"))
    k2 = L.entry(data=b"same\n", ino=k.ino, mode=0o600)
    L.add("attrib", "keep", k2)
    L.add("moved", "kept", k2, **{"from": "keep"})
    L.add("deleted", "gone", g)
    # A symlink whose target hashes like the deleted file must not pair with it.
    L.add("created", "ln", Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=900, size=3, sha256=g.sha256, content="stored", target="bye"))
    L.add("attrib", "own", L.entry(data=b"o", ino=o.ino, uid=0))
    L.add("attrib", "idf", L.entry(data=b"i"))
    L.add("git", ".git", head="ref: refs/heads/main", refs={"refs/heads/main": "b" * 40}, backend="files")
    L.commit()
    L.write()
    ds = {d.path: d for d in diff(L.out, 0, 1)}
    assert ds["t.txt"].status == "modified" and "-two\n+three\n" in ds["t.txt"].text
    assert ds["bin"].status == "modified" and ds["bin"].text is None and "binary" in ds["bin"].note
    assert ds["big"].status == "modified" and "oversize" in ds["big"].note
    assert ds["wo"].status == "modified" and "unknown" in ds["wo"].note
    assert ds["kept"].status == "renamed" and ds["kept"].old_path == "keep"
    assert ds["gone"].status == "removed"
    assert ds["ln"].status == "added"
    assert ds["own"].status == "owner"
    assert ds["idf"].status == "identity"
    assert ds[".git"].status == "git" and "b" * 40 in ds[".git"].note
    assert "keep" not in ds
    assert "renamed keep -> kept" in render(list(ds.values()))


def test_diff_git_only_change_is_reported(tmp_path):
    L = Log(tmp_path)
    L.add("git", ".git", head="ref: refs/heads/main", refs={}, backend="files")
    L.commit()
    L.add("git", ".git", head="ref: refs/heads/other", refs={}, backend="files")
    L.commit()
    L.write()
    ds = diff(L.out, 0, 1)
    assert [d.status for d in ds] == ["git"]
    assert diff(L.out, 1, 1) == []


def test_diff_missing_blob_raises(tmp_path):
    L = Log(tmp_path)
    t = L.add("created", "t.txt", L.entry(data=b"one\n"))
    L.commit()
    L.add("modified", "t.txt", L.entry(data=b"two\n", ino=t.ino))
    L.commit()
    L.write()
    os.unlink(L.blobs.path(sha(b"two\n")))
    with pytest.raises(ReplayError):
        diff(L.out, 0, 1)


def test_verify_blobs_on_request(tmp_path):
    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"one"))
    L.commit()
    L.write()
    assert state_at(L.out, 0, verify_blobs=True).entries["a"].content == "stored"
    os.unlink(L.blobs.path(sha(b"one")))
    assert state_at(L.out, 0).entries["a"].content == "stored"  # without verification
    with pytest.raises(ReplayError):
        state_at(L.out, 0, verify_blobs=True)


def test_verify_blobs_catches_a_corrupt_blob(tmp_path):
    L = Log(tmp_path)
    L.add("created", "a", L.entry(data=b"one"))
    L.commit()
    L.write()
    with open(L.blobs.path(sha(b"one")), "wb") as f:
        f.write(b"two")
    with pytest.raises(ReplayError):
        state_at(L.out, 0, verify_blobs=True)


def test_diff_pairs_renames_only_by_recorded_moves(tmp_path):
    L = Log(tmp_path)
    a = L.add("created", "a", L.entry(data=b"x", ino=7))
    L.commit()
    L.add("deleted", "a", a)
    L.add("created", "b", L.entry(data=b"x", ino=7))  # the inode number reused, no move
    L.commit()
    L.write()
    assert sorted(d.status for d in diff(L.out, 0, 1)) == ["added", "removed"]


def test_diff_follows_a_move_chain_and_a_directory_move(tmp_path):
    L = Log(tmp_path)
    d = L.add("created", "d", L.entry("dir", mode=0o755))
    f = L.add("created", "d/f", L.entry(data=b"f"))
    L.commit()
    L.add("moved", "e", d, **{"from": "d"})
    L.commit()
    L.add("moved", "g", f, **{"from": "e/f"})
    L.commit()
    L.write()
    ds = {x.path: x for x in diff(L.out, 0, 2)}
    assert ds["e"].status == "renamed" and ds["e"].old_path == "d"
    assert ds["g"].status == "renamed" and ds["g"].old_path == "d/f"
    back = {x.path: x for x in diff(L.out, 2, 0)}
    assert back["d/f"].status == "renamed" and back["d/f"].old_path == "g"


def test_links_include_hard_linked_symlinks(tmp_path):
    L = Log(tmp_path)
    e = Entry(type="symlink", mode=0o777, uid=1000, gid=1000, dev=1, ino=55, size=1, sha256=sha(b"t"), content="stored", target="t")
    L.add("created", "l1", e)
    L.add("created", "l2", e)
    L.commit()
    L.write()
    assert state_at(L.out, 0).links() == [["l1", "l2"]]
