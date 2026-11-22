"""The identifier registry: first seen, known to the user, private identifiers
sent to the user, paths, task patterns, generic tokens, false positives."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from cohbench.trajectory import Segment, changelog, io, t3, validate
from cohbench.trajectory import scorer_view
from cohbench.trajectory.registry import TaskIdentifiers, build, generic_tokens, scorer_identifiers, trajectory_texts, validate_identifiers

from . import mini
from .conftest import FIX, export_segment, real_watcher, run

TASK = TaskIdentifiers.from_dict(json.loads((FIX / "identifiers-task.json").read_text()))
KW: dict = {}


def episode():
    th = mini.thread()
    r1 = mini.run_rec(th["id"], 1, 0, 10)
    r2 = mini.run_rec(th["id"], 2, 12, 20)
    items = [
        mini.user(r1, 0, "Process data/a.csv and write the summary to out/summary.txt. Use worker WKR-alpha7."),
        mini.assistant(r1, 1, "Submitting."),
        mini.command(r1, 2, 3.4, "./tool submit data/a.csv", "submitted job-7f3a9c\nwrote /work/project/tmp/c0ffee12ab.bin\nchecksum b3f9c2a17e\nsplit job-7f3a\n9c\n"),
        mini.item(r1, "file_change", 5, 6, title="write", fileName="/work/project/out/summary.txt", newStr="done with deadbeef01 run"),
        mini.assistant(r1, 8, "I used base64 and sha256 on utf-8 data with python3.12 in a well-known x86_64 setup on 2026-11-21T10:00:00Z, 50KB, step3 of 12345678, and/or."),
        mini.assistant(r1, 9, "Submitted job-7f3a9c; the output is in tmp/c0ffee12ab.bin and ./out/summary.txt."),
        mini.user(r2, 12, "What does job-7f3a9c mean? Also check the README.md file."),
        mini.dynamic(r2, 13, 13.1, "mcp__t3-code__send_to_user", {"message": "Worker wkr-alpha7 finished; checksum B3F9C2A17E."},
                     {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"sentMessage": "Worker wkr-alpha7 finished; checksum B3F9C2A17E."}}),
        mini.dynamic(r2, 14, 15, "ask_user", {"question": "Delete tmp/c0ffee12ab.bin?", "options": ["yes", "no"]}, {"content": [{"type": "text", "text": "yes"}]}),
    ]
    q = mini.item(r2, "user_input_request", 14.01, 14.9, requestId="req-1", questions=[{"id": "q1", "question": "Delete tmp/c0ffee12ab.bin?", "options": [{"label": "yes"}, {"label": "no"}]}])
    q["providerTurnId"] = items[-1]["providerTurnId"]
    items += [q, mini.assistant(r2, 16, "Deleted the job-7f3a9c output; summary.txt kept.")]
    res = run([Segment(t3.from_export(mini.export([(th, [r1, r2], items)])))])
    assert validate(res.trajectory).ok, validate(res.trajectory).errors
    return res


@pytest.fixture(scope="module")
def reg():
    res = episode()
    log = real_log(res.manifest["episode_start_utc"], **KW)
    return res, build(res.trajectory, log, TASK)


def real_log(start, **kw):
    """Segment 1 of the real watcher fixture, its episode start moved onto this episode's."""
    w, _ = real_watcher(1, start)
    return changelog.build(w.events, start, w.metrics, **kw)


def closed_log(records, start, **kw):
    """A change log from hand-written records, each batch with its closing
    record. A record without a batch is its own batch, numbered in time order."""
    loose = sorted((r for r in records if "batch" not in r), key=lambda r: r["wall"])
    top = max([r["batch"] for r in records if "batch" in r], default=0)
    records = [r for r in records if "batch" in r] + [{**r, "batch": top + n + 1} for n, r in enumerate(loose)]
    out = list(records)
    for b in sorted({r["batch"] for r in records if "batch" in r}):
        last = max(r["wall"] for r in records if r.get("batch") == b)
        out.append({"seq": 900 + b, "wall": last, "kind": "batch", "batch": b})
    return changelog.build(out, start, **kw)


def ident(reg, text, kind=None):
    return next(r for r in reg[1]["identifiers"] if r["text"] == text and (kind is None or r["kind"] == kind))


