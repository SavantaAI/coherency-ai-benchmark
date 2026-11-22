"""The task instance (contract 1): a directory a generator writes on the host.

    <instance>/
      task.json                 everything below except the files themselves
      workspace/                the files meant for the agent
      workspace.manifest.json   exactly those files: path, type, mode, size, sha256 (or a symlink's target)
      metric/                   optional: what the watcher's metric command runs, for the watcher only
      metric.manifest.json      exactly the metric directory's files, the same shape

Only the workspace reaches the agent, and only the entries on its manifest,
every directory included. Admission is a set of mechanical checks (D-031),
not a proof that the workspace gives nothing away: every host-only artifact
(`task.json`, both manifests, the metric directory, and the user policy file
and model script the caller names) must resolve outside the workspace; no
workspace file may hold the canary, equal a host-only document byte for byte
or as parsed JSON, hold a private section of the task document (card,
scoring, events, grouping, probes) as a JSON value, or name the measurement.
`load` validates everything strictly and raises `TaskError` with one line per
problem, each naming the JSON path or file it is about. `README.md`
describes the format and `task.schema.json` is its JSON Schema.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import triggers
from .words import message_words, name_words, telling_words
from .worklog import glob_problem

FORMAT = "cohbench-task"
VERSION = 1
FAMILIES = ("routing", "explanation", "steering")
OPAQUE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
NAME = re.compile(r"\A[a-z][a-z0-9_]{0,40}\Z")
CARD_ID = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,39}\Z")
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
ACTIONS = ("observe", "correction", "message", "reset")
PROBE_ABOUT = ("output", "message", "change", "attempt")
QUOTE = "{quote}"
HOST_METADATA = re.compile(r"(^|/)(\.DS_Store|\._[^/]*|\.AppleDouble|\.Spotlight-V100|\.Trashes|\.fseventsd|Thumbs\.db|desktop\.ini|Icon\r)(/|$)")
# The largest workspace or metric file the content checks read whole (the
# measurement words, the canary, the parsed-JSON comparisons). A larger file
# is refused, so no file escapes a check by its size.
INSPECT_MAX = 4 << 20
FIRST_HANDBACK = {"handbacks": {"at_least": 1}}  # the trigger of Explanation's fixed follow-up
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")  # the harnesses' instruction files; never task files (D-026)

TOP = {"format", "version", "instance", "canary", "grouping", "workspace", "watcher", "prompt", "user", "budgets", "reachability", "events", "scoring", "awareness"}
TOP_OPTIONAL: set[str] = set()


class TaskError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("task instance is not valid:\n  " + "\n  ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------------------
# The loaded task
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Grouping:
    family: str
    seed: str
    pair: str | None
    sibling: str | None
    control: bool
    horizon: str


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    type: str  # file, dir, symlink
    mode: int | None
    size: int | None
    sha256: str | None
    target: str | None


@dataclass(frozen=True)
class Workspace:
    dir: Path
    entries: tuple[ManifestEntry, ...]
    manifest_doc: dict[str, Any]  # the manifest as validated, for the environment

    def manifest(self) -> dict[str, Any]:
        return copy.deepcopy(self.manifest_doc)


@dataclass(frozen=True)
class Metric:
    command: tuple[str, ...]
    dir: Path
    timeout_s: float | None
    manifest_doc: dict[str, Any]


@dataclass(frozen=True)
class Watcher:
    ignore: tuple[str, ...]
    metric: Metric | None


@dataclass(frozen=True)
class Replies:
    unanswerable: str
    already_given: str  # holds {quote}
    secret_refusal: str


@dataclass(frozen=True)
class Probe:
    question: str
    expected: str | None  # a literal answer, or None with expected_from
    about: str  # output, message, change, attempt
    expected_from: dict[str, str] | None = None  # {"shown_to_user": text}: "yes" or "no" from what had reached the user at the copy


@dataclass(frozen=True)
class Action:
    type: str  # observe, correction, message, reset
    text: str | None = None  # correction, message
    override_policy: bool = True  # message
    notice: str | None = None  # reset
    continuation: str | None = None  # reset


@dataclass(frozen=True)
class Event:
    name: str
    trigger: dict[str, Any]
    action: Action
    required: bool
    reopen: str | None
    probe: Probe | None

    def scheduled(self) -> dict[str, Any]:
        """What episode.json's `planted[].scheduled` holds for this event."""
        return {"trigger": copy.deepcopy(self.trigger), "action": self.action.type, "required": self.required,
                "probe": self.probe.about if self.probe else None}

    def prerequisites(self) -> set[str]:
        """The events this event's trigger cannot hold without (its positive
        hard dependencies; nothing inside `not`, only what every branch of
        `any` needs)."""
        return triggers.prerequisites(self.trigger)


