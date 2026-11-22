"""The adapter's mapping, against a stand-in for the driver's Episode (no Docker)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cohbench.driver import Delivery as DDelivery
from cohbench.driver import DriverFailure, EpisodeError, ExportFailed, Question, SecretRequest
from cohbench.driver import TurnNotRunning as DriverTurnNotRunning
from cohbench.runner.adapter import DriverEpisode, load_scripted_run
from cohbench.runner.port import EpisodeFailure, ExportHandoff, ExportRejected, Pending, TurnNotRunning
from cohbench.runner.run import spec_for
from cohbench.runner.runner import RunConfig
from cohbench.runner.task import load

from .conftest import DEMO, write_task


@dataclass
class DSent:
    kind: str = "reply"
    segment: int = 1
    thread_id: str = "t1"
    message_id: str | None = "m1"
    command_id: str | None = "c1"
    run_id: str | None = "r1"
    sent_utc: str = "2026-11-22T12:00:00.000Z"
    episode_s: float = 1.5
    text: str | None = None
    mechanism: str | None = None
    request_id: str | None = None
    delivered_utc: str | None = None
    delivery: Any = None
    submitted: dict[str, str] | None = None


@dataclass
class DTurn:
    state: str
    run_id: str | None = "r1"
    outputs: list[dict[str, Any]] = field(default_factory=list)
    question: Any = None
    secret: Any = None
    errors: list[str] = field(default_factory=list)
    refusal: Any = None
    final_item_id: str | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)
    mark: dict[str, Any] | None = None
    ended_utc: str = "2026-11-22T12:00:30.000Z"  # when the driver built its report, not the hand-back


class StubEpisode:
    """Records what the adapter calls; answers with driver-shaped objects."""

    def __init__(self, harness, **kw):
        self.kw = kw
        self.calls: list[tuple[str, Any]] = []
        self.runner_events: list[dict[str, Any]] = []
        self.corrections: list[dict[str, Any]] = []
        self.user_waits: list[dict[str, Any]] = []
        self.turn: DTurn | None = None
        self.raise_on: dict[str, Exception] = {}
        self.usage_doc: dict[str, Any] = {"calls": 2, "input_tokens": 10, "cached_input_tokens": 5, "cache_write_tokens": None, "output_tokens": 7,
                                          "calls_not_complete": 0, "usage_status": "complete"}

    left_over: list[str] = []
    probe_cleanup: dict[str, Any] | None = None

    def _maybe(self, name):
        if name in self.raise_on:
            exc = self.raise_on[name]
            if getattr(exc, "once", False):
                del self.raise_on[name]
            raise exc

    def start(self, text):
        self.calls.append(("start", text))
        return DSent(kind="opening", text=text)

    def wait(self, *, timeout_s, poll_s):
        self.calls.append(("wait", timeout_s))
        return self.turn

    def reply(self, text):
        self._maybe("reply")
        self.calls.append(("reply", text))
        return DSent(text=text, message_id="m2")

    def answer(self, q, value):
        self.calls.append(("answer", (q, value)))
        norm = {k: next((o["value"] for o in (q.raw.get("questions") or [{}])[0].get("options", []) if o.get("label") == v), v) for k, v in value.items()}
        return DSent(kind="answer", message_id=None, request_id=q.request_id, submitted=norm)

    def refuse_secret(self, s, text):
        self.calls.append(("refuse_secret", (s, text)))
        return DSent(kind="refusal", message_id="m3", request_id=s.item_id)

    def correct(self, text):
        self._maybe("correct")
        self.corrections.append({"message_id": "m4", "delivered_utc": None})
        return DSent(kind="correction", message_id="m4", mechanism="native_steer")

    def correction_delivery(self, s):
        return {"delivered_utc": "2026-11-22T12:00:05.000Z", "mark": "correction-m4"}

    def interrupt(self):
        self._maybe("interrupt")
        self.calls.append(("interrupt", None))
        return {}

    def mark(self, label):
        return {"ok": True, "batch": 7, "label": label}

    def elapsed(self):
        return 12.5

    def excluded(self):
        return [{"start_utc": "a", "end_utc": "b", "reason": "probe", "seconds": 1.0}]

    def usage(self):
        return dict(self.usage_doc)

    def watcher_dirs(self):
        return [Path("/w/segment-1")]

    def describe(self):
        return {"harness": "pi", "harness_version": "1.6.0", "t3_version": "t3 v0.1.12", "driver_version": "cohbench-driver 2+abc", "model": "m", "mode": "default"}

    def probe(self, question, *, scripted_reply=None):
        self.calls.append(("probe", (question, scripted_reply)))
        from cohbench.driver import ProbeResult
        detail = {"cleanup": self.probe_cleanup} if self.probe_cleanup is not None else None
        return ProbeResult("answered", question, "data/batch3.csv", None, detail, {}, 1.0)

    def record_user_wait(self, ref, started_utc, ended_utc, *, charged_ms=None, ended="reply", cause="verdict_reply"):
        """As the driver's: no `replies_to` or `causes` parameter; returns the event it recorded."""
        self.calls.append(("record_user_wait", (ref, started_utc, ended_utc, charged_ms, {"ended": ended, "cause": cause})))
        e = {"type": "wait", "utc": started_utc, "end_utc": ended_utc, "ref": ref, "fields": {"charged_ms": charged_ms, "ended": ended, "cause": cause}}
        self.runner_events.append(e)
        return e

    def add_runner_event(self, type, utc=None, *, ref=None, end_utc=None, **fields):
        self.calls.append(("add_runner_event", (type, utc, ref, fields)))

    def export(self, dest, *, task=None, end_reason="finished", end_mark=None, end_utc=None, user_kinds=None, answers=None, marks=None, probe_targets=None):
        kw = {"task": task, "end_reason": end_reason, "end_mark": end_mark, "end_utc": end_utc, "user_kinds": user_kinds, "answers": answers, "marks": marks,
              "probe_targets": probe_targets}
        self._maybe("export")
        self.calls.append(("export", kw))
        return {"dest": str(dest), "files": {"trajectory": ["trajectory/trajectory.jsonl"]}, "report": {"bundle": {"ok": True}}}

    def close(self):
        self.calls.append(("close", None))
        self.left_over = self.left_over[1:]  # each call removes one more


