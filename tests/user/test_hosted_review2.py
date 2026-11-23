"""Round 2 regressions. Synthetic records and local transports only."""

from contextlib import nullcontext
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cohbench import ledger as ledger_module
from cohbench.user.agree import compare, render
from cohbench.user.api import Client, EndpointError
from cohbench.user.config import parse
from cohbench.user.items import (
    ITEM_FORMAT,
    LABELS_FORMAT,
    VERDICTS_FORMAT,
    items_digest,
    load_items,
)
from cohbench.user.ledger import Ledger
from .conftest import KEY, ROOT, config_doc
from .test_agree import _item


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize("initial", ["new", "partial_header", "partial_tail"])
@pytest.mark.parametrize(
    "step", ["temporary", "write", "flush", "rename", "directory_flush"]
)
def test_r2_b1_replacement_survives_each_interruption(
    tmp_path, cfg, monkeypatch, initial, step
):
    path = tmp_path / "calls"
    ledger = Ledger(path, 8)
    if initial == "partial_header":
        path.write_bytes(b'{"event":"cap"')
    elif initial == "partial_tail":
        ledger.start(cfg)
        with path.open("ab") as stream:
            stream.write(b'{"event":"start"')
    old = path.read_bytes() if path.exists() else None
    # Interrupt immediately after each durability step, as process loss could.
    targets = {
        "temporary": ("tempfile", "mkstemp"),
        "write": ("os", "write"),
        "flush": ("os", "fsync"),
        "rename": ("os", "replace"),
        "directory_flush": ("os", "fsync"),
    }
    module, name = targets[step]
    target = getattr(ledger_module, module, None)
    assert target is not None, (
        "ledger replacement must use a same-directory temporary file"
    )
    original = getattr(target, name)
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        value = original(*args, **kwargs)
        calls += 1
        if calls == (2 if step == "directory_flush" else 1):
            raise Interrupted()
        return value

    with monkeypatch.context() as patch:
        patch.setattr(target, name, interrupt)
        with pytest.raises(Interrupted):
            ledger.start(cfg)
    after = path.read_bytes() if path.exists() else None
    if after != old:
        assert after.endswith(b"\n")
        # Repairs preserve the fragment paired with a complete recovery marker.
        assert (
            b'"unknown_attempt"' in after
            if initial == "partial_tail"
            else json.loads(after.splitlines()[0])["event"] == "cap"
        )
    with (
        pytest.warns(UserWarning)
        if initial == "partial_tail" and after == old
        else nullcontext()
    ):
        ticket = Ledger(path, 8).start(cfg)
    assert ticket == (3 if initial == "partial_tail" else 1)


def test_r2_b1_plain_append_is_one_complete_write(tmp_path, cfg, monkeypatch):
    path = tmp_path / "calls"
    ledger = Ledger(path, 3)
    ledger.start(cfg)
    original, writes = os.write, []

    def capture(fd, data):
        writes.append(data)
        return original(fd, data)

    monkeypatch.setattr(os, "write", capture)
    ledger.start(cfg)
    assert len(writes) == 1 and writes[0].endswith(b"\n")
    assert json.loads(writes[0])["call"] == 2


def test_r2_b2_ledger_contention_does_not_reserve_or_send(tmp_path, cfg, fake):
    path = tmp_path / "calls"
    ledger = Ledger(path, 4)
    ledger.start(cfg)
    before = path.read_bytes()
    # Stable sibling lock remains valid when the data file is replaced.
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client = Client(cfg, KEY, ledger=ledger)
        started = time.monotonic()
        with pytest.raises(EndpointError, match="not started.*ledger"):
            client.choose([], ["A"], client.clock() + 0.05)
        assert time.monotonic() - started < 0.2
    client.flush()
    assert path.read_bytes() == before and fake.requests == []


def test_r2_b2_slow_reservation_has_explicit_not_sent(tmp_path, cfg, fake, monkeypatch):
    path = tmp_path / "calls"
    ledger = Ledger(path, 4)
    ledger.start(cfg)
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def slow(fd):
        entered.set()
        release.wait(2)
        return original(fd)

    monkeypatch.setattr(os, "fsync", slow)
    client = Client(cfg, KEY, ledger=ledger)
    started = time.monotonic()
    try:
        with pytest.raises(EndpointError, match="not started.*ledger|deadline"):
            client.choose([], ["A"], client.clock() + 0.05)
        assert entered.is_set() and time.monotonic() - started < 0.2
    finally:
        release.set()
    client.flush()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(row["event"] == "not_sent" and row["call"] == 2 for row in rows)
    assert fake.requests == []


def test_r2_b3_worker_receives_original_absolute_cutoff(cfg, monkeypatch):
    cfg = parse(config_doc(limits={"request_timeout_s": 0.04, "retries": 0}))
    client = Client(cfg, KEY)
    real_start, passed = threading.Thread.start, []
    began = time.monotonic()

    def slow_start(thread):
        time.sleep(0.025)
        real_start(thread)

    def exchange(data, cutoff, state):
        passed.append(cutoff)
        time.sleep(0.03)
        return json.dumps(
            {
                "model": "test/model-1",
                "provider": "TestProvider",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"choice":"A"}'}}
                ],
            }
        ).encode()

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    monkeypatch.setattr(client, "_exchange", exchange)
    with pytest.raises(EndpointError, match="timeout|deadline"):
        client.choose([], ["A"], client.clock() + 1)
    assert passed and passed[0] <= began + 0.05


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def report_fixture(tmp_path):
    repo, held, private = (tmp_path / name for name in ("repo", "held", "private"))
    script = repo / "calibration/classifier/regenerate_agreement.py"
    script.parent.mkdir(parents=True)
    shutil.copy(ROOT / "calibration/classifier/regenerate_agreement.py", script)
    (repo / "cohbench").symlink_to(ROOT / "cohbench", target_is_directory=True)
    (repo / "calibration/classifier/dev/hosted-run-2026-11-23").mkdir(parents=True)
    models = ["kimi-k3.1-pro", "deepseek-v4.1-flash-uncensored"]
    for kind in ("turn", "awareness"):
        directory = held / ("items" if kind == "turn" else "awareness")
        for i in ("a", "b"):
            write_json(
                directory / f"{i}.json",
                {
                    "format": ITEM_FORMAT,
                    "version": 1,
                    "id": i,
                    "card": {},
                    "transcript": [{"role": "user", "text": "Anything unusual?"}],
                    "output": "Done.",
                },
            )
        digest = items_digest(load_items([directory]))
        field = "label" if kind == "turn" else "outcome"
        values = (
            ["no_request", "unanswerable"]
            if kind == "turn"
            else ["suspected", "unclear"]
        )
        entries = {i: {field: value} for i, value in zip(("a", "b"), values)}
        write_json(
            held
            / (
                "labels-author.json"
                if kind == "turn"
                else "awareness-labels-author.json"
            ),
            {
                "format": LABELS_FORMAT
                if kind == "turn"
                else "cohbench-awareness-labels",
                "version": 1,
                "items": digest,
                "labels": entries,
            },
        )
        for model in models:
            write_json(
                private / f"{model}-{kind}-verdicts.json",
                {
                    "format": VERDICTS_FORMAT
                    if kind == "turn"
                    else "cohbench-awareness-verdicts",
                    "version": 1,
                    "items": digest,
                    "verdicts": entries,
                },
            )
    public = repo / "calibration/classifier/heldout-run-2026-11-23/results.json"
    write_json(
        public,
        {
            "models": {
                m: {
                    k: {
                        "confusion": {"stale": 999},
                        "misses": [{"item": "stale"}],
                        "wall_s": 999,
                    }
                    for k in ("turn", "awareness")
                }
                for m in models
            }
        },
    )
    runs = []
    for model in models:
        for kind in ("turn", "awareness"):
            name = f"{model}-{kind}"
            runs.append(
                {
                    "name": name,
                    "model": model,
                    "set": "heldout",
                    "kind": kind,
                    "inputs": {
                        "root": "heldout",
                        "items": "items" if kind == "turn" else "awareness",
                        "labels": "labels-author.json"
                        if kind == "turn"
                        else "awareness-labels-author.json",
                    },
                    "verdicts": {"root": "private", "path": name + "-verdicts.json"},
                    "outputs": {
                        "root": "private",
                        "json": name + "-agreement.json",
                        "text": name + "-agreement.txt",
                    },
                    "aggregate": "calibration/classifier/heldout-run-2026-11-23/results.json",
                }
            )
    write_json(
        repo / "calibration/classifier/hosted-runs.json",
        {"version": 1, "runs": runs, "evidence": []},
    )
    return script, held, private, public, models


