# Classifier episode path, review round 5

The runner judges synchronously and serially again. Each output uses its
own delivery-time card and transcript, including several outputs observed
in one poll. Only the absolute judging deadline is shared by a hand-back.
The driver withdrew the earlier hand-back snapshot instruction after
checking `main`. Scripted policy calls retain `main`'s behavior.

The driver also replaced the original transport lifetime requirement.
Bounded-return transport threads remain; subprocess isolation was not added.
Python cannot cancel blocked name lookup or connect. A cancelled attempt
cannot submit a later request, its result is unused, and its only permitted
ledger change is closing its own attempt. Episode cleanup waits up to 0.25
seconds by default and records still-blocked transport and pending accounting
thread counts in `run.json.classifier_cleanup`.

Prompts, model configs, saved calibration verdicts and agreement reports
are unchanged. No held-out files were opened or copied for this review.
All dates below are 2026-11-23.

## Rulings and regressions

| Ruling                  | Change and location                                                                                                                                                                                                                                    | Covering regression                                                                                                                                                                                                                                                                                             |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R5-E1                   | `driver/episode.py` answers delivery queries for every send it owns, including refusals absent from its correction list. `runner/adapter.py` registers message and native request ids.                                                                 | `test_r5_refusal_delivery_through_real_adapter_and_driver` uses the real adapter and driver over fake T3, first without receipt and then with receipt.                                                                                                                                                          |
| R5-E2                   | `runner/runner.py` chooses the earlier reported hand-back or reset grace before judging. Outputs and the reset drain share that dispatch cutoff.                                                                                                       | `test_r5_reset_chooses_dispatch_instant_before_judging` tests both orders, including a notice first observed in the hand-back poll.                                                                                                                                                                             |
| R5-E3                   | Removed runner jobs, pending result collection, cancellation at reset and deferred applications. Calls return before the loop continues.                                                                                                               | `test_r5_serial_outputs_share_deadline_and_keep_last_closing`, `test_r5_full_limit_preserves_reset_grace_instant`, the two dispatch-order cases and strict replay reset scenarios.                                                                                                                              |
| R5-E4                   | A native question gets the classifier's full limit. Expiry raises the endpoint failure class and sends no reopen as an invented answer. Scripted empty replies retain their existing behavior.                                                         | `test_r5_pending_question_gets_full_limit_and_no_invented_reopen` checks the 25-second limit, unusable outcome and absence of an answer send.                                                                                                                                                                   |
| R5-E5                   | Serial judging checks the episode boundary before each classifier call. The classifier rejects an already-expired deadline before layout or networking.                                                                                                | `test_r5_serial_outputs_share_deadline_and_keep_last_closing`, `test_r5_full_limit_preserves_elapsed_end_and_interrupt`, and existing classifier deadline guards.                                                                                                                                               |
| R5-V1                   | `cohbench/delivery.py` supplies receipt instants to the driver and trajectory. Native answers use request resolution. Queued replies add conversation/card facts and scorer `verdict.reply` only on receipt.                                           | `test_r5_native_answer_uses_resolution_time_in_driver_and_trajectory`, three `test_r5_message_routes_use_same_driver_and_trajectory_instant` cases, `test_r5_correction_receipt_preserves_submillisecond_instant`, and both retained refusal-delivery cases in `test_classifier_review4.py`.                    |
| R5-S1                   | Removed asynchronous state. `tests/runner/policy_replay.py` compares whole records and exact call inputs by output item id, preserving snapshot context and journal metadata. Missing audits are compared and only an absent reply permits divergence. | `test_r5_strict_replay_keeps_snapshot_context_and_missing_audits`, `test_r5_replay_checks_inputs_by_identity_even_with_repeated_text`, and the full replay property.                                                                                                                                            |
| R5-S2                   | Classifier missing-verdict audits use `deadline` or `endpoint_failure`. Historical `cutoff` remains for outputs outside admitted judging coverage and scripted policy failures.                                                                        | Both `test_r5_missing_verdict_audit_names_cause` cases and the native-question failure test.                                                                                                                                                                                                                    |
| Transport clarification | `user/api.py` keeps bounded-return transport and adds bounded flush counts. `runner/run.py` invokes the policy flush hook and `runner/record.py` stores its result.                                                                                    | Both `test_r5_abandoned_transport_has_bounded_flush_and_no_late_submission` cases block lookup or connect, check deadline return and flush, release the block, and check zero requests, unchanged served state and one closing ledger line. `test_r5_cleanup_count_is_in_episode_run_record` checks the record. |

Test names without another path are in `tests/runner/test_classifier_review5.py`.
The transport tests are in `tests/user/test_hosted_review5.py`.
The initial serial, failure-cause and native-question regressions failed
before the implementation changed. The transport tests failed before the
bounded flush change. Replaying the old real-driver methods made the
refusal and native-resolution regressions fail. Loading the old comparison
functions made the snapshot-context mutation regression fail because it
reported no difference.

