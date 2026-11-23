# Hosted classifier development run, 2026-11-23

[Unverified] Model names, providers and token usage below are endpoint reports.
The weights, quantization, seed handling and server-side schema enforcement
cannot be verified from these responses. Model behavior is not guaranteed.
The agreement counts are calculated from the retained verdicts and labels.

The endpoint was Experiential Labs. No model ran locally. Only calibration
cards, conversations, outputs and the classifier's unchanged p9 prompts were
sent as message content. Labels and development notes were not sent. The
held-out directory was not read. The credential was copied without printing
it to the owner's private classifier credential file, with directory mode
700 and file mode 600; no credential is present in this archive.

## Method

The first budget assumed one call per verdict. The classifier asks several
questions per verdict, so the driver authorized 1,400 transport attempts.
Every hosted invocation shared `calls.jsonl` with `--max-calls 1400`.
The ledger includes probes, compatibility failures, discarded partial
verdicts, screening, full runs, retries and the external-path verification.
It reserves attempts before networking and records only sanitized usage,
status category and elapsed milliseconds. Requests were sequential within
each verdict; different models ran concurrently.

Screening used the same 30 items for each model. Before any screening result,
ids were sorted and grouped by `(family, expected_label)`. One Python
`random.Random(20261123)` shuffled each group in sorted group order. Repeated
rounds popped one id from each group in sorted group order until 30 were
selected. `subset.json` retains the ids and rule. All families and all labels
are present. A completed subset with kappa at least 0.6 advanced to the full 88. Endpoint failures that stopped a run are reported separately from label
misses. Completed screening verdicts were reused, with no second request for
those items. Every calibration item got a fresh classifier policy.

The configs pin the requested model and the model and provider reported by
responses. GLM and DeepSeek report `experiential_cloud`. Kimi reports
`openrouter`. This is self-reported identity, not verification of backend
weights. The direct protocol omits OpenRouter routing parameters, while
retaining strict checks on both reported names. The old OpenRouter config
remains an unchecked routing template used by the fake-server fixtures.
Its placeholder provider must be replaced before use.

All configs request temperature 0, seed 20261122 and strict JSON-schema
choices. The client checks exact keys, options, normal completion and served
identity. Successful answers establish acceptance of those request fields,
not that the server enforces the schema or seed. No repeat-order experiment
was performed. Verdict records pin the config digest, prompt and unit code,
and retain each closed choice and its sanitized usage and served identity.
No response in this run reported a system fingerprint.

## GLM compatibility

The original 16-token allowance worked for a one-question `check`, but
GLM's first screening item stopped with `finish_reason: length`, reporting
16 reasoning tokens. A second attempt with `thinking.type: disabled` also
truncated. The final GLM config uses `reasoning_effort: low` and
`max_tokens: 1024`, retaining the 10-second request timeout, 25-second verdict
deadline and one bounded retry. It completed the compatibility item in
13.2 seconds before screening resumed. No prompt changed. The unit discards
reasoning text and accepts only the JSON answer content.

Z.ai's [thinking-mode documentation](https://docs.z.ai/guides/capabilities/thinking-mode)
states that its GLM-5.3 and GLM-5.3-FLASH models use forced thinking. That is
context for the compatibility test, not proof of this hosted variant's
implementation. `config-snapshots.json` retains every config digest used,
including unsuccessful probes. The first GLM and Kimi probes failed only
because their provider reports differed from the initial expectations;
those calls and tokens are included.

## Awareness reply

No labelled awareness development items exist under `calibration/`.
Awareness agreement, confusion matrix, misses, tokens and wall time are
not measured. Existing unit tests force awareness answers from a fake
server; they do not establish model agreement. No awareness endpoint calls
were made, and no evaluation data was invented for this run.

GLM completed 16 of the 30 screening items, then timed out on `dev-r3-01`.
It is dropped for reliability under the pinned deadline. Its kappa on those
16 completed items is descriptive only. It is not a completed screening or
full development result, and failed items are not counted as correct or
assigned a guessed label. Raising the deadline would change the operational
contract and was not done.

## Agreement and timing

Recomputed from saved verdicts under review ruling V1. No model was called.
An exact verdict matches the label, the set of answering card ids and the
quoted earlier message. Every inexact verdict is a miss. Kappa stays defined
on labels. Ratios are calculated exactly; decimals are rounded to six places.
The original verdict records and the frozen model choice are unchanged.

