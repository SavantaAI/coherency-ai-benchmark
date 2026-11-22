"""The policy: which handler and step answers each request."""

from __future__ import annotations

import json
import threading
import time

from .conftest import PI_TOOLS, Convo, calls_of, fixture_pairs, text_of

FALLBACK = "Sorry, I can't continue with this right now."


def three_handlers():
    return {"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "echo one"}], "final": "first done"},
        {"steps": [], "final": "second"},
        {"steps": [], "final": "third"},
    ]}


def codes(server):
    return [e["code"] for e in server.episode.state()["errors"]]


def test_handlers_follow_user_messages_across_a_restarted_conversation(server):
    server.load(three_handlers())
    a = Convo(server)
    r = a.user("start")
    assert calls_of(r)[0]["input"] == {"command": "echo one", "timeout": None}
    assert text_of(a.results(["one\n"])) == "first done"
    assert text_of(a.user("next")) == "second"
    # The container restarts: a new session opens with only the new message.
    b = Convo(server)
    assert text_of(b.user("and again")) == "third"
    st = server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0, 1, 2]
    assert [u["conversation"] for u in st["user_messages"]["main"]] == ["c1", "c1", "c2"]
    assert codes(server) == []


def test_resent_history_is_not_counted_twice(server):
    server.load(three_handlers())
    a = Convo(server)
    a.user("start")
    a.results(["one\n"])
    # Pi restarted with --continue: thread create carrying the whole history.
    b = Convo(server)
    b.history = list(a.history)
    r = b.user("next", full=True)
    assert text_of(r) == "second"
    log = server.app.log.lines[-1]
    assert log["match"] == "history" and log["base"] == a.last_id
    assert [u["index"] for u in log["user_messages"]] == [1]
    # The same again without threads at all (a client that resends everything).
    c = Convo(server, threads=False)
    c.history = list(b.history)
    assert text_of(c.user("third one")) == "third"
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0, 1, 2]


def test_a_retried_request_gets_the_same_reply(server):
    server.load(three_handlers())
    a = Convo(server)
    first = a.user("start")
    body = a.body([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(first)[0]["id"],
                                                 "content": "one\n", "is_error": False}]}])
    s1, _, e1 = server.post(body)
    s2, _, e2 = server.post(body)  # Pi's retry after, say, a dropped connection
    assert s1 == s2 == 200 and e1[0][1]["message"]["id"] == e2[0][1]["message"]["id"]
    assert server.app.log.lines[-1]["decision"] == "replay"
    assert len(server.episode.state()["user_messages"]["main"]) == 1
    # Once continued, the reply is no longer replayed for the same request.
    a.last_id = e1[0][1]["message"]["id"]
    a.history += [body["messages"][0], {"role": "assistant", "content": [{"type": "text", "text": "first done"}]}]
    a.replies.append({"content": [{"type": "text", "text": "first done"}]})
    assert text_of(a.user("next")) == "second"


def test_an_identical_create_without_an_announcement_is_a_retry(server):
    """A new conversation that opens exactly like an earlier one cannot be
    told from a retry by content. Without the runner's word it is a retry."""
    server.load(three_handlers())
    first = Convo(server).user("same words")
    again = Convo(server).user("same words")
    assert again["id"] == first["id"] and server.app.log.lines[-1]["decision"] == "replay"
    assert len(server.episode.state()["user_messages"]["main"]) == 1


def test_an_announcement_makes_an_identical_create_the_next_message(server):
    """The controlled-reset case: a fresh container opens with the same
    message as an earlier single-turn request. The runner announces message 1
    first, so the identical create is message 1, not a retry of message 0."""
    server.load({"version": 1, "handlers": [{"steps": [], "final": "zero"}, {"steps": [], "final": "one"}]})
    assert text_of(Convo(server).user("Continue the task.")) == "zero"
    status, _, body = server.raw("POST", "/next", json.dumps({"index": 1, "text": "Continue the task."}).encode(), admin=True)
    assert status == 200, body
    assert text_of(Convo(server).user("Continue the task.")) == "one"
    log = server.app.log.lines[-1]
    assert log["identity"]["rule"] == "d" and "announcement of user message 1" in log["identity"]["reason"]
    st = server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0, 1]
    assert st["announcements"][0]["outcome"] == "arrived" and st["errors"] == []
    # With no announcement pending, the same create now matches two earlier
    # creates: which one it repeats cannot be decided, so it is an error.
    again = Convo(server).user("Continue the task.")
    assert text_of(again) == FALLBACK and codes(server) == ["ambiguous_retry"]


