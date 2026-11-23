# Episode-path review round 4

Date: 2026-11-23. Base: `c38cff7600b324d1a18c8ead4181acb5c23b2b05`.
Prompts, pinned configs, development agreement outputs and held-out evidence
are unchanged. No held-out directory was read for this round.

## Rulings and regressions

Runner changes below are in `cohbench/runner/runner.py`. Endpoint cause
typing and cancellation are in `cohbench/user/api.py`; classification and
job lifecycle changes are in `cohbench/user/classifier.py` and
`cohbench/user/job.py`. The comparison contract is in
`cohbench/runner/README.md`.

| Ruling | Change                                                                                                                                                                                 | Covering test in `tests/runner/test_classifier_review4.py` unless stated otherwise                                                                                                                                                                                                                                                            |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R4-E1  | `Runner._observe_output` inserts, counts and retains every observed output before judgement. Closing items and probe targets include unjudged outputs.                                 | `test_r4_e1_missing_verdict_preserves_closing_answer_and_probe`                                                                                                                                                                                                                                                                               |
| R4-E3  | Observation and event baselines follow output delivery order. Historical contexts count only outputs delivered by that instant.                                                        | `test_r4_e3_observation_and_correction_baseline_follow_stream`, both verdict variants                                                                                                                                                                                                                                                         |
| R4-E2  | One absolute hand-back cutoff survives the reset drain. Grace judging and dispatch share the grace instant. Each asynchronous job also retains its own start-based limit.              | `test_r4_e2_reset_drain_cannot_open_a_new_window`, both reset variants; `test_r4_own_start_limit_and_handback_cutoff_both_apply`                                                                                                                                                                                                              |
| R4-S1  | `DeadlineExceeded` is a typed cause. Request timeout or other endpoint failure with time left aborts the episode. Job cancellation stops further transport submission.                 | `tests/user/test_hosted_review4.py`: `test_r4_s1_failure_with_time_left_is_not_an_unjudged_output`, `test_r4_s1_attempt_timeout_carries_shared_deadline_cause`, `test_r4_s1_typed_deadline_is_an_unjudged_output`, `test_r4_job_tightened_deadline_cancels_and_discards_late_result`, `test_r4_job_retains_an_endpoint_failure_before_cutoff` |
| R4-V1  | `text` preserves user words and `wire_text` records the submitted option value. Refusal steers enter the transcript and deliver card ids only when the driver reports actual delivery. | `test_r4_v1_native_option_keeps_user_words_and_wire_value`, `test_r4_v1_refusal_steer_waits_for_observed_delivery`, delivered and never-taken variants                                                                                                                                                                                        |
| R4-S2  | Classifier rows separate snapshot `as_of` from `output_at`. `trajectory.replay.rebuild` uses output time, with historical-row fallbacks.                                               | `test_r4_s2_rebuild_uses_output_instant_not_snapshot`                                                                                                                                                                                                                                                                                         |
| R4-S3  | README lists comparison metadata by name. The reversed-completion test compares complete logical records. Every scripted episode is replayed from the same initial fixture state.      | `tests/runner/test_classifier_review3.py::test_r3_b1_completion_order_does_not_change_records`; `tests/runner/policy_replay.py`                                                                                                                                                                                                               |

The driver clarified that each output needs its arrival snapshot and a job
started off the main loop. `Classifier.start_judgement` and `user/job.py`
implement that path. Completion instants join the runner's replay timeline;
later completed results wait for earlier outputs. Tests:
`test_r4_arrival_snapshot_does_not_wait_for_handback` and
`test_r4_verdict_effects_wait_for_completion_and_output_order`.

The driver withdrew the proposal for a separate mid-run reply deadline after
checking `main`. Replies still wait for a hand-back, scheduled from its
closing output. The trajectory wait contract did not change.

Additional differences found during this work:

- The fake exporter used an older check requiring a verdict on every charged
  wait's closing target. It now checks `replies_to`, like the trajectory
  validator. Fixed-message waits can have an unjudged closing output.
