# Round 6 episode-path review, 2026-11-23

The runner remains synchronous and serial. This round changes end and reset
handling, delivery accounting and optional ledger cleanup. It adds no hosted
requests, prompt edits, config edits or held-out data. Hosted calls and tokens
used in this round: 0.

Implementation commit: `94215d41edbb09fefa92e56baba01cef0a890246`.
Reference `main`: `d0ae913c3b3d4ec99e7c5e02c842b0f6c8b87438`.

## Rulings and regressions

Each regression below failed against the round 5 code before its corresponding
change. There are 14 new parametrized cases. The wording-only ruling has a
README contract test. The four retained limits require documentation only.

| Ruling | Change                                                                                                                                                                                                                                                                                                                                                                             | Covering test                                                                                                                                                                                              |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R6-E3  | `Runner._loop` performs one zero-timeout poll after a judgement advances time, before choosing a timer end. The normal chronological replay then handles an earlier hand-back, its probe and clipped wait.                                                                                                                                                                         | `tests/runner/test_classifier_review6.py::test_r6_e3_catch_up_finds_handback_before_timer`, grace and elapsed variants                                                                                     |
| R6-E4  | `Runner._output` limits the pending-question exception to the live matching request outside final and reset replay. Covered final-drain outputs receive their own classifier limit. An expiry records `deadline` without replacing the usable cutoff.                                                                                                                              | `tests/runner/test_classifier_review6.py::test_r6_e4_final_drain_question_gets_own_limit_without_reply`, verdict and expiry variants                                                                       |
| R6-V1  | `_late` uses a known receipt instant. `_Message` retains an unsettled wait until receipt, and `_settle_message_wait` closes it as reply or cutoff. Known or queued receipt after the end adds no delivered card ids and leaves `verdict.reply` null.                                                                                                                               | `tests/runner/test_classifier_review6.py::test_r6_v1_receipt_after_end_is_not_a_delivered_reply`, known and queued variants                                                                                |
| R6-V3  | `cohbench/delivery.py` reproduces `main`'s kind-sensitive timestamp rules. The driver supplies the preceding run and the actual kind hint; the normaliser retains `main`'s choices. An explicit reply uses its item timestamp. An unhinted message after interruption may infer a correction and use restart time. A steer completed at queue time can use a later native receipt. | `tests/trajectory/test_classifier_review6.py::test_r6_v3_equal_queue_steer_prefers_native_receipt` and `test_r6_v3_queued_refusal_matches_main_restart_inference`, with and without the explicit kind hint |
| R6-E2  | Shared deadline selection includes reset notices carried in `_Message.actions` as well as `_Queued.event`.                                                                                                                                                                                                                                                                         | `tests/runner/test_classifier_review6.py::test_r6_e2_message_carried_notice_sets_shared_deadline`                                                                                                          |
| R6-S2  | Optional completion accounting uses daemon workers, bounded reservation/submission waits and a 0.25-second lock deadline. `Classifier.flush` reports pending and incomplete writes without raising after an episode result. Calibration CLI flush still requires completed accounting. The runner factory already leaves the ledger off by default.                                | `tests/user/test_hosted_review6.py::test_r6_s2_blocked_completion_cannot_keep_process_alive` and `test_r6_s2_completion_lock_has_a_deadline`                                                               |
| R6-S1  | Both READMEs distinguish a new submission after the deadline from finishing an already-started send.                                                                                                                                                                                                                                                                               | `tests/user/test_hosted_review6.py::test_r6_s1_readmes_distinguish_new_and_inflight_requests`                                                                                                              |
| R6-T2  | `tests/runner/policy_replay.py` keeps the handoff event list ordered. Matching verdicts by output identity no longer hides swapped handoff rows.                                                                                                                                                                                                                                   | `tests/runner/test_classifier_review6.py::test_r6_t2_replay_compares_handoff_row_order`                                                                                                                    |

R6-V3 changes two round 5 fixture expectations to follow `main`. Explicit
reply hints date the reply at the item timestamp, even on steer and restart
routes. Without that hint, interruption can infer a correction instead. The
driver was informed of this distinction. The two new route regressions and
the saved-episode comparison cover it.

R6-E3 and R6-V1 correct the pre-existing `main` edges expressly identified by
the rulings. No asynchronous judgement machinery was reintroduced.

## Retained limits

Both `cohbench/user/README.md` and `cohbench/runner/README.md` now record:

- R6-E1: `no_request` on a pending native question can use a required event's
  eligible reopen as the answer, as an empty scripted reply does on `main`.
- R6-V2: a reset is recorded as fired at its continuation's send instant,
  as on `main`.
- R6-E5: event records after a scripted policy error can differ from `main`.
  That run is unusable either way.
- R6-T1: both replay arms use this branch's runner and a fake port. The check
  establishes the logical-record comparison for the same verdicts and
  scripted clock advances. It does not establish parity with `main`, real
  receipt behavior or equality of exported scorer files.

Python cannot cancel a blocked name lookup or connect. After the deadline no
new request is submitted and no result is used; a request already being
written may finish being sent. A blocked filesystem operation can also remain
in its daemon thread. Cleanup waits at most 0.25 seconds by default and records
`blocked_transport_threads`, `pending_accounting_threads` and
`incomplete_accounting_writes` under `run.json.classifier_cleanup`.

