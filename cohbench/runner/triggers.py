"""Event triggers: conditions the runner can observe without reading the
agent's mind.

A trigger is a JSON object with exactly one key:

| Form | Holds when |
|---|---|
| `{"path_exists": GLOB}` | a path matches that exists: in the committed tree, or created by a batch that has begun (from its first write, D-030) |
| `{"content_matches": {"path": GLOB, "regex": RE}}` | a stored file whose path matches has content the regex finds; `^` and `$` match at line ends (MULTILINE) |
| `{"changes": {"path": GLOB, "exclude": [GLOB, ...]?, "at_least": N, "since": EVENT?, "unit": "record" or "batch"}}` | at least N committed change records touched a path matching `path` and no `exclude` glob (default `record`); a move counts once if either its source or destination qualifies; `batch` counts distinct committed batches with at least one qualifying record |
| `{"metric": {"name": NAME, "op": OP, "value": NUMBER, "reading": "latest" or "any", "since": EVENT?}}` | the latest (or any) committed reading after the baseline compares true |
| `{"turns": {"at_least": N, "since": EVENT?}}` | at least N turns finished (the agent ended its turn; a pending question is not a finished turn) |
| `{"handbacks": {"at_least": N, "since": EVENT?}}` | at least N hand-backs: finished turns plus pauses on a pending question or secret request |
| `{"outputs": {"at_least": N, "since": EVENT?}}` | at least N user-channel outputs, message-tool texts mid-turn included |
| `{"verdicts": {"label": LABEL, "at_least": N, "since": EVENT?}}` | at least N verdicts with that label |
| `{"elapsed_ms": {"at_least": N, "since": EVENT?}}` | the episode clock reached N ms (since the event fired, if given) |
| `{"fired": EVENT}` | that event has fired |
| `{"count": {"of": [T, ...], "at_least": N}}` | at least N listed triggers hold now; zero always holds, more than the list never holds |
| `{"all": [T, ...]}`, `{"any": [T, ...]}`, `{"not": T}` | as named |

`since` counts from the moment the named event fired, and is false until it
has: counters from their values at that moment, the clock from its reading
then, and watcher-derived counts from the first batch after the mark the
runner took when the event fired. `OP` is one of `lt`, `le`, `gt`, `ge`,
`eq`, `ne`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .worklog import Pos, WorkspaceLog, glob_match, glob_problem

VERDICT_LABELS = ("answerable", "already_given", "unanswerable", "no_request")
OPS = {"lt": lambda a, b: a < b, "le": lambda a, b: a <= b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
       "eq": lambda a, b: a == b, "ne": lambda a, b: a != b}
COUNTERS = ("turns", "handbacks", "outputs", "elapsed_ms")
KINDS = ("path_exists", "content_matches", "changes", "metric", *COUNTERS, "verdicts", "fired", "all", "any", "count", "not")


@dataclass(frozen=True)
class Snapshot:
    """The counters at the moment an event fired, for `since`."""

    turns: int
    handbacks: int
    outputs: int
    verdicts: dict[str, int]
    elapsed_ms: int
    pos: Pos  # the watcher position of the event's mark
    skip: frozenset = frozenset()  # G6: a batch straddling the event's boundary counts on neither side


@dataclass
class Context:
    turns: int = 0
    handbacks: int = 0
    outputs: int = 0
    verdicts: Counter[str] = field(default_factory=Counter)
    elapsed_ms: int = 0
    work: WorkspaceLog = field(default_factory=WorkspaceLog)
    fired: dict[str, Snapshot] = field(default_factory=dict)

    def snapshot(self, elapsed_ms: int, pos: Pos, skip: frozenset = frozenset()) -> Snapshot:
        return Snapshot(self.turns, self.handbacks, self.outputs, dict(self.verdicts), elapsed_ms, pos, skip)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


LIMIT = 2**53  # integers beyond this are refused: JSON readers disagree past it, and float comparison overflows


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= LIMIT


def _num(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -LIMIT <= v <= LIMIT
    return isinstance(v, float) and math.isfinite(v)


def check(t: Any, path: str, names: set[str], err: list[str], deps: set[str], *, has_metric: bool = True, depth: int = 0) -> None:
    """Append `path: problem` lines to `err`; collect the event names the
    trigger depends on into `deps`. Never raises, whatever `t` is."""
    if depth > 32:
        err.append(f"{path}: nested too deep")
        return
    if not isinstance(t, dict) or len(t) != 1:
        err.append(f"{path}: a trigger is an object with exactly one key")
        return
    (k, v), = t.items()
    p = f"{path}.{k}"

    def keys(obj: Any, required: set[str], optional: set[str] = set()) -> bool:
        if not isinstance(obj, dict):
            err.append(f"{p}: an object")
            return False
        missing, extra = required - set(obj), set(obj) - required - optional
        if missing:
            err.append(f"{p}: missing {sorted(missing)}")
        if extra:
            err.append(f"{p}: unknown {sorted(extra)}")
        return not missing and not extra

    def event_name(x: Any, where: str) -> None:
        if not isinstance(x, str):
            err.append(f"{where}: an event name")
        elif x not in names:
            err.append(f"{where}: {x!r} is not an event of this task")
        else:
            deps.add(x)

    def since(obj: dict[str, Any]) -> None:
        if "since" in obj:
            event_name(obj["since"], f"{p}.since")

    def at_least(obj: dict[str, Any]) -> None:
        if not _int(obj.get("at_least")):
            err.append(f"{p}.at_least: a non-negative integer of at most {LIMIT}")

    def glob(g: Any, where: str) -> None:
        why = glob_problem(g) if isinstance(g, str) else "a relative glob"
        if why:
            err.append(f"{where}: {why}")

    if k == "path_exists":
        glob(v, p)
    elif k == "content_matches":
        if keys(v, {"path", "regex"}):
            glob(v["path"], f"{p}.path")
            regex_problem(v["regex"], f"{p}.regex", err)
    elif k == "changes":
        if keys(v, {"path", "at_least"}, {"since", "unit", "exclude"}):
            if v.get("unit", "record") not in ("record", "batch"):
                err.append(f"{p}.unit: record or batch")
            glob(v["path"], f"{p}.path")
            if "exclude" in v:
                if not isinstance(v["exclude"], list):
                    err.append(f"{p}.exclude: a list of non-empty globs")
                else:
                    for n, pattern in enumerate(v["exclude"]):
                        glob(pattern, f"{p}.exclude[{n}]")
            at_least(v)
            since(v)
    elif k == "metric":
        if not has_metric:
            err.append(f"{p}: the task has no metric command (watcher.metric is null)")
        if keys(v, {"name", "op", "value"}, {"reading", "since"}):
            if not isinstance(v["name"], str) or not v["name"]:
                err.append(f"{p}.name: a metric name")
            if not isinstance(v["op"], str) or v["op"] not in OPS:
                err.append(f"{p}.op: one of {sorted(OPS)}")
            if not _num(v["value"]):
                err.append(f"{p}.value: a finite number of magnitude at most {LIMIT}")
            if not isinstance(v.get("reading", "latest"), str) or v.get("reading", "latest") not in ("latest", "any"):
                err.append(f"{p}.reading: latest or any")
            since(v)
    elif k in COUNTERS:
        if keys(v, {"at_least"}, {"since"}):
            at_least(v)
            since(v)
    elif k == "verdicts":
        if keys(v, {"label", "at_least"}, {"since"}):
            if not isinstance(v["label"], str) or v["label"] not in VERDICT_LABELS:
                err.append(f"{p}.label: one of {VERDICT_LABELS}")
            at_least(v)
            since(v)
    elif k == "fired":
        event_name(v, p)
    elif k in ("all", "any"):
        if not isinstance(v, list) or not v:
            err.append(f"{p}: a non-empty list of triggers")
        else:
            for n, sub in enumerate(v):
                check(sub, f"{p}[{n}]", names, err, deps, has_metric=has_metric, depth=depth + 1)
    elif k == "count":
        if keys(v, {"of", "at_least"}):
            at_least(v)
            if not isinstance(v["of"], list):
                err.append(f"{p}.of: a list of triggers")
            else:
                for n, sub in enumerate(v["of"]):
                    check(sub, f"{p}.of[{n}]", names, err, deps, has_metric=has_metric, depth=depth + 1)
    elif k == "not":
        check(v, p, names, err, deps, has_metric=has_metric, depth=depth + 1)
    else:
        err.append(f"{path}: unknown trigger {k!r}")


def regex_problem(r: Any, path: str, err: list[str], groups: int | None = None) -> None:
    if not isinstance(r, str) or not r:
        err.append(f"{path}: a regular expression")
        return
    try:
        rx = re.compile(r)
    except (re.error, RecursionError, OverflowError) as exc:
        err.append(f"{path}: does not compile ({exc})")
        return
    if groups is not None and rx.groups != groups:
        err.append(f"{path}: must have exactly {groups} group(s), has {rx.groups}")


def depends_on(t: dict[str, Any]) -> set[str]:
    """Every event a valid trigger names through `fired` or `since`, anywhere
    in it (for cycle checks)."""
    return _names(t)


def prerequisites(t: Any) -> set[str]:
    """The events a valid trigger cannot hold without: its positive hard
    dependencies. `fired` and `since` name one; `all` needs each branch's;
    `any` needs only what every branch needs; nothing inside `not` is one."""
    if not isinstance(t, dict) or len(t) != 1:
        return set()
    (k, v), = t.items()
    if k == "fired":
        return {v}
    if k == "all":
        return set().union(*(prerequisites(s) for s in v))
    if k == "any":
        sets = [prerequisites(s) for s in v]
        return set.intersection(*sets) if sets else set()
    if k == "count":
        if v["at_least"] == 0 or v["at_least"] > len(v["of"]):
            return set()
        sets = [prerequisites(s) for s in v["of"]]
        # A name is required when too few branches can hold without it.
        return {name for name in set().union(*sets) if sum(name not in s for s in sets) < v["at_least"]}
    if k == "not":
        return set()
    return {v["since"]} if isinstance(v, dict) and "since" in v else set()


def _names(t: Any) -> set[str]:
    if not isinstance(t, dict) or len(t) != 1:
        return set()
    (k, v), = t.items()
    if k == "fired":
        return {v}
    if k in ("all", "any"):
        return set().union(*(_names(s) for s in v))
    if k == "count":
        return set().union(*(_names(s) for s in v["of"]))
    if k == "not":
        return _names(v)
    return {v["since"]} if isinstance(v, dict) and "since" in v else set()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def holds(t: dict[str, Any], ctx: Context) -> bool:
    (k, v), = t.items()
    if k == "path_exists":
        return any(glob_match(v, p) for p in ctx.work.paths())
    if k == "content_matches":
        rx = re.compile(v["regex"], re.MULTILINE)
        for p in ctx.work.files():
            if glob_match(v["path"], p):
                text = ctx.work.content(p)
                if text is not None and rx.search(text):
                    return True
        return False
    if k == "fired":
        return v in ctx.fired
    if k == "all":
        return all(holds(s, ctx) for s in v)
    if k == "any":
        return any(holds(s, ctx) for s in v)
    if k == "count":
        return sum(holds(s, ctx) for s in v["of"]) >= v["at_least"]
    if k == "not":
        return not holds(v, ctx)
    base = ctx.fired.get(v["since"]) if "since" in v else None
    if "since" in v and base is None:
        return False
    if k == "changes":
        matches = [c for c in ctx.work.changes if (base is None or (c.pos > base.pos and c.pos not in base.skip))
                   and any(glob_match(v["path"], x) and not any(glob_match(g, x) for g in v.get("exclude", [])) for x in c.paths)]
        count = len({c.pos for c in matches}) if v.get("unit", "record") == "batch" else len(matches)
        return count >= v["at_least"]
    if k == "metric":
        vals = [r.metrics[v["name"]] for r in ctx.work.readings if (base is None or (r.pos > base.pos and r.pos not in base.skip)) and _num(r.metrics.get(v["name"]))]
        if not vals:
            return False
        op = OPS[v["op"]]
        if v.get("reading", "latest") == "latest":
            return op(vals[-1], v["value"])
        return any(op(x, v["value"]) for x in vals)
    if k == "verdicts":
        return ctx.verdicts[v["label"]] - (base.verdicts.get(v["label"], 0) if base else 0) >= v["at_least"]
    if k in COUNTERS:
        now = getattr(ctx, k)
        return now - (getattr(base, k) if base else 0) >= v["at_least"]
    raise ValueError(f"unknown trigger {k!r}")
