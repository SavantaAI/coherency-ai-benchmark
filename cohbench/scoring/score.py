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
from typing import Any

from cohbench.trajectory import episode as TE

from . import controls, explanation, routing, steering
from . import reader as R
from .load import REQUEST_LABELS, Episode

FORMAT = "cohbench-score"
VERSION = 1
DIGITS = 6


def tidy(obj: Any) -> Any:
    """Floats rounded to DIGITS places, so a last-bit difference in a
    platform's arithmetic cannot change a written file."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("a score is never infinite or NaN")
        r = round(obj, DIGITS)
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        return {k: tidy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [tidy(v) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(tidy(obj), sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def _norm(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    s = " ".join(s.split()).casefold().strip().strip("\"'`").rstrip(".").strip()
    return s


INSTANCE_KEYS = ("family", "seed", "pair", "sibling", "control", "horizon")


def task_identity(ep: Episode) -> dict[str, Any]:
    """The task instance this episode ran, from the task-authored parts of the
    bundle only: the grouping keys other than the mode, the answer key, the
    planted schedule, the control's expected values, the budget limits, and
    the baseline tree (path, type, mode, content hash and link target of each
    entry the watcher recorded before the agent started; git state left out).
    Coached and Default runs of one instance share it. The episode record has
    no task digest of its own; this stands in for one."""
    g = ep.grouping
    base = sorted([r["path"], r.get("file_type"), r.get("mode"), r.get("sha256"), r.get("target")]
                  for r in ep.changes.records if r.get("type") == "change" and r.get("batch") == 0)
    parts = {"instance": {k: g.get(k) for k in INSTANCE_KEYS}, "answer_key": ep.doc.get("answer_key"),
             "planted": ep.doc.get("planted"), "control_expected": (ep.doc.get("control") or {}).get("expected"),
             "limits": (ep.doc.get("budget") or {}).get("limits"), "baseline": base}
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    return {"sha256": digest, "instance": parts["instance"]}


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
                  "planted": sorted(p["name"] for p in ep.doc.get("planted", [])), "fired": fired},
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


def score_episode(ep: Episode, rd: R.Reader | None = None, cfg: R.Config | None = None) -> dict[str, Any]:
    g = ep.grouping
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
    return {
        "format": FORMAT, "version": VERSION,
        "episode": ep.doc["episode"], "bundle_sha256": ep.digest,
        "grouping": g, "poll_s": ep.doc.get("poll_s"), "task": task_identity(ep),
        "family": ep.family, "score": fam, "control": ctl,
        "reported": reported(ep), "probes": probes(ep, fam, ctl),
        "reader": rd.describe() if rd is not None and ep.family == "explanation" else None,
    }
