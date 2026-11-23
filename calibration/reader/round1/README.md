# Reader review and calibration, 2026-11-23

[Inference] Kimi remains the provisional development reader because all four large-sample checks were measured and passed. DeepSeek also passed the fresh no-answer check. Its earlier 1/72 result on ordinary development tasks does not support rejecting it on this larger sample. Model behavior is not guaranteed. Human calibration and timed human readers are not measured, so there is no valid release manifest.

## Fresh sample and thresholds

The driver revised the allocation after request rendering showed that the original plan needed 6,000 calls. The approved run kept the 4,500-call cap: seeds 5000 through 5199, 200 fresh non-control h20 instances, Kimi no-answer and buried inputs on all 200, Kimi ideal and worst inputs on the first 60, and DeepSeek no-answer on all 200. Six balanced orders per input produce 4,320 distinct requests. No development seeds were reused. The ideal and worst answers are generated scripts. No human labels are implied.

The driver chose the thresholds on 2026-11-23: no answer within five percentage points of 1/6 (11.67% through 21.67%), buried at most 25%, ideal at least 95%, worst at most 10%. The sanity inputs require at least 200 instances and the controls at least 60. These bars were fixed before the run. The earlier development expectations were ideal near 100%, worst and buried below 25%, and no answer near 1/6. All new measurements meet those earlier expectations and the stricter prescribed control bars.

| Model    | Input                |    Instances | True cause / votes |         Rate | Prescribed check |
| -------- | -------------------- | -----------: | -----------------: | -----------: | ---------------- |
| Kimi     | No answer            |          200 |           178/1200 |       14.83% | Pass             |
| Kimi     | Buried               |          200 |           207/1200 |       17.25% | Pass             |
| Kimi     | Ideal                |           60 |            360/360 |      100.00% | Pass             |
| Kimi     | Worst                |           60 |             16/360 |        4.44% | Pass             |
| DeepSeek | No answer            |          200 |           171/1200 |       14.25% | Pass             |
| DeepSeek | Buried, ideal, worst | not measured |       not measured | not measured | Not assessed     |

Kimi is `kimi-k3.1-pro`, reported provider `openrouter`, config digest `dfcc9145ef59fa61c5739d62d4a958a6986391fc8abc77cfc39ff8a3f20ffa2c`. DeepSeek is `deepseek-v4.1-flash-uncensored`, reported provider `experiential_cloud`, digest `d3df0d929f7179fc904c6d7cb157f80324840896bb2865fa4342070246382acc`. Both used unchanged `reader-1`, temperature 0, seed 20261123, 16 output tokens, reasoning effort `none`, six orders and order seed 0. Each large-sample record binds its config digest and the rendered prompt template. The DeepSeek record is incomplete for a release by design.

| Model    | Input     | Correct counts in orders 0 through 5   | Selected positions A through F |
| -------- | --------- | -------------------------------------- | ------------------------------ |
| Kimi     | No answer | 30, 31, 29, 30, 25, 33 (200 per order) | 167, 320, 254, 213, 148, 98    |
| Kimi     | Buried    | 35, 30, 35, 37, 36, 34 (200 per order) | 195, 283, 274, 200, 143, 105   |
| Kimi     | Ideal     | 60, 60, 60, 60, 60, 60 (60 per order)  | 60, 60, 60, 60, 60, 60         |
| Kimi     | Worst     | 3, 2, 2, 1, 4, 4 (60 per order)        | 73, 80, 66, 54, 47, 40         |
| DeepSeek | No answer | 29, 29, 28, 33, 28, 24 (200 per order) | 289, 244, 221, 145, 136, 165   |

Selected positions are visibly uneven for the null inputs. The true-cause rate meets the prescribed aggregate bar; that does not establish uniform position selection. Orders within an instance are dependent.