- Evaluating every trigger at late verdict completion could fire an
  elapsed-only event after a final hand-back. Completion now evaluates only
  verdict-dependent triggers. The replay property includes
  `test_a_live_threshold_reads_the_origin_clock[0.0006]`. A dedicated
  compound-trigger regression also checks that an elapsed branch of `any`
  is not fired merely because a verdict completed:
  `test_r4_completion_does_not_fire_an_elapsed_branch_after_handback`.
- Two queued endpoint failures could skip the next output before observation.
  `test_r4_endpoint_failure_does_not_remove_following_observations` failed
  before observation moved ahead of failure handling.
- A failed end-coverage mark could bypass pending-job settlement. Ending
  now tightens every remaining job and records each observed output even
  when final coverage fails. Covered by
  `test_r4_failed_end_coverage_still_closes_pending_jobs`.

The original targeted regressions failed on the previous implementation.
The arrival-snapshot regressions were also run against the base runner and
failed before its asynchronous path was added. No unit test calls the hosted
endpoint.

## Replay property

The plugin forks each scripted runner scenario before it mutates fixture
state. Child processes use separate watcher and journal files, saved oracle
verdicts and the same fixture clock advances. One returns the saved verdicts;
one removes them. Endpoint failures remain failures in both variants.
These are test processes, with no model calls.

The comparison includes episode records, handoff data, transcripts,
classifier and awareness records, manifests, journals, observed outputs and
event baselines. Verdict rows are matched by output identity. The named
metadata exclusions are in `cohbench/runner/README.md`. Verdict-count baseline
differences are retained explicitly. Missing-result runs compare the common
input prefix and record the first changed reply or hand-back decision.

The final property covers 216 tests, 235 scripted episodes and 470 replay
variants on each Python version. Its oracle contains 2,754 scripted judge
calls, including injected failures, with zero endpoint calls. There are zero undeclared same-verdict differences
and zero missing-verdict prefix differences. Of the missing-verdict runs,
75 diverge at a reply or hand-back decision; 160 retain the complete input
sequence. Each scenario and divergence point is in
[replay-property.json](episode-checks-2026-11-23/replay-property.json).

Five scenarios retain verdict-count baseline differences, all for
`no_request`: the synchronous baseline contains 1 and the asynchronous
baseline contains 0 at that event instant. They are:

- `test_closing_item_not_a_progress_message`, event `conflict`.
- `test_a_later_correction_does_not_leak_into_an_earlier_verdict[5.0]`,
  events `fix` and `seen_fix`.
- `test_a_later_correction_gives_the_same_verdicts_at_both_intervals`,
  scenario 2, events `fix` and `seen_fix`.
- `test_a_probe_target_is_the_batch_at_the_event_never_a_later_one[2.0]`,
  event `after_note`.
- The same test with `[20.0]`, event `after_note`.

None of those event triggers depends on verdicts, so their observed event
records still agree exactly. The difference records when the verdict was
available; it was not removed from the comparison report.

## Verdict-dependent trigger inventory

Regenerate the public inventory without endpoint calls:

```sh
uv run --isolated --python 3.12 python \
  calibration/classifier/inventory_verdict_triggers.py \
  --out out/trigger-inventory.json
```

The [saved inventory](episode-checks-2026-11-23/trigger-inventory.json)
contains the generated suite and both checked-in task fixtures.
The generated development suite has 88 tasks and 116 planted events. None
has a verdict-dependent event or card unlock. Neither checked-in runner task
fixture has one. Existing trigger unit tests exercise the predicate directly.
The new asynchronous regression fixtures contain two verdict-dependent
planted events, both with action `observe` and no card unlock: `after`,
triggered by one `no_request` verdict, and `elapsed_or_verdict`, an `any`
trigger with one `answerable` verdict or 1,001 ms elapsed. The first checks
ordered completion; the second checks that a completion cannot fire its
elapsed branch after the hand-back.

## Verification

The runtime source tested is `cb54370bdd94525ad2070a72e8ba64b3d243652a`.