## Saved-episode comparison against main

`compare_saved_episodes.py` runs the reference and branch rebuilds on the same
raw episode files, then compares every bundle file and the full trajectory.
It temporarily unpacks reference code and deletes its temporary outputs.
The published evidence contains episode ids, error types and file digests.

Of 187 saved episodes, 186 rebuilt on both versions. All 2,031 files from those
186 episodes were byte-identical. There were zero differing rebuilt files.

The remaining archive, `variant-014753/rdemo-variant`, raises `KeyError` on both
versions: a probe entry in its `run.json` lacks `deferred`, which
`cohbench/trajectory/replay.py` requires while rebuilding probe targets. It
cannot meet the requested every-archive rebuild as written. The archive was
not changed and the missing field was not invented. This is one unrebuildable
archive, not a successful parity result.

Set `SAVED_EPISODES` to the existing saved runner-integration directory.
The check was run with Python 3.12 and 3.15, with identical results.

```sh
uv run --isolated --python 3.12 python calibration/classifier/compare_saved_episodes.py \
  --episodes "$SAVED_EPISODES" \
  --reference d0ae913c3b3d4ec99e7c5e02c842b0f6c8b87438 \
  --out out/review6-saved-parity.json
```

The command returns 1 to flag the unrebuildable archive. The committed
[saved-episode evidence](episode-checks-round6-2026-11-23/saved-episode-parity.json)
retains the identical digest map for every successful comparison.

## Strict replay

Both Python versions covered 245 scenarios, 2,770 verdicts and 8,024
checkpoints. The missing-verdict arm compared 745 common-prefix checkpoints.
It recorded an absent-reply divergence in 80 scenarios and agreement through
the end in the other 165. Same-verdict and common-prefix differences: 0.

Only measured `record.ms` and `record.questions[].ms` are masked. Handoff row
order, supplied card and transcript, snapshot context, journal ordinals and
substantive records remain compared. Every absent-reply divergence is in the
[Python 3.12 report](episode-checks-round6-2026-11-23/property-3.12.json) and
[Python 3.15 report](episode-checks-round6-2026-11-23/property-3.15.json).

## Validation

All commands completed successfully on both Python versions. Tests use
fixtures and local test servers; they make no hosted calls.

| Suite                                 | Python 3.12                                        | Python 3.15                                        |
| ------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| `tests/user`                          | 283 passed, 88.44 s                                | 283 passed, 87.76 s                                |
| `tests/runner`, strict replay enabled | 564 passed, 9 skipped, 449.43 s                    | 564 passed, 9 skipped, 372.83 s                    |
| `tests/trajectory`                    | 606 passed, 5 skipped, 3.57 s                      | 606 passed, 5 skipped, 3.45 s                      |
| Whole unit suite                      | 3,705 passed, 313 skipped, 49 deselected, 714.90 s | 3,705 passed, 313 skipped, 49 deselected, 662.17 s |
| Slow pipeline suite                   | 13 passed, 275.93 s                                | 13 passed, 234.56 s                                |

Commands, run for each version:

```sh
for PYTHON_VERSION in 3.12 3.15; do
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/user \
    -q -p no:cacheprovider
  COHBENCH_POLICY_REPLAY="out/review6-property-$PYTHON_VERSION.json" \
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/runner \
    -q -p no:cacheprovider -p tests.runner.policy_replay
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/trajectory \
    -q -p no:cacheprovider
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests \
    -q -p no:cacheprovider -m "not integration and not slow"
  uv run --isolated --python "$PYTHON_VERSION" --with pytest pytest tests/pipeline \
    -q -p no:cacheprovider -m "slow and not integration"
done
```

The 14 new parametrized cases also passed together after final formatting.
The existing slow-accounting fixture now forwards the completion deadline to
its parent ledger method. The initial user-suite run exposed that fixture
signature mismatch; both final user and whole-unit runs above include the
corrected fixture.

Free space was checked before the long runs. Both slow suites ran one at a
time. Four disposable `runs/self-check-*` directories owned by this worktree
were removed afterward; 226 GiB was free at the final check. Successful
strict-replay snapshots are deleted by the existing test helper. Saved raw
episodes and other worktrees' output were left in place.

## Diff against main

Counts from `git diff --numstat main -- cohbench/runner cohbench/trajectory`:

| Scope                          | Added | Deleted |
| ------------------------------ | ----: | ------: |
| Runner Python, six files       |   253 |      62 |
| Runner README                  |   353 |     142 |
| Runner total                   |   606 |     204 |
| Trajectory, three Python files |    14 |      21 |

`cohbench/runner/runner.py` accounts for 204 added and 52 deleted lines;
`cohbench/trajectory/normalise.py` accounts for 9 added and 17 deleted lines.
Outside those directories, the shared `cohbench/delivery.py` has 48 added
lines and the driver episode changes have 53 added and 2 deleted lines
against `main`. These totals include the earlier classifier work.

## Open problems

The one malformed saved archive remains unrebuildable on both versions.
The four recorded limits and blocked platform-call limits above remain.
Real hosted and harness latency was not measured in this round. The existing
calibration recommendation and agreement figures are unchanged.