Every large-control exception: ideal has none. Worst selected the true cause at seed 5020 order [4], 5021 [4], 5042 [4, 5], 5045 [0, 5], 5046 [1, 2, 3, 4, 5], 5053 [0, 1, 2, 5], and 5056 [0]. The negative-control expectation is a low rate, not zero. No-answer and buried inputs have aggregate bars rather than a uniquely correct per-item alternative.

## Generated-answer rerun with the main question

The generator's only behavior change adds the main `question` to the Explanation answer key. Both scored answers use that field. Legacy bundles still use the opening prompt and say so in score provenance. This removes the unrelated h80 chore list from reader inputs. A fresh suite again completed 268 scripted episodes with zero failures and zero expectation mismatches.

The suite digest pinned by `tests/tasks/test_determinism.py` changed from `92287ff7f11871a5c5f4771981550e6a16a448e09f7206a539b717ad874e7769` to `26b536546e26d6d7aa1ea52e9c97ed3ec15bc7b5280045fd32e1e99762c37033` because answer keys are part of the task bytes.

Old cache keys did not cover the rendered user-message template. They were not trusted automatically. The local migration rendered all 792 old generated requests with the reader from commit `5e9d53d`, compared their complete request bodies with the new renderer, checked the unchanged config digest, then rekeyed accepted rows. Of the new run's 432 unique requests, 360 replayed those verified rows and 72 needed new calls. The separate rerun ledger had a 100-call cap. Every bundle was validated and passed through `score_episode` and `explanation.score`.

| Population       | Input     |            Before |             After |
| ---------------- | --------- | ----------------: | ----------------: |
| All 24 instances | Ideal     | 288/288 (100.00%) | 288/288 (100.00%) |
| All 24 instances | Worst     |     8/288 (2.78%) |    12/288 (4.17%) |
| All 24 instances | No answer |   31/144 (21.53%) |    13/72 (18.06%) |
| All 24 instances | Buried    |   32/144 (22.22%) |    15/72 (20.83%) |
| Ordinary tasks   | Ideal     | 144/144 (100.00%) | 144/144 (100.00%) |
| Ordinary tasks   | Worst     |     2/144 (1.39%) |     3/144 (2.08%) |
| Ordinary tasks   | No answer |    11/72 (15.28%) |     5/36 (13.89%) |
| Ordinary tasks   | Buried    |    12/72 (16.67%) |     6/36 (16.67%) |

Sanity inputs now deduplicate across horizons: 12 distinct inputs across all instances and six on ordinary tasks. Their denominators fell accordingly. These small-sample smoke checks pass but do not establish chance behavior. The large calibration above supplies the separate specification checks.

Every rerun exception: ideal has none. Worst selected the true cause for `explanation-h20-s4-single`, answers 1 and 2, order [1]; `explanation-h80-s4-single`, answer 2, order [1]; `explanation-h20-s1006-control`, answers 1 and 2, orders [0, 4, 5]; and `explanation-h80-s1006-control`, answer 2, orders [0, 4, 5]. The aggregate JSON gives each order count and the control-only stratum.

The 40 authored answers were not changed or rerun. Both models' original visible-attribution accuracy was 60/60 in each of the four groups, with no misses. The full answers remain outside the repository. See the initial record for their construction and results.

## Calls, tokens and latency

All attempts were accepted. There were no retries, failed transports or calls without reported usage in this round. Input counts include provider-reported cached input; DeepSeek reported 1,792 cached tokens, included in its input total. Latency here is per endpoint attempt, measured by the shared client, excluding subsequent ledger flush and cache persistence.

| Run               | Model    | Calls | Input tokens | Output tokens | Mean / median / p95 seconds |
| ----------------- | -------- | ----: | -----------: | ------------: | --------------------------- |
| Large calibration | Kimi     |  3120 |    1,334,982 |        18,720 | 1.358 / 1.162 / 2.771       |
| Large calibration | DeepSeek |  1200 |      382,188 |         8,400 | 1.244 / 1.069 / 2.506       |
| Generated rerun   | Kimi     |    72 |       33,480 |           432 | 1.255 / 1.130 / 2.332       |

