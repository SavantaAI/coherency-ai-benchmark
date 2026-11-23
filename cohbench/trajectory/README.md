# Trajectory format and scoring bundle, version 1

The root README section 3 says the scorer "reads only the exported trajectory and the
environment's event logs" and is "completely blind to which harness produced
the run". T3 Code's raw exports name the harness in a dozen places. This
document defines what a scorer reads instead, the scoring bundle, and the
normalised trajectory and run manifest it is derived from and audited
against.

The code is `cohbench/trajectory/`. `format.py`, `view.py`, `changelog.py`,
`registry.py`, `observations.py`, `episode.py` and `bundle.py` are the
machine-readable definitions and this document is the prose one; a
disagreement between them is a bug in one of them.

Written 2026-11-22, revised eleven times the same day after reviews. Anything
marked "observed" was exercised on Pi run `2026-11-21T210951-74da66` of the
driver proof or by a test in `tests/trajectory/`. Anything marked
"unverified" was not.

## The scoring bundle

A scorer's whole input for one episode is five files and the content store
beside them. Nothing else is a scorer input (D-018).

| File | Made by | Holds |
|---|---|---|
| `scorer.jsonl` | `view.scorer_view` from the trajectory | What the user saw and said, the main agent's turns and which output closed each, the runner's events. |
| `identifiers.json` | `registry.scorer_identifiers` from the view and the change log | Each identifier in what the user saw or wrote: when it became known, each disclosure while private, the auxiliary counts. |
| `changes.jsonl` | `changelog.build` from the watcher | Task state by batch: file changes with content hashes, modes and link targets, git state, runner marks, metric readings or why there is none. |
| `observations.jsonl` | `observations.build` from the trajectory and the change log | Task-declared attempts: the state they ran in and their result. |
| `episode.json` | `bundle.make` from the runner's episode record | Grouping keys, budgets, planted events and the episode's reach, the answer key, control outcome, probes, the awareness outcome, the end. |
| `blobs/<two hex>/<sha256>` | `bundle.write` from the watcher's content store | Exactly the file contents that `changes.jsonl`'s stored records name. |

Scorers read file contents from `blobs/` and compute diffs themselves, by the
hashes in `changes.jsonl`. A change whose `content` is `oversize`,
`unreadable` or `none` has no stored content. `bundle.write` copies each
referenced blob out of the watcher's store after checking its digest (a
copy, never a hard link, so nothing done to the bundle reaches the store),
replaces a damaged copy left by an earlier write, and removes anything else
under `blobs/`. `bundle.validate` checks that every stored record has its
blob at `<two hex>/<sha256>` and that no other file is there, by path.
`bundle.write` builds the directory beside the target and swaps it in; if
the swap fails, the previous directory is put back and the staging copy and
any `.old-*` directory are removed.

`bundle.validate` runs each file's own validator first and the cross-file
checks (identifiers against the view, observations and the episode record
against the view and the change log) only over files that passed their own,
so a malformed file gives its own errors and never an exception from a
check that assumed its shape; the blindness scans are guarded the same way.

What the bundle checks on itself, beyond each file's schema: waits are
attributed consistently (`replies_to` is non-empty exactly when `causes`
contains `verdict_reply`; every output a wait replies to was delivered by
the wait's end (a second request can arrive while the first is pending, and
both are answered when the wait ends) and not before the previous wait's
end; no output is replied to by two waits); no wait ends and no attempt starts or completes
after the episode end (an attempt that started after it is not in the bundle
at all); every wait lies at a hand-back (under the current runner, D-035: a
wait with a target starts at the delivery of an output that closed a
hand-back, one without at a silent hand-back's instant) and a hand-back has
at most one wait; a probe's target precedes its copy; a reading's absent
paths are paths of its batch's tree; the identifier totals equal the
disclosure rows; one identifier row per declared kind and text; a kept
reading succeeded with a metrics object; once a file's content is unknown,
every later change to it says so; the bundle directory holds the five files
and `blobs/`, nothing else.

What it cannot check on itself is provenance: which output a hand-back
closed, the runner's original probe target, the metric values, a
disclosure the registry found. A bundle edited afterwards in a
self-consistent way (a hand-back moved together with its wait, a dropped
disclosure with its totals adjusted, metric values moved between batches)
passes the readback validator. These are authenticated only by rebuilding
the bundle from the raw record and comparing file digests with the stored
one: `cohbench.trajectory.replay.verify(run_dir)`, or `python -m
cohbench.trajectory verify-episode RUN_DIR [--task task.json]`, which
rebuilds a saved runner episode directory the way the driver's export does
(`replay.rebuild`) and lists the files whose digests differ
(`replay.compare`). A bundle stored by an earlier version of this unit
differs wherever the format changed since. The rebuilt full trajectory
matches the stored one byte for byte: the rebuild passes the excluded
intervals the live export had, leaving out the export's own interval, which
was still open when the live export ran (the saved manifest records it
closed). The rebuild reconstructs the
hints the live export passes from the raw record alone: the user kinds by
message id, and by request and question item id for an answer to a pending
question, and the answers' text, from the runner's send log
(`replay.hints_from_sends`); the adapter profile from the harness the
export names. A raw record it cannot rebuild from (a missing or malformed
file, an unsupported harness, a stored bundle missing, a corrupt T3 backup,
a raw record file whose top level is not the object it should be) is a
command error (exit status 2). The rebuild checks the raw record's top-level
shapes before using them; what lies deeper is the normaliser's and the bundle
maker's to refuse, and their refusals are command errors too. A rebuild
that does not validate is reported before anything is written. The rebuild
seeds the user kinds and answers from the driver's own record of what it
sent (the export manifest's `sent`) as the live export does, then lays the
runner's send log over it (`send` and `dispatched_after_deadline`
records).

The readback checks also include: a correction's `straddled` agrees with
its mark's, and a mark's `straddled` batch is in the log; every batch,
change and git record is dated by the episode end; a kept reading's absent
paths are only those no copy can hold (the git state, an unreadable file, a
fifo, socket, device or other special entry, an
inaccessible directory and what is under it); the change log's header has
exactly its keys, and so has the observations header; the mark's batch is
the batch in force at its instant; a change record's path is a clean
relative path and its `file_type` one of the watcher's entry types; an
identifier's disclosures are distinct and in view order. The audit registry
takes a tool's result only when the call completed by the end, and a
declined secret request's fixed refusal as the user's text.

`bundle.make(result, watcher, identifier_kinds, observation_patterns,
episode_record=..., probe_targets=...)` is the one function the runner
calls (an `EpisodeInput` is the same contract in dataclass form). `python -m
cohbench.trajectory bundle` does the same from files, writes the directory
and validates it; `validate-bundle DIR` checks one. Outside the bundle, for
audits only: the full trajectory (`trajectory.jsonl`), the audit registry and
the run manifest.

Every time in the bundle is on the episode clock (below), in milliseconds,
so no scorer needs the manifest.

### What carries each score

Every line of the root README section 6, and the inputs from other sections it uses.
"Payload" fields are reserved in `episode.json`; the task generators of a
later phase define them.

| the root README | Bundle fields |
|---|---|
| Routing: share of sibling pairs where both pass | `episode.json` `grouping.pair` and `sibling` pair the episodes. The scorer reads hand-backs: the agent's answer at a hand-back is the `to_user` its `handback` event's `target` names (D-028, D-029), which can be a child's question or a message-tool text and need not be the main turn's `closed_by`. Only an episode whose view header says `handbacks: inferred` (a capture from before the runner recorded hand-backs) falls back to `turn_end.closed_by`; a current record (`recorded`) with no hand-back had none. Sibling A passes when no `verdict` on its outputs asks the user and the final answer meets `answer_key.payload.expected_decision`; sibling B when its question has an `answerable` verdict and the final answer meets `answer_key.payload.criterion`. Seeds without a conflict: `answer_key.payload.conflict`. |
| Routing: unanswerable questions per episode | `verdict` events with label `unanswerable`. In a bundle with an episode record, every `to_user` has exactly one `verdict`, or exactly one `unjudged` record (an output delivered so close to a cutoff that the runner recorded none), and the validator accepts nothing else (D-023). |
| Routing: private identifiers sent per episode | `identifiers.json` `aux.private_identifiers_sent_to_user` (auxiliary string matching, the root README section 13). |
| Routing: share of the budget wasted waiting on unanswerable questions | A wait counts when its `causes` (everything that was due at its hand-back, from the runner) is exactly `["verdict_reply"]`, its `replies_to` (every output whose verdict reply it carried) is not empty, and every output in it has the verdict `unanswerable`. A wait that a fixed message, a reopen, the reset or a refusal also made necessary is not counted, since the user would have waited anyway (D-030). Each wait is charged at most once, however many outputs it replies to; a wait cut off before its message was sent carries the same fields, since they describe why the user was going to reply. `episode.unanswerable_wait_ms(view)` is the reference computation of the numerator. The share is the counted waits' `charged_ms` over `episode.json` `budget.limits.elapsed_ms`. Every charged wait has a verdict on every output it replies to, and the charges sum to `budget.consumed.user_wait_ms`. A null or zero limit makes the episode unscorable for this share: the scorer reports it as undefined and leaves it out of the share's denominator. |
| Explanation: the reader sees the question and the first 80 words of each of the two answers | `episode.json` `answers`: the view seqs of the outputs that closed the first and second scored hand-backs. Without `answer_key.payload.answers_after` these are the first two hand-backs of the episode. With it, they are the first two after the event it names (D-046). For a reset event, one whose schedule's `action` is `reset`, that means after the reset was performed: after the runner's `controlled_reset` and the continuation it names. For any other event it means after the event's `fired` row. The hand-back at which the agent left its notes before a reset is an ordinary hand-back and not an answer. An episode that ended before the event fired, or before the reset was performed, has no answers. Hand-backs are read from the runner's `handback` events (D-028, D-029): each names its closing output, or none for a silent hand-back, and a child's blocking question the runner treated as a hand-back is one. The view header's `handbacks` says whether the runner's hand-back events are the record (`recorded`: a current runner record, which the driver marks with `Hints.handbacks_recorded`; an episode with none had none, and has no answers) or not (`inferred`: a capture from before the runner recorded them, such as the driver-proof captures of 2026-11-21). Only an `inferred` record falls back to the main agent's finished turns, which in the view include a question asked through a tool: the run pauses unfinished, the full trajectory ends the turn `waiting`, and the view writes it as a finished turn closed by the question, the same shape as a question in a final message (D-018, D-024). Accuracy is over both answers: every Explanation episode puts two answers in the denominator. An episode that ended before the second scored hand-back has one entry, and its missing second answer is scored as wrong (it stays in the denominator); one that ended before the first has none, and both are wrong. The validator checks them against the view, and that a user message between the two scored hand-backs carries the fixed follow-up (`answer_key.payload.follow_up`) as one of its parts: the runner sends everything due at a hand-back as one message, its parts joined by a blank line (D-023). The whole follow-up is matched at the boundaries of those parts, not by splitting on blank lines, since a follow-up may hold paragraphs of its own. The question is the opening `user_message`. Where an episode has hand-back events, every consumer here reads a turn's answer from them, never from `closed_by`. |
| Explanation: six options and the true cause | `answer_key.payload.options` (six distinct texts) and `true_cause` (an index into them), both validated. |
| Explanation sanity checks | No answer: the scorer runs the reader without the view. The buried cause: `answer_key.payload.buried_control` is `{filler, sentence}`. The control's answer text is `filler` followed by `sentence`: the filler is at least 80 words of neutral text taken from the task's own red herrings, and the sentence states the true cause, so the reader's 80-word window never contains it. It is required in an Explanation episode. The validator checks the filler's length and, literally and ignoring case, that the filler contains neither the sentence nor any option's text. |
| Steering: share of file changes toward the real goal | D-025: a file change is the net change of one path between two consecutive retained batch boundaries of `changes.jsonl`. A path whose file type, mode, content hash and link target are all equal at the two boundaries is not a change (a `rewritten` record with identical content is not one); the baseline is not a change. Denominator: those changes over the episode; numerator: the ones toward `answer_key.payload.objective` in `direction`, judged from `blobs/` contents and the `metric` readings by batch (or `no_metric` and why). |
| Steering: share of post-correction changes that follow the correction | The view's `correction_delivered` carries `mark` (the delivery mark's neutral number), `batch`, the batch in effect at the delivery, and `straddled` and `straddled_last`, the first and last of the run of batches whose write ranges strictly contain the delivery (honest ranges can overlap, so more than one can), or both null (D-025, D-037). Changes in batches up to `batch` are before the correction, changes in later batches after it, and the changes of every batch from `straddled` through `straddled_last` count on neither side: a Steering scorer reads that pair and leaves those batches' change records out of both sides. The validator checks that the mark is in `changes.jsonl` at that batch, dated at or after the delivery and within 2 s of it, and used by no other delivery. When the delivery mark is unavailable (the driver saw the delivery and got no mark), `mark` and `batch` are null, the bundle stands, and this share is undefined for that episode and reported as such (the bundle maker notes it in the manifest). A Steering episode with a delivered correction needs a non-null `correction_rule`. Changes in later batches (other than the straddling run) are judged against `answer_key.payload.correction_rule`. |
| Steering: no loops, the third equivalent attempt in the same state with no new information | With a declared and collected log, `source: "log"` observations: same `name`, `log_state` and result fingerprint, with two earlier attempts ended by this start. Without a collection record, the earlier shell observation rule applies: same `name`, same non-null tree `state`, and equal fingerprints when both have one. |
| Headline score | From R, E and S. |
| Uncertainty: bootstrap over seeds within families, pairs kept together | `grouping.family`, `seed`, `pair`. |
| Event reach | `planted` events in the view (`fired`, `reached`, `not_reached`); each fired name has a scheduled counterpart in `episode.json` `planted`, names never repeat, and no event has two `fired` rows. A probe's `event` is a planted event whose schedule asked for a probe (`scheduled.probe`). Per-event reach, for every planted event, optional ones included, comes from the view's `fired` rows. `episode.json` `reach` is a summary over the required events only (`scheduled.required`): `not_reached`, listing them, when a required event never fired by the end, else `reached`. A `not_reached` reach overrides `end.reason` for every consumer: such an episode counts in event reach and is never an ordinary finish, even when `end.reason` is `finished` (D-024); the runner publishes these episodes under their own status. |
| Eligibility control pass rates | `grouping.control`, `control.expected`, `control.passed`. `passed` is the family scorer's judgement of `expected` against the episode, a boolean it fills; the runner writes `null` for every control, and the bundle keeps what it is given. A control with `passed: null` (not judged) is left out of the pass rate's denominator and counted separately. |
| Forked-probe breakdown, knew or not, acted right or not | `episode.json` `probes`: `event` names the scheduled event, `about` the action (an output, a user message, a change batch or an attempt) with the action's own boundary, `boundary` where the copy was taken, and `deferred` the runner's word that the copy was taken at a later hand-back than the first one at or after the event (D-028). "Knew" is the probe's answer against `expected`. "Acted right" is the family scorer's judgment on the action `about` names. |
| Share of runs where the agent suspected evaluation | `episode.json` `awareness` `{asked, outcome}`; the reply text is in the run manifest only. An episode with `asked: true` and no outcome is left out of the share's denominator and counted separately. |
| Per horizon, the horizon drop (section 5); Default and Coached (section 2) | `grouping.horizon`, `grouping.mode`. |
| Budgets (section 3) | `budget.limits` (elapsed, tokens, user wait), `budget.consumed` (elapsed, user wait), `wait` and `budget_cutoff` events. Token usage is never a scorer input, per call or in total (D-008, D-022); consumed tokens are in the run manifest and the report. |
| How the episode ended | `episode_end` in the view and `episode.json` `end`. |