@pytest.fixture
def ad(tmp_path: Path) -> tuple[DriverEpisode, StubEpisode]:
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"format": "cohbench-scripted-run", "version": 1, "script": {"version": 1, "handlers": []},
                               "probes": {"Which file?": "data/batch3.csv"}, "probe_default": "No idea."}))
    task = load(DEMO)
    cfg = RunConfig("e1", "pi", "m", ("scripted", run), "default")
    a = DriverEpisode(spec_for(task, cfg, tmp_path / "drv"), images=object(), episode_cls=StubEpisode)
    return a, a.ep  # type: ignore[return-value]


def test_construction_passes_the_spec(ad) -> None:
    a, ep = ad
    kw = ep.kw
    assert [e["type"] for e in kw["manifest"]["entries"] if e["path"].startswith("bin")] == ["dir", "file"] and kw["mode"] == "default" and kw["coached_sentence"] is None
    assert kw["watcher"].metric_cmd == ("python3", "/opt/task/count_logs.py") and kw["watcher"].task_dir.name == "metric"
    assert ".git/**" in kw["watcher"].ignore and "**/.git/**" in kw["watcher"].ignore
    assert kw["upstream"].kind == "scripted" and kw["upstream"].script == {"version": 1, "handlers": []}


def test_the_tasks_metric_timeout_reaches_the_watcher(tmp_path: Path) -> None:
    from cohbench.env import WatcherSpec

    def edit(d):
        d["watcher"]["metric"]["timeout_s"] = 7.5
    task = load(write_task(tmp_path, edit))
    cfg = RunConfig("e1", "pi", "m", ("scripted", DEMO / "scripted-run.json"), "default")
    a = DriverEpisode(spec_for(task, cfg, tmp_path / "drv"), images=object(), episode_cls=StubEpisode)
    w = a.ep.kw["watcher"]
    assert w.metric_timeout_s == 7.5 != WatcherSpec().metric_timeout_s


