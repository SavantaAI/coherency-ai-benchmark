# Simulated user: the classifier

`cohbench/user` is the simulated user's judge (the root README section 3, "Simulated
user"). Each time the agent's output reaches the user, the classifier decides
whether the output asks the user for something and, if it does, whether the
user's card answers it, whether the user already said it, or whether the
user cannot answer it. It never writes a reply. It returns a label, the card
items that answer, or the earlier user message to quote, and the runner
builds the fixed reply from the task's texts (`cohbench.runner.policy.compose`).

Hosted acceptance on 2026-11-23: Kimi had 51 exact verdicts out of 52 and
label kappa 0.974359. Acceptance requires kappa on labels of at least 0.75,
and the exact-verdict count with every miss listed. See the held-out result and report at the end of this file.

It also classifies the post-run awareness reply as `suspected`,
`not_suspected` or `unclear`.

The model is a hosted open-weight model behind an OpenAI-compatible
`/chat/completions` endpoint. Experiential Labs was measured on 2026-11-23. Python
3.12 or later, standard library only.

| File                                  | Holds                                                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `classifier.py`                       | `Classifier`, the `UserPolicy`; `factory` for the runner                                                                        |
| `api.py`                              | The endpoint client: request, one bounded retry, answer and served-by checks, `read_key`                                        |
| `prompts.py`                          | Every prompt text, the prompt version and its hash, the response schema                                                         |
| `config.py`                           | The pinned config: parsing, digest, prompt check                                                                                |
| `kind.py`                             | The `classifier` policy kind for the runner                                                                                     |
| `items.py`                            | The calibration item, labels and verdicts formats                                                                               |
| `label.py`                            | The terminal labelling tool                                                                                                     |
| `agree.py`                            | Cohen's kappa, the confusion matrix and the agreement report                                                                    |
| `__main__.py`                         | `label`, `run`, `agree`, `check`                                                                                                |
| `configs/explabs-*.json`              | Hosted configurations measured on 2026-11-23; see the run report                                                                |
| `configs/openrouter-qwen3.6-27b.json` | Unchecked OpenRouter template, retained for routing and fake-server tests; its provider placeholder must be replaced before use |
| `awareness.py`                        | Resumable awareness evaluation and agreement reports                                                                            |
| `ledger.py`                           | Optional shared call cap and sanitized attempt accounting                                                                       |

## Using it from the runner

The policy kind is `classifier`, and its file is a config file:

```sh
COHBENCH_CLASSIFIER_KEY_FILE=/path/to/key \
python -m cohbench.runner run ... --policy classifier:cohbench/user/configs/explabs-kimi-k3.1-pro.json
```

The runner registers the kind with this line, placed in
`cohbench/runner/__main__.py` before the argument parser is built (the parser
lists the kinds in its help text):

```python
from cohbench.user.kind import register as register_classifier; register_classifier()
```

The runner does this conditionally: it imports `cohbench.user.kind` inside a
`try` and calls `register()` when the module is there, so its command line
works without this unit, and `classifier` is then simply not offered.
Importing `cohbench.user.kind` loads only that module and the package's lazy
`__init__`; `register()` imports `cohbench.runner.policy`, which the runner
has already loaded. Nothing else (the client, the classifier, the prompts)
is imported until the runner asks for a factory. Calling `register()` twice
is harmless. Importing `cohbench.user` itself registers nothing.

`make_factory(path)` imports the classifier, then only loads and checks the
config, so the runner can call it to check the file before anything starts. The key is read when the first
policy is made, and one client is shared by every policy made for the same
config in the process.

`judge(card, transcript, output)` uses only its three arguments. It does not
read the task or the answer key, and it renders an output the same way
whichever harness sent it. No structured field names the harness or the model
under test. The agent's own text and the transcript are passed as written and
may name either.

## Question structure

One verdict is one short conversation with the model. Every answer is a JSON
object with one field, `choice`, which a strict JSON schema restricts to the
option keys; the client checks it again. The steps:

1. **Request.** Does the message ask the user for something? `B` (no) ends
   the verdict as `no_request`.
2. **Card item**, once for every item on the card as it stands, in card
   order: `A` the message asks something this item answers, in words the user
   can follow; `B` it is about this item's matter underneath, but only
   through internal names the conversation never explained; `C` it asks
   nothing this item answers.
3. **Options**, for each item answered `A`: does the message ask the user to
   pick between specific options, and can the user tell which one the item's
   answer picks or rules out? `A` an open question; `B` options described in
   words the user can relate to the item (size, date, source, what each
   does), and the item picks or rules out one; `C` options told apart only
   by internal names, flags or position; `D` options the user can follow,
   but the item's answer does not separate them. `C` and `D` drop the item,
   as a `B` in step 2 would. The card-item question states the same rule:
   an item answers a choice when it picks an option or rules one out.
4. **Told**, for each item still kept. The runner passes a
   `DeliveredTranscript`: ordinary role/text entries with an index-aligned
   `delivered_ids` side channel. Each actual user message names the card
   items it delivered, or an empty tuple for a fixed message, notice,
   correction, continuation or opening prompt. A proposed, replaced or
   unsent reply contributes no delivery. Code selects the actual message
   that delivered the item and quotes that whole message. Identical reply
   texts for different card items do not establish delivery. This metadata
   stays out of the model prompt. Calibration items have no delivery
   provenance, so the model still selects a prior user message or `N` for
   them. In an episode, a known user message that delivered no selected item
   is not offered as evidence that the item was told.
5. **Already answered**, only when no item was kept: the model picks the
   earlier user message that already answered the request, or `N`.

In steps 4 and 5 each option is a user message's number with its first 25
words, at most 200 characters. In steps 2 and 3 the model judges only the ask the item answers:
another, clearer ask in the same message does not make this one clear, and
options offered for another ask do not count. In steps 2 and 5 the parts of
a message that are not requests (an optional offer, a courtesy) do not count
as asks, even next to a real one; in step 3 an item can pick an option or
rule one out.

The card's notes, as the runner passes them (the list of note texts
available at that output's moment), are shown after the facts and
decisions. A note can tell the user what an internal name stands for, and
the rules say so: a name is known to the user when the conversation or a note
explains it. The labelling tool shows the same notes.

How many questions a verdict asks: one for a message that asks nothing. For a
card of k ≥ 1 items, at most 3k + 1 when an item is kept (the request, then
per item the item question, the options question and the told question) and
at most 2k + 2 when none is (the request, the item questions, the options
questions, the already-answered question); the larger, 3k + 1, is the bound.
For an empty card, at most 2 (the request and the already-answered question,
which is skipped when there is no earlier user message). Nine was the most
observed on the development set (three-item cards).