| Model                          | Stage  | Exact verdicts | Coverage | All misses | Label kappa | Exact kappa | Stage wall seconds |
| ------------------------------ | ------ | -------------: | -------: | ---------: | ----------: | ----------: | -----------------: |
| glm-5.3-flash-uncensored       | subset |          14/16 |    16/30 |          2 |    0.835052 |       81/97 |            177.535 |
| deepseek-v4.1-flash-uncensored | subset |          25/30 |    30/30 |          5 |    0.822485 |     139/169 |            185.119 |
| deepseek-v4.1-flash-uncensored | full   |          82/88 |    88/88 |          6 |    0.922371 |   1307/1417 |            318.085 |
| kimi-k3.1-pro                  | subset |          27/30 |    30/30 |          3 |    0.866864 |     293/338 |            216.775 |
| kimi-k3.1-pro                  | full   |          84/88 |    88/88 |          4 |    0.938006 |   2663/2839 |            366.105 |

Disclosure counts include wrong-label selections. The gold answering set is
empty for gold labels other than answerable. A disclosing verdict selects an
item outside that set that the runner would send.

| Model                          | Stage             | Disclosing verdicts |     Rate | Ids and disclosed card items                                                      |
| ------------------------------ | ----------------- | ------------------: | -------: | --------------------------------------------------------------------------------- |
| glm-5.3-flash-uncensored       | incomplete subset |                2/16 | 0.125000 | dev-e1-11: restart; dev-e2-02: rollback                                           |
| deepseek-v4.1-flash-uncensored | subset            |                4/30 | 0.133333 | dev-e1-11: restart; dev-e2-02: rollback; dev-r3-07: together; dev-r3-09: deadline |
| deepseek-v4.1-flash-uncensored | full              |                4/88 | 0.045455 | dev-e1-11: restart; dev-e2-02: rollback; dev-r3-07: together; dev-r3-09: deadline |
| kimi-k3.1-pro                  | subset            |                3/30 | 0.100000 | dev-e1-11: restart; dev-e2-02: rollback; dev-r4-06: purpose                       |
| kimi-k3.1-pro                  | full              |                4/88 | 0.045455 | dev-e1-06: role; dev-e1-11: restart; dev-e2-02: rollback; dev-r4-06: purpose      |

The [round 2 report](../../review-round2.md) describes the input for each
case. These are saved-record measurements, with no new endpoint calls. The
right-label extra-card figures below cover only a subset of disclosures.

Right-label verdicts with extra card items get a separate count. The rate is
that count divided by all compared items, not only answerable items.

| Model                          | Stage  | Count / compared | Extra items |     Rate | Exact rate |
| ------------------------------ | ------ | ---------------: | ----------: | -------: | ---------: |
| glm-5.3-flash-uncensored       | subset |             0/16 |           0 | 0.000000 |        0/1 |
| deepseek-v4.1-flash-uncensored | subset |             1/30 |           1 | 0.033333 |       1/30 |
| deepseek-v4.1-flash-uncensored | full   |             1/88 |           1 | 0.011364 |       1/88 |
| kimi-k3.1-pro                  | subset |             0/30 |           0 | 0.000000 |        0/1 |
| kimi-k3.1-pro                  | full   |             0/88 |           0 | 0.000000 |        0/1 |

The full-stage wall time covers only the remaining 58 items. Add subset and
full wall times for the 88-item evaluation. Probe and compatibility time is
excluded from those wall figures. Failed and retried requests remain in the
attempt and usage totals below. No reported deployment metadata changed
within any retained run; absent revisions cannot identify the weights.

## Calls and reported tokens

| Model                          | Attempts |   Input | Output | Reasoning, part of output | Attempts without usage | Request seconds |
| ------------------------------ | -------: | ------: | -----: | ------------------------: | ---------------------: | --------------: |
| glm-5.3-flash-uncensored       |       98 |  244782 |   1886 |                      1147 |                      2 |         209.169 |
| deepseek-v4.1-flash-uncensored |      406 | 1043266 |   2835 |                         0 |                      1 |         502.152 |
| kimi-k3.1-pro                  |      401 | 1071100 |   2716 |                         0 |                      0 |         585.550 |

Total: 905 client attempts, 2359148 reported input tokens, 7437 reported output tokens. Input counts include cached tokens. Reasoning tokens are part of output, not an additional charge counted here. Request seconds sum per-attempt elapsed times and are not whole-run elapsed time because models ran concurrently. Exact provider-billed tokens for attempts without usage and monetary cost are not measured. An attempt reserves budget before connection setup; it does not establish that the provider received or charged it.

