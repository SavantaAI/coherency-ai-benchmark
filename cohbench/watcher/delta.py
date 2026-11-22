"""Records from the difference between two manifests.

`plan` derives a legal sequence of records that turns the old manifest into
the new one:

1. Fate of every old path, shallowest first. It is kept where its identity
   (device, inode, type) appears at the path its parent now has. Failing
   that, it moved where its identity appears at the path that the batch's
   observed renames map it to (renames can only confirm a move the
   identities show, never invent one). Failing that, a non-directory is kept
   in place, changed, if a non-directory is there now. Anything else is
   deleted, and every new path nobody claimed is created.
2. Deletions (deepest first), moves and creations (shallowest first) are
   applied in rounds, each only when legal on the working manifest, until
   none is left. A move cycle that cannot be ordered returns None.
3. In-place differences become `modified` (content), `attrib` (mode, owner,
   identity, or a type change between non-directories), and an observed
   write that left the entry identical becomes `rewritten`.

`resync` is the fallback: deletions and creations only, from the plain
difference. `check` applies records with the replay code and reports the
first difference from the scanned manifest.
"""

from __future__ import annotations

from .manifest import Entry, Manifest, ReplayError, apply_record, depth, join, name_of, parent, under
from .scan import rewrite


def _rec(kind: str, path: str, e: Entry | None, **extra) -> dict:
    r = {"kind": kind, "path": path}
    if e is not None:
        r.update(e.fields())
    for k, v in extra.items():
        if v is not None:
            r[k] = v
    return r


def _changes(a: Entry, b: Entry) -> list[str]:
    out = []
    if a.type != b.type:
        out.append("type")
    if (a.sha256, a.size, a.content, a.target, a.error, a.epoch) != (b.sha256, b.size, b.content, b.target, b.error, b.epoch):
        # For a directory the only "content" is whether it could be listed.
        out.append("access" if a.type == b.type == "dir" else "content")
    if a.mode != b.mode:
        out.append("mode")
    if (a.uid, a.gid) != (b.uid, b.gid):
        out.append("owner")
    if a.ident != b.ident:
        out.append("identity")
    return out


