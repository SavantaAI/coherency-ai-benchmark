"""The ninth review round's minors: a resent completion-only create, and an
edited export whose last tool result lacks what conditions read."""

from __future__ import annotations

import pytest

from cohbench.scripted import Episode

from .conftest import Convo
from .test_round8 import TASK_ID, TOOLS, completion, completion_script


def test_an_identical_completion_only_create_is_a_retry(server):
    server.load(completion_script())
    first = Convo(server, tools=TOOLS)
    a = first.user(completion(TASK_ID))
    again = Convo(server, tools=TOOLS)
    b = again.user(completion(TASK_ID))
    line = server.app.log.lines[-1]
    assert b["id"] == a["id"] and line["decision"] == "replay"
    assert len([c for c in server.episode.state()["conversations"].values() if c["kind"] == "main"]) == 1


@pytest.mark.parametrize("result", [{"exit": 0}, {"text": "x", "is_error": "no", "exit": None},
                                    {"text": "x", "is_error": False, "exit": "0"}])
def test_import_checks_the_last_tool_result(server, result):
    server.load({"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "x"}, {"do": "bash", "command": "y"}],
                                             "final": "done"}]})
    c = Convo(server, tools=TOOLS)
    c.user("go")
    c.results(["first"])
    doc = server.episode.export_state()
    states = [n["state"] for n in doc["nodes"] if n["state"]["last_result"] is not None]
    assert states
    states[-1]["last_result"] = result
    with pytest.raises(ValueError):
        Episode().import_state(doc)


def test_a_completion_reply_that_cannot_render_is_the_owning_scripts_fallback(server):
    script = completion_script()
    script["fallback"] = "Cannot go on."
    # `later` is only captured by a step that has not run when the prompt arrives.
    script["handlers"][1]["steps"] = [{"do": "bash", "command": "x", "capture": [{"var": "later", "regex": "(x)"}]}]
    script["on_child_completion"] = {"text": "Noted {{later}}."}
    server.load(script)
    c = Convo(server, tools=TOOLS)
    r = c.user(completion(TASK_ID))
    line = server.app.log.lines[-1]
    assert r["content"][0]["text"] == "Cannot go on."
    assert line["decision"] == "fallback" and line["delegation_completion"]
    assert [e["code"] for e in server.episode.state()["errors"]] == ["missing_variable"]


def test_the_schema_file_lists_on_child_completion():
    import json
    from pathlib import Path

    import cohbench.scripted as pkg
    schema = json.loads((Path(pkg.__file__).parent / "schema.json").read_text())
    oc = schema["properties"]["on_child_completion"]
    assert oc["required"] == ["text"] and oc["additionalProperties"] is False
    assert set(oc["properties"]) == {"text", "capture"}
