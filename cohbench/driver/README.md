# Episode driver

`cohbench/driver` runs one episode through T3 Code: it starts the
environment (`cohbench/env`), talks to the T3 server inside the harness
container, and gives a runner the operations the root README section 3 needs. It
hides Docker and T3. Python 3.12, standard library.

The T3 client (`t3client.py`) is the driver proof's, copied: every request is
a one-shot `node -` inside the container with its program on stdin; reads
are HTTP with an owner session from `t3 auth session issue`; actions are T3's
WebSocket RPC. Commands from this socket are stamped `createdBy: "user"`, so
the harness gets each message exactly as sent.

## The API

```python
from cohbench.driver import Episode, TaskInfo, install_sigterm
from cohbench.env import Upstream, provision

images, image_record = provision("pi")          # once; shared by episodes
install_sigterm()                               # SIGTERM becomes SystemExit, so the with block closes
with Episode("pi", model="claude-haiku-5-5", upstream=Upstream.scripted(script), images=images,
             workspace=Path("task/ws"), manifest=Path("task/ws.manifest.json"), out_dir=Path("runs/e1"),
             mode="coached", coached_sentence="...") as ep:
    ep.start("Opening prompt")
    turn = ep.wait(timeout_s=900)
    if turn.state == "question":
        ep.answer(turn.question, "blue")
    ...
    ep.export(task=TaskInfo(identifiers=..., observations=..., episode=...))
```

`Episode(harness, model=, upstream=, images=, workspace=, manifest=, out_dir=,
mode="default", coached_sentence=None, options=None,
harness_default_model=None, diagnostic_question_tool=False, watcher=WatcherSpec(),
driver_events=DRIVER_EVENTS)`.