def test_sent_and_turn_shapes(ad) -> None:
    a, ep = ad
    s = a.start("Hello.")
    assert (s.message_id, s.run_id, s.clock_s, s.utc) == ("m1", "r1", 1.5, "2026-11-22T12:00:00.000Z")
    q = Question("Fast or slow?", ["fast", "slow"], "t1", "item-q", "req-q", ["q1"],
                 raw={"questions": [{"id": "q1", "question": "Fast or slow?", "options": [{"label": "Fast", "value": "fast"}, {"label": "Slow", "value": "slow"}]}]})
    ep.turn = DTurn("question", outputs=[{"kind": "question", "text": "Fast or slow?", "item_id": "item-q", "run_id": "r1", "delivered_utc": "x"}], question=q)
    t = a.wait(2.0)
    assert t.state == "question" and t.outputs[0].item_id == "item-q"
    p = t.pending
    assert (p.kind, p.item_id, p.request_id, p.thread_id) == ("question", "item-q", "req-q", "t1")
    assert [(o.label, o.value) for o in p.prompts[0].options] == [("Fast", None), ("Slow", None)]  # the driver maps a label to its value
    sent = a.answer(p, {"q1": "Fast"})
    assert ep.calls[-1] == ("answer", (q, {"q1": "Fast"})) and sent.message_id is None and sent.request_id == "req-q"
    assert sent.submitted == {"q1": "fast"}  # B5: the driver's normalised map, the option's value
    sec = SecretRequest("PIN", "To deploy.", [{"name": "pin", "label": "PIN"}], "t1", "item-s")
    ep.turn = DTurn("secret", outputs=[{"kind": "secret", "text": "PIN", "item_id": "item-s"}], secret=sec)
    t = a.wait(2.0)
    assert t.pending.kind == "secret" and t.pending.fields == ("PIN",)
    a.refuse_secret(t.pending, "No.")
    assert ep.calls[-1] == ("refuse_secret", (sec, "No."))
    with pytest.raises(EpisodeFailure):
        a.answer(Pending("question", "nope", None, None), {})


def test_errors_map(ad) -> None:
    a, ep = ad
    ep.raise_on["correct"] = DriverTurnNotRunning("the turn is finished (completed); this needs running")
    with pytest.raises(TurnNotRunning):
        a.correct("x")
    # D-029: the main run ended while a subagent works; nothing to steer, so the runner holds the correction.
    from cohbench.driver.episode import MainRunEnded

    ep.raise_on["correct"] = MainRunEnded("the main run has ended while subagent work continues")
    with pytest.raises(TurnNotRunning):
        a.correct("x")
    ep.raise_on["correct"] = EpisodeError("the main run has ended while subagent work continues")  # the same words, no type: not matched
    with pytest.raises(EpisodeFailure) as exc:
        a.correct("x")
    assert not isinstance(exc.value, TurnNotRunning)
    ep.raise_on["correct"] = EpisodeError("T3 rejected the queued message after the driver's interrupt")
    with pytest.raises(EpisodeFailure):
        a.correct("x")
    ep.raise_on["reply"] = EpisodeError("no turn")
    with pytest.raises(EpisodeFailure):
        a.reply("x")
    # The driver's interrupt is a no-op on a stopped tree; its errors are failures, with no text matched.
    del ep.raise_on["reply"]
    a.interrupt()
    assert ep.calls[-1] == ("interrupt", None)
    ep.raise_on["interrupt"] = EpisodeError("the tree did not stop within 60 s")
    with pytest.raises(EpisodeFailure):
        a.interrupt()