def seq_of(res, etype, t):
    return next(e["seq"] for e in res.trajectory.events if e["type"] == etype and e["t"] == t)


def tiny(items_fn):
    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 30)
    return run([Segment(t3.from_export(mini.export([(th, [r], items_fn(r))])))])


def test_registry_lists_the_expected_identifiers(reg):
    texts = {r["text"]: r["kind"] for r in reg[1]["identifiers"]}
    assert texts == {
        "out/summary.txt": "path", "data/a.csv": "path", "WKR-alpha7": "worker", "README.md": "path", "job-7f3a9c": "job_id",
        "tmp/c0ffee12ab.bin": "path", "c0ffee12ab": "token", "b3f9c2a17e": "hash", "deadbeef01": "token", "out/final.txt": "path",
        "job-7f3a": "token",  # the head of the identifier split across two lines, seen as its own token
        "tool": "path",  # the task's ./tool, a workspace file
        "out/report.txt": "path", "big.bin": "path", "out/.report.txt.tmp": "path",  # files from the change log; its directories are not identifiers
    }


def test_false_positive_words_are_not_identifiers():
    words = "I used base64 and sha256 on utf-8 data with python3.12 in a well-known x86_64 setup on 2026-11-21T10:00:00Z, 50KB, step3 of 12345678. Reading README files, sha256sum output, and/or."
    assert generic_tokens(words) == []


def test_generic_tokens():
    found = [t for _, _, t in generic_tokens("job-7f3a9c21 9d4cee55-5978-4338-80e1-79da7f9b9911 abcdefghijklmnopqrstuvwxyz0123 END-T005522 r24 deadbeef")]
    assert found == ["job-7f3a9c21", "7f3a9c21", "9d4cee55-5978-4338-80e1-79da7f9b9911", "abcdefghijklmnopqrstuvwxyz0123", "END-T005522"]
    assert [t for _, _, t in generic_tokens("12345678 and 87654321", digits_only=True)] == ["12345678", "87654321"]
    # A short worker name like r24 has the shape of "x86": only a task pattern registers it.
    assert generic_tokens("r24", min_len=3) == []


def test_first_seen_source_and_time(reg):
    res, _ = reg
    assert ident(reg, "job-7f3a9c")["first_seen"] == {"t": 3400, "seq": seq_of(res, "tool_call", 2000), "change_seq": None, "source": "tool_output", "agent": 0, "event": "tool_call"}
    assert ident(reg, "tmp/c0ffee12ab.bin")["first_seen"]["source"] == "tool_output"
    assert ident(reg, "c0ffee12ab")["first_seen"]["source"] == "tool_output"  # inside the path
    assert ident(reg, "deadbeef01")["first_seen"]["source"] == "agent_text"
    assert ident(reg, "README.md")["first_seen"]["source"] == "workspace_initial"
    assert ident(reg, "out/summary.txt")["first_seen"]["source"] == "user_text"
    final = ident(reg, "out/final.txt")["first_seen"]
    assert {k: final[k] for k in ("seq", "source", "agent", "event")} == {"seq": None, "source": "tool_file", "agent": 0, "event": "change:moved"}
    move = next(r for r in real_log(res.manifest["episode_start_utc"], **KW).records if r.get("kind") == "moved" and r["path"] == "out/final.txt")
    assert (final["t"], final["change_seq"]) == (move["t"], move["seq"]) and 20000 <= move["t"] < 20500


def test_file_sources_from_the_change_log():
    res = episode()
    log = closed_log([
        {"seq": 1, "wall": "2026-11-21T10:00:05.500Z", "kind": "created", "path": "made_by_agent.txt"},
        {"seq": 2, "wall": "2026-11-21T10:00:02.500Z", "kind": "created", "path": "made_by_tool_9f8e7d6c.out"},
    ], res.manifest["episode_start_utc"])
    t = copy.deepcopy(res.trajectory)
    for e in t.events:
        if e["type"] == "tool_call" and e["tool"] == "file_write":
            e["input"]["path"] = "made_by_agent.txt"
    r = build(t, log, TaskIdentifiers())
    by = {x["text"]: x for x in r["identifiers"]}
    assert by["made_by_tool_9f8e7d6c.out"]["first_seen"]["source"] == "tool_file"
    assert (by["made_by_tool_9f8e7d6c.out"]["first_seen"]["change_seq"], by["made_by_tool_9f8e7d6c.out"]["first_seen"]["t"]) == (1, 2500)
    assert by["9f8e7d6c"]["first_seen"]["source"] == "tool_file"  # the hash inside the file name
    assert by["made_by_agent.txt"]["first_seen"]["source"] == "agent_text"  # the call named it before the change log saw it
    assert "tmp.swp" not in by  # a write-then-rename temp file


