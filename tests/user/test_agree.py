"""Cohen's kappa against hand-worked cases."""

from __future__ import annotations

import pytest

from cohbench.runner.policy import Entry
from cohbench.user.agree import compare, confusion, kappa, render
from cohbench.user.items import Item

A, G, U, N = "answerable", "already_given", "unanswerable", "no_request"


def test_perfect_agreement_with_several_labels():
    k = kappa([(A, A), (G, G), (U, U), (N, N), (A, A)])
    assert k.observed == 1 and k.kappa == 1


def test_one_label_only_is_undefined():
    # Both always say no_request: observed 1, chance 1, kappa 0/0.
    k = kappa([(N, N)] * 6)
    assert k.observed == 1 and k.chance == 1 and k.kappa is None


def test_one_side_constant_other_varies():
    # First: N N N N. Second: N N A A. po = 2/4; pe = (4*2)/16 = 0.5; kappa = 0.
    k = kappa([(N, N), (N, N), (N, A), (N, A)])
    assert k.observed == 0.5 and k.chance == 0.5 and k.kappa == 0


def test_chance_agreement_is_zero():
    # Each side half A, half U, agreeing on exactly half: po 0.5, pe 0.5.
    k = kappa([(A, A), (A, U), (U, A), (U, U)])
    assert k.kappa == 0


def test_systematic_disagreement_is_negative():
    # po 0, pe 0.5: kappa -1.
    k = kappa([(A, U), (U, A), (A, U), (U, A)])
    assert k.kappa == -1


def test_worked_example():
    # 10 items. Agreements: A 3, U 2, N 2 -> po 0.7.
    # First counts: A 4, U 3, N 3. Second counts: A 3, U 3, N 3, G 1.
    pairs = [(A, A)] * 3 + [(A, G)] + [(U, U)] * 2 + [(U, N)] + [(N, N)] * 2 + [(N, U)]
    k = kappa(pairs)
    assert k.observed == pytest.approx(0.7)
    assert k.chance == pytest.approx((4 * 3 + 3 * 3 + 3 * 3) / 100)  # 0.30
    assert k.kappa == pytest.approx((0.7 - 0.3) / 0.7)


def test_bad_input():
    with pytest.raises(ValueError):
        kappa([])
    with pytest.raises(ValueError):
        kappa([(A, "maybe")])


def test_confusion_rows_are_first():
    m = confusion([(A, U), (A, U), (N, N)])
    assert m[A][U] == 2 and m[U][A] == 0 and m[N][N] == 1


def _item(i: str) -> Item:
    tr = (Entry("user", "do it"), Entry("agent", "ok"), Entry("user", "fine"))
    return Item(i, {"facts": [{"id": "f", "text": "t"}], "decisions": [{"id": "d", "question": "q", "answer": "a"}]}, tr, "msg " + i, {"id": i})


def test_compare_reports_items_and_quotes():
    items = [_item(x) for x in "abcde"]
    first = {"a": {"label": A, "answer_ids": ["d"]}, "b": {"label": A, "answer_ids": ["d", "f"]}, "c": {"label": G, "quote_of": 0},
             "d": {"label": U}, "e": {"label": N}}
    second = {"a": {"label": A, "answer_ids": ["d"]}, "b": {"label": A, "answer_ids": ["d"]}, "c": {"label": G, "quote_of": 2},
              "d": {"label": N, "record": {"questions": [{"type": "request", "options": ["A", "B"], "chosen": "B"}]}}, "e": {"label": N}}
    rep = compare(items, first, second)
    assert rep["n"] == 5
    assert rep["answer_items"]["both_answerable"] == 2 and rep["answer_items"]["exact"] == 1
    assert rep["answer_items"]["mean_jaccard"] == pytest.approx((1 + 0.5) / 2)
    assert rep["quotes"]["differences"] == [{"item": "c", "first": 0, "second": 2}]
    assert [d["item"] for d in rep["disagreements"]] == ["d"]
    assert rep["per_label"][U] == {"first": 1, "second": 0, "agree": 0}
    text = render(rep, items, ("me", "model"))
    assert "Cohen's kappa" in text and "request: options A/B -> B" in text and "msg d" in text


def test_compare_only_common_items():
    items = [_item(x) for x in "ab"]
    rep = compare(items, {"a": {"label": N}, "b": {"label": N}}, {"a": {"label": N}})
    assert rep["n"] == 1 and rep["only_first"] == ["b"] and rep["kappa"] is None
    assert "undefined" in render(rep, items)
