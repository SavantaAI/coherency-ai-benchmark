# Hosted classifier review, round 3

On 2026-11-23, the episode-path work preceded the calibration-tooling work.
This round made zero endpoint calls and used zero endpoint tokens. Prompts,
configs, calibration items, gold labels, original verdicts and call ledgers
are unchanged. No held-out item or reply was copied into the repository.

## Rulings and covering tests

The first run of the four R3-V1 regressions failed all four. The four
R3-B1 runner regressions then failed against serial judging, while the
R3-V1 cases passed. The remaining-rulings run recorded ten failures and ten
passes. Each ruling below therefore has a regression that failed before
its change. The two additional classifier-worker cases exercise the shared
cutoff and late-result discard directly. A final additional regression
confirmed that an unjudged secret request raised `StopIteration` in the
fixed-refusal wait path. That path now tests whether a wait is needed
without looking up a verdict that may be absent.

| Ruling | Change and location                                                                                                                                                                                                                                                                                                                                                                                                   | Covering test                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R3-V1  | `runner/policy.py` adds `DeliveredTranscript`, preserving `Entry(role, text)`. `runner/runner.py` attaches the delivered card-id union only after accepted dispatch. `user/classifier.py` uses those ids and the actual message index. Proposed replies create no delivery state.                                                                                                                                     | `tests/runner/test_classifier_review3.py`: `test_r3_v1_overridden_deploy_does_not_claim_later_test_permission`, `test_r3_v1_fixed_override_delivers_no_card_item`, `test_r3_v1_proposal_cut_off_by_episode_end_delivers_nothing`, `test_r3_v1_identical_texts_keep_their_distinct_delivered_items`                                                                                                                                                                                                    |
| R3-B1  | `runner/runner.py` collects outputs until hand-back, freezes one card and transcript, passes the remaining return window less the 3-second dispatch allowance, and applies results in output order. `user/classifier.py` caps that shared window at the configured verdict limit and returns no result for a late worker. `user/api.py` supplies independent client sessions. Scripted policies retain serial replay. | `tests/runner/test_classifier_review3.py`: `test_r3_b1_two_sixteen_second_outputs_share_one_deadline`, `test_r3_b1_one_slow_output_is_unjudged_without_delaying_other_replies`, `test_r3_b1_completion_order_does_not_change_records`, `test_r3_b1_streamed_outputs_wait_for_the_same_handback_snapshot`, `test_r3_b1_unjudged_secret_still_gets_the_fixed_refusal`; `tests/user/test_hosted_review3.py`: `test_r3_b1_classifier_workers_share_cutoff_and_discard_late_result`, two completion orders |
| R3-B4  | `hosted-runs.json` names six development and four held-out runs, their item scopes, saved verdicts and outputs. `regenerate_agreement.py` rejects missing inputs or unowned published reports before any writes. Original accounting evidence is explicitly separate.                                                                                                                                                 | `tests/user/test_hosted_review3.py`: `test_r3_b4_manifest_preflight_owns_every_report`, missing-verdict and orphan-report cases, both asserting unchanged output bytes                                                                                                                                                                                                                                                                                                                                |
| R3-S1  | `regenerate_agreement.py` validates verdict coverage against the manifest scope before selecting report items. An outside id is an error even in partial mode. The gold labels may cover the declared shared source pool.                                                                                                                                                                                             | `tests/user/test_hosted_review3.py`: `test_r3_s1_extra_id_is_not_filtered_out_of_manifest_scope`                                                                                                                                                                                                                                                                                                                                                                                                      |
| R3-S2  | `regenerate_agreement.py` checks turn and awareness file kinds before comparing them. Labels cannot occupy the verdict slot.                                                                                                                                                                                                                                                                                          | `tests/user/test_hosted_review3.py`: `test_r3_s2_verdict_slot_refuses_labels_by_kind`, turn and awareness cases                                                                                                                                                                                                                                                                                                                                                                                       |
| R3-V3  | `regenerate_agreement.py` reports missing input paths relative to the caller's supplied root, on one line without a traceback.                                                                                                                                                                                                                                                                                        | `tests/user/test_hosted_review3.py`: `test_r3_v3_missing_input_names_relative_file_without_traceback`                                                                                                                                                                                                                                                                                                                                                                                                 |
| R3-B3  | `user/api.py` counts submission only after the socket accepts request bytes. Completion accounting waits for that submission outcome. Failure before any transmitted bytes records `not_sent`.                                                                                                                                                                                                                        | `tests/user/test_hosted_review3.py`: `test_r3_b3_submission_failure_before_transmission_is_not_sent`                                                                                                                                                                                                                                                                                                                                                                                                  |
| R3-V2  | `runner/run.py` includes `COHBENCH_CLASSIFIER_KEY_FILE` in the existing host-only path and content-digest admission check. Tests use synthetic credentials.                                                                                                                                                                                                                                                           | `tests/runner/test_classifier_review3.py`: `test_r3_v2_classifier_key_cannot_be_staged`, staged-path and copied-content cases                                                                                                                                                                                                                                                                                                                                                                         |
| R3-B2  | `cohbench/user/README.md` records the reservation-write limit. No reservation logic changed: a write crossing the cutoff can persist, gets `not_sent`, and still consumes one slot.                                                                                                                                                                                                                                   | `tests/user/test_hosted_review3.py`: `test_r3_b2_limit_documents_reservation_write_crossing_cutoff`; existing behavior coverage in `test_r2_b2_slow_reservation_has_explicit_not_sent`                                                                                                                                                                                                                                                                                                                |