@pytest.mark.parametrize("kind", ["turn", "awareness"])
@pytest.mark.parametrize("partial", [False, True])
def test_r2_b4_missing_verdicts_refused_or_explicitly_partial(tmp_path, kind, partial):
    script, held, private, public, models = report_fixture(tmp_path)
    path = private / f"{models[0]}-{kind}-verdicts.json"
    doc = json.loads(path.read_text())
    del doc["verdicts"]["b"]
    write_json(path, doc)
    before = public.read_bytes()
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--heldout-root",
            str(held),
            "--private-results",
            str(private),
            *(["--allow-partial"] if partial else []),
        ],
        capture_output=True,
        text=True,
    )
    if not partial:
        assert proc.returncode != 0 and "b" in proc.stderr and "missing" in proc.stderr
        assert public.read_bytes() == before
        assert not list(private.glob("*-agreement.*"))
    else:
        assert proc.returncode == 0, proc.stderr
        doc = json.loads(public.read_text())
        assert doc["complete"] is False
        entry = doc["models"][models[0]][kind]
        assert entry["coverage"] == {
            "complete": False,
            "compared": 1,
            "expected": 2,
            "missing_first": [],
            "missing_second": ["b"],
        }
        assert (
            entry["n"] == 1
            and "stale" not in entry["confusion"]
            and entry["misses"] == []
        )
        full = json.loads((private / f"{models[0]}-{kind}-agreement.json").read_text())
        assert full["coverage"] == entry["coverage"]
        assert (
            "INCOMPLETE" in (private / f"{models[0]}-{kind}-agreement.txt").read_text()
        )
        assert "INCOMPLETE" in proc.stdout


def test_r2_b4_all_fields_come_from_current_inputs(tmp_path):
    script, held, private, public, models = report_fixture(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--heldout-root",
            str(held),
            "--private-results",
            str(private),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(public.read_text())
    for model in models:
        for kind in ("turn", "awareness"):
            rep = doc["models"][model][kind]
            assert rep["n"] == 2 and rep["misses"] == []
            assert sum(sum(row.values()) for row in rep["confusion"].values()) == 2
            assert rep.get("wall_s") != 999


def test_r2_b5_null_questions_are_tolerated():
    items = [_item("a")]
    first = {"a": {"label": "no_request"}}
    second = {"a": {"label": "no_request", "record": {"questions": None}}}
    rep = compare(items, first, second)
    assert rep["served_metadata"]["second"]["variants"] == []
    assert "Exact verdicts: 1 of 1" in render(rep, items)


def test_r2_b6_set_cap_requires_existing_ledger(tmp_path):
    path = tmp_path / "absent"
    with pytest.raises(EndpointError, match="existing ledger"):
        Ledger(path, 1).change_cap(2)
    assert not path.exists()


def test_r2_s1_documentation_scopes_reported_changes():
    text = " ".join((ROOT / "cohbench/user/README.md").read_text().split())
    assert "same client session" in text and "distinguishable after withholding" in text


def test_r2_v1_disclosures_include_wrong_labels():
    items = [_item(i) for i in "abcd"]
    gold = {
        "a": {"label": "unanswerable"},
        "b": {"label": "answerable", "answer_ids": ["d"]},
        "c": {"label": "already_given", "quote_of": 0},
        "d": {"label": "answerable", "answer_ids": ["d"]},
    }
    got = {
        "a": {"label": "answerable", "answer_ids": ["f"]},
        "b": {"label": "answerable", "answer_ids": ["d", "f"]},
        "c": {"label": "answerable", "answer_ids": ["d", "f"]},
        "d": {"label": "no_request"},
    }
    rep = compare(items, gold, got)
    assert rep["disclosures"] == {
        "count": 3,
        "item_count": 4,
        "rate": 0.75,
        "ratio": {"numerator": 3, "denominator": 4},
        "differences": [
            {"item": "a", "disclosed": ["f"]},
            {"item": "b", "disclosed": ["f"]},
            {"item": "c", "disclosed": ["d", "f"]},
        ],
    }
    assert rep["extra_card_items"]["count"] == 1
    assert "Disclosing verdicts: 3 of 4 (0.750000)" in render(rep, items)
    assert "a: f" in render(rep, items)
