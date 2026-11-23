"""Round-one reader regressions. No paid endpoints."""

import json
from pathlib import Path
import subprocess


def test_v4_published_calibration_and_unit_readmes_have_no_machine_paths():
    root = Path(__file__).resolve().parents[2]
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files", "calibration", "cohbench"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        # An export without git metadata: every file there is a published one.
        tracked = [
            path.relative_to(root).as_posix()
            for top in ("calibration", "cohbench")
            for path in sorted((root / top).rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.name != "LEARNINGS.md"
        ]
    forbidden = ("/Users/", "/home/", "/private/", ".t3/")
    violations = [
        name
        for name in tracked
        if (name.startswith("calibration/") or name.endswith("/README.md"))
        and any(marker in (root / name).read_text(errors="replace") for marker in forbidden)
    ]
    assert not violations, violations


import copy
import fcntl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from cohbench.scoring import reader as R, explanation, stats, report
from .test_explanation import hosted, fake, completion, episode, cfg, CAUSE
from .build import OPTIONS


def test_s4_key_question_used_for_both_answers_and_provenance(tmp_path):
    ep = episode(tmp_path, CAUSE, CAUSE)
    ep.payload["question"] = "Why is the machine slow?"
    seen = []

    class Spy:
        def choose(self, question, excerpt, shown):
            seen.append(question)
            return 0

        def describe(self):
            return {}

    result = explanation.score(ep, Spy(), cfg())
    assert set(seen) == {ep.payload["question"]}
    assert result["question_source"] == "answer_key"
    del ep.payload["question"]
    assert explanation.score(ep, Spy(), cfg())["question_source"] == "opening_prompt"


def test_s4_generator_stores_main_question_without_horizon_chores():
    from cohbench.tasks.common import Options
    from cohbench.tasks.explanation import generate

    a, b = [generate(Options("explanation", 1, h)).task for h in ("h20", "h80")]
    q = a["scoring"]["answer_key"]["question"]
    assert q == b["scoring"]["answer_key"]["question"] == a["prompt"]["opening"]
    assert q != b["prompt"]["opening"]


def test_b6_complete_cache_replays_without_credentials(tmp_path, monkeypatch):
    transport, calls = fake(completion("C"))
    path = tmp_path / "cache"
    R.RecordedReader(R.make(hosted(), key="test", transport=transport), path).choose(
        "q", "a", OPTIONS
    )
    monkeypatch.delenv(R.KEY_ENV, raising=False)
    rd = R.RecordedReader(R.make(hosted(), transport=transport), path)
    assert rd.choose("q", "a", OPTIONS) == 2
    assert len(calls) == 1
    with pytest.raises(R.ReaderError, match="key"):
        rd.choose("new", "a", OPTIONS)


def test_b3_cache_covers_rendered_template(tmp_path, monkeypatch):
    transport, calls = fake(completion("A"))
    rd = R.RecordedReader(
        R.make(hosted(), key="test", transport=transport), tmp_path / "cache"
    )
    rd.choose("q", "a", OPTIONS)
    original = R.user_message
    monkeypatch.setattr(
        R, "user_message", lambda *args: original(*args) + "\nChanged instruction."
    )
    rd.choose("q", "a", OPTIONS)
    assert len(calls) == 2
    rows = [json.loads(s) for s in (tmp_path / "cache").read_text().splitlines()]
    assert len({r["request_digest"] for r in rows}) == 2


def test_b3_parsed_config_is_deeply_immutable():
    raw = copy.deepcopy(hosted().raw)
    parsed = R.parse_config(raw)
    digest = parsed.digest
    raw["sampling"]["max_tokens"] = 33
    assert parsed.digest == digest
    with pytest.raises(TypeError):
        parsed.raw["sampling"]["seed"] = 99
    with pytest.raises((TypeError, AttributeError)):
        parsed.raw["endpoint"]["provider"]["order"].append("other")


@pytest.mark.parametrize("tail", [b'{"truncated":', b""])
def test_b1_cache_tail_repaired_before_append(tmp_path, tail):
    path = tmp_path / "cache"
    t, calls = fake(completion("A"))
    rd = R.RecordedReader(R.make(hosted(), key="test", transport=t), path)
    rd.choose("q", "a", OPTIONS)
    data = path.read_bytes()
    path.write_bytes(data + tail if tail else data.rstrip(b"\n"))
    rd = R.RecordedReader(R.make(hosted(), key="test", transport=t), path)
    assert rd.choose("q", "a", OPTIONS) == 0
    rd.choose("other", "a", OPTIONS)
    assert len(calls) == 2
    assert len([json.loads(s) for s in path.read_text().splitlines()]) == 2


@pytest.mark.parametrize(
    "line",
    [
        b"{bad}\n",
        b'{"reader_digest":"x","reader_digest":"x","request_digest":"y","choice":"A"}\n',
    ],
)
def test_b1_b4_bad_complete_rows_name_line(tmp_path, line):
    path = tmp_path / "cache"
    path.write_bytes(line)
    with pytest.raises(R.ReaderError, match="line 1"):
        R.RecordedReader(R.make(hosted(), key="test"), path).choose("q", "a", OPTIONS)


def test_b2_concurrent_scorers_do_not_hold_lock_over_transport(tmp_path):
    barrier = threading.Barrier(2)

    def transport(letter):
        def exchange(*args):
            barrier.wait(timeout=2)
            return 200, json.dumps(completion(letter)).encode()

        return exchange

    readers = [
        R.RecordedReader(
            R.make(hosted(), key="test", transport=transport(letter)),
            tmp_path / "cache",
        )
        for letter in "AB"
    ]
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda r: r.choose("q", "a", OPTIONS), readers))
    assert answers[0] == answers[1]
    assert len((tmp_path / "cache").read_text().splitlines()) == 1


