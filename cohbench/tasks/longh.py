"""The long horizon (D-046): one opening message with the filler chores
first and the main job after them; a reset notice sent into the running turn
once a seeded number of chores are done; a fresh context that opens with
"Picking this back up. Continue where you left off." and nothing else.

This module builds that shape's task parts and turns a family's short-
horizon scripts into long-horizon ones:

- ideal: on the notice it stops (its first handler abandons on a steer),
  writes NOTES.md with the whole job and which chores are done, checked
  against their results, and hands back; after the reset it reads the notes,
  does the chores not ticked, then the main job;
- ideal, grace variant: on the notice it writes the same notes and keeps
  working without handing back, slowly enough that the runner's grace period
  (two minutes of episode time) runs out first; after the reset it reads its
  notes and carries on, tolerating work already done;
- worst: on the notice it writes notes that say nothing, and after the reset
  asks the user what it was doing, then does the main job without the chores
  that were left.
"""

from __future__ import annotations

from typing import Any

from . import fillers as F
from .common import NOTES_PATH, RESET_NOTICE, RESET_REOPEN, Options, bash, handler, write

CONTINUATION = "Picking this back up. Continue where you left off."
NOTES_DONE = f"I've left notes in {NOTES_PATH}: what you asked for, and which of the small jobs are done and which are left."
GRACE_S = 120  # the runner's default grace after a notice (D-046)
SLOW_TOTAL_S = 150  # the grace variant spreads its remaining work over at least this long


