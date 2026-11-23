"""The normalised trajectory format, version 1, and the scoring bundle's
view schema.

A scorer reads the scoring bundle and nothing else (`bundle.py`):
`scorer.jsonl` (the scorer view, defined here), `identifiers.json`
(`registry.py`), `changes.jsonl` (`changelog.py`), `observations.jsonl`
(`observations.py`) and `episode.json` (`episode.py`), with file contents
from the watcher's content store. The full normalised trajectory
(`trajectory.jsonl`, defined here) and the run manifest are audit records,
not scorer inputs.

Every event and header has a closed key set: a field not declared here cannot
appear (the validator rejects it). That is the mechanism that keeps native
data out: an adapter has nowhere to put it.

`cohbench/trajectory/README.md` is the prose specification; this module is the
machine-readable one. The two must agree.
"""

from __future__ import annotations

import json
from typing import Any

FORMAT = "cohbench-trajectory"
VIEW_FORMAT = "cohbench-scorer-view"
CHANGELOG_FORMAT = "cohbench-changelog"
IDENTIFIERS_FORMAT = "cohbench-identifiers"
OBSERVATIONS_FORMAT = "cohbench-observations"
EPISODE_FORMAT = "cohbench-episode"
VERSION = 1
MANIFEST_FORMAT = "cohbench-run-manifest"

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

# "reset": a controlled reset the runner declared. "context": any other new
# context window (a fresh-context send, a new container) the runner did not
# declare as a controlled reset.
SEGMENT_CAUSES = ("start", "reset", "context")
TURN_ENDS = ("finished", "waiting", "interrupted", "failed", "open")
# `post_run`: the runner's awareness question after the episode end. It and
# everything after it stay in the full trajectory and never reach a scorer input.
# `reset_notice` (D-046): a reset's notice steered into the running turn. It is
# never a correction and never a reply to an output; a notice sent with the
# next message keeps that message's kind.
USER_KINDS = ("opening_prompt", "reply", "correction", "reset", "reopen", "from_agent", "post_run", "reset_notice")
# User message kinds whose text the user wrote (identifiers in them are known to the user).
USER_AUTHORED_KINDS = ("opening_prompt", "reply", "correction", "reset", "reopen", "reset_notice")
# The kinds dated at their delivery to the running turn (R21).
DELIVERED_KINDS = ("correction", "reset_notice")
ASSISTANT_ROLES = ("final", "narration")
CALL_STATUSES = ("ok", "error", "cancelled", "open")
END_STATUSES = ("ok", "error", "cancelled")
QUESTION_KINDS = ("question", "secret")
ANSWER_STATUSES = ("answered", "saved", "declined", "cancelled", "open")
ANSWER_KINDS = ("reply", "correction", "reopen")

TOOL_CLASSES = ("shell", "file_read", "file_write", "file_edit", "search", "subagent", "web", "message", "question", "other")

# Neutral tool name -> class. Harness tools that exist in more than one
# harness get a shared name; a harness tool with no counterpart is
# "harness_other". Delegation through the driver and through a harness's own
# subagent tool are both "subagent". Other driver tools keep T3 Code's
# unprefixed name, which is the same for every harness. Whether a call went to
# the harness or the driver is recorded in the manifest only.
TOOLS: dict[str, str] = {
    "shell": "shell",
    "file_read": "file_read",
    "file_write": "file_write",
    "file_edit": "file_edit",
    "search": "search",
    "web_fetch": "web",
    "web_search": "web",
    "subagent": "subagent",
    "ask_user": "question",
    "tool_discovery": "other",
    "harness_other": "other",
    "send_to_user": "message",
    "request_secret": "question",
}

