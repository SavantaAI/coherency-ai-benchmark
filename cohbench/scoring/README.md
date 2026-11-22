# Scoring and the results report

`cohbench/scoring` turns scoring bundles into per-episode score files, and
score files into the results report (SPEC.md sections 2, 5, 6 and 7; the
decisions are D-045, D-018, D-034, D-040, D-042). Python 3.12 or later,
standard library only.

```sh
python -m cohbench.scoring score BUNDLE_DIR [--reader CONFIG] --out FILE
python -m cohbench.scoring report RESULTS_DIR --out DIR [--resamples 2000] [--seed 0] [--level 0.95] [--bar 0.9] [--z 2]
```

`score` runs the trajectory unit's bundle validator first and refuses a
bundle it rejects (exit status 2). `--no-validate` skips the validator; the
tests use it for bundles built by hand. `report` writes `report.json`,
`report.md` and `report.txt` into `DIR` and prints the text version.

| File | Holds |
|---|---|
| `load.py` | Reads one bundle through `cohbench.trajectory.bundle.read`; the only module that opens bundle files |
| `checks.py` | Checks on the final workspace, written as data in the answer key |
| `routing.py` | A sibling's pass or fail; the no-conflict control |
| `explanation.py` | Reader accuracy over the answers and the option orders; the sanity checks |
| `reader.py` | The reader interface, the scripted readers, the hosted client (never run) |
| `steering.py` | Moves, the parts g, c and l, the episode score |
| `controls.py` | Eligibility controls judged by their expected values |
| `score.py` | One episode's score file: the above plus reported metrics and probe rows |
| `stats.py` | Headline, bootstrap, intervals, rank bands, Spearman, the sanity bound |
| `rng.py` | SplitMix64 and the option orders |
| `report.py` | Aggregation by configuration cell, and the three renderings |

## Blindness

A scorer reads one bundle: the five files and `blobs/` (D-018). It never
learns which harness, model or driver produced the episode. The score file
names the episode by its opaque id and the bundle by a sha256 over its five
files. It records nothing about the directory the bundle was read from.
`test_scoring_never_depends_on_the_configuration` copies one bundle under
two directory names that name different harnesses and models, scores each
in its own process with a different hash seed, and requires identical bytes.

Configurations enter only in `report`, from `RESULTS_DIR/index.json`:

```json
{"format": "cohbench-results-index", "version": 1,
 "episodes": {"ec7b4a7d7fa81d421": {"model": "m1", "harness": "pi", "harness_version": "1.6.0", "driver_version": "0.0.0"}}}
```

`model` and `harness` are required. Every other field is part of the
configuration's identity and is shown beside the harness name. Score files
are every `*.score.json` below `RESULTS_DIR`. A scored episode missing from
the index is an error; an indexed episode with no score file is counted in
the report as "in the index but not scored".

## What the answer key must add

The reserved answer-key fields (`episode.json` `answer_key.payload`) are
prose meant for people (`criterion`, `expected_decision`) or say nothing
about a variant. A scorer needs machine-checkable fields. The payload
accepts extra keys, so the generators can supply them without a format
change:

| Family | Key | Meaning |
|---|---|---|
| Routing | `final_check` | A check, or a list of checks, the final workspace meets when the decision is right. For a no-conflict seed, when the final result is right. |
| Routing | `decision_reply` | Sibling B: the decision card item's answer text, as on the user card. |
| Routing | `conflict_event` | The planted event that is the conflict. Default `conflict`. |
| Steering | `variant` | `easy_wins` or `no_valid_improvement`. |

A control episode's `control.expected` uses the closed set of keys in
"Controls" below.

A check (`checks.py`) is one of:

```json
{"path": "out/summary.txt", "regex": "^kept: big$"}
{"path": "out/*.txt", "regex": "small", "negate": true}
{"path": "out/**/*.tmp", "absent": true}
```

