"""The item format, the labelling tool and the command line."""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pytest

from cohbench.user.__main__ import main
from cohbench.user.items import ITEM_FORMAT, ItemError, check_label, items_digest, load_items, parse_item, read_answers
from cohbench.user.label import HELP, Session, Term

from .conftest import KEY, config_doc
from .fake_server import Fake

CARD = {"facts": [{"id": "no_files", "text": "I can't see your files."}],
        "decisions": [{"id": "slot", "question": "Which job keeps the slot", "answer": "The bigger batch."}]}


def item_doc(i: int, output: str = "Which job keeps the slot?", card=CARD) -> dict:
    return {"format": ITEM_FORMAT, "version": 1, "id": f"t-{i:02d}", "card": card,
            "transcript": [{"role": "user", "text": "Run the batches."}, {"role": "agent", "text": "Running."},
                           {"role": "user", "text": "Thanks."}], "output": output}


@pytest.fixture
def items_dir(tmp_path: Path) -> Path:
    d = tmp_path / "items"
    d.mkdir()
    for i in range(6):
        (d / f"t-{i:02d}.json").write_text(json.dumps(item_doc(i)))
    return d


# ----- items -------------------------------------------------------------------------------

def test_items_load_and_digest(items_dir):
    items = load_items([items_dir])
    assert [i.id for i in items] == [f"t-{i:02d}" for i in range(6)]
    assert items[0].user_indices() == [0, 2] and items[0].card_ids() == ["no_files", "slot"]
    assert items_digest(items) == items_digest(list(reversed(items)))


@pytest.mark.parametrize("change", [
    lambda d: d.update(label="answerable"),  # a label inside an item
    lambda d: d["card"]["decisions"][0].update(available_after="conflict"),
    lambda d: d["transcript"].append({"role": "tool", "text": "x"}),
    lambda d: d.update(output=""),
    lambda d: d["card"]["facts"].append({"id": "slot", "text": "dup"}),
])
def test_bad_items_are_refused(change):
    d = item_doc(0)
    d = json.loads(json.dumps(d))
    change(d)
    with pytest.raises(ItemError):
        parse_item(d)


def test_check_label(items_dir):
    it = load_items([items_dir])[0]
    assert check_label({"label": "answerable", "answer_ids": ["slot"]}, it) == []
    assert check_label({"label": "answerable", "answer_ids": []}, it)
    assert check_label({"label": "answerable", "answer_ids": ["nope"]}, it)
    assert check_label({"label": "already_given", "quote_of": 2}, it) == []
    assert check_label({"label": "already_given", "quote_of": 1}, it)  # an agent message
    assert check_label({"label": "unanswerable", "quote_of": 0}, it)
    assert check_label({"label": "maybe"}, it)


# ----- the labelling tool ---------------------------------------------------------------------

def session(items_dir: Path, out: Path, keys: list[str], seed: int | None = 3) -> tuple[Session, str]:
    stdout = io.StringIO()
    s = Session(load_items([items_dir]), out, seed, Term(io.StringIO("\n".join(keys) + "\n"), stdout), width=80)
    s.run()
    return s, stdout.getvalue()


def test_order_is_shuffled_by_the_seed(items_dir, tmp_path):
    s, _ = session(items_dir, tmp_path / "a.json", ["q"], seed=3)
    want = sorted(f"t-{i:02d}" for i in range(6))
    random.Random(3).shuffle(want)
    assert s.order == want
    s2, _ = session(items_dir, tmp_path / "b.json", ["q"], seed=4)
    assert s2.order != s.order


