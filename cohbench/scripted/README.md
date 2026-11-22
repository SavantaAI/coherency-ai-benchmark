# Scripted agent

A small server that speaks the part of the Anthropic Messages API that Pi
1.6.0 uses and answers with scripted tool calls and messages (decision
D-015). The gateway forwards to it in place of `https://api.anthropic.com`.
Pi, and later T3 Code, the watcher and the runner, run unchanged. Pi believes
it is talking to the real API.

Python 3.12, standard library only. Files:

| File | What it holds |
|---|---|
| `script.py` | The script format, its validator, templates, conditions and the regular expression check |
| `engine.py` | Episode state, how a request finds its handler and step, the policy |
| `wire.py` | Request parsing and checks, the event stream and response bodies, token estimates |
| `server.py` | The API listener, the admin listener, the request log |
| `ids.py` | Message, request and tool call ids in the real format |
| `schema.json` | The script format as a JSON Schema, for editors |
| `__main__.py` | `serve`, `validate`, `admin` |

## Running it

```sh
python -m cohbench.scripted serve --api 0.0.0.0:8080 --admin 127.0.0.1:8081 \
    --log-dir /var/log/scripted [--script script.json]
python -m cohbench.scripted validate script.json
python -m cohbench.scripted admin load script.json      # or `load -` to read stdin
python -m cohbench.scripted admin next 3 "the text"     # the next user message is number 3
python -m cohbench.scripted admin state
python -m cohbench.scripted admin reset
python -m cohbench.scripted admin export > episode.json   # the whole episode
python -m cohbench.scripted admin import episode.json [--next-script probe.json]   # into a fresh server
```