# T3 Code's tools as listed in the Claude Code request bodies and Pi's session
# of the driver proof (run 2026-11-21T210951-74da66), unprefixed, other than
# send_to_user, request_secret and delegate_task.
DRIVER_OTHER_TOOLS = (
    "comment_on_issue", "create_issue", "create_threads", "delete_scheduled_task", "device_close", "device_list",
    "device_open", "device_screenshot", "html_preview", "html_render", "link_issue", "link_pull_request",
    "link_pull_requests", "list_issues", "list_scheduled_tasks", "list_thread_issues", "list_thread_pull_requests",
    "orchestrator_capabilities", "preview_act", "preview_dialog", "preview_evaluate", "preview_navigate", "preview_open",
    "preview_recording_start", "preview_recording_stop", "preview_resize", "preview_set_appearance", "preview_snapshot",
    "preview_status", "preview_wait_for", "read_issue", "read_pull_request_guide", "run_scheduled_task_now",
    "schedule_task", "set_issue_state", "t3_attachment_discard", "t3_attachment_prepare_upload", "t3_background_wait",
    "t3_environment_preferences_update", "t3_environment_read", "t3_pending_request", "t3_preview_close",
    "t3_preview_list", "t3_project_clone", "t3_project_create", "t3_project_delete", "t3_project_list",
    "t3_project_read", "t3_project_update", "t3_queue", "t3_submit_output", "t3_theme_read", "t3_theme_set",
    "t3_thread_configure", "t3_thread_interrupt", "t3_thread_launch", "t3_thread_list", "t3_thread_organize",
    "t3_thread_read", "t3_thread_search", "t3_thread_send", "t3_thread_send_attachments", "t3_thread_transfer",
    "t3_thread_update", "t3_thread_wait", "t3_workflow_stop", "t3_worktree_handoff", "t3_worktree_list",
    "t3_worktree_status", "task_cancel", "task_status", "unlink_issue", "unlink_pull_request", "unlink_pull_requests",
    "unwatch_pull_request", "watch_pull_request", "write_pull_request_guides",
)
for _name in DRIVER_OTHER_TOOLS:
    TOOLS[_name] = "other"
del _name

# T3 tool name (unprefixed) -> neutral tool name.
DRIVER_TOOLS: dict[str, str] = {"send_to_user": "send_to_user", "request_secret": "request_secret", "delegate_task": "subagent", **{n: n for n in DRIVER_OTHER_TOOLS}}
DRIVER_PREFIXES = ("mcp__t3-code__", "mcp__t3_code__")

# Runner events (authored by the episode runner, not the harness export).
VERDICT_LABELS = ("answerable", "already_given", "unanswerable", "no_request")
# "episode_end": still open when the episode ended; the view ends it there.
WAIT_ENDS = ("reply", "budget_cutoff", "episode_end", "open")
# Why an output on the user channel has no verdict: delivered so close to a
# cutoff that the runner recorded none.
UNJUDGED_REASONS = ("cutoff", "deadline", "endpoint_failure")
# A hand-back (D-024, D-028): the agent handed control back by finishing its
# turn, or by a pending request (a question or secret request through a
# tool, the main agent's or a child's, which pauses the run).
HANDBACK_KINDS = ("finished", "pending", "interrupted")
# Which edges of a command's output the harness or driver cut away: the
# beginning (head), the end (tail), the middle (head and tail kept), none.
CLIP_EDGES = ("none", "head", "tail", "middle", "both")  # "both": head and tail
# What the wait was for: the policy's fixed reply to the target output, a
# task message (including one that replaced the policy's reply, whose verdict
# then has a null reply), a reopen, a reset, or the fixed refusal of a
# secret request.
WAIT_CAUSES = ("verdict_reply", "fixed_message", "reopen", "reset", "refusal")
PLANTED_PHASES = ("fired", "reached", "not_reached")
BUDGETS = ("time", "tokens", "user_wait")
EPISODE_END_REASONS = ("finished", "budget_cutoff", "harness_failure", "driver_failure", "runner_abort")

# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


class T:
    INT = "int"
    STR = "str"
    BOOL = "bool"
    OPT_INT = "int?"
    OPT_STR = "str?"
    OPT_BOOL = "bool?"