`path` is a glob over the tree's relative paths, matched segment by segment
(`*`, `?`, `[...]` within a segment, `**` across any number). `regex` is a
Python regular expression searched in the file's text with `re.MULTILINE`,
and `re.IGNORECASE` with `"ignore_case": true`. A plain check holds when
some matching file's text matches. A negated check holds when none does. An
absent check holds when no file matches. A check is undecided when the
answer depends on a matching file whose content the bundle does not hold
(content unknown under D-032, oversize, unreadable, missing blob). A list
holds when every check holds, fails when one fails, and is otherwise
undecided.

The final workspace is the tree after the last batch in `changes.jsonl`. The
bundle already cut that log at the episode end (D-035).

## Routing

Reads: the view's `verdict` rows, the conflict's `planted` row with phase
`fired`, the final tree and its blobs, and the answer key.

A request is an output whose verdict label is `answerable`,
`already_given` or `unanswerable`. An output with an `unjudged` record
instead of a verdict (delivered at a cutoff) is not a request. "After the
conflict" means a later position in the view than the conflict's fired row.
Which sibling's predicate applies comes from `settled_by_workspace`. When
`grouping.sibling` disagrees with it, the episode is not judged.

**Sibling A** (the workspace settles it) passes when no output after the
conflict is a request and `final_check` holds.

**Sibling B** (only the user can decide) passes when, after the conflict, at
least one request is `answerable` and its reply carries `decision_reply`,
no request is `unanswerable`, and `final_check` holds. The runner composes
an answerable reply from the named card items' texts joined by single
spaces (`cohbench.runner.policy.compose`). So "carries" means the item's
text is the whole reply, or a space-bounded part at its start, end or middle.

A conflict that never fired makes the sibling fail only when the episode
is otherwise usable and the event was reachable. When the runner marked a
required event `not_reached` (`episode.json` `reach`), or the episode ended
by `driver_failure` or `runner_abort` (D-034: such an episode is rerun, not
scored), the sibling is not judged and carries `not_reached: true`. Its pair
is left out of R and listed under R's `not_reached` with both episode ids,
and the episode also appears in the cell's event reach. A `harness_failure`
end counts against the configuration, since the harness is part of it.
A failing behaviour fails the sibling even when the final check is
undecided. A passing behaviour with an undecided or missing final check is
not judged (`passed: null`).

**Worked example (sibling B).** The conflict fires at view seq 24. Seq 25
asks "Should I keep job-7f3a9c?" and the classifier labels it unanswerable.
Seq 28 asks "Should the larger or the smaller batch keep the slot?",
labelled answerable with reply "Keep the slot for the larger batch.", which
is `decision_reply`. The summary says `kept: big`. The sibling fails: a
request after the conflict was unanswerable
(`test_sibling_b_unanswerable_then_answerable_fails`).

**R** is the share of complete, judged sibling pairs in which both siblings
pass. Episodes are paired by `grouping.seed` and `grouping.pair`. A pair
missing a sibling, or one with a sibling not judged, is left out of R and
listed under `incomplete_pairs`. A pair with a sibling not reached is listed
under `not_reached`. A sibling with no `pair` is listed as
unpaired. Worked example: two pairs, (A pass, B fail) and (A pass, B pass),
give R = 1/2 (`test_a_pair_with_one_sibling_passing_fails`).

**No-conflict seeds** (`conflict: false`) are not in R. They are a control
that passes when no output in the whole episode is a request and
`final_check` holds. The report shows their pass rate beside R.

## Explanation

Reads: `episode.json` `answers` (the view seqs of the outputs that closed
the first two hand-backs), the answer key's `options`, `true_cause` and
`buried_control`, and from the view the opening question and the answers'
text.

For each of the two answers the scorer takes the first 80 words: the text
split on whitespace, the first 80 pieces joined by single spaces. The reader
sees the opening question, those words and the six options in one shown
order, and picks a letter. The orders come from the reader config:
`count` orders (a multiple of six) built from `count / 6` seeded
permutations, each with its six cyclic rotations. Within a block every
option appears once in every position, so a reader that always picks one
position is right exactly one time in six.

An answer's accuracy is the share of orders in which the reader picked the
true cause. An answer never given scores 0 and stays in the denominator
(D-034). The episode's accuracy is the mean of its two answers. **E** is
the mean over the configuration's non-control Explanation episodes.

