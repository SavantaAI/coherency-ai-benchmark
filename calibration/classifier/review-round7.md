# Round 7 episode-path review, 2026-11-23

Core implementation commit: `2f3e5f296bfb96041c60904a53c93bbbfcfea244`.
Hosted-check preflight commit: `79b73436baf0be86728b5e97b4ac69492dd88604`.
Reference `main`: `d0ae913c3b3d4ec99e7c5e02c842b0f6c8b87438`.

This round changes four episode-path behaviors. Judging remains synchronous
and serial. Prompts, pinned configs, calibration verdicts and agreement figures
are unchanged. No hosted calls were made; hosted calls and tokens used: 0.
No held-out files were read or copied.

## Rulings and tests

All 12 cases in `tests/runner/test_classifier_review7.py` were run against a
temporary archive of the round 6 commit, `5e7fc53`, with the final test file.
All 12 failed there. They all pass with the changes below. The temporary
reference code and test output tree were deleted after the check. One further
regression in `tests/user/test_hosted_review7.py` fails before the hosted-check
preflight and passes after it, for 13 new cases in total.

| Ruling            | Change and location                                                                                                                                                                                                                                                                                                                                                                                       | Covering tests in `tests/runner/test_classifier_review7.py`                                                                                                                                                                                           |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R7-02             | `Runner._output` gives admitted pre-end outputs their own limit when the end has arrived or final replay is active. `_begin_judging` no longer clips a hand-back deadline by the episode end. Input admission and reply dispatch remain bounded by the episode end.                                                                                                                                       | `test_r7_02_cutoff_poll_gives_pre_end_output_own_limit`, cutoff-poll and near-end hand-back cases                                                                                                                                                     |
| R7-03             | `_loop` adds one catch-up look only after a judgement longer than one second. It carries outputs later than an earlier reset grace into that reset's drain. `_replay` attempts a correction reached before a later output before judging that output. It retains pending inputs if the dispatch gate ends the episode. A judgement in the extra look does not chain another look ahead of timer handling. | `test_r7_03_catch_up_handles_grace_before_later_output`; `test_r7_03_due_correction_precedes_next_catch_up_judgement`; `test_r7_03_short_judgement_does_not_add_catch_up_poll`, 0.01 s and 1 s; `test_r7_03_catch_up_does_not_chain_before_due_grace` |
| R7-04             | `run.admit_policy_delay` runs after policy construction and before opening the driver episode. `ScriptedPolicy.max_judgement_s` is zero; `Classifier.max_judgement_s` is its configured verdict limit. A positive declaration requires the task delay to cover that maximum plus the 3-second dispatch allowance. Rejection names actual and required seconds.                                            | `test_r7_04_classifier_rejected_before_open_when_delay_too_short`, 2 s and 27.999 s; `test_r7_04_boundary_and_generated_delays_are_admitted`, 28 s equality, 2 s scripted and all 88 generated tasks                                                  |
| R7-06, first part | `_delivered_with` retains the original send for a carried correction and its watcher mark. A queued message keeps that original send when its receipt becomes known.                                                                                                                                                                                                                                      | `test_r7_06_carried_correction_uses_send_time_and_mark`, known and queued receipts                                                                                                                                                                    |

The driver confirmed the admission exception for zero declarations. Applying
zero plus the dispatch allowance to the two-second scripted fixture would
contradict the ruling's explicit instruction to retain it and `main`'s behavior.
Positive declarations alone receive the new admission check.

R7-04 also applies to `check_episode_path.py --include-short-deadline`, whose
historical fourth scenario uses seven seconds. The script now calls the same
admission helper before constructing a client or reading a key, so an
incompatible request is rejected before any earlier paid scenario runs.
`tests/user/test_hosted_review7.py::test_r7_04_hosted_short_check_is_rejected_before_reading_key`
covers this with credential loading replaced by a test failure. The ordinary
three scenarios remain available; no hosted check was run in this round.

The generated tasks all pass. Their generator lower bound is 30 seconds;
the 88 seeded instances in this suite have a measured minimum of 36 seconds.
The shipped classifier requires 25 + 3 = 28 seconds. No generator changed.

For carried corrections, `main`'s send-time boundary wins over the branch's
previous receipt-time boundary. The reply's delivery accounting and other
receipt handling remain separate from that correction boundary.

## Recorded limits and open task-format item

Both unit READMEs record the following without runtime changes:

- R7-01: a silent hand-back needs two idle observations. When the extra poll
  supplies only the first and a timer became due during judging, the runner
  can take an interrupted hand-back at that timer. `main` shares this case.
- R7-05: a hand-back probe is timestamped before the copy, after judging, so
  its timestamp can move by the judgement duration while the agent is idle.
  Normalisation reanchors it only when the copy is after episode end.
- R7-06, second part: live steer timestamp selection lacks the harness-native
  receipt. Rebuild can use it when no runner-supplied delivery instant exists;
  supplied runner facts take precedence. Saved rebuild parity holds those
  facts fixed and does not establish live receipt parity.