Loading a script resets the episode. `reset` clears the episode and keeps the
script. `validate` and `admin load` print every problem with its JSON path and
exit non-zero. `next` is the runner's announcement, described under
[The identity rules](#the-identity-rules); `export` and `import` are under
[Export and import](#export-and-import).

### Docker

```sh
docker build -f docker/scripted/Dockerfile -t sdev-scripted:dev .
docker network create --internal sdev-up-1          # gateway <-> scripted server only
docker run -d --name sdev-scripted-1 --network sdev-up-1 \
    -v "$PWD/runs/<run>/scripted:/var/log/scripted" sdev-scripted:dev
docker exec -i sdev-scripted-1 python -m cohbench.scripted admin load - < script.json
```

The image is `python:3.12.16-slim-bookworm` with this package only, running
as uid 10002. The gateway's Anthropic route then points at
`http://sdev-scripted-1:8080`, which needs `insecure_allow_http_upstream: true`
in the gateway config. The gateway still terminates TLS for
`api.anthropic.com` toward the agent, so the agent side is the topology in
`gateway/README.md` with no change. `tests/scripted/e2e_pi.py` builds the
whole thing, including the bridge-address flush for Docker before 28.0.

### Keeping the admin interface from the agent

Three things keep the agent's container away from the admin listener.

1. The admin listener binds `127.0.0.1:8081` inside the scripted server's
   own container. Nothing outside that network namespace can connect to it,
   the gateway included. The host reaches it with `docker exec`.
2. The scripted server sits on its own internal network with the gateway and
   nothing else. The agent's container is on a different internal network
   whose only peer is the gateway, so it can neither reach nor resolve the
   scripted server.
3. The gateway forwards only its allowlist (`POST /v1/messages`,
   `POST /v1/messages/count_tokens` and two Claude Code paths), and only to the
   upstream URL in its config, which names the API port. The API listener has
   no admin paths at all; `/state`, `/script`, `/reset`, `/next`, `/export`,
   `/import` and `/healthz` get 404 there
   (`test_admin_paths_do_not_exist_on_the_api_listener`).

`python -m gateway verify` passed for every agent container in the real check
below, including its probes of the bridge address and of published ports.

## The script format

A script is one JSON object. Version 1:

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | `1` |
| `handlers` | yes | Ordered list. Handler k answers the k-th user message of the episode |
| `children` | no | `{name: {handlers: [...]}}`, one handler list per delegated child |
| `vars` | no | Initial variables, `{name: "value"}` |
| `fallback` | no | The reply when the script cannot go on. Default "Sorry, I can't continue with this right now." Must not be blank (below) |
| `description` | no | Free text |
| `on_child_completion` | no | `{"text": "...", "capture": [...]}`: the reply to T3's delegation completion prompt (below). Default text "Noted."; captures run on the prompt and may not be `required` |

### Handlers

| Key | Meaning |
|---|---|
| `steps` | List of steps, run in order, one model reply per tool step |
| `final` | Text that ends the turn when the steps run out. Required unless the handler always replies (below) |
| `capture` | Captures from the user message, applied when the handler starts |
| `end` | `statement` (default) or `question`. A label for the log and the state: Pi has no question tool, so a question is plain text that ends the turn |
| `on_steer` | What happens when a user message arrives while this handler is still running. `finish` (default) runs the remaining steps, then the new message's handler in the same turn; this handler's final text goes ahead of the next reply as narration. `abandon` drops the remaining steps and starts the new message's handler at once. Results that arrive with the steer are still recorded and their captures still run: a capture that matches sets its variable as usual, and a required capture that misses is not a failure |
| `expect` | Optional condition that must hold for the user message (`contains` and `regex` apply to it). If it does not hold, the reply is the fallback and the mismatch is logged. Use it to catch a script that has drifted out of step with the episode |
| `final_delay` | Seconds to wait before sending the final reply |
| `id` | A label for the log |

A handler always replies, and needs no `final`, when one of its steps always
replies. A step always replies when it is a `reply`; a `branch` with an `else`
where every case and the `else` always reply; or a `repeat` whose steps
always reply, since its first round then ends the handler. Steps run in
order, so anything after such a step is unreachable. No other form counts.

**Blank** means what Pi's client means: what ECMAScript's
`String.prototype.trim()` leaves empty. Its whitespace is the WhiteSpace set
(tab, vertical tab, form feed, space, U+00A0, U+FEFF and every Unicode Zs
character) and the LineTerminator set (line feed, carriage return, U+2028,
U+2029). One helper (`es_strip` and `es_blank` in `wire.py`) decides it
everywhere: emitted narration, turn-ending text, the fallback's validation at
load, incoming text blocks and the canonical history. It differs from
Python's `str.strip()`: U+FEFF is blank, while U+0085 and U+001C to U+001F
are not; U+200B is not blank in either.

Handler k is chosen by the k-th user message counted across the whole
episode: every main conversation shares one counter, including conversations
in a fresh container after a reset. A child counts its own messages. Each
user message on the wire is one user message, even when two arrive in one
request.

### Steps

Every step has `do`. Tool steps may also have `say` (narration sent as a text
block before the tool call, which does not end the turn), `delay` (seconds to
hold the reply) and `capture`.

| `do` | Fields | Tool call it makes |
|---|---|---|
| `bash` | `command`, `timeout` (default null) | `bash {"command", "timeout"}` |
| `read` | `path`, `offset`, `limit` (default null) | `read {"path", "offset", "limit"}` |
| `write` | `path`, `content` | `write {"path", "content"}` |
| `edit` | `path`, `edits: [{oldText, newText}]` | `edit {"path", "edits"}` |
| `tool` | `name`, `input` | any tool the conversation was offered, as given, except the delegation tool: `mcp__t3-code__delegate_task` and any alias a `delegate` step names in `tool` may be called only through `do: delegate` (refused at load otherwise, in any handler) |
| `delegate` | `child`, `task`, `input` (extra fields such as `role`, `title`, `target`, `timeoutMs`), `tool` (default `mcp__t3-code__delegate_task`). Main handlers only: delegation is one level deep, and a `delegate` in a child's handlers is refused at load. `role` must be one of T3's roles (`implementation`, `research`, `review`, `design`, `test`, `general`). Delegation is blocking: the call always carries `mode: "wait"`, and any other mode is refused at load (below) | `mcp__t3-code__delegate_task {"task", "mode": "wait", ...input}` |
| `parallel` | `calls: [tool steps]`, `say`, `delay` | several tool calls in one reply |
| `branch` | `cases: [{when, steps}]`, `else` | none; runs the first case whose condition holds |
| `repeat` | `max` (1 to 1000), `steps`, `until` | none; runs `steps` up to `max` times, stopping early once `until` holds after a round |
| `say` | `text` | none; narration for the next reply. Narration that renders blank is skipped (logged as `skipped_narration`), the same for a tool step's `say` |
| `reply` | `text`, `end` | none; ends the turn with this text |
| `set` | `var`, `value` | none; sets a variable |

The argument names are Pi's own, read from its tool list in the stored
requests: `bash` requires `command` and `timeout`, `read` requires `path`,
`offset` and `limit`, all with `strict: true` and no extra keys. The server
fills the nullable ones with null, as the real model did in the stored runs.

**Conditions.** Each is an object with one key.

| Condition | Holds when |
|---|---|
| `{"exit": N}` | The previous tool result's exit status is N |
| `{"is_error": true}` | The previous tool result is flagged as an error |
| `{"contains": "text"}`, `{"regex": "pattern"}` | The previous tool result's text contains it or matches it. In `expect`, the user message's text |
| `{"var_set": "name"}` | The variable has a value |
| `{"var": {"name": "x", "equals": "text"}}` | Also `contains` and `regex`. False when `x` has no value |
| `{"var": {"name": "x", "lt": 10}}` | Also `le`, `gt`, `ge`, `eq`, `ne`. The other side is a number or `{"var": "y"}`. Both sides compare as exact decimals, so 9007199254740992 is less than 9007199254740993 and "0.3" equals 0.3. A literal keeps the precision it was written with in the script's JSON text (`0.30000000000000001` stays that, through load, export and import), and a literal outside the range a decimal can hold (`1e-99999999999999999999`) is refused at load; a script given as a Python object compares its floats by their shortest decimal form. A variable's text must be a plain decimal number (`-12`, `0.5`, `1e3`); anything else, no value, or a number the decimal library cannot hold (`1e9999999999999999999`) is a logged `not_a_number` and the fallback |
| `{"not": ...}`, `{"all": [...]}`, `{"any": [...]}` | As named |

The exit status comes from Pi's result text: Pi's bash tool returns the plain
output on exit 0, and otherwise the output, a blank line and
`Command exited with code N`, flagged `is_error`. A timeout or abort is an
error with no code, so `exit` matches nothing for it. For other tools exit is
0 when the result is not an error and unknown when it is.

**Captures** run on a tool step's own result, or for a handler's `capture`, on
the user message: `{"var": "job", "regex": "job id: (\\S+)", "group": 1,
"required": false}`. The group defaults to 1 when the pattern has a group and
to the whole match otherwise. A capture misses when the pattern does not
match or the chosen group took no part in the match (`x(y)?` against `x`). A
miss leaves the variable as it was and is logged; a missed `required` capture
gives the fallback.

**Templates.** `{{name}}` in any command, path, content, text, condition text
or tool input string is replaced by the variable's value. The main
conversations share one set of variables for the whole episode: a value
captured in handler 0 is there for handler 5, in another container too. Each
delegated child has its own set. It starts as a copy of its parent's
variables as they were when the delegating reply was decided, and its writes
(captures, `set`) stay its own: neither the parent nor a sibling ever sees
them. A parent learns from a child only through captures on the delegation's
tool result, which run in tool-call order, so when two parallel delegations
capture into the same name the later call's value is the one kept. After an
import with a replacement script, the replacement's handlers get a scope of
their own when the first of them starts: a copy of the main variables at
that moment with the replacement's `vars` laid over it. Original handlers
and their children keep reading and writing the original scopes. Built-ins: `{{user_message}}` is the text
of the message the running handler answers, `{{last_output}}` and
`{{last_exit}}` hold the conversation's previous tool result. The validator
refuses a template that names a variable no capture, `set` or `vars` entry
defines. A variable that is defined but has no value yet when a step runs
gives the fallback.

**Regular expressions** must compile, and the validator refuses a repeat that
can match more than once around another repeat that can, when either count is
unbounded: `(a+)+`, `(\w+\s?)+`, `(a*)*`. Python's backtracking engine can
take exponential time on such a pattern when the text nearly matches.
Possessive repeats and atomic groups count as no repeat around what they hold
(`(?>a+)+b` is allowed). The check does not see overlapping alternatives
inside a repeat (`(a|ab)*`). Patterns run on at most the first 1 MiB of a
text, and they run while the episode is locked, so a slow pattern the check
misses delays every conversation until it finishes.

**Validation is total.** Any input to the validator, however malformed (a
list or null where a string belongs, NaN or an infinite number, an integer
of 400 digits, text that is not UTF-8, a lone surrogate, nesting deep enough
to exhaust the stack), gives a list of path-specific errors and nothing else.
Integers are range-checked as integers, never converted to floating point.
`schema.json` agrees with the validator on nulls: `final`, `id` and
`description` must be strings when present. A field-by-field fuzz test
checks this.

### Worked example

The agent runs a workspace tool, reads the job id it printed, and asks the
user which queue to use. It acts on the answer, including an answer the user
cannot give, and has a child move the job. Then it tunes the job in a bounded
loop, keeping the best operation count.

```json
{
  "version": 1,
  "description": "submit, ask, act on the answer, tune in a bounded loop",
  "vars": {"best": "1000000"},
  "handlers": [
    {
      "id": "submit",
      "on_steer": "abandon",
      "end": "question",
      "steps": [
        {"do": "bash", "command": "./bin/submit --batch nightly", "say": "Submitting the batch.",
         "capture": [{"var": "job", "regex": "job id: (\\S+)", "required": true}]},
        {"do": "branch", "cases": [
          {"when": {"not": {"exit": 0}}, "steps": [
            {"do": "reply", "text": "Submitting failed with exit {{last_exit}}. Should I retry?", "end": "question"}]}
        ]},
        {"do": "bash", "command": "./bin/status {{job}}", "delay": 3}
      ],
      "final": "Job {{job}} is {{last_output}}. Fast queue or slow queue?"
    },
    {
      "id": "route",
      "capture": [{"var": "queue", "regex": "\\b(fast|slow)\\b"}],
      "steps": [
        {"do": "branch", "cases": [
          {"when": {"var": {"name": "user_message", "contains": "I don't know what that refers to"}},
           "steps": [{"do": "reply", "text": "Job {{job}} is the nightly batch you asked for. Fast or slow?", "end": "question"}]},
          {"when": {"not": {"var_set": "queue"}},
           "steps": [{"do": "reply", "text": "Fast or slow?", "end": "question"}]}
        ]},
        {"do": "delegate", "child": "mover", "task": "Run ./bin/move {{job}} {{queue}} in /work/project and reply with its output only.",
         "input": {"mode": "wait", "role": "general", "title": "Move job"}}
      ],
      "final": "Moved {{job}} to the {{queue}} queue."
    },
    {
      "id": "tune",
      "steps": [
        {"do": "repeat", "max": 5, "until": {"var": {"name": "gain", "equals": "none"}}, "steps": [
          {"do": "bash", "command": "./bin/tune {{job}} && ./bin/measure {{job}}",
           "capture": [{"var": "ops", "regex": "operations: (\\d+)", "required": true}]},
          {"do": "branch", "cases": [
            {"when": {"var": {"name": "ops", "lt": {"var": "best"}}},
             "steps": [{"do": "set", "var": "best", "value": "{{ops}}"}, {"do": "set", "var": "gain", "value": "some"}]}
          ], "else": [{"do": "set", "var": "gain", "value": "none"}]}
        ]}
      ],
      "final": "The best I reached is {{best}} operations."
    }
  ],
  "children": {
    "mover": {"handlers": [
      {"steps": [{"do": "bash", "command": "cd /work/project && ./bin/move {{job}} {{queue}}"}], "final": "{{last_output}}"}
    ]}
  }
}
```

## How a request finds its conversation and handler

Every reply the server sends is a node, named by its message id, in one
conversation. A conversation's identity is its model, its system prompt and
its tool list (cache markers removed, since Pi moves them). A node records a
hash of that identity and of the conversation's blocks up to and including
the reply. The hash keys a text block by its trimmed text, a tool call by its
id and name, and a tool result by its id, text and error flag, and leaves out
system-role messages, so it does not depend on details Pi changes between a
thread `continue` and a full resend.

### The identity rules

The server places each request by these rules, in this order. It never
chooses between two candidates: when a rule cannot decide, the request is an
error. Every request's log line has `identity`: the rule, the decision and
the reason.

**a. A `continue`** is identified by the message it continues from. A second
continue from the same message with the same new content is a retry and gets
the same reply with the same ids. "The same" is decided by a fingerprint of
the new messages exactly as sent: every message in order with its role and
every other message-level field (`output_config` included), every block with
its type and all its fields, text byte for byte (a trailing
space counts), system-role blocks and tool results included. Only the cache
markers the protocol defines are left out, where it puts them: `cache_control`
on a content block, on a block inside a tool result, and on a tool
definition. A key of that name inside tool input or other data counts. The
same fingerprint decides retries for every kind of request (rules a, c and
d). It shares no code with the history hash, whose trimmed, system-free
forms serve only to find where a request belongs. Two conversations with equal histories are still told
apart, because the retry key is the previous message id plus the
fingerprint. Any other continue from a message that has already been
continued gets the API's 400 `thread_already_continued`; Pi then resends the
whole conversation, which rule c places. Nothing else is a retry of a
continue. Refusals under rule a (`thread_not_found`,
`thread_fingerprint_mismatch`, `thread_already_continued`, a tool-pairing
error) are logged with `identity` like every other decision.

Delegation is one level deep: main handlers delegate, a child's handlers may
not (the validator refuses it with the step's path). The task families need a
main agent and its subagents, not grandchildren.

**b. A child's launch.** A `create` with no assistant content whose first
user message is the launch envelope of an unclaimed delegation is that
delegation's child, never a replay. The envelope is what T3 builds from the
call, not what the script wrote. Read from T3's source at the pinned
release's ancestor:

- T3 decodes `task` with its `TrimmedNonEmptyString` schema
  (`packages/contracts/src/baseSchemas.ts`): JavaScript `trim()`, and it must
  be non-blank and at most 120,000 characters
  (`packages/contracts/src/orchestratorMcp.ts`). `role` must be one of its
  literals; nothing else of the call reaches the child's message.
- `taskPrompt` (`apps/server/src/mcp/delegatedTaskModes.ts`) is the trimmed
  task alone when the role is absent or `general`, otherwise `Act as the
  <role> sub-agent for this task.`, a blank line and the task.
- The child's first message is `withMessageSenderNote`
  (`packages/contracts/src/orchestrationV2.ts`) of that prompt, sent by the
  parent's thread: T3's sender note (`[T3 Code: the agent in T3 thread "<id>"
  sent this message, not the user.]`, or the same with `on linked environment
  "<id>"`), a blank line and the prompt; but no note at all when the prompt
  starts with a slash command or a skill mention such as `$deploy` (T3's
  `LEADING_COMMAND`). That case is matched on the text as T3 sends it. Pi can
  handle an installed extension command without a model request, or expand a
  registered skill or prompt template before sending, and the server then
  never sees the launch text it expects. Scripts written for this project
  use plain-text tasks.

The server builds the same envelope with one function (`launch_prompt`
and `is_child_launch`) and uses it for the load-time collision check, the
issue-time equivalence check, launch matching and the stream seed, so a task
written with a trailing newline launches its child, and two calls whose
tasks differ only by what T3 trims away collide. The whole message must
equal the envelope. Each delegation instance gets its own
conversation and its own message counter, starting at handler 0 of the named
child (`child:<name>/<instance>` in the state), and later messages to that
child conversation advance within it. So a `repeat` that delegates to the
same child twice gives two children that each start at handler 0. A launch
names only its envelope, so within one episode every delegation whose
envelope equals that of a delegation issued earlier, claimed or not,
finished or not, must be equivalent to it: the same child name, the same
owning script, equal variable snapshots, and identical tool input (target
model, every other argument and the task as written), compared as canonical
JSON so that `1`, `1.0` and `true` are three different values. The same
holds between calls of one `parallel` step. If it is not, the step that
issues it fails with `ambiguous_delegation` (logged, fallback reply) before
anything is sent. The rule reads no claim or delivery state, so the outcome
is the same whether the earlier child has launched or not. A script that
wants the same child to do the same task with different variables or
arguments has to make the task text differ. Equivalent delegations are interchangeable, and a
launch takes the earliest unclaimed one. A script that sends
the same task with the same role to two different children in one
`parallel` step is refused at load: T3 starts both with the same message, so
which child is which cannot be decided. Templates that render to the same
task at run time are refused when the step runs (`ambiguous_delegation`), and
a launch that matches unclaimed delegations to different children is an
error (`ambiguous_child_launch`).

**Delegation is blocking.** A `delegate` step always sends T3's waiting
mode, `mode: "wait"` (the saved `delegate_task` schema in
`tests/scripted/fixtures/subagent/` defines `mode` as `"async"` or `"wait"`,
default async), and a script that asks for another mode is refused at load.
The reason: in async mode T3 reports a child's end to the parent as a new
prompt it writes itself (`ProviderContinuationService.ts`,
`delegatedCompletionText`), which the parent's Pi receives like a user
message and which would be mistaken for the simulated user. A wait can still
end on T3's side (it gives up after `timeoutMs`, at most 45 s), and T3 then
sends that prompt later anyway. So the server recognises it by its exact
shape, `Delegated task <id> reached a terminal state. Use task_status with
taskId <id> to read the result.` or the plural `Delegated tasks <id>, <id>
reached terminal states. Use task_status with each taskId to read the
results.` (T3 adds no sender note: it is dispatched by T3's server with no
sender thread). Such a message is never counted as a user message: it is
logged as `delegation_completion`, it advances no handler, and when nothing
else arrives with it the reply is the script's `on_child_completion` text
(default "Noted."), with that block's captures applied to the prompt. No saved
traffic contains such a prompt; its shape comes from T3's source only.

**c. A resume.** A `create` that carries assistant content (Pi sends one
after its process restarts, with `pi -p --continue`, after a thread error,
and in a probe copy) resumes the conversation whose recorded history equals
the request's, identity included, up to the request's last assistant
message, and only at that conversation's latest reply. No match
(`unknown_history`), several matches (`ambiguous_history`), or a match at an
earlier reply when the conversation has replied since (`stale_history`) is an
error. A resend of the same resume with the same new content after the
history (the fingerprint above) is a retry; one that differs by a byte is a
new request, which at a reply the conversation has moved past is
`stale_history`, never the old reply. A resume with
another system prompt or other tools than the conversation had is unknown
history. A server that never answered the history, such as a fresh server
behind a probe copy, answers `unknown_history` unless it was seeded with an
import (below).

**d. A main-conversation message.** Any other `create` starts a main
conversation.

- If the runner has announced the next message with its text
  (`POST /next {"index": k, "text": "..."}`), a create with that text is
  message k, even when it is identical to an earlier create. A create with
  other text is not the announced message: it is a retry of the one earlier
  create it is identical to, or an error (`unannounced_message`) when there
  is no such create or there are several. The announcement stays pending.
  "Identical" means the same identity (model, system prompt, tools) and the
  same fingerprint of the messages: a create whose user text differs by one
  byte is not identical.
- If the runner announced message k without text, the next create is
  message k.
- Without an announcement, a create identical to exactly one earlier create
  is a retry; identical to several (possible only after announcements) it is
  an error (`ambiguous_retry`); otherwise it is a new message. Once the
  runner has used announcements, a new message it did not announce is still
  counted and is logged as `unannounced_message`.

`/next` is refused unless k is the number of main user messages counted so
far. A user message in a `continue` or a resume that differs from the
announced text is counted, logged as `announced_text_mismatch`, and leaves
the announcement pending.

**Without announcements, as when Pi is run by hand, a new conversation that
opens exactly like an earlier one is taken for a retry of it.**

A request placed by an error rule gets the fallback reply in a conversation
of its own (kind `unplaced`), counts no user message, and anything that
continues from that reply is placed the same way.

### Delivery

The server registers each attempt to send a reply before writing its first
byte, and reports how it ended. Delivery only moves forward: once any attempt
of a reply was delivered, a later failed attempt does not undo it. A
delegation is claimable from the moment the reply that contains it is
decided, unless every attempt to send that reply so far has been reported
failed and none is in flight; and it stops being claimable when the parent's
tool result for it is an error. So a child launch that arrives between two
attempts' reports is placed the same way whichever report came first.

### Tool results, captures and steers

User messages are counted first. Then a request's tool results are processed
in two passes: every result and every delegation's outcome is recorded, and
only then do captures run. A required capture that misses marks its handler
failed (`error` in the state). A correction that arrived in the same request,
or earlier under `finish`, still runs its own handler; the reply comes from
that handler and the failure is in the log. Narration and delay belong to the
handler that produced them: what a failed handler had not yet sent (its
`say` text, its delays) is dropped before the correction's handler runs,
while a handler that completed in the same reply keeps its final text and
delay. Only when no handler is left to run is the reply the fallback, and
that reply still carries the text and delay that completed handlers
committed to it, followed by the fallback text.

A user message that arrives while a handler is still running, meaning its
last reply was a tool call, is a steer. Pi delivers a steer as a separate user
message after the tool result, between tool calls; the stored steering run
shows this, and the replay test feeds it to the server. Under `abandon` the
running handler and any corrections queued behind it are dropped (`abandoned`
and `skipped`). The abandoned handler's results that arrive with the
correction are still recorded and their captures still run: one that matches
sets its variable, one that misses is not a failure even when required.

### Export and import

A probe (D-009) snapshots the agent's container at a turn boundary, starts
the copy behind its own fresh scripted server, and asks the copy one
question. The copy's Pi resends the whole conversation as a `create`. For the
copy's server to recognise that history, it is seeded with the main server's
state.

- `GET /export` (CLI `admin export`) returns the whole episode as one JSON
  document (`format: "cohbench-scripted-episode"`, `version: 1`): the loaded
  script, every conversation with its identity, every reply with its
  conversation state and history hash, the retry indexes, the user message
  counters, handler states, variables, delegations with their delivery
  record, announcements and the error list.
- `POST /import {"state": <export>, "next_script": "<script JSON text>"}`
  (`next_script` optional; CLI `admin import FILE|- [--next-script FILE]`)
  replaces all state of a
  fresh server. A server stops being fresh with the first request its model
  listener answers in its lifetime, `count_tokens` and refused requests
  included, and `/reset` does not make it fresh again; on such a server the
  import is refused with 409 and changes nothing. A document that does not hold together (a reply
  it names is missing, a queued step is not in the script) is refused with
  400 and changes nothing. The checks cover what the server reads back, not
  every way a hand-edited document could be wrong:
  replies (each in the conversation its state names, each stored block a
  text block with `text` or a tool call with `id`, `name` and `input`, and a
  stored last tool result with its `text`, `is_error` and `exit`),
  conversations (each
  head a reply of that conversation), the replacement's scope when it has
  started, queued steps and `@repeat` round counts, the active
  handler, scopes, announcement records (every key present; a null text is a
  value) and the pending one, and each delegation's reply, tool call, parent
  conversation (which must be the conversation of the reply that issued it)
  and owning script.
- With `next_script`, that script answers what comes next: its handler 0
  answers the next main user message after the import, its handler 1 the one
  after, and so on, so a probe needs one handler, not padding. Its children
  serve the delegations its own handlers start. Nothing of it applies until
  its first handler is started by a main user message that arrives after the
  import: only then does it get its scope, the main variables with its `vars`
  laid over them (above); the main variables themselves never change. A
  conversation that was running at export keeps the script and handler it was
  running, children included, and behaves exactly as it would on the
  original server: its variables, its `on_steer` policy and its fallback text
  come from the script that owns the handler. Its templates may use every
  variable of the episode and of the original script. It is checked under the
  root `$next`, and an export that already carries a replacement script
  refuses a second one.
- Scripts travel as JSON text end to end, so numeric literals keep their
  written precision: the export holds each script's source text,
  `next_script` must be a string holding the replacement's JSON text (an
  inline object is refused with 400 rather than silently rounded), and the
  CLI reads the replacement file's text and passes it through unparsed, as it
  forwards a wrapper file unmodified. A script's hash is over that exact text.

After an import, the same request gets the decision the original server
would have given: the same rule, handler, step, reply text and tool inputs,
with new ids. Export, import and export again gives the same document. The
copy's server shares nothing with the main one, so whatever the copy does
leaves the main episode as it was; a test checks the main server's export
before and after.

### When the script cannot go on

The reply is the fallback text as an ordinary `end_turn` message. That
covers an exhausted script, no script loaded, a tool the conversation was not
offered, tool arguments that break the offered schema's `required` or
`additionalProperties: false`, a variable with no value, a numeric comparison
on a non-number, a required capture that missed when no correction is
waiting, an `expect` that does not hold, a handler that takes more than
100,000 steps without a reply (a repeat with no tool call inside), and every
identity error of rules b to d. A turn-ending text (`final`, `reply`, a
question) that renders blank is a `blank_reply` failure:
Pi would drop the whole message from its history. The step limit is a failure of the handler
like the others: it is marked `error`, its unsent narration and delay are
dropped, and a queued correction runs with a step budget of its own. The cause goes to the request
log, to `errors` in the state, and to stderr. It never becomes an HTTP 500 or
a hang. An unexpected exception in the server's own code is the one case
that answers 500, in the API's error shape; the fuzz tests found none.

The tools a conversation was offered are the `create` request's `tools`
without the deferred ones (`defer_loading: true`), plus every
`tool_addition` and minus every `tool_removal` in system-role messages since.
In the stored runs T3's tools such as `send_to_user` reach Pi this way after
a `tool_search` call. A script that calls them must call `tool_search` first,
as the real model did.

## What Pi sends and needs

Read from the driver proof's stored bodies (run `2026-11-21T210951-74da66`,
104 streamed responses) and from Pi's client
(`pi-ai/dist/api/anthropic-messages.js` in the 1.6.0 image).

Requests. Always `POST /v1/messages?beta=true` with `stream: true`, `model`,
`max_tokens: 128000` and `thinking: {"type": "disabled"}`. The
`anthropic-beta` header lists `message-threads-2026-08-12` and
`inline-tools-2026-09-15` among four. The first request of a session has
`thread: {"type": "create"}`, `system` and nine `tools` under T3 Code, or five
when Pi runs alone: `read`, `bash`, `edit`, `write` and the deferred
`__pi_deferred_placeholder__`. Every later request in the same process has
`thread: {"type": "continue", "previous_message_id": <the last reply's id>}`
and only the new messages, with no `system` or `tools`. Pi puts a cache marker
on the newest block. Tool results arrive as one user message per batch of
calls; a steer arrives as a further user message after it. In all 506 Pi
requests of the stored runs Pi never called `count_tokens` and never sent
`stream: false`.

Responses. Pi needs the named SSE events (`event:` lines; it skips events not
in its list, so `ping` is ignored), a `message_start` whose `message.id` it
keeps as the next `previous_message_id`, and a `message_start.model` equal to
the request's model. A different model marks the reply as a fallback model's,
and Pi then does not continue the thread from it. It needs a `message_delta`
with `stop_reason` and a `message_stop`; without them it fails the turn. It
reads usage from both events: `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`,
`cache_creation.ephemeral_1h_input_tokens` and
`output_tokens_details.thinking_tokens`. The gateway also requires the four
counters on every reply and reported `usage_status: complete` for all of them
in the real check. Of the response headers Pi reads `request-id`,
`retry-after`, `retry-after-ms` and `x-should-retry`.

How Pi rebuilds history when it resends a conversation. Read from Pi
1.6.0's client (pi-ai `dist/api/anthropic-messages.js` `convertMessages` and
`dist/api/transform-messages.js` `transformMessages`; the harness image and
the global bun install hold identical copies). What it drops or rewrites,
and how the server's canonical history agrees with each. Where the last
column names no limit, a test resends a history built by Pi's rules and
requires a resume. Those tests build the resend with `pi_resend` in
`tests/scripted/test_round6.py`, a Python reconstruction of those two
functions written from and checked against pi-ai 1.6.0, not Pi's own code;
it starts from Pi's native form (separate tool result messages) where that
matters:

