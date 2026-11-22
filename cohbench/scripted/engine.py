"""Episode state and the policy that turns a request into a scripted reply.

Identity: which conversation a request belongs to
-------------------------------------------------

Every reply the server sends is a node, named by its message id, in one
conversation. A conversation's identity is its model, system prompt and tool
list; a node records the hash of that identity and of the conversation's
blocks up to and including the reply. The server decides a request's place
by these rules, in this order, and logs the rule and its reason
(`identity` in the request log). It never picks between two candidates.

a. A thread `continue` is identified by the message it continues from. An
   identical second continue from the same message is a retry and gets the
   same reply. A different continue from a message that has already been
   continued gets the API's `thread_already_continued` error. Nothing else is
   a retry of a continue.
b. A `create` with no assistant content whose first user message is the T3
   launch envelope of an unclaimed delegation instance is that child's
   launch, never a replay. Each instance has its own conversation and its
   own message counter, starting at handler 0 of its named child.
c. A `create` carrying assistant content resumes the conversation whose
   recorded history, identity included, it equals up to its last assistant
   message, and only at that conversation's latest reply. Several matches,
   no match, or a match at an earlier reply is an error: the fallback reply,
   logged as ambiguous, unknown or stale history.
d. Any other `create` is a main-conversation message. If the runner has
   announced the next message with its text, a create with that text is that
   message; one with other text is a retry of the one identical earlier
   create, or an error when there is none or several, and the announcement
   stays pending. An announcement without text takes the next create. Without
   an announcement, an identical earlier create (exactly one) makes this a
   retry; otherwise it is a new message.

A request placed by an error rule gets the fallback reply in a conversation
of its own (kind `unplaced`), counts no user message, and anything that
continues from it is placed the same way.

Delivery: the server reports each attempt to send a reply. Delivery only
moves forward: once any attempt of a reply was delivered, a later failed
attempt does not undo it.

Steering: a user message that arrives while a handler is still running (its
last reply was a tool call) is a steer. The running handler's `on_steer`
decides: `abandon` drops its remaining steps and starts the steer's handler
at once; `finish` runs the remaining steps first, then the steer's handler
in the same turn.

Failures never become an HTTP 500 or a hang. Tool results are processed in
two passes: every result and every delegation outcome is recorded first,
then captures run. A failing handler is marked failed, and a correction that
arrived for it still runs its own handler. User messages are counted before
anything that can fail.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import ids
from .script import (DEFAULT_FALLBACK, DELEGATE_TOOL, T3_TASK_MAX, ConditionError, Handler, RenderError, Script,
                     ScriptError, capture_value, evaluate, evaluate_expect, launch_prompt, load_script, render,
                     render_value, t3_leading_command, utf16_len)
from .wire import (ApiError, Item, Request, Usage, apply_tool_changes, check_tool_pairs, es_blank, es_strip, estimate_tokens,
                   invalid, offered_tools, output_tokens, prompt_tokens, schema_problem, tool_result_text,
                   tool_schemas)

EXPORT_FORMAT = "cohbench-scripted-episode"
EXPORT_VERSION = 1
NEXT_ROOT = "$next"
EXIT_RE = re.compile(r"Command exited with code (-?\d+)\s*$")
REPEAT_MARK = "@repeat|"
MAX_STEPS_PER_REPLY = 100_000

# T3 Code's sender note on a message an agent sent to another thread
# (t3 runtime 0.1.12-nightly.20261121.5051), followed by a blank line and the
# child's task prompt.
T3_NOTE_RE = re.compile(
    r'\[T3 Code: the agent in T3 thread "(?:[^"\\]|\\.)*"'
    r'(?: on linked environment "(?:[^"\\]|\\.)*")? sent this message, not the user\.\]\n\n')


def is_child_launch(text: str, task: str, role: Any) -> bool:
    """Whether `text` is the first message T3 gives the child of a
    delegate_task call with this task and role: withMessageSenderNote of
    taskPrompt of the trimmed task, which is the prompt alone when it starts
    with a slash command or a skill mention (T3's LEADING_COMMAND)."""
    prompt = launch_prompt(task, role)
    if t3_leading_command(prompt):
        return text == prompt
    m = T3_NOTE_RE.match(text)
    return m is not None and text[m.end():] == prompt


# T3 Code's prompt to a parent when delegated tasks it started reach a
# terminal state (apps/server/src/orchestration-v2/ProviderContinuationService.ts,
# delegatedCompletionText). It is dispatched with createdBy "agent" and
# creationSource "server" and no sender thread, so withMessageSenderNote adds
# no note: Pi receives exactly this text. Task ids are T3 node ids, which hold
# no spaces.
T3_COMPLETION_ONE = re.compile(
    r"Delegated task (\S+) reached a terminal state\. Use task_status with taskId (\S+) to read the result\.")
T3_COMPLETION_MANY = re.compile(
    r"Delegated tasks (\S+(?:, \S+)+) reached terminal states\. Use task_status with each taskId to read the results\.")


def is_completion_prompt(text: str) -> bool:
    m = T3_COMPLETION_ONE.fullmatch(text)
    if m is not None:
        return m.group(1) == m.group(2)
    return T3_COMPLETION_MANY.fullmatch(text) is not None


def canonical(value: Any) -> str:
    """JSON with sorted keys: 1, 1.0 and true are three different values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _no_cache(d: Any) -> Any:
    """A copy of one protocol object without its own `cache_control` key.
    Nothing inside it is touched."""
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if k != "cache_control"}
    return d


def _strip_block(b: Any) -> Any:
    """A content block without the cache markers the protocol puts on it: on
    the block itself, on the blocks inside a tool result's content, and on the
    tool definition a `tool_addition` carries. Tool input, schemas and any
    other data are kept exactly, a key named `cache_control` in them included."""
    if not isinstance(b, dict):
        return b
    out = _no_cache(b)
    if out.get("type") == "tool_result" and isinstance(out.get("content"), list):
        out["content"] = [_no_cache(x) for x in out["content"]]
    if out.get("type") == "tool_addition" and isinstance(out.get("tool"), dict):
        tool = dict(out["tool"])
        if isinstance(tool.get("definition"), dict):
            tool["definition"] = _no_cache(tool["definition"])
        out["tool"] = tool
    return out


def fingerprint(messages: list) -> str:
    """A request's new content exactly as sent, for telling a retry from a
    different request, for every kind of request: every message in order with
    its role, every block with its type and all its fields, text byte for byte,
    system-role blocks and tool results included. Only the protocol's cache
    markers are left out. This is deliberately not the history hash, whose
    canonical forms (trimmed text, no system blocks) serve only to find where
    a request belongs."""
    body = []
    for m in messages:
        # Every message-level field (role, content, `output_config` and any
        # other) is kept; only cache markers go.
        msg = {k: v for k, v in m.items() if k != "cache_control"}
        c = msg["content"]
        msg["content"] = [_strip_block(b) for b in c] if isinstance(c, list) else c
        body.append(msg)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def chain(h: str, key: tuple) -> str:
    return hashlib.sha256((h + json.dumps(key, ensure_ascii=False)).encode()).hexdigest()


def identity_hash(model: str, system: Any, tools: Any) -> str:
    """A conversation's identity: model, system prompt and tools, without the
    cache markers on system blocks and tool definitions (Pi moves them
    between requests). Nothing inside a block or a definition is changed."""
    sys_ = [_no_cache(b) for b in system] if isinstance(system, list) else system
    tools_ = [_no_cache(t) for t in tools] if isinstance(tools, list) else tools
    body = ["identity", model, sys_, tools_]
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stream_seed(position: str, reply_index: int, handler: Any, blocks: list, stop: str) -> str:
    """What the stream's chunking, padding and first output count are drawn
    from: the reply's content (tool call ids masked) and its position, the
    conversation's position key, the reply's index in it and the handler that
    produced it. The position key is fixed by content, not arrival: a main
    conversation's is the episode index of its first user message, a child's
    is its child name and launch envelope (not the delegation instance it
    claimed), an unplaced reply's is "unplaced"."""
    masked = [{k: ("" if k == "id" else v) for k, v in b.items()} for b in blocks]
    return f"{position}|{reply_index}|{handler}|{stop}|" + json.dumps(masked, sort_keys=True, ensure_ascii=False)


def block_key(b: dict) -> tuple:
    if b["type"] == "text":
        return ("assistant", "text", es_strip(b["text"]))
    return ("assistant", "tool_use", b["id"], b["name"])


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def result_exit(name: str, text: str, is_error: bool) -> int | None:
    """Exit status from Pi's tool result. Pi's bash tool (dist/core/tools/
    bash.js, 1.6.0) returns plain output on exit 0, and on any other exit the
    output, a blank line and "Command exited with code N", flagged is_error.
    A timeout or abort is an error with no code. Other tools: 0 when not an
    error, otherwise unknown."""
    if not is_error:
        return 0
    if name == "bash":
        m = EXIT_RE.search(text)
        if m:
            return int(m.group(1))
    return None


class StepFailure(Exception):
    """The script cannot go on; the reply becomes the fallback."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ConvState:
    conv: str
    kind: str                       # "main", "child" or "unplaced"
    child: str | None
    ns: str                         # the message counter this conversation uses
    model: str
    offered: frozenset
    schemas: dict
    active: int | None = None
    active_text: str | None = None
    queue: tuple = ()
    queued: tuple = ()              # ((index, text), ...) waiting behind the active handler
    pending: tuple = ()             # ({"id", "step", "name"}, ...)
    tool_ids: tuple = ()            # tool calls in this reply
    last_result: dict | None = None
    script: str = "orig"            # "orig" or "next": which script this conversation's handlers come from
    scope: str = "main"             # whose variables it reads and writes
    hscope: str = "main"            # main conversations: "next" while a replacement handler runs


@dataclass
class Node:
    msg_id: str
    conv: str
    hash: str
    state: ConvState
    model: str
    blocks: list
    stop_reason: str
    usage: Usage
    created: float
    context_tokens: int
    cached_upto: int
    delivered: bool = False
    failed_attempts: int = 0
    in_flight: int = 0
    seed: str = ""                  # stream framing seed: content and position, not the id


@dataclass
class Outcome:
    model: str
    msg_id: str
    blocks: list
    stop_reason: str
    usage: Usage
    delay: float
    log: dict
    replay: bool = False
    seed: str = ""


class ImportRefused(ValueError):
    """An import into a server that has already answered a request."""


class _Placed(Exception):
    """Identity resolution ended in a decision that skips the policy."""

    def __init__(self, outcome: Outcome):
        self.outcome = outcome


@dataclass
class Episode:
    script: Script | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        # Requests this server has answered in its lifetime; reset does not clear it.
        self.answered = 0
        self.reset()

    # ------------------------------------------------------------ admin

    def reset(self) -> None:
        with self.lock:
            self.started = time.time()
            self.seq = 0
            self.nodes: dict[str, Node] = {}
            self.by_hash: dict[str, list[str]] = {}
            self.continue_retries: dict[str, str] = {}
            self.create_retries: dict[str, list[str]] = {}
            self.ledgers: dict[str, list[dict]] = {"main": []}
            self.handler_status: dict[str, dict[int, dict]] = {"main": {}}
            self.vars: dict[str, str] = dict(self.script.vars) if self.script else {}
            self.scopes: dict[str, dict[str, str]] = {}
            self.delegations: list[dict] = []
            self.conversations: dict[str, dict] = {}
            self.errors: list[dict] = []
            self.prefix_cache: dict[str, int] = {}
            self.announced: dict | None = None
            self.announcements: list[dict] = []
            self._conv_seq = 0
            self._unplaced_seq = 0
            self.next_script: Script | None = None
            self.next_offset: int | None = None
            self.next_active = False   # a replacement handler has started; its vars are overlaid

    def load(self, script: Script) -> None:
        with self.lock:
            self.script = script
            self.reset()

    def announce(self, index: Any, text: Any = None) -> dict:
        """The runner says the next main user message it sends is number
        `index`, optionally with its text. Raises ValueError when the
        announcement cannot be right."""
        with self.lock:
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("index: expected a non-negative integer")
            if text is not None and not isinstance(text, str):
                raise ValueError("text: expected a string")
            have = len(self.ledgers["main"])
            if index != have:
                raise ValueError(f"index: {have} user messages have been counted, so the next one is {have}, not {index}")
            if self.announced is not None:
                self.announced["outcome"] = "replaced"
            self.announced = {"index": index, "text": text, "after_request": self.seq, "outcome": "pending"}
            self.announcements.append(self.announced)
            return dict(self.announced)

    def mark_answered(self) -> None:
        with self.lock:
            self.answered += 1

    def reply_attempt(self, msg_id: str) -> None:
        """An attempt to send a reply is about to write its first byte."""
        with self.lock:
            node = self.nodes.get(msg_id)
            if node is not None:
                node.in_flight += 1

    def reply_sent(self, msg_id: str, ok: bool) -> None:
        """One attempt to send a reply finished. Delivery only moves forward."""
        with self.lock:
            node = self.nodes.get(msg_id)
            if node is None:
                return
            node.in_flight = max(0, node.in_flight - 1)
            if ok:
                node.delivered = True
            else:
                node.failed_attempts += 1

    # ------------------------------------------------------------- core

    def handle(self, req: Request, now: float | None = None) -> Outcome:
        with self.lock:
            now = time.time() if now is None else now
            self.seq += 1
            self.answered += 1
            log: dict[str, Any] = {}
            try:
                return self._handle(req, now, log)
            except ApiError as e:
                # A refused request changes nothing and takes no number.
                self.seq -= 1
                e.identity = log.get("identity")
                raise

    def _handle(self, req: Request, now: float, log: dict) -> Outcome:
        log.update({"seq": self.seq, "thread": req.thread_type, "model": req.model,
                    "stream": req.stream, "errors": []})
        try:
            base, S, new_items, h_end, kind = self._place(req, now, log)
        except _Placed as p:
            return p.outcome
        return self._answer(req, now, log, base, S, new_items, h_end, kind)

    # -------------------------------------------------------- identity

    def _identity(self, log: dict, rule: str, decision: str, reason: str) -> None:
        log["identity"] = {"rule": rule, "decision": decision, "reason": reason}
        log.setdefault("match", {"a": "thread", "c": "history"}.get(rule, "root"))

    def _replay(self, log: dict, prior: Node, rule: str, reason: str) -> _Placed:
        self._identity(log, rule, "retry", reason)
        log.update(conversation=prior.conv, decision="replay", message_id=prior.msg_id,
                   stop_reason=prior.stop_reason, replay_of=prior.msg_id,
                   conversation_kind=self.conversations[prior.conv]["kind"])
        return _Placed(Outcome(prior.model, prior.msg_id, prior.blocks, prior.stop_reason, prior.usage, 0.0, log,
                               replay=True, seed=prior.seed))

    def _unplaced(self, req: Request, now: float, log: dict, rule: str, code: str, detail: str) -> _Placed:
        self._identity(log, rule, "error", detail)
        self._unplaced_seq += 1
        conv = f"u{self._unplaced_seq}"
        self.conversations[conv] = {"kind": "unplaced", "child": None, "instance": None, "parent": None,
                                    "created": now, "model": req.model, "reason": code, "head": None, "replies": 0}
        S = ConvState(conv=conv, kind="unplaced", child=None, ns="unplaced", model=req.model,
                      offered=frozenset(), schemas={})
        self._error(log, S, code, detail)
        blocks = [{"type": "text", "text": self._fallback_text(None)}]
        h = chain(identity_hash(req.model, None, None), ("unplaced", conv))
        node = self._record(req, None, S, blocks, "end_turn", now, h)
        log.update(conversation=conv, conversation_kind="unplaced", decision="fallback", message_id=node.msg_id,
                   stop_reason="end_turn", delay_s=0.0, tool_calls=[])
        return _Placed(Outcome(req.model, node.msg_id, blocks, "end_turn", node.usage, 0.0, log, seed=node.seed))

    def _is_head(self, node: Node) -> bool:
        return self.conversations[node.conv]["head"] == node.msg_id

    def _place(self, req: Request, now: float, log: dict):
        if req.thread_type == "continue":
            return self._place_continue(req, now, log)
        try:
            check_tool_pairs(req.messages, None)
        except ApiError as e:
            has_history = any(it.role == "assistant" for it in req.items)
            self._identity(log, "c" if has_history else "d", "refused", e.message)
            raise
        root = identity_hash(req.model, req.system, req.tools)
        last_assistant = max((i for i, it in enumerate(req.items) if it.role == "assistant"), default=None)
        if last_assistant is not None:
            return self._place_history(req, now, log, root, last_assistant)
        h_end = root
        for it in req.items:
            if it.key is not None:
                h_end = chain(h_end, it.key)
        results, user_msgs, _ = _split_new(req.items)
        first = user_msgs[0] if user_msgs else ""
        # Whether a create repeats an earlier one is decided by its exact
        # content (the same fingerprint as for a continue), not the canonical hash.
        retry_key = f"create|{root}|{fingerprint(req.messages)}"
        log["retry_key"] = retry_key
        # Rule b: a child's launch.
        cands = [d for d in self.delegations if self._eligible(d) and is_child_launch(first, d["task"], d["role"])]
        if cands:
            children = sorted({d["child"] for d in cands})
            if len(children) > 1:
                raise self._unplaced(req, now, log, "b", "ambiguous_child_launch",
                                     f"the launch message matches unclaimed delegations to different children {children}")
            d = cands[0]
            S = self._new_conversation(req, now, log, child=d["child"], deleg=d)
            self._identity(log, "b", "child launch",
                           f"first message is the launch envelope of delegation {d['instance']} to {d['child']!r}"
                           + (f"; {len(cands)} interchangeable instances were unclaimed" if len(cands) > 1 else ""))
            return None, S, req.items, h_end, "create"
        if user_msgs and all(is_completion_prompt(u) for u in user_msgs):
            # Only T3's completion prompt: no simulated-user message at all.
            # A byte-identical resend is a retry of the reply already given.
            again = self.create_retries.get(retry_key, [])
            if len(again) == 1:
                raise self._replay(log, self.nodes[again[0]], "d", f"identical to one earlier create ({again[0]})")
            if again:
                raise self._unplaced(req, now, log, "d", "ambiguous_retry",
                                     f"identical to {len(again)} earlier creates; which one this repeats cannot be decided")
            S = self._new_conversation(req, now, log, child=None, deleg=None)
            self._identity(log, "d", "new main conversation", "T3's delegation completion prompt, not a user message")
            return None, S, req.items, h_end, "create"
        # Rule d: a main-conversation message.
        prior = self.create_retries.get(retry_key, [])
        a = self.announced if self.announced and self.announced["outcome"] == "pending" else None
        if a is not None and a["text"] is not None:
            if first == a["text"]:
                reason = f"the text equals the runner's announcement of user message {a['index']}"
            elif len(prior) == 1:
                raise self._replay(log, self.nodes[prior[0]], "d",
                                   "the text is not the announced one and the request is identical to exactly one "
                                   f"earlier create ({prior[0]}); the announcement stays pending")
            else:
                raise self._unplaced(req, now, log, "d", "unannounced_message",
                                     f"the runner announced other text for user message {a['index']}, and this create "
                                     f"is identical to {len(prior)} earlier creates, not one")
        elif a is not None:
            reason = f"the runner announced user message {a['index']} without text"
        elif prior:
            if len(prior) == 1:
                raise self._replay(log, self.nodes[prior[0]], "d", f"identical to one earlier create ({prior[0]})")
            raise self._unplaced(req, now, log, "d", "ambiguous_retry",
                                 f"identical to {len(prior)} earlier creates; which one this repeats cannot be decided")
        else:
            reason = "a create with no history and no matching delegation"
            if self.announcements:
                self._error(log, None, "unannounced_message",
                            "the runner uses announcements and did not announce this message; it is counted")
        if T3_NOTE_RE.match(first):
            self._error(log, None, "unmatched_child",
                        "the message has T3 Code's agent envelope but matches no unclaimed delegation; "
                        "it is a main-conversation message")
        S = self._new_conversation(req, now, log, child=None, deleg=None)
        self._identity(log, "d", "new main conversation", reason)
        return None, S, req.items, h_end, "create"

    def _place_continue(self, req: Request, now: float, log: dict):
        prev = req.thread["previous_message_id"]
        log["previous_message_id"] = prev
        base = self.nodes.get(prev)
        if base is None:
            self._identity(log, "a", "refused", f"{prev} is not a reply this server sent (thread_not_found)")
            raise invalid(f"thread.previous_message_id: no message {prev} to continue", code="thread_not_found")
        if base.model != req.model:
            self._identity(log, "a", "refused",
                           f"the model {req.model!r} is not the thread's {base.model!r} (thread_fingerprint_mismatch)")
            raise invalid("thread: the request's settings differ from the thread's",
                          code="thread_fingerprint_mismatch")
        try:
            check_tool_pairs(req.messages, list(base.state.tool_ids))
        except ApiError as e:
            self._identity(log, "a", "refused", e.message)
            raise
        h_end = base.hash
        for it in req.items:
            if it.key is not None:
                h_end = chain(h_end, it.key)
        retry_key = f"{prev}|{fingerprint(req.messages)}"
        rid = self.continue_retries.get(retry_key)
        if rid is not None:
            raise self._replay(log, self.nodes[rid], "a", f"an identical continue from {prev} was answered by {rid}")
        if base.state.kind == "unplaced":
            raise self._unplaced(req, now, log, "a", "continues_unplaced",
                                 f"{prev} was a fallback reply that belongs to no conversation")
        if not self._is_head(base):
            self._identity(log, "a", "refused",
                           f"{prev} has already been continued by a different request (thread_already_continued)")
            raise invalid(f"thread.previous_message_id: {prev} has already been continued",
                          code="thread_already_continued")
        log["base"] = prev
        log["retry_key"] = retry_key
        self._identity(log, "a", "continue", f"continues {prev}, the latest reply of {base.conv}")
        return base, copy.copy(base.state), req.items, h_end, "continue"

    def _place_history(self, req: Request, now: float, log: dict, root: str, last: int):
        h = root
        for it in req.items[:last + 1]:
            if it.key is not None:
                h = chain(h, it.key)
        h_end = h
        for it in req.items[last + 1:]:
            if it.key is not None:
                h_end = chain(h_end, it.key)
        cands = self.by_hash.get(h, [])
        if not cands:
            raise self._unplaced(req, now, log, "c", "unknown_history",
                                 "the history, with this model, system prompt and tools, matches no conversation")
        if len(cands) > 1:
            raise self._unplaced(req, now, log, "c", "ambiguous_history",
                                 f"the history matches {len(cands)} replies: {', '.join(cands)}")
        base = self.nodes[cands[0]]
        if base.state.kind == "unplaced":
            raise self._unplaced(req, now, log, "c", "continues_unplaced",
                                 f"the history ends at {base.msg_id}, a fallback reply that belongs to no conversation")
        # The canonical history found the conversation; whether this repeats
        # an earlier resume is decided by the exact new content after it.
        new_messages = req.messages[req.items[last].msg_index + 1:]
        retry_key = f"resume|{base.msg_id}|{fingerprint(new_messages)}"
        log["retry_key"] = retry_key
        if not self._is_head(base):
            prior = self.create_retries.get(retry_key, [])
            if len(prior) == 1:
                raise self._replay(log, self.nodes[prior[0]], "c",
                                   f"identical to one earlier resume ({prior[0]})")
            raise self._unplaced(req, now, log, "c", "stale_history",
                                 f"the history ends at {base.msg_id}, but {base.conv} has replied since")
        check = req.items[last + 1:]
        log["base"] = base.msg_id
        self._identity(log, "c", "resume", f"the history equals {base.conv} up to its latest reply {base.msg_id}")
        return base, copy.copy(base.state), check, h_end, "create"

    def _eligible(self, d: dict) -> bool:
        """Claimable from the moment its reply is decided, unless every
        attempt to send that reply so far has been reported failed and none
        is in flight."""
        if d["conversation"] is not None or d["failed"]:
            return False
        node = self.nodes.get(d["message_id"])
        if node is None:
            return False
        return node.delivered or node.in_flight > 0 or node.failed_attempts == 0

    # ---------------------------------------------------------- policy

    def _answer(self, req: Request, now: float, log: dict, base: Node | None, S: ConvState,
                new_items: list[Item], h_end: str, kind: str) -> Outcome:
        if base is not None:
            base_offered, base_schemas = set(base.state.offered), dict(base.state.schemas)
            if req.tools is not None:
                offered, schemas = offered_tools(req.tools), tool_schemas(req.tools)
                apply_tool_changes(offered, schemas, req.items)
            else:
                offered, schemas = base_offered, base_schemas
                apply_tool_changes(offered, schemas, new_items)
            S.offered, S.schemas = frozenset(offered), schemas
        S.model = req.model
        log["conversation"] = S.conv
        log["conversation_kind"] = S.kind
        if S.child:
            log["child"] = S.child
        if req.system is not None or req.tools is not None:
            log["tools"] = sorted(t["name"] for t in req.tools or [])
            log["tools_offered"] = sorted(S.offered)

        results, user_msgs, _ = _split_new(new_items)
        handlers = self._handlers(S)
        ns = S.ns
        # T3's completion prompt for a delegation is not a user message: it
        # counts no index, advances no handler and gets the script's short reply.
        completions = [u for u in user_msgs if is_completion_prompt(u)]
        if completions:
            user_msgs = [u for u in user_msgs if not is_completion_prompt(u)]
            owner = self.next_script if (S.hscope == "next" and self.next_script is not None) else self.script
            oc = owner.on_child_completion if owner else {"text": "Noted.", "capture": []}
            log["delegation_completion"] = [text_sha(u) for u in completions]
            oc_root = NEXT_ROOT if (owner is not None and owner is self.next_script) else "$"
            for u in completions:
                self._apply_captures(S, oc["capture"], u, f"{oc_root}.on_child_completion.capture",
                                     "T3's completion prompt", log)
            if not user_msgs and not results:
                return self._completion_reply(req, now, log, base, S, h_end, kind, oc, owner, oc_root)
        indexed = []
        log["user_messages"] = []
        for um in user_msgs:
            idx = self._index(ns, um, S.conv, now, log)
            indexed.append((idx, um))
            log["user_messages"].append({"index": idx, "sha": text_sha(um)})

        fallback = False
        sends: list[dict] = []
        log["steer"] = S.active is not None and bool(indexed)
        running = S.active
        try:
            abandoning = (S.active is not None and bool(indexed)
                          and S.active < len(handlers) and handlers[S.active].on_steer == "abandon")
            failure = self._take_results(S, results, log, lenient=abandoning)
            if failure is not None:
                running = S.active
                if S.active is not None:
                    self._status(ns, S.active, status="error", finished=now, error=failure.code)
                S.active, S.active_text, S.queue = None, None, ()
                if not indexed and not S.queued:
                    raise failure
            self._dispatch(S, handlers, ns, indexed, now, log)
            if not user_msgs and not results:
                raise StepFailure("nothing_to_answer", "the request has no new user message or tool result")
            blocks, stop, delay, sends = self._run(S, handlers, ns, now, log, failure)
        except StepFailure as f:
            fallback = True
            for k in ("handler", "step", "ends_with"):
                log.pop(k, None)
            if not any(e["code"] == f.code and e["detail"] == f.detail for e in log["errors"]):
                self._error(log, S, f.code, f.detail)
            if S.active is not None:
                self._status(ns, S.active, status="error", finished=now, error=f.code)
            for idx, _ in S.queued:
                self._status(ns, idx, status="skipped", finished=now, error=f.code)
            # The fallback text belongs to the script that owns the handler that failed.
            failed = log.get("failed_handlers") or []
            owner_idx = (S.active if S.active is not None else failed[-1] if failed
                         else running if running is not None else indexed[-1][0] if indexed else None)
            fallback_text = self._fallback_text(self._owner(S, owner_idx))
            S.active, S.active_text, S.queue, S.queued, S.pending = None, None, (), (), ()
            # What completed handlers committed to this reply stays in it;
            # only the failing handler's own unsent narration and delay go.
            kept_narration, kept_delay = getattr(f, "committed", ([], 0.0))
            blocks = [{"type": "text", "text": "\n\n".join(kept_narration + [fallback_text])}]
            stop, delay, sends = "end_turn", kept_delay, []

        S.tool_ids = tuple(b["id"] for b in blocks if b["type"] == "tool_use")
        node = self._record(req, base, S, blocks, stop, now, None, h_end=h_end,
                            handler=None if fallback else log.get("handler"))
        for d in sends:
            d["message_id"] = node.msg_id
            d["instance"] = len(self.delegations) + 1
            self.delegations.append(d)
        if kind == "continue":
            self.continue_retries[log["retry_key"]] = node.msg_id
        else:
            self.create_retries.setdefault(log["retry_key"], []).append(node.msg_id)
        if fallback:
            log["decision"] = "fallback"
        log.update(message_id=node.msg_id, stop_reason=stop, delay_s=delay,
                   tool_calls=[{"name": b["name"], "id": b["id"]} for b in blocks if b["type"] == "tool_use"])
        return Outcome(req.model, node.msg_id, blocks, stop, node.usage, delay, log, seed=node.seed)

    def _completion_reply(self, req: Request, now: float, log: dict, base: Node | None, S: ConvState, h_end: str,
                          kind: str, oc: dict, owner: Script | None, root: str) -> Outcome:
        """The reply to a request that carries only T3's completion prompt:
        the owning script's `on_child_completion` text, the conversation's
        handler state unchanged. If the text cannot be rendered the reply is
        the owning script's fallback, and the log says so."""
        decision = "child_completion"
        try:
            text = self._reply_text(S, oc["text"], f"{root}.on_child_completion.text")
        except StepFailure as f:
            self._error(log, S, f.code, f.detail)
            text = self._fallback_text(owner)
            decision = "fallback"
        blocks = [{"type": "text", "text": text}]
        S.tool_ids = ()
        node = self._record(req, base, S, blocks, "end_turn", now, None, h_end=h_end, handler=None)
        if kind == "continue":
            self.continue_retries[log["retry_key"]] = node.msg_id
        else:
            self.create_retries.setdefault(log["retry_key"], []).append(node.msg_id)
        log.update(decision=decision, message_id=node.msg_id, stop_reason="end_turn", delay_s=0.0,
                   tool_calls=[], user_messages=[])
        return Outcome(req.model, node.msg_id, blocks, "end_turn", node.usage, 0.0, log, seed=node.seed)

    def _record(self, req: Request, base: Node | None, S: ConvState, blocks: list, stop: str, now: float,
                h_override: str | None, *, h_end: str | None = None, handler: Any = None) -> Node:
        msg_id = ids.message_id()
        if h_override is not None:
            h_after = h_override
        else:
            h_after = h_end
            for b in blocks:
                h_after = chain(h_after, block_key(b))
        usage, context, cached = self._usage(req, base, blocks)
        rec = self.conversations[S.conv]
        node = Node(msg_id, S.conv, h_after, S, req.model, blocks, stop, usage, now, context, cached,
                    seed=stream_seed(rec.get("position", "unplaced"), rec["replies"], handler, blocks, stop))
        self.nodes[msg_id] = node
        self.by_hash.setdefault(h_after, []).append(msg_id)
        self.conversations[S.conv]["head"] = msg_id
        self.conversations[S.conv]["replies"] += 1
        return node

    def _owner(self, S: ConvState, idx: int | None) -> Script | None:
        """The script that owns handler `idx` of conversation S."""
        nxt = self.next_script
        if self.script is None or nxt is None:
            return self.script
        if S.kind == "child":
            return nxt if S.script == "next" else self.script
        if S.kind == "main" and idx is not None:
            return nxt if idx >= self.next_offset else self.script
        # A reply that belongs to no handler (an unplaced request) takes the
        # original script's fallback, whatever has run since the import.
        return self.script

    def _fallback_text(self, owner: Script | None) -> str:
        if owner is None:
            owner = self.script
        return owner.fallback if owner else DEFAULT_FALLBACK

    def _step_def(self, path: str) -> dict:
        if self.next_script is not None and path.startswith(NEXT_ROOT + "."):
            return self.next_script.steps[path]
        return self.script.steps[path]

    def _handlers(self, S: ConvState) -> list:
        """The handler list a conversation counts against. After an import
        with a replacement script, main message k from the import's count
        onward is answered by the replacement's handler k minus that count.
        A child uses the children of the script whose handler delegated it,
        so a conversation running at export keeps its script."""
        if self.script is None:
            return []
        nxt = self.next_script
        if S.child is not None:
            owner = nxt if (S.script == "next" and nxt is not None) else self.script
            return owner.children.get(S.child, [])
        if nxt is None:
            return self.script.handlers
        earlier = list(self.script.handlers[:self.next_offset])
        earlier += [None] * (self.next_offset - len(earlier))
        return earlier + nxt.handlers

    def _new_conversation(self, req: Request, now: float, log: dict, *, child: str | None,
                          deleg: dict | None) -> ConvState:
        self._conv_seq += 1
        conv = f"c{self._conv_seq}"
        ns = "main"
        script, scope = "orig", "main"
        if deleg is not None:
            deleg["conversation"] = conv
            deleg["matched_at"] = now
            ns = f"child:{child}/{deleg['instance']}"
            self.ledgers.setdefault(ns, [])
            self.handler_status.setdefault(ns, {})
            script = deleg["script"]
            scope = f"d{deleg['instance']}"
            # The child reads its parent's variables as they were when the
            # delegating reply was decided; its own writes stay its own.
            self.scopes[scope] = dict(deleg["vars"])
        system_text = _system_text(req.system)
        self.conversations[conv] = {
            # Fixed by content and position, not by arrival (see stream_seed).
            # A child's is its child name and launch envelope, so equivalent
            # delegations give identical streams whichever claims which.
            "position": (f"child:{child}|" + hashlib.sha256(launch_prompt(deleg["task"], deleg["role"]).encode())
                         .hexdigest()[:24]) if deleg is not None else f"main@{len(self.ledgers['main'])}",
            "kind": "child" if child else "main",
            "child": child,
            "instance": deleg["instance"] if deleg else None,
            "parent": deleg["parent_conversation"] if deleg else None,
            "created": now,
            "model": req.model,
            "identity": identity_hash(req.model, req.system, req.tools),
            "system_sha256": hashlib.sha256(system_text.encode()).hexdigest() if req.system is not None else None,
            "system_chars": len(system_text) if req.system is not None else None,
            "tools": sorted(t["name"] for t in req.tools or []),
            "head": None,
            "replies": 0,
        }
        log["new_conversation"] = True
        if req.system is not None:
            log["system_sha256"] = self.conversations[conv]["system_sha256"]
            log["system_chars"] = len(system_text)
        offered, schemas = offered_tools(req.tools), tool_schemas(req.tools)
        apply_tool_changes(offered, schemas, req.items)
        return ConvState(conv=conv, kind="child" if child else "main", child=child, ns=ns, model=req.model,
                         offered=frozenset(offered), schemas=schemas, script=script, scope=scope)

    def _index(self, ns: str, text: str, conv: str, now: float, log: dict) -> int:
        ledger = self.ledgers.setdefault(ns, [])
        idx = len(ledger)
        ledger.append({"index": idx, "conversation": conv, "time": now, "sha": text_sha(text),
                       "preview": text[:120]})
        a = self.announced
        if ns == "main" and a is not None and a["outcome"] == "pending":
            if a["text"] is None or a["text"] == text:
                a["outcome"] = "arrived"
                a["request"] = self.seq
                a["counted_as"] = idx
                if idx != a["index"]:
                    self._error(log, None, "announced_index_mismatch",
                                f"the announced message was counted as {idx}, not {a['index']}")
                self.announced = None
            else:
                self._error(log, None, "announced_text_mismatch",
                            f"user message {idx} is not the announced text; the announcement stays pending: {text[:200]!r}")
        return idx

    def _status(self, ns: str, idx: int, **fields: Any) -> None:
        self.handler_status.setdefault(ns, {}).setdefault(idx, {"index": idx}).update(fields)

    def _dispatch(self, S: ConvState, handlers: list[Handler], ns: str, msgs: list, now: float, log: dict) -> None:
        """Route new user messages: start a handler, queue behind a running
        one (`finish`), or replace it (`abandon`)."""
        for idx, um in msgs:
            if S.active is not None:
                log["steer"] = True
                if handlers[S.active].on_steer == "abandon":
                    self._status(ns, S.active, status="abandoned", finished=now)
                    for q, _ in S.queued:
                        self._status(ns, q, status="skipped", finished=now, error="abandoned")
                    S.active, S.active_text, S.queue, S.queued = None, None, (), ()
                    self._try_activate(S, handlers, ns, idx, um, now, log)
                else:
                    S.queued = S.queued + ((idx, um),)
            elif S.queued:
                # Corrections that are already waiting go first.
                S.queued = S.queued + ((idx, um),)
            else:
                self._try_activate(S, handlers, ns, idx, um, now, log)
        log.setdefault("steer", False)

    def _try_activate(self, S: ConvState, handlers: list[Handler], ns: str, idx: int, text: str, now: float,
                      log: dict) -> None:
        try:
            self._activate(S, handlers, ns, idx, text, now, log)
        except StepFailure as f:
            self._error(log, S, f.code, f.detail)
            log.setdefault("failed_handlers", []).append(idx)

    def _activate(self, S: ConvState, handlers: list[Handler], ns: str, idx: int, text: str, now: float,
                  log: dict) -> None:
        if self.script is None:
            raise StepFailure("no_script", "no script is loaded")
        if idx >= len(handlers):
            self._status(ns, idx, status="error", finished=now, error="script_exhausted")
            raise StepFailure("script_exhausted",
                              f"user message {idx} of {ns!r} has no handler ({len(handlers)} defined)")
        h = handlers[idx]
        if h is None:
            raise StepFailure("handler_replaced", f"user message {idx} came before the import's replacement script")
        if S.kind == "main":
            replacement = self.next_script is not None and idx >= self.next_offset
            if replacement and not self.next_active:
                # The replacement's first handler starts. Its handlers get a
                # scope of their own: the main variables as they are now, with
                # its vars laid over them. Original handlers keep the original.
                self.scopes["next"] = {**self.vars, **self.next_script.vars}
                self.next_active = True
                log.setdefault("replacement_started", True)
            S.hscope = "next" if replacement else "main"
        self._status(ns, idx, status="starting", conversation=S.conv, started=now, label=h.label, steps_emitted=0)
        S.active_text = text
        try:
            if h.expect is not None:
                try:
                    ok = evaluate_expect(h.expect, text, self._values(S), f"{h.path}.expect")
                except RenderError as e:
                    raise StepFailure("missing_variable", str(e)) from None
                except ConditionError as e:
                    raise StepFailure("not_a_number", str(e)) from None
                if not ok:
                    raise StepFailure("expect_mismatch",
                                      f"{h.path}: the user message does not match `expect`: {text[:200]!r}")
            failure = self._apply_captures(S, h.capture, text, f"{h.path}.capture", "the user message", log)
            if failure is not None:
                raise failure
        except StepFailure as f:
            S.active_text = None
            self._status(ns, idx, status="error", finished=now, error=f.code)
            raise
        S.active, S.queue = idx, tuple(h.steps)
        self._status(ns, idx, status="active")

    def _scope(self, S: ConvState) -> dict[str, str]:
        if S.scope != "main":
            return self.scopes[S.scope]      # a child's own scope
        if S.hscope == "next":
            return self.scopes["next"]       # a replacement handler's scope
        return self.vars

    def _values(self, S: ConvState) -> dict[str, str | None]:
        values: dict[str, str | None] = dict(self._scope(S))
        r = S.last_result
        values["last_output"] = r["text"] if r else None
        values["last_exit"] = str(r["exit"]) if r and r.get("exit") is not None else None
        values["user_message"] = S.active_text
        return values

    def _render(self, S: ConvState, text: str, where: str) -> str:
        try:
            return render(text, self._values(S), where)
        except RenderError as e:
            raise StepFailure("missing_variable", str(e)) from None

    def _narrate(self, S: ConvState, narration: list, template: str, where: str, log: dict) -> None:
        """Narration that renders to nothing but whitespace is skipped: Pi
        drops such a text block when it resends history, so it must never be
        sent or enter a history hash."""
        text = self._render(S, template, where)
        if not es_blank(text):
            narration.append(text)
        else:
            log.setdefault("skipped_narration", []).append(where)

    def _reply_text(self, S: ConvState, template: str, where: str) -> str:
        """A turn-ending text. Blank would be dropped by Pi with the whole
        message, so it is a handler failure."""
        text = self._render(S, template, where)
        if es_blank(text):
            raise StepFailure("blank_reply", f"{where} renders to nothing but whitespace")
        return text

    def _cond(self, S: ConvState, cond: dict, where: str) -> bool:
        try:
            return evaluate(cond, S.last_result, self._values(S), where)
        except RenderError as e:
            raise StepFailure("missing_variable", str(e)) from None
        except ConditionError as e:
            raise StepFailure("not_a_number", str(e)) from None

    def _apply_captures(self, S: ConvState, caps: list, text: str, where: str, source: str, log: dict, *,
                        lenient: bool = False) -> StepFailure | None:
        """Apply every capture; return the first required one that missed."""
        failure = None
        for cap in caps:
            value = capture_value(cap, text)
            if value is None:
                if cap.get("required") and not lenient and failure is None:
                    failure = StepFailure("capture_failed", f"{where}: {cap['regex']!r} found nothing in {source}")
                log.setdefault("captures_missed", []).append(cap["var"])
                continue
            self._scope(S)[cap["var"]] = value
            log.setdefault("captured", {})[cap["var"]] = value
        return failure

    def _take_results(self, S: ConvState, results: list[Item], log: dict, *, lenient: bool) -> StepFailure | None:
        """Two passes: record every result and delegation outcome, then run
        captures. Returns the first required capture that missed."""
        pending = {p["id"]: p for p in S.pending}
        got: dict[str, dict] = {}
        for it in results:
            tid = it.data["tool_use_id"]
            p = pending.get(tid)
            if p is None:
                self._error(log, S, "unmatched_tool_result", f"tool result {tid} answers no call this server is waiting on")
                continue
            text = tool_result_text(it.data)
            is_error = bool(it.data.get("is_error", False))
            got[tid] = {"tool_use_id": tid, "name": p["name"], "text": text, "is_error": is_error,
                        "exit": result_exit(p["name"], text, is_error)}
            for d in self.delegations:
                if d["tool_use_id"] == tid:
                    d["result"] = "error" if is_error else "ok"
                    if is_error:
                        d["failed"] = True
        log["tool_results"] = len(results)
        ordered = [got[p["id"]] for p in S.pending if p["id"] in got]
        if ordered:
            S.last_result = ordered[-1]
        failure = None
        for p in S.pending:
            if p["id"] not in got:
                continue
            step = self._step_def(p["step"]) if self.script else {}
            f = self._apply_captures(S, step.get("capture", []), got[p["id"]]["text"], p["step"],
                                     f"the result of {p['name']}", log, lenient=lenient)
            failure = failure or f
        S.pending = ()
        if failure is not None:
            self._error(log, S, failure.code, failure.detail)
        return failure

    def _run(self, S: ConvState, handlers: list[Handler], ns: str, now: float, log: dict,
             failure: StepFailure | None) -> tuple[list, str, float, list]:
        narration: list[str] = []
        delay = [0.0]
        # What completed handlers have committed to this reply. Narration and
        # delay past this mark belong to the running handler and are dropped
        # if it fails.
        committed = (0, 0.0)
        # Each handler has its own step budget: a handler that exhausts it
        # fails like any other, and a queued correction starts afresh.
        budget = MAX_STEPS_PER_REPLY
        while True:
            if S.active is None:
                if S.queued:
                    (nidx, ntext), S.queued = S.queued[0], S.queued[1:]
                    log.setdefault("chained", []).append(nidx)
                    self._try_activate(S, handlers, ns, nidx, ntext, now, log)
                    budget = MAX_STEPS_PER_REPLY
                    continue
                if failure is not None:
                    raise self._keep(failure, narration, committed)
                if log.get("failed_handlers"):
                    last = log["errors"][-1]
                    raise self._keep(StepFailure(last["code"], last["detail"]), narration, committed)
                raise self._keep(StepFailure("no_active_handler", "no handler is running for this conversation"),
                                 narration, committed)
            try:
                budget -= 1
                if budget < 0:
                    raise StepFailure("too_many_steps",
                                      f"handler {S.active} took more than {MAX_STEPS_PER_REPLY} steps without a reply")
                out = self._step(S, handlers, ns, now, log, narration, delay)
            except StepFailure as f:
                if not S.queued:
                    raise self._keep(f, narration, committed)
                # A correction is waiting: record this handler as failed, drop
                # what it had not yet sent, and go on to the correction.
                self._error(log, S, f.code, f.detail)
                self._status(ns, S.active, status="error", finished=now, error=f.code)
                S.active, S.active_text, S.queue = None, None, ()
                del narration[committed[0]:]
                delay[0] = committed[1]
                continue
            if out is not None:
                return out[0], out[1], delay[0], out[2]
            if S.active is None:
                committed = (len(narration), delay[0])

    def _step(self, S: ConvState, handlers: list[Handler], ns: str, now: float, log: dict,
              narration: list, delay: list):
        """Advance the active handler by one step. Returns (blocks, stop,
        delegation sends) when a reply is ready, else None."""
        h = handlers[S.active]
        if not S.queue:
            if h.final is None:
                raise StepFailure("no_final", f"{h.path} ran out of steps and has no `final`")
            text = self._reply_text(S, h.final, f"{h.path}.final")
            delay[0] += h.final_delay
            return self._end_handler(S, ns, h.end, now, narration, text, log, f"{h.path}.final", "final")
        path, S.queue = S.queue[0], S.queue[1:]
        if path.startswith(REPEAT_MARK):
            rpath, n = path[len(REPEAT_MARK):].rsplit("|", 1)
            n = int(n)
            step = self._step_def(rpath)
            if "until" in step and self._cond(S, step["until"], f"{rpath}.until"):
                log.setdefault("repeats", []).append({"step": rpath, "iterations": n, "stopped": "until"})
            elif n >= step["max"]:
                log.setdefault("repeats", []).append({"step": rpath, "iterations": n, "stopped": "max"})
            else:
                S.queue = self._repeat_body(rpath, step, n + 1) + S.queue
            return None
        step = self._step_def(path)
        kind = step["do"]
        if kind == "repeat":
            S.queue = self._repeat_body(path, step, 1) + S.queue
            return None
        if kind == "branch":
            chosen, label = step.get("else", []), "else"
            for i, case in enumerate(step["cases"]):
                if self._cond(S, case["when"], f"{path}.cases[{i}].when"):
                    chosen, label = case["steps"], f"cases[{i}]"
                    break
            prefix = f"{path}.else" if label == "else" else f"{path}.{label}.steps"
            S.queue = tuple(f"{prefix}[{i}]" for i in range(len(chosen))) + S.queue
            log.setdefault("branches", []).append({"step": path, "taken": label})
            return None
        if kind == "say":
            self._narrate(S, narration, step["text"], f"{path}.text", log)
            delay[0] += step.get("delay", 0)
            return None
        if kind == "set":
            self._scope(S)[step["var"]] = self._render(S, step["value"], f"{path}.value")
            return None
        if kind == "reply":
            text = self._reply_text(S, step["text"], f"{path}.text")
            delay[0] += step.get("delay", 0)
            S.queue = ()
            return self._end_handler(S, ns, step.get("end", h.end), now, narration, text, log, path, "reply")
        # A tool step, or several in parallel. Every call is built and
        # checked before anything about them is recorded.
        calls = ([(f"{path}.calls[{i}]", c) for i, c in enumerate(step["calls"])]
                 if kind == "parallel" else [(path, step)])
        say = self._render(S, step["say"], f"{path}.say") if step.get("say") else None
        if say is not None and es_blank(say):
            log.setdefault("skipped_narration", []).append(f"{path}.say")
            say = None
        built = [(cpath, c, *self._tool_call(S, cpath, c)) for cpath, c in calls]
        envelopes: dict[str, tuple] = {}
        snapshot = dict(self._scope(S))
        for cpath, c, name, value in built:
            if c["do"] == "delegate":
                env = launch_prompt(value["task"], value.get("role"))
                # Calls in this step with one envelope must be the same call:
                # same child and identical tool input (target, mode, every argument).
                if env in envelopes and envelopes[env] != (c["child"], canonical(value)):
                    raise StepFailure("ambiguous_delegation",
                                      f"{cpath} has the launch message of another call in this step but a different "
                                      "child or different arguments; make the task text differ")
                envelopes[env] = (c["child"], canonical(value))
                owner = "next" if cpath.startswith(NEXT_ROOT + ".") else "orig"
                # A child launch names only its envelope. Any delegation issued
                # earlier in the episode with the same envelope, claimed or
                # not, finished or not, must be equivalent to this one; the
                # rule reads no claim or delivery state, so the outcome does
                # not depend on when children launch.
                for d in self.delegations:
                    if launch_prompt(d["task"], d["role"]) != env:
                        continue
                    if ((d["child"], d["script"], canonical(d["vars"]), canonical(d["input"]))
                            != (c["child"], owner, canonical(snapshot), canonical(value))):
                        raise StepFailure(
                            "ambiguous_delegation",
                            f"{cpath} would launch a child with the same message as delegation {d['instance']}, "
                            "which differs in child, script, variables or arguments; make the task text differ")
        if say:
            narration.append(say)
        delay[0] += step.get("delay", 0)
        blocks = [{"type": "text", "text": "\n\n".join(narration)}] if narration else []
        pending, sends = [], []
        for cpath, c, name, value in built:
            tid = ids.tool_use_id()
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": value})
            pending.append({"id": tid, "step": cpath, "name": name})
            if c["do"] == "delegate":
                sends.append({"task": value["task"], "role": value.get("role"), "input": value,
                              "script": "next" if cpath.startswith(NEXT_ROOT + ".") else "orig",
                              "vars": dict(self._scope(S)),
                              "child": c["child"], "parent_conversation": S.conv, "tool_use_id": tid,
                              "step": cpath, "conversation": None, "failed": False, "result": None,
                              "issued_at": now, "matched_at": None})
        S.pending = tuple(pending)
        st = self.handler_status.setdefault(ns, {}).setdefault(S.active, {"index": S.active})
        st["steps_emitted"] = st.get("steps_emitted", 0) + 1
        log.update(decision="tool_use", handler=S.active, step=path)
        log.pop("ends_with", None)
        return blocks, "tool_use", sends

    @staticmethod
    def _keep(f: StepFailure, narration: list, committed: tuple) -> StepFailure:
        """Attach to a failure what completed handlers committed to this
        reply, so the fallback still carries it."""
        f.committed = (list(narration[:committed[0]]), committed[1])
        return f

    @staticmethod
    def _repeat_body(path: str, step: dict, n: int) -> tuple:
        body = tuple(f"{path}.steps[{i}]" for i in range(len(step["steps"])))
        return body + (f"{REPEAT_MARK}{path}|{n}",)

    def _end_handler(self, S: ConvState, ns: str, end: str, now: float, narration: list, text: str, log: dict,
                     path: str, how: str):
        """Close the active handler. When a queued handler follows in the
        same turn, the text becomes narration and None is returned."""
        self._status(ns, S.active, status="done", finished=now, ended_with=end, ended_by=path)
        log.setdefault("finished", []).append({"handler": S.active, "step": path, "ends_with": end})
        log.update(decision=how, handler=S.active, step=path, ends_with=end)
        S.active, S.active_text = None, None
        if S.queued:
            narration.append(text)
            return None
        return [{"type": "text", "text": "\n\n".join(narration + [text])}], "end_turn", []

    def _tool_call(self, S: ConvState, path: str, c: dict) -> tuple[str, dict]:
        kind = c["do"]
        vals = self._values(S)
        try:
            if kind == "bash":
                name, value = "bash", {"command": render(c["command"], vals, path), "timeout": c.get("timeout")}
            elif kind == "read":
                name, value = "read", {"path": render(c["path"], vals, path), "offset": c.get("offset"),
                                       "limit": c.get("limit")}
            elif kind == "write":
                name, value = "write", {"path": render(c["path"], vals, path), "content": render(c["content"], vals, path)}
            elif kind == "edit":
                name, value = "edit", {"path": render(c["path"], vals, path),
                                       "edits": [{"oldText": render(e["oldText"], vals, path),
                                                  "newText": render(e["newText"], vals, path)} for e in c["edits"]]}
            elif kind == "tool":
                name, value = c["name"], render_value(c.get("input", {}), vals, path)
            else:  # delegate
                name = c.get("tool", DELEGATE_TOOL)
                extra = render_value(c.get("input", {}), vals, path)
                # Scripted delegation is blocking: T3's waiting mode, always.
                value = {"task": render(c["task"], vals, path), **({} if "mode" in extra else {"mode": "wait"}), **extra}
                if es_blank(value["task"]):
                    raise StepFailure("delegation_input", f"{path}: the task is blank once T3 trims it")
                if utf16_len(launch_prompt(value["task"], None)) > T3_TASK_MAX:
                    raise StepFailure("delegation_input", f"{path}: T3 refuses a task longer than {T3_TASK_MAX} characters")
        except RenderError as e:
            raise StepFailure("missing_variable", str(e)) from None
        if name not in S.offered:
            raise StepFailure("tool_not_offered",
                              f"{path} calls {name!r}, which this conversation was not offered "
                              f"(offered: {', '.join(sorted(S.offered)) or 'none'})")
        problem = schema_problem(S.schemas.get(name, {}), value)
        if problem:
            raise StepFailure("tool_arguments", f"{path} calls {name!r}: {problem}")
        return name, value

    def _error(self, log: dict, S: ConvState | None, code: str, detail: str) -> None:
        rec = {"time": time.time(), "seq": self.seq, "conversation": S.conv if S else None, "code": code, "detail": detail}
        self.errors.append(rec)
        log["errors"].append({"code": code, "detail": detail})
        print(f"scripted: error seq={self.seq} {code}: {detail}", file=sys.stderr, flush=True)

    def _usage(self, req: Request, base: Node | None, blocks: list) -> tuple[Usage, int, int]:
        """Plausible counts, not the real tokenizer: about 2.7 characters a
        token, a cache prefix that ends at Pi's cache marker, cache reads of
        what the conversation (or, for a first request, the same system
        prompt and tools) cached before."""
        if req.thread_type == "continue" and base is not None:
            new_total, new_cached = prompt_tokens(None, None, req.messages)
            total = base.context_tokens + new_total
            cached = base.context_tokens + new_cached if new_cached else 0
            prior = base.cached_upto
        else:
            total, cached = prompt_tokens(req.system, req.tools, req.messages)
            if base is not None:
                prior = base.cached_upto
            else:
                key = identity_hash(req.model, req.system, req.tools)
                prior = self.prefix_cache.get(key, 0)
                self.prefix_cache[key] = estimate_tokens(req.system) + estimate_tokens(req.tools)
        read = min(prior, cached)
        creation = max(cached - read, 0)
        uncached = max(total - cached, 0) if cached else total
        out = output_tokens(blocks)
        return Usage(uncached + 2, creation, read, out), total + out, cached

    # --------------------------------------------------- export / import

    def export_state(self) -> dict:
        """The whole episode as one JSON document, for `import_state` on a
        fresh server (a probe copy's)."""
        with self.lock:
            return copy.deepcopy({
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                # The scripts as their JSON text, so numbers keep the precision they were written with.
                "script": self.script.source if self.script else None,
                "next_script": self.next_script.source if self.next_script else None,
                "next_offset": self.next_offset,
                "next_active": self.next_active,
                "started": self.started,
                "seq": self.seq,
                "conv_seq": self._conv_seq,
                "unplaced_seq": self._unplaced_seq,
                "nodes": [_node_out(n) for n in self.nodes.values()],
                "by_hash": self.by_hash,
                "continue_retries": self.continue_retries,
                "create_retries": self.create_retries,
                "ledgers": self.ledgers,
                "handler_status": {ns: {str(i): v for i, v in d.items()} for ns, d in self.handler_status.items()},
                "vars": self.vars,
                "scopes": self.scopes,
                "delegations": self.delegations,
                "conversations": self.conversations,
                "errors": self.errors,
                "prefix_cache": self.prefix_cache,
                "announcements": self.announcements,
                "announced": (next(i for i, a in enumerate(self.announcements) if a is self.announced)
                              if self.announced is not None else None),
            })

    def import_state(self, doc: Any, next_script: Any = None) -> dict:
        """Replace this server's whole state with an export. Refused (with
        ValueError) once this server has answered a request, or when the
        document does not hold together. With `next_script`, that script's
        handler 0 answers the next main user message, its handler 1 the one
        after, and so on; its vars are added and its fallback is used from
        then on. Nothing is changed unless everything checks out."""
        with self.lock:
            if self.answered or self.seq or self.nodes:
                raise ImportRefused("this server has answered a request in its lifetime; import needs a fresh server")
            try:
                built = _restore(doc)
            except ScriptError as e:
                raise ValueError("the export's script does not load: " + "; ".join(e.errors)) from None
            except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
                raise ValueError(f"not a usable episode export: {type(e).__name__}: {e}") from None
            nxt = None
            if next_script is not None:
                if not isinstance(next_script, str):
                    raise ValueError("next_script must be a string holding the script's JSON text, so that its "
                                     "numbers keep the precision they are written with")
                if built["next_script"] is not None:
                    raise ValueError("the export already carries a replacement script; a second one is not supported")
                if built["script"] is None:
                    raise ValueError("a replacement script needs an export with a script")
                try:
                    known = set(built["script"].defined) | set(built["fields"]["vars"])
                    nxt = load_script(next_script, root=NEXT_ROOT, known_vars=known,
                                      reserved_tools=built["script"].delegate_names)
                except ScriptError as e:
                    raise ValueError("next_script: " + "; ".join(e.errors)) from None
            answered = self.answered
            self.script = built["script"]
            self.reset()
            self.answered = answered
            for k, v in built["fields"].items():
                setattr(self, k, v)
            self.next_script, self.next_offset = built["next_script"], built["next_offset"]
            self.next_active = built["next_active"]
            if nxt is not None:
                # Nothing of the replacement applies yet: its vars are
                # overlaid when its first handler starts.
                self.next_script, self.next_offset, self.next_active = nxt, len(self.ledgers["main"]), False
            return {"requests": self.seq, "conversations": len(self.conversations), "replies": len(self.nodes),
                    "user_messages": len(self.ledgers["main"]), "next_offset": self.next_offset}

    # ------------------------------------------------------------ state

    def _delegation_view(self, d: dict) -> dict:
        v = dict(d)
        node = self.nodes.get(d["message_id"])
        if d["conversation"] is not None:
            v["status"] = "claimed"
        elif d["failed"]:
            v["status"] = "failed"
        elif node is not None and node.delivered:
            v["status"] = "delivered"
        elif node is not None and node.failed_attempts:
            v["status"] = "undelivered"
        else:
            v["status"] = "sending"
        v["delivered"] = bool(node and node.delivered)
        v["failed_attempts"] = node.failed_attempts if node else 0
        return v

    def state(self) -> dict:
        with self.lock:
            return {
                "started": self.started,
                "requests": self.seq,
                "script": self.script.summary() if self.script else None,
                "user_messages": copy.deepcopy(self.ledgers),
                "handlers": {ns: [v for _, v in sorted(d.items())] for ns, d in self.handler_status.items()},
                "conversations": copy.deepcopy(self.conversations),
                "delegations": [self._delegation_view(d) for d in self.delegations],
                "announcements": copy.deepcopy(self.announcements),
                "vars": dict(self.vars),
                "scopes": copy.deepcopy(self.scopes),
                "errors": list(self.errors),
            }


def _state_out(S: ConvState) -> dict:
    return {"conv": S.conv, "kind": S.kind, "child": S.child, "ns": S.ns, "model": S.model,
            "offered": sorted(S.offered), "schemas": S.schemas, "active": S.active, "active_text": S.active_text,
            "queue": list(S.queue), "queued": [[i, t] for i, t in S.queued], "pending": list(S.pending),
            "tool_ids": list(S.tool_ids), "last_result": S.last_result, "script": S.script, "scope": S.scope,
            "hscope": S.hscope}


def _node_out(n: Node) -> dict:
    u = n.usage
    return {"msg_id": n.msg_id, "conv": n.conv, "hash": n.hash, "state": _state_out(n.state), "model": n.model,
            "blocks": n.blocks, "stop_reason": n.stop_reason,
            "usage": [u.input_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens, u.output_tokens],
            "created": n.created, "context_tokens": n.context_tokens, "cached_upto": n.cached_upto,
            "delivered": n.delivered, "failed_attempts": n.failed_attempts, "in_flight": n.in_flight,
            "seed": n.seed}


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(what)


def _int(v: Any, what: str, *, nullable: bool = False) -> int | None:
    if v is None and nullable:
        return None
    _need(isinstance(v, int) and not isinstance(v, bool) and v >= 0, f"{what} is not a non-negative integer")
    return v


def _str(v: Any, what: str, *, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    _need(isinstance(v, str), f"{what} is not a string")
    return v


def _obj(v: Any, what: str) -> dict:
    _need(isinstance(v, dict), f"{what} is not an object")
    return v


def _list(v: Any, what: str) -> list:
    _need(isinstance(v, list), f"{what} is not a list")
    return v


def _num(v: Any, what: str) -> float:
    _need(isinstance(v, (int, float)) and not isinstance(v, bool), f"{what} is not a number")
    return float(v)


def _str_map(v: Any, what: str) -> dict[str, str]:
    m = _obj(v, what)
    for k, x in m.items():
        _str(x, f"{what}.{k}")
    return dict(m)


def _restore(doc: Any) -> dict:
    """Rebuild every structure of an export, checking types and that every
    reference resolves. Raises ValueError (or ScriptError) on the first
    problem; builds nothing the caller sees until everything passed."""
    doc = copy.deepcopy(doc)
    _obj(doc, "the export")
    _need(doc.get("format") == EXPORT_FORMAT, f"format is not {EXPORT_FORMAT!r}")
    _need(doc.get("version") == EXPORT_VERSION, f"version is not {EXPORT_VERSION}")
    script = load_script(_str(doc["script"], "script")) if doc["script"] is not None else None
    vars_ = _str_map(doc["vars"], "vars")
    nxt = None
    if doc["next_script"] is not None:
        _need(script is not None, "next_script without a script")
        nxt = load_script(_str(doc["next_script"], "next_script"), root=NEXT_ROOT,
                          known_vars=set(script.defined) | set(vars_), reserved_tools=script.delegate_names)
    offset = _int(doc["next_offset"], "next_offset", nullable=True)
    _need((nxt is None) == (offset is None), "next_script and next_offset go together")
    steps = dict(script.steps) if script else {}
    if nxt:
        steps.update(nxt.steps)
    conversations = {}
    for c, rec in _obj(doc["conversations"], "conversations").items():
        _obj(rec, f"conversations.{c}")
        _need(rec.get("kind") in ("main", "child", "unplaced"), f"conversations.{c}.kind is not known")
        _str(rec.get("head"), f"conversations.{c}.head", nullable=True)
        _int(rec.get("replies"), f"conversations.{c}.replies")
        conversations[c] = rec
    scopes = {}
    for name, m in _obj(doc["scopes"], "scopes").items():
        scopes[name] = _str_map(m, f"scopes.{name}")
    ledgers = {}
    for ns, lst in _obj(doc["ledgers"], "ledgers").items():
        ledgers[ns] = [_obj(e, f"ledgers.{ns}") for e in _list(lst, f"ledgers.{ns}")]
    _need("main" in ledgers, "the main ledger is missing")
    handler_status: dict[str, dict[int, dict]] = {}
    for ns, d in _obj(doc["handler_status"], "handler_status").items():
        handler_status[ns] = {}
        for k, v in _obj(d, f"handler_status.{ns}").items():
            _need(isinstance(k, str) and re.fullmatch(r"[0-9]{1,9}", k) is not None,
                  f"handler_status.{ns} key {k!r} is not an index")
            handler_status[ns][int(k)] = dict(_obj(v, f"handler_status.{ns}.{k}"))

    def handlers_for(st: ConvState) -> list:
        if script is None or st.kind == "unplaced":
            return []
        if st.child is not None:
            owner = nxt if (st.script == "next" and nxt is not None) else script
            return owner.children.get(st.child, [])
        if nxt is None:
            return script.handlers
        return list(script.handlers[:offset]) + [None] * max(0, offset - len(script.handlers)) + nxt.handlers

    nodes: dict[str, Node] = {}
    for i, d in enumerate(_list(doc["nodes"], "nodes")):
        _obj(d, f"nodes[{i}]")
        mid = _str(d["msg_id"], f"nodes[{i}].msg_id")
        st = _obj(d["state"], f"node {mid}: state")
        w = f"node {mid}: state"
        S = ConvState(conv=_str(st["conv"], f"{w}.conv"), kind=_str(st["kind"], f"{w}.kind"),
                      child=_str(st["child"], f"{w}.child", nullable=True), ns=_str(st["ns"], f"{w}.ns"),
                      model=_str(st["model"], f"{w}.model"),
                      offered=frozenset(_str(x, f"{w}.offered") for x in _list(st["offered"], f"{w}.offered")),
                      schemas=dict(_obj(st["schemas"], f"{w}.schemas")),
                      active=_int(st["active"], f"{w}.active", nullable=True),
                      active_text=_str(st["active_text"], f"{w}.active_text", nullable=True),
                      queue=tuple(_str(x, f"{w}.queue") for x in _list(st["queue"], f"{w}.queue")),
                      queued=tuple((_int(q[0], f"{w}.queued"), _str(q[1], f"{w}.queued"))
                                   for q in _list(st["queued"], f"{w}.queued")),
                      pending=tuple(_obj(x, f"{w}.pending") for x in _list(st["pending"], f"{w}.pending")),
                      tool_ids=tuple(_str(x, f"{w}.tool_ids") for x in _list(st["tool_ids"], f"{w}.tool_ids")),
                      last_result=st["last_result"] if st["last_result"] is None else _obj(st["last_result"], w),
                      script=_str(st["script"], f"{w}.script"), scope=_str(st["scope"], f"{w}.scope"),
                      hscope=_str(st["hscope"], f"{w}.hscope"))
        _need(S.kind in ("main", "child", "unplaced"), f"{w}.kind is not known")
        if S.last_result is not None:
            # What `_values` and conditions read from the last tool result.
            _need(isinstance(S.last_result.get("text"), str), f"{w}.last_result has no text")
            _need(isinstance(S.last_result.get("is_error"), bool), f"{w}.last_result.is_error is not a boolean")
            ex = S.last_result.get("exit")
            _need(ex is None or (isinstance(ex, int) and not isinstance(ex, bool)), f"{w}.last_result.exit is not a number")
        _need(S.script in ("orig", "next") and (S.script == "orig" or nxt is not None), f"{w}.script is not known")
        _need(S.scope == "main" or S.scope in scopes, f"{w}.scope {S.scope} is missing")
        _need(S.hscope == "main" or (S.hscope == "next" and "next" in scopes), f"{w}.hscope {S.hscope} is not known")
        if S.active is not None:
            hs = handlers_for(S)
            _need(S.active < len(hs) and hs[S.active] is not None, f"{w}.active {S.active} is not a handler")
        for path in S.queue:
            if path.startswith(REPEAT_MARK):
                parts = path[len(REPEAT_MARK):].rsplit("|", 1)
                _need(len(parts) == 2 and re.fullmatch(r"[0-9]{1,4}", parts[1]) is not None,
                      f"node {mid}: repeat marker {path[:80]!r} has no round count")
                real, n = parts[0], int(parts[1])
                _need(real in steps and steps[real].get("do") == "repeat",
                      f"node {mid}: repeat marker names {real}, which is not a repeat step")
                _need(1 <= n <= steps[real]["max"], f"node {mid}: repeat round {n} is outside 1 to {steps[real]['max']}")
            else:
                _need(path in steps, f"node {mid}: queued step {path} is not in the script")
        for p in S.pending:
            for k in ("id", "step", "name"):
                _str(p.get(k), f"node {mid}: pending.{k}")
            _need(p["step"] in steps, f"node {mid}: pending step {p['step']} is not in the script")
        for j, b in enumerate(_list(d["blocks"], f"node {mid}: blocks")):
            bw = f"node {mid}: blocks[{j}]"
            _obj(b, bw)
            _need(b.get("type") in ("text", "tool_use"), f"{bw}.type is not text or tool_use")
            if b["type"] == "text":
                _str(b.get("text"), f"{bw}.text")
            else:
                _str(b.get("id"), f"{bw}.id")
                _str(b.get("name"), f"{bw}.name")
                _obj(b.get("input"), f"{bw}.input")
        u = [_int(x, f"node {mid}: usage") for x in _list(d["usage"], f"node {mid}: usage")]
        _need(len(u) == 4, f"node {mid}: usage needs four counts")
        _need(isinstance(d["delivered"], bool), f"node {mid}: delivered is not a boolean")
        nodes[mid] = Node(mid, _str(d["conv"], f"node {mid}: conv"), _str(d["hash"], f"node {mid}: hash"), S,
                          _str(d["model"], f"node {mid}: model"), list(_list(d["blocks"], f"node {mid}: blocks")),
                          _str(d["stop_reason"], f"node {mid}: stop_reason"), Usage(*u),
                          _num(d["created"], f"node {mid}: created"), _int(d["context_tokens"], f"node {mid}: context"),
                          _int(d["cached_upto"], f"node {mid}: cached"), d["delivered"],
                          _int(d["failed_attempts"], f"node {mid}: failed_attempts"),
                          _int(d.get("in_flight", 0), f"node {mid}: in_flight"),
                          _str(d["seed"], f"node {mid}: seed"))
    for n in nodes.values():
        _need(n.conv in conversations, f"node {n.msg_id}: conversation {n.conv} is missing")
        _need(n.state.conv == n.conv, f"node {n.msg_id}: its state names conversation {n.state.conv}, not {n.conv}")
    for c, rec in conversations.items():
        _need(rec["head"] is None or rec["head"] in nodes, f"conversation {c}: head {rec['head']} is missing")
        _need(rec["head"] is None or nodes[rec["head"]].conv == c,
              f"conversation {c}: its head {rec['head']} belongs to another conversation")
    by_hash = {h: [_str(x, "by_hash") for x in _list(v, "by_hash")] for h, v in _obj(doc["by_hash"], "by_hash").items()}
    create_retries = {h: [_str(x, "create_retries") for x in _list(v, "create_retries")]
                      for h, v in _obj(doc["create_retries"], "create_retries").items()}
    continue_retries = _str_map(doc["continue_retries"], "continue_retries")
    for v in [x for lst in by_hash.values() for x in lst] + [x for lst in create_retries.values() for x in lst] \
            + list(continue_retries.values()):
        _need(v in nodes, f"index names a missing reply {v}")
    delegations = []
    for i, d in enumerate(_list(doc["delegations"], "delegations")):
        _obj(d, f"delegations[{i}]")
        _need(_str(d.get("message_id"), f"delegations[{i}].message_id") in nodes,
              f"delegation {d.get('instance')}: reply {d.get('message_id')} is missing")
        _str(d.get("task"), f"delegations[{i}].task")
        _str(d.get("role"), f"delegations[{i}].role", nullable=True)
        _str(d.get("child"), f"delegations[{i}].child")
        _need(d.get("script") in ("orig", "next"), f"delegations[{i}].script is not known")
        _need(d["script"] == "orig" or nxt is not None, f"delegations[{i}] names a next script the export lacks")
        _need(_str(d.get("parent_conversation"), f"delegations[{i}].parent_conversation") in conversations,
              f"delegations[{i}].parent_conversation is missing")
        _need(d["parent_conversation"] == nodes[d["message_id"]].conv,
              f"delegations[{i}].parent_conversation is not the conversation of the reply that issued it")
        tid = _str(d.get("tool_use_id"), f"delegations[{i}].tool_use_id")
        _need(any(b.get("id") == tid for b in nodes[d["message_id"]].blocks if isinstance(b, dict)),
              f"delegations[{i}].tool_use_id is not a call in its reply")
        _str_map(d.get("vars"), f"delegations[{i}].vars")
        _obj(d.get("input"), f"delegations[{i}].input")
        _need(isinstance(d.get("failed"), bool), f"delegations[{i}].failed is not a boolean")
        _str(d.get("conversation"), f"delegations[{i}].conversation", nullable=True)
        _need(d["conversation"] is None or d["conversation"] in conversations,
              f"delegations[{i}].conversation is missing")
        _int(d.get("instance"), f"delegations[{i}].instance")
        delegations.append(d)
    announcements = []
    for i, rec in enumerate(_list(doc["announcements"], "announcements")):
        w = f"announcements[{i}]"
        _obj(rec, w)
        for k in ("index", "text", "after_request", "outcome"):
            _need(k in rec, f"{w} has no {k!r} (a null text is written as null, not left out)")
        _int(rec.get("index"), f"{w}.index")
        _str(rec.get("text"), f"{w}.text", nullable=True)
        _int(rec.get("after_request"), f"{w}.after_request")
        _need(rec.get("outcome") in ("pending", "arrived", "replaced"), f"{w}.outcome is not known")
        announcements.append(rec)
    a = _int(doc["announced"], "announced", nullable=True)
    _need(a is None or a < len(announcements), "announced is not an announcement")
    _need(a is None or announcements[a]["outcome"] == "pending", "announced names an announcement that is not pending")
    _need(sum(1 for r in announcements if r["outcome"] == "pending") == (0 if a is None else 1),
          "the pending announcements and `announced` do not agree")
    next_active = doc.get("next_active", False)
    _need(isinstance(next_active, bool), "next_active is not a boolean")
    _need(nxt is not None or not next_active, "next_active without a next script")
    _need(not next_active or "next" in scopes, "next_active without the replacement's scope")
    fields = {
        "started": _num(doc["started"], "started"), "seq": _int(doc["seq"], "seq"),
        "_conv_seq": _int(doc["conv_seq"], "conv_seq"), "_unplaced_seq": _int(doc["unplaced_seq"], "unplaced_seq"),
        "nodes": nodes, "by_hash": by_hash, "continue_retries": continue_retries, "create_retries": create_retries,
        "ledgers": ledgers, "handler_status": handler_status, "vars": vars_, "scopes": scopes,
        "delegations": delegations, "conversations": conversations,
        "errors": [_obj(e, "errors") for e in _list(doc["errors"], "errors")],
        "prefix_cache": {k: _int(v, "prefix_cache") for k, v in _obj(doc["prefix_cache"], "prefix_cache").items()},
        "announcements": announcements, "announced": announcements[a] if a is not None else None,
    }
    return {"script": script, "next_script": nxt, "next_offset": offset, "next_active": next_active, "fields": fields}


def _split_new(items: list[Item]) -> tuple[list[Item], list[str], int]:
    """Tool results, user messages, and the number of assistant blocks among
    the new items. Each user message on the wire stays one user message: its
    non-result blocks are joined, and blocks of different messages never are."""
    results: list[Item] = []
    users: list[list[str]] = []
    cur_msg: int | None = None
    stray = 0
    for it in items:
        if it.role == "system":
            continue
        if it.role == "assistant":
            stray += 1
            cur_msg = None
        elif it.kind == "tool_result":
            results.append(it)
        elif it.kind == "text" and es_blank(it.data["text"]):
            continue   # Pi drops whitespace-only text; it is no part of a message
        else:
            if cur_msg != it.msg_index:
                users.append([])
                cur_msg = it.msg_index
            if it.kind == "text":
                users[-1].append(it.data["text"])
    return results, ["\n".join(u) for u in users], stray


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n".join(b["text"] for b in system)
