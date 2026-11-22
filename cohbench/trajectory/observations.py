"""Task-declared observations, extracted from the full trajectory the way the
registry extracts identifiers, and written as `observations.jsonl`.

One kind so far, `attempt`. The task names command patterns (its measurement
or job command, say), each optionally with a capture: a regular expression
with one group, applied to the command's output, that finds the task's own
result line. Each shell call by any agent whose command matches becomes a
record with:

- its start and end on the episode clock;
- `state`: a digest of the task tree it ran in (each path's type, mode,
  content hash and link target after the batch in force at the start), so
  two calls in equal trees have equal states whatever happened between them;
  and `batch_start`, `batch_end` for replay;
- `result`: `{status, fingerprint}`. Status is `complete`, `truncated`,
  `cancelled` (the command was stopped), `unfinished` (still running at the
  episode end, or never finished) or `unavailable` (no output recorded).
  With a task capture, the status describes the task's result: `complete`
  when the capture found a whole result line (the fingerprint is the hash of
  what it captured, the same for every harness whenever the line survived
  whole), `truncated` when it did not and the harness or driver cut the
  output, `complete` with a null fingerprint when the whole output holds no
  result line. A line is whole when it does not touch a clipped edge: not
  the first retained line when the head was cut, and ended by a line break
  inside the retained text when the tail was cut; nothing counts when the
  middle was cut, since the cut's place is not known. Without a capture, the
  status is the output's (`truncated` when cut) and the fingerprint the hash
  of the whole output when complete. Only `complete` carries a fingerprint,
  and a null fingerprint equals nothing.

Nothing else from the call: not the command text, not the output, not the
exit status (no score uses it, and a null against a zero would name the
harness), not the agent. Equivalent attempts have the same name and the same non-null state, and
equal fingerprints when both have one (D-048).

With `by: "output"`, a capture is required. Each whole-line match in any
shell call is an attempt, with the call's times and tree and the captured
text's hash. Available output without a whole match yields no attempt.
Unavailable output falls back to the command regex and has no fingerprint.
Without `by`, command selection and its one record per call are unchanged.

A further kind adds a record type to `KINDS` with its own fields and an
extractor in `build`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import format as F
from .changelog import ChangeLog
from .clock import Clock, episode_end
from .io import Trajectory

KINDS: dict[str, tuple[str, ...]] = {
    "attempt": ("type", "seq", "name", "t", "t_end", "state", "state_unknown", "batch_start", "batch_end", "result"),
}
RESULT_STATUSES = ("complete", "truncated", "cancelled", "unfinished", "unavailable")
# Which fields each status may carry: (has an end, may have a fingerprint).
STATUS_FIELDS = {"complete": (True, True), "truncated": (True, False), "cancelled": (True, False), "unfinished": (False, False), "unavailable": (True, False)}
NAME = re.compile(r"\A[a-z][a-z0-9_]{0,40}\Z")
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass
class AttemptPattern:
    name: str
    regex: str
    capture: str | None = None
    by: str | None = None


@dataclass
class TaskObservations:
    attempts: list[AttemptPattern] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskObservations":
        for a in d.get("attempts", []):
            if "by" in a and a["by"] != "output":
                raise ValueError("attempt pattern by: output")
            if a.get("by") == "output" and not isinstance(a.get("capture"), str):
                raise ValueError("attempt pattern by output requires a capture")
        out = cls([AttemptPattern(a["name"], a["regex"], a.get("capture"), a.get("by")) for a in d.get("attempts", [])])
        for a in out.attempts:
            if not NAME.match(a.name):
                raise ValueError(f"attempt pattern name {a.name!r}: lower-case word")
            re.compile(a.regex)
            if a.capture is not None and (re.compile(a.capture).groups != 1 or not _required_group(a.capture)):
                raise ValueError(f"attempt pattern {a.name!r}: the capture has exactly one group, and it is not optional")
        return out


def _required_group(pattern: str) -> bool:
    """The capture's one group takes part in every match: it is not under a
    repeat that allows zero, nor inside an alternation."""
    try:
        from re import _constants as C, _parser as P  # Python 3.11 and later
    except ImportError:  # pragma: no cover
        import sre_constants as C  # type: ignore[no-redef]
        import sre_parse as P  # type: ignore[no-redef]

    def walk(sub: Any) -> bool:
        for op, av in sub:
            if op is C.SUBPATTERN:
                if av[0] == 1 or walk(av[-1]):
                    return True
            elif op in (C.MAX_REPEAT, C.MIN_REPEAT, getattr(C, "POSSESSIVE_REPEAT", None)) and op is not None:
                if av[0] >= 1 and walk(av[2]):
                    return True
            elif op is getattr(C, "ATOMIC_GROUP", None):
                if walk(av):
                    return True
        return False

    return walk(P.parse(pattern))


@dataclass
class Observations:
    header: dict[str, Any]
    records: list[dict[str, Any]]
    sources: dict[int, int] = field(default_factory=dict, compare=False, repr=False)  # observation seq -> trajectory seq, in memory only
    wall: dict[int, tuple[int, int | None]] = field(default_factory=dict, compare=False, repr=False)  # seq -> wall (start, end) microseconds, in memory only

    def dumps(self) -> str:
        return "".join(F.canonical(x) + "\n" for x in [self.header, *self.records])


def digest(obj: Any) -> str:
    return hashlib.sha256(F.canonical(obj).encode("utf-8")).hexdigest()


def tree_digest(changes: ChangeLog | None, batch: int | None) -> str | None:
    """The digest of the tree after `batch`, or None when no comparable
    digest exists: no change log, no batch in force, or (D-032) a file in the
    tree whose content is unknown (`state_unknown` says which)."""
    if changes is None or batch is None or changes.unknown(batch):
        return None
    return digest(sorted([p, *v] for p, v in changes.tree(batch).items()))


def whole_matches(cap: re.Pattern[str], text: str, clipped: str, last_line_whole: bool = False):
    """Every match of the capture on a whole line: one that does not
    touch a clipped edge. When the tail was cut, the line ends with a line
    break inside the retained text, or it is the last line and the adapter
    recorded that it ended with one before cleanup (`last_line_whole`).
    Nothing is whole when the middle was cut."""
    if clipped == "middle":
        return
    for m in cap.finditer(text):
        if m.group(1) is None:
            continue  # the group did not take part: no result here
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        if clipped in ("head", "both") and start == 0:
            continue
        if clipped in ("tail", "both") and end == -1 and not last_line_whole:
            continue
        if "\n" in text[m.start():m.end()]:
            continue
        yield m


def whole_match(cap: re.Pattern[str], text: str, clipped: str, last_line_whole: bool = False):
    return next(whole_matches(cap, text, clipped, last_line_whole), None)


def build(traj: Trajectory, changes: ChangeLog | None, task: TaskObservations) -> Observations:
    """Decided on wall microseconds (start, end, the cutoff, the batch in
    force), written in episode milliseconds."""
    pats = [(a, re.compile(a.regex), re.compile(a.capture, re.M) if a.capture else None) for a in task.attempts]
    clock = Clock(traj.header.get("excluded", []))
    end = episode_end(traj.events)
    end_us = end["us"] if end else None
    walled = changes is not None and len(changes.wall_us) == len(changes.records)

    def batch_at(us: int) -> int | None:
        if changes is None:
            return None
        return changes.batch_at_us(us) if walled else changes.batch_at(clock(us))

    recs = []
    post = set(traj.header.get("post_run") or [])  # the post-run exchange, never scored
    for e in traj.events:
        if e["seq"] in post:
            continue
        if e["type"] != "tool_call" or e["class"] != "shell" or (end_us is not None and e["us"] > end_us):
            continue
        cmd = e["input"].get("command") or ""
        out = e["output"]
        match = None
        for a, rx, cap in pats:
            if a.by == "output" and out["available"]:
                matches = list(whole_matches(cap, out["text"], out["clipped"], out["last_line_whole"]))
                if matches:
                    match = (a, cap, matches)
                    break
            elif rx.search(cmd):
                match = (a, cap, [None])
                break
        if match is None:
            continue
        a, cap, matches = match
        unfinished = e["status"] == "open" or e["us_end"] is None or (end_us is not None and e["us_end"] > end_us)
        t = clock(e["us"], f"attempt (trajectory seq {e['seq']})")
        t_end = None if unfinished else clock(e["us_end"], f"the end of attempt (trajectory seq {e['seq']})")
        for captured in matches:
            if unfinished:
                result = {"status": "unfinished", "fingerprint": None}
            elif e["status"] == "cancelled":
                result = {"status": "cancelled", "fingerprint": None}
            elif not out["available"]:
                result = {"status": "unavailable", "fingerprint": None}
            elif cap is not None:
                # The task's result decides: a whole captured line is the result,
                # whatever else of the output the harness kept.
                m = captured or whole_match(cap, out["text"], out["clipped"], out["last_line_whole"])
                if m:
                    result = {"status": "complete", "fingerprint": hashlib.sha256(m.group(1).encode("utf-8")).hexdigest()}
                else:
                    result = {"status": "truncated" if out["truncated"] else "complete", "fingerprint": None}
            else:
                status = "truncated" if out["truncated"] else "complete"
                fp = hashlib.sha256(out["text"].encode("utf-8")).hexdigest() if status == "complete" else None
                result = {"status": status, "fingerprint": fp}
            b0 = batch_at(e["us"])
            recs.append({"_src": e["seq"], "_us": e["us"], "_us_end": None if unfinished else e["us_end"], "type": "attempt", "name": a.name, "t": t, "t_end": t_end,
                         "state": tree_digest(changes, b0), "state_unknown": bool(changes is not None and b0 is not None and changes.unknown(b0)), "batch_start": b0, "batch_end": batch_at(e["us_end"]) if changes and not unfinished else None, "result": result})
    recs.sort(key=lambda r: (r["_us"], r["_us_end"] if r["_us_end"] is not None else r["_us"], r["name"]))
    sources, wall = {}, {}
    for n, r in enumerate(recs, 1):
        r["seq"] = n
        sources[n] = r.pop("_src")
        wall[n] = (r.pop("_us"), r.pop("_us_end"))
    header = {"type": "header", "format": F.OBSERVATIONS_FORMAT, "version": F.VERSION, "kinds": sorted(KINDS), "records": len(recs)}
    return Observations(header, recs, sources, wall)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _in_force(changes: ChangeLog, batch: Any, t: int) -> bool:
    """`batch` can be the one in force at millisecond t: it closed at or
    before t, and the next one closed at or after t (null before the
    baseline)."""
    closes = [(r["batch"], r["t"]) for r in changes.records if r.get("type") == "batch" and _int(r.get("batch")) and _int(r.get("t"))]
    if batch is None:
        return not closes or closes[0][1] >= t
    at = next((n for n, (b, _) in enumerate(closes) if b == batch), None)
    if at is None or closes[at][1] > t:
        return False
    return at + 1 == len(closes) or closes[at + 1][1] >= t


def validate(obs: Observations, changes: ChangeLog | None = None) -> list[str]:
    errors = []
    h = obs.header
    if not isinstance(h, dict) or h.get("type") != "header" or h.get("format") != F.OBSERVATIONS_FORMAT or h.get("version") != F.VERSION \
            or h.get("kinds") != sorted(KINDS) or h.get("records") != len(obs.records) or set(h) != {"type", "format", "version", "kinds", "records"}:
        errors.append("observations: header")
    batches = {r["batch"] for r in changes.records if r.get("type") == "batch"} if changes else None
    last = None
    for n, r in enumerate(obs.records, 1):
        where = f"observation {n}"
        typ = r.get("type") if isinstance(r, dict) else None
        if not isinstance(typ, str) or typ not in KINDS or set(r) != set(KINDS[typ]):
            errors.append(f"{where}: fields differ from the {typ!r:.20} schema")
            continue
        if r["seq"] != n or not _int(r["t"]) or r["t"] < 0 or (last is not None and r["t"] < last):
            errors.append(f"{where}: seq or time out of order")
            continue
        last = r["t"]
        if not (r["t_end"] is None or (_int(r["t_end"]) and r["t_end"] >= r["t"])):
            errors.append(f"{where}: t_end is null or at or after t")
        if not isinstance(r["name"], str) or not NAME.match(r["name"]):
            errors.append(f"{where}: name")
        if not (r["state"] is None or (isinstance(r["state"], str) and HEX64.match(r["state"]))):
            errors.append(f"{where}: state is a digest or null")
        elif r["state"] is not None and r["batch_start"] is None:
            errors.append(f"{where}: a state without a batch in force")
        if not isinstance(r["state_unknown"], bool) or (r["state_unknown"] and r["state"] is not None):
            errors.append(f"{where}: state_unknown is a boolean, and a state with unknown content has no digest")
        res = r["result"]
        if not isinstance(res, dict) or set(res) != {"status", "fingerprint"} or res.get("status") not in RESULT_STATUSES \
                or not (res.get("fingerprint") is None or (isinstance(res.get("fingerprint"), str) and HEX64.match(res["fingerprint"]))):
            errors.append(f"{where}: result is {{status, fingerprint}}")
        else:
            has_end, may_fp = STATUS_FIELDS[res["status"]]
            if (r["t_end"] is not None) != has_end or (r["batch_end"] is not None and not has_end):
                errors.append(f"{where}: a {res['status']} attempt {'has' if has_end else 'has no'} end")
            if res["fingerprint"] is not None and not may_fp:
                errors.append(f"{where}: a {res['status']} attempt has no fingerprint")
        for k in ("batch_start", "batch_end"):
            if not (r[k] is None or _int(r[k])):
                errors.append(f"{where}: {k} is an integer or null")
            elif batches is not None and r[k] is not None and r[k] not in batches:
                errors.append(f"{where}: {k} {r[k]} is not a batch of the change log")
        if changes is not None and _int(r["t"]):
            # Milliseconds cannot order a batch and an attempt that share one;
            # the batch must be in force at that time, ties either way.
            if not _in_force(changes, r["batch_start"], r["t"]) or (_int(r["t_end"]) and not _in_force(changes, r["batch_end"], r["t_end"])):
                errors.append(f"{where}: batch_start and batch_end are the batches in force at its start and end")
            elif _int(r["batch_start"]) and (r["state"] != tree_digest(changes, r["batch_start"]) or r["state_unknown"] != bool(changes.unknown(r["batch_start"]))):
                errors.append(f"{where}: state is not the digest of batch {r['batch_start']}'s tree (null, with state_unknown, when it holds a file of unknown content)")
    return errors


def read(path: str | Path) -> Observations:
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").split("\n") if x.strip()]
    return Observations(rows[0], rows[1:])