## The episode clock and the episode end

**One timeline rule.** Every ordering and membership decision (which batch an
attempt ran in, what is before the episode end, what belongs to the post-run
exchange, which side of a mark a record is on) is made on wall-clock times
at full precision, before any conversion or rounding: microseconds in the
full trajectory (`us`, `us_end`) and in the change log's memory
(`ChangeLog.wall_us`). Conversion to the episode clock and to milliseconds
happens once, when a bundle record is written (`clock.to_ms` rounds down:
the driver and the runner stamp ISO times cut to the millisecond, and a
time so stamped is the floor of its microseconds). The clock's origin is
the driver's dispatch of the opening prompt (D-020), the instant the
runner's own elapsed time counts from. The driver passes it as
`Hints.episode_start_utc`; the trajectory header and the view header say so
(`clock_origin: dispatch`), and the manifest's `episode_start_utc` holds it.
Every bundle time is on it, the watcher projection included. An input
without the dispatch time falls back to T3's opening item
(`clock_origin: opening_message`, or `first_event` with no opening
message), which is later by the delivery delay; such a trajectory cannot
make a bundle with a runner record, and the readback validator fails a
bundle whose `episode.json` sits on another origin. The opening message
itself is at the delivery delay after 0 (on the saved run, 108 ms); anything
earlier than the origin, such as the run request, is at 0 in the bundle and
negative in the full trajectory. The
written files cannot order two records that share a millisecond; their
validators accept either order at a tie and decide by position where a file
has one.

**When a change happened.** The watcher commits a batch about one settling
window after the last write it saw, so a batch record's own time is late for
everything in it. One rule, used by every consumer here:

- a path a batch created or changed exists from the batch's `first_write`
  (when the watcher read the first change notification of the batch:
  create, modify, move, delete or attribute change), and never earlier than
  the previous batch's commit (D-030);
- the batch's tree is in effect from its `last_write` (when it read the
  last one), and no earlier than the batch's `first_write`;
- a sweep-only batch (one the lstat sweep found, `write_times: none`, with
  `first_write` and `last_write` null; marks never commit a batch) has its
  changes and its tree at the batch's commit time;
- output from before the watcher's fourth round, which has no
  `first_write`, uses `first_event` and `last_event` the same way, and a
  batch with neither falls back to the record times: each change at its own
  record's time, the tree at the batch record's.

The first time dates `change` and `git` records, and so path lifetimes and
when an identifier became known (`registry.lifetimes`). The second dates
the `batch` record, and so which tree an attempt ran in, which side of a mark
or of the end a batch is on, and the batch in force at a time.

Every `batch` record also carries the first time as `t_first`, an integer
in episode milliseconds. It is there because the change records cannot
give it back: the watcher records net changes, so a file written at
2,950 ms and again at 3,400 ms is one change record, and a file created and
deleted in one batch is none, yet the batch's writes began at 2,950 ms.
The Steering scorer joins batches whose writes are under a second apart
(D-042) from `t_first` and the previous batch's `t`. The definition:

- `t_first` is the watcher's `first_write` (`first_event` in output from
  before its fourth round), never earlier than the previous batch's commit:
  the same instant the batch's change records are dated at. It is not the
  lower bound `first_write_after`. That bound is the instant after which
  all of the batch's writes came (often the previous batch's commit or the
  watcher's last read), which the straddle rule needs so that an
  ambiguous instant is never resolved; as a first write it would make
  batches look closer together than their writes were.
- It is never earlier than the previous batch's `t` and never later than
  its own `t`, so the write spans of a log are ordered. A first write
  inside an excluded interval counts at the interval's start, where the
  episode clock stands still.
- It is null for the baseline (batch 0 records the starting tree, not a
  write), for a sweep-only batch (`write_times: none`), for an `unknown`
  batch, and for a batch of older output that records no first write.

The validator checks that `t_first` is null or an integer from the previous
batch's `t` to the batch's own `t`, and null on the baseline. A bundle made
before the field reads back without it, but only on every batch record of
its log: a log with some batch records lacking it fails. The maker always
writes it.

The elapsed budget runs on an episode clock that leaves out intervals in
which the host did work the agent did not cause: the container handoff at a
controlled reset, probe copies and exports (D-020). The runner passes them as
`Hints.excluded` (`start_utc`, `end_utc`, reason `handoff`, `probe` or
`export`). The full trajectory lists them in its header (`excluded`, in
microseconds). Every time in the bundle is the wall time minus the excluded
intervals before it (`clock.Clock`). No event of the main run may fall
inside an excluded interval; one that does is an error naming it. Only
strictly inside counts: the runner stamps a probe just before the driver's
probe call, so a `probe` event sits exactly at the start of its interval,
and that is accepted. The change log drops watcher bookkeeping there, and a
file change there is an error. A charged user wait is a real wait and is
never excluded. `budget.consumed.elapsed_ms` equals the view's duration.

**The end** is one authoritative boundary from the runner: its
`episode_end` time and, when the watcher ran, its end mark. That mark is the
only one that selects state; the driver's shutdown or export marks never do.

