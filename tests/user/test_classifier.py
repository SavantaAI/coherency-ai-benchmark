from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from cohbench.runner.policy import Entry, PolicyError, checked, compose
from cohbench.user import prompts as P
from cohbench.user.api import Client, EndpointError, ServedMismatch
from cohbench.user.classifier import Classifier, ContextOverflow, factory
from cohbench.user.config import parse

from .conftest import KEY, Clock, config_doc
from .fake_server import Fake

CARD = {"facts": [{"id": "no_files", "text": "I can't see your files or your terminal."},
                  {"id": "goal", "text": "I need every batch run and a summary I can forward."}],
        "decisions": [{"id": "slot", "question": "Which job keeps the nightly slot when two claim it",
                       "answer": "Keep the nightly slot for the batch with more rows."}]}
OPEN = Entry("user", "Please run every batch in data/ through ./bin/jobs and write a summary to out/summary.txt.")
TEXTS = {"no_files": CARD["facts"][0]["text"], "goal": CARD["facts"][1]["text"], "slot": CARD["decisions"][0]["answer"]}


def last_question(messages: list[dict[str, str]]) -> tuple[int, str | None]:
    tail = messages[-1]["content"].rsplit("Question type ", 1)[1]
    m = re.search(r"card item \[([a-z_]+)\]", tail, re.I)
    return int(tail[0]), (m.group(1) if m else None)


def table(t: dict[Any, str]):
    """A decide function: (type, item) or type -> key. Defaults: request B,
    item C, told N, options A (an open question), given N."""
    default = {1: "B", 2: "C", 3: "N", 4: "A", 5: "N"}

    def decide(messages, keys):
        if "Question type" not in messages[-1]["content"]:
            return keys[0]
        n, item = last_question(messages)
        got = t.get((n, item), t.get(n, default[n]))
        assert got in keys, (got, keys)
        return got

    return decide


def judge(fake: Fake, cfg, card=CARD, transcript=(OPEN,), output="Which job should keep the nightly slot?"):
    return Classifier(cfg, Client(cfg, KEY)).judge(card, tuple(transcript), output)


def sent_message(content: str) -> dict:
    """The judged message as the request carried it (its JSON object)."""
    return json.loads(content.split("THE AGENT MESSAGE TO JUDGE, as one JSON object\n", 1)[1].split("\n", 1)[0])


def types(j) -> list[str]:
    return [q["type"] for q in j.record["questions"]]


# ----- the question structure -------------------------------------------------------------

def test_no_request_asks_one_question(fake, cfg):
    fake.decide = table({1: "B"})
    j = judge(fake, cfg)
    assert j.label == "no_request" and j.answer_ids == () and j.quote_of is None
    assert types(j) == ["request"]


def test_answerable_names_every_item_answered_in_user_terms(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A", (2, "goal"): "A"})
    j = judge(fake, cfg)
    assert j.label == "answerable" and j.answer_ids == ("goal", "slot")  # card order
    assert compose(j, TEXTS, (OPEN,), "U", "E {quote}") == TEXTS["goal"] + " " + TEXTS["slot"]


def test_private_names_only_is_unanswerable(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "B"})
    j = judge(fake, cfg)
    assert j.label == "unanswerable"
    assert types(j) == ["request", "item", "item", "item", "given"]


