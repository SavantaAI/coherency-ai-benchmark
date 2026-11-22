"""T3 Code operations on one harness container: connect, send, read.

The client (t3client.py) is the driver proof's. This adds the calls an
episode needs, each returning what it sent so it can be lined up with the
export later.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from cohbench.env.docker import Container

from . import t3client
from .t3client import Api, T3Error, new_id

T3_BASE_DIR = "/home/node/.t3"


def q(thread_id: str) -> str:
    return urllib.parse.quote(thread_id, safe="")


class T3:
    """One owner session on one harness container's T3 server."""

    def __init__(self, container: Container):
        self.container = container
        self.api: Api | None = None
        self.project_id: str | None = None

    def connect(self, *, timeout_s: float = 120) -> None:
        t3client.wait_ready(self.container, timeout_s=timeout_s)
        self.api = Api(self.container, t3client.owner_token(self.container))
        projects = self.api.get("/api/projects").get("projects", [])
        self.project_id = projects[0]["id"] if projects else None

    def create_project(self, workspace: str) -> str:
        assert self.api is not None
        self.api.rpc("projects.mutate", {"type": "project.create", "commandId": new_id(), "projectId": new_id(), "title": "project", "workspaceRoot": workspace})
        projects = self.api.get("/api/projects")["projects"]
        if not projects:
            raise T3Error("project.create left no project")
        self.project_id = projects[0]["id"]
        return self.project_id

    # ----- sends -----------------------------------------------------------------

    def launch(self, text: str, *, model_selection: dict, title: str, ids: tuple[str, str] | None = None, timeout_s: float = 90) -> dict[str, Any]:
        """Launch a thread with its first message. `ids` (command, message)
        reuses a caller's ids, so a retry after a lost answer is the same
        command and T3 replays it instead of running it twice. Returns the
        accepted ids; the run is looked up separately (`run_for_message`)."""
        assert self.api is not None
        cmd, mid = ids or (new_id(), new_id())
        result = self.api.rpc(
            "orchestration.launchThread",
            {
                "commandId": cmd,
                "creationSource": "web",
                "projectId": self.project_id,
                "title": title,
                "modelSelection": model_selection,
                "runtimeMode": "full-access",
                "interactionMode": "default",
                "workspaceStrategy": {"type": "root"},
                "initialMessage": {"messageId": mid, "text": text, "attachments": []},
            },
            resend_if_lost=True,
            timeout_s=timeout_s,
        )
        return {"thread_id": result["threadId"], "message_id": mid, "command_id": cmd, "projection": result.get("projection")}

    def dispatch_message(self, tid: str, text: str, *, mode: str = "queue", target_run: str | None = None, fresh: bool = False, ids: tuple[str, str] | None = None,
                         timeout_s: float = 90) -> dict[str, Any]:
        """mode: queue (queue_after_active), steer (steer_active) or restart
        (restart_active). Returns the accepted ids; see `launch`."""
        assert self.api is not None
        cmd, mid = ids or (new_id(), new_id())
        dispatch_mode: dict[str, Any] = {"queue": {"type": "queue_after_active"}, "steer": {"type": "steer_active", "targetRunId": target_run}, "restart": {"type": "restart_active", "targetRunId": target_run}}[mode]
        command: dict[str, Any] = {
            "type": "message.dispatch", "commandId": cmd, "createdBy": "user", "creationSource": "web",
            "threadId": tid, "messageId": mid, "text": text, "attachments": [], "dispatchMode": dispatch_mode,
        }
        if fresh:
            command["freshContext"] = True
        self.api.dispatch(command, timeout_s=timeout_s)
        return {"thread_id": tid, "message_id": mid, "command_id": cmd}

    def interrupt(self, tid: str, run_id: str, *, hold_queue: bool = True, cmd: str | None = None, timeout_s: float = 90) -> dict[str, Any]:
        assert self.api is not None
        cmd = cmd or new_id()
        self.api.dispatch({"type": "run.interrupt", "commandId": cmd, "threadId": tid, "runId": run_id, "holdQueue": hold_queue}, timeout_s=timeout_s)
        return {"command_id": cmd, "run_id": run_id}

    def cancel_queued(self, tid: str, run_id: str, *, timeout_s: float = 90) -> dict[str, Any]:
        """`queued-run.cancel`: a run T3 queued and has not started."""
        assert self.api is not None
        cmd = new_id()
        self.api.dispatch({"type": "queued-run.cancel", "commandId": cmd, "threadId": tid, "runId": run_id}, timeout_s=timeout_s)
        return {"command_id": cmd, "run_id": run_id}

    def stop_thread(self, tid: str, *, timeout_s: float = 90) -> dict[str, Any]:
        """`thread.stop`: T3's own stop of a thread, background work owned by a
        terminal run and the thread's delegated tasks included."""
        assert self.api is not None
        cmd = new_id()
        self.api.dispatch({"type": "thread.stop", "commandId": cmd, "threadId": tid}, timeout_s=timeout_s)
        return {"command_id": cmd}

    def respond(self, tid: str, request_id: str, answers: dict[str, Any], *, cmd: str | None = None, timeout_s: float = 90) -> dict[str, Any]:
        assert self.api is not None
        cmd = cmd or new_id()
        self.api.dispatch({"type": "runtime-request.respond", "commandId": cmd, "threadId": tid, "requestId": request_id, "answers": answers}, timeout_s=timeout_s)
        return {"command_id": cmd, "request_id": request_id}

    def answer_secret(self, tid: str, item_id: str, answer: dict[str, Any], *, timeout_s: float = 90) -> dict[str, Any]:
        """`secrets.answerRequest` has no command id, so it is sent once; the
        caller reconciles a lost answer by the item's `secretStatus`."""
        assert self.api is not None
        self.api.rpc("secrets.answerRequest", {"threadId": tid, "turnItemId": item_id, "answer": answer}, timeout_s=timeout_s)
        return {"item_id": item_id}

    def find_message(self, mid: str, *, thread_id: str | None = None, timeout_s: float = 20) -> dict[str, Any] | None:
        """Where T3 recorded user message `mid`: {thread_id, message, run_id}
        or None. Looks in `thread_id` when given, else in every thread the
        shell lists (a launch whose answer was lost). Raises T3Error when a
        read fails, so an absent message is never inferred from a failed read."""
        end = time.monotonic() + timeout_s
        tids = [thread_id] if thread_id else [r["id"] for r in (self.shell(timeout_s=end - time.monotonic()).get("threads") or [])]
        for t in tids:
            proj = self.snapshot(t, timeout_s=end - time.monotonic())["projection"]
            msg = next((m for m in proj.get("messages", []) if m["id"] == mid), None)
            if msg is not None:
                run = msg.get("runId") or next((r["id"] for r in proj.get("runs", []) if r.get("userMessageId") == mid), None)
                return {"thread_id": t, "message": msg, "run_id": run, "projection": proj}
        return None

    def run_for_message(self, tid: str, message_id: str, projection: dict[str, Any] | None = None, timeout_s: float = 20) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            proj = projection or self.snapshot(tid, timeout_s=deadline - time.monotonic())["projection"]
            msg = next((m for m in proj.get("messages", []) if m["id"] == message_id), None)
            if msg and msg.get("runId"):
                return msg["runId"]
            run = next((r for r in proj.get("runs", []) if r.get("userMessageId") == message_id), None)
            if run:
                return run["id"]
            if time.monotonic() + 0.4 >= deadline:
                raise t3client.T3Timeout(f"no run found for message {message_id} within {timeout_s:.1f} s")
            projection = None
            time.sleep(0.4)

    # ----- reads -------------------------------------------------------------------

    def snapshot(self, tid: str, *, timeout_s: float = 120) -> dict[str, Any]:
        assert self.api is not None
        return self.api.get(f"/api/orchestration/threads/{q(tid)}", timeout_s=timeout_s)

    def snapshot_raw(self, tid: str) -> t3client.HttpResponse:
        assert self.api is not None
        return self.api.get_raw(f"/api/orchestration/threads/{q(tid)}")

    def shell(self, *, timeout_s: float = 120) -> dict[str, Any]:
        assert self.api is not None
        return self.api.get("/api/orchestration/shell", timeout_s=timeout_s)

    def turn_item(self, tid: str, item_id: str, *, timeout_s: float = 90) -> dict[str, Any] | None:
        assert self.api is not None
        return self.api.rpc("orchestration.getTurnItem", {"threadId": tid, "itemId": item_id}, timeout_s=timeout_s)["item"]

    def tree(self, tid: str, *, timeout_s: float = 120) -> dict[str, dict[str, Any] | Exception]:
        """The thread and every subagent child, recursively: projection, or the
        exception its read raised."""
        deadline = time.monotonic() + timeout_s
        out: dict[str, dict[str, Any] | Exception] = {}
        todo = [tid]
        while todo:
            t = todo.pop(0)
            if t in out:
                continue
            try:
                proj = self.snapshot(t, timeout_s=deadline - time.monotonic())["projection"]
            except Exception as exc:
                out[t] = exc
                continue
            out[t] = proj
            todo += [s["childThreadId"] for s in proj.get("subagents", []) if s.get("childThreadId")]
        return out