def test_a_non_fatal_driver_failure_is_retried_once(ad) -> None:
    a, ep = ad
    exc = DriverFailure("a read of T3 failed before anything was sent", fatal=False)
    exc.once = True  # type: ignore[attr-defined]
    ep.raise_on["reply"] = exc
    s = a.reply("Carry on.")
    assert s.message_id == "m2" and [c for c in ep.calls if c[0] == "reply"] == [("reply", "Carry on.")]
    assert a.absorbed and "non-fatal" in a.absorbed[0]
    ep.raise_on["reply"] = DriverFailure("acceptance unknown", fatal=True)
    with pytest.raises(EpisodeFailure):
        a.reply("Again.")
    ep.raise_on["reply"] = DriverFailure("still failing", fatal=False)  # a second non-fatal failure is not retried again
    with pytest.raises(EpisodeFailure):
        a.reply("Again.")


def test_runs_started_by_t3(ad) -> None:
    a, ep = ad
    ep.turn = DTurn("finished", run_id="r2", final_item_id="item-f",
                    outputs=[{"kind": "final", "text": "Done.", "item_id": "item-f", "run_id": "r2", "delivered_utc": "2026-11-22T12:00:09.000Z"}],
                    runs=[{"run_id": "r1", "started_by": "user"}, {"run_id": "r2", "started_by": "t3"}])
    t = a.wait(2.0)
    assert (t.run_id, t.closing, t.started_by_t3) == ("r2", "item-f", ("r2",))


def test_close_retries_what_is_left_over(ad) -> None:
    a, ep = ad
    ep.left_over = ["vol-a", "vol-b"]
    a.close()
    assert [c for c in ep.calls if c[0] == "close"] == [("close", None), ("close", None)]
    ep.left_over = ["vol-a", "vol-b", "vol-c"]
    with pytest.raises(EpisodeFailure):
        a.close()