| Pi on resend | The server |
|---|---|
| Drops text blocks that `trim()` leaves empty, in any role | Gives them no history key, never sends one (blank narration is skipped, a blank turn-ending text is `blank_reply`) and does not count a blank user message, all by ECMAScript's whitespace (above) |
| Drops an assistant message left with no blocks | Never sends one, for the same reason |
| Keeps text otherwise as received, trailing whitespace included | Keys text trimmed, so it matches either way |
| Sends a tool call's input as `arguments ?? {}` | Keys a tool call by id and name only |
| Joins a tool result's text blocks into one string with newlines | Keys a tool result by that joined text |
| Puts consecutive tool results in one user message | Receives them that way already: the API's pairing rule requires all results of a reply at the start of the next message, so the server never sees them split. Keys block by block. The test builds the resend from Pi's native form, one tool result message each |
| Sends older user messages as plain strings, the newest as a block with a cache marker | Keys string and block forms alike and ignores cache markers |
| Moves system-role messages (tool additions) to just before the next assistant message | Leaves system-role messages out of the history hash |
| Adds a "No result provided" error result for a tool call left without one | Takes it as a new tool result after the reply it answers |
| Drops an assistant message whose stream ended in error or was aborted | Cannot follow: a later full resend ends at the reply before it, which is no longer the conversation's latest, so it gets `stale_history` (Pi's own retry repeats the continue instead, which rule a answers) |
| Drops thinking blocks without a signature, converts others | Never sends thinking blocks |
| Moves images of an error tool result after all results, with a caption text | Would count the caption as a user message; Pi's tools did not produce such a result in the stored runs or the real check |