def test_known_to_user_timing(reg):
    res, _ = reg
    assert ident(reg, "out/summary.txt")["known_to_user"]["t"] == 0
    assert ident(reg, "WKR-alpha7")["known_to_user"]["t"] == 0
    assert ident(reg, "job-7f3a9c")["known_to_user"] == {"t": 12000, "seq": seq_of(res, "user_message", 12000)}
    assert ident(reg, "README.md")["known_to_user"]["t"] == 12000
    for private in ("tmp/c0ffee12ab.bin", "c0ffee12ab", "b3f9c2a17e", "deadbeef01", "out/final.txt"):
        assert ident(reg, private)["known_to_user"] is None


def test_private_identifiers_sent_to_the_user(reg):
    res, r = reg
    final1 = seq_of(res, "assistant_message", 9000)  # delivered when its item finished, at 9 s
    stu = next(e["seq"] for e in res.trajectory.events if e["type"] == "tool_call" and e["tool"] == "send_to_user")
    q = next(e["seq"] for e in res.trajectory.events if e["type"] == "question")
    assert ident(reg, "job-7f3a9c")["sent_while_private"] == [{"t": 9000, "seq": final1, "event": "assistant_message"}]
    assert [x["seq"] for x in ident(reg, "tmp/c0ffee12ab.bin")["sent_while_private"]] == [final1, q]
    # Dated at delivery (the call's end, 13.1 s), matched in any case for hex.
    assert ident(reg, "b3f9c2a17e")["sent_while_private"] == [{"t": 13100, "seq": stu, "event": "tool_call"}]
    assert ident(reg, "c0ffee12ab")["sent_while_private"] == []  # only inside the path, counted once there
    assert ident(reg, "WKR-alpha7")["sent_while_private"] == []
    assert ident(reg, "out/summary.txt")["sent_while_private"] == []
    assert r["aux"] == {"identifiers": 15, "private_identifiers_sent_to_user": 3, "private_mentions_sent_to_user": 4}


def test_agent_mention_does_not_make_an_identifier_known(reg):
    j = ident(reg, "job-7f3a9c")
    assert j["sent_while_private"][0]["t"] < j["known_to_user"]["t"]


def test_paths_relative_absolute_and_bare_names(reg):
    # user (relative), write call (path field), final (./ form), last final (bare name alias), change log (created, rewritten).
    assert ident(reg, "out/summary.txt")["occurrences"] == 6


def test_split_identifiers_are_not_joined(reg):
    assert ident(reg, "job-7f3a9c")["occurrences"] == 4
    assert ident(reg, "job-7f3a")["occurrences"] == 1


# The reported cases --------------------------------------------------------------


def test_a_bare_name_the_user_wrote_does_not_make_a_longer_path_known():
    res = tiny(lambda r: [mini.user(r, 0, "Please use report.txt."), mini.command(r, 1, 2, "find .", "/work/project/internal/report.txt\n"),
                          mini.assistant(r, 3, "I used internal/report.txt.")])
    reg = (res, build(res.trajectory))
    assert ident(reg, "report.txt")["known_to_user"]["t"] == 0
    p = ident(reg, "internal/report.txt")
    assert p["known_to_user"] is None and len(p["sent_while_private"]) == 1


def test_a_bare_name_from_the_agent_counts_as_a_leak_of_the_unique_path():
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.command(r, 1, 2, "x", "/work/project/internal/notes-9.md\n"), mini.assistant(r, 3, "See notes-9.md.")])
    reg = (res, build(res.trajectory))
    assert len(ident(reg, "internal/notes-9.md")["sent_while_private"]) == 1