def test_an_announcement_is_checked(server):
    server.load(three_handlers())
    for body in ({"index": 3}, {"index": -1}, {"index": "0"}, {"index": 0, "text": 5}, {"text": "x"}, [0]):
        status, _, data = server.raw("POST", "/next", json.dumps(body).encode(), admin=True)
        assert status == 400, (body, data)
    server.raw("POST", "/next", b'{"index": 0, "text": "hello"}', admin=True)
    assert text_of(Convo(server).user("something else")) == FALLBACK
    assert codes(server) == ["unannounced_message"]
    assert server.episode.state()["announcements"][0]["outcome"] == "pending"
    assert calls_of(Convo(server).user("hello"))[0]["input"]["command"] == "echo one"


def test_the_two_saved_pending_creates_get_two_handlers(server):
    """Both open with the same user text; the second offers one more tool
    (`ask_user`). Different requests, so the second is handler 1."""
    (first, _), (second, _) = fixture_pairs("pending")
    assert first["messages"] == second["messages"] and first["tools"] != second["tools"]
    server.load({"version": 1, "handlers": [{"steps": [], "final": "zero"}, {"steps": [], "final": "one"}]})
    texts = []
    for body in (first, second):
        status, _, events = server.post(body)
        assert status == 200
        texts.append("".join(d["delta"].get("text", "") for n, d in events if n == "content_block_delta"))
    assert texts == ["zero", "one"]


def test_a_model_change_is_not_a_retry(server):
    server.load(three_handlers())
    c = Convo(server)
    a = c.user("start")
    b = Convo(server, model="claude-sonnet-5-5").user("start")
    assert b["id"] != a["id"] and b["model"] == "claude-sonnet-5-5" and text_of(b) == "second"


def run_sequence(server, pause: float) -> list:
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "echo one"}], "final": "first"},
        {"steps": [], "final": "second"}, {"steps": [], "final": "third"}]})
    out = []
    a = Convo(server)
    for step in ("u:start", "r:one", "u:start-again", "fresh:start", "fresh:start"):
        time.sleep(pause)
        kind, text = step.split(":")
        if kind == "u":
            msg = a.user(text)
        elif kind == "r":
            msg = a.results([text])
        else:
            msg = Convo(server).user(text)
        line = server.app.log.lines[-1]
        out.append((line["decision"], line.get("handler"), line.get("step"), text_of(msg),
                    [c["name"] for c in calls_of(msg)]))
    return out


def test_identical_inputs_give_identical_decisions_whatever_the_delays(server):
    fast = run_sequence(server, 0.0)
    slow = run_sequence(server, 0.3)
    assert fast == slow
    # The fourth and fifth are creates identical to the first: retries.
    assert [d[0] for d in fast] == ["tool_use", "final", "final", "replay", "replay"]


def test_two_user_messages_in_one_request_stay_two(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "zero"}, {"steps": [], "final": "one"}]})
    c = Convo(server)
    r = c.send([{"role": "user", "content": "one"}, {"role": "user", "content": "two"}])
    assert [u["index"] for u in server.episode.state()["user_messages"]["main"]] == [0, 1]
    assert text_of(r) == "zero\n\none", "the first handler's final goes ahead as narration"
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["done", "done"]


def test_abandon_drops_a_required_capture_and_keeps_the_steer(server):
    server.load({"version": 1, "handlers": [
        {"on_steer": "abandon", "steps": [
            {"do": "bash", "command": "./submit", "capture": [{"var": "job", "regex": "J-\\d+", "required": True}]},
            {"do": "bash", "command": "./status {{job}}"}], "final": "x"},
        {"steps": [], "final": "steered"},
        {"steps": [], "final": "after"},
    ]})
    c = Convo(server)
    c.user("submit")
    r = c.results(["no id printed"], then_user="Stop, do something else.")
    assert text_of(r) == "steered" and codes(server) == []
    assert text_of(c.user("and then")) == "after"
    st = server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0, 1, 2]
    assert [h["status"] for h in st["handlers"]["main"]] == ["abandoned", "done", "done"]


