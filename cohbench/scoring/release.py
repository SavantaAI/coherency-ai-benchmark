"""Verify release calibration attestations without reading private labels."""

from datetime import date
import hashlib
import json
import math
import re
from pathlib import Path

from cohbench import endpoint
from .calibration import QUESTION_SOURCE, validate_record


def digest(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def human_errors(row, minimum, models, label):
    errors = []
    if not isinstance(row, dict):
        return [f"{label} human calibration is missing"]
    n, correct, kappa = row.get("n"), row.get("correct"), row.get("kappa")
    if not (
        type(n) is int
        and n >= minimum
        and type(correct) is int
        and 0 <= correct <= n
        and type(kappa) in (int, float)
        and math.isfinite(kappa)
        and 0.75 <= kappa <= correct / n
    ):
        errors.append(f"{label} human counts or kappa below required bar")
    who = row.get("annotators")
    if (
        not isinstance(who, list)
        or len(who) != 2
        or any(not isinstance(x, str) or not x.strip() for x in who)
        or len(set(who)) != 2
        or row.get("independent") is not True
    ):
        errors.append(f"{label} needs two independent named annotators")
    try:
        date.fromisoformat(row["date"])
    except (KeyError, ValueError, TypeError):
        errors.append(f"{label} human calibration needs an absolute date")
    rates = row.get("disagreement_by_model")
    if (
        not isinstance(rates, dict)
        or not models.issubset(rates)
        or not rates
        or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
            for v in rates.values()
        )
    ):
        errors.append(f"{label} disagreement rates do not cover the ranked models")
    elif max(rates.values()) - min(rates.values()) > 0.05 + 1e-12:
        errors.append(
            f"{label} disagreement rates differ by more than five percentage points"
        )
    return errors


def check(path, scores, index):
    if path is None:
        return {
            "passed": False,
            "reasons": ["calibration manifest is required for a release"],
        }
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(
            raw,
            object_pairs_hook=endpoint._no_duplicates,
            parse_constant=endpoint._no_constant,
        )
    except (OSError, ValueError, UnicodeDecodeError):
        return {
            "passed": False,
            "reasons": ["calibration manifest cannot be read as strict JSON"],
        }
    errors = []
    if (
        not isinstance(doc, dict)
        or doc.get("format") != "cohbench-calibration-manifest"
        or type(doc.get("version")) is not int
        or doc.get("version") != 1
    ):
        return {"passed": False, "reasons": ["unsupported calibration manifest format"]}
    used = [d for d in scores if not d.get("rerun_required")]
    readers = {
        (d.get("reader") or {}).get("config_sha256") for d in used if d.get("reader")
    }
    classifiers = {d.get("classifier_config_sha256") for d in used}
    rd, cl = doc.get("reader", {}), doc.get("classifier", {})
    if not isinstance(rd, dict) or not isinstance(cl, dict):
        return {
            "passed": False,
            "reasons": ["calibration manifest needs reader and classifier records"],
        }
    if (
        not digest(rd.get("config_sha256"))
        or not readers
        or any(not digest(x) for x in readers)
        or readers != {rd.get("config_sha256")}
    ):
        errors.append("reader calibration digest does not match the score files")
    if (
        not digest(cl.get("config_sha256"))
        or not classifiers
        or any(not digest(x) for x in classifiers)
        or classifiers != {cl.get("config_sha256")}
    ):
        errors.append(
            "classifier calibration digest is missing or does not match the score files"
        )
    # The calibration read the generator's main question; a score that read
    # another question, or does not say which, is outside what it measured.
    sources = sorted(
        {
            src if isinstance(src, str) else "not recorded" if src is None else json.dumps(src)
            for d in used
            if d.get("reader")
            for src in [d.get("reader_question_source")]
        }
        - {QUESTION_SOURCE}
    )
    if sources:
        errors.append(
            f"reader scores carry question source {', '.join(sources)}; "
            f"a release needs {QUESTION_SOURCE}, the source the reader calibration used"
        )
    if not validate_record(rd.get("large_sample"), rd.get("config_sha256")):
        errors.append(
            "large-sample reader calibration is missing, mismatched or below its bars"
        )
    models = {index[d["episode"]]["model"] for d in used}
    for label, row, minimum in [("reader", rd, 100), ("classifier", cl, 150)]:
        errors.extend(human_errors(row.get("human"), minimum, models, label))
    timed = rd.get("timed_humans", {})
    if not isinstance(timed, dict):
        timed = {}
    if (
        type(timed.get("n")) is not int
        or timed["n"] < 1
        or timed.get("disagreements_resolved") is not True
        or not isinstance(timed.get("who"), list)
        or not timed["who"]
        or any(not isinstance(x, str) or not x.strip() for x in timed["who"])
        or not isinstance(timed.get("context"), str)
        or not timed["context"].strip()
    ):
        errors.append("timed human reader calibration is missing or unresolved")
    try:
        date.fromisoformat(timed["date"])
    except (KeyError, ValueError, TypeError):
        errors.append("timed human reader calibration needs an absolute date")
    return {
        "passed": not errors,
        "reasons": errors,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }
