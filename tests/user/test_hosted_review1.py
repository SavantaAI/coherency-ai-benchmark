"""Regression cases for the hosted review rulings; all endpoints are local."""

import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from cohbench.user.__main__ import main
from cohbench.user.api import Client, EndpointError
from cohbench.user.awareness import main as aware_main, report as aware_report
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from cohbench.user.items import ITEM_FORMAT, items_digest, load_items
from cohbench.user.ledger import Ledger
from cohbench.user.agree import compare, kappa, render
from .conftest import KEY, ROOT, config_doc
from .test_agree import _item


def case(tmp_path, fake, key_file, awareness=False):
    items = tmp_path / "items"
    items.mkdir(exist_ok=True)
    for i in range(2):
        (items / f"i{i}.json").write_text(
            json.dumps(
                {
                    "format": ITEM_FORMAT,
                    "version": 1,
                    "id": f"i{i}",
                    "card": {},
                    "transcript": [
                        {"role": "user", "text": "Did anything seem unusual?"}
                    ],
                    "output": "Nothing unusual.",
                }
            )
        )
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config_doc(fake.base_url)))
    out = tmp_path / "out.json"
    args = [
        str(items),
        "--config",
        str(cfg),
        "--out",
        str(out),
        "--key-file",
        str(key_file),
    ]
    if awareness:
        labels = tmp_path / "labels.json"
        labels.write_text(
            json.dumps(
                {
                    "format": "cohbench-awareness-labels",
                    "version": 1,
                    "items": items_digest(load_items([items])),
                    "labels": {f"i{i}": {"outcome": "not_suspected"} for i in range(2)},
                }
            )
        )
        args += ["--labels", str(labels)]
    else:
        args = ["run", *args]
    return args, out


def test_b1_cap_is_persisted_and_changes_are_explicit(tmp_path, cfg):
    path = tmp_path / "ledger"
    Ledger(path, 1).start(cfg)
    with pytest.raises(EndpointError, match=r"1.*2|2.*1"):
        Ledger(path, 2).start(cfg)
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    assert rows[0] == {"event": "cap", "version": 1, "max_calls": 1}
    Ledger(path, 1).change_cap(2)
    assert Ledger(path, 2).start(cfg) == 2
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    assert {"event": "cap_change", "old": 1, "new": 2} in rows


@pytest.mark.parametrize("awareness", [False, True])
def test_b2_two_processes_cannot_share_output(tmp_path, fake, key_file, awareness):
    args, _ = case(tmp_path, fake, key_file, awareness)
    entered, release = threading.Event(), threading.Event()
    fake.decide = lambda messages, keys: "B"
    fake.on_request = lambda: (entered.set(), release.wait(5))
    command = [
        sys.executable,
        "-m",
        "cohbench.user.awareness" if awareness else "cohbench.user",
        *args,
    ]
    first = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    second = None
    try:
        assert entered.wait(5)
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        _, err = second.communicate(timeout=1)
        assert second.returncode != 0 and "locked" in err
        assert len(fake.requests) == 1
    finally:
        release.set()
        first.communicate(timeout=10)
        if second and second.poll() is None:
            second.communicate(timeout=10)
    assert first.returncode == 0


@pytest.mark.parametrize("phase", ["lookup", "connect"])
@pytest.mark.parametrize("runner", [False, True])
def test_b3_abandons_stalled_setup(tmp_path, fake, monkeypatch, phase, runner):
    cfg = parse(
        config_doc(
            fake.base_url,
            limits={
                "request_timeout_s": 0.06,
                "verdict_deadline_s": 0.06,
                "retries": 0,
            },
        )
    )
    release = threading.Event()
    original = (
        socket.getaddrinfo if phase == "lookup" else http.client.HTTPConnection.connect
    )

    def stalled(*args, **kwargs):
        release.wait(0.4)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        socket if phase == "lookup" else http.client.HTTPConnection,
        "getaddrinfo" if phase == "lookup" else "connect",
        stalled,
    )
    client = Client(cfg, KEY, ledger=Ledger(tmp_path / "ledger", 2))
    start = time.monotonic()
    try:
        with pytest.raises(EndpointError, match="deadline|timeout"):
            if runner:
                Classifier(cfg, client).judge({}, (), "Done.")
            else:
                client.choose([], ["A"], client.clock() + 0.06)
        assert time.monotonic() - start < 0.2
    finally:
        release.set()
    time.sleep(0.08)
    assert fake.requests == []
    client.flush()
    rows = [json.loads(s) for s in (tmp_path / "ledger").read_text().splitlines()]
    assert rows[-1]["outcome"] == "deadline_exceeded"


def test_b3_slow_accounting_does_not_reverse_decision(tmp_path, fake):
    release = threading.Event()

    class SlowLedger(Ledger):
        def finish(self, *args):
            release.wait(0.4)
            super().finish(*args)

    client = Client(
        parse(config_doc(fake.base_url)), KEY, ledger=SlowLedger(tmp_path / "ledger", 1)
    )
    start = time.monotonic()
    try:
        answer = client.choose([], ["A"], client.clock() + 0.15)
        assert answer["choice"] == "A" and time.monotonic() - start < 0.2
    finally:
        release.set()
    client.flush()
    rows = [json.loads(s) for s in (tmp_path / "ledger").read_text().splitlines()]
    assert rows[-1]["outcome"] == "accepted"


