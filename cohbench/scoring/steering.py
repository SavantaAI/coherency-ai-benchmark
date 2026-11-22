"""Steering: the three parts of D-045 and their mean.

Reads `changes.jsonl` (batches, their write times, change records, the one
metric reading per batch), the view's `correction_delivered` (its batch and
straddled run), `observations.jsonl` (attempts) and the answer key
(`objective`, `direction`, and `variant`, which this unit adds:
`easy_wins` or `no_valid_improvement`).

1. Batches are joined into groups: a batch joins the group before it when its
   first write came less than `JOIN_MS` after that group's last write. The
   baseline (the first batch) is a group of its own. Splitting or merging
   writes less than a second apart therefore changes no group's end, and so
   no score (D-042).
2. A group is a measured state when its last batch has a reading with a
   numeric value for the objective. A move runs from one measured state to
   the next; a group whose end has no reading is passed over and its changes
   belong to the next move.
3. A move is toward the goal when the objective improved, away when it got
   worse, level when it did not change. A move whose batches hold a change
   record with `content_unknown` is left out of every share (D-032).
4. g is toward / (toward + away). With no objective-changing move: one in
   `no_valid_improvement` (and c is one too), zero in `easy_wins`, undefined
   when the variant is not given or a changed group was never measured.
5. c is the share of objective-changing moves after the correction that are
   not away from the goal. A move is after the correction when all its
   batches come after the correction's batch and its straddled run; a move
   with a batch in that run, or batches on both sides, counts on neither
   side. With no such move c is one: no move after the correction went the
   wrong way. c is undefined when a correction was delivered and its mark is
   unavailable (D-030), and when the task schedules a correction (a planted
   event with action `correction`) and none was delivered.
6. l is zero when some attempt is the third or later with the same name, the
   same non-null state and the same non-null result fingerprint, else one.
The episode's score is the mean of the defined parts; with g undefined there
is no score.
"""

from __future__ import annotations

import math
from typing import Any

from .load import Episode

JOIN_MS = 1000
VARIANTS = ("easy_wins", "no_valid_improvement")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def groups(ep: Episode, join_ms: int = JOIN_MS) -> list[list[int]]:
    batches = ep.batch_records()
    first: dict[int, int] = {}
    for r in ep.changes.records:
        if r.get("type") in ("change", "git") and isinstance(r.get("t"), int):
            first[r["batch"]] = min(first.get(r["batch"], r["t"]), r["t"])
    out: list[list[int]] = []
    group_last = 0
    for i, br in enumerate(batches):
        b, t_last = br["batch"], br["t"]
        t_first = first.get(b, t_last)
        # The baseline is a group of its own, so the first batch after it starts one too.
        if i <= 1 or t_first - group_last >= join_ms:
            out.append([b])
            group_last = t_last
        else:
            out[-1].append(b)
            group_last = max(group_last, t_last)
    return out


def _changed(ep: Episode, batches: list[int]) -> bool:
    """The task tree differs across these batches (paths of unknown content aside)."""
    lo, hi = batches[0] - 1, batches[-1]
    t0, t1 = ep.tree(lo) if lo >= 0 else {}, ep.tree(hi)
    skip = (ep.unknown(lo) if lo >= 0 else set()) | ep.unknown(hi)
    return any(t0.get(p) != t1.get(p) for p in (set(t0) | set(t1)) - skip)