**Deadlines.** Every public call takes `timeout_s` and holds to it. The
driver turns it into one monotonic deadline and passes it down to every T3
read and call (each `docker exec node -` gets the time left, and the node
program's own timer that less 1.5 s) and, through
`cohbench.env.docker.deadline`, to every Docker command and scanner the call
runs, watcher marks, `gateway verify`, the waits for T3 and the scripted
server to come up included; a retry loop stops when the deadline has
passed. There are no per-read minimums and no fresh default limits inside a
call. The exceptions, all of them: removing what a call created is never
cut short; a probe's recovery of the paused main container has its own
60 s limit; `wait` may run past `timeout_s` by at most `finish_s` (default
15 s, see `wait`); `start` opens the environment (`open`: images, checks,
T3 ready) before its `timeout_s` begins, which covers the launch only; and
the export's host-only stages (normalisation, validation, publication)
cannot be cut short inside one, so the deadline is checked between them.

**Sends.** Every send first records its `Sent` with the ids the driver chose
(command id and message id), `status: "dispatching"`, then dispatches. T3
answers `accepted` or a failure (`rejected`, raised as `EpisodeError`). A
call that never reached T3 (too little time left to start the exec, or the
socket never carried it) is `unsent`: nothing changed, it is not
reconciled, `Sent.status` is `unsent`, and the call raises a non-fatal
`DriverFailure` with `unsent` True (a correction's entry is rolled back);
the caller carries on as if nothing was sent. A lost or timed-out answer is
reconciled by those ids, reading T3 until the deadline (a third of the time
left, at most 10 s and never so much that the dispatch cannot start, is
kept for this). Every path that provably sent nothing is `unsent`: too
little time left to start the exec, the deadline checked before `docker
exec` was spawned, and a failed websocket ticket (the socket never opened).
Where a call had made a change before its last part went unsent, the
non-fatal `DriverFailure` says which part happened in `done` and
`not_sent`: `refuse_secret` (`["decline"]`, `["refusal words"]`), the
interrupt-then-queue correction (`["interrupt"]`, `["correction"]`), and
`reset` (`["handoff"]`, `["continuation"]`, with `handoff_done` True). After
an unsent continuation the handoff stands, the new segment has no
dispatch (`Episode.awaiting_launch`, `handoffs[-1]["continuation"]`), and
the caller either calls `reset` again, which then only launches, or ends
the episode: the export then leaves that segment out of the trajectory and
bundle (its watcher log stays in the raw record) and the bundle's end is
the end instant, not a mark. The reconciliation reads (lost answers only) are the
message in the thread (a lost launch is looked for in every thread the
shell lists), the runtime request resolved, the secret item settled. Still
not found at the deadline, the status is `unknown` and the call raises
`DriverFailure` with `fatal` True; every later call raises it again, and
the caller closes the episode. A read of T3 that fails before anything was
sent raises `DriverFailure` with `fatal` False: nothing changed, and the
call may be retried. That is the rule for every call that does more than
one thing (`answer`, `correct`, `refuse_secret`, `interrupt`, `reset`,
`probe`): a non-fatal failure, `TurnNotRunning` or `MainRunEnded` is only
ever raised before the call's first change to T3's or Docker's state;
after it, a failure is fatal, or (`refuse_secret`) the remaining step is
done by the other route, with one exception: a last part that provably
never reached T3 (`unsent`) is a non-fatal failure that names what was done
and what was not (`done`, `not_sent`). Every outbound user text (the opening prompt, replies,
corrections, answers, refusals, the reset message, a probe question) is
checked for the upstream credential first and refused with `EpisodeError`
if it holds it.

| Operation | Returns | What it guarantees |
|---|---|---|
| `start(text, timeout_s=120)` | `Sent` | Starts the environment (in its `prepare` hook: the harness settings, T3 ready, the project; not under `timeout_s`), then launches the thread. The episode clock starts at this dispatch. |
| `wait(timeout_s=600, poll_s=1, finish_s=15)` | `TurnResult` | Below. |
| `reply(text, timeout_s=60)` | `Sent` | Needs a hand-back with nothing pending: the whole tree idle (below). |
| `answer(question, value, timeout_s=60)` | `Sent` | The `Question` from `wait`, in its own thread. One string for a one-question prompt; a dict by question id or a list for several. An answer that matches an option is sent as its value. `Sent.request_id` is the question's turn item id; T3's runtime request id is `Sent.rpc_request_id`; `Sent.submitted` is the map actually sent to T3 (question id to value, a label already turned into its option's value). Accepted only when the request is `resolved` by this answer: T3's recorded answers equal the submitted values, or, where T3 records none (the diagnostic dialog's requests showed none), T3 confirms this command id, whose receipt it replays when it took the command. `cancelled` or `expired` (T3 writes cancelled when it dismisses a native user input), other values, or a resolution by another command is a rejection (`EpisodeError`, `Sent.status` rejected). |
| `answer_secret(secret, values, timeout_s=60)` | `Sent` | The `SecretRequest` from `wait`, in its own thread. One value per field (a string when there is one field), or one non-empty secret. Never recorded. A value that contains the upstream key is refused before anything is sent. `Sent.request_id` is the request's turn item id. Accepted when the item goes to `saved`; another end is a rejection. |
| `correct(text, timeout_s=60)` | `Sent` | The whole tree's state decides, right before dispatch, after each read while waiting for capabilities, and again after a rejection, in this order: a request to the user pending anywhere (also with the main run still live) raises `TurnNotRunning`, the hand-back has happened; a live main run is steered; a main run that has ended while child work remains (a live, queued or background child run, or a continuation T3 reserved) raises `MainRunEnded` (an `EpisodeError`, `retryable` True, nothing sent): the runner holds the correction and sends it at the continuation run or the hand-back, whichever comes first; an idle tree raises `TurnNotRunning`. A thread whose state cannot be read (it may hold a request) defers the correction with a non-fatal `DriverFailure`, nothing sent, on every read, the capability wait included. A failed read before anything is sent is `DriverFailure` (not fatal). Waits within `timeout_s` for the provider session's turn capabilities (raises rather than guess from an empty list), then `native_steer`, else `interrupt_restart`, else interrupt (queue not held), wait for the run to end, and queue; once that path has interrupted the run every failure is fatal. A steer T3 rejects with the main run still live is `EpisodeError`. `Episode.corrections` records the requested mechanism; `Sent.mechanism` and the record's `observed` are the mechanism seen once the new message's run is known (`steer_requested_new_run` if T3 started a new run for a steer), None until then, and filled in when a later `wait` or `correction_delivery` resolves the run. |
| `correction_delivery(sent, timeout_s=20)` | `Delivery` | When the harness took a correction (T3's `completedAt` on its message) and the watcher mark the driver took the moment it observed that: `mark_label`, `mark_batch`, `mark_requested_utc`, `mark_acked_utc`. `mark_taken` False with `mark_error` says no mark could be taken (no watcher, or no acknowledgement); `delivered_utc` None says delivery was not observed yet. Every `wait` poll and `correct` itself look for delivery and request the mark at once, so the mark does not wait for this call. `Sent.delivery` holds the same record once observed. The runner uses this mark as the correction's boundary. When delivery is already known as `correct` returns, the returned `Sent.delivery` holds it. The mark names the delivery's instant (`mark_at` equals `delivered_utc`); `mark_straddled` is the batch whose span holds the instant (from the lower bound of its first write, the time of the watcher's read before it, to its last write; D-040), or None, and `mark_covered_utc` the instant up to which the watcher confirmed coverage. A mark the watcher did not accept is `mark_taken` False with its reason. |
| `refuse_secret(secret, text, timeout_s=60)` | `Sent` | Declines a pending secret request with T3's `secrets.answerRequest {type: "decline"}` and confirms the item went to `declined`. The agent's `request_secret` call then returns `{status: "declined", note: "The user declined. Don't ask for it in chat or another way; say what's blocked and go on without it."}` (T3's `SECRET_REQUEST_UNSAVED_NOTE`). Then `text` goes to the main thread by the route that applies at that moment: steered (or restarted, per the harness's steering) into the run while it runs, else queued as the next message. The route comes from a consistent sample: after the decline the run resumes and the tree changes, so an inconsistent or failed sample leads to another one while the call has time; the words are queued only when a consistent sample shows the main run ended, when the sampling time runs out, or when a steer T3 rejects because the run just ended, and `Sent.route_basis` says which. The words are never lost; once the decline is in T3 every failure is fatal, never retryable. The decline adds no user message of its own, so the refusal's words are the only user message. Recorded as kind `refusal`, with the route in `mechanism`; the export gives it the trajectory user kind `reply`; it gets no `correction_delivered` event and no correction record. The decline is its own `Sent` (kind `secret`, mechanism `decline`). No value is sent. |
| `interrupt(timeout_s=60)` | dict | Stops the whole tree by the one definition of busy (`turns.busy_state`). Each round reads the tree and sends `run.interrupt` (queue not held) to every live run in every thread (which cancels the requests pending in it), `queued-run.cancel` to every queued run, and `thread.stop` to a thread whose only work left is provider background work; a target still busy after `RETRY_S` (1.5 s) is sent again (T3 can reject a queued run it then starts). Returns once two consistent samples, `SETTLE_S` (1 s) apart, read quiet, also when nothing was sent: `attempts` (each with its outcome: accepted, rejected, lost or unsent, and the run's status in the final sample as `observed`), `stopped` (the attempts T3 accepted, and those whose run the confirmed tree shows interrupted or cancelled even if the receipt was lost), `interrupted` (their thread and run ids). The next hand-back is reported as `interrupted`. On a quiet tree it sends nothing and returns normally (`stopped` empty). Before anything was sent a failed read is `DriverFailure` (not fatal); after, a tree not quiet by the deadline is a fatal `DriverFailure`. |
| `mark(label, at=None, timeout_s=None, limit_s=None)` | dict | A watcher mark naming the instant `at` (D-037; default the time of the request). It never closes a batch: the watcher answers once everything written up to `at` is committed, with `accepted`, `at`, `batch` (in effect at `at`), `straddled` (or null) and `covered_utc`, plus `requested_utc` and `acked_utc`; a mark not accepted names no state and raises. `limit_s` is the watcher's own limit for confirming coverage. Every mark names an explicit instant: the caller's, else the request's own time. The driver's own marks name the instant they stand for: a turn-end mark the hand-back's (`handback_utc`), a delivery mark the delivery's. Its final marks at a reset and at export follow the stop and are strict. |
| `excluded()`, `watcher_dirs()`, `describe()` | | The clock's left-out intervals (start_utc, end_utc, reason, seconds); the watcher output directories, one per segment, live; harness name and version, T3 version, driver version (`driver_version()`: a format number, now 3, and a hash of the driver's and env's sources), model and mode. |
| `idle_gate(timeout_s=60)` | list | Problems that make this moment not a turn boundary; below. |
| `reset(text, timeout_s=900, dispatch_timeout_s=None)` | `Sent` | At a turn boundary only. `timeout_s` covers the gate and the handoff; `dispatch_timeout_s`, when given, covers the continuation's launch from when the handoff ended (the runner passes the episode time left there). Below. |
| `probe(question, scripted_reply=, timeout_s=300)` | `ProbeResult` | Below. |
| `record_user_wait(after, started_utc, ended_utc, replies_to=None)` | dict | The simulated user's return delay, against a `TurnResult` (its closing output's item) or a turn item id; a `wait` runner event, with `replies_to` in its fields when given. |
| `add_runner_event(type, utc, ref=, **fields)` | dict | Any other runner event the trajectory unit takes: `verdict`, `planted`, `reopen`, `budget_cutoff`, `episode_end`. |
| `export(dest, task=, end_reason=, end_mark=, user_kinds=, answers=, marks=, probe_targets=, end_utc=None, timeout_s=900)` | dict | Below. Raises `ExportFailed(stage, reasons)`. The result's `files` maps each SPEC section 3 "Logging" item the export holds (the runner's `episode_dir.LOGGING` keys) to its files. |
| `close()`, `left_over` | removal results | Removes every container, network, volume and snapshot image the episode made, by exact name, outside any deadline; the shared images stay. Also on leaving the `with` block, after an exception, and after SIGTERM with `install_sigterm`. A later call retries whatever `left_over` still lists. |
| `elapsed()`, `wall_elapsed()`, `usage()` | | The episode clock, the raw wall clock, and gateway token accounting with runner-canceled calls settled (below). |

Every `Sent` carries `kind`, `segment`, T3's `thread_id`, `message_id`,
`command_id` and `run_id`, the UTC time and the episode-clock time it was
sent, `status`, and `acked_utc` (when acceptance was known). If the run
lookup after acceptance is slow, `run_id` is None and `wait` resolves it by
message id, so a caller never needs to resend.

**The thread tree and `wait`** (D-029). One function over one projection
of every thread, `turns.busy_state`, is the definition of busy for every
caller: `wait`, `interrupt`, `reply`, `correct`, the reset and probe gate,
and a probe copy's wait. It lists every pending request (questions and
secret requests of runs that have not ended, in delivery order, each with
its thread), the live runs, the queued runs, the continuations T3 reserved
and has not dispatched, provider background work (whatever the status of
the run that owns it), and the threads that could not be read; the tree is
idle when the main thread's latest run is terminal and every list is empty.
A thread that cannot be read is its own state, neither idle nor busy.

T3's `delegate_task` is asynchronous by default: the parent's run can end
while a child still runs, and when the child ends T3 sends the parent a
prompt of its own that starts a new parent run with no user message. T3
shows that continuation on the parent's run before it dispatches it
(`delegatedCompletion` with disposition `open`, a delivery, and no message
with the delivery's id yet; T3's own idleness check in `Orchestrator.ts`
reads the same fields).

**Reading the tree** (`read_tree`). A sample of the tree is used only if
it is consistent. One sample is: T3's shell rows (one call that returns
every thread's summary row); the main thread; the children, level by
level, up to `TREE_READERS` (4) at once; then the shell rows again. The
sample is consistent only when both shell reads succeeded, no row of a
thread in the sample differs between them, every thread was read, and
every child any projection names was read. An inconsistent sample is
discarded whole: no output is given out or counted as given, no idle is
recorded, no quiet is counted, and the next poll starts over. Only a
consistent sample publishes outputs (in delivery order across all
threads), reports a hand-back, counts toward quiet in `interrupt`, passes
the idle gate, or lets `correct` steer (an inconsistent one defers it with a
non-fatal `DriverFailure`). The cost: a tree that is busy (a run writing
items) can take several polls to give a consistent sample, so outputs
delivered during a busy stretch may be given out a slice or two later; they
are never given out of order.

Within a sample, a child read as settled (every run terminal, nothing
pending, reserved or in the background) is cached with its shell row's
update time, status, item count, latest run, activity and pending fields,
and is not read again while that row is unchanged: nothing can restart a
settled thread without T3 updating its row, and reading every finished
subagent on every poll made the read grow with them until a short slice
could not finish it (40 finished children at 0.5 s a read; tested below). A
child that restarts during a sample changes its row between the two shell
reads, so that sample is discarded.

While a message the driver sent is accepted and has not appeared as a run
(its run lookup timed out), the tree is busy for `wait`, `interrupt` and
the idle gate, whatever the older runs say; the pending message is resolved
only by its own id, never against an older run.

`wait` hands back when a request is pending anywhere and every thread was
read, or when the tree is idle and the idle state is confirmed: the first
idle read stores the tree's signature (every run with its status, and each
thread's item and message counts) and the time, on the episode, so it
lasts across calls; a later read, in this call or a later one, hands back
once `SETTLE_S` (1 s) has passed with the signature unchanged; any change,
busy read, failed read or unreadable thread starts it again. Every read in `wait` may use the finish allowance (its deadline is
`timeout_s` + `finish_s`), so even a slice shorter than one read makes one.
A read needs more than `NODE_MARGIN_S` (1.5 s) of limit and takes its own
time, about 0.3 to 0.6 s per thread here (not measured further). What was
observed (integration test `polling_in_two_second_slices`, below): with the
runner's `wait(2.0)` a turn that ran a 3 s command was handed back in its
third slice and the next turn in its first; a `wait(0.5)` got a projection back.
Because a read may use the finish allowance, a slice can return up to
`finish_s` (15 s) after its `timeout_s` when T3 is slow; the hand-back then
needs a later successful read, in the same or a later slice, `SETTLE_S`
after the first idle one. The smallest slice that makes sense is one long
enough for one read, about 0.5 s; shorter slices still read once each.
After `UNREADABLE_LIMIT` (10) reads in a row with a thread that cannot be
read, `wait` raises a fatal `DriverFailure` instead of time limits.

The states are `finished` (with `final_text`, the closing run's final
message), `question` or `secret`, `failed`, `refused` (by the model API,
from the gateway log), `interrupted` (also when `interrupt` stopped
anything in the cycle, whatever the main run's status), or `time_limit`.
`run_id` is the run that closed the hand-back (the main thread's latest);
`runs` lists the main thread's runs since the previous hand-back, each with
`started_by` (`user`, or `t3` for a run T3's continuation started), status
and message id. `requests` lists every pending request, `actionable` marking
the one the caller must answer (also in `question` or `secret`), `new` the
ones no earlier wait returned; each new one is also an output.

`outputs` lists each user-channel output once across the episode, keyed by
T3 turn item id: completed `send_to_user` calls anywhere in the tree (by
exact tool name, with the text from the result), the final message of each
completed main run in the cycle, and each pending request, with kind
(`message`, `final`, `question`, `secret`), text, item id, run id, thread
id, delivery time and `raw`, the T3 item itself (for a request, marked
actionable or not, its `user_input_request` or `secret_request` item; for a
message, the full `send_to_user` item). Outputs come strictly in delivery
order, requests included. A `send_to_user` payload already in the
projection is used as it is; only an omitted one is read in full. One that
cannot be read is listed in `unresolved` with its item id and retried by
the next `wait`; while it is, nothing delivered at or after it is given
out or counted as given, the hand-back is not final, and after
`UNRESOLVED_LIMIT` (10) failed reads of the same item `wait` raises a fatal
`DriverFailure` naming it. `handback_utc` is the hand-back's instant: the
delivery of the output that closed it (the actionable request or the final
message), else, for a silent hand-back, the instant the agent stopped: the
latest completion among the tree's runs in the consistent sample that
showed it idle, taken from T3's own run records, so it does not depend on
polling. `ended_utc` is when the driver reported it. The turn-end mark names
`handback_utc`. Collecting
the outputs and the turn-end mark are bounded by the original deadline
plus `finish_s`; the tree observed by the last poll is reused.

**What the driver records itself.** By default the export passes the
trajectory unit these runner events and hints, from the driver's own
records: `user_kinds` (each accepted opening, reply, correction, reset and
refusal message's kind, a refusal as `reply`), `controlled_reset` (dated
when `reset` was called, recorded once the handoff completed with
`continuation` `launched` or `unsent`, so the driver's count of resets is
the number of handoffs; an unsent one is not passed to the trajectory,
whose segment has no dispatch), `correction_delivered` (T3's `completedAt` on
each correction, with the driver's delivery mark, which the bundle turns
into the mark's number and batch), `probe` (dated when `probe` was called)
and `episode_end` (unless the caller added its own: dated at the canonical
end instant, by default when `export` was called). `driver_events=`
takes the subset to keep; a caller that records one of these itself leaves
it out, and the driver then never adds it. `DRIVER_EVENTS` lists them.

**The word list.** `cohbench.env.MEASUREMENT_WORDS`, with `telling_words(text)`
and `telling_names(names)`, is the one list of words that must not reach the
agent (D-019): the project and package names, benchmark, coherency or
coherence, eval as a word, answer key, user card, scorer, probe, test
harness, simulated user. The workspace check uses it; the runner checks its
own texts with it.

**Clocks.** `elapsed()` starts when the opening prompt is dispatched and
leaves out the handoff's plumbing, every probe and the export, each recorded
in `Episode.clock.excluded` with its reason and start and end times in UTC
with microseconds; an interval of zero length is not recorded. Each excluded interval
starts when the operation is called, before any read it makes (the idle
gate, the session lookup), so a probe rejected at the idle gate is excluded
too; the driver's event for the operation carries that same start time,
which the trajectory unit accepts (an event exactly at an interval's start
is on the clock). `wall_elapsed()` is the raw time since the environment
began starting. The simulated user's delay is the runner's to charge
(`record_user_wait`).

**The idle gate** (`turns.idle_problems`), used by reset and probe, fails
closed: every thread in the tree readable; in each, no unfinished run, no
pending runtime request or waiting question or secret item of a run that
has not ended, no pending background task, no delegated completion reserved
and not yet dispatched; a shell row for every thread,
archived rows included (`archivedThreads`), with an idle or any terminal
status (`interrupted` included: an interrupted run is a stopped run) and no
activity or pending work in its other fields; the current run present and
terminal. A read that fails or runs out of time is a problem, never a pass.
It cannot see a shell process the agent detached; the probe's pause covers
that case (below), the reset stops T3 and then the container.

**Reset.** The idle gate, then `Environment.handoff()`, which stops T3 and
the container, takes the final watcher mark and then the segment's T3
record from the stopped container (below), then the next message in a new
thread, and a `controlled_reset` runner event. Everything up to that
message is excluded from the clock. A rejected idle gate raises
`EpisodeError` and changes nothing; a handoff that fails part way raises
`DriverFailure`.

**Probes.** Everything from the call on is excluded from the clock. `probe`
passes the idle gate, reads the main run's harness session, then
`Environment.probe_copy`: the main container paused while it is committed
and its volume cloned, the snapshot scanned for every key in play, and the
copy on its own site. With the scripted upstream the copy's server is seeded
from the main server's exported state, and `scripted_reply` (a text or one
handler) answers as the next script's handler 0. The runner supplies that
reply from the task; without it a scripted probe is `unavailable`. The
question goes to the same thread id in the copy.

Before anything else the question is checked for the credential. The copy
waits on its whole tree by the same definition of busy (idle, confirmed
`SETTLE_S` later), and its answer is the final message of its latest main
run. Outcomes:
`answered` (a non-empty final message, and `continuity`: the
copy's run continued the main run's harness session), `unavailable` (not at
a turn boundary, setup failed, a snapshot key scan or a check failed, the
copy did not resume the session, the copy did not answer, time limit,
driver error) or `failed` (the copy's run failed, or completed without a
final message, or the copy's cleanup found the key). Every copy-side
problem, cleanup included, is an outcome and never an exception. The copy
is removed outside the call's deadline. Cleanup that does not complete is
reported in `ProbeResult.cleanup_complete` (False), in the probe's entry in
`Episode.probes` and the export manifest, and in `detail.cleanup`
(`left_over`, `problems`), whatever the outcome, and stays registered;
`close()` retries it. A copy that asks the user is `failed` ("the copy
asked the user"). The one
exception: from the moment the main container's pause is attempted, any
path that does not end with it confirmed running and unpaused ends the
episode with `DriverFailure`.

The probe has an observable effect on the main container: its processes are
frozen during the pause, and its workspace files are read to hash them.
Every T3 read or call the driver makes, polling included, is a short-lived
`node -` process started in the agent's container by `docker exec`, visible
in its process table while it runs; it names nothing telling (it is T3's
own runtime) and reads nothing of the workspace. The
main run's conversation and scripted server are untouched.

## Limits

What the driver does not handle, by design or for now:

- A busy tree can take several polls to give a consistent sample (above);
  outputs delivered meanwhile are given out later, never out of order.
- A probe pauses the main container for the capture and reads its
  workspace files to hash them; the agent's processes are frozen meanwhile.
- Every T3 read or call is a short-lived `node -` process the host starts
  in the agent's container with `docker exec`, visible in its process
  table while it runs.
- An asynchronous subagent cannot be scripted (the scripted server
  delegates in T3's waiting mode only, D-029), so that path is unit tested
  against a fake T3 only.
- Deadlines: a call already running when a deadline passes is cut at its
  own limit capped by the deadline, but these are not cut inside: the
  export's host-only stages (normalisation, validation, publication; the
  deadline is checked between them), reading one gateway log file during the
  refusal scan (checked between records), and the scripted server's admin
  calls during a site's start (a fixed 60 s limit, capped by the deadline
  only when one is set). Removing what a call created, and a probe's
  recovery of the paused main container, run outside the deadline.
- An export after a handoff whose continuation was never sent leaves the
  dispatch-less segment out of the trajectory and the bundle (its watcher log
  stays in the raw record), and the bundle's end is the end instant, not a
  mark of that segment.
- Two writes closer together than the watcher's read interval can share a
  batch (D-040), so a boundary between them is reported as straddled.
- The consistency check assumes T3 changes a thread's shell row (its update
  time, status, item count, latest run, activity or pending fields)
  whenever it changes that thread's child links or delivers an item to it.
  Whether the pinned T3 can do either without changing the row was not
  verified, by me or by the reviewer.
- A tree with very many live children cannot be read within one finish
  allowance (168 live children at 0.5 s per read, four at a time, take
  longer than 15 s); settled children are cached, live ones are read on
  every sample.

## Harness descriptions

`harness.py` holds everything harness-specific: T3 provider instance, the
model slug per provider, options, version command, home and session
directories, T3's status cache, the project instruction file, the question
tool, the exact `send_to_user` tool names, whether it is the reference
harness, and the trajectory profile.

- **Pi 1.6.0**, the reference harness: `anthropic/<model>`, `thinking: off`,
  instruction file `AGENTS.md` (Pi loads it from the working directory
  without project trust, docs/configuration.md "Context files"; the coached
  test shows the sentence in every stored system prompt in coached mode and
  in none in default mode), no question tool. Its own default model is set
  to the model under test (`~/.pi/agent/settings.json`), because Pi has to be
  configured with a model and the driver proof showed a delegated child
  otherwise ran on Pi's own default; the setting is recorded in the export
  manifest (`harness_default_model`, `settings_written`). Exercised.
- **Claude Code 2.1.330**, a native harness: nothing is written and no
  option is sent (no `effort`); it runs at its own defaults (SPEC section
  2). A caller's `options` is an explicit override, recorded in the export. Unexercised (every turn was refused by the model API in the
  driver proof, D-002): its instruction file, question tool and steering are
  from the proof's notes and T3's adapter. T3 can compact an idle Claude
  thread on its own (`IdleCompactionService`, beta); not handled.

`diagnostic_question_tool=True` installs the driver proof's diagnostic Pi
extension (`PI_DIALOG_EXTENSION`, an `ask_user` tool with a blocking
dialog). It is not part of the reference harness; the tests use it to
exercise `answer`.

## The export

```
dest/manifest.json                   clocks, harness and version, T3 version, model, provider, mode, settings
                                     written, image ids, docker names, every Sent, turns, corrections, probes,
                                     handoffs, marks, the end mark and the shutdown mark, runner events, usage
dest/segment-<n>/t3-state.sqlite     T3's database, copied from the stopped container, its WAL folded in
dest/segment-<n>/threads.json        every thread in that database (parent and subagents), as JSON
dest/segment-<n>/sessions/           the harness's session files
dest/segment-<n>/server-log.txt      the T3 server's output
dest/gateway/, dest/scripted/        the gateway's and the scripted server's logs
dest/watcher/segment-<n>/            the watcher's output directory
dest/environment.json                names, checks, timings; rewritten at close with the removal results
dest/trajectory/                     the full trajectory and its run manifest (audit record)
dest/bundle/                         the scoring bundle (D-018)
```

The gateway's and the scripted server's logs are written by their own
users (uid 10001 and 10002) into host bind mounts, which an ordinary host
user on native Linux may not read. The driver never reads them there: the
live `usage()` runs `python -m gateway summary --episode` inside the
gateway's container, refusal evidence is a copy of the gateway's log
directory taken through Docker, and the export copies both logs out of the
stopped containers through Docker (`Site.copy_logs`), scans them in memory,
unpacks them in a private directory and only then stages them; the export's
usage summary is computed from that copy.

After a confirmed driver interrupt, `usage()` also reads sanitized per-call
accounting inside the gateway. An unknown-outcome call whose recorded span
overlaps a successful stop is settled with the tokens recorded for it, or
zero if none were recorded. The stop interval runs from the stop dispatch to
the run's recorded completion, or the stop receipt when there is no run
completion. It does not end at a later quiet-tree poll. Completed calls with
missing usage, and incomplete calls outside those intervals, remain
incomplete. Token totals retain all recorded tokens.

The effective summary includes `recorded` (the unmodified gateway summary),
`runner_cancelled_calls` (ids, start times, interrupt intervals and token
fields), and `interrupts`. `calls_not_complete` and `usage_status` describe
only calls still needing usage. The runner's usage grace therefore does not
wait for a response it canceled. With no driver interrupt the summary is
unchanged. This adjustment is accounting only; raw export logs retain the
canceled response's original status.

`export` quiesces first: it stops T3 and the harness container, takes the
watcher's shutdown mark (`export`), stops the watcher (a clean shutdown is
required), and stops the gateway and the scripted server. Only then does it
copy anything, from the stopped containers (`docker cp` of T3's database
and its `-wal` file, folded into one file with SQLite's backup on the host;
the harness's session directory; `docker logs`) and the finished host logs.
A segment that ended in a reset was captured the same way from its stopped
container during the handoff. So every artifact is from one moment. A
source that cannot be read fails the export with its stage (`quiesce`,
`capture`); nothing is published from different moments.

Every failure, in any stage, is `ExportFailed` with that stage; its
reasons, quiesce and capture diagnostics included, are kept whole (item ids
are never cut; `docker cp` errors are passed on in full).

`end_utc` is the scored end when the caller has one (D-035: an elapsed
cutoff ends at its deadline instant), earlier than the call and than the
end mark. The driver's own `episode_end` is dated there, the manifest's
`episode_end_utc` and clock give that instant and the episode clock up to
it, and the episode clock is frozen there: `elapsed()` returns that value
from then on, through `close()`, whose cleanup is on the wall clock only.
The export's own excluded interval starts when `export` is called and gets
its end at close. Without `end_utc` the end is the
start of the export's interval, which opens when `export` is called. The
export resolves one canonical end instant: `end_utc`, else the caller's own
`episode_end` event, else the end mark's instant, else the call; any two
given must be the same instant (compared parsed, exactly), else
`ExportFailed`, and the caller's `episode_end` event is passed on with the
canonical instant. The shutdown mark names the end instant and is strict:
straddled by a batch, the export fails at `quiesce`, except at an elapsed
cutoff (the caller gave the end instant and its end reason is
`budget_cutoff`), where the batch that straddles the deadline is outside the
episode by decision (D-035) and the trajectory unit cuts by the end
instant. A caller with a scored end passes the same instant to all
three: `mark("end", at=end)`, `add_runner_event("episode_end", end, ...)`
and `export(end_mark="end", end_utc=end)`. Nothing else in the driver takes
the end to be the time of the call. An excluded interval (a probe, a
handoff) that spans the scored end is the caller's to avoid: the trajectory
unit refuses an event strictly inside one. A container
counts as stopped only when an inspect succeeded and said so.

`end_mark` is the runner's end mark: a label it requested with `mark` in the
current segment, after the scored work stopped. It is forwarded to the
bundle maker as the end, so the bundle's final filesystem state is the
state at that mark, and files changed between it and the shutdown are in
the raw watcher log only. The trajectory unit requires the end mark and the
episode end to agree (no watcher batch may close between them), so a runner
that passes `end_mark` takes it at the episode's end instant and passes the
same instant to its `episode_end` event and to `export(end_utc=)`. Without
any end instant from the caller, the end is when `export` is called; the
shutdown mark (taken after the stop) names that same instant, and the
export's excluded interval starts there. Without a watcher there is no end
mark, and passing `end_mark` is refused. The shutdown mark is recorded in
the raw manifest (`shutdown_mark`) either way, and the manifest's clock
lists the export's own interval, its end filled in at `close`. An export
that fails on T3 items the trajectory unit does not map names their count
and types.

Every file is scanned in memory for every key in play before it is written
(the generated trajectory, bundle, manifest and `environment.json`
included; the bundle's blobs come from the staged, already scanned watcher
stores and are scanned again), into a staging directory beside `dest`. The
driver passes the trajectory unit what it knows (above, "What the driver
records itself"), the answers, the D-026 lists (every harness's
instruction file and `refs/t3/`, the same lists the watcher was given,
also used for the bundle's validation), and the task's identifier kinds,
observation patterns and the runner's episode record from `TaskInfo`
(with `Hints.episode_start_utc`, and `Hints.handbacks_recorded` True when
the caller is the current runner, which passes an episode record or
hand-back events: the record is then authoritative about hand-backs), and
`probe_targets` (`{probe: {about, ref}}`) as the bundle takes them. The full
trajectory must validate and pass the blindness check, the bundle must
validate and pass its blindness gate, and a T3 `compaction` item the
trajectory does not map fails the export by name. Only then does the staging
directory become `dest`; otherwise it is removed and `ExportFailed.stage`
says which part failed (quiesce, capture, artifacts, trajectory, bundle,
blindness). A rejection at the trajectory, bundle or blindness stage keeps
the raw record for examination and replay at `<dest>.rejected`: everything
a published export holds except the bundle (T3's databases, threads,
sessions, server logs, service and watcher logs, the trajectory written so
far, the manifest), each file scanned for every key when staged, and
`rejected.json` with the stage, reasons and report. `ExportFailed.raw_record`
gives its path.

## Tests

```sh
uv run pytest tests/driver                       # unit tests; integration tests are skipped, Docker is not asked
uv run pytest -m integration tests/driver -v     # needs Docker and provisioned images
uv run --isolated --python 3.12 --with pytest pytest tests/driver   # the unit tests on Python 3.12
```

Unit tests (`test_units.py`, 76, no Docker; pass on Python 3.15 and 3.12).
The fake T3 they run against refuses a read with too little time left, as
the real transport does (`NODE_MARGIN_S`), so a caller that leaves its reads
no time fails there too. Covered:
turn states, pending requests in any thread, questions with one and several
prompts, the final message rule, `send_to_user` by exact name, the steering
choice, the idle gate (strict; any terminal shell status; archived rows; a
question left by an ended run), refusal binding, the harness descriptions,
the workspace manifest (D-027's one `mtime`), symlink targets, the shared
word list, the coached file's time, the episode clock (microsecond
intervals, none of zero length), the driver version, opaque names and
placeholders, the registry, the key scanner, decoded key hits, a tar member
whose name holds the key named by position, `write_scanned`, the Docker
deadline and the `-m` parser. Against a fake T3: `wait` reporting a child's
question after the parent finished, each output once, and its time bound;
`wait` not handing back while a child runs or while T3's continuation is
reserved, then handing back on the continuation run with `runs` marked
user and t3; `interrupt` stopping a child's run and its request, and a
second `interrupt` sending nothing; a lost send reconciled by its ids and an
unknown one raising a fatal `DriverFailure`; a read failure in `correct`
raising a non-fatal `DriverFailure`, not `TurnNotRunning`; a steer rejected
after the run ended raising `TurnNotRunning`; an answer counted only when
resolved with the values sent (cancelled and other values rejected); the
credential guard on replies, corrections and probe questions and in
`answer_secret`; a correction's `Delivery` saying no mark was taken. Added
this round: the runner's 2 s slices and 0.75 s slices reaching the
hand-back within three calls (the idle confirmation kept across calls); a
change resetting the confirmation; every pending request returned, one
actionable, each with its T3 item; the steer gate (a child's question with
the main run live is `TurnNotRunning`, a main run ended with a child
working is `MainRunEnded`, nothing sent); background work and a queued run
counted busy and stopped (`thread.stop`, `queued-run.cancel`), and the
interrupted cycle reported as `interrupted`; a failed read after the
decline queuing the refusal's words; an unread send_to_user payload holding
the hand-back; a persistently unreadable child ending in a fatal
`DriverFailure`; `Sent.submitted`; reserved names at any depth; packing
modes from the manifest and refusing an unlisted directory or a missing
mode (D-031).

Integration tests (`test_integration.py`): the scripted server answers every
model call; real Pi 1.6.0 under T3 Code `0.1.12-nightly.20261121.5051`, the
real gateway and watcher (its second fix round's launch contract), the
trajectory unit's bundle. On 2026-11-22 between 07:14 and 07:26 UTC, on
Docker Desktop 27.4.0, with no other container of this project running, 25
of 26 passed in 688 s and one was skipped (below). After the last pass the
export and reset tests were run again between 08:13 and 08:18 UTC with the
new elapsed-cutoff test: all 9 passed.

| Test | What it showed |
|---|---|
| `start_wait_two_turns_and_export` | T3 ids and times on each `Sent`; `finished` with `outputs` naming the final's item; `describe()`, `watcher_dirs()` and `mark()`; the export's files, versions, image ids and names, captured after quiescing; a mark at each turn end; trajectory, bundle and blindness passing; nothing left after close. |
| `question_in_plain_text` | A scripted question ends the turn as text. |
| `correction_abandons_remaining_steps` | `native_steer` requested and observed; steps 3 and 4 never ran; the delivery observed inside `wait` got a mark at once (`completedAt` 03:00:59.791, mark requested 03:00:59.968, acknowledged 03:01:00.083); `Delivery` on the `Sent`; `TurnNotRunning` after the turn ended; the export passes and its scorer view has one `correction_delivered` with the mark's number and batch. |
| `watcher_records_every_write` | The watcher's last state equals the volume's files, each write has its record. |
| `reset_keeps_the_workspace_and_drops_tmp` | The handoff (final mark accepted, not straddled), T3 refs removed and none back, dangling objects kept, the `controlled_reset` event at the exclusion's start, and now the two-segment export passes (the trajectory unit's earlier join fault is fixed). |
| `probe_leaves_the_main_run_untouched` | `answered`; the same session; the unpause confirmed; the main run unchanged; every copy resource and the snapshot image gone. |
| `coached_sentence_reaches_the_model_request` | The sentence in every stored system prompt in coached mode only. |
| `secret_and_question_answers` | `answer_secret` accepted on `saved`; `answer` accepted through T3's replayed receipt of its command id (T3 recorded no answers for the dialog's request); the export passes. |
| `send_to_user_and_subagent_export` | The `send_to_user` text; a child as a second thread; the export passes. |
| `close_after_a_forced_exception`, `close_after_sigterm` | Nothing left after an exception, and after SIGTERM mid-turn (exit 143). |
| `close_after_sigterm_during_a_probe_capture` | SIGTERM while the probe captures: exit 143, every name and the snapshot image gone. |
| `key_scan_of_the_harness_container` | With a stand-in secret: export, volume and inspect clean. |
| `workspace_admission` | The undeclared file, the key file, the key as bytes, in gzip, and in git history loose and packed, each refused before population. |
| `secret_requested_by_a_subagent` | `wait` returned the child's secret; the answer reached it. |
| `compaction_surfaces_in_the_export` | A failed compaction mapped; the export passes. |
| `refuse_secret` | Declined; the words reached the agent by steering; the export passes, and its scorer view holds exactly two user messages, the opening and the refusal's words as `reply`, with no "declined" message. |
| `interrupt_then_reset` | An interrupted turn passes the idle gate and resets. |
| `a_finished_parent_with_a_childs_pending_question` | With T3's waiting delegation, the parent's run ended while the child asked the user (what this T3 build did); `wait` returned the child's question, a second `wait` none of the outputs again; the answer was accepted; the next `wait` returned only after the child ended and T3's own continuation run finished, `runs` marked `user` then `t3`, final "The helper is done."; the tree then idle. |
| `reset_against_a_childs_pending_question` | `interrupt` stopped the child's run and its request (no longer pending), the idle gate passed, the reset's new segment finished. |
| `cutoff_against_a_childs_pending_question` | `interrupt` stopped the tree and waited out T3's continuation run; the next `wait` reported `interrupted`; a second `interrupt` sent nothing; the probe answered with its cleanup complete; the export passes (the trajectory unit now maps T3's `run_interrupt_request` item). |
| `polling_in_two_second_slices` | `wait(2.0)` in a loop, as the runner's adapter calls it: a turn with a 3 s command handed back in the third slice, the next turn in the first; a `wait(0.5)` got a projection back from the transport. |
| `export_takes_the_runners_end_mark` | A file written after the end mark is in the raw log and not in the bundle; the export carries a runner episode record (as `cohbench/runner/record.py` writes it, with the final output's verdict), so a hint such a record needs, `Hints.episode_start_utc` among them, is required by this suite. |
| `export_at_an_elapsed_cutoff_straddled_by_a_batch` | The agent writes every 50 ms around the deadline; the end mark at the deadline came back straddled (batch 1 over the instant), the strict shutdown mark accepted the straddle at this elapsed cutoff, and the export passes. |
| `export_with_an_earlier_scored_end` | D-035: an end instant 1.5 s before the end mark and the export: the manifest's end and clock give that instant, the export's interval starts after it, the episode clock stays frozen there through close. |
| `the_metric_runs_isolated` | Every metric reading `ok` with isolation `users`, as uid 1500, unable to list `/srv/w`, `/srv/w/live` or `/srv/w/out`, at `/work/project` with the workspace's README there. |
| `wait_waits_for_a_running_child_and_the_continuation` | Skipped: it needs an asynchronous child, which the scripted server refuses (D-029). The same logic is unit tested against a fake T3, and the child-question test above exercises T3's continuation run for real. |

Not exercised: an asynchronous child against T3 (above); Claude Code;
`correct` by interrupt and restart or interrupt then queue; a refusal's
words queued after a lost steer race, a send reconciled after a real lost
answer, and an answer whose request T3 cancelled (unit tested only);
`MainNotResumed` and a probe cleanup retried at close; the snapshot key
scans and the gateway's credential copy with a real key (they run only with
a key, and no paid call was made); native Linux (the credential copy's
reason); a turn refused by the model API (unit tested only); a successful
compaction; another provider; probe targets and task declarations in the
bundle (a runner episode record is exercised, above); Docker 28 or later.