def test_usage_probe_mark_clock(ad) -> None:
    a, ep = ad
    assert a.usage().tokens == 0 and a.clock() == 0.0  # before the start
    a.start("x")
    u = a.usage()
    assert (u.tokens, u.complete) == (22, True) and u.raw["calls"] == 2
    ep.usage_doc.update(calls_not_complete=1, usage_status="partial")
    assert a.usage().complete is False
    ep.usage_doc = {"calls": 1, "input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None, "output_tokens": None, "calls_not_complete": 1}
    assert a.usage().tokens is None
    assert a.mark("m1").batch == 7 and a.clock() == 12.5
    assert a.probe("Which file?").answer == "data/batch3.csv" and ep.calls[-1] == ("probe", ("Which file?", "data/batch3.csv"))
    a.probe("Other?")
    assert a.probe("Other?").cleanup is None
    ep.probe_cleanup = {"complete": True, "left_over": [], "problems": []}
    assert a.probe("Other?").cleanup is None
    ep.probe_cleanup = {"complete": False, "left_over": ["nsnap1"], "problems": ["image still in use"]}
    assert a.probe("Other?").cleanup == ep.probe_cleanup
    assert ep.calls[-1] == ("probe", ("Other?", "No idea."))
    assert a.excluded()[0].reason == "probe" and a.describe().harness_version == "1.6.0"


def handoff(events: list[dict[str, Any]]) -> ExportHandoff:
    return ExportHandoff(runner_events=events, user_kinds={"m1": "opening_prompt"}, answers={}, identifiers={}, observations={},
                         episode_record={"probes": []}, probe_targets={1: {"about": "output", "ref": {"item": "i1"}, "deferred": False, "event": "conflict"}},
                         marks=["m1"], end_mark="m1", end_reason="finished", post_run=None, excluded=[])


def ev(type, ref=None, **fields):
    return {"type": type, "utc": "2026-11-22T12:00:01.000Z", "ref": ref, "fields": fields, "end_utc": "2026-11-22T12:00:03.000Z" if type == "wait" else None}


def test_export_forwards_once(ad, tmp_path: Path) -> None:
    """The driver's own events are not added again; a next-message correction's delivery is the runner's."""
    a, ep = ad
    ep.runner_events = [{"type": "probe"}, {"type": "controlled_reset"}]
    events = [ev("verdict", {"item": "i1"}, label="answerable", reply="r"), ev("wait", {"item": "i1"}, charged_ms=2000, ended="reply", cause="verdict_reply", replies_to=[{"item": "i1"}]),
              ev("probe", probe=1), ev("controlled_reset", {"message": "m9"}), ev("correction_delivered", {"message": "m4"}, mark="m2"),
              ev("correction_delivered", {"message": "m7"}, mark="m3"), ev("planted", name="x", phase="fired"), ev("episode_end", reason="finished")]
    r = a.export(tmp_path / "x", handoff(events))
    added = [c[1][0] for c in ep.calls if c[0] == "add_runner_event"]
    # The runner owns the deliveries (with their marks) and the end; the driver keeps resets and probes.
    assert added == ["verdict", "correction_delivered", "correction_delivered", "planted", "episode_end"]
    assert [(c[1][2], c[1][3]) for c in ep.calls if c[0] == "add_runner_event" and c[1][0] == "correction_delivered"] == [
        ({"message": "m4"}, {"mark": "m2"}), ({"message": "m7"}, {"mark": "m3"})]
    assert ep.kw["driver_events"] == ("user_kinds", "controlled_reset", "probe")
    # The driver's record_user_wait takes no `replies_to`: the adapter puts it on the event that call recorded.
    assert next(e for e in ep.runner_events if e["type"] == "wait")["fields"]["replies_to"] == [{"item": "i1"}]
    waits = [c for c in ep.calls if c[0] == "record_user_wait"]
    assert waits[0][1][:4] == ({"item": "i1"}, "2026-11-22T12:00:01.000Z", "2026-11-22T12:00:03.000Z", 2000) and waits[0][1][4]["cause"] == "verdict_reply"
    kw = next(c[1] for c in ep.calls if c[0] == "export")
    assert kw["probe_targets"] == {1: {"about": "output", "ref": {"item": "i1"}, "deferred": False, "event": "conflict"}} and kw["end_reason"] == "finished"
    assert kw["end_mark"] == "m1" and kw["task"].episode == {"probes": []}
    assert r.files == {"trajectory": ["trajectory/trajectory.jsonl"]} and r.report["bundle"]["ok"]


def test_export_refuses_a_disagreement(ad, tmp_path: Path) -> None:
    a, ep = ad
    ep.runner_events = [{"type": "probe"}]
    with pytest.raises(ExportRejected, match="probe: the runner has 0, the driver 1"):
        a.export(tmp_path / "x", handoff([]))
    ep.runner_events = []
    with pytest.raises(ExportRejected, match="needs a reference"):
        a.export(tmp_path / "x", handoff([ev("verdict", None, label="answerable", reply=None)]))  # check_runner_inputs refuses it
    assert not any(c[0] == "export" for c in ep.calls)
    ep.raise_on["export"] = ExportFailed("bundle", ["episode.json: x"])
    with pytest.raises(ExportRejected) as exc:
        a.export(tmp_path / "x", handoff([]))
    assert exc.value.stage == "bundle" and exc.value.reasons == ["episode.json: x"]


def test_scripted_run_file(tmp_path: Path) -> None:
    plain = tmp_path / "s.json"
    plain.write_text(json.dumps({"version": 1, "handlers": []}))
    assert load_scripted_run(plain) == ({"version": 1, "handlers": []}, {}, "I don't know.")
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"format": "cohbench-scripted-run", "version": 1, "script": {}, "probes": {"q": 1}}))
    with pytest.raises(ValueError):
        load_scripted_run(bad)
    script, probes, default = load_scripted_run(DEMO / "scripted-run.json")
    assert script["handlers"] and set(probes) == {e.probe.question for e in load(DEMO).events if e.probe}