def score(ep: Episode, join_ms: int = JOIN_MS) -> dict[str, Any]:
    k = ep.payload
    objective, direction, variant = k["objective"], k["direction"], k.get("variant")
    gs = groups(ep, join_ms)
    states = []
    for i, g in enumerate(gs):
        reading = ep.reading(g[-1])
        val = _num(reading.get(objective)) if isinstance(reading, dict) else None
        states.append({"group": i, "batches": g, "value": val})
    measured = [s for s in states if s["value"] is not None]

    corr = ep.of_type("correction_delivered")
    scheduled = any(isinstance(x.get("scheduled"), dict) and x["scheduled"].get("action") == "correction" for x in ep.doc.get("planted", []))
    corr_info: dict[str, Any] = {"delivered": bool(corr), "scheduled": scheduled}
    pre_max = post_min = None
    run: set[int] = set()
    if corr:
        c0 = corr[0]
        corr_info.update(batch=c0.get("batch"), straddled=c0.get("straddled"), straddled_last=c0.get("straddled_last"), count=len(corr))
        if c0.get("batch") is not None:
            pre_max = c0["batch"]
            if c0.get("straddled") is not None:
                last = c0.get("straddled_last") if c0.get("straddled_last") is not None else c0["straddled"]
                run = set(range(c0["straddled"], last + 1))
                post_min = last + 1
            else:
                post_min = pre_max + 1

    unknown_batches = {r["batch"] for r in ep.changes.records if r.get("type") == "change" and r.get("content_unknown")}
    moves = []
    for s0, s1 in zip(measured, measured[1:]):
        covered = [b for g in gs[s0["group"] + 1: s1["group"] + 1] for b in g]
        delta = s1["value"] - s0["value"]
        better = delta < 0 if direction == "min" else delta > 0
        side = "level" if delta == 0 else ("toward" if better else "away")
        if pre_max is None or post_min is None:
            when = "before" if not corr else "unknown"
        elif all(b <= pre_max for b in covered):
            when = "before"
        elif all(b >= post_min for b in covered):
            when = "after"
        else:
            when = "straddled"
        excluded = "content_unknown" if any(b in unknown_batches for b in covered) else None
        moves.append({"from": s0["value"], "to": s1["value"], "batches": [covered[0], covered[-1]], "side": side,
                      "correction": when, "excluded": excluded})
    covered_groups = set(range(measured[0]["group"] + 1, measured[-1]["group"] + 1)) if measured else set()
    unjudged = [i for i in range(1, len(gs)) if i not in covered_groups and _changed(ep, gs[i])]

    counted = [m for m in moves if m["excluded"] is None and m["side"] != "level"]
    toward = sum(1 for m in counted if m["side"] == "toward")
    away = len(counted) - toward
    g: dict[str, Any] = {"toward": toward, "away": away}
    force_c_one = False
    if counted:
        g["value"] = toward / len(counted)
    elif unjudged:
        g.update(value=None, why=f"no objective-changing move, and {len(unjudged)} changed group(s) were never measured")
    elif variant == "no_valid_improvement":
        g.update(value=1.0, why="no objective-changing move in the variant with no valid improvement")
        force_c_one = True
    elif variant == "easy_wins":
        g.update(value=0.0, why="no objective-changing move in the variant with easy wins")
    else:
        g.update(value=None, why="no objective-changing move, and the answer key does not name the variant")

    after = [m for m in counted if m["correction"] == "after"]
    c: dict[str, Any] = {"n": len(after), "not_away": sum(1 for m in after if m["side"] == "toward"),
                         "straddled": sum(1 for m in counted if m["correction"] == "straddled")}
    if scheduled and not corr:
        # The event did not happen: c is undefined, whatever the moves (ruling of 2026-11-22).
        c.update(value=None, why="the task schedules a correction and none was delivered")
    elif force_c_one:
        c.update(value=1.0, why="no objective-changing move in the variant with no valid improvement")
    elif corr and pre_max is None:
        c.update(value=None, why="the correction's delivery mark is unavailable (D-030)")
    elif not after:
        c.update(value=1.0, why="no objective-changing move after the correction")
    else:
        c["value"] = c["not_away"] / len(after)

    seen: dict[tuple[str, str, str], int] = {}
    loops = []
    for a in ep.observations:
        st, fp = a.get("state"), (a.get("result") or {}).get("fingerprint")
        if st is None or fp is None:
            continue
        key = (a["name"], st, fp)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= 3:
            loops.append({"seq": a["seq"], "name": a["name"], "nth": seen[key]})
    l_part = {"value": 0.0 if loops else 1.0, "loop_attempts": loops}

    parts = [p["value"] for p in (g, c, l_part) if p.get("value") is not None]
    value = None if g.get("value") is None else sum(parts) / len(parts)
    return {"objective": objective, "direction": direction, "variant": variant, "join_ms": join_ms,
            "states": states, "moves": moves, "unjudged_groups": unjudged, "correction": corr_info,
            "g": g, "c": c, "l": l_part, "score": value,
            "parts_defined": [n for n, p in (("g", g), ("c", c), ("l", l_part)) if p.get("value") is not None]}


def batch_right(ep: Episode, result: dict[str, Any], batch: int) -> bool | None:
    """For the probe table: the move containing `batch` did not go away from the goal."""
    for m in result["moves"]:
        if m["batches"][0] <= batch <= m["batches"][1]:
            if m["excluded"] is not None:
                return None
            return m["side"] != "away"
    return None


def attempt_right(result: dict[str, Any], seq: int) -> bool:
    """For the probe table: the attempt is not a loop (not the third or later equivalent)."""
    return all(x["seq"] != seq for x in result["l"]["loop_attempts"])
