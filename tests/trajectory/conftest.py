from __future__ import annotations

import copy
import glob
import json
from pathlib import Path

import pytest

from cohbench.trajectory import Hints, Segment, normalise, t3
from cohbench.trajectory.adapters import ClaudeCodeProfile, PiProfile

FIX = Path(__file__).parent / "fixtures"


REAL = FIX / "watcher-real"


def _shift(ts, delta):
    from datetime import datetime, timezone

    if not isinstance(ts, str):
        return ts
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")) + delta
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def real_watcher(segment: int, start_utc: str | None = None):
    """Segment 1 or 2 of the real watcher fixture as a WatcherInput, every
    time moved so that the fixture's episode start falls on `start_utc`
    (unchanged when it is None). Also returns the sequence (its times moved
    the same way)."""
    from datetime import datetime

    from cohbench.trajectory import changelog
    from cohbench.trajectory.bundle import WatcherInput

    seq = json.loads((REAL / "sequence.json").read_text())
    delta = (datetime.fromisoformat(start_utc.replace("Z", "+00:00")) - datetime.fromisoformat(seq["start_utc"].replace("Z", "+00:00"))) if start_utc else None
    move = (lambda x: _shift(x, delta)) if delta is not None else (lambda x: x)  # noqa: E731
    events, metrics, status = changelog.read_dir(REAL / f"segment-{segment}")
    events = [{**r, **{k: move(r[k]) for k in ("wall", "first_event", "last_event", "first_write", "last_write") if k in r and r[k] is not None}} for r in events]
    metrics = [{**m, **{k: move(m[k]) for k in ("start", "end") if k in m}} for m in metrics]
    seq = {**seq, "start_utc": move(seq["start_utc"]), "end_utc": move(seq["end_utc"]),
           "excluded": [{**i, "start_utc": move(i["start_utc"]), "end_utc": move(i["end_utc"])} for i in seq["excluded"]],
           "steps": [{**x, "utc": move(x["utc"])} for x in seq["steps"]]}
    return WatcherInput(events, metrics, REAL / f"segment-{segment}" / "blobs", status), seq


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def sessions(profile, name: str) -> list:
    return [profile.read_session(f) for f in sorted(glob.glob(str(FIX / "sessions" / name / "*.jsonl")))]


def export_segment(name: str, session_dir: str | None = None, profile=None, obj: dict | None = None) -> Segment:
    profile = profile or PiProfile()
    threads = t3.from_export(copy.deepcopy(obj) if obj is not None else load(name))
    return Segment(threads, None, sessions(profile, session_dir) if session_dir else [])


def snapshot_segment(name: str, session_dir: str | None = None, transcript: str | None = None, profile=None) -> Segment:
    profile = profile or PiProfile()
    threads = t3.from_snapshot(load(name), load(transcript) if transcript else None)
    return Segment(threads, None, sessions(profile, session_dir) if session_dir else [])


def run(segments, profile=None, hints: Hints | None = None):
    return normalise(segments, profile or PiProfile(), hints)


def events(result, etype: str | None = None, **match):
    out = [e for e in result.trajectory.events if etype is None or e["type"] == etype]
    return [e for e in out if all(e.get(k) == v for k, v in match.items())]


@pytest.fixture
def pi():
    return PiProfile()


@pytest.fixture
def claude():
    return ClaudeCodeProfile()