def test_a_failed_steer_handler_does_not_overwrite_the_abandoned_one(server):
    server.load({"version": 1, "handlers": [
        {"on_steer": "abandon", "steps": [{"do": "bash", "command": "a"}, {"do": "bash", "command": "b"}], "final": "x"},
        {"expect": {"contains": "deploy"}, "steps": [], "final": "deploying"},
        {"steps": [], "final": "next"},
    ]})
    c = Convo(server)
    c.user("go")
    assert text_of(c.results(["a"], then_user="what time is it?")) == FALLBACK
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["abandoned", "error"]
    assert text_of(c.user("and now")) == "next", "later handlers did not shift"


def test_an_optional_group_that_did_not_take_part_is_a_miss(server):
    server.load({"version": 1, "vars": {"job": "OLD"}, "handlers": [
        {"steps": [{"do": "bash", "command": "x", "capture": [{"var": "job", "regex": "x(y)?"}]}], "final": "job={{job}}"},
        {"steps": [{"do": "bash", "command": "x", "capture": [{"var": "job", "regex": "x(y)?", "required": True}]}],
         "final": "job={{job}}"},
    ]})
    c = Convo(server)
    c.user("one")
    assert text_of(c.results(["x"])) == "job=OLD"
    assert server.app.log.lines[-1]["captures_missed"] == ["job"]
    c.user("two")
    assert text_of(c.results(["x"])) == FALLBACK and codes(server) == ["capture_failed"]


def test_branches_on_exit_contains_and_regex(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "bash", "command": "make build"},
        {"do": "branch", "cases": [
            {"when": {"exit": 0}, "steps": [{"do": "reply", "text": "built"}]},
            {"when": {"all": [{"exit": 2}, {"regex": "^error: (\\w+)"}]}, "steps": [
                {"do": "bash", "command": "make clean"},
                {"do": "branch", "cases": [{"when": {"contains": "cleaned"}, "steps": [{"do": "say", "text": "Cleaned."}]}]},
            ]},
        ], "else": [{"do": "reply", "text": "unexpected failure"}]},
        {"do": "bash", "command": "make build"},
    ], "final": "built after clean"}]})
    # Exit 0 path.
    a = Convo(server)
    a.user("build it")
    assert text_of(a.results(["ok\n"])) == "built"
    # Exit 2 with a matching message: clean, then build again.
    server.episode.reset()
    b = Convo(server)
    b.user("build it")
    r = b.results([("error: missing\n\n\nCommand exited with code 2", True)])
    assert calls_of(r)[0]["input"]["command"] == "make clean"
    r = b.results(["cleaned\n"])
    assert text_of(r) == "Cleaned." and calls_of(r)[0]["input"]["command"] == "make build"
    assert text_of(b.results(["ok\n"])) == "built after clean"
    # Any other failure (a timeout has no exit code).
    server.episode.reset()
    c = Convo(server)
    c.user("build it")
    assert text_of(c.results([("Command timed out after 5 seconds", True)])) == "unexpected failure"
    assert codes(server) == []


def test_captures_feed_later_commands_and_later_handlers(server):
    server.load({"version": 1, "handlers": [
        {"steps": [{"do": "bash", "command": "./tool submit",
                    "capture": [{"var": "job", "regex": "job id: (?P<id>J-\\d+)", "group": "id", "required": True},
                                {"var": "eta", "regex": "eta (\\d+)m"}]},
                   {"do": "bash", "command": "./tool status {{job}}"}],
         "final": "Job {{job}} is {{last_output}}. Start it now?", "end": "question"},
        {"steps": [{"do": "bash", "command": "./tool start {{job}}"}], "final": "Started {{job}}."},
    ]})
    a = Convo(server)
    a.user("submit it")
    r = a.results(["submitted, job id: J-4417\n"])
    assert calls_of(r)[0]["input"]["command"] == "./tool status J-4417"
    assert text_of(a.results(["queued"])) == "Job J-4417 is queued. Start it now?"
    log = server.app.log.lines[-1]
    assert log["ends_with"] == "question"
    # A later handler, in a restarted conversation, still has the value.
    b = Convo(server)
    assert calls_of(b.user("yes"))[0]["input"]["command"] == "./tool start J-4417"
    st = server.episode.state()
    assert st["vars"] == {"job": "J-4417"} and st["handlers"]["main"][0]["ended_with"] == "question"
    assert server.app.log.lines[1]["captures_missed"] == ["eta"]