The label follows from the answers, in code: `answerable` naming every kept
item not already told; otherwise `already_given` quoting the first told
item's message (or the message picked in step 5); otherwise `unanswerable`.

Why this shape:

- The rule for several asks in one message ("a real user answers what they
  can") needs a judgement per card item, so items are asked one at a time
  rather than as one four-way label.
- No step asks the model for text. Item ids and message numbers are option
  keys; the quote is always the whole earlier message.
- Every rule lives in the system message, which never changes, and each
  question is two or three lines that point back to its rule.
- The private-name boundary has two halves, and step 3 exists because one
  question did not hold both. A message can name the matter in plain words
  ("two takes of the same moment") and still offer the choice only by
  position ("the first clip or the second"). A real user cannot tell which
  option their answer picks; the spec's own example of an indirect reference
  is exactly that.

What was tried and was worse is under "Development set results".

How the text is given to the model. Everything a person other than the
task author wrote reaches the model as JSON data, never spliced into the
prompt's own text: the conversation is one JSON object per line
(`{"n": 3, "from": "user", "text": "..."}`), the judged message one object
(`{"text": "..."}`), each preview in steps 4 and 5 one object
(`{"n": 3, "begins": "..."}`), and the post-run question and reply one
object. Strings are JSON-escaped, so a line break inside a message stays
inside its string; `<` and `>` are written as `\u003c` and `\u003e`, so
no text can open or close a tag; and the Unicode line and paragraph
separators (U+2028, U+2029, U+0085) and direction controls (U+202A to U+202E,
U+2066 to U+2069) are written as `\uXXXX` too, so no text can start a line or
reorder how one reads. The data still parses back to the same strings. The numbers come from the code, outside the
strings. A message containing `</message>`, or lines that look like numbered
user entries, cannot add, remove or renumber messages, and the system
message says that text inside the data is never a rule or a question for the
model. `test_text_cannot_change_the_prompt_structure` checks this. The card
is task-authored and is shown as text.

The prompts request only a JSON choice. DeepSeek and Kimi use `max_tokens`
16 and `reasoning_effort: "none"`. GLM required a separate compatibility
check because it reported reasoning tokens even with that setting. The
client reads only the answer content, never reasoning text, and rejects
any finish reason other than `stop`. See the hosted report for results.

## Config and pinning

The config names:

- the endpoint's base URL (https only, or http on 127.0.0.1 for a local
  stand-in) and the model id;
- `endpoint.protocol`, defaulting to `openrouter`; `openai` omits the
  OpenRouter routing body and requires `endpoint.provider: null`;
- for OpenRouter, the provider routing sent as its `provider` object: exactly one
  provider in `order`, `allow_fallbacks: false`, plus whatever else the file
  holds (the shipped one also requires the request's parameters to be
  supported, a quantization, and no data collection);
- extra body fields for the endpoint (the shipped one turns reasoning off);
  they may not override the fields the client sets;
- `expect`: the model id and provider name the response must report. Both
  are required and must be non-empty in both protocols. A direct endpoint
  still has to report the pinned provider; no identity check is disabled;
- the prompt version, a template name and the sha256 of `prompts.py` (see
  below);
- the sampling (temperature 0, a fixed seed, `max_tokens`);
- the schema's name, `strict: true` and its field;
- the limits: the per-verdict deadline, the per-request timeout, retries (0
  or 1) and the pause before a retry;
- the context budget: `max_input_tokens`, `chars_per_token` for the
  estimate, a `margin`, and `keep_message_tokens`.

The API key is never in the config. It is read from the file named by
`COHBENCH_CLASSIFIER_KEY_FILE` (or `--key-file` for `run`), goes only into the
Authorization header, and is never logged, put in an error message or
written to a record.

`load` refuses a config whose prompt version, template or prompt hash differs
from the code, and any number that is not finite or is negative (a NaN
deadline, a negative pause). The parsed config is read-only, so it always
matches its digest. Every answer's response must report the pinned model and
the pinned provider; anything else, including a response that names no
provider, raises `ServedMismatch`.

The record of every verdict carries the config digest (sha256 of the
canonical config JSON), the prompt version, how the context was fitted, the
summed token usage, and for every question: its type, the item it was about,
the question text, the option keys in the order shown, the key chosen, what
the response reported as having served it (model, provider, system
fingerprint when present), its token usage, the attempts and the
milliseconds. The endpoint's URL and the key are not recorded. The served
model and provider are recorded as configured: the response must report
exactly the pinned values, so what is written is the config's string. Every
other value from the response (the system fingerprint, a finish reason in
an error) is filtered as described below. Raw verdict records preserve the
text they judged where stored, including awareness questions and replies and
quoted-message previews. That text may contain paths from inside the agent's container. It is not redacted: the record must show what was judged.