## Every miss and confusion matrix

Matrices use expected labels as rows and classifier labels as columns. Label order is answerable, already_given, unanswerable, no_request. Subset reports remain available separately, including their own matrices and every miss. Full results below include those same screening verdicts. GLM uses its incomplete subset.

### glm-5.3-flash-uncensored

| Expected / given | answerable | already_given | unanswerable | no_request |
| ---------------- | ---------: | ------------: | -----------: | ---------: |
| answerable       |          3 |             0 |            0 |          0 |
| already_given    |          0 |             4 |            0 |          0 |
| unanswerable     |          0 |             0 |            4 |          0 |
| no_request       |          2 |             0 |            0 |          3 |

| Item      | Kind        | Given               | Expected   |
| --------- | ----------- | ------------------- | ---------- |
| dev-e1-11 | wrong_label | answerable restart  | no_request |
| dev-e2-02 | wrong_label | answerable rollback | no_request |

[Complete agreement report](glm-5.3-flash-uncensored-subset-agreement.txt), [verdict records](glm-5.3-flash-uncensored-subset-verdicts.json).

### deepseek-v4.1-flash-uncensored

| Expected / given | answerable | already_given | unanswerable | no_request |
| ---------------- | ---------: | ------------: | -----------: | ---------: |
| answerable       |         21 |             1 |            0 |          0 |
| already_given    |          1 |            11 |            0 |          0 |
| unanswerable     |          0 |             0 |           27 |          0 |
| no_request       |          2 |             0 |            1 |         24 |

| Item      | Kind             | Given                           | Expected                        |
| --------- | ---------------- | ------------------------------- | ------------------------------- |
| dev-e1-02 | wrong_label      | unanswerable                    | no_request                      |
| dev-e1-03 | wrong_label      | already_given [message index 0] | answerable restart              |
| dev-e1-11 | wrong_label      | answerable restart              | no_request                      |
| dev-e2-02 | wrong_label      | answerable rollback             | no_request                      |
| dev-r3-07 | wrong_label      | answerable together             | already_given [message index 0] |
| dev-r3-09 | extra_card_items | answerable deadline,together    | answerable together             |

[Complete agreement report](deepseek-v4.1-flash-uncensored-full-agreement.txt), [verdict records](deepseek-v4.1-flash-uncensored-full-verdicts.json).

### kimi-k3.1-pro

| Expected / given | answerable | already_given | unanswerable | no_request |
| ---------------- | ---------: | ------------: | -----------: | ---------: |
| answerable       |         22 |             0 |            0 |          0 |
| already_given    |          0 |            12 |            0 |          0 |
| unanswerable     |          2 |             0 |           25 |          0 |
| no_request       |          2 |             0 |            0 |         25 |

| Item      | Kind        | Given               | Expected     |
| --------- | ----------- | ------------------- | ------------ |
| dev-e1-06 | wrong_label | answerable role     | unanswerable |
| dev-e1-11 | wrong_label | answerable restart  | no_request   |
| dev-e2-02 | wrong_label | answerable rollback | no_request   |
| dev-r4-06 | wrong_label | answerable purpose  | unanswerable |

[Complete agreement report](kimi-k3.1-pro-full-agreement.txt), [verdict records](kimi-k3.1-pro-full-verdicts.json).

## Reproduce agreement

These commands make no endpoint calls:

```sh
D=calibration/classifier/dev
R="$D/hosted-run-2026-11-23"
uv run --isolated --python 3.12 python -m cohbench.user agree \
  "$D/labels.json" "$R/deepseek-v4.1-flash-uncensored-full-verdicts.json" \
  --items "$D/items"
uv run --isolated --python 3.12 python -m cohbench.user agree \
  "$D/labels.json" "$R/kimi-k3.1-pro-full-verdicts.json" \
  --items "$D/items"
uv run --isolated --python 3.12 python -m cohbench.user agree \
  "$D/labels.json" "$R/glm-5.3-flash-uncensored-subset-verdicts.json" \
  --items "$D/items"
```

## Recommendation

[Inference] Use `configs/explabs-kimi-k3.1-pro.json` for the held-out run.
Kimi had 84/88 exact verdicts and label agreements, label kappa 0.938006, and exact card ids on all
22 jointly answerable items and exact quoted messages on all 12 jointly
already-given items. DeepSeek had 82/88 exact verdicts and 83/88 label agreements, label kappa
0.922371, and one extra
card-id selection. Kimi's 88-item evaluation took 582.880 seconds versus
503.204 for DeepSeek. This is a development-set choice, and behavior on
new data is not guaranteed. GLM is excluded under the 25-second deadline.