def test_case_insensitive_pattern_is_not_also_a_token():
    # JOB-7F3A9C is also a generic token (an all-hex part); it must not become a second, private identifier.
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}", "case_sensitive": False}]})
    res = tiny(lambda r: [mini.user(r, 0, "check JOB-7F3A9C"), mini.assistant(r, 1, "job-7f3a9c is done.")])
    rows = build(res.trajectory, None, task)["identifiers"]
    assert [(x["text"], x["kind"]) for x in rows] == [("JOB-7F3A9C", "job_id")]
    assert rows[0]["sent_while_private"] == [] and rows[0]["occurrences"] == 2


def test_short_tmp_paths_and_dot_paths():
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.dynamic(r, 1, 2, "read", {"path": "/work/project"}, {"content": [{"type": "text", "text": "wrote /tmp/ab and /work/project/./out/a.txt"}]}),
                          mini.assistant(r, 3, "done")])
    texts = {x["text"] for x in build(res.trajectory)["identifiers"]}
    assert texts == {"/tmp/ab", "out/a.txt"}


def test_a_relative_path_in_tool_output_is_registered():
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.command(r, 1, 2, "x", "Created jobs/run.txt\n"), mini.assistant(r, 3, "Wrote jobs/run.txt.")])
    reg = (res, build(res.trajectory))
    row = ident(reg, "jobs/run.txt")
    assert row["first_seen"]["source"] == "tool_output" and len(row["sent_while_private"]) == 1


def test_a_file_and_a_task_id_with_the_same_spelling_stay_apart():
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "build", "regex": "build-[0-9a-f]{8}"}]})
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "build-7a9f3c21"}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.command(r, 1, 2, "x", "started build-7a9f3c21\n"), mini.assistant(r, 3, "ok")])
    rows = build(res.trajectory, log, task)["identifiers"]
    assert sorted((x["text"], x["kind"]) for x in rows) == [("build-7a9f3c21", "build"), ("build-7a9f3c21", "path")]


def test_answers_to_a_subagents_question_are_user_text():
    res = episode()
    t = copy.deepcopy(res.trajectory)
    a = next(e for e in t.events if e["type"] == "answer")
    a["text"] = "use job-7f3a9c"
    a["agent"] = 1  # the registry does not care which agent asked
    x = next(x for x in trajectory_texts(t) if x.event == "answer")
    assert x.user_authored and x.source == "user_text"


def test_a_cancelled_answer_does_not_make_anything_known():
    res = episode()
    t = copy.deepcopy(res.trajectory)
    a = next(e for e in t.events if e["type"] == "answer")
    a.update(status="cancelled", text="tmp/c0ffee12ab.bin")
    reg = (res, build(t))
    assert ident(reg, "tmp/c0ffee12ab.bin")["known_to_user"] is None


def test_the_final_newline_rule_changes_no_extraction():
    res = episode()
    with_newline = copy.deepcopy(res.trajectory)
    for e in with_newline.events:
        if e["type"] == "tool_call" and e["tool"] == "shell" and e["output"]["text"] is not None:
            e["output"]["text"] += "\n"
    assert build(with_newline, None, TASK)["identifiers"] == build(res.trajectory, None, TASK)["identifiers"]


def test_from_agent_messages_are_not_user_text():
    res = run([export_segment("pi-subagent.json")])
    texts = trajectory_texts(res.trajectory)
    child_prompt = next(x for x in texts if x.agent == 1 and x.event == "user_message")
    assert child_prompt.source == "agent_text" and not child_prompt.user_authored
    assert next(x for x in texts if x.agent == 0 and x.event == "user_message").user_authored