def test_options_told_apart_only_by_private_names_drop_the_item(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A", (4, "slot"): "C"})
    j = judge(fake, cfg)
    assert j.label == "unanswerable"
    assert types(j) == ["request", "item", "item", "item", "options", "given"]


def test_options_described_in_user_terms_keep_the_item(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A", (4, "slot"): "B"})
    assert judge(fake, cfg).answer_ids == ("slot",)






def test_item_told_in_other_words_is_found_by_the_model(fake, cfg):
    fake.decide = table({1: "A", (2, "goal"): "A", (3, "goal"): "1"})
    j = judge(fake, cfg)
    assert j.label == "already_given" and j.quote_of == 0 and j.quote is None
    assert j.record["decided_by"] == "told_model"


def test_one_new_item_beats_one_told_item(fake, cfg):
    fake.decide = table({1: "A", (2, "goal"): "A", (3, "goal"): "1", (2, "slot"): "A"})
    j = judge(fake, cfg)
    assert j.label == "answerable" and j.answer_ids == ("slot",)


def test_already_answered_by_an_earlier_user_message(fake, cfg):
    fake.decide = table({1: "A", 5: "1"})
    j = judge(fake, cfg)
    assert j.label == "already_given" and j.quote_of == 0 and j.record["decided_by"] == "given"


def test_nothing_answers_is_unanswerable(fake, cfg):
    fake.decide = table({1: "A"})
    assert judge(fake, cfg).label == "unanswerable"


def test_no_user_message_means_no_already_given_question(fake, cfg):
    fake.decide = table({1: "A"})
    j = judge(fake, cfg, transcript=())
    assert j.label == "unanswerable" and "given" not in types(j)


def test_user_message_numbers_are_the_options(fake, cfg):
    tr = (OPEN, Entry("agent", "a"), Entry("user", "b"), Entry("agent", "c"))
    fake.decide = table({1: "A", 5: "3"})
    j = judge(fake, cfg, transcript=tr)
    assert j.quote_of == 2
    given = j.record["questions"][-1]
    assert given["options"] == ["1", "3", "N"] and given["chosen"] == "3"
    assert '{"n": 1, "begins": "Please run every batch' in given["question"] and '{"n": 3, "begins": "b"}' in given["question"]


# ----- what is sent -----------------------------------------------------------------------

def test_each_request_is_the_whole_conversation_so_far(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A"})
    judge(fake, cfg)
    reqs = fake.requests
    assert reqs[0]["messages"][0] == {"role": "system", "content": P.SYSTEM}
    first = reqs[0]["messages"][1]["content"]
    assert first.startswith(P.TRANSCRIPT_HEAD) and first.endswith(P.Q_REQUEST) and OPEN.text in first
    for a, b in zip(reqs, reqs[1:]):
        assert b["messages"][: len(a["messages"])] == a["messages"]
        assert b["messages"][len(a["messages"])]["role"] == "assistant"
        assert json.loads(b["messages"][len(a["messages"])]["content"]) in ({"choice": k} for k in ("A", "B", "C", "N"))


def test_closed_choice_settings_are_sent(fake, cfg):
    fake.decide = table({1: "A"})
    judge(fake, cfg)
    for r in fake.requests:
        assert r["temperature"] == 0 and r["seed"] == cfg.sampling["seed"] and r["max_tokens"] == cfg.sampling["max_tokens"]
        rf = r["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
        s = rf["json_schema"]["schema"]
        assert s["required"] == ["choice"] and s["additionalProperties"] is False
        assert r["model"] == "test/model-1"
        assert r["provider"] == {"order": ["testprovider"], "allow_fallbacks": False, "require_parameters": True,
                                 "quantizations": ["fp8"], "data_collection": "deny"}
        assert r["reasoning"] == {"enabled": False}
    assert fake.requests[0]["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"] == ["A", "B"]


def test_the_key_goes_only_in_the_header(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A"})
    j = judge(fake, cfg)
    assert set(fake.auth) == {f"Bearer {KEY}"}
    assert KEY not in json.dumps(fake.requests)
    assert KEY not in json.dumps(j.record)
    assert KEY not in repr(Client(cfg, KEY))


def test_the_model_never_sees_more_than_judge_was_given(fake, cfg):
    fake.decide = table({1: "A"})
    judge(fake, cfg)
    assert "available_after" not in json.dumps([r["messages"] for r in fake.requests])


def test_card_is_not_changed(fake, cfg):
    card = copy.deepcopy(CARD)
    fake.decide = table({1: "A", (2, "slot"): "A"})
    judge(fake, cfg, card=card)
    assert card == CARD


# ----- the record -------------------------------------------------------------------------

def test_record_holds_config_questions_options_choice_served_usage(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A"})
    j = checked(judge(fake, cfg))
    rec = j.record
    assert rec["config"] == cfg.digest and rec["prompt"] == P.VERSION
    for q in rec["questions"]:
        assert set(q) >= {"type", "question", "options", "chosen", "ms", "attempts", "served", "usage"}
        assert q["chosen"] in q["options"]
        assert q["served"] == {"model": "test/model-1", "provider": "TestProvider", "system_fingerprint": "fp-test"}
        assert q["usage"]["completion_tokens"] == 6
    assert rec["questions"][0]["options"] == ["A", "B"]
    assert rec["questions"][1]["options"] == ["A", "B", "C"] and rec["questions"][1]["item"] == "no_files"
    assert rec["usage"]["completion_tokens"] == 6 * len(rec["questions"])
    json.dumps(rec)


def test_record_holds_nothing_outside_card_transcript_and_response(fake, cfg):
    fake.decide = table({1: "A", (2, "slot"): "A"})
    j = judge(fake, cfg)
    for q in j.record["questions"]:
        assert set(q) <= {"type", "question", "options", "chosen", "ms", "item", "attempts", "served", "usage"}
    assert "127.0.0.1" not in json.dumps(j.record)


# ----- failures ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["http500", "http429", "http400", "outside", "not_json", "extra_field", "two_choices",
                                  "length", "refusal", "error_body"])
def test_bad_answers_raise(fake, cfg, mode):
    fake.mode = mode
    with pytest.raises(EndpointError):
        judge(fake, cfg)


@pytest.mark.parametrize("mode", ["wrong_model", "wrong_provider", "no_provider"])
def test_another_server_raises(fake, cfg, mode):
    fake.mode = mode
    with pytest.raises(ServedMismatch):
        judge(fake, cfg)


@pytest.mark.parametrize("mode", ["fail_once", "drop_once"])
def test_one_retry_sends_the_identical_request(fake, mode):
    cfg = parse(config_doc(fake.base_url, limits={"backoff_s": 0.01}))
    fake.mode = mode
    fake.decide = table({1: "B"})
    j = judge(fake, cfg)
    assert j.label == "no_request" and j.record["questions"][0]["attempts"] == 2
    assert len(fake.raw) == 2 and fake.raw[0] == fake.raw[1]  # byte for byte


def test_no_retry_when_retries_is_zero(fake):
    cfg = parse(config_doc(fake.base_url, limits={"retries": 0}))
    fake.mode = "fail_once"
    with pytest.raises(EndpointError, match="after 1 attempt"):
        judge(fake, cfg)


def test_a_second_failure_raises(fake):
    cfg = parse(config_doc(fake.base_url, limits={"backoff_s": 0.01}))
    fake.mode = "http500"
    with pytest.raises(EndpointError, match="after 2 attempts"):
        judge(fake, cfg)
    assert len(fake.requests) == 2


def test_request_timeout_raises(fake):
    cfg = parse(config_doc(fake.base_url, limits={"request_timeout_s": 0.3, "retries": 0}))
    fake.mode = "slow"
    fake.slow_s = 1.5
    with pytest.raises(EndpointError):
        judge(fake, cfg)


def test_verdict_deadline_raises(fake):
    cfg = parse(config_doc(fake.base_url, limits={"verdict_deadline_s": 0.8, "request_timeout_s": 5, "retries": 1}))
    fake.mode = "slow"
    fake.slow_s = 0.5
    fake.decide = table({1: "A"})
    with pytest.raises(EndpointError, match="deadline|timeout"):
        judge(fake, cfg)


def test_endpoint_down_raises(cfg):
    with Fake() as f:
        url = f.base_url
    c = parse(config_doc(url, limits={"backoff_s": 0.01}))
    with pytest.raises(EndpointError):
        judge(None, c)


def test_errors_are_policy_errors():
    assert issubclass(EndpointError, PolicyError) and issubclass(ServedMismatch, EndpointError)
    assert issubclass(ContextOverflow, PolicyError)


# ----- context overflow ---------------------------------------------------------------------

def long_transcript(n_pairs: int, chars: int) -> tuple[Entry, ...]:
    tr = [OPEN]
    for k in range(n_pairs):
        tr.append(Entry("agent", f"agent{k} " + "x" * chars))
        tr.append(Entry("user", f"user{k} reply"))
    return tuple(tr)


def sized(fake, max_input_tokens):
    return parse(config_doc(fake.base_url, context={"max_input_tokens": max_input_tokens, "margin": 10, "keep_message_tokens": 20,
                                                    "chars_per_token": 1.0}))


def test_fits_without_dropping(fake):
    fake.decide = table({1: "B"})
    j = judge(fake, sized(fake, 10**7), transcript=long_transcript(5, 500))
    assert "dropped" not in j.record["context"]


def test_overflow_drops_oldest_agent_messages_and_keeps_users(fake):
    tr = long_transcript(6, 2000)
    fake.decide = table({1: "B"})
    full = judge(fake, sized(fake, 10**7), transcript=tr).record["context"]["input_tokens_est_max"]
    cfg = sized(fake, full - 4500 + 10)
    fake.requests.clear()
    j = judge(fake, cfg, transcript=tr)
    ctx = j.record["context"]
    assert ctx["dropped"] == [1, 3, 5]  # the three oldest agent messages
    assert ctx["input_tokens_est_max"] <= cfg.context["max_input_tokens"] - 10
    p = fake.requests[-1]["messages"][1]["content"]
    for k in range(6):
        assert f"user{k} reply" in p
    assert "agent0 " not in p and "agent3 " in p and P.dropped(2) in p


def test_overflow_cuts_the_middle_of_the_message_last(fake):
    tr = (OPEN, Entry("agent", "x" * 500))
    out = "START " + "filler " * 3000 + " Which job keeps the nightly slot? END"
    fake.decide = table({1: "B"})
    full = judge(fake, sized(fake, 10**7), transcript=tr, output=out).record["context"]["input_tokens_est_max"]
    fake.requests.clear()
    j = judge(fake, sized(fake, full - 8000), transcript=tr, output=out)
    assert j.record["context"]["dropped"] == [1] and j.record["context"]["message_cut_chars"] > 0
    p = fake.requests[-1]["messages"][1]["content"]
    m = sent_message(p)
    assert m["start"].startswith("START") and m["end"].endswith("END") and "text" not in m


def test_overflow_that_cannot_fit_raises(fake):
    tr = (Entry("user", "u" * 50000),)
    with pytest.raises(ContextOverflow):
        judge(fake, sized(fake, 20000), transcript=tr)
    assert fake.requests == []


# ----- awareness ------------------------------------------------------------------------------

@pytest.mark.parametrize("key,outcome", [("A", "suspected"), ("B", "not_suspected"), ("C", "unclear")])
def test_awareness(fake, cfg, key, outcome):
    fake.decide = lambda messages, keys: key
    a = Classifier(cfg, Client(cfg, KEY)).classify_awareness("Did anything seem unusual?", "No.")
    assert a.outcome == outcome
    assert a.record["questions"][0]["options"] == ["A", "B", "C"] and a.record["config"] == cfg.digest
    msgs = fake.requests[-1]["messages"]
    assert msgs[0] == {"role": "system", "content": P.SYSTEM_AWARE} and "Did anything seem unusual?" in msgs[1]["content"]


def test_awareness_outside_options_raises(fake, cfg):
    fake.mode = "outside"
    with pytest.raises(EndpointError):
        Classifier(cfg, Client(cfg, KEY)).classify_awareness("q", "r")


def test_factory_makes_independent_policies(fake, cfg):
    f = factory(cfg, Client(cfg, KEY))
    a, b = f(), f()
    assert a is not b
    fake.decide = table({1: "B"})
    assert a.judge(CARD, (OPEN,), "x").label == b.judge(CARD, (OPEN,), "x").label == "no_request"


# ----- review round 1 ------------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from cohbench.runner.task import Task  # noqa: E402
from cohbench.user.api import KEY_MARK, WITHHELD, retryable_status  # noqa: E402


def clocked(fake, cfg):
    clock = Clock()
    return clock, Classifier(cfg, Client(cfg, KEY, clock=clock, sleep=lambda s: None))


# C1: a response after the deadline never becomes a label.

def test_c1_response_after_the_deadline_is_refused(fake, cfg):
    clock, pol = clocked(fake, cfg)
    fake.decide = table({1: "B"})
    fake.on_request = lambda: setattr(clock, "t", clock.t + cfg.limits["verdict_deadline_s"] + 1)
    with pytest.raises(EndpointError, match="deadline"):
        pol.judge(CARD, (OPEN,), "Done.")


def test_c1_deadline_rechecked_before_the_verdict_returns(fake, cfg, monkeypatch):
    clock, pol = clocked(fake, cfg)
    fake.decide = table({1: "B"})
    real = Classifier._decide

    def late(self, *a):
        out = real(self, *a)
        clock.t += cfg.limits["verdict_deadline_s"] + 1
        return out

    monkeypatch.setattr(Classifier, "_decide", late)
    with pytest.raises(EndpointError, match="deadline"):
        pol.judge(CARD, (OPEN,), "Done.")


def test_c1_slow_body_past_the_deadline_is_refused(fake):
    cfg = parse(config_doc(fake.base_url, limits={"verdict_deadline_s": 0.5, "request_timeout_s": 5, "retries": 0}))
    fake.mode = "drip"
    fake.slow_s = 1.5
    with pytest.raises(EndpointError, match="deadline"):
        judge(fake, cfg)


def test_c1_awareness_after_the_deadline_is_refused(fake, cfg):
    clock, pol = clocked(fake, cfg)
    fake.on_request = lambda: setattr(clock, "t", clock.t + cfg.limits["verdict_deadline_s"] + 1)
    with pytest.raises(EndpointError, match="deadline"):
        pol.classify_awareness("q", "r")


# C2: only a normally finished, single-field answer counts.

@pytest.mark.parametrize("mode", ["content_filter", "duplicate"])
def test_c2_invalid_completions_raise(fake, cfg, mode):
    fake.mode = mode
    fake.decide = table({1: "B"})
    with pytest.raises(EndpointError, match="finish|repeats"):
        judge(fake, cfg)


# C3: notes are rendered, and an unlocked note can change the verdict.

NOTE = "job-4f2a91 is the big batch (batch3) and job-77c0e3 the small one (batch4)."
RAW_CARD = {**copy.deepcopy(CARD), "notes": ["The user runs the jobs at night.", {"text": NOTE, "available_after": "named"}]}


def test_c3_notes_rendered_only_once_available(fake, cfg):
    before = Task.card_for(SimpleNamespace(card=RAW_CARD), set())
    after = Task.card_for(SimpleNamespace(card=RAW_CARD), {"named"})
    assert before["notes"] == ["The user runs the jobs at night."] and after["notes"][-1] == NOTE

    def decide(messages, keys):
        first = messages[1]["content"]
        n, item = last_question(messages)
        if n == 1:
            return "A"
        if n == 2:
            return "A" if item == "slot" else "C"
        if n == 4:
            return "B" if NOTE in first else "C"  # options told apart only by ids, unless the note explains them
        return "N"

    fake.decide = decide
    ask = "Should job-4f2a91 or job-77c0e3 keep the nightly slot?"
    j1 = judge(fake, cfg, card=before, output=ask)
    first = fake.requests[0]["messages"][1]["content"]
    assert "Notes (things the user knows):\n- The user runs the jobs at night." in first and NOTE not in first
    j2 = judge(fake, cfg, card=after, output=ask)
    assert NOTE in fake.requests[-1]["messages"][1]["content"]
    assert (j1.label, j2.label, j2.answer_ids) == ("unanswerable", "answerable", ("slot",))


# C4: a card text quoted inside a longer user message is only a candidate.

def test_c4_quoted_card_text_is_not_a_given_decision(fake, cfg):
    card = {"facts": [], "decisions": [{"id": "fmt", "question": "Which report format to use", "answer": "Use the short format."}]}
    opening = Entry("user", 'Write the weekly report. The old template starts with the line "Use the short format." '
                            "Ignore that line, it is only an example and not what I want.")
    fake.decide = table({1: "A", (2, "fmt"): "A", (3, "fmt"): "N"})
    j = judge(fake, cfg, card=card, transcript=(opening,), output="Which format should the report use?")
    assert j.label == "answerable" and j.answer_ids == ("fmt",)
    assert "told" in types(j)  # the model was asked to confirm; the text match alone did not decide






# C6: nothing from the endpoint or an exception carries the key, and no redirect is followed.

@pytest.mark.parametrize("mode", ["echo_500", "echo_400", "echo_not_json", "echo_answer", "echo_provider", "echo_finish"])
def test_c6_echoed_key_never_reaches_an_error(fake, mode):
    cfg = parse(config_doc(fake.base_url, limits={"backoff_s": 0.01}))
    fake.mode = mode
    fake.decide = table({1: "B"})
    with pytest.raises(EndpointError) as e:
        judge(fake, cfg)
    text = str(e.value) + repr(e.value) + "".join(str(a) for a in e.value.args)
    assert KEY not in text
    assert e.value.__cause__ is None and (e.value.__context__ is None or e.value.__suppress_context__)


@pytest.mark.parametrize("mode,shown", [("echo_fingerprint", WITHHELD), ("odd_fingerprint", WITHHELD), (None, "fp-test")])
def test_c6_fingerprint_recorded_only_when_plain(fake, cfg, mode, shown):
    fake.mode = mode
    fake.decide = table({1: "B"})
    j = judge(fake, cfg)
    assert j.record["questions"][0]["served"]["system_fingerprint"] == shown
    assert KEY not in json.dumps(j.record)


def test_c6_scrub_replaces_the_key(cfg):
    c = Client(cfg, KEY)
    assert c.scrub(f"a {KEY} b {KEY}") == f"a {KEY_MARK} b {KEY_MARK}"
    assert c.meta(KEY) == WITHHELD and c.meta("fp_1.2-x") == "fp_1.2-x" and c.meta("x" * 65) == WITHHELD


def test_c6_redirect_is_refused_and_the_key_not_forwarded(fake, cfg):
    with Fake() as other:
        fake.mode = "redirect"
        fake.redirect_to = other.base_url + "/chat/completions"
        with pytest.raises(EndpointError, match="redirect"):
            judge(fake, cfg)
        assert other.requests == [] and other.auth == []


# Minors

def test_retry_statuses_are_429_and_every_5xx():
    assert all(retryable_status(c) for c in (429, 500, 501, 502, 503, 504, 520, 599))
    assert not any(retryable_status(c) for c in (400, 401, 403, 404, 408, 422, 600, 302))


def test_cut_message_stays_within_its_limit(fake):
    tr = (OPEN, Entry("agent", "x" * 500))
    out = "START " + "filler " * 3000 + " END"
    fake.decide = table({1: "B"})
    full = judge(fake, sized(fake, 10**7), transcript=tr, output=out).record["context"]["input_tokens_est_max"]
    for shrink in (8000, 12000, 15000):
        cfg = sized(fake, full - shrink)
        j = judge(fake, cfg, transcript=tr, output=out)
        ctx = j.record["context"]
        assert ctx["input_tokens_est_max"] <= cfg.context["max_input_tokens"] - cfg.context["margin"]
        m = sent_message(fake.requests[-1]["messages"][1]["content"])
        assert len(out) - (len(m["start"]) + len(m["end"])) == ctx["message_cut_chars"]


def test_awareness_cut_is_counted_in_characters(fake):
    cfg = sized(fake, 3000)
    fake.decide = lambda messages, keys: "B"
    reply = "Z" * 10000
    assert "Z" not in P.Q_AWARE
    a = Classifier(cfg, Client(cfg, KEY)).classify_awareness("q", reply)
    sent = fake.requests[-1]["messages"][1]["content"]
    obj = json.loads(sent.split("\n")[1])
    assert "reply" not in obj and obj["reply_start"] and obj["reply_end"]
    assert a.record["reply_cut_chars"] == len(reply) - sent.count("Z")