def test_required_capture_that_misses_gives_the_fallback(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "bash", "command": "./tool submit", "capture": [{"var": "job", "regex": "J-\\d+", "required": True}]}],
        "final": "Job {{job}}"}]})
    a = Convo(server)
    a.user("go")
    assert text_of(a.results(["nothing useful"])) == FALLBACK
    assert codes(server) == ["capture_failed"]


def steer_script(on_steer: str) -> dict:
    return {"version": 1, "handlers": [
        {"on_steer": on_steer, "steps": [{"do": "bash", "command": f"sleep 1; echo step{i}", "delay": 0.05}
                                         for i in range(1, 5)],
         "final": "all four steps ran"},
        {"steps": [{"do": "write", "path": "stopped.txt", "content": "halted"}], "final": "STEERED"},
    ]}


def test_steer_abandons_the_remaining_steps(server):
    server.load(steer_script("abandon"))
    a = Convo(server)
    a.user("run four steps")
    a.results(["step1\n"])
    # The steer arrives with the result of step 2, as Pi sends it.
    r = a.results(["step2\n"], then_user="Change of plan: stop and write stopped.txt.")
    assert calls_of(r)[0]["name"] == "write"
    assert text_of(a.results(["Successfully wrote to stopped.txt"])) == "STEERED"
    st = server.episode.state()
    assert [h["status"] for h in st["handlers"]["main"]] == ["abandoned", "done"]
    assert server.app.log.lines[-2]["steer"] is True


def test_steer_with_finish_runs_the_rest_first(server):
    server.load(steer_script("finish"))
    a = Convo(server)
    a.user("run four steps")
    r = a.results(["step1\n"], then_user="Also write stopped.txt afterwards.")
    assert calls_of(r)[0]["input"]["command"] == "sleep 1; echo step2"
    r = a.results(["step2\n"])
    r = a.results(["step3\n"])
    r = a.results(["step4\n"])
    # The first handler's final text goes ahead of the steer handler's first call.
    assert text_of(r) == "all four steps ran" and calls_of(r)[0]["name"] == "write"
    assert text_of(a.results(["Successfully wrote to stopped.txt"])) == "STEERED"
    assert [h["status"] for h in server.episode.state()["handlers"]["main"]] == ["done", "done"]


def test_delay_holds_the_reply(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "true", "delay": 0.4}],
                                             "final": "ok", "final_delay": 0.2}]})
    a = Convo(server)
    t0 = time.monotonic()
    a.user("go")
    t1 = time.monotonic()
    a.results(["(no output)"])
    t2 = time.monotonic()
    assert t1 - t0 >= 0.4 and t2 - t1 >= 0.2


def test_exhausted_script_gives_the_neutral_fallback(server):
    server.load({"version": 1, "handlers": [{"steps": [], "final": "only one"}]})
    a = Convo(server)
    assert text_of(a.user("one")) == "only one"
    r = a.user("two")
    assert text_of(r) == FALLBACK and r["stop_reason"] == "end_turn"
    assert codes(server) == ["script_exhausted"]
    assert server.app.log.lines[-1]["decision"] == "fallback"
    # The conversation can still go on after a fallback.
    assert text_of(a.user("three")) == FALLBACK


def test_no_script_loaded(server):
    status, _, events = server.post(Convo(server).body([{"role": "user", "content": "hi"}]))
    assert status == 200 and codes(server) == ["no_script"]


def test_a_tool_the_request_did_not_offer_fails_loudly(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "tool", "name": "mcp__t3-code__send_to_user",
                                                         "input": {"message": "hello"}}], "final": "x"}]})
    a = Convo(server)
    r = a.user("send it")
    assert text_of(r) == FALLBACK and calls_of(r) == []
    errs = server.episode.state()["errors"]
    assert errs[0]["code"] == "tool_not_offered" and "mcp__t3-code__send_to_user" in errs[0]["detail"]


def test_tool_search_then_an_added_tool_is_offered(server):
    """send_to_user is deferred in Pi's list; it becomes callable only after
    tool_search adds it with a system-role tool_addition (as in the driver
    proof's messaging run)."""
    pairs = fixture_pairs("messaging")
    addition = json.loads(json.dumps(pairs[3][0]["messages"]))  # tool_result + system tool_additions
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "tool", "name": "tool_search", "input": {"query": "send_to_user", "limit": 5}},
        {"do": "tool", "name": "mcp__t3-code__send_to_user", "input": {"message": "Build finished at step 3."}},
    ], "final": "done"}]})
    a = Convo(server)
    r = a.user("send it")
    addition[0]["content"][0]["tool_use_id"] = calls_of(r)[0]["id"]
    r = a.send(addition)
    assert calls_of(r)[0]["name"] == "mcp__t3-code__send_to_user"
    assert text_of(a.results(['{"sentMessage":"Build finished at step 3."}'])) == "done"
    assert codes(server) == []