Source paths in the first column are relative to `cohbench/` unless they
name a manifest or regeneration file, which lives beside this report.
The runner and classifier READMEs describe the new interface. Existing
runner tests were not changed. Three earlier classifier tests were updated
to supply explicit delivery provenance or remove assertions about the
removed proposed-reply memory.

## Saved-report verification

The manifest regenerated all 18 public development outputs. Every JSON
object and every text report matched its prior value. Prettier restored
JSON whitespace after the standard-library writer. The four private
held-out reports and their summaries were regenerated only from the
permitted item directories, labels and saved verdicts. The public held-out
aggregate is unchanged. No old report was used as a statistical input.

```sh
uv run --isolated --python 3.12 python calibration/classifier/regenerate_agreement.py \
  --allow-partial \
  --heldout-root /path/to/private/heldout \
  --private-results /path/to/private/heldout-results-2026-11-23
```

The private roots are caller-supplied. Omitting both regenerates only the
six declared development runs. `--allow-partial` is required for the
historical GLM run, which completed 16 of its fixed 30 screening items.

The full development results remain Kimi 84/88 exact, label kappa 0.938006,
and four disclosing verdicts; DeepSeek 82/88 exact, label kappa 0.922371,
and four disclosing verdicts. GLM remains 14/16 exact, label kappa 0.835052,
and two disclosing verdicts, with incomplete 16/30 coverage. Held-out turns
remain Kimi 51/52 exact, label kappa 0.974359, and zero disclosures;
DeepSeek 52/52 exact, label kappa 1.000000, and zero disclosures. Each
held-out awareness result remains 10/12 exact with label kappa 0.750000.
The frozen recommendation is unchanged.

## Tests

Each command uses `uv run --isolated --python VERSION --with pytest`.
The test suites use synthetic responses or local transports and make no
hosted endpoint calls.

| Command after the uv prefix                                             | Python 3.12                                       | Python 3.15                                       |
| ----------------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------- |
| `pytest tests/user -q -p no:cacheprovider`                              | 276 passed in 89.44 seconds                       | 276 passed in 87.26 seconds                       |
| `pytest tests/runner -q -p no:cacheprovider`                            | 534 passed, 9 skipped in 9.85 seconds final rerun | 534 passed, 9 skipped in 9.14 seconds final rerun |
| `pytest tests -q -p no:cacheprovider -m "not integration and not slow"` | Pending final rerun final rerun                   | Pending final rerun                               |

Prettier checks pass for the changed Markdown and JSON files.
`git diff --check` passes. The protected-file diff is empty for prompts,
configs, calibration inputs, gold labels, original verdicts and ledgers.

## Open problems

- The episode changes were checked with local fake transports and the fake
  episode clock. Hosted behavior of this revised code is not measured.
- A platform lookup or filesystem flush already in progress can outlive the
  caller's cutoff. Late results are discarded, and CLI exit can still wait
  for accounting. A reservation that crosses the cutoff consumes a slot.
- Calibration transcripts have no delivery provenance. Their told-message
  decision remains a model call; live episode delivery attribution is code.
- Historical model and prompt limitations in the hosted reports still apply.
  This round makes no new model-behavior claim and changes no prompt.