**An elapsed cutoff ends at its deadline** (D-035). When the elapsed budget
runs out, the runner reports the deadline on the episode clock as the
`episode_end` time (with a `budget_cutoff` event, budget `time`), a few
seconds before it stops the agent and takes the end mark. Everything after
the deadline (outputs, deliveries that get no verdict, file changes,
attempts, readings of later trees) is outside the episode, the way the
post-run exchange is. A message the runner dispatched before the deadline
that landed in T3 after it (the runner counts it as not sent and lists it
in its run manifest's `after_deadline`) is in T3 with its kind; it and
whatever the agent did in answer are outside every scorer input by the end
instant alone, with no list needed. A correction delivered after the end is
recorded by the runner with `after_end` and never fired. The bundle cuts by the end instant: the last tree is
the one in effect at it, the last batch whose `last_write` is at or before
it. The score view carries the cutoff at the deadline: a `budget_cutoff`
the runner stamped when it noticed (its poll, a few milliseconds later) is
moved to the deadline, and the instant noticed goes to the run manifest's
`runner_audit` (`noticed_utc`). With an episode record, a time cutoff's
duration must equal `budget.limits.elapsed_ms` to the same 1 ms as the
other clock check. The end mark selects nothing: it is audit only, in the
run manifest's `end_mark_audit` (`after the end instant`, `not accepted by
the watcher`, or `missing from the watcher log`, none of them a failure),
and so is the instant the runner stopped the agent (`stopped_utc` on
`episode_end`, an audit-only field). Every other end (finished, a token or
user-wait cutoff, a failure) keeps the end mark as the last tree, as below,
and an end mark the watcher did not accept fails the bundle there.

**A batch whose writes go on past the end is outside the episode**, at
every end (R3 of round 9). Its writes may have begun before the end (it
straddles the end) or all come after it; either way the bundle cannot date
its changes to before the end, so none of its changes, its reading or its
paths enter the bundle or the registry. The last tree of the episode is the
batch before it; an end mark on it moves there. It is listed in the run
manifest's `change_log_audit.outside_end` (the watcher's batch number, its
first and last write, whether it straddles).

For every end but an elapsed cutoff the two must agree. Since D-037 a mark
closes no batch: the end mark's `batch` is the batch in effect at its
instant, so its last write is at or before the mark's `at`. The rule:
that batch's last write may come after the end time by no more than the
watcher's settling window (`quiet_ms` in its `config.json`, 100 ms by
default), and no other batch's last write may fall between that batch's and
the end; otherwise `bundle.make` fails. A batch whose writes went on past
the end is outside the episode (above), and an end mark on it moves to the
batch before. After an elapsed cutoff none of this applies: the end mark is
audit only. Every retained record is reconciled with it,
by record type (`clock.END_CONTRACT`, `view.py`, `changelog.py`,
`observations.py`):

| Record | At the end |
|---|---|
| View: user message | Kept when sent at or before the end. |
| View: output to the user | Kept when delivered at or before the end. |
| View: turn | A turn whose closing output was delivered by the end is `finished` and closed by it, dated at that output, though its raw run completed after the end. A turn still active at the end otherwise (its run open, or ending later with no closing output by the end) gets a `turn_end` with `end: interrupted` at the end time and `closed_by: null`. |
| View: wait | A wait still open at the end, or ending later, ends at the end: `ended` is `budget_cutoff` when the end is a budget cutoff, else `episode_end`. Its charge is cut to the time up to the end on the episode clock (an excluded interval inside it is not charged), and a wait the runner left uncharged (`charged_ms` null) is charged that time. The validator checks `charged_ms` is at most `t_end - t` for every wait. |
| View: runner event | Kept when at or before the end; nothing follows `episode_end`. One exception (D-041): a probe copied after the end at the episode's last hand-back stays, dated at that hand-back and placed right after it, whatever order the runner's events arrive in. The view's duration is the end time. |
| Observations: shell attempt | Kept when started at or before the end. One that finished after the end, or never, has result status `unfinished` and no fingerprint or end time. |
| Change log: state | Selected on the raw log before any projection: the batch of the runner's end mark, else the last batch closed at or before the end. |
| Change log: metric reading | At most one per retained batch: the earliest reading that measured that batch's whole tree (it succeeded, and its copy lacked nothing the tree could give it), whatever asked for it (a change, a mark, the baseline, the final one) and whenever it finished, after the end or inside an excluded interval included: the tree it measured is what matters. A failed or incomplete reading is never kept. The kept one carries no time. |
| Everything else | Dropped from every bundle file. |

Only a reply the view scores must have its text: an audit-only trajectory
that misses one makes no bundle, and one that misses an answer only in the
post-run exchange or after the end does.

Probe copies are separate conversations in copies. The post-run awareness
exchange runs in the main thread after the end, and is left out by reference,
not by time. The runner names its message with user kind `post_run` in
`Hints.user_kinds`. The run that message starts, its turn, and every
non-runner event after it in file order are listed in the trajectory header
(`post_run`, by seq) and stay in the full trajectory for audit. They are left
out of the view, the identifiers, the observations, and the change log (which
ends no later than the exchange's first event), whatever their timestamps. A
`post_run` message dated at or before the episode end, or in an episode with
no end, is still left out, and is reported in the manifest's
`clock_anomalies`. A runner event inside the exchange, or naming it, is an
error. Probe and awareness results are in `episode.json`.

## The scorer view (`scorer.jsonl`)

A header (`events`, `turns`, `duration_ms`), then events with `seq`, `t`
and `type` and closed fields:

| Type | Fields | Why it is there |
|---|---|---|
| `turn_start`, `turn_end` | `turn`; `end` (`finished`, `interrupted`, `failed`, `open`) and `closed_by` | Routing needs to know when the agent handed control to the user, and with which output. `closed_by` is the view seq of the main agent's final answer, or of the question that handed control back, or null when the turn ended with neither. Main agent only. A `turn_end` with a closing output is dated at that output's delivery, right after it (m7 of round 11): a prose final message and a question through a tool then give the same order against the runner's verdict, wait and hand-back at that instant; without a closing output it keeps the run's end. Without the runner's `episode_end`, the view's duration is still the last turn's own end. The effective end is decided before the end cut: a final turn whose closing output was delivered by the end is `finished` and closed by it, though its run completed a little after the end. A `budget_cutoff` end needs its `budget_cutoff` row. |
| `user_message` | `turn`, `kind`, `text`, `secret` | What the user wrote: `opening_prompt`, `reply`, `correction`, `reset`, `reopen`, `reset_notice` (D-046: a reset's notice steered into the running turn; never a correction and never a reply to an output). |
| `to_user` | `turn`, `text`, `secret` | Everything the user was shown: final messages, message-tool text, questions. |
| runner events | below | Verdicts and `unjudged` records, waits, hand-backs (the closing output only), planted events, probes, resets, delivered corrections (with the mark's number and its `batch`), budget cutoffs, the end. |

The view is shaped so that two agents that showed the user the same things at
the same times give the same view, whatever their harness did out of sight.

- **The runner's shapes (D-023).** One verdict per output (or one
  `unjudged`, in a bundle with an episode record, never both), with a null
  `reply` when a fixed task message replaced the classifier's; one merged
  user message per hand-back; a refused secret request, shown as its
  `to_user` marked `secret`, then the user's fixed refusal as a plain
  `reply` (not marked secret), whether T3 records it as a user message or
  the runner passes it for the declined request in `Hints.answers`.
- **One shape for questions.** A question asked through a question tool
  becomes what a question in a final message is: one `to_user` with
  everything the user was shown (header, question, each option with its
  description when it differs from the label, and `(choose one or more)`
  for a multi-select), a finished turn closed by it, and the answer as a
  `reply` opening the next turn. A secret request is marked `secret`; its
  answer is a `reply` with `secret: true` and empty text. An answered
  question whose text cannot be recovered is an error, not an empty string
  (R19).
- **Dated at delivery**: one function says when an output reached the user
  (`format.delivered_at`), and every consumer calls it: the normaliser's
  runner-target resolution and its "not before delivery" checks, the
  view's times and its ordering against user messages, disclosures, and
  hand-backs. It is the driver's item delivery time, the instant the runner
  judges at: a final message when its item finished, not when its run
  ended (only an old capture with no item completion falls back to the
  run's end), a message-tool text when the tool's result came back, a
  question when it was put to the user. A verdict or wait dated before its
  output reached the user is an error.
- **A delivered correction's batch.** The view's `correction_delivered`
  carries its delivery mark's neutral number (`mark`) and the batch that mark
  closed in `changes.jsonl` (`batch`), which `bundle.make` fills in; the
  runner's label stays in the full trajectory. A delivery whose mark is
  unavailable (no mark, or one the watcher did not accept) has `mark`,
  `batch` and `straddled` null, and the post-correction Steering share is
  undefined for that episode (D-030).
- **Hand-backs** (D-028, D-029). The runner's `handback` events are kept
  with the output that closed each (`target`, null for a silent one). A
  hand-back is validated on its own terms: its output is a user-channel
  output of any agent of the tree (a child's question, a message through
  the message tool, a final message), delivered at or before it. It need
  not be the `closed_by` of the main turn before it: `closed_by` stays what
  the main run's own ending said, and a main run can end while a child is
  still working (D-029). The hand-back's kind (finished, or a pending
  request) stays in the full trajectory: a question through a tool and one
  in a final message are the same hand-back to the user (D-018).
- **Waits** name every output whose verdict reply they carry
  (`replies_to`, empty for a wait caused only by a fixed message, a reopen,
  the reset or a refusal); `target` is the output that closed the
  hand-back, or null; `causes` is everything that was due at the hand-back,
  a sorted list. Under D-037 a wait names every output answered at its
  hand-back, across all pending requests, and `causes` is their union. A
  bundle with an episode record needs `replies_to` and `causes` on every
  wait; an older runner's single `cause` is read as a one-item `causes` only
  for an episode with no runner record (the run manifest counts such waits
  in `legacy_wait_cause`), or in a replay of an episode recorded before
  D-035 (`Hints.legacy_waits`).
- **How waits were charged** (`wait_charges` in both headers): `scheduled`
  under the current runner (D-035: the return delay cut to the budgets),
  where a charge never exceeds the span of the wait's floored stamps, and
  the validator holds that exactly; `measured` in a replay of an episode
  recorded before D-035, whose runner measured each charge from its own
  stamps: the recorded charge is kept, and may be 1 ms above the projected
  span (one rounding). The replay of `021448/rdemo-1` has one such wait,
  charged 2,009 ms over a 2,008 ms span.
- **Turns after sorting**, by delivery time. Output that arrives after a turn
  ended has the next turn, or none.
- **Left out**: tool calls and outputs, narration, subagents (a child's
  user-channel output is an unmarked `to_user`), the question mechanism,
  errors, unknown items, truncation markers, tool origin, segments the runner
  did not declare. No score reads them, and they carry the structural
  differences between harnesses.

## Identifiers (`identifiers.json`)

Each row is an identifier that occurs in what the user saw or wrote: `text`,
`kind` (`path`, `external_path`, `token`, or a task-declared kind), `known`
(`{seq, t}` of the view's `user_message` that made it known, or null) and
`disclosed_while_private` (`{seq, t}` of each `to_user`). Rows are sorted by
text, then kind, one row per identifier: by declared kind and text, for
every kind, so a job id `job-7f3a9c` and a root file `./job-7f3a9c` are two
rows, and so are two declared kinds whose patterns match one spelling. A
generic token is not a declared kind: a token with the spelling of a
declared identifier is that identifier. `aux` holds
the auxiliary counts. There is no first-seen data,
occurrence count, agent or source: those would reveal hidden events. The file
is computed from the view, the change log and the task's identifier kinds
only. The audit registry (`registry.build`) reads the full trajectory and
keeps provenance; it is outside the bundle. The validator checks that `known`
names a `user_message` and each disclosure a `to_user`, at that time, with
integer fields, and that no identifier (kind and text) has two rows.

A path has lifetimes in the change log: from its creation, or its arrival by
a move, to its deletion or a move away, and possibly created again
(`registry.lifetimes`), the files a directory rename carried included (the
watcher writes one record for the directory, none per child). Every path of
the replayed log is in the alias index. A bare file name in a text resolves
only among the paths alive at the time of the text, and a dead path never
shadows a live one: a deleted root `answer.txt` does not hide a live
`new/answer.txt`. An explicit spelling (`old/answer.txt`,
`./old/answer.txt`, or the absolute path) keeps naming its path after a
deletion, as history, unless a live path takes that bare name. Lifetimes
and text times are compared on wall microseconds (the view's and the change
log's in memory), like every other decision. The audit registry, given a
change log read back from its file, maps its episode milliseconds back to
the trajectory's wall clock (`Clock.wall_us`), so both are on one clock.

## The change log (`changes.jsonl`)

| Record | Fields |
|---|---|
| `change` | `seq`, `batch`, `t`, `kind` (`created`, `modified`, `rewritten`, `deleted`, `moved`, `attribute`), `path`, `from`, `sha256`, `content` (`stored`, `oversize`, `unreadable`, `inaccessible`, `none`), `file_type`, `mode`, `target` (a symlink's), `content_unknown` (D-032: true on every change to a file whose content is unknown, from its first record as unreadable or its first write the watcher could not read, through later metadata changes and renames, until it is deleted; the deletion itself carries it too) |
| `batch` | `batch`, `t`, `t_first`, `baseline`, `resync`, `incomplete`, `settled`: the boundary at which the batch's state is defined. `t` is when the batch's tree is in effect: its last write (the watcher's `last_write`), or its commit for a batch with no write times. `t_first` is its first write, so `[t_first, t]` is the batch's write span (see below). |
| `git` | `batch`, `t`, `head`, `refs` |
| `mark` | `batch`, `t`, `mark`, `straddled`, `straddled_last`: `mark` a neutral number, 1, 2, ... in order of appearance; `t` the instant the mark stands for (D-037); `straddled` and `straddled_last` the first and last batch whose write range strictly contains it, or both null; `batch` the last batch entirely at or before it that precedes `straddled` (the watcher's `replay.answer_for`). A log without `straddled_last` is resolved by the same rule. Readback checks that both are batches of the log and that each batch of the run ended after the instant. |
| `metric` | `batch`, `ok` (always true), `metrics`, `absent` (the paths no copy of the tree can hold: the coarse git state `.git`, files unreadable in the workspace itself, inaccessible directories and what is under them), `absent_count`. No time, run number, reason or mark: when and why a reading was taken is driver activity (marks at turn ends, handoffs, the post-run turn, the export). |
| `no_metric` | `batch`, `reason`: a retained batch with no reading of its whole tree, and why, from a closed set that does not describe driver activity: `none_taken` (no reading of that tree), `failed` (every reading of it failed), `incomplete` (every successful one lacked a path the tree could give). The detail (no metric command, skipped by a later reading, not recorded, the paths an incomplete copy lacked) is in the run manifest's `change_log_audit.no_reading`, beside which reading each batch kept (`change_log_audit.readings`). |

Every retained batch has exactly one `metric` or `no_metric` row, placed
after its `batch` record. A file change is a net change of a path's type,
mode, hash or link target between two boundaries (`ChangeLog.net_change`).
Under D-032 a path whose content is unknown at either boundary is not
counted: a Steering scorer leaves out every change record with
`content_unknown: true` from its denominators and numerators, and treats no
two trees that hold such a file as equal (`ChangeLog.unknown(batch)` lists
those paths).

**Unsettled batches.** The watcher commits a batch `settled: false` when the
tree did not hold still through its validation rounds; that boundary is not
necessarily a state the tree had at one instant. Such a batch is kept and
carries `settled: false` as a neutral boolean: dropping it would move its
changes to a later batch and change what the next boundary means, and the
flag is the watcher's, not the harness's. No score is computed differently
on it here. The bundle maker reports in the manifest (`bundle_notes`) an end
mark or a correction's delivery mark that falls on an unsettled batch, since
the runner takes those marks with the agent idle and expects them settled,
and the watchers' shutdown facts from `heartbeat.json`: a final batch
committed unsettled (`final_settled`), events after the final batch closed
(`late_events`, `still_changing`, `late_overflow`, and `late_capped` when
that count reached the watcher's cap), and readings that did not run as a
separate user (`metric_isolation` other than `users`). None of these fails
the bundle.

D-026: the watcher leaves out the harness's instruction files (`CLAUDE.md`,
`AGENTS.md`, at any depth) and git refs in the driver's namespace
(`refs/t3/`, loose and packed) itself, and any name that contains an
ignored name (`.CLAUDE.md.tmp`, `CLAUDE.md~`), so a temp-file write of an
instruction file leaves no trace; they never enter its log, its store or
the tree its metric runs on, and activity on them changes neither a
batch's `events`, its event or write times, nor where batches are cut.
Since the watcher's fourth round, names are ignored by exact match plus a
fixed list of temporary forms (`ignore_name_forms` in its `config.json`),
so `AGENTS.md.example` is recorded again; the bundle gate matches the
basename exactly and does not flag it. Git is recorded as HEAD and refs only
(D-036 removed the mode that recorded the contents of `.git`). Nothing here filters them. The bundle
gate fails if either appears in `changes.jsonl` (in a path, a link target, a
reading's absent paths, a ref, or the head with or without its `ref: `
prefix), as a check that the source did its job.

`changelog.build` reduces the watcher's records to task state. All of it is
decided on the watcher's wall times at full precision, in this order:

1. **The episode end** selects the final state on the raw log, before any
   projection (above): the batch of the runner's end mark, which must agree
   with the end time, else the last batch closed at or before the end.
2. **Nothing is merged across batches** (D-028). A file created in one batch
   and moved in a later one is a creation and a move, whether it was a temp
   file or task output renamed a moment later (draft.txt to final.txt): the
   two have the same shape, so no rule on the records can tell them apart.
   Within one batch the watcher already records the net result, which
   covers a tool that writes a temp file and renames it at once: it writes
   only the target's record (its `via_rename` detail is not carried into the
   bundle). The limit: a write tool slower than one batch (100 ms by default)
   between creating its temp file and renaming it shows an extra change, the
   temp file's creation and its move. A change of identity or owner alone,
   which the change log does not keep, is `rewritten`.
3. **Excluded intervals**: bookkeeping inside one is dropped; a change is an
   error.
4. **Marks** (D-028): only those a bundle record refers to are kept: the end
   mark, each correction's delivery mark, each probe's target mark. They are
   numbered 1, 2, ... in order of appearance; the labels the driver and the
   runner gave them stay in the full trajectory, the manifest, and the
   in-memory `ChangeLog.mark_ids`. A mark ID that appears twice in a log is
   an error. Kept marks move to the retained batch whose state they saw.
5. **Readings.** One per retained batch: the earliest reading that measured
   that batch's whole tree, whatever asked for it (a change, a mark, the
   baseline, the final one). Definition (D-036): a reading measured the
   whole tree when it succeeded (`ok`) and the watcher did not mark its copy
   incomplete (`complete` is not false). For a log from before the watcher
   gave `complete`, when its copy lacked only what no copy can hold (where
   the watcher states `complete`, that is the evidence: a display of absent
   paths it capped, `absent_count` above the list, is not a cut copy, and
   each displayed path is still checked) (the
   coarse git state, a file unreadable in the workspace itself, an
   inaccessible directory and what is under it, and an entry of a type no
   copy materialises: a fifo, a socket, a device, other), with its absent
   list not cut. A file left out for size (oversize) makes the copy partial: such a
   reading is kept for audit and is no reading of the tree. Its time relative to the end or to an excluded interval does not
   disqualify it: the tree it measured is what matters, and the kept row
   carries no time, reason or run. A failed reading (a timeout, a bad exit,
   an unparsable result, or the watcher's `copy verification failed: ...`)
   and an incomplete one (a file left out for size, a missing blob) are
   never kept. No reading
   is ever moved to a neighbouring batch. The others stay in the full
   record. Every retained batch without one has a `no_metric` record with
   `none_taken`, `failed` or `incomplete`, the detail in the audit record.
   Since the watcher's fifth round (D-036) every committed batch gets a
   reading computed from its own recorded state, reasons `baseline` or
   `change`; a reading reused from an earlier batch with an identical tree
   (`reused_from`, `reused_run`) is this batch's reading; one added after
   the episode by the watcher's `complete-metrics` (`offline`) counts the
   same; a batch not measured before shutdown has a `backlog` record
   (`measured: false`), which is no reading (`none_taken`, detail `backlog`).
   Completeness comes from the reading's `complete` (with `lacking`, the
   paths that made it partial) when it is there. A mark creates no reading
   of its own. Older logs (mark and final readings, `skipped` ranges) are
   read as before.

**One watcher log per context segment.** A controlled reset (D-010) stops
the watcher on the old volume and starts one on the clone, so a reset episode
has one watcher log per segment. `changelog.join` (and `bundle.make`, given a
list of watcher inputs, one per segment, in order) joins them into one
task-state sequence:

- each later log's baseline must equal the task state the previous logs ended
  in, or the join fails naming the differing paths;
- the join produces no change records;
- batches, the batches a reading lists as skipped, and changes are numbered
  across it;
- marks a later log issued on its batch 0 (before its first change) stay, on
  the batch the previous log ended in;
- the handoff's own bookkeeping is dropped: a later log's baseline records;
  its baseline reading measures the state the previous log ended in, and
  is that batch's reading only if no earlier successful one exists (an
  earlier log's final reading usually is); the batches a reading lists as
  skipped count across the join; the handoff is an excluded interval of
  the episode clock.

Each segment has its own content store, and `bundle.write` takes each blob
from whichever store holds it.

A batch with records but no closing `batch` record is an error, except the
last one, which the watcher was still writing; its records are left out. A
gap in a log's `seq` (the watcher numbers 1, 2, ... with none) is an error. A
watcher whose heartbeat does not say `stopped` (it failed, or was still
running when its output was copied) is accepted only when the runner's end
mark bounds the log, and only for the last segment.
`ChangeLog.batch_at_us(us)` gives the batch in force at a wall microsecond
(in memory, where `wall_us` holds each record's), `ChangeLog.batch_at(t)` the
same from a written log at millisecond precision, and `ChangeLog.tree(batch)`
the task tree after a batch. Free-form strings
(reasons, errors, request ids, commands, stderr, raw paths of unwatched
directories, mark labels) are gone; what remains are closed flags and
neutral numbers. The watcher's mark labels must be runner-issued ids
(`[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}`, and on the runner's list when it passes
one). The gate fails, not counts, on an instruction file, a driver ref, or a
mark that is not a neutral number. The validator also checks that marks are
numbered in order, that every batch has exactly one `metric` or `no_metric`
row and after its `batch` record, that only batch 0 is the baseline, that
`settled` and `content_unknown` are booleans, that a mark's `straddled` is
null or a later batch, that a reading's absent paths are paths of its
batch's tree (or the git state), that a kept reading succeeded and carries
a metrics object, and that no change or git record comes after its batch
closed.

### The watcher's format

The reader follows `cohbench/watcher/README.md` and the watcher's code as of
2026-11-22, and is tested on real output: `tests/trajectory/fixtures/watcher-real/`
holds two watcher output directories made by `python -m cohbench.watcher` in
a Linux container (Docker Desktop, kernel 6.10.14-linuxkit, Python 3.12.16)
over a scripted sequence, with `make_fixture.py`, the script that made them,
and `sequence.json`, the UTC time of every step. The sequence covers an
in-place write, an identical rewrite, a temp name reused inside one batch and
across two (over an existing file and onto a new one), an oversize file, an
instruction file written, edited, rewritten through `.CLAUDE.md.tmp` and
backed up as `CLAUDE.md~`, and a driver ref under `refs/t3/` updated (none
recorded: the watcher leaves them out itself, and the script passes no
ignore rules), marks, a metric command, a stop and a
restart on a copy of the tree (two segments across a handoff), a rename
cycle (written as a `resync` of deletions and creations), an inaccessible
directory, and a write after the end mark. The fixture was regenerated on
2026-11-22 with the watcher's sixth fix round (`first_write`,
`last_write`, `first_write_after`, `write_times`; marks with `accepted`,
`at`, `batch`, `straddled`, `straddled_last`; readings with `complete` and
`lacking`, one per committed batch). Batch records' `write_times` is
`known`, `none` (a sweep-only batch) or `unknown` (an overflow or recovery
batch); either of the last two has its upper bound at its commit, the batch
row's own time, which is the bound the watcher's marks use. Every batch's
span opens at its recorded `first_write_after`; a batch with `write_times`
but no lower bound has no span. Only a log without `write_times` falls back
to `first_write`, or for an `unknown` batch to the previous batch's last
write. The `mark` batch reason of older logs is read and ignored. When the
change log resolves an instant itself (a probe's instant, the end cut, an
older mark without `straddled`) it uses the watcher's rule: the batch in
effect is the last whose `last_write` is at or before the instant, and a
batch straddles it on the span above. The tests check the projected
tree at each mark against the watcher's own replay
(`cohbench.watcher.state_at`), and each kept reading against the projected
tree of its batch.

State records `created`, `deleted`, `moved` (with `from`), `modified` and
`attrib` (with `changed` and `via_rename`), `rewritten` and `git` carry
`batch`; `content_unknown: true` is on every record of an entry whose
content is unknown (an unreadable file once written), for the life of the
inode and on every name, and the change log takes it from the record (older
output flags only the write; the change log carries the flag forward
itself, and also treats a file recorded `unreadable` as unknown, D-032). A `batch` record
ends each batch (`resync`, `recovered`, `unwatched`, `renames`,
`first_event`, `last_event`, `first_write`, `last_write`, `settled`), and batch 0 is the baseline. `mark`
records carry `label`, `batch` and `accepted`; since D-037 a mark never
closes a batch (batches are closed by the file activity alone, so they are
the same whatever the runner did) and also carries `at`, the instant it
stands for, `batch` being the batch in effect at it and `straddled` the
batch whose writes began at or before it and ended after it, or null (and
`covered_utc`, not read). The change log takes `at`, `batch` and
`straddled` from the record; an older log, in which a mark closed its
batch, is resolved by time the same way (the mark's instant its record
time, a batch whose write span contains it straddled). A mark with
`accepted: false` (its drain did not finish) took no reading and names no
state, so it is never kept and resolves no boundary, probe target or
correction delivery (a correction whose mark was not accepted has `mark`
and `batch` null; an end mark not accepted fails the change log, except
after an elapsed cutoff, where the end mark selects nothing); a record
without the field counts as accepted. A mark the watcher did not accept
(`accepted: false`, `batch: null`) is set aside as an audit record before
anything else is read (`change_log_audit.rejected_marks`): its shape never
fails the build. A mark is inside the episode when its `at` is at or before
the end, whenever the watcher acknowledged it (its record time only for a
mark without `at`). Metric readings carry `run`, `batch`, `reason`
(`baseline`, `change`, `backlog`, `internal`; older logs also `final` and
`mark`, with the mark's `label`, and `skipped` ranges), `measured`,
`complete`, `lacking`, `reused_from`, `reused_run`, `offline`, `absent` and
`ok`. Batch records may carry the reason `recovery` and
`validation_listed` (not read). Read and not carried: an entry's `epoch` (the
`content_unknown` flag carries what a change needs), a batch's `unsettled`
paths, `validation_rounds` and `root_accessible`, a mark's `settled`,
`quiescent` and `unsettled` (the batch's own `settled` is the fact that
matters), a reading's `isolation`, `links_outside`, `cwd` and
`stragglers_killed` (how the reading ran, reported through the heartbeat's
`metric_isolation`), and the heartbeat's shutdown facts, which go to the
manifest (above), `late_capped` and `event_log_broken` among them (the
watcher then stops `failed`, so the log is read only up to an end mark). Also not read: `config.json`'s
`ignore_names_sha256` and `ignore_git_refs_sha256` (digests where the lists
used to be; only `quiet_ms` is read from `config.json`), and a mark's
acknowledgement in `control/`, which can say `ok: false` when its drain did
not finish (the runner's concern: the mark record in `events.jsonl` is what
the change log uses). A reading that failed, `copy verification failed:
...` included, has `ok: false` and is never the batch's reading. Entry fields are `type`, `mode`, `uid`, `gid`, `dev`, `ino`,
`size`, `sha256`, `content`, and `target` and `error` where they apply. The
content store is `blobs/<two hex>/<sha256>`. Fields the reader does not use
are ignored. These are errors: an unknown kind, an unclean path, a missing
or non-integer `seq` or `batch`, a gap in `seq`, a time without a zone, an
unknown `content`, a `sha256` that is not a digest, and a mark without a
valid id. The format the README described before 2026-11-22 (records
without `batch`) is no longer read.

`python -m cohbench.trajectory changelog DIR [DIR ...] --manifest M
[--end-mark ID] [--excluded FILE]` joins one directory per segment.

## Observations (`observations.jsonl`)

The task names command patterns, each with an optional capture: `{"attempts":
[{"name": "measure", "regex": "\\./measure", "capture": "^RESULT (ops=\\d+)$"}]}`.
A capture has exactly one group, and that group takes part in every match
(not under a repeat that allows zero, not inside an alternation); the
declaration is refused otherwise. A match whose group did not take part all
the same is no result, not an error. Each shell call by any agent whose
command matches becomes a record. Without `by`, extraction retains the same
one-attempt-per-call behavior and output bytes.

An attempt spec can instead add `"by": "output"`. This requires `capture`.
Every whole-line match in any shell call's output is an attempt, regardless
of the command text. Thus `cd bin && ./profile`, a wrapper, and three runs
in one call are recognized; three result lines produce three attempts. Each
fingerprint hashes that match's captured group. The attempts share the call's
start, end and workspace state. The whole-line clipping rule below applies
unchanged. Every output pattern is collected, including several kinds from
one shell result. Legacy command patterns retain first-match, independently
of output patterns.

The command and output tests form a union: a command matching `regex` yields
at least one attempt, even with unavailable output or no whole result line.
That fallback has no fingerprint. Matching result lines replace the fallback,
so three whole lines produce three attempts, not four. Three runs with all
result lines hidden yield only the command-backed lower bound of one. A
wrapper that hides both its command name and result lines is still missed.
Call completion and cancellation still determine result status.

Each record has these fields:

| Field | Meaning |
|---|---|
| `type`, `seq`, `name` | `attempt`, order from 1, the pattern's name |
| `source` | `shell` or `log`. Older records without it read as shell observations and retain their bytes. |
| `log_state` | Log observations only: the tool's input digest. |
| `t`, `t_end` | start and end on the episode clock (`t_end` null when unfinished) |
| `state`, `state_unknown` | a digest of the task tree it ran in: each path's type, mode, content hash and link target after `batch_start`. Two attempts in equal trees have equal states, whatever happened between them. A tree that holds a file whose content is unknown (D-032) has no comparable digest: `state` is null and `state_unknown` true, and a null state equals nothing. |
| `batch_start`, `batch_end` | the batches in force at its start and end (decided on wall microseconds), for replay |
| `result` | `{status, fingerprint}` |

There is no exit status: no score uses it, and a null from a harness that
does not report one against a zero from one that does would name the
harness.

Statuses and the fields each may carry (`observations.STATUS_FIELDS`, which
the validator enforces):

| Status | Meaning | End | Fingerprint |
|---|---|---|---|
| `complete` | the command finished and its result is known | yes | the result's hash, or null when the whole output holds no result line |
| `truncated` | the output was cut and the result is not known whole | yes | null |
| `cancelled` | the command was stopped | yes | null |
| `unfinished` | still running at the episode end, or never finished | no | null |
| `unavailable` | no output was recorded | yes | null |

With a capture, the status describes the task's result. It is `complete` when
the capture matched a whole line, and the fingerprint is the hash of what it
captured, the same for every harness whenever the line survived whole. The
adapter records which edges of every output were cut (`output.clipped`:
`none`, `head`, `tail`, `middle`, `both`). A line is whole when it does not
touch a clipped edge: when the head was cut it is not the first retained
line, and when the tail was cut a line break inside the retained text ends
it, or it is the last retained line and the adapter recorded that a line
break ended it before cleanup removed it (`output.last_line_whole`: T3's
transport cut appends `\n… output truncated for transport` after a byte
cut, so the kept text ends with a line break exactly when its last line is
whole; Pi's and Claude Code's line-count notices keep whole lines; Claude
Code's character preview does not). Nothing is whole when the middle was
cut, since where is not known. A
clipped prefix of the result line (`RESULT ops=89` of `RESULT ops=899`) is
therefore no result. In command-based mode its status is `truncated`.
In that mode, with no whole match, the status is `truncated` when the output was cut, else `complete` with a null
fingerprint. Without a capture, the status is the output's (`truncated` when
cut) and the fingerprint the hash of the whole output when complete. Two attempts are equivalent when name and a non-null state are equal, and
their fingerprints are equal when both have one; when either fingerprint is
null, name and state decide alone, so that equivalence does not depend on
how much output a harness kept (D-048). A hash over the part of
a long output one harness happened to keep is not evidence that two attempts
returned the same information, and it differs by harness for the same task
result; so tasks print a short result line and declare a capture for it
(D-022). Nothing else from the call is kept: not the command, not the
output, not the agent. Further kinds are added as record types in
`observations.KINDS`. The validator checks types, the fields of each
status, the batches in force at each time (either side of a tie in
milliseconds), and the state digest against the change log; with the view,
that no attempt completes after the episode end.

### Attempts from collected logs

An attempt spec can add `"log": "attempts"` beside its existing keys. The
name refers to a task `collect` entry. In a collect path, `~` names the agent
account home from the container user database, never the `HOME` environment
variable. The agent container
has no `XDG_STATE_HOME` override. The driver reads that file as the
agent user from each harness container before a controlled reset loses its
home, and again before final teardown. It concatenates segment snapshots
on the host in segment order. The file is outside the workspace and does
not produce change records. A failed read rejects the export. The cap is
1 MiB across all segments for each collect name.

`episode.json` adds `collected`, one record per declared name:
`{"attempts": {"status": "collected", "bytes": 123, "sha256": "..."}}`.
A collected file, including an empty one, has a digest and its exact bytes
in `blobs/<two hex>/<sha256>`. `absent` has zero bytes and a null digest.
`oversize` has a byte count above the cap, a null digest, and no blob.
The raw export keeps the same records in its manifest and the bytes under
`collected/blobs/`. Bundle validation checks the digest and byte count;
`verify-episode` reads the raw blobs and rebuilds them byte for byte.
`attempt_logs` maps attempt names to collect names and is absent or empty
when no spec declares a log.

The standalone `bundle` command reads a saved `--record`'s collected blobs
from `export/collected/blobs` beside that record, using the same size and
digest checks as rebuild. `--collected-blobs DIR` selects another location
when the record or blobs were moved. Missing or corrupt blobs stop the
command before it writes a bundle.

Each JSON line has exactly `start_ms`, `end_ms` (nonnegative integer epoch
milliseconds, with end at or after start), `state` (a hexadecimal digest or
null when inputs changed during the run), and `result` (a UTF-8 string). Valid lines yield `source: "log"`,
`log_state` equal to the line's state, including null, `state: null`,
`state_unknown: false`,
and null batch fields. `result` is `{status: "complete", fingerprint:
<sha256 of the result string>}`. Shell-derived records retain their existing
extraction rules and gain `source: "shell"`. Attempt probes select those
shell records.

The log stamps are compared on wall microseconds using the recorded opening
instant, then converted once to episode milliseconds. A line before the
opening or completing after the episode end is dropped. A stamp inside an
excluded interval is placed at that interval's start. Records are ordered
by start, end, and attempt name, retaining line order for ties. The header's
`malformed_logs` maps each collect name used by an attempt spec to its
invalid-line count. A null state is valid and does not increase the malformed
count. Malformed lines add no observations. A partial final line is parsed
by the same rule. Empty files have zero malformed lines.

The optional `log_lines` header maps each collect name to retained zero-based
line indexes in observation order. Standalone bundle validation checks that
selection's count, order, states, result fingerprints and malformed count
against the collected bytes. It does not verify time placement because the
scorer files omit the wall-clock origin and exclusions. `verify-episode`
checks selection and times by rebuilding from the raw clock. Older bundles
without indexes are checked for content and multiplicity as a subset of
valid lines. Their order requires the raw clock because exclusions can
collapse timestamps into ties.
New raw manifests carry `observation_log_lines: true`; its absence preserves
the older header on rebuild.

Old bundles without `source`, `collected` or `attempt_logs` remain readable.
A raw manifest's `observation_sources: true` selects the current extraction
format; its absence keeps the older observation bytes during rebuild.
The log is agent-owned. Editing or deleting it changes the evidence for the
loop score; nothing tells the agent that it matters.

## The episode record (`episode.json`)

The runner writes the record (`cohbench/runner/record.py`) and passes it to
`bundle.make` as `episode_record`, with `probe_targets` beside it.
(`EpisodeInput` is the same contract in dataclass form and goes the same
way.) The bundle maker finishes it:

- `end.duration_ms` and `budget.consumed.elapsed_ms` are taken from the view.
  Both count from the same instants (the dispatch, the end, the excluded
  intervals); the runner rounds its span to the nearest millisecond and the
  bundle rounds the same span down, so they may differ by one rounding:
  `bundle.ELAPSED_TOLERANCE_MS` is 1 ms, and more is an error. On the saved
  run `rdemo-1` both are 29,323 ms.
- `budget.consumed.user_wait_ms` is the sum of the view's wait charges, the
  figure every share uses. The charges are the runner's own, so its figure
  must agree exactly (`bundle.USER_WAIT_TOLERANCE_MS`, 0), a check skipped
  when the view cut a wait at the end (the runner may count the open wait
  differently).
- Every wait names the outputs it replies to (`replies_to`, possibly
  empty) and everything due at its hand-back (`causes`). A wait without
  `replies_to`, or with an older runner's single `cause`, is accepted only
  in an episode with no runner record; the readback validator checks
  `replies_to` is a list whenever `episode.json` is present.
- `awareness` keeps `asked` and `outcome`; the reply text stays in the run
  manifest, outside the bundle.
- `reach` is computed from the view's planted events and the schedules'
  `required` flags.
- Each probe target is resolved, with its boundaries.

The runner's probe targets are `{about, ref, event, deferred}` (a
`coincides` key, from before D-028, is ignored):

| Runner's `about` | `ref` | Resolves to |
|---|---|---|
| `output` | `{"item": id}` | the user-channel output made from that item: kind `output`, a view seq |
| `output` | `{"run": id}` | that run's last output before the probe mark |
| `message` | `{"message": id}` | that user message: kind `user_message`, a view seq |
| `message` | `{"item": id}` | the user message made from that item; for a message delivered as the answer to a pending question (the driver returns no message id for one), the question's item id or request id names the answer |
| `message` | `{"run": id}` | the run's last user message, or merged answer, before the probe mark |
| `change` | `{"mark": id}` | the change-log batch in effect at the mark's instant (D-037): kind `change`. It is eligible only with a net tree change (D-025), never the baseline, and at or before the copy's batch. A mark whose instant a batch straddles is `no_target`. |
| `attempt` | `{"mark": id}` | the last attempt completed at or before the mark (and by the copy), on wall microseconds: kind `attempt`, an observation seq. One started before the mark and completed after it is not the one. |
| `change`, `attempt` | `{"utc": instant}` | the tree in effect at that instant (ISO 8601 with a zone): the batch whose `last_write` is the latest at or before it, then as by a mark. An instant inside a batch's write span (the watcher's predicate, strict at both ends: after the batch's recorded `first_write_after`, whatever its `write_times`, and before its `last_write` (at the lower bound the batch's writes all came after; at the last write the batch is in effect), or before its commit for a `none` or `unknown` batch; only a log without `first_write_after` falls back to `first_write`, to the previous batch's last write for an `unknown` batch, or to the commit for a `none` one), so an instant resolved here gives exactly the watcher's own mark's answer is ambiguous, and so is one inside the writes of a batch cut at the end, and an instant after the probe's copy names nothing: either way the probe is `no_target`, for change and attempt targets alike. A mark the watcher did not accept resolves nothing either. The runner names every attempt probe by the event's instant. |

A probe never moves the end (D-041). Probes due at a hand-back are taken at
that hand-back whenever the runner gets to it, so a probe due at the
episode's last hand-back (the final one, or one whose wait ends in a cutoff)
can have its copy taken after the end instant. Such a probe stays in the
bundle, dated at that hand-back and right after it in the view, with the
copy's own time in the run manifest's `runner_audit` (`copied_utc`); any
other event after the end is still outside. Saved runs in which the end
fell just after the probe's excluded interval (the runner's round 7) still
read as before.

`deferred` is recorded as the runner gives it (D-028): the copy was taken at
a later hand-back than the first one at or after the probe's event. It is
never checked against file batches. Nothing about a probe can fail the
bundle except a malformed record (a target without its `event` or with a
`deferred` that is not a boolean or null, a malformed `ref`, a probe with no
target, a probe mark missing from the view, or an attempt probe in a task
that declares no attempt pattern). A target that cannot be resolved (a mark
the change log does not hold, no attempt completed by the mark, no output or
message before the probe mark), is not eligible (no net tree change), or
lies after the copy is recorded with status `no_target`, `about` `{kind, ref:
null, boundary: null}`, its copy boundary and no answer. The reason goes to
the run manifest (`probes_no_target`) and `Bundle.notes`, never to the
bundle. Probes are diagnostic and never exclude an episode (the root README section
3).

| Field | Content |
|---|---|
| `episode` | An opaque id. |
| `grouping` | `family` (`routing`, `explanation`, `steering`), `seed` (a non-negative integer: a caller-chosen string could carry anything, a model name say), `pair` (an opaque id, null outside sibling pairs), `sibling` (`A`, `B` or null, exactly when there is a pair), `control` (bool), `horizon` (opaque label), `mode` (`default`, `coached`). |
| `budget` | `limits` `{elapsed_ms, tokens, user_wait_ms}`, each a count or null (null or zero: the score's share is undefined and reported so), and `consumed` `{elapsed_ms, user_wait_ms}`. `elapsed_ms` equals the view's duration and `user_wait_ms` the sum of the view's wait charges. Consumed tokens are not in the bundle. |
| `planted` | `[{name, scheduled}]`, names unique; every name the view reports `fired` is here. `scheduled.required`, when present, is a boolean. |
| `collected`, `attempt_logs` | Optional for older records. Collected file status, byte count and digest by collect name, and attempt name to collect name, as described above. |
| `resets` | Optional for older records. One `{event, reached, fired, performed, reason}` per scheduled reset. The three flags are booleans. `performed` names a completed controlled handoff, even with an unsent continuation. `reason` is null when performed, otherwise the episode end reason followed by `_before_reset`, such as `finished_before_reset`. The view checks the reached and fired flags and total performed count. |
| `reach` | `{status, not_reached}`, over the required events only: `not_reached` with the required events that never fired by the end, else `reached` with an empty list (D-024). Per-event reach, optional events included, is the view's `fired` rows. |
| `answers` | Explanation only (null otherwise): the view seqs of the outputs that closed the first and second scored hand-backs (all hand-backs, or those after `answers_after`'s event, as in the scoring table), as far as the episode got (an entry is null for a hand-back with no output). |
| `answer_key` | `{family, payload}`, validated by family. Routing: `conflict` and `settled_by_workspace` booleans, `expected_decision` and `criterion` text or null. Explanation: `options`, six distinct texts; `true_cause`, an index into them; `follow_up`, the fixed follow-up text; `answers_after`, absent, null or the name of a planted event in `planted` (D-046); `question`, absent in legacy keys or the main question as non-empty text; `buried_control`, required, `{filler, sentence}` with a filler of at least 80 words that contains neither the sentence nor any option's text (checked literally, ignoring case). Steering: `objective` and `proxy` names, `direction` `min` or `max`, `correction_rule`. |
| `control` | Null, or `{expected, passed}` exactly when `grouping.control` is true. `passed` is a boolean the family scorer fills by judging `expected` against the episode; the runner leaves it null, and null means not judged. |
| `probes` | `[{probe, event, about, boundary, deferred, question, expected, answer, status}]`. `event` is the planted event the probe was scheduled with, a name in `planted` whose `scheduled.probe` names a kind, and `about.kind` is that kind (`message` in the runner's schedule is `user_message` here). The scorer view holds exactly one `probe` mark per probe. `about` is `{kind, ref, boundary}`: kind `output` or `user_message` (ref a view seq, and its own boundary that seq), `change` (ref a batch with a changed path, never the baseline; boundary that batch) or `attempt` (ref an observation completed by the copy; boundary the batch in force at its end). `boundary` is the copy's: `{seq, batch}`, the view seq of the probe's `probe` mark and the batch in force there. The action's boundary is at or before the copy's by position: a view seq before the probe mark, a batch at or before the copy's batch. `deferred` is the runner's (a boolean, or null when unknown). `status` is `answered`, `unavailable`, `failed` or `no_target`; only an answered probe has an answer, and only a `no_target` probe has a null `about.ref` and `about.boundary`. |
| `awareness` | `{asked, outcome}`, outcome `suspected`, `not_suspected`, `unclear` or null (none when not asked). |
| `end` | `{reason, duration_ms}`, agreeing with the view. |
| `poll_s` | The runner's polling interval in seconds, a fixed benchmark parameter (D-040), so a report can refuse to compare runs that differ in it: a positive number in a current record, which the bundle maker requires; null only in a record from before the runner carried it (the replay fills null). |
| `task_sha256` | The task instance's digest, 64 lowercase hex characters (D-046), so a report can tell which instance an episode ran. The bundle maker requires the key in a current record. On readback the key may be absent: the bundle was made before the field existed. A rebuild from such a raw record leaves the key out again, so it compares equal to the stored bundle. When present it must be 64 hex characters or null. |

No structural field may name a harness, model, provider or driver, and the
gate fails on one. Task-authored structure (planted schedules, the answer
key, control expectations, and a probe's question and expected answer, all
task-authored) is structure too: a tell there fails. A probe's answer is
content: a harness name there is counted, not removed. The release report
joins configurations on the episode id. `task_sha256` is task-owned: the
same instance has the same digest under every configuration, so it is no
harness tell.

## Runner events

The runner passes them to `normalise` as `Hints.runner`: `RunnerEvent(type,
utc, ref, fields, end_utc)`, with `ref` a T3 id it knows (`item`, `run`,
`message` or `thread`).

| Type | Fields | Meaning |
|---|---|---|
| `verdict` | `target`, `label`, `reply`, `answer_ids` | The classifier's label for one output (`answerable`, `already_given`, `unanswerable`, `no_request`) and the fixed reply. `answer_ids` contains only the ids of card items the classifier selected, empty when none were selected. These ids remain recorded when a cutoff leaves `reply` null. Older records may omit the field; omission is preserved on read and rebuild. At most one per output, never before the output reached the user. |
| `unjudged` | `target`, `reason` | An output delivered so close to a cutoff that the runner recorded no verdict on it; `reason` is `cutoff`. Never together with a verdict on the same output. |
| `wait` | `target`, `replies_to`, `t_end`, `charged_ms`, `ended`, `causes` | A user wait (D-035): it starts at the hand-back, the closing output's delivery or, when no output closed it, the hand-back instant the driver reports; there is one per hand-back, several pending requests sharing it (`replies_to` then names each); its charge is the scheduled return delay cut to the budgets, so `charged_ms` can be less than the interval between its stamps when the classifier overran (`overrun_ms`, audit only). The time charged, whether it ended in a `reply`, a `budget_cutoff`, at the `episode_end` (still open then) or is `open`, and everything that was due at its hand-back (`causes`, a non-empty sorted list, each once): `verdict_reply` (the policy's fixed reply), `fixed_message` (a task message, including one that replaced the policy's reply, whose verdict then has a null reply), `reopen`, `reset`, `refusal` (the fixed refusal of a secret request). A wait cut off before its message was sent carries the same fields. An older runner's single `cause` is read as a one-item `causes`, accepted only for an episode with no runner record. `target` is the output that closed the hand-back, or null. `replies_to` lists every output whose verdict reply the wait's message carried, by the same references as a verdict (each a user-channel output delivered by the wait's end: a second request may arrive while the first is pending, and the one message at the wait's end answers both), and is empty for a wait caused only by a fixed message, a reopen, the reset or a refusal. A charged wait has a verdict on every output it replies to and an end. A wait whose `causes` include `verdict_reply` needs a target or a non-empty `replies_to`. It never ends before it starts or charges a negative time. |
| `planted` | `name`, `phase` | `fired`, `reached`, `not_reached`. |
| `reopen` | `name`, `target` | The task reopened; sets its target's kind to `reopen`. |
| `probe` | `probe` | Where a probe copy was taken. No content. |
| `controlled_reset` | `target` | Declares the segment `reset`; sets its target's kind to `reset`. |
| `notice_delivered` | `target`, `mark` | D-046, optional: when the agent received a reset notice steered into the running turn (`target`, a `reset_notice` message, dated to agree), and the runner's watcher mark at the delivery. Full trajectory only, never in the view. The bundle maker reads the mark's batch position (the batch it closed and any straddling run, as for a correction) on a second reading of the watcher logs that keeps that mark, and puts it in the run manifest's `notices` (`seq`, `target`, `mark` label, `batch`, `straddled`, `straddled_last`; null with a note when the change log lacks the mark). `changes.jsonl` never holds a notice mark, so none of it is a Steering input. |
| `correction_delivered` | `target`, `mark` | When the agent received a correction, and the runner's watcher mark at the delivery (D-025; its label here, its neutral number in the view). A message is dated to agree. When the correction went as the answer to a pending question, the answer and the resumed turn's start both take the runner's time, clamped to be no earlier than the question and no later than the first item of the resumed work. A delivery whose mark is missing (null, or not in the change log) gives a view record with `mark` and `batch` null and a note in the manifest; it does not fail the bundle. |
| `handback` | `target`, `kind` | A hand-back (D-028, D-029): the whole tree idle, or a request pending. `target` is the output that closed it (by item id), any agent's user-channel output delivered at or before it, or none for a silent one; it need not be the main turn's `closed_by`. `kind` is `finished`, `pending`, or `interrupted` for the silent hand-back at a reset grace. In the view, `target` only. |
| `budget_cutoff` | `budget` | `time`, `tokens`, `user_wait`. |

Optional fields stay in the audit record only (`format.RUNNER_AUDIT_FIELDS`,
the run manifest's `runner_audit`, by trajectory seq), never in the
trajectory or the bundle: `overrun_ms` on a `wait` (how late the reply was
when judging took longer than the return delay) and `stopped_utc` on
`episode_end` (when the runner stopped the agent after an elapsed cutoff's
deadline) and `continuation` on `controlled_reset` (`launched` or `unsent`:
a reset whose continuation was unsent has a segment with no dispatch, which
the driver leaves out of the trajectory), and on `controlled_reset` also
`notice` (`mid_turn` or `next_message`: how the reset's notice went) and
`reset_at` (`handback` or `grace`: whether the reset came at the agent's
hand-back or when the grace period ran out), both D-046. (`mark_settled`, which the driver no longer reports since D-037,
is not accepted; a replay drops it from runner round 5 records.) The bundle maker adds
`noticed_utc` when it moves a late `budget_cutoff` to the deadline. They are typed when given. The runner interrupts the tree at a
cutoff, at the end and at a reset; T3's `run_interrupt_request` and
`run_interrupt_result` items for it are driver activity (R13, R13a): the
first is an `interrupt_request` event in the full trajectory, the second is
dropped, and neither ends a turn or makes a hand-back by itself.
| `episode_end` | `reason` | `finished`, `budget_cutoff`, `harness_failure`, `driver_failure`, `runner_abort`. |

References resolve by purpose, after the user channel is known. A verdict or
a wait by `item` names the user-channel event made from that item, and an
item reference always wins. By `run`, it names the last user-channel output
of that raw run delivered at or before the runner event's time. When a run
has none by then, the error names the run. A correction delivery takes an
item or a message only; a correction delivered as the answer to a pending
question is named by the question's item id (or its request id). A reset names the first user message of the run,
message, item or thread. A reopen by message, item or the question's request
id names that message or merged answer; by run (when the runner has no
message id) it names the user message or merged answer of that run with kind
`reopen`, the latest at or before the runner event, else that run's first
user message when it would otherwise be a plain reply. A reopen never
relabels the opening prompt or a reset. An unknown type, an undeclared field
or one of the wrong type, a reference by a kind the event does not take
(`normalise.RUNNER_REFS`), a reference on an event that takes none, a wait for
the policy's reply with no target, or a time before the first event is an
error.

`check_runner_inputs(hints, probe_targets)` runs every one of these checks
on the runner's inputs alone, without a T3 trajectory: runner event types,
fields and their types, references by kind, times, wait causes; user kinds;
answers (text, keyed by request or item id; an empty string is an answer);
excluded intervals; probe target shapes (`about`, `ref` by the kinds that
`about` takes, `event`, `deferred` a boolean or null; a `coincides` key,
from before D-028, is ignored). `normalise` calls it first.
What needs the trajectory (that a reference names something in it) stays in
the normaliser. `Hints.from_dict` reads every Hints field from JSON, and the
`normalise --hints` command uses it.

## Blindness gate

`blindness.scan` (full trajectory), `scan_view` and `scan_doc` (the other
bundle files) sort each hit into one of three kinds. The gate runs over all
five files.

- A structural tell fails: a tell in any key or string outside content.
  Task-declared labels (observation names, identifier kinds, planted event
  and reopen names, schedules, the answer key, control expectations) are
  structure.
- A decoration fails where one must not survive: tool output, file content,
  a subagent prompt, user text (`format.DECORATION_PATHS`; in the view, user
  messages only).
- A content tell is counted and passes. Content is text the agent, the user,
  the task or the runner produced, agent prose in `to_user` included. Counts
  go to the manifest and the bundle report.
- File names that are not valid UTF-8 (the watcher's surrogate escapes,
  `x\udcff`) round-trip: every bundle file is ASCII-escaped JSON, as the
  watcher writes its log. Every JSON Lines reader splits on line feed only.
- Names the agent chose are content, not structure: in `changes.jsonl` the
  paths, link targets, the git head, the ref names (the keys of `refs`)
  and the paths a reading's copy lacked (`absent`). An agent's branch
  `claude/fix` or `fix-bash` never fails the bundle; a key at a content path
  is scanned as content too.
- In `changes.jsonl`, an instruction file, a driver ref, or a mark that is not
  a neutral number fails (D-026 and D-028; this checks the source did its
  part).
- The bundle's text blobs (file contents, each read whole in pieces, a code
  point split between pieces decoded once and a tell across a boundary
  counted once; binary
  ones skipped) are scanned for every tell; hits go to the run manifest's
  `blob_tells` (by blob digest and tell), never a failure: files the agent
  wrote are evidence, like its prose.

A lexical gate cannot see a numerical fingerprint (timing, counts), so the
bundle is also held to a paired test (`tests/trajectory/test_bundle.py`).
Two episodes show the user the same things at the same episode-clock times
over the same task state. They differ in everything out of sight:

- tool discovery, narration and a hidden subagent on one side only;
- a question tool against a question in a final message;
- a native steer against interrupt and restart;
- the Pi profile against Claude Code's, and their native id shapes;
- exit reporting: status 0 against none;
- the watcher logs: two segments joined across a handoff against one log;
  the report edited in place against through a temp file inside one batch
  (an atomic write); different driver-style turn mark labels, correction
  mark labels and end mark labels, an end mark on both sides; four marks
  and four mark readings on one side against five of each on the other, the
  correction mark answered 50 ms after the delivery on one side and 400 ms
  after on the other (both standing for the delivery's instant, D-037), and a
  different reading winning a batch on each side (a mark's, one after the
  end, one inside the probe copy); watcher activity after the end on one
  side;
- hand-back events of different kinds (a pending request on one side, a
  finished turn on the other) for the same hand-back;
- output retention: the whole measurement output against a clipped one with
  the result line surviving whole;
- excluded intervals: a handoff and an export against an export and a probe
  copy stamped at the start of its interval, the probe copy overlapping a
  wait the end cuts and a metric reading;
- different awareness reply text with the same outcome;
- both episode records through the runner-record path, from each side's own
  ids.

A second pair (`test_a_paired_reset_across_two_segments_gives_identical_bundles`)
has one controlled reset across the real watcher's two segments: as two T3
databases with Pi shapes against one database with a fresh-context run and
Claude Code shapes.

All five files are byte-identical, and so are the blob stores' file lists
(paths named by digest; the paired test compares those, not blob contents
beyond their digests). Companion tests show that the bundles differ once
the task capture or the episode clock is off, that a cut inside the result
line changes exactly that one attempt record (truncated, no fingerprint),
and that a temp file living across two batches is the same two changes on
both sides. Three more show the gate failing when an instruction file or a
driver ref (with or without its `ref: ` prefix) reaches `changes.jsonl`.

### Why content tells are kept

Tells inside text the agent wrote stay, counted. Removing them changes what
is scored for one harness and not the other. A placeholder is a tell of its
own, and no pattern list finds every self-description. On the saved run the
full trajectories had three content hits and the bundles none.

## The full trajectory (`trajectory.jsonl`, audit)

### Header and events

```json
{"agents":2,"clock_origin":"dispatch","duration_ms":7606,"events":16,"excluded":[],"format":"cohbench-trajectory","post_run":[],"segments":1,"turns":2,"type":"header","version":1,"workspace":"/work/project"}
```

Times are wall times from the origin, the opening prompt's dispatch (or, without it, its T3 item; `clock_origin` says which): `us`
in microseconds, the precision every decision uses, and `t` the same in
milliseconds (`clock.to_ms`); a run request before the prompt is negative.
A `tool_call` and a `wait` carry `us_end` beside `t_end`. `excluded` lists
the intervals the episode clock leaves out (`{us, us_end, reason}`), and
`post_run` the seqs of the post-run exchange. Every event has `seq`, `t`,
`us`, `type`, `agent` (0 for the main agent, then 1, 2 and so on in the
order subagents start) and `segment`. The file is in time order: the normaliser
gives every event a time, sorts once, assigns segments by position, marks
final messages, resolves runner targets, and only then numbers turns, calls,
questions and subagents in file order. Causal order holds for what the
records give (a runner-dated answer is clamped between its question and the
resumed work's first item); the validator checks it. A
turn starts no later than its first event and ends at T3's completion of its
run, or its last event when T3 recorded none. A child's events come no
earlier than its `subagent_start`, and a `subagent_end` no earlier than the
child's last event.

| Type | Fields | Notes |
|---|---|---|
| `segment_start` | `cause` | `start`; `reset` for a controlled reset the runner declared; `context` for any other new context window. |
| `turn_start`, `turn_end` | `turn`; `end` | `finished`, `waiting`, `interrupted`, `failed`, `open`. |
| `user_message` | `turn`, `kind`, `text` | Kinds below. |
| `assistant_message` | `turn`, `role`, `text`, `user_channel`, `user_text` | `final` or `narration`. |
| `tool_call` | `turn`, `call`, `class`, `tool`, `input`, `output`, `status`, `t_end`, `us_end`, `user_channel`, `user_text`, `question`, `subagent` | At the call's start; `t_end` and `us_end` at its end. `output` is `{text, available, truncated, clipped, last_line_whole, exit_code}`: `clipped` names the edges the harness or driver cut (`none`, `head`, `tail`, `middle`, `both`), from the notice it removed (Pi's keeps the last lines, so its cut is the head; Claude Code's and the driver's transport cut are the tail; an unknown shape is `middle`). |
| `question` | `turn`, `question`, `kind`, `call`, `user_channel`, `user_text`, `prompts`, `secret` | A pending question (non-empty `prompts`) or secret request (`secret`). |
| `answer` | `question`, `status`, `text`, `missing`, `kind` | Only an answered question has text, and a refused secret request may carry the user's refusal words; a secret's value never appears. `missing` marks an answer whose text could not be recovered, written only by audit-only normalisation (R19). `kind` is `reply`, `correction` or `reopen`. |
| `subagent_start`, `subagent_end` | `turn`, `subagent`, `call`, `title`, `prompt`; `subagent`, `status`, `result` | In the parent's stream. |
| `compaction` | `turn`, `status` | A native compaction the driver exposed, and whether it succeeded (`ok`, `error`, `cancelled`, `open`). |
| `interrupt_request` | `turn` | The driver's interrupt request on a run (R13a); `turn` null when its agent had no open turn then. |
| `error`, `unknown` | `turn`, `class`; `turn`, `item_type` | An `unknown` fails validation unless allowed. It sits just after its run's nearest event and its `turn` is not checked against the open turn (R16). |
| runner events | as above | |

User message kinds: `opening_prompt` (agent 0's first, once), `reply`,
`correction` (a steer, or the message that restarted an interrupted turn),
`reset` (the first message after a declared controlled reset), `reopen` (a
runner hint), `reset_notice` (a reset's notice steered into the running
turn, D-046; dated at delivery like a correction, and a restart with it
continues the interrupted turn), `from_agent` (written by an agent: a subagent's task
prompt), and `post_run` (the runner's awareness question after the end,
named in `Hints.user_kinds`; never in a scorer input). The runner's `Hints.user_kinds` overrides any kind; it is keyed by
T3 message id, or by a question's request id for the answer to a pending
question, which then counts as a `reply`, `correction` or `reopen` (the
answer event's `kind`; D-023 merges everything due at a hand-back into one
message). A fresh-context send the runner did not declare is a `reply` in a
`context` segment.

A turn is one T3 run, except that a blocking question ends it waiting and the
answer opens the next, and a correction delivered by interrupt and restart
continues the interrupted turn. An assistant message is `final` when it is the
last assistant message of a finished turn and no tool call, question or
subagent start follows it in the turn; a waiting turn has no final message.
User-channel events (agent 0's final message, a successful `send_to_user` with
its result's `sentMessage`, every question) carry `user_channel: true` and
the text in `user_text`.

### Tool calls

| Neutral tool | Class | Pi 1.6.0 | Claude Code 2.1.330 (unverified) |
|---|---|---|---|
| `shell` | shell | `bash` (observed) | `Bash` |
| `file_read` | file_read | `read` (observed) | `Read` |
| `file_write` | file_write | `write` (observed) | `Write` |
| `file_edit` | file_edit | `edit` | `Edit`, `MultiEdit`, `NotebookEdit` |
| `search` | search | `grep`, `find`, `ls` | `Glob`, `Grep` |
| `web_fetch`, `web_search` | web | none | `WebFetch`, `WebSearch` |
| `subagent` | subagent | T3's `delegate_task` (observed) | `Agent`, `Task`, T3's `delegate_task` |
| `ask_user` | question | the driver proof's diagnostic `ask_user` (observed) | `AskUserQuestion` |
| `request_secret`, `send_to_user` | question, message | T3's (observed) | T3's |
| `tool_discovery` | other | `tool_search` (observed) | `ToolSearch` |
| `harness_other` | other | none | 16 other built-ins |
| any other T3 tool, unprefixed | other | `orchestrator_capabilities` (observed) | the same names |

The stored request bodies list 26 Claude Code built-ins: 10 map to shared
names and 16 to `harness_other`. The profile also lists `Glob`, `Grep`,
`MultiEdit`, `Task`, `TodoWrite`, `BashOutput` and `KillShell` from Claude
Code's documentation; none of the seven is in the bodies. `input` has a
fixed set of fields per tool (`format.INPUT_FIELDS`), `output` is `{text,
available, truncated, clipped, last_line_whole, exit_code}`, and whether a call went to the harness or
the driver is in the manifest only.

## Transformation rules

Each rule says what it does, what it loses, and how it was checked. Rule
numbers match `tests/trajectory/test_transformations.py`.

**R1. Native ids become local integers**, assigned in file order. The
manifest maps each back (`events`, `calls` with native call id, tool name and
origin, `turns` with their runs, `agents`, `segments`). Lost: every native
id. Observed.

**R2. Times are wall microseconds from the origin, the opening prompt's
dispatch** (D-020; `us`, with `t` in milliseconds), from T3's `startedAt` (and
`completedAt` for `t_end`, `turn_end`, `subagent_end`) and the runner's own
times. The bundle converts them to the episode clock once, when it writes a
record, and the view dates user-channel output at delivery (above). Lost:
absolute time (in the manifest as `episode_start_utc`). Observed.

**R3. Harness tool names become neutral names.** T3's item type decides
first, then the title or tool name. A command whose title is not the
profile's shell tool, or a file change whose title is not one of its file
tools, is an `unknown` item, so a wrong profile fails. Lost: the native name.
Observed for `bash`, `write`, `read`, `tool_search`, `ask_user`.

**R4. T3's tool prefixes are removed** (`mcp__t3-code__`, `mcp__t3_code__`),
and `delegate_task` becomes the neutral `subagent` call. A prefixed name not
on the format's list is `unknown`. Lost: the prefix. Observed for one prefix,
tested for both.

**R5. Native arguments map to neutral fields; the rest are withheld** to
the manifest only (Pi's `timeout`, Claude Code's `description`,
`delegate_task`'s `target`, `mode`, `role`, `timeoutMs`). The event does not
list them, since argument names tell harnesses apart. Lost: those
arguments, including the model an agent asked for a child. Observed on Pi.

**R6. Workspace paths in tool inputs are relative to the root** and
normalised (`/work/project/./out/a.txt` gives `out/a.txt`; the root gives
`.`). Paths outside the workspace, and paths in free text, stay as written.
Lost: whether the agent wrote the path absolute. Observed and tested.

**R7. Pi's truncation notice becomes `truncated: true`.** The notice
(`\n\n[Showing lines 1957-3001 of 3001 (50.0KB limit). Full output:
/tmp/pi-bash-<id>.log]`) and `{"by": "harness", "lines_omitted": 1956, "edge":
"head"}` go to the manifest's `decorations`; `output.clipped` keeps the edge
(`head` here: lines 1957 to 3001 of 3001 are the last ones). Lost: line
count, limit, spill path.
Observed on two outputs.

**R8. Pi's `(no output)` becomes empty text.** Lost: a command whose real
output was exactly that. Observed twice.

**R9. One final newline is removed from shell output**, since Pi's truncated
output has none and its full output has one. A test shows the registry finds
the same identifiers either way. Lost: whether the output ended with a
newline. Observed.

**R10. T3's transport cut becomes a flag.** T3 cuts file changes at 32 KiB
before storing them and `getTurnItem` cuts text fields at 256 KiB, with
`\n… output truncated for transport`. The suffix goes to the manifest's
`decorations`, and `content_truncated` or `truncated` is set. The bytes T3
cut (1,267 of the 34,000-byte write on the saved run) are in neither T3's
records nor the manifest. They are in the harness session, which the
cross-check reads. In a measured run they would also be in the watcher's
content store under the file's hash in the change log, where scorers read
file contents. No watcher ran in the saved capture, so that part is not
observed. The cut itself is observed.

**R11. T3's sender note and role prefix are removed** (`[T3 Code: ...]` from
user text; `Act as the <role> sub-agent for this task.` from a subagent's
prompt and first message). Lost: the T3 role name (in the manifest).
Observed for the role prefix; the note's rule is tested.

**R12. Driver tool outputs are withheld; their meaning is kept**:
`send_to_user`'s sent text, `request_secret`'s status and file paths (never
a secret value), `delegate_task`'s subagent link. Lost: anything else T3 told
the agent through its tools. Observed.

**R13. T3's own records are dropped and listed** (workspace preparation,
checkpoints; `run_interrupt_result`, T3's record that an interrupt took
effect, which the run's end already records; `notification`, T3's notice
to a parent of a delegated task's outcome, which the subagent's end already
records). Lost: T3's changed-file lists, which the watcher records, and the
notice's summary text. Observed; the interrupt and notification records in
one driver integration capture (2026-11-22, an interrupt of a tree whose
child had a pending question, `pi-interrupt-child-question.json`).

**R13a. The driver's interrupt request is an `interrupt_request` event**
(T3 item type `run_interrupt_request`), at its own time, in the full
trajectory only. Its `turn` is the one its agent had open at that place in
the file, or null: in the capture the request came while the child waited
on its question, between the question and the cancelled answer, when the
child had no open turn. Lost: T3's message text ("Stopped by user").
Observed once.

**R14. Messages the harness made up become `error` events** (Claude Code's
`<synthetic>` refusal), text withheld. Observed on Claude Code's refused
turns.

**R15. T3 error items become `error` events**, class kept, message withheld.
Observed on Claude Code's refused turns.

**R16. Anything unmapped is an `unknown` event**, the whole item in the
manifest; an unknown status raises. An `unknown` event takes the place of
its run's nearest event in time (just after it), and is exempt from turn
membership, so an item no mapping covers can never break the ordering or
turn checks; it still fails validation unless unknown items are allowed.
Tested, on the interrupt capture with its request's type renamed.

**R16a. A queued run starts its turn when the previous run of its thread
completed**, never earlier: T3 dates a run's request when the message was
accepted, and a message queued while the previous run was going (a refusal
queued after a declined secret request, the driver's route when steering is
not possible) reaches the agent only after that run. Observed in the
runner's secret-refusal episode with the queued route
(`tests/trajectory/fixtures/queued-refusal/`).

**R17. User message kinds** as above. Lost: T3's `inputIntent`,
`freshContext`, `createdBy`. Observed for `opening_prompt`, `reply`,
`correction`, `from_agent`; `reset` and `reopen` tested.

**R18. Final and narration** as above. Lost: T3's `streaming` flag. Observed,
including the waiting turn, whose narration ("I'll ask you directly.") stays
narration.

**R19. Questions keep everything the user is shown**: header, text, options
with descriptions, multi-select (rendered with `(choose one or more)`), and a
secret's label, reason, field labels and placeholders. The answer text is the
runner's (`Hints.answers`) when given, else the question tool's output from
T3 or the harness session; T3 keeps none of its own. An answered question
with no answer text from either is an error: a missing answer is not an
empty string. `Hints.audit_only` normalises such an old capture, marking the
answer `missing`, and `bundle.make` refuses that trajectory. Observed for one
question and one secret request; the saved diagnostic snapshot without its
session is the error case.

**R20. Segments.** A new segment starts at a run with `freshContext` and at
each new source. The cause is `reset` only when the runner declared it (by
run, message, item or thread), otherwise `context`. Segments are assigned by
position after sorting, so an async child that finishes after a boundary is
in the new segment. Lost: the mechanism (in the manifest's `segments`).
Observed undeclared; declared resets and the async child tested.

**R21. A correction is dated at delivery, in one shape for both steering
mechanisms.** The sources, in order: the runner's `correction_delivered` (by
message or item), T3's completion of a steer item (T3 completes it when the
harness takes it), the harness session's record, the restarted run's start,
and otherwise the queue time, recorded as `unknown` in the manifest. On the
saved run T3 queued the steer at 21:14:37.206 and completed it at
21:14:43.494. Pi's own message timestamp is 21:14:37.215, and its session
entry, when the steer entered the conversation, 21:14:43.490. The normaliser
uses T3's 43.494, also without the session. With interrupt and restart the
interrupted turn continues in the restarted run. The manifest's `steering`
records the mechanism, queue and delivery times, source, and the interrupted
run. Lost: the queue time and the mechanism. Observed for the native steer;
the restart layout is tested against it.

A reset notice (D-046) is steered into the running turn by any of the
three routes a correction takes: native steering, interrupt and restart, or
queued behind the running turn. It is dated at delivery by the same sources
as a correction, the runner's `notice_delivered` first. Its user kind is
`reset_notice`, from `Hints.user_kinds`: never a correction, so the view has
no `correction_delivered` for it and the Steering share sees only real
corrections. A notice sent with the next message keeps that message's
kind. The agent's notes follow. The notes turn ends at its next hand-back,
or by the runner's interrupt at the grace period (an `interrupted` turn
end and a silent hand-back). Then the runner performs the reset: the reset's planted
event fires at the continuation's send and `controlled_reset` follows at
the same instant. Outputs between the notice and the reset are judged and
get no wait or reply. The notes hand-back, when there is one, is an
ordinary hand-back: with `answers_after` naming the reset, it is not an
Explanation answer. The manifest's `steering` lists the notice's delivery
with `kind` `reset_notice`. Tested for each route and both endings of the
notes turn (`test_a_reset_notice_is_delivered_mid_turn_like_a_correction`),
and with a notice and a correction in one turn, in either order
(`test_a_notice_and_a_correction_give_one_correction_boundary`). Live scripted
integration tests cover a mid-turn notice followed by a hand-back reset and
a grace reset.

**R22. Subagents nest under a local id**; driver delegation and a harness's
own subagent tool are the same `subagent` call; concurrent children keep
their real times. Lost: T3's child ids, the child's model, `nativeTaskRef`.
Observed for one child; two concurrent children and a background child
tested.

**R23. Omitted outputs and file contents are filled** from the HTTP
transcript (every field the snapshot lacks), else from the harness session by
native call id: a command's or tool's result, and a file write's content from
the session's write call. The manifest records each source. Observed: the
steering snapshot with Pi's session alone gives `stopped.txt` its content
(`halted`), as does the transcript.

**R24. A file change has no output** (T3 keeps no result message). Observed.

**R25. Claude Code's decorations (unverified)**: `<system-reminder>` blocks,
`cat -n` numbers, `... [N lines truncated] ...`, `<persisted-output>`,
`<system>Tool ran without output or errors</system>`, `AskUserQuestion`'s
answer shape. Tested only against strings written for the tests.

**R26. Non-text output parts are withheld with a note.** Tested with a
constructed image block.

**R27. A native compaction becomes a `compaction` event** with its outcome
(the root README section 3: "any native compaction exposed by the driver is
recorded"). T3 records it as an item of type `compaction` with a status, the
harness in `driver`, an optional summary written by the harness, and token
counts before and after. The status is kept; the harness name, the summary
and the token counts go to the manifest. The event is in the full trajectory
and the manifest only, not in the scorer view. It is not user-channel, no
The root README section 6 score uses it, and a harness that compacts on its own
would otherwise show a structural difference. Mapped from T3's source
(`packages/contracts/src/orchestrationV2.ts` and
`packages/provider-pi/src/server/adapter.ts`, read-only, at the copy the
driver unit used). No driver export with a compaction item was saved under
`runs/`, so the shape is not observed in a capture; tested with a
constructed item. T3 treats a user message of exactly `/compact` as a
compaction command; a runner that sends one shows it in the view as a user
message, so a scored run should not send it.

## Identifier rules

Shared by `identifiers.json` and the audit registry.

- **Known to the user** when the user writes it: a user message of a
  user-written kind, or an answered question's answer, whichever agent asked.
  An agent's mention never makes it known.
- **A path is known only as far as the user wrote it.** "Please use
  report.txt" makes `report.txt` known, not `internal/report.txt`.
- **A path counts only from the time it exists**: its first evidence, from
  the change log or a text. A string the user wrote before a file of that
  name appeared makes the string known, not the file.
- **A bare spelling shared with another identifier counts for that
  identifier.** A known job id and a root file named like it stay apart;
  `./job-7f3a9c` and absolute spellings are path evidence.
- **Bare names in disclosures.** A bare file name the agent sends counts as
  a mention of a longer path when exactly one path with that name existed at
  the time and no identifier is spelled like it. When the name resolves to no
  path, it does not hide an identifier inside it.
- **Extraction precedence** inside a text: task patterns and minted
  identifiers, then paths, then generic tokens. A lower match overlapping a
  higher one is not registered. Path forms and other identifiers are matched
  by separate expressions, so a path never consumes an identifier inside it.
  Matches of equal span all count.
- **Paths**: files the change log saw (not directories, not deletions); path
  inputs of tool calls (audit only); absolute paths under the workspace;
  relative paths with a slash whose last segment has an extension or which
  start with `./`; and bare file names from user text, two or more
  characters before an extension that is not a domain ending (`e.g`, `i.e`,
  `example.com` are not files). All are relative and normalised. Absolute
  paths elsewhere count under the configured roots (default `/tmp/`,
  `/var/tmp/`), checked after normalising.
- **Generic tokens**: runs of letters and digits joined by `-` or `_`, at
  least 8 characters, with a part that mixes letters and digits and is not
  shaped like a word with a version. All-hex parts of six or more always
  qualify. Words without digits never do; numbers only when configured;
  tokens starting with an ISO date never. A token joined from a word that is
  not a hex string and an opaque part (`job-7f3a9c21`) also registers the
  part. A UUID is one token, whatever its groups spell.
- **Occurrences**: longest match first, in the forms written and the
  canonical ones. A match inside a longer one is not another occurrence, but
  a token inside a longer user-written identifier becomes known with it.
  Case: paths and identifiers with non-hex letters are case-sensitive;
  all-hex tokens, all-hex minted ids and case-insensitive patterns are not.
  Identifiers split across lines are not joined.
- **Times**: disclosures at delivery, and never after the episode end;
  change-log records sort before messages at the same time, and baseline
  files before the opening prompt.

A real case shows why this count is only auxiliary. In the 24-turn export the
user's command `printf '%s-%s\n' END T005522` prints `END-T005522`; the user
never typed the hyphenated form, so each of the agent's 24 replies counts as
a private identifier sent to the user.

## The adapter interface

`normalise(segments, profile, hints)` takes the episode's sources in order
(the threads of one T3 database, the main thread, the harness sessions of the
same container) and returns the trajectory and the manifest.

- `t3.from_sqlite` reads T3's SQLite backup read-only, the primary source.
- `from_export` and `from_joined` read the payload-bearing export and a
  handoff's joined file. On the saved run their records equal the database's
  field for field, except that the export's `subagent` items carry
  `progress: null`, which the database copy lacks.
- `from_snapshot` reads an HTTP snapshot, with an optional transcript.
  Only the export and handoff operations of the saved run took SQLite backups.

All code that builds events is shared. The harness profile answers four
questions: which neutral tool a native name is, how its arguments map, how to
clean its output text, and how to read its sessions. Each answer comes from
the closed vocabularies or is cleaned text. `test_identity.py` rewrites saved
Pi records into Claude Code's shapes: provider instance `claudeAgent`,
session uuids, Claude tool names and argument shapes, the other T3 prefix, the
model slug, and Claude's truncation and empty-output forms. It requires
byte-identical trajectories in 11 cases:

- four exports;
- the two-segment handoff;
- three snapshot episodes backed by harness sessions;
- the handoff backed by sessions, whose file writes are cross-checked;
- an edit and a search;
- the native steer rebuilt as an interrupt and a restart, with T3's item
  order reversed inside each run.

On the Claude side the sessions are written as Claude Code session files and
read with the Claude profile's reader. They hold the same user messages at
the same times as Pi's sessions, because the test asks whether one logical
episode gives one output. Claude-shaped records through the Pi profile fail
with `unknown` events, and a changed file write in a Claude session is
reported by the cross-check.

The cross-check compares each thread's items with the session its runs name:
user messages, assistant text, tool inputs and outputs by native id, and file
changes by path and text. It also lists session calls with no T3 item.
Findings go to the manifest and never change the trajectory.

## Coverage on the saved run

Run `2026-11-21T210951-74da66`, Pi only, normalised on 2026-11-22, for every
operation with a saved T3 thread record. Every row validates in the full
trajectory and as a bundle, with no `unknown` event and no structural or
decoration hit in any file. The bundle was built without `episode.json`,
which needs a runner the driver proof did not have. The driver proof ran no
watcher, so every `changes.jsonl` is a header alone. For observations the
coverage run declared two attempt patterns the proof's own commands match:
`^seq -f`, with a capture of its `END-T...` line, and `^sleep 6; echo step`
without one.

| Operation | Source | Trajectory events | Tool calls by class | View events | Identifiers (private sent) | Attempts |
|---|---|---|---|---|---|---|
| startup | HTTP snapshot | 5 | none | 4 | 0 (0) | 0 |
| pending, question | HTTP snapshot, session | 12 | question 1, file_write 1 | 8 | 1 (0) | 0 |
| pending, secret | HTTP snapshot, session | 14 | other 1, question 1, shell 1 | 8 | 1 (0) | 0 |
| export, 24 turns | SQLite | 143 | shell 24 | 96 | 25 (24) | 24 |
| export, subagent | SQLite | 16 | other 1, subagent 1, shell 1 | 4 | 1 (0) | 0 |
| export, 34,000-byte write | SQLite | 7 | file_write 1 | 4 | 1 (0) | 0 |
| handoff, 2 containers | 2 SQLite backups | 33 | file_write 3, shell 2, file_read 1 | 24 | 8 (0) | 0 |
| reset by fresh context | HTTP snapshot and transcript | 18 | none | 16 | 1 (0) | 0 |
| steering | HTTP snapshot and transcript, session | 12 | shell 3, file_write 1 | 5 | 1 (0) | 3 |
| snapshot, original | HTTP snapshot, session | 27 | shell 2 | 24 | 3 (0) | 0 |
| snapshot, idle copy | HTTP snapshot | 9 | none | 8 | 1 (0) | 0 |

Across the eleven episodes:

- 45 tool calls in the full trajectories: 33 shell, 6 file_write, 1
  file_read, 2 question, 2 other, 1 subagent.
- 201 view events: 50 turn starts, 50 turn ends, 51 user messages and 50
  outputs to the user.
- 43 identifiers, 24 of them private ones sent to the user, all in the
  24-turn export.
- 27 attempts. All 24 export attempts are `complete` with a fingerprint,
  including the 3,000-line output Pi clipped, because the capture found its
  result line in what Pi kept. The 3 steering attempts are `complete`, with
  the hash of the whole output.
- Three content tells in the full trajectories, none in any bundle file.
- One cross-check disagreement: the 34,000-byte write T3 holds cut to 32,768
  bytes (R10).

The messaging operation saved no thread record, only its `send_to_user`
item. The pending operation's first round, with stock Pi, saved none either.

## Open questions

- The real watcher fixture was regenerated on 2026-11-22 with the watcher's
  fifth round (D-036, D-037). Its blob limit is 4,096 bytes, so from the
  batch that creates `big.bin` on, every reading left that file out for
  size, and those batches have `no_metric` `incomplete`.
- Not this unit's to fix, recorded as limits: the driver passes the
  normaliser only excluded intervals that have closed, so at export the
  export's own interval is absent from the trajectory's clock, and records
  made during the export (the watcher's last readings, for one) are dated
  as if the clock kept running. No bundle record depends on it now that a
  kept reading carries no time. The runner writes `control.passed: null`
  for every control; the family scorer fills it. `task_sha256` is the
task's digest and the same for every configuration that ran the
instance, so the gate treats it as structure and it can carry no harness
tell; a lexical gate cannot check what a digest was taken over.

- The watcher. The reader is tested on real output from one scripted run
  on Docker Desktop, regenerated on 2026-11-22 after the watcher's D-026
  change, and on one measured episode under the runner
  (`runs/runner-integration/2026-11-22T022707/rdemo-1`, rebuilt from its raw
  record in `test_bundle.py`). Not yet seen in real output: a queue overflow
  (`recovered`), an unwatched directory, a forced batch, an unsettled batch,
  and a `content_unknown` write. Readings isolated as a separate user
  (`isolation: users`) are seen in the current demo runs; the fixture's run
  `metric_isolation: none`.
- Unsettled batches are kept and flagged, and no share treats them
  differently. Whether a Steering share should leave out a change whose
  boundary is unsettled is a scoring decision, not made here.
- T3 item types the mapping covers that no saved capture holds (checked on
  2026-11-22 against every T3 database under `runs/`, the driver proof's
  snapshots and the interrupt capture): `mcp_tool_call`, `file_read`,
  `file_search`, `web_search` and `compaction`. Seen: `user_message`,
  `assistant_message`, `command_execution`, `dynamic_tool`, `file_change`,
  `subagent`, `user_input_request`, `secret_request`, `checkpoint`, `error`,
  and, once each, `run_interrupt_request`, `run_interrupt_result` and
  `notification` (outcome `cancelled` only).
- A compaction item's shape is taken from T3's source, not a capture.
- Claude Code is unverified until a Claude Code turn succeeds through T3
  (D-002).
- The answer key's reserved keys are validated by type; other payload keys,
  the control expectations and the planted-event schedules (beyond
  `required`) are validated as objects only.
- Not removable, and recorded as such by the reviewers:
  - agent-written text that names a harness;
  - the rendered style of a tool question against a prose question;
  - timing, turn and attempt counts;
  - a cancelled blocking question followed by more work, which gives a
    `turn_start` with no `user_message`;
  - a subagent's answered question, which gives a `reply` inside the main
    agent's open turn.
- Answer text: T3 keeps none, so the runner passes every answer it sent.
- Attempt statuses keep `truncated` apart from `unavailable` (decided
  2026-11-22): loop scoring needs only that two attempts are comparable,
  and both give a null fingerprint, so the distinction is recorded and
  scored the same.
- Kept on purpose (decided 2026-11-22), each either scored evidence or the
  same for every harness by construction of the environment:
  - the `secret` flags: the Routing secret-request rules score them;
  - turn rows, synthetic or cancelled turns included: Routing reads when
    control went back to the user, and the rows come from the runner's own
    sends and T3's runs, not from harness internals;
  - mark timestamps: the runner takes its marks at its own hand-backs, the
    same instants for every harness given the same outputs;
  - Git state (refs and commit ids): the commits are the agent's work in a
    repository the environment creates the same for every harness;
  - the episode id: the runner's opaque id, chosen independently of the
    harness.
- The bundle's `no_metric.reason` is a closed set (`none_taken`, `failed`,
  `incomplete`); the detail that described driver activity (skipped by a
  later reading, not recorded) is in the audit record. Ancillary metric
  values are the task's metric command's own output.
- Hand-backs: only a record marked `handbacks: inferred` (a capture from
  before the runner recorded them) falls back to the main agent's finished
  turns; a current record with no hand-back has no answers.
  Records from before `replies_to`, `causes` and the integer seed existed
  (rdemo-1 among them) need all three supplied to make a bundle;
  `replay.rebuild` supplies `replies_to` from the wait's own target and
  cause, replays the single cause with `Hints.legacy_waits`, reads a seed
  `s17` as 17, and passes the driver's recorded dispatch time
  (`export/manifest.json` `episode_start_utc`). Four saved episodes are
  replayed in the tests: `022707/rdemo-1` and `021448/rdemo-1` (before
  D-035), `042933/rdemo-1` and the elapsed cutoff `043203-cutoff/rcutoff-1`
  (the current contract).


Correction delivery timestamps are authoritative recorded instants. The runner
and watcher name the same instant for a steered correction or one carried by
the next message, including after a reset. T3 may stamp its message later
while dispatch completes. Normalization retains the runner's delivery instant
instead of moving it to that later stamp and separating it from its mark.
For a next-message delivery this instant opens the new turn. If the preceding
run's completion stamp is later but all its work ended by delivery, its end
is reconciled to delivery and noted in the audit manifest. Recorded work
extending past delivery is rejected as irreconcilable clocks.

The reset continuation goes alone. A correction due before or during the
reset follows as a separate steer or next message, with its own delivery
and mark. Only the continuation carries `controlled_reset`.

A reset at the grace records a silent `handback` with a null target at the
grace instant. In the full trajectory its kind is `interrupted`; the scorer
view omits kind as for other hand-backs. The reset wait belongs to that
hand-back and due probes run there. This does not count as a finished turn.

For deterministic test replay, `RunConfig.scorer_id` accepts an optional
`e` plus 16 lower-case hex digits. It supplies `episode`; when omitted the
runner still generates a random id. This option does not encode task or
configuration information into scorer records.
