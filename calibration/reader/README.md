# Initial hosted reader development run, 2026-11-23

The [round-one record](round1/README.md) supersedes the provisional conclusion below with a fresh 200-instance calibration and the main-question rerun. The original aggregate now uses the same clipped smoke bounds as the production report; counts and choices are unchanged.

[Inference] This record includes a provisional model recommendation. Measurements describe this development run. Model behavior is not guaranteed.

The unchanged `reader-1` prompt and six balanced cyclic option orders, seed 0, were used for both models. Sampling was temperature 0, seed 20261123, 16 output tokens and reasoning effort `none`. Kimi reported provider `openrouter`; DeepSeek reported `experiential_cloud`. Neither reported a weight revision or system fingerprint.

A fresh 88-instance suite produced 268 scripted pipeline episodes, with zero failures and zero expectation mismatches. The hosted calibration used the ideal and worst bundles for all 24 Explanation instances: 12 ordinary tasks and 12 controls, across h20 and h80. Both answers were read for every instance. The production control score uses answer 1; a separate call to the production Explanation scorer supplies answer 2 and the sanity diagnostics for controls. Those extra diagnostics do not change production eligibility.

Before paid calls, the expectations were ideal accuracy near 100%, worst accuracy below 25%, no-answer accuracy near 1/6, buried-control accuracy below 25%, and at least 90% visible-attribution accuracy in each authored group. No prompt, option, label or sampling changes followed the measurements.

## Generated answers

All 24 Explanation instances. Ideal and worst columns each cover 48 answers and 288 votes. Sanity inputs are deduplicated by the production input key.

| Model                          |             Ideal |         Worst |       No answer |   Buried sanity | Both sanity checks |
| ------------------------------ | ----------------: | ------------: | --------------: | --------------: | ------------------ |
| deepseek-v4.1-flash-uncensored | 288/288 (100.00%) | 5/288 (1.74%) | 22/144 (15.28%) | 18/144 (12.50%) | True               |
| kimi-k3.1-pro                  | 288/288 (100.00%) | 8/288 (2.78%) | 31/144 (21.53%) | 32/144 (22.22%) | True               |

The production report uses the 12 non-control Explanation instances for its sanity verdict. Its current clipped two-standard-error smoke-check bound is 0% to 38.18%; its buried upper bound is 38.18%. The original report used a negative lower bound; clipping it to zero does not change the verdict. Passing this rule does not establish chance behavior.

| Model                          | Non-control ideal | Non-control worst |      No answer |  Buried sanity |
| ------------------------------ | ----------------: | ----------------: | -------------: | -------------: |
| deepseek-v4.1-flash-uncensored | 144/144 (100.00%) |     0/144 (0.00%) |   1/72 (1.39%) |   0/72 (0.00%) |
| kimi-k3.1-pro                  | 144/144 (100.00%) |     2/144 (1.39%) | 11/72 (15.28%) | 12/72 (16.67%) |

The all-instance diagnostic uses 24 distinct sanity inputs. Its no-answer bound is 1.45% to 31.88%; its buried upper bound is 31.88%. These are diagnostic extensions to the production report, which excludes controls from E.

### Variation over option orders

Correct votes in orders 0 through 5, over all 24 instances. Each order rotates the same seeded permutation. These votes are not independent samples.

| Model                          | Input     | Correct / total for each order           |
| ------------------------------ | --------- | ---------------------------------------- |
| deepseek-v4.1-flash-uncensored | buried    | 5/24, 2/24, 1/24, 3/24, 2/24, 5/24       |
| deepseek-v4.1-flash-uncensored | ideal     | 48/48, 48/48, 48/48, 48/48, 48/48, 48/48 |
| deepseek-v4.1-flash-uncensored | no_answer | 4/24, 4/24, 4/24, 3/24, 3/24, 4/24       |
| deepseek-v4.1-flash-uncensored | worst     | 3/48, 0/48, 0/48, 0/48, 0/48, 2/48       |
| kimi-k3.1-pro                  | buried    | 7/24, 8/24, 5/24, 4/24, 4/24, 4/24       |
| kimi-k3.1-pro                  | ideal     | 48/48, 48/48, 48/48, 48/48, 48/48, 48/48 |
| kimi-k3.1-pro                  | no_answer | 5/24, 5/24, 5/24, 5/24, 6/24, 5/24       |
| kimi-k3.1-pro                  | worst     | 2/48, 2/48, 0/48, 0/48, 2/48, 2/48       |