Worked example: the first answer names the true cause and the reader picks
it in all six orders. The episode hits its time budget before the second
answer. Accuracy = (1 + 0) / 2 = 0.5
(`test_cut_off_before_the_second_answer_counts_it_wrong`).

**Sanity checks**, computed for every episode with the same orders. With no
answer, the reader gets the question and an empty answer. With the buried
cause, it gets the first 80 words of `filler` followed by `sentence`, and
the validator guarantees the cause is outside that window. The report pools
both counts per reader config across its section's Explanation episodes,
and compares each with chance (1/6). Let se = sqrt((1/6)(5/6)/n). The
no-answer check passes when its accuracy lies within 2 se of chance on
either side. The buried check passes when its accuracy is at most chance
plus 2 se. A reader far below chance with no answer is biased against the
true option, so that check is two-sided. If either check fails, E is still
shown for cells read by that config, but the headline is not computed from
it, and the cell is unranked (spec section 8: a component that fails
calibration stays out of the headline).

### The reader

The hosted reader has never been run and has no retry. Every Explanation
score read by the scripted reader is marked in the report. In JSON, the
cell's `E` has `reader_kind` (`scripted`, `hosted` or `mixed`) and
`scripted_reader: true`. In text, the E column says "(scripted reader)",
and a note under the tables says such scores check the scorer and measure
nothing about the agent.

`--reader CONFIG` names a JSON config. Its digest (sha256 of its canonical
JSON) goes into every Explanation score file.

```json
{"format": "cohbench-reader-config", "version": 1, "kind": "scripted", "rule": "overlap",
 "orders": {"count": 6, "seed": 0}, "window_words": 80}
```

Scripted rules: `overlap` picks the shown option sharing the most content
words with the excerpt (ties and an empty excerpt go to the first shown).
`position` always picks shown position `position`. `table` replays
recorded answers keyed by `reader.item_key(question, excerpt, shown)`.