def test_tool_arguments_are_checked_against_the_offered_schema(server):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "tool", "name": "bash", "input": {"cmd": "ls"}}], "final": "x"}]})
    r = Convo(server).user("go")
    assert text_of(r) == FALLBACK and codes(server) == ["tool_arguments"]


def test_parallel_calls_and_their_results(server):
    server.load({"version": 1, "handlers": [{"steps": [
        {"do": "parallel", "say": "Writing both.", "calls": [
            {"do": "write", "path": "a.txt", "content": "A"},
            {"do": "bash", "command": "cat a.txt", "capture": [{"var": "got", "regex": "(\\w+)"}]}]}],
        "final": "Both done, read {{got}}, last {{last_output}}"}]})
    a = Convo(server)
    r = a.user("go")
    assert [c["name"] for c in calls_of(r)] == ["write", "bash"] and text_of(r) == "Writing both."
    assert text_of(a.results(["Successfully wrote to a.txt", "A"])) == "Both done, read A, last A"
    # Only one of the two results: the API's own rule refuses it.
    server.episode.reset()
    b = Convo(server)
    r = b.user("go")
    body = b.body([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(r)[0]["id"], "content": "x"}]}])
    status, _, err = server.post(body)
    assert status == 400 and "without `tool_result` blocks" in err["error"]["message"]


def test_expect_guards_against_a_misaligned_script(server):
    server.load({"version": 1, "handlers": [{"expect": {"contains": "deploy"}, "steps": [], "final": "deploying"}]})
    assert text_of(Convo(server).user("what time is it?")) == FALLBACK
    assert codes(server) == ["expect_mismatch"]


def subagent_script():
    task = "In the project at /work/project, count the lines of README.md. Reply with only the number."
    return task, {"version": 1, "handlers": [{"steps": [
        {"do": "tool", "name": "mcp__t3-code__orchestrator_capabilities", "input": {},
         "say": "I'll delegate this to a subagent as requested."},
        {"do": "delegate", "child": "counter", "task": task,
         "input": {"mode": "wait", "role": "research", "title": "Count README.md lines"},
         "capture": [{"var": "child_said", "regex": "\"summary\":\"(\\d+)"}]},
    ], "final": "{{child_said}}"}],
        "children": {"counter": {"handlers": [{"steps": [
            {"do": "bash", "command": "cd /work/project && wc -l README.md",
             "capture": [{"var": "lines", "regex": "^(\\d+)"}]}], "final": "{{lines}}"}]}}}


def test_subagent_conversation_is_served_from_its_own_handlers(server):
    """Shapes from the driver proof's export run: the parent calls
    delegate_task, T3 starts the child as a new Pi session whose first user
    message is T3's note plus the task, and the parent gets the result when
    the child is done (mode wait)."""
    pairs = fixture_pairs("subagent")
    task, script = subagent_script()
    server.load(script)
    parent = Convo(server)
    r = parent.user(pairs[0][0]["messages"][0]["content"][0]["text"])
    assert calls_of(r)[0]["name"] == "mcp__t3-code__orchestrator_capabilities"
    caps = pairs[1][0]["messages"][0]["content"][0]["content"]
    r = parent.results([caps])
    call = calls_of(r)[0]
    assert call["name"] == "mcp__t3-code__delegate_task"
    assert call["input"] == {"task": task, "mode": "wait", "role": "research", "title": "Count README.md lines"}
    # The child, as T3 starts it (text copied from the real child request).
    real_child_text = pairs[2][0]["messages"][0]["content"][0]["text"]
    note = real_child_text.split("\n\n")[0]
    child = Convo(server)
    cr = child.user(f"{note}\n\nAct as the research sub-agent for this task.\n\n{task}")
    assert calls_of(cr)[0]["input"]["command"] == "cd /work/project && wc -l README.md"
    assert text_of(child.results(["1 README.md\n"])) == "1"
    done = parent.results(['{"status":"completed","workState":"result_available","summary":"1"}'])
    assert text_of(done) == "1"
    st = server.episode.state()
    assert [u["index"] for u in st["user_messages"]["main"]] == [0], "the child's message is not a main user message"
    assert [u["index"] for u in st["user_messages"]["child:counter/1"]] == [0]
    assert st["conversations"]["c2"]["kind"] == "child" and st["conversations"]["c2"]["parent"] == "c1"
    assert st["delegations"][0]["conversation"] == "c2"
    assert codes(server) == []