### Every generated-answer exception

An exception here means an ideal answer not decoded as the true cause, or a worst answer decoded as the true cause. The pre-run worst expectation was a low aggregate rate, not necessarily zero.

- deepseek-v4.1-flash-uncensored, ideal: none.
- deepseek-v4.1-flash-uncensored, worst: `explanation-h20-s1006-control`, answer 1, orders [0, 5]; `explanation-h20-s1006-control`, answer 2, orders [0, 5]; `explanation-h80-s1006-control`, answer 2, orders [0].
- kimi-k3.1-pro, ideal: none.
- kimi-k3.1-pro, worst: `explanation-h20-s4-single`, answer 1, orders [1]; `explanation-h20-s4-single`, answer 2, orders [1]; `explanation-h20-s1006-control`, answer 1, orders [0, 4, 5]; `explanation-h20-s1006-control`, answer 2, orders [0, 4, 5].

## Forty authored answers

Ten generated h20 instances supplied unchanged questions and answer keys. The implementation agent authored four answers per instance before viewing hosted results. The groups are paraphrased true cause, red herring explicitly rejected before the true cause, true cause after word 80, and confidently wrong red herring. This is constructed development data, not a human-labelled holdout.

The buried group explicitly blames a red herring in the first 80 words and retracts it later. Its expected visible choice is the red herring. A neutral prefix would not determine a unique correct letter. Interpretation accuracy and true-cause selection are therefore separate measures.

The 40 full answers and their construction README remain in a directory outside the repository. No authored answer text is committed.