Thread errors. Pi reads `error.details.error_code`. For `thread_not_found`,
`thread_already_continued`, `thread_continue_in_flight` and
`thread_fingerprint_mismatch` it resends the request in full as a new
thread. The server sends three of these codes: `thread_not_found` for a
`previous_message_id` it never sent, `thread_fingerprint_mismatch` for a
continue with another model, and `thread_already_continued` for a different
continue from a reply that has already been continued. Whether Pi's full
resend then recovers is decided by rule c, which stays strict:

- It recovers when the resend's history, with the same model, system prompt
  and tools, ends at a conversation's latest reply. That covers a
  `thread_not_found` caused by the client (a reply id it lost) when this
  server did answer the history.
- It gets the fallback (`unknown_history`) after `thread_fingerprint_mismatch`,
  because the resend carries the new model and no conversation has that
  identity; and after `thread_not_found` on a server that never answered the
  history (a restarted or unseeded server; seed it with an import).
- After `thread_already_continued` it gets the fallback (`stale_history`),
  because the history ends at a reply the conversation has moved past, unless
  it is an identical resend of an earlier resume, which is a retry.

These paths are tested in process only.

## What is implemented

- `POST /v1/messages`, streaming and not. The streamed events follow the
  stored ones: `message_start` with the message's keys in the real order,
  `content_block_start`, one `ping` after the first block start,
  `content_block_delta` (`text_delta` or `input_json_delta`),
  `content_block_stop`, `message_delta` and `message_stop`. Text blocks come
  before tool calls. A tool call starts with `input: {}` and
  `caller: {"type": "direct"}`. Its input is framed as in all 50 stored tool
  calls: an empty first delta, the body in pieces, and a last delta that is
  the closing brace, joined by the closing quote when the last value is a
  string (`"}`, 10 of the stored calls) and on its own otherwise (`}`, 39). A
  call with no arguments is one empty delta and nothing else, as the stored
  `orchestrator_capabilities` call was. The joined input uses `", "` and
  `": "` separators. Each event's JSON has a run of spaces before its closing
  brace, as the real ones do.