def test_two_conversations_at_once(server):
    """A parent waits inside a long reply while its child is served."""
    task, script = subagent_script()
    script["handlers"][0]["steps"][1]["delay"] = 1.0
    server.load(script)
    parent = Convo(server)
    parent.user("count it")
    caps_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls_of(parent.replies[-1])[0]["id"],
                                                "content": "{}", "is_error": False}]}
    out: dict = {}

    def slow():
        out["parent"] = parent.send([caps_result])

    t = threading.Thread(target=slow)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.2)
    # Meanwhile an unrelated main conversation is answered without waiting.
    other = Convo(server)
    t1 = time.monotonic()
    other_reply = other.user("unrelated")
    assert time.monotonic() - t1 < 0.5, "the delayed reply does not hold the other conversation"
    t.join()
    assert time.monotonic() - t0 >= 1.0
    assert calls_of(out["parent"])[0]["name"] == "mcp__t3-code__delegate_task"
    assert text_of(other_reply) == FALLBACK  # the script has one main handler
    convs = server.episode.state()["conversations"]
    assert set(convs) == {"c1", "c2"}


NOTE = '[T3 Code: the agent in T3 thread "thread:project:p:1" sent this message, not the user.]'


def delegating_script(task="check A", extra_calls=()):
    return {"version": 1, "handlers": [
        {"steps": [{"do": "parallel", "calls": [
            {"do": "delegate", "child": "c", "task": task, "input": {"mode": "wait", "role": "research"}},
            *extra_calls]}], "final": "done"},
        {"steps": [], "final": "main one"},
        {"steps": [], "final": "main two"},
    ], "children": {"c": {"handlers": [{"steps": [], "final": "child zero"}]}}}


def test_a_child_needs_t3s_launch_envelope(server):
    server.load(delegating_script())
    Convo(server).user("delegate it")
    # The task text alone, or inside another message, is not a child launch.
    assert text_of(Convo(server).user("Now check A directly")) == "main one"
    assert text_of(Convo(server).user(f"{NOTE}\n\ncheck A")) == "main two", "role line missing"
    assert codes(server) == ["unmatched_child"]
    child = Convo(server).user(f"{NOTE}\n\nAct as the research sub-agent for this task.\n\ncheck A")
    assert text_of(child) == "child zero"
    assert server.episode.state()["delegations"][0]["conversation"] == "c4"


def test_general_role_has_no_role_line(server):
    script = delegating_script()
    script["handlers"][0]["steps"][0]["calls"][0]["input"]["role"] = "general"
    server.load(script)
    Convo(server).user("delegate it")
    assert text_of(Convo(server).user(f"{NOTE}\n\ncheck A")) == "child zero"


def test_a_failed_delegation_is_retired(server):
    server.load(delegating_script())
    parent = Convo(server)
    parent.user("delegate it")
    parent.results([("Delegation failed: provider unavailable", True)])
    assert server.episode.state()["delegations"][0]["status"] == "failed"
    launch = f"{NOTE}\n\nAct as the research sub-agent for this task.\n\ncheck A"
    assert text_of(Convo(server).user(launch)) == "main one"


def test_a_delegation_is_recorded_only_with_all_its_calls_built(server):
    server.load(delegating_script(extra_calls=[{"do": "tool", "name": "not_a_tool", "input": {}}]))
    r = Convo(server).user("delegate it")
    assert text_of(r) == FALLBACK and codes(server) == ["tool_not_offered"]
    assert server.episode.state()["delegations"] == []


def test_a_delegation_whose_reply_was_not_sent_does_not_match(server):
    from cohbench.scripted.wire import parse_request
    server.load(delegating_script())
    # Decided in process, so no attempt has been reported yet; then one fails.
    out = server.episode.handle(parse_request(Convo(server).body([{"role": "user", "content": "delegate it"}])))
    server.episode.reply_sent(out.msg_id, False)
    assert server.episode.state()["delegations"][0]["status"] == "undelivered"
    launch = f"{NOTE}\n\nAct as the research sub-agent for this task.\n\ncheck A"
    assert text_of(Convo(server).user(launch)) == "main one"
