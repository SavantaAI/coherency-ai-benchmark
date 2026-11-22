"""Regressions for the second review of the classifier unit."""

from __future__ import annotations

import io
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cohbench.runner.policy import Entry, compose
from cohbench.runner.task import Task
from cohbench.user import prompts as P
from cohbench.user.__main__ import main
from cohbench.user.api import WITHHELD, Client, EndpointError
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from cohbench.user.items import ITEM_FORMAT, items_digest, load_items, read_answers
from cohbench.user.label import Term
from cohbench.user.revision import code_revision

from .conftest import KEY, config_doc
from .fake_server import Fake
from .test_classifier import CARD, OPEN, judge, last_question, sent_message, table, types


# ----- 1. a reply text two card items share -------------------------------------------------------

RAW = {"facts": [], "decisions": [
    {"id": "wipe", "question": "Whether the staging table may be wiped", "answer": "Yes."},
    {"id": "notify", "question": "Whether the support team should be told about the load", "answer": "Yes.", "available_after": "loaded"}]}




# ----- 2. only a complete 200 response is a completion ----------------------------------------------

@pytest.mark.parametrize("mode,match", [("dup_provider", "repeats a field"), ("short_body", "declared length"),
                                        ("partial", "HTTP 206")])
def test_2_invalid_http_completions_raise(fake, mode, match):
    cfg = parse(config_doc(fake.base_url, limits={"backoff_s": 0.01}))
    fake.mode = mode
    fake.decide = table({1: "B"})
    with pytest.raises(EndpointError, match=match):
        judge(fake, cfg)


# ----- 3. an absolute cutoff per attempt, headers and body ------------------------------------------

@pytest.mark.parametrize("mode", ["drip", "drip_headers"])
def test_3_slow_response_is_cut_off_at_the_request_timeout(fake, mode):
    cfg = parse(config_doc(fake.base_url, limits={"request_timeout_s": 0.27, "verdict_deadline_s": 2.0, "retries": 0}))
    fake.mode = mode
    fake.slow_s = 1.5
    fake.decide = table({1: "B"})
    t0 = time.monotonic()
    with pytest.raises(EndpointError, match="timeout"):
        judge(fake, cfg)
    assert time.monotonic() - t0 < 0.6


def test_3_slow_response_is_cut_off_at_the_verdict_deadline(fake):
    cfg = parse(config_doc(fake.base_url, limits={"request_timeout_s": 5.0, "verdict_deadline_s": 0.35, "retries": 1}))
    fake.mode = "drip"
    fake.slow_s = 1.5
    t0 = time.monotonic()
    with pytest.raises(EndpointError):
        judge(fake, cfg)
    assert time.monotonic() - t0 < 0.7


def test_3_retry_gets_its_own_cutoff(fake):
    cfg = parse(config_doc(fake.base_url, limits={"request_timeout_s": 0.27, "verdict_deadline_s": 2.0, "retries": 1, "backoff_s": 0.05}))
    fake.mode = "drip"
    fake.slow_s = 1.5
    t0 = time.monotonic()
    with pytest.raises(EndpointError, match="after 2 attempts"):
        judge(fake, cfg)
    assert time.monotonic() - t0 < 1.1 and len(fake.requests) == 2


# ----- 4. every exception from the HTTP stack becomes a fixed category --------------------------------

def test_4_malformed_status_line_with_the_key(fake):
    cfg = parse(config_doc(fake.base_url, limits={"backoff_s": 0.01}))
    fake.mode = "bad_status"
    with pytest.raises(EndpointError) as e:
        judge(fake, cfg)
    assert KEY not in str(e.value) and "transport error" in str(e.value)
    assert e.value.__cause__ is None and (e.value.__context__ is None or e.value.__suppress_context__)


# ----- minors ---------------------------------------------------------------------------------------

def test_metadata_uses_fullmatch(cfg):
    c = Client(cfg, KEY)
    assert c.meta("fp-1\n") == WITHHELD and c.meta("fp-1") == "fp-1"


def test_see_other_redirect_is_not_followed_by_any_method(fake, cfg):
    with Fake() as other:
        fake.mode = "see_other"
        fake.redirect_to = other.base_url + "/chat/completions"
        with pytest.raises(EndpointError, match="redirect refused"):
            judge(fake, cfg)
        assert fake.methods == ["POST"]
        assert other.methods == [] and other.auth == []


def test_raw_terminal_end_of_input():
    t = Term(io.StringIO(), io.StringIO())
    for ch in ("", "\x04"):
        with pytest.raises(EOFError):
            t._raw_key(ch)
    with pytest.raises(KeyboardInterrupt):
        t._raw_key("\x03")
    assert t._raw_key("\r") == "\n" and t._raw_key("a") == "a"


def test_record_carries_the_code_revision(fake, cfg):
    fake.decide = table({1: "B"})
    j = judge(fake, cfg)
    assert j.record["code"] == code_revision() and len(code_revision()) == 12
    a = Classifier(cfg, Client(cfg, KEY)).classify_awareness("q", "r")
    assert a.record["code"] == code_revision()