`main` also had the single-output closing omission: it incremented output
count before judging, but only added to `seen` after a verdict. Its unjudged
path added an audit without adding a closing candidate. Observation now
precedes the serial call. Retained round 4 tests check output counts,
planted events, closing items and probes with missing verdicts. The new
`test_r5_unjudged_secret_keeps_fixed_refusal` keeps that fixed response on
an expired judgement.

## Timers and polling

The timer preflight passed before the serial change. A 25-second simulated
judgement does not discard the original usage grace, elapsed end or reset
grace instant. Usage recovery is read when the call returns. If still
unavailable past grace, the episode fails. An elapsed end at 10 seconds
remains dated at 10 seconds when processing resumes at 26 seconds; the
runner interrupts and drains once. A reset grace at 6 seconds remains dated
at 6 seconds when processing resumes at 28 seconds.

The tests are `test_r5_full_limit_respects_usage_grace`, with and without
recovery, `test_r5_full_limit_preserves_elapsed_end_and_interrupt`, and
`test_r5_full_limit_preserves_reset_grace_instant`. No timer required a
protocol change or a stop for clarification.

The known cost is polling latency. While judging, the episode loop does not
poll. A planted event or correction after a mid-run message can be
dispatched late by up to the judgement duration. Observation and delivery
timestamps remain their own instants. Both unit READMEs state this limit.

## Strict replay and event inventory

The property forks identical fixture state before mutation and replays
saved verdicts through `judge(..., deadline=...)`. It asserts exact card,
transcript and delivery provenance for each output item id. Identical text
does not identify an output. The saved oracle returns immediately even for
a fixture whose return delay is below the dispatch allowance; the real
classifier's deadline guard still rejects an expired call before networking.

Whole episode, handoff, transcript, classifier, awareness, manifest and
journal records are compared. The only masks are measured `record.ms` and
`record.questions[].ms`. Snapshot `as_of`, `card_items`, `record.context`,
journal `n`, `utc` and `clock_ms` are included. Verdict rows are matched by
output identity and duplicate identities fail.

For missing verdicts, the expected verdict row becomes the required unjudged
row with the same output, timestamp and journal ordinal. The expected
transcript label becomes null and names its missing-verdict cause.
Classifier verdict rows and baseline verdict counts are removed. Every
other field is compared at each output and hand-back decision. A scenario
stops at the first absent reply, including an absent native answer, after
checking its unjudged row. A different input alone is an error.

On each Python, the property covers 240 scripted episodes and 480 variants,
2,761 captured verdict calls and 8,006 output/decision checkpoints. It
reports zero differences. Missing-verdict runs agree through the full run
in 160 scenarios. They stop at an absent hand-back reply in 59 and an absent
native-question reply in 21. The two Python reports have identical comparison records. Every scenario's
stopping point is in the [Python 3.12 report](episode-checks-round5-2026-11-23/replay-property-3.12.json)
and [Python 3.15 report](episode-checks-round5-2026-11-23/replay-property-3.15.json).

The inventory command inspected 88 generated development-suite tasks with
116 planted events and two checked-in runner task fixtures with four
planted events. None has a verdict-dependent event or card unlock. No
remaining dynamic regression fixture has one. The round 4 asynchronous
completion fixtures were removed with the path they tested.

```sh
uv run --isolated --python 3.12 python \
  calibration/classifier/inventory_verdict_triggers.py \
  --out out/review5-trigger-inventory.json
```

## Verification

The first concurrent broad runs encountered host disk exhaustion, confirmed
by `Errno 28`. They are not counted as successful validation. Only this
worktree's disposable pipeline outputs and successful replay snapshots were
removed. The replay helper now discards successful snapshot trees after
saving compact comparison and divergence records; failed snapshots remain
for diagnosis. The matrix was restarted with the slow suites run one Python
at a time.

| Suite                                   | Python 3.12                                        | Python 3.15                                        |
| --------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| `tests/user`                            | 280 passed, 90.58 s                                | 280 passed, 89.89 s                                |
| `tests/runner`, including strict replay | 556 passed, 9 skipped, 576.12 s                    | 556 passed, 9 skipped, 481.37 s                    |
| Whole unit suite                        | 3,691 passed, 313 skipped, 49 deselected, 854.39 s | 3,691 passed, 313 skipped, 49 deselected, 804.99 s |
| Slow pipeline suite                     | 13 passed, 346.83 s                                | 13 passed, 280.29 s                                |