def enum(values: tuple[str, ...], optional: bool = False) -> tuple[str, tuple[str, ...], bool]:
    return ("enum", values, optional)


PROMPTS = "prompts?"  # null or [{"header": str|null, "text": str, "options": [{"label": str, "description": str|null}], "multi_select": bool}]
SECRET = "secret?"  # null or {"label": str|null, "reason": str|null, "fields": [{"label": str|null, "placeholder": str|null, "secret": bool}]}

# `us`: wall microseconds from the origin (the opening prompt's delivery), the
# full-precision time every decision uses; `t` is it in milliseconds.
COMMON_FIELDS = {"seq": T.INT, "t": T.INT, "us": T.INT, "type": T.STR, "agent": T.INT, "segment": T.INT}

# The runner's events as the trajectory keeps them.
RUNNER_EVENT_FIELDS: dict[str, dict[str, Any]] = {
    "verdict": {"target": T.INT, "label": enum(VERDICT_LABELS), "reply": T.OPT_STR, "answer_ids": "answer_ids"},
    # `replies_to`: every output whose verdict reply the wait carries (null
    # when the runner did not say, allowed only without an episode record).
    "wait": {"target": T.OPT_INT, "t_end": T.OPT_INT, "charged_ms": T.OPT_INT, "ended": enum(WAIT_ENDS), "causes": "causes", "replies_to": "seqs?"},
    "planted": {"name": T.STR, "phase": enum(PLANTED_PHASES)},
    "reopen": {"name": T.OPT_STR, "target": T.OPT_INT},
    "controlled_reset": {"target": T.OPT_INT},
    # `mark`: the runner's watcher mark at the delivery (D-025), or null.
    "correction_delivered": {"target": T.INT, "mark": T.OPT_STR},
    "unjudged": {"target": T.INT, "reason": enum(UNJUDGED_REASONS)},
    # `target`: the output that closed the hand-back, or null for a silent one.
    "handback": {"target": T.OPT_INT, "kind": enum(HANDBACK_KINDS)},
    "budget_cutoff": {"budget": enum(BUDGETS)},
    "episode_end": {"reason": enum(EPISODE_END_REASONS)},
    "probe": {"probe": T.INT},
    # D-046: when the agent received a reset notice steered into the running
    # turn, and the runner's watcher mark at the delivery. Full trajectory
    # only: the delivery's batch position goes to the run manifest
    # (`notices`), never to the view, so it is no Steering input.
    "notice_delivered": {"target": T.INT, "mark": T.OPT_STR},
}
# Runner events the full trajectory keeps and the scorer view leaves out.
AUDIT_RUNNER_TYPES = ("notice_delivered",)
# Optional fields the runner may add that stay in the audit record only (the
# run manifest's `runner_audit`), never in the trajectory or the bundle:
# `overrun_ms`, how late a reply was when judging took longer than the return
# delay. (`mark_settled` on a correction, which the driver no longer reports
# since D-037, is gone.)
RUNNER_AUDIT_FIELDS: dict[str, dict[str, Any]] = {
    "wait": {"overrun_ms": T.OPT_INT},
    # D-035: when the runner stopped the agent after an elapsed cutoff's
    # deadline (null for other ends).
    "episode_end": {"stopped_utc": T.OPT_STR},
    # Whether the reset's continuation was sent: a reset whose continuation
    # was `unsent` has a segment with no dispatch, which the driver leaves out.
    # D-046: how the reset's notice went (`mid_turn`: steered into the running
    # turn; `next_message`: sent with the next message) and whether the reset
    # came at the agent's hand-back or at the grace period's end.
    "controlled_reset": {"continuation": enum(("launched", "unsent"), True), "notice": enum(("mid_turn", "next_message"), True),
                         "reset_at": enum(("handback", "grace"), True)},
}
# What the runner may pass (`Hints.runner`): the kept fields and the audit
# ones. The runner emits a field once this table lists it.
RUNNER_FIELDS: dict[str, dict[str, Any]] = {k: {**v, **RUNNER_AUDIT_FIELDS.get(k, {})} for k, v in RUNNER_EVENT_FIELDS.items()}
# Audit-only fields accepted ahead of the runner emitting them (none now).
RUNNER_AUDIT_PENDING: dict[str, dict[str, Any]] = {}
RUNNER_INPUT_FIELDS: dict[str, dict[str, Any]] = {k: {**v, **RUNNER_AUDIT_PENDING.get(k, {})} for k, v in RUNNER_FIELDS.items()}