def plan(old: Manifest, new: Manifest, renames: list[tuple[str, str]] | None = None, written: set[str] | frozenset = frozenset(), trust_renames: bool = True, moved_out: set[str] | frozenset = frozenset()) -> list[dict] | None:
    renames = list(renames or []) if trust_renames else []
    # Renamed away to a place with no watch yet (a directory created in the
    # same batch): the rename was observed, its destination was not. A
    # renamed inode stays allocated, so its identity at one new path is it.
    moved_out = {rewrite(p, renames) for p in moved_out} if trust_renames else set()
    new_by_ident: dict[tuple, list[str]] = {}
    for q, e in new.entries.items():
        new_by_ident.setdefault((e.ident, e.type), []).append(q)
    claimed: set[str] = set()
    newloc: dict[str, str] = {}
    moved: dict[str, str] = {}
    order = sorted(old.entries, key=lambda p: (depth(p), p))

    def implied(p: str) -> str | None:
        par = parent(p)
        if not par:
            return p
        if par not in newloc:
            return None
        return join(newloc[par], name_of(p))

    for p in order:
        e = old.entries[p]
        q = implied(p)
        if q is not None and q not in claimed and q in new.entries and new.entries[q].ident == e.ident and new.entries[q].type == e.type:
            claimed.add(q)
            newloc[p] = q
            continue
        if renames or moved_out:
            target = rewrite(p, renames)
            cands = new_by_ident.get((e.ident, e.type), ())
            if target not in claimed and target in new.entries and target in cands:
                claimed.add(target)
                newloc[p] = target
                moved[p] = target
            elif target in moved_out and target not in new.entries:
                free = [a for a in cands if a not in claimed and a not in old.entries]
                if len(free) == 1:
                    claimed.add(free[0])
                    newloc[p] = free[0]
                    moved[p] = free[0]
    for p in order:
        if p in newloc or old.entries[p].type == "dir":
            continue
        q = implied(p)
        if q is not None and q not in claimed and q in new.entries and new.entries[q].type != "dir":
            claimed.add(q)
            newloc[p] = q

    w = old.copy()
    loc = {p: p for p in old.entries}
    back = {p: p for p in old.entries}  # current path -> old path
    recs: list[dict] = []
    pend_del = {p for p in old.entries if p not in newloc}
    pend_mov = [(p, moved[p]) for p in order if p in moved]
    pend_cre = {q for q in new.entries if q not in claimed}

    def emit(r: dict) -> None:
        apply_record(w, r)
        recs.append(r)

    def parent_ok(q: str) -> bool:
        par = parent(q)
        return par == "" or (par in w.entries and w.entries[par].type == "dir")

    while pend_del or pend_mov or pend_cre:
        progress = False
        for p in sorted(pend_del, key=lambda p: (-depth(loc[p]), loc[p])):
            cur = loc[p]
            if w.children(cur):
                continue
            emit(_rec("deleted", cur, w.entries[cur]))
            del back[cur]
            pend_del.discard(p)
            progress = True
        for item in list(pend_mov):
            p, a = item
            cur = loc[p]
            if a in w.entries or under(a, cur) or not parent_ok(a):
                continue
            emit(_rec("moved", a, w.entries[cur], **{"from": cur}))
            for cp in [c for c in list(back) if under(c, cur)]:
                op = back.pop(cp)
                np = a + cp[len(cur) :]
                loc[op] = np
                back[np] = op
            pend_mov.remove(item)
            progress = True
        for q in sorted(pend_cre, key=lambda q: (depth(q), q)):
            if q in w.entries or not parent_ok(q):
                continue
            emit(_rec("created", q, new.entries[q]))
            pend_cre.discard(q)
            progress = True
        if not progress:
            return None

    if set(w.entries) != set(new.entries):
        return None
    temps = {d: s for s, d in renames if s not in old.entries}
    touched = {r["path"] for r in recs}
    for q in sorted(new.entries, key=lambda q: (depth(q), q)):
        a, b = w.entries[q], new.entries[q]
        if a == b:
            if q in written and b.type == "file" and b.sha256 is not None and b.epoch == 0 and q not in touched:
                emit(_rec("rewritten", q, b))
            continue
        ch = _changes(a, b)
        via = temps.get(q)
        if "content" in ch and "type" not in ch:
            unknown = True if a.sha256 is None and b.sha256 is None and b.type == "file" else None
            emit(_rec("modified", q, b, changed=ch, prev_sha256=a.sha256, via_rename=via, content_unknown=unknown))
        else:
            emit(_rec("attrib", q, b, changed=ch, prev_type=a.type if "type" in ch else None, prev_sha256=a.sha256 if "type" in ch else None, via_rename=via))
    if w.git != new.git:
        emit(_git_rec(new.git))
    return recs


def _git_rec(g: dict | None) -> dict:
    if g is None:
        return {"kind": "git", "path": ".git", "head": None, "refs": None, "backend": None}
    return {"kind": "git", "path": ".git", "head": g["head"], "refs": dict(g["refs"]), "backend": g.get("backend")}


def resync(old: Manifest, new: Manifest) -> list[dict]:
    w = old.copy()
    recs: list[dict] = []
    bad = {p for p, e in old.entries.items() if new.entries.get(p) != e}
    for p in list(bad):
        if old.entries[p].type == "dir":
            bad.update(old.subtree(p))
    for p in sorted(bad, key=lambda p: (-depth(p), p)):
        r = _rec("deleted", p, w.entries[p])
        apply_record(w, r)
        recs.append(r)
    for q in sorted(new.entries, key=lambda q: (depth(q), q)):
        if q not in w.entries:
            r = _rec("created", q, new.entries[q])
            apply_record(w, r)
            recs.append(r)
    if w.git != new.git:
        recs.append(_git_rec(new.git))
    return recs


def check(old: Manifest, new: Manifest, recs: list[dict]) -> str | None:
    """None when applying recs to old gives new; otherwise what went wrong."""
    w = old.copy()
    try:
        for r in recs:
            apply_record(w, r)
    except ReplayError as e:
        return f"replay error: {e}"
    return w.first_difference(new)