def test_registry_cli(tmp_path):
    res = episode()
    io.write(res.trajectory, tmp_path / "e.jsonl")
    changelog.write(real_log(res.manifest["episode_start_utc"]), tmp_path / "changes.jsonl")
    out = subprocess.run([sys.executable, "-m", "cohbench.trajectory", "registry", str(tmp_path / "e.jsonl"), "--changes", str(tmp_path / "changes.jsonl"),
                          "--task", str(FIX / "identifiers-task.json")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["aux"]["private_identifiers_sent_to_user"] == 3


# The scorer's identifiers.json ---------------------------------------------------


@pytest.fixture(scope="module")
def scorer():
    res = episode()
    log = real_log(res.manifest["episode_start_utc"], **KW)
    view = scorer_view(res.trajectory)
    return view, scorer_identifiers(view, log, TASK)


def test_scorer_identifiers_refer_to_the_view_only(scorer):
    view, doc = scorer
    assert validate_identifiers(doc, view) == []
    assert set(doc) == {"format", "version", "identifiers", "aux"}
    assert all(set(r) == {"text", "kind", "known", "disclosed_while_private"} for r in doc["identifiers"])
    by = {r["text"]: r for r in doc["identifiers"]}
    assert "deadbeef01" not in by  # only in a tool argument: not something the user saw
    j = by["job-7f3a9c"]
    assert [view.events[d["seq"]]["type"] for d in j["disclosed_while_private"]] == ["to_user"]
    assert j["disclosed_while_private"][0]["t"] == 9000 and j["known"]["t"] == 12000
    assert view.events[j["known"]["seq"]]["type"] == "user_message"
    assert [d["t"] for d in by["tmp/c0ffee12ab.bin"]["disclosed_while_private"]] == [9000, 14010]
    assert by["b3f9c2a17e"]["disclosed_while_private"][0]["t"] == 13100
    assert doc["aux"] == {"private_identifiers_sent_to_user": 3, "private_mentions_sent_to_user": 4}
    text = json.dumps(doc)
    for word in ("first_seen", "occurrences", "agent", "tool_file", "agent_file", "source"):
        assert word not in text


def test_directories_from_the_change_log_are_not_identifiers():
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "out", "type": "dir", "batch": 1},
                           {"seq": 2, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "out/a.txt", "type": "file", "batch": 1}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 3, "Everything is in the out folder, see data too.")])
    v = scorer_view(res.trajectory)
    assert [x["text"] for x in scorer_identifiers(v, log)["identifiers"]] == []


def test_a_known_id_and_a_file_of_the_same_name_stay_apart():
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}]})
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "jobs/job-7f3a9c", "type": "file", "batch": 1}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "check job-7f3a9c"), mini.assistant(r, 3, "job-7f3a9c is done.")])
    doc = scorer_identifiers(scorer_view(res.trajectory), log, task)
    by = {(x["text"], x["kind"]): x for x in doc["identifiers"]}
    assert by[("job-7f3a9c", "job_id")]["known"] is not None
    assert ("jobs/job-7f3a9c", "path") not in by  # the agent named the id, not the file
    assert doc["aux"]["private_identifiers_sent_to_user"] == 0


def test_an_equal_span_path_does_not_shadow_a_case_insensitive_id():
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "build", "regex": "build-[0-9a-f]{8}", "case_sensitive": False}]})
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "build-7a9f3c21", "type": "file", "batch": 1}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 3, "Started build-7a9f3c21.")])
    rows = scorer_identifiers(scorer_view(res.trajectory), log, task)["identifiers"]
    # The bare spelling counts for the build id; the file needs ./ or an absolute spelling.
    assert sorted((x["kind"], len(x["disclosed_while_private"])) for x in rows) == [("build", 1)]


def test_external_roots_are_checked_after_normalising():
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 3, "Read /tmp/../etc/passwd and wrote /tmp/./ab.")])
    rows = scorer_identifiers(scorer_view(res.trajectory))["identifiers"]
    assert [x["text"] for x in rows] == ["/tmp/ab"]


def test_a_later_file_does_not_change_an_earlier_bare_name_disclosure():
    log = closed_log([
        {"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "out/notes-9.md", "type": "file", "batch": 1},
        {"seq": 2, "wall": "2026-11-21T10:00:45Z", "kind": "created", "path": "old/notes-9.md", "type": "file", "batch": 2},
    ], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 3, "See notes-9.md.")])
    by = {x["text"]: x for x in scorer_identifiers(scorer_view(res.trajectory), log)["identifiers"]}
    assert len(by["out/notes-9.md"]["disclosed_while_private"]) == 1


def test_abbreviations_and_domains_are_not_paths():
    res = tiny(lambda r: [mini.user(r, 0, "Use the defaults, e.g. the ones on example.com, i.e. nothing special, and keep plan.md."), mini.assistant(r, 3, "ok")])
    rows = scorer_identifiers(scorer_view(res.trajectory))["identifiers"]
    assert [x["text"] for x in rows] == ["plan.md"]


def test_baseline_files_come_before_the_opening_prompt(reg):
    assert ident(reg, "data/a.csv")["first_seen"]["source"] == "workspace_initial"
    assert ident(reg, "data/a.csv")["known_to_user"]["t"] == 0


