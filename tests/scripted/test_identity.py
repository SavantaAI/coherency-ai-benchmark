"""Identity: which conversation, delegation and handler a request belongs to.
One test per reviewer scenario, then the rules a to f one by one."""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from cohbench.scripted import ScriptError, load_script
from cohbench.scripted.wire import parse_request

from .conftest import Convo, calls_of, fixture_pairs, text_of

FALLBACK = "Sorry, I can't continue with this right now."
NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def launch(task: str, role: str | None = "research") -> str:
    prompt = task if role in (None, "general") else f"Act as the {role} sub-agent for this task.\n\n{task}"
    return f"{NOTE}\n\n{prompt}"


def resume(body: dict, reply_text: str, new_text: str) -> dict:
    """The full-history create Pi sends after a restart."""
    b = copy.deepcopy(body)
    b["messages"] = b["messages"] + [{"role": "assistant", "content": [{"type": "text", "text": reply_text}]},
                                     {"role": "user", "content": [{"type": "text", "text": new_text}]}]
    return b


def sse_text(events) -> str:
    return "".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta")


# ------------------------------------------------------- reviewer cases


def test_full_history_resumes_of_the_two_pending_creates_stay_apart(server):
    """The two saved creates differ only in their tools. Each resume goes
    back to its own conversation, and four user messages are counted."""
    (first, _), (second, _) = fixture_pairs("pending")
    server.load({"version": 1, "handlers": [{"steps": [], "final": "same"}] * 4})
    for body in (first, second):
        assert sse_text(server.post(body)[2]) == "same"
    for body, text in ((first, "next A"), (second, "next B")):
        status, _, events = server.post(resume(body, "same", text))
        assert status == 200 and sse_text(events) == "same"
        assert server.app.log.lines[-1]["identity"]["rule"] == "c"
    ledger = server.episode.state()["user_messages"]["main"]
    assert [(u["index"], u["conversation"]) for u in ledger] == [(0, "c1"), (1, "c2"), (2, "c1"), (3, "c2")]
    assert codes(server) == []


def test_a_stale_retry_does_not_consume_the_announcement(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": f"h{i}"} for i in range(3)]})
    assert text_of(Convo(server).user("start")) == "h0"
    server.raw("POST", "/next", json.dumps({"index": 1, "text": "next please"}).encode(), admin=True)
    retry = Convo(server).user("start")  # Pi resending the earlier create
    assert text_of(retry) == "h0" and server.app.log.lines[-1]["decision"] == "replay"
    assert server.episode.state()["announcements"][0]["outcome"] == "pending"
    assert text_of(Convo(server).user("next please")) == "h1"
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0, 1]
    assert codes(server) == []


def two_sequential_delegations(task_a="check A", task_b="check A"):
    return {"version": 1, "handlers": [{"steps": [
        {"do": "delegate", "child": "worker", "task": task_a, "input": {"role": "research"}},
        {"do": "delegate", "child": "worker", "task": task_b, "input": {"role": "research"}},
    ], "final": "both done"}], "children": {"worker": {"handlers": [{"steps": [], "final": "child first"}]}}}


def test_a_second_launch_with_the_same_prompt_gets_its_own_child(server):
    server.load(two_sequential_delegations())
    parent = Convo(server)
    parent.user("delegate twice")
    first = Convo(server).user(launch("check A"))
    parent.results(['{"summary":"child first"}'])
    second = Convo(server).user(launch("check A"))
    assert text_of(first) == text_of(second) == "child first"
    assert second["id"] != first["id"] and server.app.log.lines[-1]["decision"] == "final"
    assert server.app.log.lines[-1]["identity"]["rule"] == "b"
    st = server.episode.state()
    assert [d["status"] for d in st["delegations"]] == ["claimed", "claimed"]
    assert [d["conversation"] for d in st["delegations"]] == ["c2", "c3"]
    assert st["user_messages"]["child:worker/1"][0]["index"] == st["user_messages"]["child:worker/2"][0]["index"] == 0


def test_a_repeat_that_delegates_twice_does_not_exhaust_the_child(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "repeat", "max": 2, "steps": [
        {"do": "delegate", "child": "worker", "task": "check B"}]}], "final": "done"}],
        "children": {"worker": {"handlers": [{"steps": [], "final": "worked"}]}}})
    parent = Convo(server)
    parent.user("go")
    assert text_of(Convo(server).user(launch("check B", None))) == "worked"
    parent.results(["ok"])
    assert text_of(Convo(server).user(launch("check B", None))) == "worked"
    assert text_of(parent.results(["ok"])) == "done"
    assert codes(server) == []


def parallel_delegations(tasks):
    return {"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "worker", "task": t, "input": {"role": "research"}} for t in tasks]}],
        "final": "done"}],
        "children": {"worker": {"handlers": [
            {"steps": [{"do": "bash", "command": "echo {{user_message}}"}], "final": "child zero"},
            {"steps": [], "final": "child one"}]}}}


def decisions_for(server, tasks, launch_order, sends):
    """Run a parent that delegates in parallel, report its reply's delivery
    attempts in the order given, then launch the children in the order
    given. Returns the decisions, named by task rather than arrival."""
    server.load(parallel_delegations(tasks))
    parent = Convo(server)
    body = parent.body([{"role": "user", "content": "go"}])
    out = server.episode.handle(parse_request(body))
    for ok in sends:
        server.episode.reply_sent(out.msg_id, ok)
    got = {}
    for i in launch_order:
        c = Convo(server)
        o = server.episode.handle(parse_request(c.body([{"role": "user", "content": launch(tasks[i])}])))
        log = o.log
        got[i] = (log["identity"]["rule"], log["identity"]["decision"], log.get("conversation_kind"),
                  log.get("handler"), [b.get("name") or b.get("text") for b in o.blocks],
                  [u["index"] for u in log.get("user_messages", [])])
    return [got[i] for i in sorted(got)], codes(server)


@pytest.mark.parametrize("tasks", [("t1", "t2"), ("same", "same")], ids=["different tasks", "identical tasks"])
def test_parallel_launches_and_send_order_do_not_change_decisions(server, tasks):
    results = set()
    for order in itertools.permutations([0, 1]):
        for sends in ([True, False], [False, True], [True]):
            got, errs = decisions_for(server, tasks, order, sends)
            results.add(json.dumps([got, errs]))
    assert len(results) == 1, results
    got, errs = json.loads(results.pop())
    assert errs == [] and all(g[:4] == ["b", "child launch", "child", 0] for g in got)


def test_send_completion_order_does_not_change_child_routing(server):
    for sends in ([True, False], [False, True]):
        got, errs = decisions_for(server, ("check A",), [0], sends)
        assert got[0][2] == "child" and errs == [], sends


def test_a_capture_failure_in_a_parallel_step_does_not_hide_a_failed_delegation(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "bash", "command": "./submit", "capture": [{"var": "job", "regex": "J-\\d+", "required": True}]},
        {"do": "delegate", "child": "worker", "task": "check A", "input": {"role": "research"}}]}],
        "final": "x"}, {"steps": [], "final": "main next"}],
        "children": {"worker": {"handlers": [{"steps": [], "final": "child"}]}}})
    parent = Convo(server)
    parent.user("go")
    assert text_of(parent.results(["no id here", ("delegation failed", True)])) == FALLBACK
    st = server.episode.state()
    assert st["delegations"][0]["status"] == "failed" and codes(server) == ["capture_failed"]
    assert text_of(Convo(server).user(launch("check A"))) == "main next"
    assert codes(server) == ["capture_failed", "unmatched_child"]


@pytest.mark.parametrize("on_steer,first_status", [("finish", "error"), ("abandon", "abandoned")])
def test_a_capture_failure_does_not_skip_the_correction(server, on_steer, first_status):
    server.load({"version": 1, "handlers": [
        {"on_steer": on_steer, "steps": [
            {"do": "bash", "command": "./submit", "capture": [{"var": "job", "regex": "J-\\d+", "required": True}]},
            {"do": "bash", "command": "./status {{job}}"}], "final": "x"},
        {"steps": [], "final": "corrected"}]})
    c = Convo(server)
    c.user("submit")
    r = c.results(["no id"], then_user="Stop, use the other batch.")
    assert text_of(r) == "corrected"
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == [first_status, "done"]
    assert codes(server) == (["capture_failed"] if on_steer == "finish" else [])


def test_the_same_task_to_two_children_in_one_parallel_step_is_refused():
    with pytest.raises(ScriptError) as e:
        load_script({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
            {"do": "delegate", "child": "a", "task": "t", "input": {"role": "research"}},
            {"do": "delegate", "child": "b", "task": "t", "input": {"role": "research"}}]}], "final": "x"}],
            "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}, "b": {"handlers": [{"steps": [], "final": "b"}]}}})
    assert "cannot be decided" in e.value.errors[0]
    # The same task with different roles gives different launch messages: allowed.
    load_script({"version": 1, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "a", "task": "t", "input": {"role": "research"}},
        {"do": "delegate", "child": "b", "task": "t", "input": {"role": "review"}}]}], "final": "x"}],
        "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}, "b": {"handlers": [{"steps": [], "final": "b"}]}}})