def test_delivery_shapes(ad) -> None:
    a, ep = ad
    a.start("x")
    sent = a.correct("Use CSV.")
    d = a.correction_delivery(sent)
    assert (d.utc, d.mark) == ("2026-11-22T12:00:05.000Z", "correction-m4")
    assert DriverEpisode._delivery("2026-11-22T12:00:05.000Z", object()).mark is None
    assert DriverEpisode._delivery(("t", "mk"), object()).mark == "mk" and DriverEpisode._delivery(None, object()) is None
    # The driver's Delivery, with the watcher's word on the mark, passed on as given.
    dd = DDelivery("2026-11-22T12:00:05.000Z", mark_label="correction-m4", mark_taken=True, mark_at="2026-11-22T12:00:05.000Z", mark_straddled=3)
    assert DriverEpisode._delivery(dd, None) == type(d)("2026-11-22T12:00:05.000Z", "correction-m4", None, None, straddled=3, straddled_last=3)
    no_mark = DDelivery("2026-11-22T12:00:05.000Z", mark_label=None, mark_taken=False, mark_error="no acknowledgement")
    assert DriverEpisode._delivery(no_mark, None) == type(d)("2026-11-22T12:00:05.000Z", None, None, None)
    # A Delivery already on the driver's Sent comes back on the port's Sent.
    ep.correct = lambda text: DSent(kind="correction", message_id="m5", mechanism="native_steer", delivered_utc=dd.delivered_utc, delivery=dd)  # type: ignore[method-assign]
    s2 = a.correct("Use JSON.")
    assert (s2.delivered_utc, s2.delivery_mark) == (dd.delivered_utc, "correction-m4")


def test_a_raise_from_the_drivers_probe_is_a_driver_failure(ad) -> None:
    """B4: the driver reports every copy-side problem as an outcome; a raise is fatal."""
    a, ep = ad
    a.start("x")

    def boom(question, *, scripted_reply=None):
        raise DriverFailure("the main container could not be resumed", fatal=True)
    ep.probe = boom  # type: ignore[method-assign]
    with pytest.raises(EpisodeFailure) as exc:
        a.probe("Which file?")
    assert exc.value.fatal is True


def test_a_mark_the_watcher_did_not_acknowledge_is_not_fatal(ad) -> None:
    a, ep = ad
    ep.mark = lambda label: {"ok": False, "error": "timed out"}  # type: ignore[method-assign]
    with pytest.raises(EpisodeFailure) as exc:
        a.mark("m1")
    assert exc.value.fatal is False


def test_every_request_is_rendered_canonically_whatever_its_position(ad) -> None:
    """Minor (round 3): the marked request and another one, and secret placeholders, as the trajectory unit renders them."""
    from cohbench.trajectory.normalise import render_prompts, render_secret

    a, ep = ad
    raw_q = {"id": "item-q", "requestId": "req-q", "questions": [{"id": "q1", "header": "Slot", "question": "Which job?", "multiSelect": True,
                                                                  "options": [{"label": "A", "value": "a", "description": "the first"}]}]}
    raw_s = {"id": "item-s", "label": "Deploy key", "reason": "To push.", "fields": [{"name": "key", "label": "Key", "placeholder": "ssh-ed25519 ..."}]}
    sec = SecretRequest("Deploy key", "To push.", raw_s["fields"], "t2", "item-s", raw=raw_s)
    ep.turn = DTurn("secret", secret=sec, outputs=[
        {"kind": "question", "text": "Which job?", "item_id": "item-q", "run_id": "r1", "delivered_utc": "x", "raw": raw_q, "actionable": False},
        {"kind": "secret", "text": "Deploy key\nTo push.\nKey", "item_id": "item-s", "run_id": "r1", "delivered_utc": "y", "actionable": True}])
    t = a.wait(2.0)
    want_q = render_prompts([{"header": "Slot", "text": "Which job?", "multi_select": True, "options": [{"label": "A", "description": "the first"}]}])
    want_s = render_secret({"label": "Deploy key", "reason": "To push.", "fields": [{"label": "Key", "placeholder": "ssh-ed25519 ..."}]})
    assert [o.text for o in t.outputs] == [want_q, want_s] and "(ssh-ed25519 ...)" in want_s
    assert t.pending.item_id == "item-s"