def test_label_every_kind_and_save(items_dir, tmp_path):
    out = tmp_path / "labels.json"
    # answerable: toggle item 2 (slot) on, Enter; already given: message 3; unanswerable; no request.
    s, text = session(items_dir, out, ["a", "2", "\n", "g", "3", "u", "n", "q"])
    kind, head, labels = read_answers(out)
    assert kind == "labels" and head["seed"] == 3 and head["items"] == items_digest(load_items([items_dir]))
    o = s.order
    assert labels[o[0]] == {"label": "answerable", "answer_ids": ["slot"], "quote_of": None}
    assert labels[o[1]] == {"label": "already_given", "answer_ids": [], "quote_of": 2}
    assert labels[o[2]]["label"] == "unanswerable" and labels[o[3]]["label"] == "no_request"
    assert len(labels) == 4
    assert "answerable" in text and "MESSAGE TO LABEL" in text


def test_answerable_needs_an_item_and_can_be_cancelled(items_dir, tmp_path):
    out = tmp_path / "labels.json"
    s, text = session(items_dir, out, ["a", "\n", "2", "\n", "q"])
    _, _, labels = read_answers(out)
    assert labels[s.order[0]]["answer_ids"] == ["slot"]
    assert "Pick at least one item." in text
    s, _ = session(items_dir, tmp_path / "c.json", ["a", "x", "n", "q"])
    _, _, labels = read_answers(tmp_path / "c.json")
    assert labels[s.order[0]]["label"] == "no_request"


def test_resume_starts_at_the_first_unlabelled(items_dir, tmp_path):
    out = tmp_path / "labels.json"
    s, _ = session(items_dir, out, ["n", "u", "q"])
    s2, text = session(items_dir, out, ["n", "q"], seed=None)
    _, _, labels = read_answers(out)
    assert len(labels) == 3 and labels[s.order[2]]["label"] == "no_request"
    assert "Item 3 of 6" in text


def test_resume_refuses_another_seed(items_dir, tmp_path):
    out = tmp_path / "labels.json"
    session(items_dir, out, ["n", "q"])
    with pytest.raises(ItemError, match="seed"):
        session(items_dir, out, ["q"], seed=9)


def test_go_back_and_change(items_dir, tmp_path):
    out = tmp_path / "labels.json"
    s, text = session(items_dir, out, ["n", "u", "b", "g", "3", "q"])
    _, _, labels = read_answers(out)
    # Back from item 3 to item 2, which showed its current label, then changed it.
    assert labels[s.order[1]]["label"] == "already_given"
    assert "Current label: unanswerable" in text


def test_card_shown_once_when_unchanged(items_dir, tmp_path):
    _, text = session(items_dir, tmp_path / "l.json", ["n", "q"])
    assert "(same as the previous item)" in text


def test_help_explains_each_label_with_an_example():
    for key in ("[a] answerable", "[g] already given", "[u] unanswerable", "[n] no request"):
        assert key in HELP
    assert HELP.count("Example:") == 5  # one per label, and one for a message mixing a fresh and a told item
    assert "\u2014" not in HELP and "\u2013" not in HELP and "--" not in HELP


def test_finishing_every_item(items_dir, tmp_path):
    out = tmp_path / "l.json"
    _, text = session(items_dir, out, ["n"] * 6 + ["q"])
    assert "All 6 items are labelled" in text
    assert len(read_answers(out)[2]) == 6


# ----- the command line -----------------------------------------------------------------------

def test_run_and_agree_against_a_fake_server(items_dir, tmp_path, key_file, capsys):
    out = tmp_path / "verdicts.json"
    with Fake() as f:
        cfgp = tmp_path / "cfg.json"
        cfgp.write_text(json.dumps(config_doc(f.base_url)))
        f.decide = lambda messages, keys: "B" if keys == ["A", "B"] else keys[-1]
        assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file)]) == 0
        n = len(f.requests)
        assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file)]) == 0
        assert len(f.requests) == n  # nothing left to do
    kind, head, verdicts = read_answers(out)
    assert kind == "verdicts" and len(verdicts) == 6 and all(v["label"] == "no_request" for v in verdicts.values())
    assert KEY not in out.read_text()
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"format": "cohbench-classifier-labels", "version": 1, "items": head["items"], "seed": 1,
                                  "labels": {k: {"label": "no_request" if k != "t-00" else "unanswerable", "answer_ids": [], "quote_of": None}
                                             for k in verdicts}}))
    capsys.readouterr()
    assert main(["agree", str(labels), str(out), "--items", str(items_dir)]) == 0
    text = capsys.readouterr().out
    assert "Disagreements: 1" in text and "t-00" in text
    assert main(["agree", str(labels), str(out), "--items", str(items_dir), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["n"] == 6 and rep["per_label"]["no_request"]["agree"] == 5


def test_run_reports_a_down_endpoint(items_dir, tmp_path, key_file, capsys):
    cfgp = tmp_path / "cfg.json"
    d = config_doc("http://127.0.0.1:9/v1", limits={"backoff_s": 0.01})
    cfgp.write_text(json.dumps(d))
    assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(tmp_path / "v.json"), "--key-file", str(key_file)]) == 1
    assert "transport error" in capsys.readouterr().err