- `POST /v1/messages/count_tokens`, answering `{"input_tokens": N}`.
- The thread fields, server-side history per thread, and three thread
  error codes: `thread_not_found`, `thread_fingerprint_mismatch` and
  `thread_already_continued`.
- The API's tool-pairing rule. A tool call without a result at the start of
  the next message, or a result for no call, gets 400 with the real API's
  wording, as far as it is known here.
- Errors in the stored shape, `{"type": "error", "error": {"type",
  "message"}, "request_id"}`, with `request-id` and `x-should-retry: false`
  headers. Every field the server reads is checked before use: 400 for bytes
  that are not UTF-8 JSON (NaN, a number too large for a float such as
  `1e400`, a lone surrogate, too deep), a missing or
  mistyped `model`, `max_tokens` (on `count_tokens` too), `messages`,
  `stream`, `system`, `tools` and their schemas' `required` and `properties`,
  `thread`, and every content block (tool call ids and names, tool result ids,
  error flags and content, text, `tool_addition` and `tool_removal`); 401
  without `x-api-key` or `authorization`; 404 for other paths and 405 for
  other methods, both before any body is read; 408 when the whole body has
  not arrived within 30 seconds of the server starting to read it (a total
  deadline, so a trickled body is cut off too); 411 without a
  `Content-Length` of ASCII digits; 413 above 32 MiB.