@dataclass(frozen=True)
class Task:
    root: Path
    instance: str
    canary: str | None
    grouping: Grouping
    workspace: Workspace
    watcher: Watcher
    opening: str
    coached_sentence: str
    card: dict[str, Any]
    return_delay_ms: int
    replies: Replies
    budgets: dict[str, int | None]  # elapsed_ms, tokens, user_wait_ms
    max_reopens: int
    events: tuple[Event, ...]
    answer_key: dict[str, Any]
    control: dict[str, Any] | None
    identifiers: dict[str, Any]
    observations: dict[str, Any]
    awareness: str | None
    digest: str
    warnings: tuple[str, ...] = ()

    def event(self, name: str) -> Event:
        return next(e for e in self.events if e.name == name)

    def card_for(self, fired: set[str] | frozenset[str]) -> dict[str, Any]:
        """A deep copy of the card with only the items available now: an item
        with `available_after` once that event has fired. Notes are subject to
        the same rule: a note given as text is something the user knows from
        the start; a note `{"text", "available_after"}` applies once its event
        has fired. The policy gets this, with `notes` as the list of texts
        available."""
        out = copy.deepcopy(self.card)
        for key in ("facts", "decisions"):
            out[key] = [{k: v for k, v in x.items() if k != "available_after"} for x in out.get(key, []) if x.get("available_after") in (None, *fired)]
        if "notes" in out:
            notes = out["notes"] if isinstance(out["notes"], list) else [out["notes"]]
            out["notes"] = [n if isinstance(n, str) else n["text"] for n in notes
                            if isinstance(n, str) or n.get("available_after") in (None, *fired)]
        return out

    @staticmethod
    def card_texts(card: dict[str, Any]) -> dict[str, str]:
        """Card item id -> the text a reply quotes for it: a fact's text, a decision's answer."""
        out = {f["id"]: f["text"] for f in card.get("facts", [])}
        out.update({d["id"]: d["answer"] for d in card.get("decisions", [])})
        return out


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class _V:
    def __init__(self) -> None:
        self.err: list[str] = []
        self.warn: list[str] = []
        self.canary: str | None = None

    def obj(self, v: Any, path: str, required: set[str], optional: set[str] = set()) -> bool:
        if not isinstance(v, dict):
            self.err.append(f"{path}: must be an object")
            return False
        missing, extra = required - set(v), set(v) - required - optional
        for k in sorted(missing):
            self.err.append(f"{path}.{k}: missing")
        for k in sorted(extra, key=str):
            self.err.append(f"{path}.{k}: unknown field")
        return not missing

    def text(self, v: Any, path: str, agent_facing: bool = False) -> bool:
        if not isinstance(v, str) or not v.strip():
            self.err.append(f"{path}: must be a non-empty string")
            return False
        if "\x00" in v:
            self.err.append(f"{path}: holds a NUL character")
            return False
        if agent_facing:
            words = message_words(v, self.canary)
            if words:
                self.err.append(f"{path}: reaches the agent and holds telling words {sorted(set(words))}")
        elif self.canary and self.canary in v:
            self.err.append(f"{path}: reaches the agent and holds the canary")
        return True

    def count(self, v: Any, path: str, optional: bool = False, positive: bool = False) -> bool:
        if optional and v is None:
            return True
        if not isinstance(v, int) or isinstance(v, bool) or v < (1 if positive else 0):
            self.err.append(f"{path}: must be a {'positive' if positive else 'non-negative'} integer" + (" or null" if optional else ""))
            return False
        return True

    def opaque(self, v: Any, path: str, optional: bool = False) -> None:
        if optional and v is None:
            return
        if not isinstance(v, str) or not OPAQUE.match(v):
            self.err.append(f"{path}: an opaque id of 1 to 64 letters, digits, - and _")

    def regex(self, v: Any, path: str, groups: int | None = None) -> None:
        triggers.regex_problem(v, path, self.err, groups)


def _clean_rel(p: Any) -> bool:
    return (isinstance(p, str) and bool(p) and not p.startswith("/") and "\x00" not in p and "\n" not in p
            and all(s not in ("", ".", "..") for s in p.split("/")))


def _inside(root: Path, rel: Any, v: _V, path: str, want_dir: bool) -> Path | None:
    """`rel` under `root`, existing, of the wanted kind; the resolved path."""
    if not _clean_rel(rel):
        v.err.append(f"{path}: a clean relative path inside the task directory")
        return None
    try:
        real = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        v.err.append(f"{path}: {rel} does not exist")
        return None
    if not real.is_relative_to(root.resolve()):
        v.err.append(f"{path}: {rel} leads outside the task directory")
        return None
    if want_dir != real.is_dir():
        v.err.append(f"{path}: {rel} must be a {'directory' if want_dir else 'file'}")
        return None
    return real


