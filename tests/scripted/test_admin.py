"""Admin listener, request log and state file."""

from __future__ import annotations

import json

from .conftest import Convo, text_of

SCRIPT = {"version": 1, "handlers": [{"steps": [{"do": "bash", "command": "echo hi"}], "final": "said {{last_output}}"}]}


def test_admin_load_state_reset(server):
    status, _, body = server.raw("POST", "/script", json.dumps(SCRIPT).encode(), admin=True)
    assert status == 200 and json.loads(body)["ok"] is True
    a = Convo(server)
    a.user("go")
    assert text_of(a.results(["hi"])) == "said hi"
    status, _, body = server.raw("GET", "/state", None, admin=True)
    st = json.loads(body)
    assert st["requests"] == 2 and st["handlers"]["main"][0]["status"] == "done"
    status, _, body = server.raw("GET", "/script", None, admin=True)
    assert json.loads(body) == SCRIPT
    status, _, _ = server.raw("POST", "/reset", b"", admin=True)
    assert status == 200 and server.episode.state()["requests"] == 0
    assert server.episode.script is not None, "reset keeps the script"


def test_admin_refuses_a_bad_script_and_keeps_the_old_one(server):
    server.raw("POST", "/script", json.dumps(SCRIPT).encode(), admin=True)
    status, _, body = server.raw("POST", "/script", b'{"version": 1, "handlers": []}', admin=True)
    assert status == 400
    assert json.loads(body)["errors"] == ["$.handlers: expected a non-empty list of handlers"]
    assert server.episode.script.raw == SCRIPT


def test_admin_paths_do_not_exist_on_the_api_listener(server):
    for method, path in (("GET", "/state"), ("GET", "/script"), ("POST", "/script"), ("POST", "/reset"),
                         ("POST", "/next"), ("GET", "/export"), ("POST", "/import"), ("GET", "/healthz")):
        status, _, body = server.raw(method, path, b"{}" if method == "POST" else None)
        assert status == 404 and json.loads(body)["error"]["type"] == "not_found_error", path


def test_request_log_and_state_file(server, tmp_path):
    server.load(SCRIPT)
    a = Convo(server)
    a.user("go")
    a.results(["hi"])
    log_dir = tmp_path / "log"
    lines = [json.loads(x) for x in (log_dir / "requests.jsonl").read_text().splitlines()]
    assert [x["seq"] for x in lines] == [1, 2]
    first, second = lines
    for key in ("time", "request_id", "conversation", "handler", "step", "decision", "message_id", "status"):
        assert key in first, key
    assert first["decision"] == "tool_use" and first["step"] == "$.handlers[0].steps[0]"
    assert second["decision"] == "final" and second["match"] == "thread"
    assert "bash" in first["tools_offered"] and first["system_chars"] > 1000
    assert (log_dir / f"system-{first['system_sha256'][:16]}.txt").read_text().startswith("You are a coding assistant")
    st = json.loads((log_dir / "state.json").read_text())
    assert st["requests"] == 2 and st["user_messages"]["main"][0]["index"] == 0