- R7-V1: generated openings do not declare the card ids they state. If a later
  question selects the same card item, the empty delivered-ID tuple leaves it
  on the fresh-item path. The task format and generators need that provenance.
  This unit does not change them or infer the missing ids.

The minor confirmation case is also documented: a pre-end receipt first seen
in the extra confirmation can be discarded if its timestamp falls after that
confirmation's replay bound. `main` shares that bound.

## Validation

All required suites passed on both Python versions. The final user and whole
unit runs include the hosted-check preflight regression. No test calls the
hosted endpoint.

| Suite                                 | Python 3.12                                        | Python 3.15                                        |
| ------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| `tests/user`                          | 284 passed, 89.34 s                                | 284 passed, 89.27 s                                |
| `tests/runner`, strict replay enabled | 576 passed, 9 skipped, 737.14 s                    | 576 passed, 9 skipped, 653.44 s                    |
| `tests/trajectory`                    | 606 passed, 5 skipped, 4.57 s                      | 606 passed, 5 skipped, 4.29 s                      |
| Whole unit suite                      | 3,718 passed, 313 skipped, 49 deselected, 758.04 s | 3,718 passed, 313 skipped, 49 deselected, 696.91 s |
| Slow pipeline suite                   | 13 passed, 443.91 s                                | 13 passed, 355.97 s                                |
| New round 7 regressions together      | 13 passed, 0.79 s                                  | 13 passed, 0.75 s                                  |

Commands, run for each version:

```sh
for PYTHON_VERSION in 3.12 3.15; do
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/user \
    -q -p no:cacheprovider
  COHBENCH_POLICY_REPLAY="out/review7-property-$PYTHON_VERSION.json" \
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/runner \
    -q -p no:cacheprovider -p tests.runner.policy_replay
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/trajectory \
    -q -p no:cacheprovider
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests \
    -q -p no:cacheprovider -m "not integration and not slow"
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/pipeline \
    -q -p no:cacheprovider -m "slow and not integration"
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest \
    tests/runner/test_classifier_review7.py tests/user/test_hosted_review7.py \
    -q -p no:cacheprovider
done
```

Free space was checked before the long runs. The slow checks ran one Python
at a time, and their two disposable `runs/self-check-*` directories were
removed afterward. Final free space: 213 GiB. Successful replay snapshots
were deleted by the existing helper. Raw saved archives and other worktrees'
output were left in place.

## Strict replay and saved-episode comparison

Each Python covered 249 scenarios, 2,778 verdicts and 8,040 checkpoints.
The missing-verdict arm checked 755 common-prefix checkpoints. There were
82 absent-reply divergences and 167 agreements through the end. Same-verdict
and common-prefix differences: 0. The two reports are identical.

The [Python 3.12 replay report](episode-checks-round7-2026-11-23/property-3.12.json)
and [Python 3.15 replay report](episode-checks-round7-2026-11-23/property-3.15.json)
record each scenario and divergence. As documented in the README, both arms
use this branch's runner and a fake port; this is not a real-receipt or
`main`-parity check.

The saved-episode rebuild ran on Python 3.12 and 3.15 with identical results.
Of 187 archives, 186 rebuilt on both `main` and the branch. Every one of their
2,031 bundle and trajectory files was byte-identical. There were zero differing
rebuilt files. As in round 6, `variant-014753/rdemo-variant` raises `KeyError` on
both versions because a probe row lacks `deferred`. That archive was left
unchanged; it is not counted as a successful comparison.

With `SAVED_EPISODES` pointing to the existing runner-integration archive root:

```sh
uv run --isolated --python 3.12 python calibration/classifier/compare_saved_episodes.py \
  --episodes "$SAVED_EPISODES" \
  --reference d0ae913c3b3d4ec99e7c5e02c842b0f6c8b87438 \
  --out out/review7-parity-3.12.json
```

Use `--python 3.15` for the other run. The command exits 1 for the shared
unrebuildable archive. The committed evidence retains only relative episode
ids, error types and file digests, without raw episode content. The
[saved comparison](episode-checks-round7-2026-11-23/saved-episode-parity.json)
reproduces on both Python versions.

## Change size

Against round 6, runtime Python files have 57 added and 26 removed lines,
including policy-contract documentation. The runner loop itself has 34 added
and 24 removed lines. The hosted-check preflight adds 8 lines. The rest is
regression tests and README text. No async
job, batch-judging or deferred-application machinery was added.

Against `main`, runner Python totals 288 added and 70 removed lines. The
trajectory directory remains at 14 added and 21 removed lines from the earlier
rounds, with no trajectory code changed in round 7.

## Open problems

The documented limits and missing opening-card provenance remain. The one
malformed saved archive cannot rebuild on either version. Real hosted latency
and real-harness receipt behavior were not measured this round.