Large calibration: 4,320/4,500 calls, 1,717,170 input and 27,120 output tokens. Separate generated rerun: 72/100 calls, 33,480 input and 432 output tokens. This round totals 4,392 calls, 1,750,650 input and 27,552 output tokens. Including the original run: 6,459 calls, 2,988,804 reported input and 40,968 reported output tokens. The original three failed DeepSeek transports still have unmeasured usage. Exact provider billing is not measured. A request-body audit found zero overlap between either model's original 1,032 requests and its new large run; the 72 new generated requests also matched none of the original generated or authored inputs and none of the large run.

## Rulings and covering tests

The regression tests use fake transports or local state. They never call the paid endpoint. New assertions were observed failing before the corresponding implementation changes; existing already-correct validation cases remain in the parameterized checks.

| Ruling     | Change                                                                                                                                            | Covering test in `tests/scoring/test_reader_rulings.py` unless stated otherwise                                                                                                                                                     |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| V4         | Removed machine paths first; scan tracked calibration and unit READMEs                                                                            | `test_v4_published_calibration_and_unit_readmes_have_no_machine_paths`                                                                                                                                                              |
| S4         | Main question for both answers; legacy fallback and score provenance; updated suite digest                                                        | `test_s4_key_question_used_for_both_answers_and_provenance`, `test_s4_generator_stores_main_question_without_horizon_chores`; task determinism suite                                                                                |
| B6         | Read key only on a cache miss                                                                                                                     | `test_b6_complete_cache_replays_without_credentials`                                                                                                                                                                                |
| B3         | Full rendered request body plus config in cache identity; deeply immutable parsed config                                                          | `test_b3_cache_covers_rendered_template`, `test_b3_parsed_config_is_deeply_immutable`                                                                                                                                               |
| B1, B4     | Ignore partial final JSON; atomic append repairs tail and missing newline; reject duplicate fields and malformed interior lines                   | `test_b1_cache_tail_repaired_before_append`, `test_b1_b4_bad_complete_rows_name_line`                                                                                                                                               |
| B2         | Bounded lock wait; no network call under the cache lock; concurrent misses adopt first durable vote                                               | `test_b2_concurrent_scorers_do_not_hold_lock_over_transport`, `test_b2_cache_lock_wait_uses_choice_deadline`                                                                                                                        |
| B5         | Required fields checked at load; booleans rejected as numbers                                                                                     | `test_b5_missing_required_fields_and_booleans_refused`                                                                                                                                                                              |
| S1         | Honest smoke bounds and counts; fresh 200/60 calibration command, position counts and fixed thresholds                                            | `test_s1_smoke_bounds_are_possible_probabilities`, `test_s1_large_calibration_thresholds_and_sample_sizes`, `test_s1_calibration_sample_is_fresh_and_has_revised_allocation`, `test_s1_position_only_reader_fails_positive_control` |
| S3         | Explicit release manifest bound to score provenance; missing, malformed, mismatched or below-bar evidence yields diagnostic reports without bands | `test_s3_missing_manifest_disables_release_bands`, `test_s3_invalid_manifest_never_releases`, `test_s3_classifier_provenance_is_validated_and_carried`; runner provenance test                                                      |
| S2, V1, S5 | Document reported-identity limits, agent-written identity, whitespace window, Markdown and unspaced scripts                                       | `test_s2_s5_limits_are_explicit`                                                                                                                                                                                                    |
| B7 to B9   | Old classifier modules re-export previous names                                                                                                   | `test_b7_compatibility_modules_export_previous_names`; existing classifier client and ledger suites                                                                                                                                 |
| S6         | Calibration summarizer uses production smoke bounds                                                                                               | `test_s6_summarizer_uses_production_smoke_bounds`                                                                                                                                                                                   |
| V2, V3     | Document red-herring exclusion in buried prefix and host-side true-option recovery                                                                | `test_s2_s5_limits_are_explicit`; documentation only                                                                                                                                                                                |