def test_run_needs_a_key(items_dir, tmp_path, capsys, monkeypatch):
    from cohbench.user.api import KEY_ENV

    monkeypatch.delenv(KEY_ENV, raising=False)
    cfgp = tmp_path / "cfg.json"
    cfgp.write_text(json.dumps(config_doc()))
    assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(tmp_path / "v.json")]) == 2
    assert KEY_ENV in capsys.readouterr().err


def test_check_command(items_dir, tmp_path, capsys):
    assert main(["check", str(items_dir)]) == 0
    bad = tmp_path / "l.json"
    bad.write_text(json.dumps({"format": "cohbench-classifier-labels", "version": 1, "labels": {"t-00": {"label": "answerable"}}}))
    assert main(["check", str(items_dir), "--labels", str(bad)]) == 1


# ----- the runner's policy kind ------------------------------------------------------------------

def test_classifier_kind_is_registered_and_lazy(tmp_path, key_file, monkeypatch):
    from cohbench.runner.policy import POLICY_KINDS, policy_factory
    from cohbench.user.api import KEY_ENV
    from cohbench.user.kind import register

    register()
    register()  # harmless twice

    assert "classifier" in POLICY_KINDS
    monkeypatch.delenv(KEY_ENV, raising=False)
    with Fake() as f:
        cfgp = tmp_path / "cfg.json"
        cfgp.write_text(json.dumps(config_doc(f.base_url)))
        make = policy_factory("classifier", cfgp)  # loads and checks the config only: no key needed yet
        assert f.requests == []
        monkeypatch.setenv(KEY_ENV, str(key_file))
        a, b = make(), make()
        assert a is not b and a.client is b.client  # one client for the process
        f.decide = lambda messages, keys: "B"
        assert a.judge({"facts": [], "decisions": []}, (), "Done.").label == "no_request"
        assert f.auth == [f"Bearer {KEY}"]


def test_classifier_kind_refuses_a_bad_config(tmp_path):
    from cohbench.runner.policy import policy_factory
    from cohbench.user.config import ConfigError
    from cohbench.user.kind import register

    register()
    bad = tmp_path / "cfg.json"
    bad.write_text("{}")
    with pytest.raises(ConfigError):
        policy_factory("classifier", bad)


def test_kind_entry_imports_nothing_heavy():
    import subprocess
    import sys

    code = ("import sys; import cohbench.runner.policy as rp; before = set(sys.modules); "
            "from cohbench.user.kind import register; register(); "
            "print(sorted(set(sys.modules) - before)); print('classifier' in rp.POLICY_KINDS)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2]).stdout.splitlines()
    assert out == ["['cohbench.user', 'cohbench.user.kind']", "True"]


# ----- review round 1: agree, run, label ------------------------------------------------------------

def _verdicts(head_items, entries, order=None):
    return {"format": "cohbench-classifier-verdicts", "version": 1, "items": head_items, "config": "c", "order": order,
            "verdicts": entries}


def test_agree_refuses_another_item_set(items_dir, tmp_path, capsys):
    good = items_digest(load_items([items_dir]))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_verdicts(good, {"t-00": {"label": "no_request"}})))
    b.write_text(json.dumps(_verdicts("0" * 64, {"t-00": {"label": "no_request"}})))
    assert main(["agree", str(a), str(b), "--items", str(items_dir)]) == 2
    assert "different item set" in capsys.readouterr().err


