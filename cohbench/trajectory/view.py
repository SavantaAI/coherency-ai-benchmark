"""The scorer view: a projection of the full trajectory holding only what
the user saw and said, the main agent's turn boundaries, and the runner's
events.

The view is shaped so that two agents that showed the user the same things
at the same times give the same view, whatever their harness did out of
sight:

- A question asked through a question tool becomes the same as a question
  asked in a final message: one `to_user` with the text the user was shown,
  the end of the turn, and the user's answer as a `reply` that opens the next
  turn. A secret request is marked `secret`, and its answer carries no text.
- Every user-channel output is dated at delivery (`delivery_us`): a final
  message when its turn ends, a message-tool text when the tool's result came
  back, a question when it was put to the user.
- Turns are assigned after sorting by delivery time, so output that arrives
  after a turn ended has the next turn, or none. Each `turn_end` names the
  output that closed the turn (`closed_by`).
- The episode end follows `clock.END_CONTRACT`: nothing delivered after the
  runner's `episode_end` is kept, a turn still active then is closed as
  interrupted, a wait still open then ends there, and nothing follows
  `episode_end`.
- Every decision is made on wall microseconds; every written time is on the
  episode clock in milliseconds (`clock.Clock`), never before the origin.

cohbench/trajectory/README.md, "The scorer view", gives the reason for each field.
"""

from __future__ import annotations

from typing import Any

from . import format as F
from .clock import Clock, episode_end, to_ms
from .io import Trajectory

USER_CHANNEL_TYPES = ("assistant_message", "tool_call", "question")


def end_seq_us(traj: Trajectory) -> tuple[int | None, int | None]:
    end = episode_end(traj.events)
    return (end["seq"], end["us"]) if end else (None, None)


def in_episode(traj: Trajectory) -> list[dict[str, Any]]:
    """The events that start at or before the runner's episode_end (all of
    them when there is none), less the post-run exchange (header `post_run`),
    which is out by reference whatever its times. Decided on wall
    microseconds. Callers apply the delivery rule themselves."""
    post = set(traj.header.get("post_run") or [])
    seq, end = end_seq_us(traj)
    if seq is None:
        return [e for e in traj.events if e["seq"] not in post]
    return [e for e in traj.events if e["seq"] not in post and e["us"] <= end and (e["seq"] <= seq or e["type"] not in F.RUNNER_TYPES)]


def delivery_us(e: dict[str, Any], turn_end_us: dict[int, int]) -> int:
    """When a user-channel event reached the user (wall microseconds), the
    driver's item delivery time, the instant the runner judged it at: a
    final message when its item finished (at its turn's end in an old
    capture with no item completion), a message-tool text when the call's
    result came back, a question when it was put to the user."""
    return F.delivered_at(e["type"], e["us"], e.get("us_end"), turn_end_us.get(e.get("turn")))


def turn_ends(traj: Trajectory) -> dict[int, int]:
    return {e["turn"]: e["us"] for e in traj.events if e["type"] == "turn_end"}