- Body limits. The model listener and the admin `/script` and `/next` take
  at most 32 MiB (the script limit). The admin `/import` takes at most
  256 MiB, eight times the script limit: an export holds the script and a
  replacement script as JSON strings, which escaping can double, plus the
  replies and state. `GET /export` answers 413 rather than produce an export
  that, inside the `{"state": ...}` wrapper `/import` takes, would be larger
  than that, so every export it gives can be imported; `admin export` writes
  the server's bytes unchanged. A replacement script added to the import
  adds its own text to the body.
- A listen backlog of 64 on both listeners. Python's default of 5 dropped
  connections with 16 clients arriving at once on Python 3.12.8.
- Ids in the real format: `msg_01`, `req_01` and `toolu_01` followed by 22
  base58 characters, message and request ids time ordered.
- Streams that repeat. The piece sizes, padding and first `output_tokens`
  of a stream are drawn from a seed made of the reply's content (tool call
  ids masked), the conversation's position key, the reply's index in it and
  the handler that produced it. A main conversation's key is the episode
  index of its first user message. A child's key is its child name and launch
  envelope, not the delegation instance it claimed, so equivalent children
  stream the same bytes whichever claims which. The same script and requests
  give the same bytes apart from ids on any server and in any order of child
  launches, with one exception: the usage counts of a conversation's first
  request. Its cache-read and cache-write figures depend on whether an
  earlier conversation used the same system prompt and tools, which is order
  dependent (see Limits). Tests check this on two fresh servers.
- Response headers: `Date`, `Content-Type`, `request-id`, and for streams
  `Transfer-Encoding: chunked` and `Cache-Control: no-cache`. No `Server`
  header. Nothing the server sends names this program or the benchmark.

## How it differs from the real API

The replay test (`tests/scripted/test_replay.py`) sends the server the real
requests of four stored Pi runs, 26 requests in all, with scripts that
reproduce those runs:

| Run | Requests | What it holds |
|---|---|---|
| steering | 5 | narration before a tool call, a steer after a tool result |
| messaging | 5 | `tool_search`, a `tool_addition`, `send_to_user` |
| subagent | 5 | a call with no arguments, `delegate_task`, the child's own conversation |
| handoff | 11 | a reply with two parallel `write` calls, a 50 KB tool result, a fresh conversation after the reset |

Each response is normalised and must then equal the real one byte for byte:
every event name in order and every event's JSON with its key order, the
`content_block_stop` and `message_stop` payloads included, the joined text of
every block, every tool input, the framing of every block's deltas (a tool
input's first and last delta, no empty text delta), and the header values
the framing depends on.
Normalising removes exactly these differences, and on 2026-11-21 they were
the only ones:

| Difference | Why it remains |
|---|---|
| Message and tool call id values | New ids. Both sides have the real format, and the mapping from real to new ids is one to one across each run |
| Token counts | Estimates at 2.7 characters a token, calibrated on the stored first requests (14,956 characters against 5,549 reported tokens). The cache split follows Pi's marker: the prefix up to the newest block is a cache read when the conversation, or a first request with the same system prompt and tools, cached it before, and a cache write otherwise. `input_tokens` is 2 where the real one is 2 or 4. Keys, key order and value types are equal |
| Number of deltas a block is split into | Piece sizes drawn at random. The joined text and tool input are equal |
| Spaces before each event's closing brace | Lengths drawn at random, as the real stream varies. Every line is otherwise the compact JSON of its event on both sides |
| Whitespace inside one tool input | In the subagent run the real `delegate_task` input has `"target": {"providerInstanceId":"pi",...}`, the model's own spacing for a nested value; the server writes `", "` and `": "` throughout. Tokens and key order are equal. This was the only such case in the four runs |
| Response headers | The real API sent the same 29 headers on all 104 stored 200 responses (from all eleven gateway logs of the run, the three snapshot copies included), among them `anthropic-ratelimit-*`, `anthropic-organization-id`, `cf-ray` and `Server: cloudflare`. The server sends 5. `Content-Type`, `Transfer-Encoding` and `Cache-Control` have the real values. Pi reads none of the missing headers |