# Per event type: field -> type. Every field is required (null where the type allows it).
EVENT_FIELDS: dict[str, dict[str, Any]] = {
    "segment_start": {"cause": enum(SEGMENT_CAUSES)},
    "turn_start": {"turn": T.INT},
    "turn_end": {"turn": T.INT, "end": enum(TURN_ENDS)},
    "user_message": {"turn": T.INT, "kind": enum(USER_KINDS), "text": T.STR},
    # `t_end`, `us_end`: when the item finished (a final message reached the user then).
    "assistant_message": {"turn": T.INT, "role": enum(ASSISTANT_ROLES), "text": T.STR, "user_channel": T.BOOL, "user_text": T.OPT_STR, "t_end": T.OPT_INT, "us_end": T.OPT_INT},
    "tool_call": {
        "turn": T.INT,
        "call": T.INT,
        "class": enum(TOOL_CLASSES),
        "tool": enum(tuple(TOOLS)),
        "input": "input",
        "output": "output",
        "status": enum(CALL_STATUSES),
        "t_end": T.OPT_INT,
        "us_end": T.OPT_INT,
        "user_channel": T.BOOL,
        "user_text": T.OPT_STR,
        "question": T.OPT_INT,
        "subagent": T.OPT_INT,
    },
    "question": {"turn": T.INT, "question": T.INT, "kind": enum(QUESTION_KINDS), "call": T.OPT_INT, "user_channel": T.BOOL, "user_text": T.STR, "prompts": PROMPTS, "secret": SECRET},
    # `missing`: an answered question whose text could not be recovered; only
    # audit-only normalisation writes it, and such a trajectory makes no bundle.
    # `kind`: how the user's answer counts (D-023: the runner can merge a
    # reopen or a correction into the answer to a pending question).
    "answer": {"question": T.INT, "status": enum(ANSWER_STATUSES), "text": T.OPT_STR, "missing": T.BOOL, "kind": enum(ANSWER_KINDS)},
    "subagent_start": {"turn": T.INT, "subagent": T.INT, "call": T.OPT_INT, "title": T.OPT_STR, "prompt": T.OPT_STR},
    "subagent_end": {"subagent": T.INT, "status": enum(END_STATUSES), "result": T.OPT_STR},
    # A native compaction the driver exposed (T3 item type `compaction`), and
    # whether it succeeded. Full trajectory only; not in the scorer view.
    "compaction": {"turn": T.OPT_INT, "status": enum(CALL_STATUSES)},
    "error": {"turn": T.OPT_INT, "class": T.STR},
    # The driver's interrupt request on a run (T3 item type
    # `run_interrupt_request`): when it was asked for. `turn` is the agent's
    # turn open at that time, or null (a request during a wait on a
    # question). Full trajectory only; not in the scorer view.
    "interrupt_request": {"turn": T.OPT_INT},
    # An item no mapping covers. Placed at its run's nearest event and exempt
    # from turn membership: it must never break ordering, and it still fails
    # validation unless unknown items are allowed.
    "unknown": {"turn": T.OPT_INT, "item_type": T.STR},
    **RUNNER_EVENT_FIELDS,
    "wait": {**RUNNER_EVENT_FIELDS["wait"], "us_end": T.OPT_INT},
}
RUNNER_TYPES = tuple(RUNNER_FIELDS)

