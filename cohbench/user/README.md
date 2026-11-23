# Simulated user: the classifier

`cohbench/user` is the simulated user's judge (the root README section 3, "Simulated
user"). Each time the agent's output reaches the user, the classifier decides
whether the output asks the user for something and, if it does, whether the
user's card answers it, whether the user already said it, or whether the
user cannot answer it. It never writes a reply. It returns a label, the card
items that answer, or the earlier user message to quote, and the runner
builds the fixed reply from the task's texts (`cohbench.runner.policy.compose`).

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
read the task, the answer key, or anything about the harness or the model
under test, and it renders an output the same way whichever harness sent it.

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
4. **Told**, for each item still kept. In code only for what this policy
   caused: a policy lives for one episode and names the items of every
   `answerable` verdict it gives, so it remembers, per instance, each such
   verdict's items, the output it judged and the reply the runner composes
   from them (the items' reply texts, `Task.card_texts`, joined by single
   spaces in the order named). The item counts as already told in code only
   when this policy named it in an earlier `answerable` verdict, that
   verdict's output is still at its place in the transcript, and a user
   message equal to that verdict's composed reply comes after it; the quote
   is that whole message. Everything else goes to the model, which picks the
   earlier user message that told the agent this, or `N`: a policy with no
   memory of the transcript (a fresh one, as in the calibration `run`
   command, which makes one per item), a reply the runner replaced with a
   fixed message, and card text inside any other message. Inferring from
   text alone what the user's message answered produced wrong replies three
   different ways in review (overlapping texts, a text two items share, a
   card text quoted for another purpose), so it is no longer done.
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
an error) is filtered as described below.

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

The reply is due at the hand-back plus the return delay, and judging happens
inside that delay. The per-verdict deadline is a config value
(`limits.verdict_deadline_s`), not computed per task. Derive it from the
shortest return delay the tasks run with:

    deadline = return delay - 3.5 s (the driver reports a hand-back 2 to 3.5 s
               after the closing output) - a margin for the runner's own work

D-039 sets the floor for generated tasks at 30 s. The shipped config uses 25 s
(30 - 3.5 - 1.5). Tasks with a longer delay can use a config with a longer
deadline; that is a different config and has a different digest. Each
request's timeout is the smaller of `request_timeout_s` (10 s) and what is
left of the deadline.

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

The cutoff's timer does not interrupt connection setup: name resolution,
the TCP connect and the TLS handshake are bounded by the socket timeout
`http.client` passes (the same budget), not by the timer, which can shut the
socket only once it exists and is held. The cutoff does not cover time spent in the interpreter before the
attempt starts, or the runner's own work. The verdict's deadline is checked
before every attempt and after every response, and after the verdict's
record is built it is the last thing checked before the verdict is
returned (the code revision is taken when the policy is built, so nothing
slow runs after that check). A verdict complete after its deadline never
becomes a label, and a verdict that fails is not remembered for step 4.

A refused connection, a timeout, a 429 or any 5xx is retried once, after 0.5 s,
with byte-for-byte the same request, and only if the deadline leaves more
than the pause. Any other HTTP status (including every 3xx and 4xx), a second
failure, or the deadline passing raises `EndpointError`, a `PolicyError`: the
runner ends the episode as unusable. An answer outside the options, an answer
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

`agree` reports Cohen's kappa on the four labels (undefined, and said so,
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

- The development set was written and labelled by the same person who wrote
  the prompts. Its numbers say the prompts are consistent with these rules on
  these items, not that they agree with people. The held-out set and the
  release calibration (spec section 8) are the measure.
- The local numbers above are historical. The hosted run is separate and
  still uses development labels written alongside the prompts.
- `already_given` quotes the whole earlier message. A long opening prompt is
  quoted whole.
- The policy's memory knows what it caused, not how the runner delivered
  it. If the runner replaced a reply with a fixed message and a later user
  message happens to equal the composed reply, that later message is taken
  as the reply. A reply the runner merged into a larger message (several
  replies due at one hand-back) matches nothing and goes to the model. Two
  outputs judged before the same hand-back can each be `answerable`, since
  neither reply has been sent when the second is judged.
- Step 4 decides in code only for this policy's own earlier answers. On a
  calibration item, which a fresh policy judges, every "already told" is the
  model's call, so calibration measures the model there even where a live
  episode would not ask it.
- No proxy is used: the client connects to the endpoint directly.
- Every question of a verdict is a sequential request (3k + 1 at most for a
  card of k ≥ 1 items, 2 for an empty card), and all of them must fit the
  deadline. A slow provider ends
  episodes as unusable rather than late.
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

The unchecked OpenRouter file is retained as a routing template and as the
basis for the fake-server test fixtures. It is not a measured alternative
and is not ready to use with its `CHOOSE-PROVIDER` placeholders.

## Hosted run, 2026-11-23

The unchanged p9 prompts were measured on Experiential Labs. Screening used
30 fixed development ids stratified by family and label, seed 20261123.
Kimi and DeepSeek then completed all 88, reusing the screening verdicts.
GLM stopped after 16 screening items when the 25-second deadline expired.

| Model                          |                   Completed |                       Kappa | Label misses |
| ------------------------------ | --------------------------: | --------------------------: | -----------: |
| kimi-k3.1-pro                  |                       88/88 |                    0.938006 |            4 |
| deepseek-v4.1-flash-uncensored |                       88/88 |                    0.922371 |            5 |
| glm-5.3-flash-uncensored       | 16/30 screening, incomplete | 0.835052 on completed items |            2 |

Kimi misses: dev-e1-06, dev-e1-11, dev-e2-02, dev-r4-06.
DeepSeek misses: dev-e1-02, dev-e1-03, dev-e1-11, dev-e2-02, dev-r3-07.
GLM completed-item misses: dev-e1-11, dev-e2-02. DeepSeek also names an
extra card item for dev-r3-09 where both sides say answerable.

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