The test also checks itself: changing a saved response's
`content_block_stop` index, `message_stop` payload, `message_delta` key order
or a text, adding a response header, or removing the saved zero-argument
call's empty input delta, fails the comparison.

Other differences, not covered by the replay:

- The non-streaming body is the streamed message with its content filled in.
  No real non-streaming body was available to compare.
- Error wording for validation errors is modelled on the real API's style
  from memory, except the stored credit-balance and not-found bodies. Thread
  error codes come from Pi's client; their real status codes and messages are
  not known here and could only be learned from the real API.
- The real API keeps a thread for a limited time and lets each reply be
  continued once. The server keeps every thread for the episode, answers a
  repeated identical `continue` with the same reply, and sends
  `thread_already_continued` only for a different continue from a reply that
  has been continued. It never sends `thread_continue_in_flight`.
- No thinking blocks, no server tools, no images in replies, no
  `max_tokens` stop, no `refusal`, no overload or rate-limit errors.

## The request log and the state

`<log-dir>/requests.jsonl` gets one line per API request, written before the
reply is sent. Fields: `time`, `request_id`, `method`, `path`, `status`,
`seq`, `thread`, `previous_message_id`, `model`, `stream`, `match`
(`thread`, `history` or `root`), `base` (the node it continued),
`identity` (the rule a to d that placed the request, the decision and the
reason), `conversation`, `conversation_kind`, `child`, `new_conversation`,
`user_messages` (episode index and text hash), `tool_results`, `steer`
(whether a user message arrived while a handler was running, set before
anything in the request can fail), and fields that describe the reply
actually sent: `decision` (`tool_use`, `final`, `reply`, `fallback`,
`replay`, or `child_completion` for the reply to a request that carried only
T3's delegation completion prompt), `delegation_completion` (the hashes of
the completion prompts in the request, when there are any), `handler` and `step` (the handler and step whose tool call or text
is the reply; absent on a fallback), `ends_with` (only when the reply ends a
handler's turn). Then `finished` (each handler that completed in this reply,
with its step and ending), `branches`, `repeats`, `chained`,
`failed_handlers`, `captured`, `captures_missed`, `tool_calls`, `message_id`,
`stop_reason`, `delay_s`, `errors`, and on a `create` the tool names, the
offered tools and the system prompt's hash and length. Each new system
prompt is written once as `system-<hash prefix>.txt`. Refused requests get a
line with `error`, and with `identity` when an identity rule refused them. A reply
that fails to send after its line was written gets a second line with
`send_failed` and the message id.

`<log-dir>/state.json` is rewritten on every change of state: before each
reply, after each report of a send attempt, and after `load`, `reset`,
`next` and `import`. The snapshot is taken and written under one lock, in
one order, so an older snapshot can never replace a newer one. `admin state` returns the same object: the script's hash and sizes, every user message by
episode index with its conversation, each handler's status (`active`,
`done`, `abandoned`, `error`, `skipped`; an exhausted script records the
message's index with `error`), how it ended (`question` or
`statement`) and how many tool steps it sent, the conversations with their
system prompt hash and tools, the delegations with their status (`sending`,
`delivered`, `undelivered`, `failed`, `claimed`), their delivery record and
which conversation took each, the runner's
announcements and what became of them, the variables, and the error list.

## Tests

```sh
uv run pytest tests/scripted                                            # the project's interpreter
uv run --isolated --python 3.12 --with pytest pytest tests/scripted -p no:cacheprovider   # the image's
```

Run both. `--isolated` matters: without it, `uv run --python 3.12` rebuilds
the project's shared `.venv` on 3.12.

In process, against the server on loopback ports. They cover the event
sequence and JSON validity for text and tool calls, a 10 KB tool input split
over more than 100 deltas, non-streaming, `count_tokens`, malformed requests
and scripts case by case and in a field-by-field fuzz, the read deadline, a
reply whose client has gone, the regular expression check, concurrent first
requests with one new system prompt, thread errors, handler selection across
a restarted conversation, full-history resends and retries not counted twice,
the two stored `pending` creates, a model change, the runner's announcement
with and without it, equal decisions with and without delays, two user
messages in one request, steering with `abandon` (including a missed
required capture) and with `finish`, branches, captures from results and from
the user message, conditions on variables and numeric comparisons, `repeat`
stopping on its condition and at its maximum, delays, an exhausted script, a
tool the request did not offer, `tool_search` followed by an added tool,
arguments checked against the offered schema, parallel calls, `expect`, the
child envelope and the delegation lifecycle, two conversations at once, the
admin listener, the log and the state file, and the replay comparison above.
`test_identity.py` has one test per reviewer scenario of the identity rules
(the two stored `pending` creates resumed after a restart, a stale retry
during an announcement, a second launch with the same prompt, a `repeat`
that delegates twice, the order of send completions, a capture failure next
to a failed delegation, a capture failure next to a correction) and a test
that runs two parallel child launches in both orders with three orders of
send completion and requires identical decisions. `test_export.py` exports
mid-episode (after tool results, captures and a completed delegation),
imports into a fresh server, resends the history with a new message to both,
and requires the same decision, reply and following turn. `test_round3.py`
has a test per finding of the third review: continue retries across
conversations with equal histories, a child launch between two send reports
in both orders, children capturing different values into one name in every
launch, result and parent order, envelopes equal at load, literals and
out-of-range numbers, a failed handler's narration and delay, a conversation
running across an import with a replacement script, an older state snapshot
racing a newer one, and the minor items. `test_round8.py` covers the eighth:
a task trimmed as T3 trims it, T3's no-note rule for leading commands, pairs
that differ only by what T3 trims, T3's roles and the waiting mode, T3's
completion prompt followed by the real follow-up, `abandon` with a capture
that succeeds, export to import at the wrapper boundary through the CLI,
inherited delegation aliases, canonical argument comparison, stored block
shapes on import, and a resend built from separate native tool results.
`test_round7.py` covers the
seventh: ECMAScript whitespace (U+FEFF, U+00A0, U+2028, U+3000 blank;
U+200B, U+0085, U+001C not) against a history built by Pi's rules, the
reserved delegation tool, the fallback keeping a completed handler's text
and delay, the import body limit at its boundary over HTTP and an export
above 32 MiB, delegations that differ only in arguments, equivalent children
streaming the same bytes, and Pi's synthetic "No result provided" result.
`test_round6.py` covers the sixth:
blank narration and blank turn-ending texts, one resume per rule of Pi's
history conversion, nested delegation refused at load, a message-level field
in the retry fingerprint, and each reply's conversation checked on import.
`test_round5.py` covers the fifth:
envelope reuse with other variables whether the child launched first or not,
an original handler running beside a started replacement, creates and
resumes one byte different, the step limit with a waiting correction, cache
markers only at protocol locations, announcement and parent checks on
import, `1e400`, and streams equal across fresh servers and launch orders.
`test_round4.py` covers the fourth:
a trailing space and a system block as different continue content, two
outstanding delegations with one envelope and different variables in both
orders, the replacement script's text through `POST /import` and through the
CLI, original handlers and children running across an import on both a
success path and a failed capture, hand-built exports `_restore` must refuse,
out-of-range literals, and the send race over HTTP with the retry paused
before its first byte.