def scorer_view(traj: Trajectory) -> Trajectory:
    clock = Clock(traj.header.get("excluded", []))
    end = episode_end(traj.events)
    end_seq, cut = (end["seq"], end["us"]) if end else (None, None)
    ends = turn_ends(traj)
    questions = {e["question"]: e for e in traj.events if e["type"] == "question"}
    # Rows: (wall us, source seq, sub-order, row, target source seq, closes full turn)
    # Each main turn's closing output's delivery (the last one), for the end cut.
    closing_delivery: dict[int, int] = {}
    for e in traj.events:
        if e["type"] in ("assistant_message", "question") and e["agent"] == 0 and e.get("user_channel") and e.get("turn") is not None:
            d = delivery_us(e, ends)
            closing_delivery[e["turn"]] = max(d, closing_delivery.get(e["turn"], d))
    rows: list[tuple[int, float, int, dict[str, Any], int | None, int | None]] = []
    pending_resume = False
    open_full: int | None = None  # agent 0's open turn in the full trajectory
    post = set(traj.header.get("post_run") or [])  # the post-run exchange: out by reference, whatever its times
    for e in traj.events:
        et, agent, src, u = e["type"], e["agent"], e["seq"], e["us"]
        if src in post:
            continue
        if et in F.AUDIT_RUNNER_TYPES:
            continue  # full trajectory only (D-046: a notice's delivery)
        if et in F.RUNNER_TYPES:
            if end_seq is None or src <= end_seq:
                out = {k: v for k, v in e.items() if k not in ("seq", "agent", "segment", "t", "us", "us_end")}
                if et == "wait":
                    out["_us_end"] = e.get("us_end")
                    if cut is not None and (e["ended"] == "open" or e.get("us_end") is None or e["us_end"] > cut):
                        # Still open at the end: it ends there, charged up to
                        # the end on the episode clock (an excluded interval
                        # inside it is not charged); an open wait the runner
                        # left uncharged is charged up to the end.
                        out["_us_end"] = cut
                        out["ended"] = "budget_cutoff" if end["reason"] == "budget_cutoff" else "episode_end"
                        span = max(0, clock(cut, "the end of a wait") - clock(u, "a wait"))
                        out["charged_ms"] = span if out.get("charged_ms") is None else max(0, min(out["charged_ms"], span))
                if et == "handback":
                    out.pop("kind", None)
                if et == "correction_delivered":
                    out["mark"] = out["batch"] = out["straddled"] = out["straddled_last"] = None  # the bundle maker numbers the mark and resolves its batch
                rows.append((u, src, 0, out, e.get("target"), None))
            continue
        if cut is not None and u > cut and et != "turn_end":
            continue
        if et == "turn_start" and agent == 0:
            open_full = e["turn"]
            if pending_resume:
                pending_resume = False  # the answer already opened this turn in the view
                continue
            rows.append((u, src, 0, {"type": "turn_start"}, None, None))
        elif et == "turn_end" and agent == 0:
            # The effective end of a turn with a closing output is that
            # output's delivery (m7 of round 11), decided before the end cut:
            # a final turn whose closing output was delivered by the end is
            # finished, though its run completed a little after the end.
            settled_by = closing_delivery.get(e["turn"])
            finished_in_time = e["end"] in ("finished", "waiting") and settled_by is not None and cut is not None and settled_by <= cut
            if cut is not None and (u > cut or e["end"] == "open") and not finished_in_time:
                continue  # still active at the end: closed at the cut below
            open_full = None
            rows.append((u, src, 2, {"type": "turn_end", "end": "finished" if e["end"] == "waiting" else e["end"], "_turn": e["turn"]}, None, None))
        elif et == "user_message" and agent == 0 and e["kind"] in F.USER_AUTHORED_KINDS:
            rows.append((u, src, 0, {"type": "user_message", "kind": e["kind"], "text": e["text"], "secret": False}, None, None))
        elif et in USER_CHANNEL_TYPES and e.get("user_channel"):
            d = delivery_us(e, ends)
            if cut is not None and d > cut:
                continue  # not delivered by the end
            secret = et == "question" and e["kind"] == "secret"
            closes = e["turn"] if agent == 0 and et in ("assistant_message", "question") else None
            rows.append((d, src, 1, {"type": "to_user", "text": e["user_text"], "secret": secret}, None, closes))
        elif et == "answer":
            q = questions.get(e["question"])
            is_secret = q is not None and q["kind"] == "secret"
            refusal = is_secret and e["status"] in ("declined", "cancelled") and e["text"] is not None
            if e["status"] in ("answered", "saved") or refusal:
                secret = is_secret and not refusal  # a refusal's words are the user's, not a secret (D-023)
                if agent == 0:
                    rows.append((u, src, 0, {"type": "turn_start"}, None, None))
                    pending_resume = True
                rows.append((u, src, 1, {"type": "user_message", "kind": e.get("kind", "reply"), "text": "" if secret else (e["text"] or ""), "secret": secret}, None, None))
    if cut is not None and open_full is not None:
        rows.append((cut, end_seq - 0.5, 2, {"type": "turn_end", "end": "interrupted", "_turn": None}, None, None))
    # m7 of round 11: a turn with a closing output ends, in the view, at that
    # output's delivery, right after it, so a prose final message and a
    # question through a tool give the same order against the runner's
    # verdict, wait and hand-back at that instant.
    closing_at: dict[int, tuple[int, int]] = {}
    for u_, src_, _, row_, _, closes_ in rows:
        if closes_ is not None and (closes_ not in closing_at or (u_, src_) > closing_at[closes_]):
            closing_at[closes_] = (u_, src_)
    # Without the runner's end, the view still lasts until the last turn's
    # own end, as before.
    last_turn_end = max((r[0] for r in rows if r[3]["type"] == "turn_end"), default=None)
    rows = [(closing_at[r[3]["_turn"]][0], closing_at[r[3]["_turn"]][1], 1.5, *r[3:])
            if r[3]["type"] == "turn_end" and r[3].get("_turn") in closing_at and r[3]["end"] == "finished" else r for r in rows]
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    first_row_of: dict[int, int] = {}
    closing_row: dict[int, int] = {}
    for n, (_, src, _, row, _, closes) in enumerate(rows):
        if row["type"] in ("to_user", "user_message"):
            first_row_of.setdefault(src, n)
        if closes is not None:
            closing_row[closes] = n
    out_events = []
    sources: dict[int, int] = {}
    wall: dict[int, int] = {}
    turn = 0
    open_turn: int | None = None
    for n, (u, src, _, row, target, _) in enumerate(rows):
        ev = {"seq": n, "t": clock(u, f"scorer view {row['type']} from trajectory event {src}"), **{k: v for k, v in row.items() if not k.startswith("_")}}
        if row["type"] == "turn_start":
            turn += 1
            open_turn = turn
            ev["turn"] = turn
        elif row["type"] == "turn_end":
            ev["turn"] = open_turn if open_turn is not None else turn
            ev["closed_by"] = closing_row.get(row["_turn"]) if row["_turn"] is not None else None
            open_turn = None
        elif row["type"] in ("to_user", "user_message"):
            ev["turn"] = open_turn
        if "target" in ev:
            if target is not None and target not in first_row_of:
                raise ValueError(f"runner event {row['type']} targets event {target}, which the scorer view does not hold")
            ev["target"] = first_row_of[target] if target is not None else None
        if isinstance(ev.get("replies_to"), list):
            missing = [x for x in ev["replies_to"] if x not in first_row_of]
            if missing:
                raise ValueError(f"a wait replies to events {missing}, which the scorer view does not hold")
            ev["replies_to"] = sorted(first_row_of[x] for x in ev["replies_to"])
        if ev["type"] == "wait":
            ev["t_end"] = clock(row["_us_end"], "the end of a wait") if row.get("_us_end") is not None else None
        if row["type"] in ("to_user", "user_message"):
            sources[n] = src
        wall[n] = u
        out_events.append(ev)
    duration = out_events[-1]["t"] if cut is None and out_events else (clock(cut, "episode_end") if cut is not None else 0)
    if cut is None and last_turn_end is not None:
        duration = max(duration, clock(last_turn_end, "the last turn's end"))
    header = {"type": "header", "format": F.VIEW_FORMAT, "version": F.VERSION, "events": len(out_events), "turns": turn, "duration_ms": duration,
              "clock_origin": traj.header.get("clock_origin", "opening_message"), "wait_charges": traj.header.get("wait_charges", "scheduled"),
              "handbacks": traj.header.get("handbacks", "inferred")}
    return Trajectory(header=header, events=out_events, sources=sources, wall=wall)
