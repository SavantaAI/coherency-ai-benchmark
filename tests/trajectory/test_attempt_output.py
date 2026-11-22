"""Output-based attempts use the same whole-line rule as command captures."""

import hashlib

import pytest

from cohbench.trajectory import Hints, RunnerEvent, Segment, normalise, t3
from cohbench.trajectory.observations import TaskObservations, build
from cohbench.trajectory.adapters import PiProfile
from . import mini


PATTERN = {"name": "profile", "regex": r"\./bin/profile", "capture": r"^PROFILE (.+)$"}


def extract(command, output, *, by=True, unavailable=False):
    th = mini.thread()
    run = mini.run_rec(th["id"], 1, 0, 9)
    call = mini.command(run, 1, 2, command, output)
    if unavailable:
        call.pop("output", None)
        call["outputOmitted"] = True
    items = [mini.user(run, 0, "go"), call, mini.assistant(run, 8, "done")]
    segment = Segment(t3.from_export(mini.export([(th, [run], items)])))
    traj = normalise([segment], PiProfile()).trajectory
    spec = {**PATTERN, **({"by": "output"} if by else {})}
    return build(traj, None, TaskObservations.from_dict({"attempts": [spec]}))


@pytest.mark.parametrize("command", ["cd bin && ./profile", "./wrapper", "./bin/profile"])
def test_output_recognizes_equivalent_commands(command):
    obs = extract(command, "PROFILE ops=12\n")
    assert len(obs.records) == 1
    assert obs.records[0]["result"] == {"status": "complete", "fingerprint": hashlib.sha256(b"ops=12").hexdigest()}
    assert (obs.records[0]["t"], obs.records[0]["t_end"]) == (1000, 2000)


def test_three_runs_in_one_shell_call_are_three_attempts():
    obs = extract("./wrapper", "PROFILE ops=12\nPROFILE ops=13\nPROFILE ops=12\n")
    assert len(obs.records) == 3
    assert [x["result"]["fingerprint"] for x in obs.records] == [hashlib.sha256(v.encode()).hexdigest() for v in ("ops=12", "ops=13", "ops=12")]
    assert len(set(obs.sources.values())) == 1
    assert len(set(obs.wall.values())) == 1


@pytest.mark.parametrize("output, count", [
    ("PROFILE ops=12\n… output truncated for transport", 0),
    ("PROFILE ops=12\n\n… output truncated for transport", 1),
    ("PROFILE ops=12\nPROFILE ops=13\n… output truncated for transport", 1),
    ("PROFILE ops=12\nPROFILE ops=13\n\n[Showing lines 2-3 of 3 (50.0KB limit). Full output: /tmp/pi-bash-0a1b2c3d.log]", 1),
    ("no result\n", 0),
])
def test_output_clipping_counts_only_whole_result_lines(output, count):
    assert len(extract("./bin/profile", output).records) == count


def test_unavailable_output_falls_back_to_command_regex():
    assert extract("./wrapper", None, unavailable=True).records == []
    assert extract("./bin/profile", None, unavailable=True).records[0]["result"] == {"status": "unavailable", "fingerprint": None}


def test_without_by_keeps_one_attempt_and_identical_bytes():
    a = extract("./bin/profile", "PROFILE ops=12\n", by=False)
    b = extract("./bin/profile", "PROFILE ops=12\nPROFILE ops=13\n", by=False)
    assert a.dumps() == b.dumps()
    assert extract("./wrapper", "PROFILE ops=12\n", by=False).records == []


@pytest.mark.parametrize("extra", [{"by": "command"}, {"by": None}, {"by": []}, {"by": "output", "capture": None}])
def test_output_spec_validation(extra):
    with pytest.raises(ValueError):
        TaskObservations.from_dict({"attempts": [{**PATTERN, **extra}]})


@pytest.mark.parametrize("fresh", [False, True])
def test_recorded_delivery_before_t3_message_keeps_its_mark_instant(fresh):
    from cohbench.trajectory.view import scorer_view

    th = mini.thread()
    first = mini.run_rec(th["id"], 1, 0, 5)
    resumed = mini.run_rec(th["id"], 2, 10, 15, fresh=fresh)
    corrected = mini.run_rec(th["id"], 3, 20.111, 25)
    opening = mini.user(first, 0, "go")
    continuation = mini.user(resumed, 10, "Continue.")
    correction = mini.user(corrected, 20.111, "Use the actual count.")
    hints = Hints(user_kinds={correction["messageId"]: "correction"}, runner=[RunnerEvent("correction_delivered", mini.ts(20), {"message": correction["messageId"]}, {"mark": "delivery"})])
    items = [opening, mini.assistant(first, 4, "Notes."), continuation, mini.assistant(resumed, 14, "Done."), correction, mini.assistant(corrected, 24, "Corrected.")]
    result = normalise([Segment(t3.from_export(mini.export([(th, [first, resumed, corrected], items)])))], PiProfile(), hints=hints)
    from cohbench.trajectory import validate

    assert validate(result.trajectory).ok, validate(result.trajectory).errors
    view = scorer_view(result.trajectory)
    delivery = next(x for x in view.events if x["type"] == "correction_delivered")
    message = next(x for x in view.events if x["type"] == "user_message" and x["kind"] == "correction")
    assert delivery["t"] == message["t"] == 20000


@pytest.mark.parametrize("clipped, count", [("middle", 0), ("head", 2), ("tail", 2), ("both", 1)])
def test_output_whole_line_rule_at_each_cut_edge(clipped, count):
    import re
    from cohbench.trajectory.observations import whole_matches

    matches = list(whole_matches(re.compile(PATTERN["capture"], re.M), "PROFILE first\nPROFILE middle\nPROFILE last", clipped))
    assert len(matches) == count