## Verification and reproduction

```sh
uv run --isolated --python 3.12 python -m cohbench.tasks suite --out runs/reader-suite-r1
uv run --isolated --python 3.12 python -m tests.pipeline.run runs/reader-suite-r1 runs/reader-pipeline-r1
uv run --isolated --python 3.12 python -m cohbench.scoring.calibration --config cohbench/scoring/configs/explabs-kimi-k3.1-pro.json --no-answer-config cohbench/scoring/configs/explabs-deepseek-v4.1-flash-uncensored.json --out runs/reader-large-r1
```

The endpoint key is read from the file named by `COHBENCH_READER_KEY_FILE`. The large command resumes its exact input plan and accepted cache; its ledger counts all attempts against the original cap. The command now names records by config digest and needs `--overwrite` to replace one, so a resumed run passes that flag. The two records committed here keep their earlier model names: `kimi-k3.1-pro.calibration.json` holds digest `dfcc9145ef59fa61c5739d62d4a958a6986391fc8abc77cfc39ff8a3f20ffa2c` and `deepseek-v4.1-flash-uncensored.calibration.json` holds `d3df0d929f7179fc904c6d7cb157f80324840896bb2865fa4342070246382acc`, each in its `reader` field. The release check reads the record embedded in a manifest, never a file by name. Published JSON contains aggregates, seeds, counts and exceptions, not authored answer text or machine paths. Detailed generated inputs, cache, call ledger and scores remain under ignored `runs/`.

Eight fresh Python 3.12 CLI replay processes, with both key variables removed and two hash seeds, reproduced saved score bytes exactly. Cache and ledger bytes remained unchanged. Post-rebase scoring tests passed 606 tests on each interpreter (71.19 seconds on Python 3.12, 71.12 seconds on Python 3.15). The slow pipeline checks passed 13 tests per interpreter (357.44 and 335.54 seconds respectively), including two complete 268-episode runs and byte-identical scores and reports. Both disposable slow workspaces and the unrelated generated run directories were removed; small verification summaries and Explanation bundles were retained.

```sh
uv run --isolated --python 3.12 --with pytest pytest tests/scoring -q -p no:cacheprovider
uv run --isolated --python 3.15 --with pytest pytest tests/scoring -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python 3.15 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python 3.12 --with pytest pytest tests/pipeline -q -p no:cacheprovider
uv run --isolated --python 3.15 --with pytest pytest tests/pipeline -q -p no:cacheprovider
```

Full unit suites each passed 3,786 tests, with 313 skipped and 49 deselected: 757.72 seconds on Python 3.12 and 737.36 seconds on Python 3.15. Tests used local or injected transports and no Docker. The final privacy and reader-limit documentation checks also passed.

The branch was rebased onto `main` at `a72e98c`, preserving its classifier status statement and updating the adjacent reader statement. No branch was pushed.

## Open problems

- Human majority agreement, disagreement spread across ranked agent models, and timed human readers are not measured. A release needs actual labels and a matching manifest. Synthetic attestations in report tests are not release evidence.
- The endpoint reports model and provider labels. Weight revision, quantization, parameter count and independent uncached determinism are not measured. Temperature zero and a seed do not establish reproducibility without the cache.
- DeepSeek's buried, ideal and worst inputs were not measured on this large sample. Its passing no-answer result alone cannot qualify it for release.
- The chosen six orders and constructed task population do not measure broader languages, alternative permutations or adversarial answers. Position selection is uneven despite acceptable true-cause rates.
- A process loss after a paid response but before its cache write can leave a paid request without a replayable verdict. Concurrent cache misses can also issue duplicate paid requests; their readers adopt the first stored vote.
- Legacy bundles without a main question still use their opening prompt, and bundles without classifier provenance cannot qualify for release.