def _canon(data: bytes) -> str | None:
    """A JSON document in one canonical form, or None if it is not JSON."""
    try:
        return json.dumps(json.loads(data.decode("utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _canon_value(x: Any) -> str:
    """The canonical JSON of a parsed value: sorted keys, no spaces, as `_canon`."""
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_nodes(x: Any, out: list[str], depth: int = 0) -> str:
    """The canonical JSON of `x`, appending that of every object and list
    inside it (itself included) to `out`, computed once bottom up."""
    if depth > 200:
        raise RecursionError("too deep")
    if isinstance(x, dict):
        c = "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _json_nodes(x[k], out, depth + 1) for k in sorted(x)) + "}"
    elif isinstance(x, list):
        c = "[" + ",".join(_json_nodes(y, out, depth + 1) for y in x) + "]"
    else:
        return json.dumps(x, ensure_ascii=False)
    out.append(c)
    return c


PRIVATE_MIN = 16  # a private value shorter than this, as canonical JSON ([], {}, a short list), is too common to match on


def _private_values(doc: dict[str, Any], extra: dict[str, Any]) -> dict[str, str]:
    """Canonical JSON -> label of every private section of the task document
    (the user card, the scoring data and its parts, the event schedule and
    each event and probe, the grouping keys) and of the model script inside a
    scripted run file. A workspace file may hold none of these as a JSON
    value, whole or inside a larger document."""
    vals: dict[str, Any] = {}
    u = doc.get("user")
    if isinstance(u, dict):
        vals["user.card"] = u.get("card")
        if isinstance(u.get("card"), dict):
            for k in ("facts", "decisions"):
                vals[f"user.card.{k}"] = u["card"].get(k)
    s = doc.get("scoring")
    vals["scoring"] = s
    if isinstance(s, dict):
        for k in ("answer_key", "control", "identifiers", "observations"):
            vals[f"scoring.{k}"] = s.get(k)
    vals["events"] = doc.get("events")
    for n, e in enumerate(doc.get("events") or [] if isinstance(doc.get("events"), list) else []):
        vals[f"events[{n}]"] = e
        if isinstance(e, dict):
            vals[f"events[{n}].trigger"] = e.get("trigger")
            vals[f"events[{n}].probe"] = e.get("probe")
    vals["grouping"] = doc.get("grouping")
    for what, d in extra.items():
        if isinstance(d, dict) and isinstance(d.get("script"), dict):
            vals[f"{what} (its script)"] = d["script"]
    out: dict[str, str] = {}
    for label, x in vals.items():
        if isinstance(x, (dict, list)) and x:
            c = _canon_value(x)
            if len(c) >= PRIVATE_MIN:
                out.setdefault(c, label)
    return out


def _answer_key_errors(family: str, payload: dict[str, Any]) -> list[str]:
    """The trajectory unit's own per-family check, through its validator: a
    record that is valid everywhere else, so only answer-key errors remain."""
    from cohbench.trajectory import episode as tep

    doc = {"format": "cohbench-episode", "version": 1, "episode": "e", "grouping": {"family": family, "seed": 0, "pair": None, "sibling": None,
           "control": False, "horizon": "h", "mode": "default"}, "budget": {"limits": {"elapsed_ms": None, "tokens": None, "user_wait_ms": None},
           "consumed": {"elapsed_ms": 0, "user_wait_ms": 0}}, "planted": [], "reach": {"status": "reached", "not_reached": []},
           "answer_key": {"family": family, "payload": payload}, "control": None, "probes": [], "answers": [] if family == "explanation" else None,
           "awareness": {"asked": False, "outcome": None}, "end": {"reason": "finished", "duration_ms": 0}}
    return [x for x in tep.validate(doc) if x.startswith("answer_key")]


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _walk(rel: str, links: dict[str, str], depth: int = 0) -> str | None:
    """`rel` with every link among `links` (path -> target) followed, component
    by component; None when it leaves the directory or links go round in a
    cycle (more than 40 links on the way)."""
    if depth > 40:
        return None
    parts: list[str] = []
    todo = rel.split("/")
    while todo:
        c = todo.pop(0)
        if c in ("", "."):
            continue
        if c == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(c)
        here = "/".join(parts)
        if here in links:
            t = links[here]
            if t.startswith("/"):
                return None
            parts.pop()
            resolved = _walk("/".join(parts + [t]) if parts else t, links, depth + 1)
            if resolved is None:
                return None
            parts = [x for x in resolved.split("/") if x]
    return "/".join(parts)


def _admit(v: _V, label: str, base: Path, mf: Path, names: bool) -> tuple[tuple[ManifestEntry, ...], dict[str, Any], dict[str, str]]:
    """A directory against its manifest: exactly the listed entries, nothing
    else, same type, mode, size and hash, no special files, symlinks with
    relative targets that stay inside. Returns the entries, the manifest
    document and path -> sha256 of every file."""
    mname = mf.name
    try:
        doc = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        v.err.append(f"{mname}: not readable JSON ({exc})")
        return (), {}, {}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        v.err.append(f"{mname}.entries: a list")
        return (), {}, {}
    v.obj(doc, mname, {"entries"} | ({"mtime"} if names else set()), {"mtime"})
    if "mtime" in doc and (not isinstance(doc["mtime"], int) or isinstance(doc["mtime"], bool) or not 0 <= doc["mtime"] < 2**33):
        v.err.append(f"{mname}.mtime: whole epoch seconds, the modification time every packed entry gets")
    entries: dict[str, ManifestEntry] = {}
    for n, r in enumerate(doc["entries"]):
        p = f"{mname}.entries[{n}]"
        if not isinstance(r, dict):
            v.err.append(f"{p}: must be an object")
            continue
        before = len(v.err)
        v.obj(r, p, {"path"}, {"type", "mode", "size", "sha256", "target"})
        rel = r.get("path")
        if not _clean_rel(rel):
            v.err.append(f"{p}.path: a clean relative path")
            continue
        if rel in entries:
            v.err.append(f"{p}.path: {rel} listed twice")
            continue
        kind = r.get("type", "file")
        if kind not in ("file", "dir", "symlink"):
            v.err.append(f"{p}.type: file, dir or symlink")
            continue
        mode = None
        if "mode" in r:
            if not isinstance(r["mode"], str) or not re.fullmatch(r"0?[0-7]{3,4}", r["mode"]):
                v.err.append(f"{p}.mode: an octal string such as \"0644\"")
            else:
                mode = int(r["mode"], 8)
        if kind == "dir" and "mode" not in r:
            v.err.append(f"{p}.mode: missing (a directory's mode is part of what the agent receives)")
        if kind == "file":
            if "mode" not in r:
                v.err.append(f"{p}.mode: missing (a file's mode is part of what the agent receives)")
            v.count(r.get("size"), f"{p}.size")
            if not isinstance(r.get("sha256"), str) or not SHA256.match(r["sha256"]):
                v.err.append(f"{p}.sha256: 64 lower-case hex digits")
            if "target" in r:
                v.err.append(f"{p}.target: only a symlink has a target")
        elif kind == "symlink":
            t = r.get("target")
            if not isinstance(t, str) or not t or t.startswith("/") or "\x00" in t:
                v.err.append(f"{p}.target: {rel} -> {t!r} must be a relative target that stays inside the directory")
            else:
                if names and name_words([t]):
                    v.err.append(f"{p}.target: {rel} -> {t} holds telling words {sorted(set(telling_words(t)))}")
                if names and v.canary and v.canary in t:
                    v.err.append(f"{p}.target: {rel} -> the target holds the task's canary")
        if kind != "file" and ("size" in r or "sha256" in r):
            v.err.append(f"{p}: only a file has size and sha256")
        if names and name_words([rel]):
            v.err.append(f"{p}.path: {rel} holds telling words {sorted(set(telling_words(rel)))}")
        if any(part in INSTRUCTION_FILES for part in rel.split("/")):
            v.err.append(f"{p}.path: {rel} uses a harness instruction file name as a path component, which a task may not (D-026)")
        if names and v.canary and v.canary in rel:
            v.err.append(f"{p}.path: {rel} holds the task's canary")
        if len(v.err) == before:
            entries[rel] = ManifestEntry(rel, kind, mode, r.get("size"), r.get("sha256"), r.get("target"))
    for k in sorted(entries):
        parent = posixpath.dirname(k)
        if parent and (parent not in entries or entries[parent].type != "dir"):
            v.err.append(f"{mname}: {k} is in {parent}/, which is not listed as a directory")
    links = {k: e.target for k, e in entries.items() if e.type == "symlink" and e.target}
    for k in sorted(links):
        if _walk(posixpath.join(posixpath.dirname(k), links[k]) if posixpath.dirname(k) else links[k], links) is None:
            v.err.append(f"{mname}: {k} -> {links[k]!r} leaves the directory or goes round in a cycle, following the manifest's own links")
    hashes: dict[str, str] = {}
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            fp = Path(dirpath) / name
            rel = fp.relative_to(base).as_posix()
            if HOST_METADATA.search(rel):
                continue
            st = fp.lstat()
            kind = "symlink" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "special"
            if kind == "special":
                v.err.append(f"{label}/{rel}: a special file (device, fifo or socket) is not admitted")
                continue
            e = entries.get(rel)
            if e is None:
                v.err.append(f"{label}/{rel}: not on the manifest" + (" (every directory is listed, with its mode)" if kind == "dir" else ""))
                continue
            seen.add(rel)
            if kind != e.type:
                v.err.append(f"{label}/{rel}: is a {kind}, the manifest says {e.type}")
                continue
            if e.mode is not None and kind != "symlink" and stat.S_IMODE(st.st_mode) != e.mode:
                v.err.append(f"{label}/{rel}: mode {stat.S_IMODE(st.st_mode):04o}, the manifest says {e.mode:04o}")
            if kind == "file" and st.st_nlink > 1:
                v.err.append(f"{label}/{rel}: a hard link ({st.st_nlink} names); task files have one name each (D-032)")
            if kind == "file" and st.st_size > INSPECT_MAX:
                v.err.append(f"{label}/{rel}: {st.st_size} bytes, more than the {INSPECT_MAX} the content checks can read")
            if kind == "file":
                sha = _sha_file(fp)
                hashes[rel] = sha
                if st.st_size != e.size or sha != e.sha256:
                    v.err.append(f"{label}/{rel}: size or content differs from the manifest")
            if kind == "symlink" and os.readlink(fp) != e.target:
                v.err.append(f"{label}/{rel}: target {os.readlink(fp)!r}, the manifest says {e.target!r}")
    for rel in sorted(set(entries) - seen):
        v.err.append(f"{mname}: {rel} is listed but missing from {label}/")
    return tuple(entries[k] for k in sorted(entries)), doc, hashes


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _grouping(v: _V, g: Any) -> Grouping | None:
    if not v.obj(g, "grouping", {"family", "seed", "pair", "sibling", "control", "horizon"}):
        return None
    before = len(v.err)
    if g.get("family") not in FAMILIES:
        v.err.append(f"grouping.family: one of {list(FAMILIES)}")
    # A number, as the trajectory unit requires: a caller-chosen string could carry anything.
    if not (isinstance(g.get("seed"), int) and not isinstance(g.get("seed"), bool) and 0 <= g["seed"] <= triggers.LIMIT):
        v.err.append("grouping.seed: a non-negative integer")
    v.opaque(g.get("pair"), "grouping.pair", optional=True)
    v.opaque(g.get("horizon"), "grouping.horizon")
    if (g.get("pair") is None) != (g.get("sibling") is None) or g.get("sibling") not in (None, "A", "B"):
        v.err.append("grouping.sibling: A or B exactly when grouping.pair is set, else null")
    if not isinstance(g.get("control"), bool):
        v.err.append("grouping.control: true or false")
    if len(v.err) != before:
        return None
    return Grouping(g["family"], g["seed"], g["pair"], g["sibling"], g["control"], g["horizon"])


def _card(v: _V, card: Any, names: set[str]) -> None:
    if not v.obj(card, "user.card", {"facts", "decisions"}, {"notes"}):
        return
    ids: set[str] = set()
    for key, fields in (("facts", ("id", "text")), ("decisions", ("id", "question", "answer"))):
        rows = card.get(key)
        if not isinstance(rows, list):
            v.err.append(f"user.card.{key}: a list")
            continue
        for n, r in enumerate(rows):
            p = f"user.card.{key}[{n}]"
            if not v.obj(r, p, set(fields), {"available_after"}):
                continue
            if not isinstance(r.get("id"), str) or not CARD_ID.match(r["id"]):
                v.err.append(f"{p}.id: lower-case letters, digits, - and _")
            elif r["id"] in ids:
                v.err.append(f"{p}.id: {r['id']} is used twice in the card")
            else:
                ids.add(r["id"])
            sent = "text" if key == "facts" else "answer"
            v.text(r.get(sent), f"{p}.{sent}", agent_facing=True)  # it can be sent as a reply
            if key == "decisions":
                v.text(r.get("question"), f"{p}.question")
            if "available_after" in r and (not isinstance(r["available_after"], str) or r["available_after"] not in names):
                v.err.append(f"{p}.available_after: {r['available_after']!r} is not an event of this task")
    if "notes" in card:
        notes = card["notes"]
        if isinstance(notes, str):
            v.text(notes, "user.card.notes")  # known from the start
        elif isinstance(notes, list) and notes:
            for n, x in enumerate(notes):
                p = f"user.card.notes[{n}]"
                if isinstance(x, str):
                    v.text(x, p)
                elif v.obj(x, p, {"text"}, {"available_after"}):
                    v.text(x.get("text"), f"{p}.text")
                    if "available_after" in x and (not isinstance(x["available_after"], str) or x["available_after"] not in names):
                        v.err.append(f"{p}.available_after: {x['available_after']!r} is not an event of this task")
        else:
            v.err.append("user.card.notes: text the user knows from the start, or a non-empty list of texts and {text, available_after} notes")


def _event(v: _V, e: Any, n: int, names: set[str], deps: dict[str, set[str]], has_metric: bool) -> Event | None:
    p = f"events[{n}]"
    if not isinstance(e, dict):
        v.err.append(f"{p}: must be an object")
        return None
    before = len(v.err)
    v.obj(e, p, {"name", "trigger", "action", "required"}, {"reopen", "probe"})
    name = e.get("name")
    d: set[str] = set()
    if "trigger" in e:
        triggers.check(e["trigger"], f"{p}.trigger", names, v.err, d, has_metric=has_metric)
    if isinstance(name, str):
        deps[name] = d
    a = e.get("action")
    action = None
    if isinstance(a, dict) and "type" in a:
        t = a.get("type")
        allowed = {"observe": set(), "correction": {"text"}, "message": {"text", "override_policy"}, "reset": {"notice", "continuation"}}
        if not isinstance(t, str) or t not in allowed:
            v.err.append(f"{p}.action.type: one of {list(ACTIONS)}")
        else:
            needed = allowed[t] - {"override_policy"}
            for k in sorted(set(a) - {"type"} - allowed[t], key=str):
                v.err.append(f"{p}.action.{k}: not a field of a {t} action")
            for k in sorted(needed - set(a)):
                v.err.append(f"{p}.action.{k}: missing")
            for k in sorted(needed & set(a)):
                v.text(a[k], f"{p}.action.{k}", agent_facing=True)
            if "override_policy" in a and not isinstance(a["override_policy"], bool):
                v.err.append(f"{p}.action.override_policy: true or false")
            action = Action(t, a.get("text"), a.get("override_policy", True), a.get("notice"), a.get("continuation"))
    else:
        v.err.append(f"{p}.action: an object with a type")
    req = e.get("required")
    if not isinstance(req, bool):
        v.err.append(f"{p}.required: true or false")
    reopen = e.get("reopen")
    if req is True:
        v.text(reopen, f"{p}.reopen", agent_facing=True)
    elif reopen is not None:
        v.err.append(f"{p}.reopen: only a required event has a reopen message")
    probe = None
    pr = e.get("probe")
    if pr is not None and v.obj(pr, f"{p}.probe", {"question", "about"}, {"expected", "expected_from"}):
        v.text(pr.get("question"), f"{p}.probe.question", agent_facing=True)
        if ("expected" in pr) == ("expected_from" in pr):
            v.err.append(f"{p}.probe: exactly one of expected and expected_from")
        elif "expected" in pr:
            v.text(pr.get("expected"), f"{p}.probe.expected")
        else:
            ef = pr["expected_from"]
            if not isinstance(ef, dict) or set(ef) != {"shown_to_user"} or not isinstance(ef.get("shown_to_user"), str) or not ef["shown_to_user"].strip():
                v.err.append(f"{p}.probe.expected_from: {{\"shown_to_user\": text}}, answered yes or no from what had reached the user at the copy")
        if not isinstance(pr.get("about"), str) or pr["about"] not in PROBE_ABOUT:
            v.err.append(f"{p}.probe.about: one of {list(PROBE_ABOUT)}")
        elif pr["about"] == "message" and action is not None and action.type == "observe":
            v.err.append(f"{p}.probe.about: an observe event sends no message to be about")
        probe = Probe(pr.get("question"), pr.get("expected"), pr.get("about"), pr.get("expected_from"))
    if len(v.err) != before or action is None:
        return None
    return Event(name, e["trigger"], action, req, reopen, probe)


def _identifiers(v: _V, d: Any) -> None:
    p = "scoring.identifiers"
    if not v.obj(d, p, set(), {"patterns", "minted", "min_token_length", "digits_only_tokens", "external_roots", "file_slack_ms"}):
        return
    pats = d.get("patterns", [])
    if not isinstance(pats, list):
        v.err.append(f"{p}.patterns: a list")
        pats = []
    for n, r in enumerate(pats):
        q = f"{p}.patterns[{n}]"
        if v.obj(r, q, {"kind", "regex"}, {"case_sensitive"}):
            if not isinstance(r.get("kind"), str) or not NAME.match(r["kind"]):
                v.err.append(f"{q}.kind: a lower-case word")
            v.regex(r.get("regex"), f"{q}.regex")
            if "case_sensitive" in r and not isinstance(r["case_sensitive"], bool):
                v.err.append(f"{q}.case_sensitive: true or false")
    m = d.get("minted", {})
    if not isinstance(m, dict) or not all(isinstance(k, str) and k and isinstance(x, str) and NAME.match(x) for k, x in m.items()):
        v.err.append(f"{p}.minted: an object of exact text to kind")
    if "min_token_length" in d:
        v.count(d["min_token_length"], f"{p}.min_token_length")
    if "file_slack_ms" in d:
        v.count(d["file_slack_ms"], f"{p}.file_slack_ms")
    if "digits_only_tokens" in d and not isinstance(d["digits_only_tokens"], bool):
        v.err.append(f"{p}.digits_only_tokens: true or false")
    if "external_roots" in d and (not isinstance(d["external_roots"], list) or not all(isinstance(x, str) and x.startswith("/") for x in d["external_roots"])):
        v.err.append(f"{p}.external_roots: a list of absolute path prefixes")


def _observations(v: _V, d: Any) -> None:
    p = "scoring.observations"
    if not v.obj(d, p, set(), {"attempts"}):
        return
    rows = d.get("attempts", [])
    if not isinstance(rows, list):
        v.err.append(f"{p}.attempts: a list")
        return
    names: set[str] = set()
    for n, r in enumerate(rows):
        q = f"{p}.attempts[{n}]"
        if v.obj(r, q, {"name", "regex"}, {"capture"}):
            if not isinstance(r.get("name"), str) or not NAME.match(r["name"]):
                v.err.append(f"{q}.name: a lower-case word")
            elif r["name"] in names:
                v.err.append(f"{q}.name: {r['name']} is used twice")
            else:
                names.add(r["name"])
            v.regex(r.get("regex"), f"{q}.regex")
            if "capture" in r:
                v.regex(r["capture"], f"{q}.capture", groups=1)


def _cycles(deps: dict[str, set[str]], v: _V) -> None:
    state: dict[str, int] = {}

    def visit(n: str, trail: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            v.err.append(f"events: the triggers depend on each other in a cycle: {' -> '.join(trail + [n])}")
            return
        state[n] = 1
        for m in sorted(deps.get(n, ())):
            visit(m, trail + [n])
        state[n] = 2

    for n in sorted(deps):
        visit(n, [])


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def check(directory: str | Path, host_files: dict[str, str | Path] | None = None) -> tuple[Task | None, list[str], list[str]]:
    """(task or None, problems, warnings). The task is None exactly when there
    are problems. `host_files` names further host-only documents by what they
    are (the user policy file, the scripted run's model script): they must
    resolve outside the workspace, and no workspace file may equal one."""
    root = Path(directory)
    v = _V()
    tj = root / "task.json"
    try:
        raw = tj.read_bytes()
        doc = json.loads(raw.decode("utf-8"), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"{c} is not JSON")))
    except (OSError, ValueError, RecursionError) as exc:
        return None, [f"task.json: not readable JSON ({exc})"], []
    if not isinstance(doc, dict):
        return None, ["task: must be an object"], []
    v.obj(doc, "task", TOP, TOP_OPTIONAL)
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        v.err.append(f"task.format and task.version: {FORMAT!r} and {VERSION}")
    v.opaque(doc.get("instance"), "task.instance")
    if "canary" in doc:
        if v.text(doc["canary"], "task.canary"):
            v.canary = doc["canary"]
    grouping = _grouping(v, doc.get("grouping"))

    # Host-only artifacts, and the workspace.
    host_only: dict[str, str] = {"task.json": hashlib.sha256(raw).hexdigest()}
    host_paths: dict[str, Path] = {"task.json": tj.resolve()}
    workspace = None
    ws_hashes: dict[str, str] = {}
    w = doc.get("workspace")
    wd = mf = None
    if v.obj(w, "workspace", {"dir", "manifest"}):
        wd = _inside(root, w.get("dir"), v, "workspace.dir", want_dir=True)
        mf = _inside(root, w.get("manifest"), v, "workspace.manifest", want_dir=False)
        if mf is not None:
            host_paths["workspace.manifest"] = mf
            host_only["workspace.manifest"] = _sha_file(mf)
    extra_docs: dict[str, Any] = {}
    for what, hp0 in (host_files or {}).items():
        try:
            hp = Path(hp0).resolve(strict=True)
        except (OSError, RuntimeError):
            v.err.append(f"{what}: {hp0} does not exist")
            continue
        host_paths[what] = hp
        if hp.is_file():
            host_only[what] = _sha_file(hp)
            try:
                extra_docs[what] = json.loads(hp.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                pass

    watcher = None
    metric_doc: dict[str, Any] | None = None
    metric_hashes: dict[str, str] = {}
    md = None
    wt = doc.get("watcher")
    if v.obj(wt, "watcher", {"ignore", "metric"}):
        ig = wt.get("ignore")
        if not isinstance(ig, list) or not all(isinstance(x, str) and x and not x.startswith("/") for x in ig):
            v.err.append("watcher.ignore: a list of relative glob patterns")
            ig = []
        for n, g in enumerate(ig):
            why = glob_problem(g)
            if why:
                v.err.append(f"watcher.ignore[{n}]: {why}")
        metric = None
        m = wt.get("metric")
        if m is not None and v.obj(m, "watcher.metric", {"command", "dir", "manifest"}, {"timeout_s"}):
            cmd = m.get("command")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
                v.err.append("watcher.metric.command: a non-empty argv list of strings")
            md = _inside(root, m.get("dir"), v, "watcher.metric.dir", want_dir=True)
            mmf = _inside(root, m.get("manifest"), v, "watcher.metric.manifest", want_dir=False)
            to = m.get("timeout_s")
            if to is not None and (not isinstance(to, (int, float)) or isinstance(to, bool) or not 0 < to <= 3600):
                v.err.append("watcher.metric.timeout_s: seconds, above 0 and at most 3600")
            if md is not None and mmf is not None:
                host_paths["watcher.metric.dir"] = md
                host_paths["watcher.metric.manifest"] = mmf
                if mmf.is_relative_to(md):
                    v.err.append("watcher.metric.manifest: must be outside the metric directory")
                else:
                    host_only["watcher.metric.manifest"] = _sha_file(mmf)
                    _, metric_doc, metric_hashes = _admit(v, "metric", md, mmf, names=False)
                    for rel, sha in metric_hashes.items():
                        host_only[f"metric/{rel}"] = sha
                if isinstance(cmd, list) and metric_doc is not None:
                    metric = Metric(tuple(cmd), md, to, metric_doc)
        watcher = Watcher(tuple(ig), metric)

    if wd is not None:
        for what, hp in host_paths.items():
            if hp == wd or hp.is_relative_to(wd):
                v.err.append(f"{what}: must resolve outside the workspace directory")
            elif wd.is_relative_to(hp) and hp.is_dir():
                v.err.append(f"workspace.dir: must not be inside {what}")
        if mf is not None and not mf.is_relative_to(wd):
            entries, mdoc, ws_hashes = _admit(v, "workspace", wd, mf, names=True)
            workspace = Workspace(wd, entries, mdoc)
            same = {sha: what for what, sha in host_only.items()}
            host_docs = {what: _canon(pth.read_bytes()) for what, pth in host_paths.items() if pth.is_file()}
            if md is not None:  # the metric files as parsed JSON too (m5)
                for rel in metric_hashes:
                    mp = md / rel
                    if mp.stat().st_size <= INSPECT_MAX:
                        host_docs[f"metric/{rel}"] = _canon(mp.read_bytes())
            private = _private_values(doc, extra_docs)
            for rel, sha in sorted(ws_hashes.items()):
                if sha in same:
                    v.err.append(f"workspace/{rel}: byte-identical to the host-only {same[sha]}")
                fp = wd / rel
                data = fp.read_bytes()
                if len(data) <= INSPECT_MAX:
                    c = _canon(data)
                    hit = next((what for what, d in host_docs.items() if c is not None and d is not None and c == d), None)
                    if hit and sha not in same:
                        v.err.append(f"workspace/{rel}: the same JSON document as the host-only {hit}, reformatted")
                    if c is not None and not hit and sha not in same:
                        nodes: list[str] = []
                        try:
                            _json_nodes(json.loads(c), nodes)
                        except RecursionError:
                            v.err.append(f"workspace/{rel}: JSON nested too deep to check against the task's private sections")
                        found = sorted({private[x] for x in nodes if x in private})
                        if found:
                            v.err.append(f"workspace/{rel}: holds the task's private {', '.join(found)} as a JSON value")
                if v.canary and v.canary.encode() in data:
                    v.err.append(f"workspace/{rel}: holds the task's canary")
                if len(data) <= INSPECT_MAX:
                    words = telling_words(data.decode("utf-8", errors="replace"))
                    if words:
                        v.err.append(f"workspace/{rel}: content names the measurement ({sorted(set(words))})")

    pr = doc.get("prompt")
    if v.obj(pr, "prompt", {"opening", "coached_sentence"}):
        v.text(pr.get("opening"), "prompt.opening", agent_facing=True)
        v.text(pr.get("coached_sentence"), "prompt.coached_sentence", agent_facing=True)
        if isinstance(pr.get("coached_sentence"), str) and "\n" in pr["coached_sentence"].strip():
            v.err.append("prompt.coached_sentence: one sentence on one line")

    evs = doc.get("events")
    if not isinstance(evs, list):
        v.err.append("events: a list")
        evs = []
    names: set[str] = set()
    for n, e in enumerate(evs):
        nm = e.get("name") if isinstance(e, dict) else None
        if not isinstance(nm, str) or not NAME.match(nm):
            v.err.append(f"events[{n}].name: a lower-case word of letters, digits and _")
        elif nm in names:
            v.err.append(f"events[{n}].name: {nm} is used twice")
        else:
            names.add(nm)

    u = doc.get("user")
    replies = None
    if v.obj(u, "user", {"card", "return_delay_ms", "replies"}):
        _card(v, u.get("card"), names)
        v.count(u.get("return_delay_ms"), "user.return_delay_ms")
        r = u.get("replies")
        if v.obj(r, "user.replies", {"unanswerable", "already_given", "secret_refusal"}):
            for k in ("unanswerable", "already_given", "secret_refusal"):
                v.text(r.get(k), f"user.replies.{k}", agent_facing=True)
            if isinstance(r.get("already_given"), str) and r["already_given"].count(QUOTE) != 1:
                v.err.append(f"user.replies.already_given: must hold {QUOTE} exactly once, where the earlier message is quoted")
            replies = Replies(r.get("unanswerable"), r.get("already_given"), r.get("secret_refusal"))

    b = doc.get("budgets")
    budgets: dict[str, int | None] = {"elapsed_ms": None, "tokens": None, "user_wait_ms": None}
    if v.obj(b, "budgets", set(), set(budgets)):
        for k in budgets:
            if k in b and v.count(b[k], f"budgets.{k}", optional=True):
                budgets[k] = b[k]

    rch = doc.get("reachability")
    max_reopens = 0
    if v.obj(rch, "reachability", {"max_reopens"}) and v.count(rch.get("max_reopens"), "reachability.max_reopens"):
        max_reopens = rch["max_reopens"]

    events: list[Event] = []
    deps: dict[str, set[str]] = {}
    has_metric = watcher is not None and watcher.metric is not None
    for n, e in enumerate(evs):
        ev = _event(v, e, n, names, deps, has_metric)
        if ev is not None:
            events.append(ev)
    _cycles(deps, v)
    required = {e.name for e in events if e.required}
    for e in events:
        if e.required:
            bad = sorted(e.prerequisites() - required)
            if bad:
                v.err.append(f"events: {e.name} is required but its trigger needs events that are not: {bad}")
    s_ = doc.get("scoring")
    attempts = ((s_ or {}).get("observations") or {}).get("attempts") if isinstance(s_, dict) and isinstance((s_ or {}).get("observations"), dict) else None
    for n, e in enumerate(events):
        if e.probe and e.probe.about == "attempt" and not attempts:
            v.err.append(f"events: {e.name}.probe.about is attempt, so scoring.observations must declare an attempt pattern")
    if any(e.required for e in events) and max_reopens == 0:
        v.err.append("reachability.max_reopens: must be at least 1 when an event is required, so a required event can be reopened")

    s = doc.get("scoring")
    if v.obj(s, "scoring", {"answer_key", "control", "identifiers", "observations"}):
        if not isinstance(s.get("answer_key"), dict):
            v.err.append("scoring.answer_key: an object (the per-family payload)")
        elif grouping is not None:
            v.err += [f"scoring.{x}" for x in _answer_key_errors(grouping.family, s["answer_key"])]
        c = s.get("control")
        if c is not None:
            if v.obj(c, "scoring.control", {"expected"}) and not isinstance(c.get("expected"), dict):
                v.err.append("scoring.control.expected: an object")
        if grouping is not None and grouping.control != (c is not None):
            v.err.append("scoring.control: present exactly when grouping.control is true")
        _identifiers(v, s.get("identifiers"))
        _observations(v, s.get("observations"))

    if grouping is not None and grouping.family == "explanation" and isinstance(s, dict) and isinstance(s.get("answer_key"), dict):
        # One source for the fixed follow-up (D-024): the answer key's text,
        # sent by exactly one event, at the first hand-back, replacing the reply.
        fu = s["answer_key"].get("follow_up")
        senders = [e.name for e in events if e.action.type == "message" and e.action.text == fu]
        first = [e.name for e in events if e.action.type == "message" and e.action.text == fu and e.action.override_policy
                 and e.trigger == FIRST_HANDBACK]
        if len(senders) != 1 or len(first) != 1:
            v.err.append("events: an Explanation task has exactly one event that sends scoring.answer_key.follow_up: a message with "
                         f"override_policy true and the trigger {json.dumps(FIRST_HANDBACK)}; found {senders or 'none'}")

    aw = doc.get("awareness")
    if aw is not None and v.obj(aw, "awareness", {"question"}):
        v.text(aw.get("question"), "awareness.question")

    if v.err:
        return None, v.err, v.warn

    assert workspace is not None and watcher is not None and grouping is not None and replies is not None
    h = hashlib.sha256()
    h.update(b"task.json\0" + raw + b"\0")
    h.update(b"workspace.manifest\0" + json.dumps(workspace.manifest_doc, sort_keys=True).encode() + b"\0")
    if watcher.metric:
        h.update(b"metric.manifest\0" + json.dumps(watcher.metric.manifest_doc, sort_keys=True).encode() + b"\0")
    task = Task(
        root=root, instance=doc["instance"], canary=doc.get("canary"), grouping=grouping,
        workspace=workspace, watcher=watcher, opening=pr["opening"], coached_sentence=pr["coached_sentence"],
        card=copy.deepcopy(u["card"]), return_delay_ms=u["return_delay_ms"], replies=replies,
        budgets=budgets, max_reopens=max_reopens, events=tuple(events),
        answer_key=s["answer_key"], control=s["control"], identifiers=s["identifiers"], observations=s["observations"],
        awareness=aw["question"] if aw else None, digest=h.hexdigest(), warnings=tuple(v.warn),
    )
    return task, [], v.warn


def load(directory: str | Path, host_files: dict[str, str | Path] | None = None) -> Task:
    task, problems, _ = check(directory, host_files)
    if task is None:
        raise TaskError(problems)
    return task