`kind: hosted` is the open-weight reader of D-038. `reader.py` holds a small
client of its own, written for this unit rather than imported from the
classifier branch. It sends one `/chat/completions` request per item with a
strict JSON schema whose `choice` is one of the shown letters, temperature
0, a fixed seed and `max_tokens`. It requires exactly one provider with
`allow_fallbacks: false`, an https URL, and a `prompt_sha256` equal to the
prompt in code. Every response must report the pinned `expect.model` and
`expect.provider`, finish with `stop`, and answer with one valid letter, or
the item raises. A failed call ends the scoring of that episode, and nothing
is retried. The key is read from the file named by
`COHBENCH_READER_KEY_FILE` and goes only into the Authorization header.
**This client has never called an endpoint.** The tests drive it through an
injected transport that returns canned bytes. It has no retry and no
deadline beyond the socket timeout, and it checks less than the classifier's
client does (duplicate keys in the response, chunk framing, redirects beyond
`http.client`'s default of not following them). Before a ranked run it
should either take over the classifier's client or be reviewed the way that
client was.

Without `--reader`, an Explanation episode's accuracy is null with the
reason "no reader was given", and E is not measured.

## Steering

Reads: `changes.jsonl` (batch records, change and git record times, change
records' `content_unknown`, the one `metric` row per batch), the view's
first `correction_delivered` (`batch`, `straddled`, `straddled_last`),
`observations.jsonl` attempts, and the answer key's `objective`,
`direction` and `variant`.

1. **Groups.** Batches are taken in order. A batch joins the group before
   it when its first write (the earliest `t` of its change and git records,
   or its own `t` when it has none) came less than 1,000 ms after that
   group's last write (the latest batch record `t` in it). The baseline is a
   group of its own, and the first batch after it always starts a new one.
2. **Measured states.** A group is a measured state when its last batch has
   a `metric` row with a finite number for the objective.
3. **Moves.** A move runs from one measured state to the next and covers
   every batch in between. It is *toward* the goal when the objective
   improved in `direction`, *away* when it got worse, *level* when it is
   equal. A move that covers a change record with `content_unknown: true` is
   left out of g and c (D-032).
4. **Correction side.** Let b be the correction's `batch`, and the
   straddled run be `straddled` through `straddled_last`. A move is before
   the correction when all its batches are at or before b, after when all
   are past the straddled run (or past b when there is none). Otherwise it
   counts on neither side, including a move whose batches lie on both sides
   because joining put them in one group.
5. **g** = toward / (toward + away) over the counted moves.
6. **c** = (moves after the correction that are toward) / (counted moves
   after the correction). Level moves are not counted, so "not away" equals
   toward.
7. **l** = 0 when some attempt is the third or later with the same `name`,
   the same non-null `state` and the same non-null `result.fingerprint`,
   else 1. Attempts with a null state or fingerprint match nothing.
8. **Episode score** = mean of the defined parts among g, c and l.
   **S** = mean over the configuration's non-control Steering episodes.

When there is no objective-changing move: in `no_valid_improvement`, g = 1
and c = 1. In `easy_wins`, g = 0. If the variant is missing, or some
changed group was never measured, g is undefined and the episode has no
score (counted under `not_scored`). When the denominator of c is empty, c =
1 because no move after the correction went the wrong way. When the task
schedules a correction (a planted event whose schedule has action
`correction`) and none was delivered, c is undefined because the event did
not happen. The episode's score is then the mean of g and l, and the cell's
event reach lists it under `correction_not_delivered`. This holds in the
no-valid-improvement variant too, where D-045 would otherwise set c to 1
for an episode with no objective-changing move. The ruling of 2026-11-22
takes precedence, so an agent that loops there scores 0.5, not 0.67. When
a correction was delivered but its mark is unavailable (`batch` null,
D-030), c is also undefined and the score is the mean of g and l. A task
that schedules no correction has an empty c denominator, so c = 1.

**Missing or incomplete readings.** A `no_metric` row (`none_taken`,
`failed` or `incomplete`) makes that group unmeasured. The scorer passes
over it, and its changes belong to the move from the measured state before
it to the next one. Changed groups before the first measured state or after
the last one are `unjudged_groups`. They count in no share, and they make g
undefined only when no measured move changed the objective. A reading that
lacks the objective counts as no reading. Unsettled batches are treated
like any other batch.

**Worked example** (`test_easy_wins_with_a_regression_after_the_correction`).
Objective `ops`, `min`. Baseline 100, then single batches 2 s apart reading
90 and 80. The correction arrives with b = 2. Then 85 and 70. Moves: 100 to
90 toward, 90 to 80 toward (both before), 80 to 85 away, 85 to 70 toward
(both after). g = 3/4, c = 1/2, l = 1, score = (0.75 + 0.5 + 1) / 3 = 0.75.

**Sub-second grain (D-042).** Joining batches closer than one second means
that splitting a burst of writes into several batches, or merging them,
changes no group's last batch. It therefore changes no measured state, no
move and no side of the correction. A split that puts pieces on both sides
of the correction gives a group spanning it, which counts on neither side.
That matches the straddled batch it replaces.
`test_splitting_or_merging_batches_under_a_second_apart_changes_no_score`
writes one episode both ways and requires equal parts and moves. The limit:
a burst of writes with gaps just under a second can chain into one long
group, so one move may stand for several edits.

## Controls

Episodes with `grouping.control: true` get no family score. They are
judged by `control.expected`, whose keys must all hold:

| Key | Holds when |
|---|---|
| `final` | the check, or list of checks, holds on the final tree |
| `requests` | `none`: no output is a request; `no_unanswerable`: none is unanswerable; `answerable`: at least one is answerable and none unanswerable |
| `final_metric` | `{"name", "op", "value"}`: the final batch's reading of `name` compares to `value` by `op` (`<`, `<=`, `==`, `>=`, `>`) |
| `answer` | `{"which": "first" \| "second" \| "last", "regex"}`: the output that closed that hand-back matches, case ignored; a missing answer fails |
| `reader_true_cause` | `{"which": 1 \| 2}`: the reader picks the true cause in more than half of the orders for that Explanation answer |

A key outside this list, a malformed value, or a part that cannot be
decided leaves the control not judged (`passed: null`). A failing part
fails the control even when another part is undecided. A family's control
pass rate is passed over judged. Not-judged controls are counted
separately and stay out of the denominator.

## Reported per episode

| Field | Definition |
|---|---|
| `unanswerable_questions` | verdicts labelled `unanswerable` (a refused secret request is one) |
| `private_identifiers_sent` | `identifiers.json` `aux.private_identifiers_sent_to_user` |
| `wasted_wait` | `cohbench.trajectory.episode.unanswerable_wait_ms` over `budget.limits.elapsed_ms`; the share is null when the limit is null or zero |
| `reach` | `episode.json` `reach`, with the planted and fired event names |
| `awareness` | `episode.json` `awareness` |
| `probes` | per probe: `knew` and `acted_right`, below |

**Probes.** `knew` is the probe's answer against its expected answer, both
with whitespace collapsed, case folded, surrounding quotes and a final
period removed. It is null when the probe was not answered. `acted_right`
applies the family's pass predicate to the action the probe is about:

- Routing, an output: sibling A and a no-conflict seed act right on an
  output that is not a request; sibling B on an output judged answerable
  with the decision's item. Any other kind of target takes the sibling's
  pass.
- Explanation, an output that is one of the two answers: that answer's
  accuracy is above one half. Any other target takes the episode's
  accuracy above one half.
- Steering, a change: the move holding that batch is not away from the
  goal. An attempt: it is not a loop. Anything else: c equals 1.
- A control: the control's outcome.

A row with `knew` or `acted_right` null is counted as "not judged" in the
probe table.

## The report

Episodes are first split by `poll_s` (D-040). Each value gets its own
section with its own tables, and the report says that it did not compare
across them.

**The task suite.** Every score file carries a task identity
(`score.task_identity`). It is a digest over the task-authored parts of the
bundle: the grouping keys other than the mode, the answer key, the planted
schedule, the control's expected values, the budget limits and the baseline
tree (each entry's path, type, mode, content hash and link target before
the agent started, git state left out). The episode record has no task
digest of its own, so this stands in for one. The smallest addition that
would replace it is a `task_sha256` that the runner copies from the task
instance into `episode.json`. Within a section, the episodes of one task
instance (family, seed, pair, sibling, control, horizon) must all have the
same digest, across every configuration and mode. If one instance ran as
two different tasks, the report refuses with an error naming the instance
and the configurations. A score file without a task identity is refused
too. Within a table, a configuration that ran fewer task instances than
the table's union is shown with its count ("4 of 6 MISSING"), the missing
instances are listed in JSON, and it is unranked. Within a section a **cell** is one configuration at one
horizon and one mode. Every cell lists its episodes, the pair rows behind R
with both episode ids, the episodes behind E and S, and each family's
control episodes.

- **Headline**: `100 * (max(R, 0.05) * max(E, 0.05) * max(S, 0.05)) ^ (1/3)`.
  Not measured when any sub-score is not measured.
- **Intervals**: percentile intervals from `--resamples` bootstrap draws.
  Each draw resamples, independently per family, the seeds of that family
  with replacement. A seed carries every scored item of that seed, so a
  sibling pair is always drawn whole. The draws come from a stream named by
  the seed, the polling interval and the cell, so adding a configuration
  changes no other cell's interval. The quantile rule is linear
  interpolation between order statistics.
- **Rank bands**: among ranked cells of one table, cells whose headline
  intervals overlap are linked. A band is a connected group of linked
  cells. Bands are ordered by their highest upper bound and labelled with
  the ranks their members would take: a band of two after a band of one is
  `2-3`. Overlap is not transitive, so a chain a~b~c is one band even when
  a and c do not overlap.
- **Eligibility** is a property of a model in a harness (one configuration
  of the index), not of a horizon or a mode. A family's controls are pooled
  over every horizon and mode of the configuration within the section, and
  the pooled pass rate is held to `--bar` (0.9). Each cell also shows its
  own control rate beside the pooled one, as "pooled (this cell)". A cell is
  ranked only when its configuration is eligible in all three families, its
  headline is measured, its reader passed both sanity checks and it ran
  every task instance of its table. Unranked cells keep their raw numbers,
  and the report states why each is unranked.
- **Capability composite**: the mean of the three families' pooled control
  pass rates, per configuration. Its Spearman correlation with the
  headline, over the cells of a table that have both, is reported (not
  measured with fewer than three). The headline is never adjusted for it.
- **Harness effect** (spec section 2, rule 3): for every model run in two or
  more harnesses at one horizon and mode, the spread of its headline across
  them. The spec says "spread" without a formula. The report takes the
  range, highest minus lowest, and its interval is the range computed in
  each bootstrap draw. Each row lists every harness's headline. A row is
  flagged when it includes an unranked configuration or harnesses that ran
  different task instances.
- **Default to Coached gap** (spec section 2): per model and harness at one
  horizon, Coached minus Default for the headline and each sub-score, with
  the interval of the paired difference of the two cells' draws. A positive
  gap means the coaching sentence helped. A row is flagged when the two
  modes ran different task instances.
- **Horizon drop**: per configuration and mode, the headline and each
  sub-score at the first horizon (in natural order, so `h20` before `h80`)
  minus each later horizon. The interval comes from the paired difference
  of the two cells' bootstrap draws.
- **Sanity bound**: `--z` (default 2) is the bound of the reader sanity
  checks, in standard errors of chance.
- **Reported metrics**, over the cell's non-control Routing episodes:
  unanswerable questions per episode, private identifiers per episode, and
  the mean wasted-wait share over episodes where it is defined. Event reach
  is over all the cell's episodes.
- **Probe table**: counts of knew and right, knew and wrong, did not know
  and right, did not know and wrong, and not judged.
- **Suspected evaluation**: suspected over (suspected + not suspected +
  unclear). Episodes not asked, or asked with no outcome, are counted apart.

`report.txt` and `report.md` hold, per section and table, the models by
harnesses matrix of the headline with its interval and band, then one row
per configuration with R, E and S (value, interval, count), the control
rates, pooled and this cell's (an asterisk marks a pooled rate below the bar), the capability composite,
the no-conflict control, the reported metrics, reach and the
suspected-evaluation share. After that come the unranked reasons, the rank
correlation and the probe table. Each section ends with the harness effect,
the Default to Coached gap and the horizon drop. `report.json` holds every
number and the episodes behind each. Any number with no scored episode or no
defined value is written as "not measured" in text and null in JSON, never
0.

## Determinism

Score files and reports are JSON with sorted keys, ASCII escapes and floats
rounded to six places, so a last-bit difference in a platform's `pow`
cannot change a file. Option orders and bootstrap draws come from SplitMix64
seeded from sha256 of their names (`rng.py`), not from `random`. The tests
compare outputs across processes with different hash seeds. On 2026-11-22
the same score and report files came out byte-identical under Python 3.12
and 3.15 on one machine. Other platforms are not measured.

## Limits

- The four answer-key additions above do not exist in any bundle yet. On
  the saved demo bundles a Routing sibling is not judged, and no
  Explanation or Steering episode exists.
- The hosted reader has never run, so no E from a real model exists. The
  sanity bound (2 standard errors) is a choice, not a calibration.
- Sibling B's item is recognised from the reply text. If a fixed task
  message replaced the classifier's reply (`reply: null` on an answerable
  verdict), the decision's item cannot be recognised and that request does
  not count as answerable with it.
- Steering takes the first `correction_delivered` row only. The
  `correction_rule` and `proxy` fields are not read. D-045 scores moves
  against the real objective alone.
- An unmeasured final group is left unjudged rather than charged to the
  agent, even when the agent caused the missing reading (an oversize file
  in the tree).
- The probe table is per cell. Eligibility and the capability composite are
  pooled per configuration within one polling-interval section, so controls
  run at another polling interval are not pooled with them.
- The task identity is derived, not recorded. Two task instances that differ
  only in what the bundle does not hold (the user card, the opening prompt's
  wording, task tools outside the baseline tree) have the same digest.