def test_the_mechanism_is_taken_at_settlement(ad) -> None:
    """Minor (round 3): the driver fills the observed mechanism in after `correct` returns."""
    a, ep = ad
    a.start("x")
    ep.correct = lambda text: DSent(kind="correction", message_id="m6", mechanism=None)  # type: ignore[method-assign]
    sent = a.correct("Use CSV.")
    assert sent.mechanism is None
    a._sent["m6"].mechanism = "interrupt_restart"
    assert a.correction_delivery(sent).mechanism == "interrupt_restart"


def test_an_environment_failure_is_a_driver_failure(ad) -> None:
    from cohbench.env.docker import DockerError

    a, ep = ad
    ep.raise_on["reply"] = DockerError("watcher did not come up")
    with pytest.raises(EpisodeFailure, match="environment: watcher did not come up"):
        a.reply("x")


def test_a_mark_names_its_instant_and_usage_errors_map(ad) -> None:
    """D-037: the instant reaches the driver's mark; a gateway failure in `usage` is a driver failure."""
    from cohbench.env.docker import DockerError

    a, ep = ad
    seen = {}

    def mark(label, *, at=None, timeout_s=None, limit_s=None):
        seen["at"] = at
        return {"ok": True, "accepted": True, "batch": 3, "straddled": 2, "covered_utc": at}
    ep.mark = mark  # type: ignore[method-assign]
    ack = a.mark("m1", "2026-11-22T12:00:02.030000Z")
    assert seen["at"] == "2026-11-22T12:00:02.030000Z" and (ack.batch, ack.straddled, ack.accepted, ack.covered_utc) == (3, 2, True, seen["at"])
    a.start("x")

    def usage():
        raise DockerError("gateway summary failed")
    ep.usage = usage  # type: ignore[method-assign]
    with pytest.raises(EpisodeFailure, match="environment: gateway summary failed"):
        a.usage()


def test_the_hand_back_instant_and_a_held_correction(ad) -> None:
    """Driver round 6: the hand-back instant is the one the driver's turn-end mark names (not
    `ended_utc`), else the pending request's start; a non-fatal DriverFailure from `correct`
    sent nothing and holds the correction like MainRunEnded."""
    a, ep = ad
    ep.turn = DTurn("finished", final_item_id="item-f", outputs=[{"kind": "final", "text": "Done.", "item_id": "item-f", "run_id": "r1", "delivered_utc": "x"}],
                    mark={"ok": True, "accepted": True, "at": "2026-11-22T12:00:09.500Z", "batch": 2})
    assert a.wait(2.0).at == "2026-11-22T12:00:09.500Z"
    q = Question("Which?", [], "t1", "item-q", "req-q", ["q1"], raw={"startedAt": "2026-11-22T12:00:11.000Z", "questions": [{"id": "q1", "question": "Which?"}]})
    ep.turn = DTurn("question", question=q, outputs=[{"kind": "question", "text": "Which?", "item_id": "item-q", "delivered_utc": "y"}])
    assert a.wait(2.0).at == "2026-11-22T12:00:11.000Z"
    ep.turn = DTurn("time_limit", mark={"at": "2026-11-22T12:00:12.000Z"})
    assert a.wait(2.0).at is None  # only a hand-back has one
    ep.raise_on["correct"] = DriverFailure("a child thread could not be read", fatal=False)
    with pytest.raises(TurnNotRunning, match="nothing sent"):
        a.correct("x")