## The real check

`uv run python -m tests.scripted.e2e_pi` builds the gateway image from
`docker/gateway/Dockerfile` and this image, and runs stock Pi from
`t3-harness-pi:0.1.12-nightly.20261121.5051` with its entrypoint replaced by
`sleep`, so T3 Code never starts. The agent's container is on an internal
network with an opaque name whose bridge address is flushed (Docker 27.4.0
here, so the pre-28 recipe), with the gateway as its only peer. The gateway's
Anthropic route points at this server over HTTP with a random placeholder
key. No model API call is made.

The first version passed on 2026-11-21 at 22:27 and 22:38 UTC, the first
review round's code at 23:18 UTC, the second round's at 00:07 UTC on
2026-11-22, the third round's at 00:39 UTC, the fourth round's at 01:02 UTC,
the fifth round's at 01:21 UTC, the sixth round's at 01:37 UTC, the
seventh round's at 02:00 UTC, and the eighth round's final code from 02:23
UTC on 2026-11-22 (`runs/scripted-e2e/2026-11-22T022352`, run with `uv run
--isolated --python 3.12`). In that last run the server placed Pi's requests as 13 continues
(rule a), 6 new main conversations (rule d) and 3 resumes of a full-history
`pi -p --continue` (rule c), with no identity errors; the only error logged
was the intended `script_exhausted`:

| Scenario | What Pi did |
|---|---|
| Run a command, report its output | `pi -p` ran the scripted `bash` call and printed "The command printed 3" |
| Write a file, then edit it | `write`, `edit` and `read` calls; the container's `notes/todo.txt` held "first line\nedited line\n" |
| Branch on a failing command | `ls` of a missing file exited 2; the branch ran a second command and replied "It was missing (ls exited 2), so I created it." |
| Capture a value, multi-turn, restart | Turn 1 captured a random job id and asked a question; `pi -p --continue` arrived as a `create` with the full history, matched by content, and used the id in handler 1; a fresh container with the workspace copied got handler 2; a fourth message got the fallback, logged as `script_exhausted` |
| Repeat, numeric comparison, the user's answer | A `repeat` ran Pi's `bash` three times as the container's `ops.txt` went 500, 400, 300, 300, and stopped by its condition once a round brought no gain (`ops lt best` false); Pi printed "Best operation count is 300. Should I record it?". The host announced message 1 through `docker exec ... admin next 1 "Yes, record it as run R-42."`. `pi -p --continue` with that text branched on `user_message contains "record"`, captured `R-42` from it, wrote `record.txt` as "R-42 300\n" and printed "Recorded 300 as R-42." |

`python -m gateway verify --allow-flushed-bridge` gave "43 checks, 0 failed,
1 waived" for each of the six agent containers. The gateway relayed all 22
requests with status 200 and parsed complete usage for each. Pi rejected
nothing the server sent in any round, so no server change came out of the
real check. One validator rule changed in the first round, because the branch
scenario's script was refused: a handler may leave out `final` when every
path through its steps ends in a `reply`.

## Limits

Tested in process only, not with real Pi: steering, parallel tool results,
`tool_search` and added tools, delegation to a child, thread error recovery,
export and import, non-streaming replies and `count_tokens`. Real Pi did not
exercise them because the real check runs Pi without T3 Code, which owns
`delegate_task`, the deferred tools and steering. The replay suite feeds real
Pi requests and covers successful responses: steering, parallel tool
results, `tool_search` and added tools, and a delegation with its child.
Thread error recovery, export and import, non-streaming replies and
`count_tokens` are covered by the in-process tests only.

- Without the runner's announcement, a new conversation that opens exactly
  like an earlier one, with the same system prompt and tools, is taken for a
  retry and gets the earlier reply. An announcement without text cannot tell
  a stale retry from the announced message; give the text.
- The model listener accepts any non-empty `x-api-key` or `authorization`
  header and answers whoever asks. A client built by hand inside the agent's
  container could reach it through the gateway, obtain the next scripted
  reply and use up its turn. This is a property of scripted runs, not
  something the server tries to block: the scripted agent is the thing under
  test, and nothing else in the agent's container should be calling the
  model. When the runner uses announcements, a create it did not announce is
  logged as `unannounced_message`.
- Rule c is strict. A resume whose system prompt or tools differ from the
  conversation's (a Pi update between turns, a changed tool set) is unknown
  history and gets the fallback.
- Episode state lives in memory. A server restart loses it unless the host
  exported it and imports it into the new server. Without that, a `continue`
  naming an earlier reply gets `thread_not_found`, Pi resends in full, and the
  resend is `unknown_history` with the fallback reply.
- One episode per server. A probe copy needs its own server, seeded with an
  import of the main server's export.
- Child recognition depends on T3's envelope wording in the pinned runtime.
  A T3 release that changes it makes children main conversations, each logged
  as `unmatched_child`.
- The regular expression check is a static rule for one shape of slow
  pattern, and patterns run under the episode lock (see the format section).
- Some decisions do depend on what has happened so far, by design. Which of
  two continues from one reply arrives first decides which one is answered
  (the other gets `thread_already_continued`), as the real API lets a reply
  be continued once. The runner's announcement decides by when it is made.
  A child launch whose delegating reply has failed to send on every attempt
  so far, with none in flight, is not a child. The main conversations share
  one set of variables, so two main conversations running at the same time
  see each other's writes in the order their requests arrive; the runner
  sends one user message at a time, so this does not arise in an episode.
  The replacement's scope copies the main variables when its first handler
  starts. Token estimates count a cache read for a conversation whose system
  prompt and tools an earlier conversation used, so the usage counts of a
  conversation's first reply depend on the order conversations open, and the
  stream bytes with them.
- `finish` steering drops the finished handler's turn end: its final text
  becomes narration in the same reply as the next handler's first call.
- Each delegated child sees a copy of its parent's variables from the
  moment of delegation; a parent sees a child's values only through captures
  on the delegation result.
- The response is distinguishable at the wire: five headers against the real
  29, estimated token counts, and all events of a reply written at once
  rather than paced like a model generating. Nothing in the stored runs or
  the real checks shows Pi handling a reply differently because of these.
- Token counts are estimates and must not be used for cost. Pi reads the
  counts the API reports when it decides whether to compact its context, so
  a scripted episode long enough to come near Pi's threshold could compact
  at a different point than a real one would. None of the stored runs comes
  near it; an episode that might should be checked for a compaction item.