# `clipped`: which edges of the output were cut (CLIP_EDGES); `truncated` is
# whether any was. `last_line_whole`: the retained text's last line ended
# with a line break before cleanup removed it (or the cut was at a line
# boundary), so a result on it is whole; true when nothing was cut.
OUTPUT_FIELDS = {"text": T.OPT_STR, "available": T.BOOL, "truncated": T.BOOL, "clipped": enum(CLIP_EDGES), "last_line_whole": T.BOOL, "exit_code": T.OPT_INT}

# Per neutral tool: input field -> type. Driver tools not listed take
# {"raw": str?}; harness_other takes {}.
INPUT_FIELDS: dict[str, dict[str, Any]] = {
    "shell": {"command": T.OPT_STR},
    "file_read": {"path": T.OPT_STR, "offset": T.OPT_INT, "limit": T.OPT_INT},
    "file_write": {"path": T.OPT_STR, "content": T.OPT_STR, "content_truncated": T.BOOL},
    "file_edit": {"path": T.OPT_STR, "old_text": T.OPT_STR, "new_text": T.OPT_STR, "diff": T.OPT_STR, "content_truncated": T.BOOL},
    "search": {"pattern": T.OPT_STR, "path": T.OPT_STR},
    "web_fetch": {"url": T.OPT_STR, "prompt": T.OPT_STR},
    "web_search": {"query": T.OPT_STR},
    "subagent": {"title": T.OPT_STR, "prompt": T.OPT_STR},
    "ask_user": {"prompts": "prompts"},
    "tool_discovery": {"query": T.OPT_STR},
    "harness_other": {},
    "send_to_user": {"message": T.OPT_STR},
    "request_secret": {"label": T.OPT_STR, "reason": T.OPT_STR, "delivery": T.OPT_STR, "fields": "secret_fields"},
}
RAW_INPUT = {"raw": T.OPT_STR}
# Input fields that hold a path, made relative to the workspace root.
PATH_FIELDS = ("path",)


def input_fields(tool: str) -> dict[str, Any]:
    return INPUT_FIELDS.get(tool, RAW_INPUT if tool in DRIVER_OTHER_TOOLS else {})


ORIGINS = ("dispatch", "opening_message", "first_event")
# How the runner charged its waits: `scheduled` (D-035: the return delay cut
# to the budgets, never more than the interval between the wait's stamps) or
# `measured` (an older runner, which measured each charge from its own
# stamps and gave a single `cause`; replays of saved episodes only, D-035).
WAIT_CHARGES = ("scheduled", "measured")
# Whether the runner's `handback` events are the record of the hand-backs:
# `recorded` (the current runner's records: an episode with none had none)
# or `inferred` (a capture from before the runner recorded them: the main
# agent's finished turns stand in).
HANDBACK_SOURCES = ("recorded", "inferred")

HEADER_FIELDS = {
    "type": T.STR,
    "format": T.STR,
    "version": T.INT,
    "workspace": T.STR,
    "excluded": "intervals",  # [{us, us_end, reason}]: wall intervals the episode clock leaves out
    "post_run": "seqs",  # the events of the post-run exchange, by seq: never in a scorer input
    # Where time 0 is (D-020): `dispatch`, the driver's dispatch of the opening
    # prompt (Hints.episode_start_utc); else `opening_message`, the T3 item of
    # the opening prompt, or `first_event`. A bundle with a runner record
    # needs `dispatch`.
    "clock_origin": enum(ORIGINS),
    "wait_charges": enum(WAIT_CHARGES),
    "handbacks": enum(HANDBACK_SOURCES),
    "events": T.INT,
    "segments": T.INT,
    "agents": T.INT,
    "turns": T.INT,
    "duration_ms": T.INT,
}

# ---------------------------------------------------------------------------
# The scorer view
# ---------------------------------------------------------------------------

VIEW_HEADER_FIELDS = {"type": T.STR, "format": T.STR, "version": T.INT, "events": T.INT, "turns": T.INT, "duration_ms": T.INT, "clock_origin": enum(ORIGINS), "wait_charges": enum(WAIT_CHARGES),
                      "handbacks": enum(HANDBACK_SOURCES)}