def test_agree_reports_an_empty_intersection(items_dir, tmp_path, capsys):
    good = items_digest(load_items([items_dir]))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_verdicts(good, {"t-00": {"label": "no_request"}})))
    b.write_text(json.dumps(_verdicts(good, {"t-01": {"label": "no_request"}})))
    assert main(["agree", str(a), str(b), "--items", str(items_dir)]) == 2
    assert "no item is labelled on both sides" in capsys.readouterr().err


def test_agree_on_two_verdict_files_compares_whole_verdicts(items_dir, tmp_path, capsys):
    good = items_digest(load_items([items_dir]))
    first = {"t-00": {"label": "answerable", "answer_ids": ["no_files", "slot"], "quote_of": None, "quote": None},
             "t-01": {"label": "already_given", "answer_ids": [], "quote_of": 2, "quote": "Thanks"},
             "t-02": {"label": "no_request", "answer_ids": [], "quote_of": None, "quote": None}}
    second = json.loads(json.dumps(first))
    second["t-00"]["answer_ids"] = ["slot", "no_files"]  # same set, other reply order
    second["t-01"]["quote"] = None  # same message, the whole message instead of a part of it
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_verdicts(good, first)))
    b.write_text(json.dumps(_verdicts(good, second)))
    assert main(["agree", str(a), str(b), "--items", str(items_dir), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["kappa"] == 1 and rep["whole_verdicts"]["identical"] == 1
    assert sorted(d["item"] for d in rep["whole_verdicts"]["differences"]) == ["t-00", "t-01"]


def test_run_refuses_to_append_with_another_order(items_dir, tmp_path, key_file, capsys):
    out = tmp_path / "v.json"
    with Fake() as f:
        cfgp = tmp_path / "cfg.json"
        cfgp.write_text(json.dumps(config_doc(f.base_url)))
        f.decide = lambda messages, keys: "B"
        assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file), "--order", "3"]) == 0
        assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file)]) == 2
        assert "--order" in capsys.readouterr().err
        assert main(["run", str(items_dir), "--config", str(cfgp), "--out", str(out), "--key-file", str(key_file), "--order", "3"]) == 0
    assert read_answers(out)[1]["order"] == 3


def test_label_selects_items_numbered_ten_and_above(tmp_path):
    card = {"facts": [{"id": f"f{i}", "text": f"fact {i}"} for i in range(1, 12)], "decisions": []}
    d = tmp_path / "many"
    d.mkdir()
    (d / "m.json").write_text(json.dumps(item_doc(0, card=card)))
    out = tmp_path / "l.json"
    stdout = io.StringIO()
    s = Session(load_items([d]), out, 1, Term(io.StringIO("a\n11\n2\n\nq\n"), stdout), width=80)
    s.run()
    assert read_answers(out)[2]["t-00"]["answer_ids"] == ["f2", "f11"]


def test_label_shows_notes(tmp_path):
    card = {**CARD, "notes": ["The user runs the jobs at night."]}
    d = tmp_path / "n"
    d.mkdir()
    (d / "n.json").write_text(json.dumps(item_doc(0, card=card)))
    stdout = io.StringIO()
    Session(load_items([d]), tmp_path / "l.json", 1, Term(io.StringIO("q\n"), stdout), width=80).run()
    assert "note: The user runs the jobs at night." in stdout.getvalue()


def test_label_completion_prompt_does_what_it_says(items_dir, tmp_path):
    out = tmp_path / "l.json"
    _, text = session(items_dir, out, ["n"] * 6 + ["b", "q"])
    s = text.split("All 6 items are labelled")[1]
    assert "Press b to show the last item again" in s
    assert s.count("MESSAGE TO LABEL") == 1  # b showed the last item once more, then q quit