| Suite                                | Python 3.12                                        | Python 3.15                                        |
| ------------------------------------ | -------------------------------------------------- | -------------------------------------------------- |
| `tests/user`                         | 283 passed, 87.89 s                                | 283 passed, 87.82 s                                |
| `tests/runner`, with replay property | 549 passed, 9 skipped, 26.39 s                     | 549 passed, 9 skipped, 26.40 s                     |
| Whole unit suite                     | 3,687 passed, 313 skipped, 49 deselected, 821.69 s | 3,687 passed, 313 skipped, 49 deselected, 791.79 s |
| Slow pipeline suite                  | 13 passed, 384.24 s                                | 13 passed, 349.14 s                                |

Commands use `uv run --isolated --python VERSION --with pytest`:

```sh
pytest tests/user -q -p no:cacheprovider
pytest tests/runner -q -p no:cacheprovider
pytest tests -q -p no:cacheprovider -m "not integration and not slow"
pytest tests/pipeline -q -p no:cacheprovider -m "slow and not integration"
```

The runner property adds
`COHBENCH_POLICY_REPLAY=out/property.json` and
`-p tests.runner.policy_replay`. Full-suite logs also use `-vv` to identify
long-running tests.

## Hosted exchanges

Completed on 2026-11-23 with the unchanged recommended
`explabs-kimi-k3.1-pro.json` config and one 40-call ledger:

```sh
COHBENCH_CLASSIFIER_KEY_FILE=/path/to/classifier.key \
uv run --isolated --python 3.12 --with pytest python \
  calibration/classifier/check_episode_path.py \
  --execute-hosted --out runs/classifier-episode-checks-2026-11-23
```

| Exchange                     | Result recorded                                                                    | Calls | Input tokens | Output tokens | Wall seconds |
| ---------------------------- | ---------------------------------------------------------------------------------- | ----- | ------------ | ------------- | ------------ |
| Single pending question      | `answerable`, id `day`; delivered `Tuesday.`                                       | 6     | 11,819       | 39            | 8.724        |
| Two outputs at one hand-back | `answerable` for `day` and `format`; delivered one combined reply                  | 10    | 21,266       | 66            | 12.552       |
| Repeat after delivery        | First `answerable`, then `already_given`, quoting the delivered `Tuesday.` message | 10    | 21,359       | 66            | 13.417       |
| Total                        | All three episodes usable                                                          | 26    | 54,444       | 171           | 34.693       |

All 26 calls completed with reported usage. There were no retries, unjudged
outputs or reply overruns. Reported cached input tokens were 42,112 and
reasoning tokens were 0. Each closing `Done.` was `no_request`; all three
awareness replies were `not_suspected`. The repeat's `quote_of: 2` selects
the delivered user answer, and the runner's reply is
`I told you earlier: "Tuesday."`.

The two simultaneous outputs completed 5.468 s and 6.156 s after delivery,
within the same hand-back cutoff. Their arrival snapshots and output times
are recorded separately. The test uses a fake harness with real hosted
judgement time; agent work and user return waits advance a fixture clock.
Its synthetic timestamp origin is 2026-11-22T12:00:00Z. The real run date is
2026-11-23. These wall times are measured script durations, not a real
harness's end-to-end latency.

The [saved episode records](episode-checks-2026-11-23/summary.json) and
[call ledger](episode-checks-2026-11-23/calls.jsonl) contain this run's
verdicts and accounting. Only generic user-card and conversation text entered
classifier requests. No personal data or machine paths were submitted.
The key was read from its private file and is absent from the published
evidence. Development and held-out agreement were not rerun.

## Open problems

No round 4 ruling remains open. The hosted checks use a fake harness;
real-harness end-to-end latency is not measured by these exchanges.
Model/provider identity remains endpoint-reported. [Unverified] Pinned
sampling does not establish hosted determinism; model behavior is not
guaranteed. Existing saved verdicts remain the reproducibility record for
the earlier development and held-out measurements. Their agreement results
and previously recorded disclosure misses are unchanged. No prompt change
was made or proposed from these three exchanges.