def test_b2_cache_lock_wait_uses_choice_deadline(tmp_path):
    path = tmp_path / "cache"
    path.write_text("")
    # Stable lock inode is needed because atomic repair replaces the data inode.
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rd = R.RecordedReader(
            R.make(
                hosted(timeout_s=0.03), key="test", transport=fake(completion("A"))[0]
            ),
            path,
        )
        started = time.monotonic()
        with pytest.raises(R.ReaderError, match="deadline"):
            rd.choose("q", "a", OPTIONS)
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["endpoint"].pop("provider"),
        lambda r: r["sampling"].pop("temperature"),
        lambda r: r["orders"].update(seed=True),
        lambda r: r["sampling"].update(temperature=False),
        lambda r: r.update(version=True),
    ],
)
def test_b5_missing_required_fields_and_booleans_refused(edit):
    raw = copy.deepcopy(hosted().raw)
    edit(raw)
    with pytest.raises(R.ReaderError):
        R.parse_config(raw)


def test_s1_smoke_bounds_are_possible_probabilities():
    for two in (True, False):
        assert all(
            0 <= v <= 1
            for v in stats.near_chance(0, 6, 1 / 6, 2, two, clusters=1)["bound"]
        )


def test_b7_compatibility_modules_export_previous_names():
    from cohbench.user import api, ledger

    for name in ("_usage", "_Final", "CHUNK", "Config", "P", "_no_duplicates"):
        assert hasattr(api, name), name
    for name in ("EndpointError", "os", "tempfile", "contextmanager"):
        assert hasattr(ledger, name), name


def test_s1_large_calibration_thresholds_and_sample_sizes():
    from cohbench.scoring.calibration import assess

    groups = {
        name: {"instances": n, "n": n * 6, "correct": correct, "positions": [n] * 6}
        for name, n, correct in [
            ("no_answer", 200, 200),
            ("buried", 200, 300),
            ("ideal", 60, 342),
            ("worst", 60, 36),
        ]
    }
    assert assess(groups)["passed"]
    for name in groups:
        changed = copy.deepcopy(groups)
        changed[name]["correct"] += (
            -1 if name == "ideal" else 1 if name in ("buried", "worst") else 61
        )
        assert not assess(changed)["passed"], name
    groups["no_answer"]["instances"] = 199
    assert not assess(groups)["passed"]


def test_s3_missing_manifest_disables_release_bands(tmp_path):
    from .test_review6 import release_inputs

    root, plan, _ = release_inputs(tmp_path)
    result = report.build(root, plan=plan, require_plan=True)
    assert result["not_release"]
    assert "calibration manifest" in result["parameters"]["nonrelease_note"]
    assert all(
        row["headline"]["band"] is None
        for section in result["sections"]
        for table in section["tables"]
        for row in table["rows"]
    )