```sh
uv run --isolated --python VERSION --with pytest pytest tests/user -q -p no:cacheprovider
COHBENCH_POLICY_REPLAY=out/review5-property-VERSION.json \
uv run --isolated --python VERSION --with pytest pytest tests/runner \
  -q -p no:cacheprovider -p tests.runner.policy_replay
uv run --isolated --python VERSION --with pytest pytest tests \
  -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python VERSION --with pytest pytest tests/pipeline \
  -q -p no:cacheprovider -m "slow and not integration"
```

## Hosted exchanges

The hosted command is run only after the offline suites pass, with the
unchanged recommended Kimi config and one 40-call ledger:

```sh
COHBENCH_CLASSIFIER_KEY_FILE=/path/to/classifier.key \
uv run --isolated --python 3.12 --with pytest python \
  calibration/classifier/check_episode_path.py --execute-hosted \
  --include-short-deadline --out runs/classifier-episode-checks-round5-2026-11-23
```

The fourth exchange has three outputs in one hand-back and a seven-second
return delay, leaving four seconds for judging before dispatch allowance.
Agent work and user waits advance a fixture clock; judging uses real time.
The timestamp origin is 2026-11-22T12:00:00Z. These are fake-harness episode
checks. Real-harness end-to-end latency is not measured.

All four exchanges were usable. The ledger reserved 30 attempts; 29 sent a
request and were accepted. Attempt 29 expired before transmission and has a
`not_sent` line. Every transmitted request reported usage. There were no
retries, reply overruns or blocked transport/accounting threads at cleanup.

| Exchange                      | Recorded behavior                                                                                                  | Reserved / sent | Input tokens | Output tokens | Wall seconds |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------ | --------------- | ------------ | ------------- | ------------ |
| Single pending question       | `answerable`, id `day`; delivered `Tuesday.`                                                                       | 6 / 6           | 11,819       | 39            | 10.686       |
| Two outputs at one hand-back  | Both `answerable`, ids `day` and `format`; delivered one combined reply                                            | 10 / 10         | 21,266       | 66            | 15.521       |
| Repeat after delivery         | `answerable`, then `already_given`, quoting delivered message 2                                                    | 10 / 10         | 21,359       | 66            | 15.982       |
| Three outputs, short deadline | First two `no_request`; final question unjudged with cause `deadline`, retained as closing item; no invented reply | 4 / 3           | 4,631        | 18            | 5.561        |
| Total                         | Four usable episodes                                                                                               | 30 / 29         | 59,075       | 189           | 47.750       |

Reported cached input tokens total 50,304; reported reasoning tokens total 0. The untransmitted reservation used one cap slot and had no provider
usage. Tokens are measured for all 29 transmitted requests. This leaves
10 of the 40 authorized attempt slots unused. Prices and billing charges
were not measured.

The [episode records](episode-checks-round5-2026-11-23/summary.json) and
[call ledger](episode-checks-round5-2026-11-23/calls.jsonl) retain the verdicts,
unjudged audit, closing reference, delivery facts and cleanup counts. They
contain generic fixture text and were checked for credentials and local
machine paths before publication. Only the unchanged classifier prompts
and generic card/conversation text entered requests. No development or
held-out model calls were made in round 5.

## Removed code and diff size

`cohbench/user/job.py` is deleted, 75 lines. `Classifier.start_judgement`
and `judge_batch` are removed, along with per-worker client forks, runner
job queues, collection, deferred application and completion-time trigger
handling. Obsolete asynchronous tests are removed; serial and strict replay
regressions cover the retained behavior. The verdict-trigger inventory
helper moved from runner runtime code to its calibration script.

The implementation commit is `c8f9e44c131c4f5f6a68a2a0e4f7113153ac2557`.
Against round 4, runner Python changes are 93 added and 133 deleted lines.
Client, classifier and removed job code total 23 added and 171 deleted lines.
Named deleted methods are `_settle_jobs`, 24 lines; `_context_at`, 5;
`judge_batch`, 41; `start_judgement`, 3; client `cancel`, 5; and client `fork`, 10. These counts exclude their surrounding blank lines.

Against `main` at `d0ae913c3b3d4ec99e7c5e02c842b0f6c8b87438`, runner Python changes are 213 added and 56 deleted lines,
269 changed lines across six Python files. `runner.py` itself is 164 added
and 46 deleted. Including the runner README, the diff is 509 added and 196
deleted lines, 705 changed lines across seven files. The Python difference
retains delivery provenance, deadline handling, credential admission and
unjudged-output handling.

## Open problems

Python cannot cancel blocked name lookup or connect; cleanup reports the
threads still blocked after its bound. Real-harness end-to-end latency is
not measured by the hosted fixture checks. Endpoint identity is reported
by the provider. [Unverified] Hosted weight identity and seed enforcement
cannot be independently verified here; model behavior is not guaranteed.
No round 5 ruling remains open. Saved calibration verdicts remain the record for reproducing previous
agreement reports. No prompt or config was changed.
