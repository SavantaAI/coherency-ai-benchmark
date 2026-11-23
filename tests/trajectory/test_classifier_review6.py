"""Receipt behavior must match main's normaliser."""

import pytest
from cohbench.trajectory import Hints, Segment, t3
from . import mini
from .conftest import run, snapshot_segment


def test_r6_v3_equal_queue_steer_prefers_native_receipt():
    seg = snapshot_segment(
        "pi-steering-snapshot.json", "pi-steering", "pi-steering-transcript.json"
    )
    msg = [
        i for i in next(iter(seg.threads.values())).items if i["type"] == "user_message"
    ][1]
    msg["completedAt"] = msg["startedAt"]
    result = run([seg])
    receipt = result.manifest["steering"][0]
    assert receipt["delivery_source"] == "harness session"
    assert receipt["delivered_utc"] == "2026-11-21T21:14:43.490Z"


@pytest.mark.parametrize("explicit_kind", [False, True])
def test_r6_v3_queued_refusal_matches_main_restart_inference(
    explicit_kind, monkeypatch
):
    from cohbench.runner.adapter import DriverEpisode
    from tests.driver.test_units import _episode, proj

    th = mini.thread("t")
    first = mini.run_rec("t", 1, 0, 3, status="interrupted")
    second = mini.run_rec("t", 2, 4, 10)
    opening = mini.user(first, 0, "Please continue.")
    msg = mini.user(second, 2, "Continue without it.", inputIntent="turn_start")
    msg["completedAt"] = mini.ts(4)
    items = [
        opening,
        mini.assistant(first, 1, "Noted."),
        msg,
        mini.assistant(second, 8, "Done."),
    ]
    ep = _episode({"t": proj("running", items, runs=[first, second])})
    ep.driver_events = ("user_kinds",) if explicit_kind else ()
    monkeypatch.setattr("cohbench.driver.episode.utc_now", lambda: mini.ts(1))
    sent = ep._new_sent("refusal", "t", text=msg["text"])
    sent.message_id = msg["messageId"]
    sent.mechanism = "queued"
    adapter = object.__new__(DriverEpisode)
    adapter.ep = ep
    adapter._sent = {}
    adapter.absorbed = []
    port_sent = adapter._port_sent(sent)
    observed = adapter.correction_delivery(port_sent)
    result = run(
        [Segment(t3.from_export(mini.export([(th, [first, second], items)])))],
        hints=Hints(user_kinds={msg["messageId"]: "reply"} if explicit_kind else {}),
    )
    event = next(
        x
        for x in result.trajectory.events
        if x["type"] == "user_message" and x["text"] == msg["text"]
    )
    want = 2 if explicit_kind else 4
    assert observed.utc == mini.ts(want).replace(".000Z", "Z")
    assert event["t"] == want * 1000
