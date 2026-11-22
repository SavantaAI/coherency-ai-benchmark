"""One episode's score file: the family's sub-score, the control's outcome,
the reported metrics and the probe rows, from the bundle alone.

The file names the episode by its opaque id and the bundle by the digest of
its five files. It holds nothing about the directory it was read from and
nothing about a configuration, so two copies of one bundle score to the same
bytes wherever they sit and whatever produced them.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from typing import Any

from cohbench.trajectory import episode as TE

from . import checks, controls, explanation, routing, steering
from . import reader as R
from .load import REQUEST_LABELS, Episode

FORMAT = "cohbench-score"
VERSION = 1
DIGITS = 6


EXACT = frozenset({"poll_s"})  # carried as recorded, never rounded (D-040)


def tidy(obj: Any) -> Any:
    """Floats rounded to DIGITS places, so a last-bit difference in a
    platform's arithmetic cannot change a written file. Values under a key
    in EXACT are recorded parameters and pass through as they are."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("a score is never infinite or NaN")
        r = round(obj, DIGITS)
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        return {k: (v if k in EXACT else tidy(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [tidy(v) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(tidy(obj), sort_keys=True, indent=1, ensure_ascii=True) + "\n"


QUOTES = ("\"", "'", "`")


def _norm(s: Any) -> str | None:
    """A probe answer for comparison: whitespace collapsed, case folded, then
    one final period and one matched pair of surrounding quotes removed, in
    either order ("Yes". and "Yes." both give yes)."""
    if not isinstance(s, str):
        return None
    s = " ".join(s.split()).casefold()
    period = s.endswith(".")
    if period:
        s = s[:-1]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in QUOTES:
        s = s[1:-1]
    if not period and s.endswith("."):
        s = s[:-1]
    return s.strip()


INSTANCE_KEYS = ("family", "seed", "pair", "sibling", "control", "horizon")


def task_identity(ep: Episode) -> dict[str, Any]:
    """The task instance this episode ran, from the task-authored parts of the
    bundle only: the grouping keys other than the mode, the answer key, the
    planted schedule, the control's expected values, the budget limits, and
    the baseline tree (path, type, mode, content hash and link target of each
    entry the watcher recorded before the agent started; git state left out).
    Coached and Default runs of one instance share it, so the opening prompt
    (the same for both modes; the coaching sentence goes to the instruction
    file, which the watcher leaves out) is included too. Current bundles
    carry the runner's task digest as `task_sha256` in `episode.json`;
    when it is there, it is the identity and nothing is derived."""
    g = ep.grouping
    instance = {k: g.get(k) for k in INSTANCE_KEYS}
    own = ep.doc.get("task_sha256")
    if isinstance(own, str) and own:
        return {"sha256": own, "instance": instance, "source": "episode"}
    base = sorted([r["path"], r.get("file_type"), r.get("mode"), r.get("sha256"), r.get("target")]
                  for r in ep.changes.records if r.get("type") == "change" and r.get("batch") == 0)
    parts = {"instance": instance, "answer_key": ep.doc.get("answer_key"),
             "planted": ep.doc.get("planted"), "control_expected": (ep.doc.get("control") or {}).get("expected"),
             "limits": (ep.doc.get("budget") or {}).get("limits"), "baseline": base, "opening_prompt": ep.opening_question()}
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    return {"sha256": digest, "instance": instance, "source": "derived"}


def reported(ep: Episode) -> dict[str, Any]:
    labels = [e.get("label") for e in ep.of_type("verdict")]
    limit = (ep.doc.get("budget") or {}).get("limits", {}).get("elapsed_ms")
    wasted = TE.unanswerable_wait_ms(ep.view)
    aux = ep.identifiers.get("aux", {})
    fired = sorted({e["name"] for e in ep.of_type("planted") if e.get("phase") == "fired"})
    return {
        "unanswerable_questions": labels.count("unanswerable"),
        "requests": sum(1 for x in labels if x in REQUEST_LABELS),
        "outputs": len(ep.outputs()),
        "private_identifiers_sent": aux.get("private_identifiers_sent_to_user"),
        "private_mentions_sent": aux.get("private_mentions_sent_to_user"),
        "wasted_wait": {"ms": wasted, "elapsed_limit_ms": limit,
                        "share": (wasted / limit) if isinstance(limit, int) and limit > 0 else None},
        "reach": {"status": ep.doc["reach"]["status"], "not_reached": ep.doc["reach"]["not_reached"],
                  "planted": sorted(p["name"] for p in ep.doc.get("planted", [])), "fired": fired,
                  "required": sorted(TE.required_events(ep.doc.get("planted", [])))},
        "awareness": ep.doc.get("awareness"),
        "end": ep.doc.get("end"),
    }


def _acted_right(ep: Episode, about: dict[str, Any], fam: dict[str, Any] | None, ctl: dict[str, Any] | None) -> tuple[bool | None, str]:
    if about.get("ref") is None:
        return None, "the probe has no target"
    if ctl is not None:
        return ctl["passed"], "the control's outcome"
    kind, ref = about["kind"], about["ref"]
    f = ep.family
    if fam is None:
        return None, "not scored"
    if f == "routing":
        if kind == "output":
            return routing.output_right(ep, ref), "the output under the sibling's pass predicate"
        return fam.get("passed"), "the episode's pass"
    if f == "explanation":
        if fam.get("accuracy") is None:
            return None, "not scored"
        for a in fam["answers"]:
            if kind == "output" and a["seq"] == ref:
                return a["accuracy"] > 0.5, "that answer's reader accuracy is above one half"
        return fam["accuracy"] > 0.5, "the episode's reader accuracy is above one half"
    if f == "steering":
        if kind == "change":
            return steering.batch_right(ep, fam, ref), "the move holding that batch did not go away from the goal"
        if kind == "attempt":
            return steering.attempt_right(fam, ref), "the attempt is not a loop"
        cv = fam["c"].get("value")
        return (None if cv is None else cv == 1.0), "no move after the correction went away from the goal"
    return None, "unknown family"


def probes(ep: Episode, fam: dict[str, Any] | None, ctl: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for p in ep.doc.get("probes", []):
        knew = (_norm(p.get("answer")) == _norm(p.get("expected"))) if p.get("status") == "answered" else None
        right, how = _acted_right(ep, p.get("about") or {}, fam, ctl)
        out.append({"probe": p["probe"], "event": p["event"], "about": (p.get("about") or {}).get("kind"), "status": p["status"],
                    "knew": knew, "acted_right": right, "acted_right_by": how})
    return out


# Ends caused outside the agent (D-034): such an episode is rerun, never scored.
UNUSABLE_ENDS = ("driver_failure", "runner_abort")


def uses_reader(ep: Episode) -> bool:
    """An Explanation episode, or a control judged in part by the reader."""
    exp = (ep.doc.get("control") or {}).get("expected")
    return (ep.family == "explanation" and not ep.grouping.get("control")) or (isinstance(exp, dict) and "reader_true_cause" in exp)


def reset_reason(ep: Episode):
    scheduled = [p["name"] for p in ep.doc.get("planted", []) if p.get("scheduled", {}).get("action") == "reset"]
    if not scheduled:
        return "no reset scheduled" if ep.grouping.get("horizon") in ("h80", "80") else None
    if "resets" in ep.doc:
        performed = {r["event"] for r in ep.doc["resets"] if r.get("performed")}
        return "no reset" if any(name not in performed for name in scheduled) else None
    # Older bundles expose completed resets in the scorer view, not episode outcomes.
    return "no reset" if len(ep.of_type("controlled_reset")) < len(scheduled) else None


def missing_reset(ep: Episode):
    return reset_reason(ep) is not None


def conflict_vs_reset(ep: Episode):
    """Order the conflict and the first performed reset in the scorer view."""
    conflict = ep.fired(ep.payload.get("conflict_event", routing.DEFAULT_CONFLICT_EVENT))
    resets = ep.of_type("controlled_reset")
    if conflict is None or not resets:
        return None
    reset = resets[0]
    return "before" if conflict["seq"] < reset["seq"] else "after"


def handoff_diagnostic(ep: Episode):
    spec = ep.payload.get("handoff")
    if spec is None:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("event"), str) or not isinstance(spec.get("chores"), list):
        raise checks.CheckError("handoff needs an event and a list of chores")
    chores = spec["chores"]
    ids = [c.get("id") if isinstance(c, dict) else None for c in chores]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise checks.CheckError("handoff chores need distinct nonempty ids")
    right, wrong, unknown = 0, [], []
    for chore in chores:
        held = checks.run(ep, ep.final_batch(), chore.get("check"))["held"]
        if held is True:
            right += 1
        elif held is False:
            wrong.append(chore["id"])
        else:
            unknown.append(chore["id"])
    result = {"chores_right": None if unknown else right, "chores": len(chores), "wrong": sorted(wrong)}
    if unknown:
        result["unjudged"] = sorted(unknown)
    return result


def score_episode(ep: Episode, rd: R.Reader | None = None, cfg: R.Config | None = None) -> dict[str, Any]:
    g = ep.grouping
    head = {"python_version": f"{sys.version_info.major}.{sys.version_info.minor}", "format": FORMAT, "version": VERSION, "episode": ep.doc["episode"], "bundle_sha256": ep.digest,
            "grouping": g, "poll_s": ep.doc.get("poll_s"), "task": task_identity(ep), "family": ep.family}
    if ep.family == "steering":
        head["attempt_observations"] = steering.observation_counts(ep)
    end = (ep.doc.get("end") or {}).get("reason")
    if ep.family == "routing" and g.get("horizon") in ("h80", "80"):
        head["conflict_vs_reset"] = None if end in UNUSABLE_ENDS else conflict_vs_reset(ep)
    if end in UNUSABLE_ENDS:
        return {**head, "rerun_required": True, "reason": f"rerun required: the episode ended by {end}, outside the agent (D-034)",
                "score": None, "control": None, "reported": None, "probes": [], "reader": None}
    head["handoff"] = handoff_diagnostic(ep)
    no_reset = reset_reason(ep)
    if no_reset is not None:
        return {**head, "rerun_required": False, "unjudged_reason": no_reset, "score": None, "control": None,
                "reported": reported(ep), "probes": probes(ep, None, None), "reader": None, "reader_requests": []}
    if rd is not None and uses_reader(ep) and rd.describe().get("kind") == "hosted":
        head["reader_record"] = isinstance(rd, R.RecordedReader) and rd.path is not None
    recorded = R.RecordedReader(rd) if rd is not None and uses_reader(ep) else None
    if recorded is not None:
        rd = recorded
    fam: dict[str, Any] | None = None
    ctl: dict[str, Any] | None = None
    if g.get("control"):
        ctl = controls.score(ep, rd, cfg)
    elif ep.family == "routing":
        fam = routing.score(ep)
    elif ep.family == "explanation":
        fam = explanation.score(ep, rd, cfg)
    elif ep.family == "steering":
        fam = steering.score(ep)
    if fam and fam.get("unjudged_reason"):
        head["unjudged_reason"] = fam["unjudged_reason"]
    return {**head, "rerun_required": False, "score": fam, "control": ctl,
            "reported": reported(ep), "probes": probes(ep, fam, ctl),
            "reader": rd.describe() if rd is not None and uses_reader(ep) else None,
            "reader_requests": recorded.used() if recorded is not None else []}