def test_rendered_tasks_that_collide_are_refused_at_run_time(server):
    server.load({"version": 1, "vars": {"x": "t", "y": "t"}, "handlers": [{"steps": [{"do": "parallel", "calls": [
        {"do": "delegate", "child": "a", "task": "{{x}}"}, {"do": "delegate", "child": "b", "task": "{{y}}"}]}],
        "final": "x"}],
        "children": {"a": {"handlers": [{"steps": [], "final": "a"}]}, "b": {"handlers": [{"steps": [], "final": "b"}]}}})
    assert text_of(Convo(server).user("go")) == FALLBACK and codes(server) == ["ambiguous_delegation"]
    assert server.episode.state()["delegations"] == []


# -------------------------------------------------------------- rules


def test_a_continue_is_identified_by_its_message(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": f"h{i}"} for i in range(3)]})
    c = Convo(server)
    first = c.user("one")
    body = c.body([{"role": "user", "content": "two"}])
    s1, _, e1 = server.post(body)
    s2, _, e2 = server.post(body)
    assert e1[0][1]["message"]["id"] == e2[0][1]["message"]["id"]
    assert server.app.log.lines[-1]["identity"] == {"rule": "a", "decision": "retry",
                                                    "reason": f"an identical continue from {first['id']} was answered by "
                                                              f"{e1[0][1]['message']['id']}"}
    other = c.body([{"role": "user", "content": "something different"}])
    status, _, err = server.post(other)
    assert status == 400 and err["error"]["details"] == {"error_code": "thread_already_continued"}


def test_history_that_matches_nothing_or_an_old_reply_is_an_error(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": f"h{i}"} for i in range(4)]})
    c = Convo(server)
    c.user("one")
    c.user("two")
    # Unknown: an assistant message this server never sent.
    stranger = Convo(server)
    stranger.history = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "something else"}]
    assert text_of(stranger.user("three", full=True)) == FALLBACK
    # Stale: the history stops at h0, but the conversation has replied since.
    stale = Convo(server)
    stale.history = c.history[:2]
    assert text_of(stale.user("three", full=True)) == FALLBACK
    assert codes(server) == ["unknown_history", "stale_history"]
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0, 1]
    assert server.episode.state()["conversations"]["u1"]["kind"] == "unplaced"


def test_history_that_matches_two_conversations_is_ambiguous(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "same"}] * 4})
    Convo(server).user("x")
    server.raw("POST", "/next", b'{"index": 1, "text": "x"}', admin=True)
    Convo(server).user("x")  # announced: a second conversation identical to the first
    c = Convo(server)
    c.history = [{"role": "user", "content": [{"type": "text", "text": "x"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "same"}]}]
    assert text_of(c.user("y", full=True)) == FALLBACK and codes(server) == ["ambiguous_history"]


def test_an_unannounced_create_is_logged_once_the_runner_uses_announcements(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": f"h{i}"} for i in range(3)]})
    server.raw("POST", "/next", b'{"index": 0, "text": "a"}', admin=True)
    Convo(server).user("a")
    assert text_of(Convo(server).user("b")) == "h1"
    assert codes(server) == ["unannounced_message"]


def test_a_correction_and_a_chained_capture_are_both_logged(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "x", "capture": [{"var": "a", "regex": "(a\\d)"}]}], "final": "h0"},
        {"capture": [{"var": "b", "regex": "(\\d+)"}], "steps": [], "final": "{{a}} {{b}}"}]})
    c = Convo(server)
    c.user("go")
    r = c.results(["a1"], then_user="also 7")
    assert text_of(r) == "h0\n\na1 7"
    assert server.app.log.lines[-1]["captured"] == {"a": "a1", "b": "7"}


def test_abandon_marks_the_queued_corrections_skipped(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "x"}, {"do": "bash", "command": "y"}], "final": "h0"},
        {"on_steer": "abandon", "steps": [{"do": "bash", "command": "z"}], "final": "h1"},
        {"steps": [], "final": "h2"},
        {"steps": [], "final": "h3"}]})
    c = Convo(server)
    c.user("go")
    r = c.send([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(c.replies[-1])[0]["id"],
                                              "content": "x"}]},
                {"role": "user", "content": "m1"}, {"role": "user", "content": "m2"}])
    assert calls_of(r)[0]["input"]["command"] == "y"
    r = c.results(["y"])
    assert text_of(r) == "h0" and calls_of(r)[0]["input"]["command"] == "z"
    assert text_of(c.results(["z"], then_user="m3")) == "h3"
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["done", "abandoned", "skipped", "done"]


def test_a_continue_with_tools_null_keeps_the_offered_tools(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "h0"},
                                             {"steps": [{"do": "bash", "command": "ls"}], "final": "h1"}]})
    c = Convo(server)
    c.user("one")
    body = c.body([{"role": "user", "content": "two"}])
    body["tools"] = None
    status, _, events = server.post(body)
    names = [d["content_block"].get("name") for n, d in events if n == "content_block_start"]
    assert status == 200 and names == ["bash"] and codes(server) == []
