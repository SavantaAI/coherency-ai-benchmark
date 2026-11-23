"""The episode directory: staged, then published by rename.

    <out>/<episode>/                   usable: finished or budget cutoff, every required event fired, export accepted
    <out>/<episode>.not_reached/       as usable, except a required event never fired
    <out>/<episode>.failed-<reason>/   anything else, with FAILED.json saying why
    <out>/.staging-<episode>-<hex>/    while the episode runs; left behind only if the process dies

      run.json             run manifest: harness and version, T3 and driver versions, model, mode, task hash, timings, budgets, the end
      index.json           each item of the root README section 3 "Logging" -> the files that hold it
      runner/events.jsonl  the runner's event log, written as it happens
      transcript.md        the user-visible transcript, readable
      transcript.jsonl     the same, one line per message
      classifier.jsonl     every verdict with the policy's record, and the awareness classification
      episode-record.json  the runner's episode record (the bundle's episode.json is made from it)
      driver/              the driver's working files
      export/              the driver's export: raw record, full trajectory, scoring bundle
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from .policy import USER

# the root README section 3 "Logging", item by item, and the runner's own records.
LOGGING = {
    "trajectory": "exported trajectory",
    "transcript": "user-visible transcript",
    "identifier_registry": "timestamped identifier registry",
    "classifier_labels": "classifier labels",
    "user_wait": "user-wait time",
    "file_diffs": "file diffs with metric readings",
    "probe_answers": "probe answers",
    "driver_tool_calls": "calls to driver tools",
    "harness": "harness name and version",
    "driver_version": "driver version",
    "scoring_bundle": "scoring bundle (D-018)",
    "runner_events": "runner event log",
    "raw_record": "raw driver record",
}

RUNNER_FILES = {
    "transcript": ["transcript.md", "transcript.jsonl"],
    "classifier_labels": ["classifier.jsonl"],
    "user_wait": ["runner/events.jsonl", "run.json"],
    "probe_answers": ["episode-record.json"],
    "harness": ["run.json"],
    "driver_version": ["run.json"],
    "runner_events": ["runner/events.jsonl"],
}

OPAQUE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")


class EpisodeDir:
    def __init__(self, out: Path, episode: str):
        if not OPAQUE.match(episode):
            raise ValueError("episode id: 1 to 64 letters, digits, - and _")
        self.out = Path(out)
        self.episode = episode
        self.final = self.out / episode
        if self.final.exists():
            raise FileExistsError(f"{self.final} already exists")
        self.out.mkdir(parents=True, exist_ok=True)
        self.staging = self.out / f".staging-{episode}-{secrets.token_hex(4)}"
        self.staging.mkdir()
        self.published: Path | None = None

    def path(self, rel: str) -> Path:
        return self.staging / rel

    def write_transcript(self, lines: list[Any]) -> None:
        """The user-visible transcript in order of delivery. The post-run
        awareness exchange is not part of it."""
        rows = []
        out = ["# Transcript", "", f"Episode {self.episode}. What the user wrote and what reached the user, in order of delivery. Nothing else the agent did is shown.", ""]
        for n, line in enumerate(lines, 1):
            e, m = line.entry, line.meta
            rows.append({"n": n, "role": e.role, "text": e.text, **m})
            ms = m.get("elapsed_ms") or 0
            when = f"{ms // 60000:02d}:{(ms % 60000) / 1000:06.3f}"
            if e.role == USER:
                head = f"## {n}. User ({m.get('shown_as') or m.get('kind')}), {when}"
            else:
                head = f"## {n}. Agent ({m.get('output_kind')}), {when}, verdict {m.get('label')}"
            out += [head, "", e.text, ""]
        _write_jsonl(self.path("transcript.jsonl"), rows)
        self.path("transcript.md").write_text("\n".join(out), encoding="utf-8")

    def write_index(self, export_files: dict[str, list[str]]) -> dict[str, Any]:
        items = []
        for key, label in LOGGING.items():
            own = list(RUNNER_FILES.get(key, []))
            exported = [f"export/{p}" for p in export_files.get(key, [])]
            files = own + exported
            # A runner file is present when it exists (an episode with no output has an empty classifier log);
            # an export file must also hold something.
            present = bool(files) and all(self.path(f).exists() for f in own) and all(self._nonempty(self.path(f)) for f in exported)
            items.append({"item": key, "spec": label, "files": files, "present": present})
        doc = {"format": "cohbench-episode-index", "version": 1, "episode": self.episode, "items": items}
        _write_json(self.path("index.json"), doc)
        return doc

    @staticmethod
    def _nonempty(p: Path) -> bool:
        if p.is_dir():
            return any(p.iterdir())
        return p.is_file() and p.stat().st_size > 0

    def publish(self, status: str, failure: str | None = None, detail: str | None = None) -> Path:
        if status == "usable":
            dest = self.final
        elif status == "not_reached":
            dest = self.out / f"{self.episode}.not_reached"
            n = 2
            while dest.exists():
                dest = self.out / f"{self.episode}.not_reached-{n}"
                n += 1
        else:
            failure = failure or "unknown"
            slug = re.sub(r"[^a-z0-9_]+", "_", failure.lower()).strip("_")[:40] or "unknown"
            dest = self.out / f"{self.episode}.failed-{slug}"
            n = 2
            while dest.exists():
                dest = self.out / f"{self.episode}.failed-{slug}-{n}"
                n += 1
            _write_json(self.path("FAILED.json"), {"episode": self.episode, "reason": failure, "detail": detail})
        os.replace(self.staging, dest)
        self.published = dest
        return dest