def test_s6_summarizer_uses_production_smoke_bounds():
    from calibration.reader.summarize import smoke_bound

    assert smoke_bound(0, 72, 12, "no_answer") == stats.near_chance(
        0, 72, 1 / 6, 2, True, clusters=12
    )


def test_s2_s5_limits_are_explicit():
    text = Path("cohbench/scoring/README.md").read_text()
    for phrase in (
        "agent-written identity",
        "not weight files",
        "prefix rules out one red herring",
        "host-side file",
        "Markdown markers",
        "unspaced scripts",
        "smoke check",
    ):
        assert phrase in text


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["reader"].update(config_sha256="b" * 64),
        lambda d: d["reader"].update(config_sha256=[]),
        lambda d: d["classifier"].update(config_sha256=[]),
        lambda d: d["classifier"].update(config_sha256="d" * 64),
        lambda d: d["reader"]["large_sample"]["groups"]["no_answer"].update(correct=0),
        lambda d: d["reader"]["large_sample"]["groups"]["buried"].update(correct=301),
        lambda d: d["reader"]["large_sample"]["groups"]["ideal"].update(correct=341),
        lambda d: d["reader"]["large_sample"]["groups"]["worst"].update(correct=37),
        lambda d: d["reader"]["human"].update(kappa=0.74),
        lambda d: d["classifier"]["human"].update(n=149, correct=149),
        lambda d: d["reader"]["human"].update(n=99, correct=99),
        lambda d: d["reader"]["human"].update(annotators=["one"]),
        lambda d: d["classifier"]["human"].pop("date"),
        lambda d: d["reader"]["human"].update(
            disagreement_by_model={"m": 0, "other": 0.051}
        ),
        lambda d: d["reader"]["timed_humans"].update(disagreements_resolved=False),
        lambda d: d["reader"]["large_sample"].update(groups=None),
    ],
)
def test_s3_invalid_manifest_never_releases(tmp_path, mutation):
    from .test_review6 import release_inputs
    from . import synth as S

    root, plan, _ = release_inputs(tmp_path)
    path = S.manifest(root)
    valid = report.build(root, plan=plan, require_plan=True, calibration_manifest=path)
    assert not valid["not_release"]
    doc = json.loads(path.read_text())
    mutation(doc)
    path.write_text(json.dumps(doc))
    result = report.build(root, plan=plan, require_plan=True, calibration_manifest=path)
    assert result["not_release"] and result["parameters"]["calibration"]["reasons"]
    assert all(
        row["headline"]["band"] is None
        for section in result["sections"]
        for table in section["tables"]
        for row in table["rows"]
    )


def test_s3_classifier_provenance_is_validated_and_carried(tmp_path):
    from cohbench.trajectory.episode import validate
    from cohbench.scoring.score import score_episode

    ep = episode(tmp_path, CAUSE, CAUSE)
    ep.doc["classifier_config_sha256"] = "c" * 64
    assert not any("classifier_config" in e for e in validate(ep.doc))
    assert score_episode(ep)["classifier_config_sha256"] == "c" * 64
    ep.doc["classifier_config_sha256"] = "bad"
    assert any("classifier_config" in e for e in validate(ep.doc))


def test_s1_calibration_sample_is_fresh_and_has_revised_allocation():
    from cohbench.scoring.calibration import sample

    rows = sample()
    assert len(rows) == 520
    assert {r["seed"] for r in rows} == set(range(5000, 5200))
    for name, n in [("no_answer", 200), ("buried", 200), ("ideal", 60), ("worst", 60)]:
        assert sum(r["input"] == name for r in rows) == n
    assert all(len(r["excerpt"].split()) <= 80 for r in rows)
    with pytest.raises(ValueError):
        sample(start=1)


def test_s1_position_only_reader_fails_positive_control():
    from cohbench.scoring.calibration import assess

    groups = {
        name: {
            "instances": n,
            "n": n * 6,
            "correct": n,
            "positions": [n * 6, 0, 0, 0, 0, 0],
        }
        for name, n in [
            ("no_answer", 200),
            ("buried", 200),
            ("ideal", 60),
            ("worst", 60),
        ]
    }
    result = assess(groups)
    assert not result["passed"] and not result["checks"]["ideal"]["passed"]