def test_a_known_id_does_not_make_a_later_file_of_its_name_known():
    """B4: the user wrote the job id before a root file of that name existed;
    the agent's ./ spelling later names the file, which is private."""
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}]})
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:02Z", "kind": "created", "path": "job-7f3a9c", "type": "file", "batch": 1}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "Check job-7f3a9c"), mini.assistant(r, 3, "See ./job-7f3a9c.")])
    by = {(x["text"], x["kind"]): x for x in scorer_identifiers(scorer_view(res.trajectory), log, task)["identifiers"]}
    assert by[("job-7f3a9c", "job_id")]["known"]["t"] == 0
    f = by[("job-7f3a9c", "path")]
    assert f["known"] is None and len(f["disclosed_while_private"]) == 1
    # A1 of round 8: the two rows share a spelling, not a namespace; the
    # bundle's validation accepts them, and refuses a true duplicate.
    view = scorer_view(res.trajectory)
    doc = scorer_identifiers(view, log, task)
    assert validate_identifiers(doc, view) == []
    dup = {**doc, "identifiers": [doc["identifiers"][0], *doc["identifiers"]]}
    assert any("one row per identifier" in e for e in validate_identifiers(dup, view))



@pytest.mark.parametrize("second_kind", [None, "ticket"])
def test_identifiers_that_share_a_spelling_survive_the_bundle(tmp_path, second_kind):
    """A1 of rounds 8 and 9: the job id `job-7f3a9c` and the root file
    `./job-7f3a9c` are two rows; so are two declared kinds that match one
    spelling. The bundle is made, written to disk, read back and validated."""
    from cohbench.trajectory import bundle
    from cohbench.trajectory.observations import TaskObservations

    pats = [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}] + ([{"kind": second_kind, "regex": r"job-\w{6}"}] if second_kind else [])
    task = TaskIdentifiers.from_dict({"patterns": pats})
    res = tiny(lambda r: [mini.user(r, 0, "Check job-7f3a9c"), mini.assistant(r, 3, "See ./job-7f3a9c.")])
    ev = [{"seq": 1, "wall": "2026-11-21T10:00:02Z", "kind": "created", "path": "job-7f3a9c", "type": "file", "mode": "0644", "sha256": None, "content": "none", "batch": 1},
          {"seq": 2, "wall": "2026-11-21T10:00:02Z", "kind": "batch", "batch": 1}]
    b = bundle.make(res, [bundle.WatcherInput(ev, [])], task, TaskObservations())
    want = {("job-7f3a9c", "job_id"), ("job-7f3a9c", "path")} | ({("job-7f3a9c", second_kind)} if second_kind else set())
    assert {(x["text"], x["kind"]) for x in b.identifiers["identifiers"]} == want
    bundle.write(b, tmp_path / "b")
    back = bundle.read(tmp_path / "b")
    rep = bundle.validate(back, require_episode=False)
    assert rep["ok"], rep["errors"]
    assert back.identifiers == b.identifiers


def test_an_unresolved_alias_does_not_shadow_the_id_inside_it():
    """N-B3: two paths share the bare name, so the alias resolves to neither,
    and the job id inside it still counts."""
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}]})
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "a/job-7f3a9c.txt", "type": "file", "batch": 1},
                      {"seq": 2, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "b/job-7f3a9c.txt", "type": "file", "batch": 1}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 3, "Check job-7f3a9c.txt please.")])
    doc = scorer_identifiers(scorer_view(res.trajectory), log, task)
    by = {(x["text"], x["kind"]): x for x in doc["identifiers"]}
    assert len(by[("job-7f3a9c", "job_id")]["disclosed_while_private"]) == 1
    assert doc["aux"]["private_identifiers_sent_to_user"] == 1


def test_a_uuid_is_one_token():
    """N-B7: a uuid with an all-letter hex group is not split."""
    assert [t for _, _, t in generic_tokens("9d4cee55-cafe-4338-80e1-79da7f9b9911")] == ["9d4cee55-cafe-4338-80e1-79da7f9b9911"]
    assert [t for _, _, t in generic_tokens("d34db33f-0000-4000-8000-567890abcdef")] == ["d34db33f-0000-4000-8000-567890abcdef"]
    assert [t for _, _, t in generic_tokens("cafe-12345678ab")] == ["cafe-12345678ab"]  # a hex word is not a label