def reset_after(o: Options, n_fillers: int) -> int:
    """How many filler results must exist before the notice: at least half of
    the chores, and at least three chores left, so the notice falls before the
    main job for an agent that works through the list in order."""
    return o.rng("reset").between((n_fillers + 1) // 2, n_fillers - 3)


def chore_checks(fills):
    return [{"id": f.key, "check": {"path": path, "regex": F.result_regex(f, path, content)}}
            for f in fills for path, content in f.solution.items()]


def reset_event(at_least, fills, main_path):
    scope = {"path": main_path} if isinstance(main_path, str) else dict(main_path)
    return {"name": "handoff", "trigger": {"any": [
        {"count": {"of": [{"content_matches": c["check"]} for c in chore_checks(fills)], "at_least": at_least}},
        {"changes": {**scope, "at_least": 1}}]},
        "action": {"type": "reset", "notice": RESET_NOTICE, "continuation": CONTINUATION, "grace_ms": GRACE_S * 1000},
        "required": True, "reopen": RESET_REOPEN}


OPENERS = (
    "Hello! Here is today's project work. Start with these chores:",
    "Hi! Please work through this list today, beginning with the small jobs:",
    "Good morning! These project chores come first:",
    "Hello! I have a list for this session. Begin here:",
    "Hi there! Let's get these items done first:",
    "Morning! The first part of today's work is this list:",
)


def opening(o, fills, main):
    transition = o.rng("transition").choice((
        "Then, ", "After that, ", "Once those are done, ", "Next, ",
        "Following the chores, ", "For the remaining work, ",
    ))
    return o.rng("phrasing").choice(OPENERS) + "\n\n" + F.listed(fills) + "\n\n" + transition + main


def paced(fills):
    # The next tool call waits while the watcher delivers the reset notice.
    # A delay before every next chore also covers every seeded threshold.
    return [step for f in fills for step in [*f.steps(), {**bash("true"), "delay": 20}]]


# ---------------------------------------------------------------------------
# Script parts
# ---------------------------------------------------------------------------


def notes_steps(job: str, fills: list[F.Filler]) -> list[dict[str, Any]]:
    """Write NOTES.md: the job word for word and each chore ticked when its
    result is in place (checked against the allowed result representations)."""
    chores = []
    for n, f in enumerate(fills, 1):
        (path, content), = f.solution.items()
        chores.append((n, f.instruction, path, F.result_regex(f, path, content)))
    src = ("import re\nimport os\n"
           f"JOB = {job!r}\nCHORES = {chores!r}\n"
           "out = ['# Notes for picking this up', '', '## What was asked', '', JOB, '', '## The small chores, in the order asked', '']\n"
           "for n, text, path, pattern in CHORES:\n"
           "    done = os.path.isfile(path) and re.search(pattern, open(path, encoding='utf-8').read()) is not None\n"
           "    out.append('- [%s] %d. %s' % ('x' if done else ' ', n, text))\n"
           "out += ['', 'Next: the chores not ticked, in order, then the main job above.']\n"
           "open('NOTES.md', 'w').write('\\n'.join(out) + '\\n')\n")
    return [bash("python3 - <<'PY'\n" + src + "PY"), bash(f"cat {NOTES_PATH}")]


def read_notes(fills: list[F.Filler]) -> dict[str, Any]:
    caps = [{"var": f"done{n}", "regex": f"^- \\[x\\] {n}\\. ", "required": False} for n in range(1, len(fills) + 1)]
    return bash(f"cat {NOTES_PATH}", capture=caps)


def chores_left(fills: list[F.Filler]) -> list[dict[str, Any]]:
    return [{"do": "branch", "cases": [{"when": {"not": {"var_set": f"done{n}"}}, "steps": f.steps()}]} for n, f in enumerate(fills, 1)]


def _tool_steps(steps: list[dict[str, Any]]) -> int:
    """Tool calls that run whatever the notes say."""
    n = 0
    for s in steps:
        if s["do"] == "branch":
            continue  # a chore already done is skipped: count only the steps that always run
        elif s["do"] in ("bash", "read", "write", "edit", "tool", "parallel"):
            n += 1
    return n


def _slowed(steps: list[dict[str, Any]], delay: int) -> list[dict[str, Any]]:
    out = []
    for s in steps:
        if s["do"] == "branch":
            out.append({**s, "cases": [{**c, "steps": _slowed(c["steps"], delay)} for c in s["cases"]]})
        elif s["do"] in ("bash", "read", "write", "edit", "tool"):
            out.append({**s, "delay": delay})
        else:
            out.append(s)
    return out


def _resume(first: dict[str, Any], look: list[dict], main: list[dict], fills: list[F.Filler]) -> dict[str, Any]:
    h = {**first, "id": "resume", "steps": [read_notes(fills)] + chores_left(fills) + look + main,
         "expect": {"contains": "Picking this back up"}}
    return h


def ideal(handlers: list[dict[str, Any]], n_look: int, fills: list[F.Filler], job: str, *, resume_main: list[dict] | None = None,
          grace: bool = False) -> list[dict[str, Any]]:
    """`handlers` is the short-horizon ideal without fillers; `n_look` is how
    many of its first handler's steps are the opening look round."""
    first = handlers[0]
    look, main = first["steps"][:n_look], first["steps"][n_look:]
    h0 = {**first, "steps": look + paced(fills) + main, "on_steer": "abandon"}
    resume = _resume(first, look, resume_main if resume_main is not None else main, fills)
    if grace:
        rest = resume["steps"]
        delay = max(10, -(-SLOW_TOTAL_S // max(_tool_steps(rest), 1)))
        notes = handler("notes", notes_steps(job, fills) + _slowed(rest, delay), "That's the rest of the list and the main job done.",
                        expect="closing this session")
    else:
        notes = handler("notes", notes_steps(job, fills), NOTES_DONE, expect="closing this session")
    return [h0, notes, resume] + handlers[1:]


def worst(handlers: list[dict[str, Any]], n_look: int, fills: list[F.Filler], lost: str, lost_expect: str) -> list[dict[str, Any]]:
    first = handlers[0]
    look, main = first["steps"][:n_look], first["steps"][n_look:]
    h0 = {**first, "steps": look + paced(fills) + main, "on_steer": "abandon"}
    notes = handler("notes", [write(NOTES_PATH, "Notes\n\nWorking through the list.\n")], "Done.", expect="closing this session")
    ask = handler("lost", [bash(f"cat {NOTES_PATH}")], lost, end="question", expect="Picking this back up")
    cont = {**first, "id": "carry_on", "steps": look + main, "expect": {"contains": lost_expect}}
    return [h0, notes, ask, cont] + handlers[1:]