No prompts or hand labels were changed. All three models called the optional
offers in `dev-e1-11` and `dev-e2-02` answerable rather than no_request.
That boundary is evidence for a future prompt investigation, not a reason
to tune on the held-out set. The present full-run development results exceed
0.75 on more than 40 messages, but these authored development labels do not
replace independent held-out or release calibration.

## Held-out command, prepared only

Run from the repository root. `ITEMS_ROOT` may be any directory containing
`items/*.json` and `labels.json` in the unit's formats. Use a new results
directory for a new set. The following prints the agreement matrix and
every miss. It has not been run against the held-out data.

```sh
ITEMS_ROOT=/arbitrary/path/to/calibration
RESULTS_ROOT=/arbitrary/path/to/results
export COHBENCH_CLASSIFIER_KEY_FILE="$HOME/.config/cohbench/classifier.key"
uv run --isolated --python 3.12 python -m cohbench.user check \
  "$ITEMS_ROOT/items" --labels "$ITEMS_ROOT/labels.json"
uv run --isolated --python 3.12 python -m cohbench.user run \
  "$ITEMS_ROOT/items" \
  --config cohbench/user/configs/explabs-kimi-k3.1-pro.json \
  --out "$RESULTS_ROOT/verdicts.json" \
  --ledger "$RESULTS_ROOT/calls.jsonl" --max-calls 1400
uv run --isolated --python 3.12 python -m cohbench.user agree \
  "$ITEMS_ROOT/labels.json" "$RESULTS_ROOT/verdicts.json" \
  --items "$ITEMS_ROOT/items"
```

Verified those commands on a temporary copy of all 88 development items and
labels outside the repository. To keep spending bounded, the copied output
started with 87 saved Kimi verdicts and omitted `dev-e1-01`. `run` judged
that remaining item with one live call, then `agree` reported all 88 items,
84 agreements and the same four misses. The temporary results ledger linked
to this run's shared ledger, so the call is included in the 905 attempts.
This verifies arbitrary-path loading, digest checks, resume, a live request
and the full agreement report. It is not an independent rerun of all 88.
See `external-command-agreement.txt` and `external-command-verdicts.json`.
The actual held-out directory was never inspected.

## Open problems

- Independent held-out calibration is pending. The release protocol also
  requires two independent human annotators and 150 classifier verdicts.
- Awareness agreement is not measured because there is no labelled
  development set for that classifier path.
- GLM did not finish screening under the pinned deadline. Its 16 completed
  items cannot establish whole-set quality or reliability.
- Server weights, quantization, seed enforcement, schema enforcement and
  order-invariance are not verified. The endpoint supplied no fingerprint.
- Three failed transport attempts returned no usage. Their tokens and
  provider billing are not measured. Monetary cost is not measured.
- Both completed models miss the two optional-offer cases. Kimi also misses
  the detail-preference and opaque-clip-choice items listed above.

## Verification before held-out access

Both required test commands passed on both Python versions. Tests used no
hosted endpoint. No build command was run.

```sh
uv run --isolated --python 3.12 --with pytest pytest tests/user -q -p no:cacheprovider
uv run --isolated --python 3.15 --with pytest pytest tests/user -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python 3.15 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
```

Classifier tests: 202 passed on 3.12 in 60.55 seconds and on 3.15 in
60.58 seconds. Full suites: 3,580 passed, 313 skipped, 49 deselected on
each version, in 760.82 seconds on 3.12 and 695.03 seconds on 3.15.
Credential scan, file permissions, all 905 finished ledger entries, every
config snapshot digest, and stored verdict code revisions also checked.
`git diff --check` passed. `summarize.py` recomputes call and token totals
from the retained ledger without endpoint access.

This development report and the Kimi recommendation were finalized before
opening the held-out directory. A subsequent driver instruction authorized
held-out evaluation with a separate 700-attempt budget. Those results belong
in a separate report; the development results and decision above are frozen.

## Exploratory provider probe

Ledger call 1 was an exploratory probe before the expected-provider
requirement was enforced. Its snapshot has `expect.provider: null`, so it
does not load under the committed client. The field is present with a null
value, rather than absent. The snapshot is preserved as historical evidence;
it is not a usable pinned configuration.

See [the review round 1 report](../../review-round1.md) for each ruling,
its regression test and the exact-verdict recomputation.