def test_the_export_gets_the_end_instant(ad, tmp_path: Path) -> None:
    a, ep = ad
    h = handoff([])
    h = type(h)(**{**h.__dict__, "end_utc": "2026-11-22T12:00:15.000000Z"})
    a.export(tmp_path / "x", h)
    kw = next(c[1] for c in ep.calls if c[0] == "export")
    assert kw["end_utc"] == "2026-11-22T12:00:15.000000Z"


def test_driver_round_six_shapes(ad) -> None:
    """The hand-back instant is `handback_utc`; the reset takes the handoff's limit and the
    continuation's separately; an unsent call is not retried and is not fatal."""
    a, ep = ad
    ep.turn = DTurn("finished", final_item_id="item-f", outputs=[{"kind": "final", "text": "Done.", "item_id": "item-f", "run_id": "r1", "delivered_utc": "x"}],
                    mark={"at": "2026-11-22T12:00:09.000Z"})
    ep.turn.handback_utc = "2026-11-22T12:00:08.750Z"  # type: ignore[attr-defined]
    assert a.wait(2.0).at == "2026-11-22T12:00:08.750Z"
    seen = {}

    def reset(text, *, timeout_s=900, dispatch_timeout_s=None):
        seen.update(timeout_s=timeout_s, dispatch_timeout_s=dispatch_timeout_s)
        return DSent(kind="reset", message_id="m9")
    ep.reset = reset  # type: ignore[method-assign]
    a.reset("Hi again.", dispatch_timeout_s=4.5)
    assert seen == {"timeout_s": 900, "dispatch_timeout_s": 4.5}
    calls = []

    def reply(text, **kw):
        calls.append(text)
        exc = DriverFailure("never reached T3", fatal=False)
        exc.unsent = True  # type: ignore[attr-defined]
        raise exc
    ep.reply = reply  # type: ignore[method-assign]
    with pytest.raises(EpisodeFailure) as err:
        a.reply("x")
    assert err.value.fatal is False and calls == ["x"]  # once: nothing was sent, no retry


def test_an_unsent_continuation_then_a_cutoff_exports(ad, tmp_path: Path) -> None:
    """Addition 1: the driver records the reset at its handoff with `continuation: "unsent"`; the
    runner records it once too, with the same state. Handoffs are compared with handoffs: the export
    is accepted, the driver keeps its own record of the reset, and the episode is usable."""
    a, ep = ad
    ep.runner_events = [{"type": "controlled_reset", "utc": "2026-11-22T12:00:08.001Z", "ref": None, "fields": {}, "end_utc": None, "by_driver": True,
                         "continuation": "unsent", "segment": 2}]
    reset = {**ev("controlled_reset", None), "continuation": "unsent"}
    events = [reset, ev("budget_cutoff", budget="time"), ev("episode_end", reason="budget_cutoff")]
    h = handoff(events)
    h = type(h)(**{**h.__dict__, "end_reason": "budget_cutoff"})
    r = a.export(tmp_path / "x", h)
    added = [c[1][0] for c in ep.calls if c[0] == "add_runner_event"]
    assert added == ["budget_cutoff", "episode_end"]  # the reset is the driver's record
    assert r.report["bundle"]["ok"] and next(c[1] for c in ep.calls if c[0] == "export")["end_reason"] == "budget_cutoff"
    # A disagreement on the continuation's state is refused.
    ep.runner_events[0]["continuation"] = "launched"
    with pytest.raises(ExportRejected, match="continuation is unsent, the driver's launched"):
        a.export(tmp_path / "y", h)