def test_prompt_digest_covers_the_whole_prompts_file(tmp_path, monkeypatch):
    before = P.digest()
    src = Path(P.__file__).read_text()
    for phrase in ("decision about", "Facts the user knows:", "Notes (things the user knows):", '"additionalProperties": False'):
        copy = tmp_path / "prompts.py"
        copy.write_text(src.replace(phrase, phrase.upper(), 1) if phrase in src else src + "#")
        monkeypatch.setattr(P, "__file__", str(copy))
        assert P.digest() != before, phrase
        monkeypatch.undo()
    assert P.digest() == before


def _items(tmp_path: Path, n: int = 8) -> Path:
    d = tmp_path / "items"
    d.mkdir()
    for i in range(n):
        doc = {"format": ITEM_FORMAT, "version": 1, "id": f"t-{i:02d}", "card": CARD,
               "transcript": [{"role": "user", "text": "Run the batches."}], "output": f"message {i}"}
        (d / f"t-{i:02d}.json").write_text(json.dumps(doc))
    return d


def test_resumed_run_keeps_its_order(tmp_path, key_file, capsys):
    d = _items(tmp_path)
    out = tmp_path / "v.json"
    seen: list[str] = []
    with Fake() as f:
        cfgp = tmp_path / "cfg.json"
        cfgp.write_text(json.dumps(config_doc(f.base_url, limits={"retries": 0})))

        def decide(messages, keys):
            seen.append(sent_message(messages[1]["content"])["text"])
            if len(seen) == 4:
                f.mode = "http400"  # this answer is served; the next request is refused and the run stops
            return "B"

        f.decide = decide
        args = ["run", str(d), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file), "--order", "5"]
        assert main(args) == 1
        f.mode = None
        assert main(args) == 0
    want = sorted(f"t-{i:02d}" for i in range(8))
    random.Random(5).shuffle(want)
    done = [f"t-{int(x.split()[1]):02d}" for x in seen]
    assert done == want  # four before the stop, the other four after the resume, in one order


def test_resume_refuses_another_code_revision(tmp_path, key_file, capsys):
    d = _items(tmp_path, 2)
    out = tmp_path / "v.json"
    with Fake() as f:
        cfgp = tmp_path / "cfg.json"
        cfgp.write_text(json.dumps(config_doc(f.base_url)))
        f.decide = lambda messages, keys: "B"
        args = ["run", str(d), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file)]
        assert main(args) == 0
        doc = json.loads(out.read_text())
        assert doc["code"] == code_revision()
        doc["code"] = "000000000000"
        out.write_text(json.dumps(doc))
        assert main(args) == 2
    assert "code revision" in capsys.readouterr().err


def test_agree_validates_entries_and_equates_whole_message_quotes(tmp_path, capsys):
    d = _items(tmp_path, 2)
    digest = items_digest(load_items([d]))

    def write(name, entries):
        p = tmp_path / name
        p.write_text(json.dumps({"format": "cohbench-classifier-verdicts", "version": 1, "items": digest, "config": "c",
                                 "order": None, "code": "x", "verdicts": entries}))
        return p

    whole = {"t-00": {"label": "already_given", "answer_ids": [], "quote_of": 0, "quote": "Run the batches."},
             "t-01": {"label": "no_request", "answer_ids": [], "quote_of": None, "quote": None}}
    implicit = json.loads(json.dumps(whole))
    implicit["t-00"]["quote"] = None
    a, b = write("a.json", whole), write("b.json", implicit)
    assert main(["agree", str(a), str(b), "--items", str(d), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["whole_verdicts"]["identical"] == 2
    for bad in ({"label": "already_given", "answer_ids": [], "quote_of": 0, "quote": "not in the message"},
                {"label": "already_given", "answer_ids": [], "quote_of": True, "quote": None},
                {"label": "answerable", "answer_ids": ["slot", "slot"], "quote_of": None, "quote": None},
                {"label": "answerable", "answer_ids": "slot", "quote_of": None, "quote": None},
                {"label": "no_request", "answer_ids": [], "quote_of": None, "quote": "x"}):
        c = write("c.json", {**whole, "t-00": bad})
        assert main(["agree", str(a), str(c), "--items", str(d)]) == 2, bad
        capsys.readouterr()


def test_cut_retained_characters_counted_from_the_sent_body(fake):
    from .test_classifier import sized

    tr = (OPEN, Entry("agent", "x" * 500))
    out = "START " + "filler " * 3000 + " END"
    fake.decide = table({1: "B"})
    full = judge(fake, sized(fake, 10**7), transcript=tr, output=out).record["context"]["input_tokens_est_max"]
    for shrink in (8000, 12000):
        j = judge(fake, sized(fake, full - shrink), transcript=tr, output=out)
        m = sent_message(fake.requests[-1]["messages"][1]["content"])
        retained = len(m["start"]) + len(m["end"])
        assert out.startswith(m["start"]) and out.endswith(m["end"])
        assert len(out) - retained == j.record["context"]["message_cut_chars"]