@pytest.mark.parametrize("change, message", [
    (lambda d, v: d["identifiers"][0].__setitem__("known", {"seq": "1", "t": 0}), "integers"),
    (lambda d, v: d["identifiers"][0].__setitem__("known", {"seq": next(e["seq"] for e in v.events if e["type"] == "turn_start"), "t": 0}), "not a user_message"),
    (lambda d, v: d["identifiers"][0]["disclosed_while_private"].append({"seq": 0, "t": 0}), "not a to_user"),
    (lambda d, v: d.__setitem__("identifiers", "nope"), "is a list"),
])
def test_identifiers_validator_checks_reference_kinds(scorer, change, message):
    view, doc = scorer
    bad = json.loads(json.dumps(doc))
    change(bad, view)
    errs = validate_identifiers(bad, view)
    assert any(message in e for e in errs), errs


def test_a_bare_name_resolves_only_among_live_paths():
    """old/answer.txt is deleted, then new/answer.txt is created: "answer.txt"
    afterwards names the new file only. Without the new file it names
    nothing alive, so it is no disclosure of the deleted one; the explicit
    old/answer.txt still names the deleted file, as history."""
    def log(with_new):
        recs = [{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "old/answer.txt", "type": "file", "batch": 1},
                {"seq": 2, "wall": "2026-11-21T10:00:02Z", "kind": "deleted", "path": "old/answer.txt", "type": "file", "batch": 2}]
        if with_new:
            recs.append({"seq": 3, "wall": "2026-11-21T10:00:03Z", "kind": "created", "path": "new/answer.txt", "type": "file", "batch": 3})
        return closed_log(recs, mini.T0)

    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 5, "See answer.txt. It replaced old/answer.txt.")])
    view = scorer_view(res.trajectory)
    by = lambda lg: {x["text"]: x for x in scorer_identifiers(view, lg, TaskIdentifiers())["identifiers"]}  # noqa: E731
    with_new = by(log(True))
    assert [d["t"] for d in with_new["new/answer.txt"]["disclosed_while_private"]] == [5000]
    assert [d["t"] for d in with_new["old/answer.txt"]["disclosed_while_private"]] == [5000]  # the explicit spelling
    without = by(log(False))
    assert "new/answer.txt" not in without
    assert [d["t"] for d in without["old/answer.txt"]["disclosed_while_private"]] == [5000]  # only through the explicit spelling
    assert without["old/answer.txt"]["disclosed_while_private"] == with_new["old/answer.txt"]["disclosed_while_private"]


def test_lifetimes_follow_moves_and_deletions():
    from cohbench.trajectory.registry import lifetimes

    recs = [{"type": "change", "kind": "created", "path": "d", "file_type": "dir", "t": 0}, {"type": "change", "kind": "created", "path": "d/a", "file_type": "file", "t": 0},
            {"type": "change", "kind": "moved", "path": "e", "from": "d", "file_type": "dir", "t": 5}, {"type": "change", "kind": "deleted", "path": "e", "file_type": "dir", "t": 9},
            {"type": "change", "kind": "created", "path": "e/a", "file_type": "file", "t": 12}]
    got = lifetimes(recs, "/work/project")
    assert got["d/a"] == [(0, 5000)] and got["e/a"] == [(5000, 9000), (12000, None)]  # microseconds, from milliseconds when no wall times
    assert lifetimes(recs, "/work/project", [0, 1, 5_000_400, 9_000_100, 12_000_000])["d/a"] == [(1, 5_000_400)]


def test_lifetimes_are_compared_on_full_precision_times():
    """A file created 400 us after the message that names it, in the same
    millisecond: the bare name does not reach it."""
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 5, "Wrote answer.txt.")])
    # The final message is delivered when its item finished, 5 s.
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:05.000400Z", "kind": "created", "path": "new/answer.txt", "type": "file", "batch": 1}], mini.T0)
    assert next(r for r in log.records if r["type"] == "change")["t"] == 5000
    view = scorer_view(res.trajectory)
    rows = {x["text"]: x for x in scorer_identifiers(view, log, TaskIdentifiers())["identifiers"]}
    assert "new/answer.txt" not in rows
    early = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:04.999600Z", "kind": "created", "path": "new/answer.txt", "type": "file", "batch": 1}], mini.T0)
    rows = {x["text"]: x for x in scorer_identifiers(view, early, TaskIdentifiers())["identifiers"]}
    assert [d["t"] for d in rows["new/answer.txt"]["disclosed_while_private"]] == [5000]


