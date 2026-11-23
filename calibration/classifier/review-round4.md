# Episode-path review round 4

Date: 2026-11-23. Base: `c38cff7600b324d1a18c8ead4181acb5c23b2b05`.
Prompts, pinned configs, development agreement outputs and held-out evidence
are unchanged. No held-out directory was read for this round.

## Rulings and regressions

| Ruling | Change | Covering test in `tests/runner/test_classifier_review4.py` unless stated otherwise |
| --- | --- | --- |
| R4-E1 | `Runner._observe_output` inserts, counts and retains every observed output before judgement. Closing items and probe targets include unjudged outputs. | `test_r4_e1_missing_verdict_preserves_closing_answer_and_probe` |
| R4-E3 | Observation and event baselines follow output delivery order. Historical contexts count only outputs delivered by that instant. | `test_r4_e3_observation_and_correction_baseline_follow_stream`, both verdict variants |
| R4-E2 | One absolute hand-back cutoff survives the reset drain. Grace judging and dispatch share the grace instant. Each asynchronous job also retains its own start-based limit. | `test_r4_e2_reset_drain_cannot_open_a_new_window`, both reset variants; `test_r4_own_start_limit_and_handback_cutoff_both_apply` |
| R4-S1 | `DeadlineExceeded` is a typed cause. Request timeout or other endpoint failure with time left aborts the episode. Job cancellation stops further transport submission. | `tests/user/test_hosted_review4.py`: `test_r4_s1_failure_with_time_left_is_not_an_unjudged_output`, `test_r4_s1_attempt_timeout_carries_shared_deadline_cause`, `test_r4_s1_typed_deadline_is_an_unjudged_output`, both job tests |
| R4-V1 | `text` preserves user words and `wire_text` records the submitted option value. Refusal steers enter the transcript and deliver card ids only when the driver reports actual delivery. | `test_r4_v1_native_option_keeps_user_words_and_wire_value`, `test_r4_v1_refusal_steer_waits_for_observed_delivery`, delivered and never-taken variants |
| R4-S2 | Classifier rows separate snapshot `as_of` from `output_at`. `trajectory.replay.rebuild` uses output time, with historical-row fallbacks. | `test_r4_s2_rebuild_uses_output_instant_not_snapshot` |
| R4-S3 | README lists comparison metadata by name. The reversed-completion test compares complete logical records. Every scripted episode is replayed from the same initial fixture state. | `tests/runner/test_classifier_review3.py::test_r3_b1_completion_order_does_not_change_records`; `tests/runner/policy_replay.py` |

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
  `test_a_live_threshold_reads_the_origin_clock[0.0006]`.
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

Final coverage and per-scenario results: verification in progress.

## Verdict-dependent trigger inventory

The generated development suite has 88 tasks and 116 planted events. None
has a verdict-dependent event or card unlock. Neither checked-in runner task
fixture has one. Existing trigger unit tests exercise the predicate directly.
The new asynchronous regression fixture contains one planted event:
`after`, triggered by one `no_request` verdict, with action `observe` and no
card unlock. It fires at ordered verdict completion.

## Verification

Both Python versions have passed `tests/user` with 283 tests. Final runner,
whole-suite and slow-suite checks are in progress after the last observation
regression. Commands use `uv run --isolated --python VERSION --with pytest`:

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

Not measured yet. The prepared command uses one 40-call ledger:

```sh
COHBENCH_CLASSIFIER_KEY_FILE=/path/to/classifier.key \
uv run --isolated --python 3.12 --with pytest python \
  calibration/classifier/check_episode_path.py \
  --execute-hosted --out runs/classifier-episode-checks-2026-11-23
```

The synthetic-client dry run completed all three exchanges as usable:
one pending question, two outputs at one hand-back, and a repeat ask after
a delivered answer. Only generic user-card and conversation text enters
classifier requests. The key is read from its private file.

## Open problems

Final hosted measurements and verification are pending. Model/provider
identity remains endpoint-reported, and pinned sampling does not establish
hosted determinism. Existing saved verdicts remain the reproducibility
record for the earlier development and held-out measurements.