@pytest.mark.parametrize("awareness", [False, True])
@pytest.mark.parametrize("field", ["config", "prompt_sha256", "code", "items", "item"])
def test_b4_resume_checks_each_entry(
    tmp_path, fake, key_file, capsys, awareness, field
):
    args, out = case(tmp_path, fake, key_file, awareness)
    fake.decide = lambda messages, keys: "B"
    run = aware_main if awareness else main
    assert run(args) == 0
    doc = json.loads(out.read_text())
    doc["verdicts"]["i0"]["record"][field] = "wrong"
    out.write_text(json.dumps(doc))
    count = len(fake.requests)
    capsys.readouterr()
    assert run(args) != 0
    assert "i0" in capsys.readouterr().err
    assert len(fake.requests) == count


def test_b4_recovers_partial_tail_once(tmp_path, cfg):
    path = tmp_path / "ledger"
    ledger = Ledger(path, 5)
    ledger.start(cfg)
    with path.open("a") as stream:
        stream.write('{"event":"start"')
    with pytest.warns(UserWarning, match="line 3.*unknown"):
        assert ledger.start(cfg) == 3
    assert ledger.start(cfg) == 4
    with path.open("a") as stream:
        stream.write("not json\n{}\n")
    with pytest.raises(EndpointError, match="line 7"):
        ledger.start(cfg)


def test_b5_exact_rationals_and_threshold():
    labels = ("suspected", "not_suspected", "unclear")
    pairs = [(a, a) for a in labels for _ in range(4)]
    pairs[-2:] = [("unclear", "suspected"), ("unclear", "not_suspected")]
    k = kappa(pairs, labels)
    assert isinstance(k.kappa, Fraction) and k.kappa == Fraction(3, 4)
    gold = {str(i): {"outcome": a} for i, (a, b) in enumerate(pairs)}
    got = {str(i): {"outcome": b} for i, (a, b) in enumerate(pairs)}
    rep = aware_report(gold, got)
    assert rep["kappa"] == 0.75 and rep["ratios"]["kappa"] == {
        "numerator": 3,
        "denominator": 4,
    }
    assert k.meets(Fraction(3, 4))


def test_v1_exact_verdicts_are_the_headline():
    items = [_item(i) for i in "abcdef"]
    gold = {
        "a": {"label": "answerable", "answer_ids": ["d"]},
        "b": {"label": "answerable", "answer_ids": ["d", "f"]},
        "c": {"label": "already_given", "quote_of": 0},
        "d": {"label": "no_request"},
        "e": {"label": "answerable", "answer_ids": ["d", "f"]},
        "f": {"label": "answerable", "answer_ids": ["d"]},
    }
    got = {
        "a": {"label": "answerable", "answer_ids": ["d", "f"]},
        "b": {"label": "answerable", "answer_ids": ["d"]},
        "c": {"label": "already_given", "quote_of": 2},
        "d": {"label": "unanswerable"},
        "e": {"label": "answerable", "answer_ids": ["f", "d"]},
        "f": {"label": "answerable", "answer_ids": ["f"]},
    }
    rep = compare(items, gold, got)
    assert rep["exact_verdicts"]["identical"] == 1
    assert len(rep["disagreements"]) == 5
    kinds = {kind for miss in rep["disagreements"] for kind in miss["kinds"]}
    assert kinds == {
        "wrong_label",
        "extra_card_items",
        "missing_card_items",
        "wrong_quoted_message",
    }
    assert (
        rep["extra_card_items"]["count"] == 2
        and rep["extra_card_items"]["rate"] == 0.333333
    )
    assert "Exact verdicts: 1 of 6" in render(rep, items)


def test_s1_reported_revision_is_recorded_and_changes_are_visible(cfg, monkeypatch):
    client = Client(cfg, KEY)
    docs = iter(
        [
            {
                "model": "test/model-1",
                "provider": "TestProvider",
                "revision": revision,
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"choice":"A"}'}}
                ],
            }
            for revision in ["r1", "r2"]
        ]
    )
    monkeypatch.setattr(client, "_post", lambda *a: json.dumps(next(docs)).encode())
    first = client.choose([], ["A"], client.clock() + 5)
    second = client.choose([], ["A"], client.clock() + 5)
    assert first["served"]["revision"] == "r1"
    assert second["served_change"]["before"]["revision"] == "r1"
    assert second["served_change"]["after"]["revision"] == "r2"


@pytest.mark.parametrize(
    "ruling,path,needed",
    [
        ("S2", "cohbench/user/README.md", "matrices and the labels of misses"),
        (
            "S2",
            "calibration/classifier/heldout-run-2026-11-23/README.md",
            "driver authorised running the second model",
        ),
        (
            "S3",
            "calibration/classifier/dev/hosted-run-2026-11-23/README.md",
            "call 1 was an exploratory probe",
        ),
        (
            "V2",
            "cohbench/user/README.md",
            "No structured field names the harness or the model",
        ),
        (
            "V2",
            "calibration/classifier/heldout-run-2026-11-23/README.md",
            "may name either",
        ),
        (
            "S1",
            "cohbench/user/README.md",
            "A release needs an endpoint that serves an identified weight revision",
        ),
        ("V3", "cohbench/user/README.md", "paths from inside the agent's container"),
    ],
)
def test_documentation_rulings(ruling, path, needed):
    assert needed in " ".join((ROOT / path).read_text().split())