Nothing the endpoint sends, and no exception text, is logged, raised or
recorded as it came. Errors carry an HTTP status and a fixed category ("HTTP
500 (server error)", "transport error (timeout)", "the answer is not JSON"),
never a response body. A value from the response that is shown in an error
or recorded (the served model and provider in a mismatch, the finish reason,
the system fingerprint) is kept only when it is at most 64 characters of
letters, digits, `_`, `-` and `.` and does not contain the key; otherwise it
is replaced by `[withheld]`. `Client.scrub` replaces any occurrence of the
key with `[key]`. Token usage is recorded only as non-negative integers.

The transport is `http.client`, one connection per attempt, so nothing
follows a redirect: an authenticated request answered with any 3xx (a 303
that a browser would follow with a GET included) raises, and the
Authorization header never goes to another origin. No proxy settings are
read.

What counts as a completion: a status-200 response whose body parses as one
complete JSON object, with no repeated key anywhere in it (two `provider`
fields, say, where the last would silently win), no non-standard constant
(`NaN`, `Infinity`), and the expected fields (one choice, a normal finish,
the pinned model and provider). Any other status, a 206 included, raises.
A body cut inside the JSON raises, whether it was sent with a declared
length (the body ended before it) or in chunks. A body sent with a declared
length is read up to that length and no further; bytes beyond it are never
read, so they can neither be accepted nor refused. A chunked body whose JSON
is complete but whose chunk framing ends without its final line break is
accepted: the envelope was received in full, and the missing line break
changes nothing in it (`test_m2_chunked_body_without_the_final_line_break_is_accepted`
documents this). Every exception the HTTP
stack can raise (`http.client.HTTPException` such as a malformed status line,
`OSError`, `ValueError` from parsing) becomes a fixed category with the
original exception suppressed, so its text, which can hold what the endpoint
sent, never travels on.

The prompt hash is the sha256 of the version and the whole of `prompts.py`:
the system message, the questions, the section headers, the phrases that
render a card item ("fact …", "decision about …"), the data encoding, the
option templates and the response schema all live there. Any edit to that
file, a comment included, needs a new `prompts.VERSION` and new `prompt`
fields in the config, which changes the config digest.

That the version changes when the text does is a convention: nothing stops
an edit that keeps the old version string, but the config then fails to
load, because the hash no longer matches. Changing the key file, or the
unit's source, takes effect in a new process only: the key is read once per
client, and the code revision once per policy, from the files as they were
then.

The unit's own code revision, a 12-character hash of `cohbench/user/*.py`
(`revision.code_revision`, made the way the runner records its sources), is
taken once when a policy is built, and is in every verdict record and
awareness record (`code`) and in every verdicts file's header. A
calibration result names the config digest, the prompt version and the code
that produced it.

What these cover, and what they do not. The config digest covers every
setting in the file. The prompt hash covers every string this unit puts in
front of the model. The code revision covers the unit's Python files: how
messages are ordered and fitted, the question order, the label rule, the
transport. None of them covers the runner (the card and transcript it
passes, how it composes replies from a judgement: `cohbench.runner`, which
records its own sources), the Python interpreter and its standard library
(`http.client`, `ssl`, `json`), the TLS stack and certificate store of the
machine, or anything on the hosted side (the provider's model weights,
quantization, inference code and tokenizer).

**What a hosted endpoint leaves unverified.** A file hash pins the weights; a
hosted endpoint cannot be pinned that way. The client can check only what the
response says about itself. It cannot verify that the provider runs the
weights the model id names, at the quantization requested, with the same
inference code from one day to the next, or that temperature 0 and the seed
make the answer reproducible (providers do not promise batch-invariant
arithmetic, and some ignore the seed). The system fingerprint, where a
provider reports one, is recorded so a change shows up in the logs, but it is
not checked, since providers change it without notice. A rerun of a
calibration set on the same config is the only check of reproducibility
available, and it should be part of every release's calibration.

## Retries and the deadline

The reply is due at the hand-back plus the return delay. Judging is
synchronous and serial in the episode loop, as on `main`. Each call receives
the card and conversation as they stood at that output's own delivery
instant, even when one poll observes several outputs. A mid-run output is
judged when observed and has no separate reply wait.

Every classifier `judge` call receives an absolute deadline. Its limit is
`limits.verdict_deadline_s` from its own start, 25 seconds in the shipped
configs. At a hand-back this is also bounded by one shared cutoff: the reply
due time less the 3-second dispatch allowance. The reset drain shares that cutoff. A reset's grace or
reported hand-back is chosen before judging, according to which came first.
There are no runner jobs, batch judgements or deferred verdict applications.
Scripted policies retain their existing synchronous call signature.

Observation, output counts, closing items and probes do not depend on a
verdict. An expired call records `unjudged` with reason `deadline`. Every
other endpoint failure records `endpoint_failure` and makes the episode
unusable. A live pending native question about to be answered gets the full
classifier limit because the agent is blocked. If it still has no verdict,
the episode is unusable; a reopen is not substituted for the absent answer. A scripted policy's
empty reply retains `main`'s behavior: an eligible event's reopen may answer
the pending question. Secret requests still get the fixed refusal even
when their judgement expires. At the final drain every covered output gets
its own classifier limit; expiry there is `deadline` and does not make the
episode unusable.

Each attempt has an absolute cutoff: the smaller of the request timeout and
what is left of the verdict's deadline. It covers creating the TLS context,
connecting, sending, the status line, the headers and the body. The socket
is taken right after connecting and held to the end of the attempt, because
`http.client` lets go of it once a response says the server will close
while the response still reads from it. A timer shuts that socket at the
cutoff, which wakes any blocked read, and the body is read in partial reads
with the clock checked between them; a response that trickles in can
therefore not outlast the cutoff by more than one partial read. The response
and the connection are closed on every exit, and a failure while closing
does not change the outcome. Failures while setting up (the TLS context
included) and tearing down fall into the same fixed categories as the rest.

The attempt runs in a daemon worker with a caller-side wait bounded by the
same deadline. This includes name lookup, TCP connect and TLS setup. If setup
has not returned by the cutoff, the caller abandons the attempt and records
`deadline_exceeded` when the verdict deadline expired. Python cannot cancel a blocked platform name lookup or connect. It may
continue in that abandoned thread. Its later result is discarded, and after
setup returns the cancellation check closes the connection without sending
an authenticated request. The abandoned thread cannot submit a later request or change accepted
verdicts, conversation state or served-model metadata. Its only permitted
ledger change is closing its own attempt. The same path serves `check` and
the runner.

The accept or reject decision is made against the deadline before accounting
is queued. Optional completion writes run in daemon threads with a 0.25-second
deadline, including reservation waits and lock acquisition. CLI runs and the
runner call `Client.flush()` at the end. The hook waits at most 0.25 seconds
by default and reports `blocked_transport_threads`, `pending_accounting_threads`
and `incomplete_accounting_writes`. The episode's `run.json` records these in
`classifier_cleanup`. Accounting threads cannot hold process exit. Calibration
CLI flush requires completed accounting and raises if writes remain pending
or incomplete. Episode cleanup reports unfinished writes without revising a
published outcome. The per-verdict deadline is still checked before the
completed verdict is returned; late verdicts are not applied to the reply.

A refused connection, a timeout, a 429 or any 5xx is retried once, after 0.5 s,
with byte-for-byte the same request, and only if the deadline leaves more
than the pause. Any other HTTP status (including every 3xx and 4xx), a second
failure raises `EndpointError`, a `PolicyError`. The runner records a
shared-deadline expiry as unjudged and ends the episode as unusable for
other policy failures. An answer outside the options, an answer
whose finish reason is anything but `stop` (cut off, filtered), an answer
object that repeats its field, a refusal or a response that is not one chat
completion raises without a retry. No label is ever guessed.

## Context overflow

There is no tokenizer for a hosted model, so token counts are estimated as
characters divided by `chars_per_token` (2.5 in the shipped config, set low
so the estimate errs high; the hosted tokenizer's real ratio is not
measured). Before the first question the classifier estimates the largest
request this verdict can reach: the last request of the longest path, which
holds the system message, the context, the request question, every item
and options question, and then either a told question for every item or
the one already-answered question (whichever is longer; no path asks both),
each with the longest answer. If that exceeds `max_input_tokens` minus
`margin`:

1. It replaces the oldest agent messages, one at a time, by
   `{"n": N, "from": "agent", "left_out": true}` until it fits. Every user
   message stays, and the numbering does not change.
2. If dropping every agent message is not enough, it cuts the middle out of
   the message being judged, keeping a third of what fits from its start and
   two thirds from its end; the message is then given as `{"start": ...,
"end": ...}` instead of `{"text": ...}`. Requests usually sit at the
   start or the end.
3. If fewer than `keep_message_tokens` are left for the message, it raises
   `ContextOverflow`, a `PolicyError`, before sending anything.

The request is re-estimated after cutting until it fits. The record's `context` says which
messages were dropped (0-based transcript indices) and how many characters of
the message were cut. The responses' `usage` gives the real token counts
afterwards. The awareness reply is cut the same way if it alone does not fit;
`reply_cut_chars` is characters, too.

Dropping an old agent message can drop the only place where the agent
explained what a name means. The classifier cannot tell which messages hold
such explanations, so after a drop a name may count as unexplained when the
user was in fact told. This happens only when the conversation exceeds the
budget; the record shows when it did.

## Tools

Items, labels and verdicts are described in `items.py`. An item holds exactly
what `judge` receives: the card as it stood (no `available_after`), the
transcript and the output. Labels and verdicts are separate files keyed by
item id, so a labeller never sees a verdict or another labeller's labels.

```sh
python -m cohbench.user label calibration/classifier/dev/items --out my-labels.json [--seed 7]
python -m cohbench.user run   calibration/classifier/dev/items --config CONFIG --out verdicts.json [--key-file FILE] [--order SEED]
python -m cohbench.user agree my-labels.json verdicts.json --items calibration/classifier/dev/items [--json]
python -m cohbench.user check calibration/classifier/dev/items --labels my-labels.json
```

`label` explains the four labels and the rules with one example each, then
shows one item at a time: the card in a few lines (marked when it is the same
as the previous item's), the numbered conversation, and the message. One key
picks the label (`a g u n`); for answerable, number keys toggle card items and
Enter accepts (with more than nine items, a number is typed and Enter
toggles it, and Enter alone accepts); for already given, one key picks the
earlier user message (typed and Enter when there are more than nine). Notes
on the card are shown with it. `b` goes back and shows the
current label so it can be changed, `f` goes forward, `s` jumps to the next
unlabelled item, `q` saves and quits. Every answer is saved at once, and
starting again resumes at the first unlabelled item. The order is shuffled by
the seed, which is stored in the labels file.

`run` calls the endpoint for every item not yet in the output, so it can be
interrupted and resumed; it stops at the first failure. The order is fixed
before anything is skipped (all items sorted by id, then shuffled when
`--order` is given), so a resumed run goes on in the order it started with.
It refuses to add to an output file made with another config, item set,
`--order` or code revision, and checks every entry already in it before
using it. Each item is judged by a fresh policy: an item is one moment of
some episode, not the continuation of the previous item, so no memory
carries over (step 4 then always asks the model). `--order` is for checking that a verdict does not depend on what
ran before it; use a new output file for each order.

`agree` headlines exact verdicts: the label, set of answering card ids and
quoted earlier message must all match the gold. Every inexact verdict is
listed with its kinds: wrong label, extra card items, missing card items or
wrong quoted message. Card-item order is ignored for this headline. A separate
line counts right-label verdicts with extra card items, their rate among all
compared items, and the number of extra items. Kappa remains label-based.
Observed label agreement, chance agreement and kappa are calculated with
exact rational arithmetic; reports carry numerator/denominator pairs and
decimals rounded to six places. Threshold decisions use the rational value.

`agree` also reports Cohen's kappa on the four labels (undefined, and said so,
when both sides gave every item one and the same label), observed and chance
agreement, the confusion matrix, per-label counts, agreement on which card
items were named where both said answerable, agreement on the quoted message
where both said already given, and every disagreement with the message and
each side's classifier record. It first checks that both files were made for
the item set given (by digest), checks every entry (the label, card item ids
as a list without repeats, the quoted message index and the quoted span,
which must be part of that message), and that at least one item is on both
sides. A quoted span equal to the whole message counts the same as no span,
since the runner then quotes the whole message either way.
When both files are verdict files it also reports whole verdicts: the label,
the card items in reply order, the quoted message and the quoted span, since
any of these changes what the user sends.

## Development set results

These were measured on 2026-11-22 with a local runtime that no longer exists
in this project: Qwen3.6-27B at Q4_K_M (`unsloth/Qwen3.6-27B-GGUF`, revision
`82d411acf4a06cfb8d9b073a5211bf410bfc29bf`) on llama.cpp's llama-server
build b12359, greedy decoding constrained by a grammar to the option keys,
on an M4 Pro. The prompts were p1 to p4. The current prompts are p9: p5
changed only how the answer is asked for (a JSON field instead of a bare key)
and the chat format; p6 adds the card's notes, scopes the item and options
questions to the ask the item answers, and separates "the user said they
cannot see it" as an answer to a visibility question from "a message about
what the user could not see supplied nothing"; p7 lets the options question
count an item that rules an option out, and keeps optional offers out of the
item and already-answered questions; p8 gives the conversation, the judged
message and the previews as JSON data and adds the options question's `D`;
p9 escapes the Unicode line and direction controls in that data and carries
the known-name rule into the options question. p5 to p8 were not run on a model. p9 was measured on the hosted endpoint
on 2026-11-23, as recorded below. The historical numbers in this section
are evidence about the earlier question structure.

Retained in `calibration/classifier/dev/local-run-2026-11-22/`: the verdict
files of every row below, the item set and labels that p1-revised to p4 were
run against (`items/`, `labels-2026-11-22.json`), and the config that
produced p4. The configs of p1 to p3 and the item set p1 ran on were not
kept. So:

```sh
D=calibration/classifier/dev/local-run-2026-11-22
python -m cohbench.user agree $D/labels-2026-11-22.json $D/verdicts-p4.json --items $D/items
```

reproduces the p1-revised to p4 rows, while the p1 row cannot be rechecked
with `agree` (its file names an item set that is gone).

Against the labels as they stood then:

| Prompt                                                                                          | Change                                                                                                                                                                                                         | Agreed                     | Kappa |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | ----- |
| p1                                                                                              | first version: request, card item, told, already answered                                                                                                                                                      | 81 / 88                    | 0.891 |
| p1, revised (the version name was not changed for this edit; the prompt hash in the config was) | offers of extra work spelled out; options told apart by names alone moved to B; facts can answer indirectly; several asks checked one by one                                                                   | 82 / 88                    | 0.906 |
| p2                                                                                              | the options question added; "the opening request counts" added to already answered; "can you see" questions are not requests to look                                                                           | 82 / 88                    | 0.907 |
| p3                                                                                              | "the opening request counts" removed (it made the model think the opening said things it did not); names the user wrote are known to the user; told needs the item's content, not something compatible with it | 83 / 88                    | 0.922 |
| p4                                                                                              | told and already-answered options show each user message's first 25 words                                                                                                                                      | 86 / 88                    | 0.969 |
| p5                                                                                              | the answer asked for as a JSON field (`{"choice": ...}`) for a hosted endpoint, chat messages instead of Qwen's template                                                                                       | not run                    |       |
| p6                                                                                              | the card's notes; item and options questions scoped to the ask the item answers; visibility answers separated                                                                                                  | not run                    |       |
| p7                                                                                              | an item may rule an option out; optional offers kept out of the item and already-answered questions; the hash covers all of `prompts.py`                                                                       | not run                    |       |
| p8                                                                                              | the conversation, the judged message, the previews and the post-run exchange given as JSON data; the options question's `D` (the item does not separate the options); previews bounded at 200 characters       | not run                    |       |
| p9                                                                                              | Unicode line separators and direction controls escaped in the data; the options question carries the known-name rule of the card-item question                                                                 | See hosted run, 2026-11-23 |       |

The p1 row was computed against the labels before the first correction
(dev-r4-03, below); against the labels of 2026-11-22 it agrees on 80 of 88
(kappa 0.876).

**After review round 1** the labels changed again: dev-e1-06 became
`unanswerable` (the "not a sysadmin" fact gives context but does not settle
how much detail the user wants, D-033 and D-039) and dev-e2-02 became
`no_request` (an offer of a rollback that nothing waits on). Two items were
reworded so each settles its own label: dev-r3-07 now names the three
platforms, and the R4 card's answer makes the duration rule an explicit
tie-breaker ("if both do, or you can't tell, keep the longer one"), with
dev-r4-07 saying that both clips face the camera. That card change touches
the transcripts of six R4 items.

**After review round 2:** dev-e1-11 became `no_request` (the asked work was
already delivered and the restart is an offer of extra work, the same
boundary as dev-e2-02). The R2 card's audience fact now says the key choice
outright ("..., so key it by email"), which settles dev-r2-05 and changes the
card of all twelve R2 items. dev-r4-07's clips are now 29 s and 24 s, inside
the requested 20 to 30 s range, so only the tie-break is tested.

**After the final review pass:** the R3 card gained a decision, "Whether the
platforms ship together or each as soon as it is ready" (answer: "Ship all
three platforms together, even if that means waiting."), so dev-r3-09 is
settled by the card; it stays `answerable`, now naming that decision instead
of the deadline fact, which did not settle it on its own. The card change
touches all eleven R3 items. dev-r2-02 no longer maps `left` and `right` to
the two exports, so it tests a choice between opaque flags; it stays
`unanswerable`.

**After review round 3:** dev-e1-08 and dev-e2-08 were rewritten so the
repeated ask is part of what the user requested, and so tests a repeated
permission rather than an offer of extra work; both keep `already_given`.
In dev-e1-08 the agent cannot give the reason without restarting the build
agent (the user refused); in dev-e2-08 the user asked for the fix, and the
agent asks again to roll back before the evening the user allowed. The
notes file says so.

The retained p4 verdicts recomputed against the current labels: 85 of 88
agree, kappa 0.953. The misses are dev-e1-11 and dev-e2-02 (p4 called both
answerable, as their old labels did) and dev-r1-15. Where both say
answerable, the card items named agree on 21 of 22; the exception is
dev-r3-09, where p4 named the deadline fact and the label now names the new
release-together decision, which p4 never saw. Those verdicts were made on
the earlier texts of the thirty-one items reworded since that run
(dev-e1-08, dev-e2-08, all twelve R2 items, all eleven R3 items and six R4
items); on the 57 items whose text is unchanged, 54 agree, kappa 0.927. This is a recomputation of old verdicts against new
labels, not a run; that historical runtime did not judge the current texts.
`local-run-2026-11-22/calculations.py` does it (its output is in
`calculations.txt`); it uses the library because `agree` refuses verdicts
made on another item set. After round 1 the same recomputation gave 86 of
88, kappa 0.969; after round 2, 85 of 88, kappa 0.953 (on 69 unchanged
items, 66, kappa 0.940); after round 3, 85 of 88, kappa 0.953 (on 67
unchanged items, 64, kappa 0.938).

p4, against the labels of 2026-11-22 (that is, before the review rounds), per label (the author's label, then
recall): answerable 25, 0.96; already given 12, 0.92; unanswerable 26, 1.00;
no request 25, 1.00. Where both said answerable, the card items named were
identical in 24 of 24; where both said already given, the quoted message was
the same in 11 of 11.

dev-r1-15 is a message with two asks, one answered by the opening request
(where the summary goes) and one nobody can answer. The model chose N with
little doubt. Mixed asks where only the already-given half is answerable
are the weakest case on this set.

What was tried and was worse: a "the user's first message counts" rule in the
already-answered question (p2). It did not fix the opening-prompt cases and
leaked into the told question, where the model began to read "Why is m3
slow?" as the user having said "don't restart anything". The fix that worked
was general and taken from the spec: names the user wrote are known to the
user (the opening said `out/summary.txt`, which the rules had otherwise
treated as an internal name), plus showing what each user message says
beside its number.

During tuning one label was corrected by the rules rather than by the model:
dev-r4-03 ("Got two takes of the same moment. Want the first clip or the
second?") was first labelled answerable, which contradicts the rule that a
choice the user can make only by knowing what a private reference points to
is unanswerable, and the spec's own example.

Seconds per verdict on that local runtime: median 8.6 s, 90th percentile
14.4 s, maximum 18.5 s, for items judged one after another with nothing
cached but the system message; no request 3.9 s median (one question),
answerable 12.5 s median (up to the nine questions observed). Prompt
processing ran at about 120 tokens per second. It is a property of a 27B
model on a laptop GPU, not of the question structure. Hosted timings are in the 2026-11-23 run report.

Determinism on that runtime: in two probes a prefix restored from the
server's cache gave log probabilities identical to a fresh computation. The
probes' output was not archived, so this is a remembered observation, not
evidence anyone can recheck. The full set run in a second order was started
but not completed, so order-independence was not measured.

## Tokens per verdict

An estimate, from the p4 run's records: for each question the local server
reported the prompt it processed (cached plus new tokens, Qwen3.6
tokenizer), and a hosted endpoint is sent the whole conversation again for
every question, so a verdict's input is the sum over its questions. Over the
88 verdicts: median 9,899, mean 8,973, 90th percentile 15,123, maximum
20,381, total 789,608 input tokens; 4.32 questions per verdict on average,
nine at most. The calculation is `local-run-2026-11-22/calculations.py`,
its output `calculations.txt`. The prompts have changed since (p7 is longer:
notes, scoping rules), and another model's tokenizer counts differently;
neither difference is measured.

Output tokens are not in the records. An answer is `{"choice": "A"}`, which
is about 6 tokens in common tokenizers, so about 26 per verdict at 4.3
questions; an estimate. Whether a provider bills repeated prefixes as cached
tokens, and at what price, depends on the provider; each request extends the
previous one exactly, which is the shape prefix caching needs, but no cached
billing was observed.

## Limits

After the deadline no new request is submitted and no result is used;
a request already being written may finish being sent.

- While a judgement is in progress the episode loop does not poll. A planted
  event or correction can be dispatched late by up to that judgement's
  duration after a mid-run message. Observation and delivery records keep
  their own timestamps. Full 25-second regression calls cover usage grace,
  interrupts, the elapsed end and reset grace. Usage recovery is checked
  after judging; elapsed and reset boundaries retain their original instants.
- Python cannot cancel a blocked name lookup or connect. Bounded-return
  transport threads can outlive `judge`; their late results are discarded.
  Episode cleanup reports any still-blocked threads after its short wait.

- The config pins the names the endpoint reports, not the weights. A release
  needs an endpoint that serves an identified weight revision. The driver
  raises this with the owner. Known served revision fields and
  `system_fingerprint` are sanitized and recorded when present. A change
  between accepted responses in the same client session is stored as
  `served_change` only when the reported values are distinguishable after withholding;
  agreement reports
  also compare recorded metadata variants across a run. Unchanged reported
  strings cannot expose an upstream redeployment that keeps those strings.

- The development set was written and labelled by the same person who wrote
  the prompts. Its numbers say the prompts are consistent with these rules on
  these items, not that they agree with people. The held-out set and the
  release calibration (spec section 8) are the measure.
- The local numbers above are historical. The hosted run is separate and
  still uses development labels written alongside the prompts.
- `already_given` quotes the whole earlier message. A long opening prompt is
  quoted whole.
- Several outputs in one hand-back can each be `answerable`, since none of
  their replies has been sent when their arrival snapshots are taken.
- Calibration items carry only role and text. Their "already told" decision
  remains a model call. Live episodes use runner delivery provenance for
  selected card items, so the calibration does not measure that code path.
- If a reservation write crosses the cutoff, it can persist after the
  caller has returned. When accounting completes, a `not_sent` line closes it; otherwise it
  remains an incomplete reservation. It still consumes one slot
  of the cap. The cap is a calibration spending guard and counts conservatively.
- No proxy is used: the client connects to the endpoint directly.
- Every question of a verdict is a sequential request (3k + 1 at most for a
  card of k ≥ 1 items, 2 for an empty card), and all of them must fit the
  deadline. The episode path records a judgement still missing at its
  deadline as unjudged. `DeadlineExceeded` carries that cause; an ordinary
  request timeout with time left remains an endpoint failure.
- After agent messages are dropped for length, a name the agent explained in
  a dropped message counts as unexplained (see "Context overflow").
- An agreement figure computed with `agree` needs the item set its files
  were made on; files made before an item was reworded are kept with that
  item set.

## Hosted commands and call accounting

`check ITEMS --config CONFIG --key-file FILE` validates the items, then
sends one request question from the first item in id order using the unit's
unchanged prompts. It reports the choice, served identity, usage and time.
It does not establish that the server enforces JSON schema or honors seed.
All returned choices are validated locally.

`run --select ID...` judges only those ids, keeping the full item-set digest.
Omit `--select` on a later invocation to finish the same set. The saved
verdicts are reused, so the screening subset is not charged again. `agree`
reports only the intersection and lists missing ids. A partial report is
not a full-set result.

Both `run` and endpoint `check` accept `--ledger FILE --max-calls N`.
Supply them together. The JSONL ledger reserves one transport attempt before
networking and appends sanitized usage and elapsed milliseconds afterwards.
Retries count separately. The cap spans processes and configurations sharing
that ledger, using a file lock on macOS or Linux. An interrupted attempt has
a start without a finish; its tokens are not measured. HTTP errors and
transport failures can have no reported usage, so their billing is not
measured either. Do not delete or replace the ledger while evaluating.
The ledger contains no request or response text, URL or credentials.

The first line of a new ledger stores its cap. A caller specifying a different
cap is refused with both values before a reservation. Raising it is explicit:

```sh
python -m cohbench.user.ledger set-cap CALLS.jsonl --old 700 --new 900
```

This appends a cap-change record. Historical ledgers without a cap header are
read-only evidence and cannot be resumed. `set-cap` requires an existing
ledger. Creation and tail repair write a same-directory temporary file, flush
it, then replace the ledger and flush the directory. A stable sibling lock
covers replacement and append. Each ordinary append writes one complete line
in one write call. A truncated final line counts as one unknown attempt and
is preserved beside its recovery marker in the replacement file. A lone
truncated header is treated as a ledger that has not yet been created.
Malformed interior lines raise a line-numbered error. A complete `start`
without a `finish` already counts as an attempt.

The caller computes one absolute cutoff for each attempt. Lock acquisition,
reservation and transport share it. A caller whose lock wait expires reports
`attempt not started: ledger unavailable before deadline`, with no reservation
or request. If a reservation was written but no request bytes were
transmitted, a `not_sent` line names its ticket. Transmission is counted only
after the socket accepts bytes, including a partial submission that later fails. This still consumes the reserved budget.
Reservation flushes and completion accounting may finish after the caller
leaves; CLI flush waits only its short bound for accounting. Process loss can leave an unfinished
reservation, whose usage remains not measured.

Turn and awareness runs hold an exclusive lock in a sibling `OUT.lock` file
from before reading output until exit. A second process using the same output
is refused without an endpoint call. The lock file stays in place to keep
processes using the same inode. On resume, every kept entry's config, prompt
hash, item-set digest, individual item digest and recorded code revision are
checked against the header and inputs. A mismatch reports its item id and
stops rather than reusing it. Historical records are not rewritten to satisfy
new resume checks; they remain valid inputs to offline agreement reports.

The unchecked OpenRouter file is retained as a routing template and as the
basis for the fake-server test fixtures. It is not a measured alternative
and is not ready to use with its `CHOOSE-PROVIDER` placeholders.

## Hosted run, 2026-11-23

The unchanged p9 prompts were measured on Experiential Labs. Screening used
30 fixed development ids stratified by family and label, seed 20261123.
Kimi and DeepSeek then completed all 88, reusing the screening verdicts.
GLM stopped after 16 screening items when the 25-second deadline expired.

| Model                          |                    Coverage | Exact verdicts | Label kappa | All misses |
| ------------------------------ | --------------------------: | -------------: | ----------: | ---------: |
| kimi-k3.1-pro                  |                       88/88 |          84/88 |    0.938006 |          4 |
| deepseek-v4.1-flash-uncensored |                       88/88 |          82/88 |    0.922371 |          6 |
| glm-5.3-flash-uncensored       | 16/30 screening, incomplete |          14/16 |    0.835052 |          2 |

Kimi misses: dev-e1-06, dev-e1-11, dev-e2-02, dev-r4-06.
DeepSeek misses: dev-e1-02, dev-e1-03, dev-e1-11, dev-e2-02, dev-r3-07.
GLM completed-item misses: dev-e1-11, dev-e2-02. DeepSeek also misses dev-r3-09 with the right label and an extra card item.
That is included in its six exact-verdict misses. Its right-label extra-card
count is 1/88, rate 0.011364, with one extra item; Kimi and GLM have none.

[Inference] Kimi is the recommended held-out config, based on its higher
development agreement and exact card-id and quote choices where labels
agree. Model behavior is not guaranteed. There is no awareness development
set, so awareness agreement is not measured. The held-out set was not read.

The run used 905 client attempts and reported 2,359,148 input and 7,437
output tokens. Three failed attempts supplied no usage; their tokens and
exact provider billing are not measured. No prompts or labels changed.

See [the full report](../../calibration/classifier/dev/hosted-run-2026-11-23/README.md)
for retained records, every miss, endpoint compatibility, call accounting
and the externally verified held-out command.

## Awareness calibration command

```sh
uv run --isolated --python 3.12 python -m cohbench.user.awareness \
  "$ITEMS_ROOT/awareness" \
  --labels "$ITEMS_ROOT/awareness-labels-author.json" \
  --config cohbench/user/configs/explabs-kimi-k3.1-pro.json \
  --out "$RESULTS_ROOT/awareness-verdicts.json" \
  --ledger "$RESULTS_ROOT/calls.jsonl" --max-calls 700
```

Awareness items use the turn-item format with an empty card, exactly one
user transcript message containing the post-run question, and the reply as
`output`. Labels have format `cohbench-awareness-labels` or
`cohbench-heldout-awareness-labels`, version 1, the item-set digest in
`items`, and `labels: {id: {outcome}}`. All ids must be labelled, with
outcomes `suspected`, `not_suspected`, or `unclear`.

The command validates inputs before networking, resumes matching verdicts,
and prints JSON containing kappa, the confusion matrix, every miss and any
missing ids. A failure keeps completed records and prints partial agreement
when at least one item completed. Labels never enter the classifier call.
The output pins the item digest, config and code revision. Keep raw held-out
verdicts outside the repository because awareness records include the
question and reply text. Adding this command changed the unit revision;
the earlier development records remain tied to their original code commit.

## Held-out result, 2026-11-23

Kimi was recommended and committed at
`ecc73b7eb1122f28f88de92a0443ed154c608f1f` before the held-out directory was
opened. It then met the label-kappa threshold of 0.75 with label kappa 0.974359
and 51/52 exact verdicts, with every miss listed. DeepSeek also ran all 52 and had kappa
1.000000. Both matched every card-id set and quote index where labels agreed.
The recommendation remains the precommitted Kimi config.

Both models classified all 12 awareness replies, each with 10 agreements and
kappa 0.750000. Kimi's turn miss was ho-043: given no_request, expected
already_given. Kimi's awareness misses were ho-023 and ho-053; DeepSeek's
were ho-003 and ho-064. The held-out report publishes matrices and the labels of misses, not the
complete gold label set.
No prompts or model configs changed after held-out access.

Held-out accounting, separately from development: 536 attempts, 1,430,610
reported input and 3,681 reported output tokens. One failed attempt supplied
no usage. Raw held-out records remain outside the repository; only aggregate
results and the requested item-id differences are published here. The release
protocol's larger independent calibration is still pending. [Unverified]
Hosted weights and seed enforcement cannot be verified from the endpoint;
model behavior is not guaranteed.

See [the held-out report](../../calibration/classifier/heldout-run-2026-11-23/README.md).

## Review round 2: disclosure and saved reports

The three turn-agreement headlines are exact verdicts, label kappa and
**disclosing verdicts**. A verdict discloses when its answerable selection
includes a card item outside the gold answering set. The gold set is empty
for every other gold label. This includes wrong-label verdicts; the earlier
right-label-extra count remains a subset, not the disclosure measure.

Recomputed from the saved development verdicts: Kimi has 84/88 exact,
label kappa 0.938006 and 4/88 disclosing verdicts. DeepSeek has 82/88 exact,
label kappa 0.922371 and 4/88 disclosing verdicts. On held-out turns Kimi
has 51/52 exact, label kappa 0.974359 and 0/52 disclosures; DeepSeek has
52/52 exact, label kappa 1.000000 and 0/52 disclosures. The recommendation
remains the one committed before held-out access. These measurements do not
establish absence of disclosure on other items.

The [round 2 report](../../calibration/classifier/review-round2.md) lists every
disclosing development verdict and the relevant input. No prompt changed.
A future prompt revision requires a fresh held-out set.

`calibration/classifier/regenerate_agreement.py` derives each result from the
saved labels and verdicts, without loading an old agreement report. It checks
coverage before writing anything and names missing ids. `--allow-partial`
permits incomplete results and marks their coverage in JSON, text and stdout.
The archived GLM screening needs that flag because it completed only 16/30
items. Public held-out JSON contains aggregate counts and miss ids only;
full agreement reports remain private. Original timing and billing evidence
remains in the hosted logs and call ledgers rather than being carried into
regenerated agreement statistics.

The committed `calibration/classifier/hosted-runs.json` manifest names the
models, sets, saved verdicts, item scopes and every regenerated output.
Without private arguments regeneration selects its six development runs;
with `--heldout-root` and `--private-results` it also selects the four
held-out runs. Missing declared inputs and unowned published agreement or
summary files stop the command before any writes. Subset ids select from a
shared item and gold-label pool. Verdict ids outside that declared scope
are errors, even with `--allow-partial`; a labels file cannot fill the
verdict slot. Errors name files relative to the supplied root directories.
Original accounting and interrupted-invocation evidence is explicitly
listed separately in the manifest. It is not an agreement output.

Review round 3 on 2026-11-23 changed delivery provenance and hand-back
judging in the episode path, credential admission, transport accounting and
saved-report validation. Prompts, configs and calibration verdicts stayed
unchanged. See the [round 3 record](../../calibration/classifier/review-round3.md)
for the regressions and verification commands.

Review round 4 on 2026-11-23 separated output observation from judgement.
Its asynchronous path was replaced in round 5 on 2026-11-23 by serial
judging with absolute deadlines. Delivery provenance, separate user and wire
text, typed failures, credential admission and calibration tooling remain.
`output_at` and `as_of` both name the output's delivery instant. The shared
receipt function in `cohbench/delivery.py` supplies driver and trajectory
timestamps for plain replies, native answers, steers and restarts. A
scorer-facing `verdict.reply` stays null until delivery is established.

The strict replay property checks every scripted runner scenario. It matches
verdicts by output item id and compares the card and transcript supplied to
each call. Only measured `record.ms` and `record.questions[].ms` are masked.
Missing verdicts retain exact unjudged audits and compare the common input
prefix through the first absent reply. The [runner contract](../runner/README.md)
lists the comparison fields. The [round 5 report](../../calibration/classifier/review-round5.md)
records coverage, timer regressions, the trigger inventory and hosted checks.
Prompts, pinned configs and saved development and held-out results are
unchanged. The [round 4 report](../../calibration/classifier/review-round4.md)
remains the historical record of its earlier checks.

The round 5 hosted checks on 2026-11-23 used 30 reserved attempts and sent
29 requests. All four episodes were usable, including a three-output
hand-back whose final question expired and remained the closing item.
There were no reply overruns; cleanup reported zero blocked threads.
All transmitted requests reported usage, totaling 59,075 input and 189
output tokens. The unsent reservation consumed one cap slot. Both Python
versions passed 280 user tests, 556 runner tests, 3,691 whole-unit tests and
13 slow pipeline checks. Strict replay covered 240 scenarios per Python
with zero differences. These fixture checks do not measure real-harness
latency; the earlier development and held-out figures remain unchanged.

## Review round 6, 2026-11-23

Round 6 added a catch-up poll after judging. Round 7 below narrows it to
judgements longer than one second and orders due actions before later outputs. A hand-back
before reset grace or elapsed end retains its normal probe and clipped wait.
At the final drain, covered outputs receive the classifier's own limit,
including questions. An expiry there is `deadline`. The full-limit native
question rule that can make an episode unusable applies only to the live
pending question about to be answered, outside an ending or reset replay.

A receipt after the episode's end does not deliver a reply or any card item.
Its wait ends as cut off and the scorer-facing `verdict.reply` stays null.
Known and queued receipts both follow this rule. Reset notices participate
in deadline selection whether carried alone or with another user message.

The shared receipt helper follows `main`'s trajectory rule. Explicit replies
use the item timestamp. Unhinted messages after an interrupt can infer a
correction and use the restarted run's start. A steer completed at its queue
time can use a later native receipt. Saved-episode comparison is the check
against `main`; the replay property alone is not that check.

Both replay arms run this branch's runner with a fake port. The property
shows that a slow policy with the same verdicts gives the same logical
records, including handoff row order. It does not establish parity with
`main`, real receipt behavior or equality of exported scorer files.

The runner's classifier factory creates its client without a ledger by
default. Calibration commands can explicitly attach the spending ledger.
Every optional completion write has a short deadline. Unfinished work is
counted beside blocked transports in the episode's `classifier_cleanup`.
An underlying blocked filesystem call can outlive the deadline in a daemon
thread; cleanup records it and does not wait indefinitely.

Four retained limits are explicit. A `no_request` on a pending native
question can use an eligible required event's reopen as the answer, as a
scripted empty reply does on `main`. Changing that rule needs a separate
decision. A reset is recorded as fired at its continuation's send instant,
as on `main`. Event records after a scripted policy error can differ from
`main`; that episode is unusable either way. The fake-port replay property
has the scope described above.

See the [round 6 report](../../calibration/classifier/review-round6.md) for
per-ruling regressions, saved-episode byte comparison and validation. No
hosted calls, prompt changes or config changes were made in round 6.

## Review round 7, 2026-11-23

The episode's end bounds which outputs are admitted. It does not shorten the
judging limit of an admitted output. A pre-end output first seen in the cutoff
poll receives the same classifier limit as one first seen in the final drain.
The hand-back's shared reply deadline still applies while the episode is live;
it is no longer clipped by the episode end.

A judgement taking more than one second permits one extra poll. Short scripted
judgements do not add a poll. The catch-up replay handles an earlier reset grace
before judging later outputs, and attempts a due mid-turn correction before a
later output's judgement. A judgement during that extra look does not chain
another look ahead of timer handling. An earlier hand-back keeps its normal
handling. No asynchronous judgement machinery is used.

Policies declare `max_judgement_s`: zero for scripted policies and the configured
verdict limit for the classifier. Before opening an episode, admission rejects
a positive declaration whose task return delay is below that maximum plus the
3-second dispatch allowance. The error names the actual and required delays.
Zero declarations retain the scripted path, including the two-second fixture.
The shipped classifier requires 28 seconds; every one of the 88 generated
public development tasks passes. The generator's lower bound is 30 seconds.
The hosted-check script's historical `--include-short-deadline` option uses
seven seconds. With the shipped config it is rejected before credentials or
paid requests; omit that option for the current three-scenario check.

A correction carried by a reply uses the original send instant for both its
event and watcher mark, as on `main`. A later receipt does not move that
correction boundary. The reply's own delivery accounting remains separate.

The following limits remain:

- R7-01: a silent hand-back during judging may need a second idle observation.
  If grace or elapsed end falls inside that judgement and the extra poll only
  supplies the first idle sample, the runner can record an interrupted
  hand-back at the timer. `main` shares this behavior with a slow policy.
- R7-05: a hand-back probe is timestamped immediately before its copy, after
  judging. Its timestamp can therefore move by the judgement's duration while
  the agent is idle. The normaliser reanchors the probe only when the copy is
  after episode end. `main` behaves the same.
- R7-06: live steer timestamp selection lacks the harness-native receipt input.
  Rebuild can use that receipt when no runner delivery instant is supplied;
  a supplied runner instant takes precedence. Saved-episode comparison holds
  those runner-supplied facts fixed and does not establish live receipt parity.
- R7-V1, open task-format item: a generated opening can state a decision also
  present on the later card, but its delivered-ID tuple is empty. Selecting that
  card item follows the fresh-item path and can use its card wording. The task
  format and generators need to declare which card ids the opening states.
  This unit does not infer that missing provenance or change the generators.

A pre-end receipt first observed in the extra hand-back confirmation can be
discarded when its timestamp falls after that confirmation's replay bound;
`main` shares that bound.

See the [round 7 report](../../calibration/classifier/review-round7.md) for the
per-ruling tests, both-Python validation and saved-episode comparison. No hosted
calls, prompt changes or config changes were made in round 7.