def test_a_dead_root_name_does_not_shadow_a_live_nested_file():
    """The user named answer.txt; that root file was deleted and
    new/answer.txt created. "See answer.txt." names the live file, privately."""
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:00.000Z", "kind": "created", "path": "answer.txt", "type": "file", "batch": 0},
                      {"seq": 2, "wall": "2026-11-21T10:00:02Z", "kind": "deleted", "path": "answer.txt", "type": "file", "batch": 1},
                      {"seq": 3, "wall": "2026-11-21T10:00:03Z", "kind": "created", "path": "new/answer.txt", "type": "file", "batch": 2}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "Fix answer.txt."), mini.assistant(r, 5, "See answer.txt.")])
    rows = {x["text"]: x for x in scorer_identifiers(scorer_view(res.trajectory), log, TaskIdentifiers())["identifiers"]}
    assert [d["t"] for d in rows["new/answer.txt"]["disclosed_while_private"]] == [5000]


def test_files_moved_by_a_directory_rename_are_identifiers():
    """N3 and Major 3: the watcher writes one record for a directory move,
    none per child; the children it carried are paths a bare name reaches."""
    log = closed_log([{"seq": 1, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "draft", "type": "dir", "batch": 1},
                      {"seq": 2, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "draft/result", "type": "file", "batch": 1},
                      {"seq": 3, "wall": "2026-11-21T10:00:01Z", "kind": "created", "path": "draft/secret_99.txt", "type": "file", "batch": 1},
                      {"seq": 4, "wall": "2026-11-21T10:00:02Z", "kind": "moved", "path": "published", "from": "draft", "type": "dir", "batch": 2}], mini.T0)
    res = tiny(lambda r: [mini.user(r, 0, "go"), mini.assistant(r, 5, "See secret_99.txt and published/result.")])
    rows = {x["text"]: x for x in scorer_identifiers(scorer_view(res.trajectory), log, TaskIdentifiers())["identifiers"]}
    assert {"published/secret_99.txt", "published/result"} <= set(rows)
    assert all(rows[k]["disclosed_while_private"] for k in ("published/secret_99.txt", "published/result"))


def test_tool_output_completed_after_the_end_is_not_in_the_audit_registry():
    """Reviewer 1's minor of round 11: a call began before the end and its
    result came back after it; that result is outside the episode."""
    from cohbench.trajectory import Hints, RunnerEvent

    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 9)
    items = [mini.user(r, 0, "go"), mini.command(r, 2, 3, "cat a", "job-aaaaaa\n"), mini.command(r, 4, 8, "cat b", "job-bbbbbb\n")]
    res = run([Segment(t3.from_export(mini.export([(th, [r], items)])))],
              hints=Hints(runner=[RunnerEvent("episode_end", mini.ts(6), None, {"reason": "finished"})]))
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}]})
    texts = {x["text"] for x in build(res.trajectory, None, task)["identifiers"]}
    assert "job-aaaaaa" in texts and "job-bbbbbb" not in texts


@pytest.mark.parametrize("status", ["declined", "cancelled", "dismissed"])
def test_a_refused_secret_requests_words_are_user_text_in_the_audit_registry(status):
    """m4 of round 11: the fixed refusal the runner passes in Hints.answers
    for a declined secret request is the user's text."""
    from cohbench.trajectory import Hints

    th = mini.thread()
    r = mini.run_rec(th["id"], 1, 0, 5)
    sec = mini.item(r, "secret_request", 1, 3, requestId="sec-1", secretStatus=status, title="API key", fields=[{"name": "k", "label": "key", "secret": True}])
    items = [mini.user(r, 0, "go"), sec, mini.assistant(r, 4, "ok")]
    res = run([Segment(t3.from_export(mini.export([(th, [r], items)])))], hints=Hints(answers={"sec-1": "No, use job-cccccc instead."}))
    task = TaskIdentifiers.from_dict({"patterns": [{"kind": "job_id", "regex": "job-[0-9a-f]{6}"}]})
    row = next(x for x in build(res.trajectory, None, task)["identifiers"] if x["text"] == "job-cccccc")
    assert row["known_to_user"] is not None