| Model                          | Group           | Correct visible attribution | True cause selected | Per-order attribution                    |
| ------------------------------ | --------------- | --------------------------: | ------------------: | ---------------------------------------- |
| deepseek-v4.1-flash-uncensored | buried          |             60/60 (100.00%) |        0/60 (0.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| deepseek-v4.1-flash-uncensored | negated_herring |             60/60 (100.00%) |     60/60 (100.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| deepseek-v4.1-flash-uncensored | paraphrase      |             60/60 (100.00%) |     60/60 (100.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| deepseek-v4.1-flash-uncensored | wrong           |             60/60 (100.00%) |        0/60 (0.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| kimi-k3.1-pro                  | buried          |             60/60 (100.00%) |        0/60 (0.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| kimi-k3.1-pro                  | negated_herring |             60/60 (100.00%) |     60/60 (100.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| kimi-k3.1-pro                  | paraphrase      |             60/60 (100.00%) |     60/60 (100.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |
| kimi-k3.1-pro                  | wrong           |             60/60 (100.00%) |        0/60 (0.00%) | 10/10, 10/10, 10/10, 10/10, 10/10, 10/10 |

Every authored miss:

- deepseek-v4.1-flash-uncensored: none.
- kimi-k3.1-pro: none.

## Calls, tokens and latency

Every attempt used the shared ledger with a hard cap of 2,500 calls. Accepted verdicts were cached, including reported usage and latency. Cache hits issue no endpoint request. Mean, median and p95 latency measure accepted choices, include retries and backoff, and exclude subsequent accounting flush and cache fsync. Input tokens include any provider-reported cached input; output tokens are reported completion tokens.

| Model                          | Phase     | Attempts | Accepted choices | Input tokens | Output tokens | Mean / median / p95 seconds |
| ------------------------------ | --------- | -------: | ---------------: | -----------: | ------------: | --------------------------- |
| deepseek-v4.1-flash-uncensored | authored  |      241 |              240 |       86,988 |         1,680 | 1.359 / 1.103 / 2.686       |
| deepseek-v4.1-flash-uncensored | generated |      794 |              792 |      510,078 |         5,544 | 1.369 / 1.135 / 2.649       |
| kimi-k3.1-pro                  | authored  |      240 |              240 |       99,480 |         1,440 | 1.545 / 1.253 / 3.043       |
| kimi-k3.1-pro                  | generated |      792 |              792 |      541,608 |         4,752 | 1.518 / 1.273 / 2.906       |

Total attempts: 2067/2500.

- deepseek-v4.1-flash-uncensored: 1035 attempts, 1032 accepted choices; outcomes {"accepted": 1032, "transport_error": 3}. Calls without measured usage: [99, 383, 1062].
- kimi-k3.1-pro: 1032 attempts, 1032 accepted choices; outcomes {"accepted": 1032}. Calls without measured usage: [].

Failed transports supplied no usage. Their token counts and exact provider billing are not measured. No request was made to GLM.

## Recommendation and open problems

[Inference] Use `explabs-kimi-k3.1-pro.json` for the next development run of
ordinary Explanation tasks. Both models decoded all ideal and authored answers,
but Kimi's no-answer rate on the actual E population was 11/72, 15.28%, against
1/72, 1.39%, for DeepSeek. Kimi is the closer match to the nominal 1/6 there.
DeepSeek's all-instance rate is close to chance because its controls contribute
21/72, 29.17%; the aggregate conceals that population difference. Kimi's control
rate was 20/72, 27.78%, so it also needs a larger null-input check.

DeepSeek was faster, used fewer reported input tokens and selected the true
cause less often from buried inputs in the all-instance diagnostic. Kimi's
all-instance buried rate was 32/144, 22.22%, versus DeepSeek's 18/144, 12.50%.
Those tradeoffs make this a provisional choice, not a release approval. Model
behavior is not guaranteed. Neither reader has the human calibration required
by the specification.

The ideal, worst and authored measurements met the pre-run expectations. The
all-instance buried rates were below 25%. DeepSeek's ordinary-task no-answer
rate did not match the expected vicinity of 1/6, despite passing the report's
broad rule. The control-only buried rates were 27.78% for Kimi and 25% for
DeepSeek; that narrower population also warrants more examples.

- The model labels and provider names were checked on every accepted response. I cannot verify the hosted weight files, their revision, parameter count, quantization or license from these responses. A model name and a config digest do not pin weights.
- Cached replay was byte-identical. Independent uncached determinism was not measured. Temperature zero and a fixed seed do not establish that property.
- No-answer behavior depends on this small task population. The production bound is too broad to reject zero accuracy with only 12 distinct items. Larger independent calibration is still needed.
- Human majority agreement, timed human readers and disparity across ranked agent models are not measured. The root specification requires these before release.
- Only six cyclic orders were measured. Additional permutations, prompt-injection answers and a human-labelled holdout are not measured.
- A process loss between accepting a paid response and persisting its cache row can leave a paid call without a replayable verdict. The cache is not a transaction with the hosted service.

## Verification and reproduction

Commands run without Docker:

```sh
uv run --isolated --python 3.12 python -m cohbench.tasks suite --out runs/reader-suite
uv run --isolated --python 3.12 python -m tests.pipeline.run runs/reader-suite runs/reader-pipeline
uv run --isolated --python 3.12 python -m calibration.reader.run runs/reader-pipeline "$READER_AUTHORED_ANSWERS" runs/reader-hosted --key-file "$COHBENCH_READER_KEY_FILE"
uv run --isolated --python 3.12 python -m calibration.reader.summarize runs/reader-hosted calibration/reader/2026-11-23.json
uv run --isolated --python 3.12 --with pytest pytest tests/scoring -q -p no:cacheprovider
uv run --isolated --python 3.15 --with pytest pytest tests/scoring -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python 3.15 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
```

Final scoring suites: 564 passed on each interpreter. Full unit suites: 3,741 passed, 313 skipped and 49 deselected on each interpreter. The two added regression tests were included in the final scoring reruns. Tests use fake or local endpoints.

Every generated bundle was validated before scoring and scored again through the recorded reader with identical output bytes. Eight CLI replays in fresh Python 3.12 processes, using two hash seeds, matched their reference score files byte for byte. Cache and ledger bytes stayed unchanged. The cache requirement and explicit non-release override remain covered by tests. Detailed bundles, scores, cache and the shared ledger remain under ignored `runs/reader-hosted` and `runs/reader-pipeline`. Disposable Routing, Steering and grace run directories were removed.

The [aggregate JSON](2026-11-23.json) holds all ratios and order counts. The
[manifest](manifest.json) records the suite and private-answer file hashes and
the implementation commit.