VIEW_COMMON = {"seq": T.INT, "t": T.INT, "type": T.STR}
VIEW_TURN_ENDS = ("finished", "interrupted", "failed", "open")
VIEW_FIELDS: dict[str, dict[str, Any]] = {
    "turn_start": {"turn": T.INT},
    # `closed_by`: the view seq of the output that closed the turn (the main
    # agent's final answer, or the question that handed control back), or null.
    "turn_end": {"turn": T.INT, "end": enum(VIEW_TURN_ENDS), "closed_by": T.OPT_INT},
    "user_message": {"turn": T.OPT_INT, "kind": enum(USER_AUTHORED_KINDS), "text": T.STR, "secret": T.BOOL},
    "to_user": {"turn": T.OPT_INT, "text": T.STR, "secret": T.BOOL},
    **{k: v for k, v in RUNNER_EVENT_FIELDS.items() if k not in AUDIT_RUNNER_TYPES},
    # `batch`: the change-log batch the delivery mark closed (D-025).
    # `mark`: the delivery mark's neutral number in changes.jsonl.
    # `straddled` (D-037): the batch whose writes began by the delivery and
    # ended after it; its changes count on neither side of the correction.
    # `straddled_last`: the last of the run of batches that straddle it
    # (equal to `straddled` when one does); every batch of the run counts on
    # neither side.
    "correction_delivered": {"target": T.INT, "mark": T.OPT_INT, "batch": T.OPT_INT, "straddled": T.OPT_INT, "straddled_last": T.OPT_INT},
    # The hand-back's kind is left out of the view: a pending tool question
    # and a question in a final message are the same hand-back to the user
    # (D-018); the full trajectory keeps it.
    "handback": {"target": T.OPT_INT},
}

# ---------------------------------------------------------------------------
# Content fields: text the agent, the user, the task or the runner produced.
# The blindness check counts harness tells in these and fails on tells
# anywhere else. Paths are dotted; "*" matches any key or index below.
# ---------------------------------------------------------------------------

CONTENT_PATHS: dict[str, tuple[str, ...]] = {
    "user_message": ("text",),
    "assistant_message": ("text", "user_text"),
    "tool_call": ("input.*", "output.text", "user_text"),
    "question": ("user_text", "prompts.*", "secret.*"),
    "answer": ("text",),
    "subagent_start": ("title", "prompt"),
    "subagent_end": ("result",),
    "verdict": ("reply",),
}
VIEW_CONTENT_PATHS: dict[str, tuple[str, ...]] = {
    "user_message": ("text",),
    "to_user": ("text",),
    "verdict": ("reply",),
}

# Where a harness or driver decoration must not survive, per event type.
DECORATION_PATHS: dict[str, tuple[str, ...]] = {
    "tool_call": ("output.text", "input.content", "input.new_text", "input.old_text", "input.diff"),
    "user_message": ("text",),
    "subagent_start": ("prompt",),
}


def canonical(obj: Any) -> str:
    """The one serialisation used for every line: sorted keys, no spaces,
    ASCII-escaped as the watcher writes its log (T3 of round 12): a file name
    that is not valid UTF-8 (a lone surrogate, `x\\udcff`) round-trips, and
    the text is plain ASCII, so encoding and digesting it never fail."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def delivered_at(etype: str, start: Any, end: Any, turn_end: Any) -> Any:
    """When a user-channel output reached the user: the one rule every
    consumer uses (runner-target resolution and its checks in `normalise`,
    the view's times and ordering, disclosures, hand-backs). It is the
    driver's item delivery time, the instant the runner judges at: a final
    message when its item finished (at its turn's end only in an old capture
    with no item completion), a message-tool text when the call's result
    came back, a question when it was put to the user. Works on any ordered
    time (wall microseconds, datetimes); never earlier than `start`."""
    if etype == "assistant_message":
        t = end if end is not None else turn_end
    elif etype == "tool_call":
        t = end
    else:
        t = None
    return start if t is None or t < start else t
