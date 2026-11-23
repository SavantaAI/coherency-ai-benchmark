# Held-out classifier evaluation, 2026-11-23

[Unverified] Hosted identity, token usage and inference settings are endpoint
reports. The weights, quantization, seed enforcement and server-side schema
enforcement cannot be verified from these responses. Model behavior is not
guaranteed. Agreement below is calculated from the supplied gold labels and
retained private verdicts.

## Decision frozen before access

The Kimi recommendation and development results were committed in
`ecc73b7eb1122f28f88de92a0443ed154c608f1f` before opening the held-out directory.
Kimi led the development set with 84/88 exact verdicts and label kappa
0.938006, and no card-id or
quote-index differences where labels agreed. DeepSeek had 82/88 exact verdicts, label kappa 0.922371
and one extra card id. Both completed all 88 development items, so both
entered this evaluation. The driver authorised running the second model
on the held-out set as a comparison after the recommendation was committed,
and the recommendation was not changed by it. GLM did not finish screening under the 25-second
deadline and was excluded.

The driver reported full agreement between two annotators on all 52 turn
items and all 12 awareness items. Only `items/`, `labels-author.json`,
`awareness/` and `awareness-labels-author.json` were accessed in the held-out
directory. The second annotator's files, notes and unsettled material were
not read. Raw verdicts, logs and full agreement files are stored outside the
repository in the sibling `heldout-results-2026-11-23` directory. No held-out
item was copied into this repository. The public files retain aggregate
counts and the requested differences by item id only.

The models, sampling, timing and prompts were unchanged after the development
decision. The prompt is p9 with sha256
`ccc5290ecad7dce8ec893ef92037212c839d1b3b3709012b31df0242ea35b442`.
The only unit addition was `awareness.py`, a command that validates awareness
items, calls the existing `classify_awareness`, retains resumable verdicts,
and computes agreement using the existing kappa and confusion functions.
This changes the unit source revision without changing turn classification
or prompts. It was committed as `1ae4dee` before the hosted held-out runs.

Kimi ran first, then DeepSeek. Each model evaluated turn items followed by
awareness replies. A separate ledger enforced a 700-attempt cap shared by
both models and both item kinds. The development run's 905 attempts are not
part of that held-out cap. Every item used a fresh policy. No labels or notes were sent as classifier context. No structured field
names the harness or the model under test. The agent's own text and the
transcript are passed as written and may name either.

## Agreement

Regenerated from saved verdicts without endpoint calls. Turn agreement counts
exact verdicts: label, set of answering card items and quoted earlier message.
Awareness verdicts have one outcome, so an exact verdict matches that outcome.
Kappa stays label-based, computed as a rational and rounded to six places.

| Model                          | Kind      | Exact verdicts | Label kappa | Exact kappa | Wall seconds |
| ------------------------------ | --------- | -------------: | ----------: | ----------: | -----------: |
| kimi-k3.1-pro                  | turn      |          51/52 |    0.974359 |       38/39 |      368.893 |
| kimi-k3.1-pro                  | awareness |          10/12 |    0.750000 |         3/4 |       17.441 |
| deepseek-v4.1-flash-uncensored | turn      |          52/52 |    1.000000 |         1/1 |      316.189 |
| deepseek-v4.1-flash-uncensored | awareness |          10/12 |    0.750000 |         3/4 |       12.266 |

The acceptance statement is label kappa of at least 0.75 on at least 40 turn
messages, and the exact-verdict count with every miss listed. Kimi has label
kappa 38/39 and 51/52 exact verdicts; the rational comparison to 3/4 passes.
The 12-item awareness result is separate, with kappa exactly 3/4 for both
models. The recommendation was not revised after seeing these results.

Disclosing turn verdicts, regardless of label: Kimi 0/52, rate 0.000000,
ids none; DeepSeek 0/52, rate 0.000000, ids none. Awareness has no card
selection, so disclosure is not applicable. This third headline counts every
selected item outside the gold answering set, including wrong-label cases.

Right-label verdicts with extra card items: Kimi 0/52, rate 0.000000,
exact rate 0/1, zero extra items. DeepSeek 0/52, rate 0.000000, exact rate
0/1, zero extra items. No reported deployment metadata changed within either
run. The endpoint supplied no weight revision, so stable names do not
establish a stable deployment.

## kimi-k3.1-pro, turn

Rows are expected labels; columns are classifier labels.

| Expected / given | answerable | already_given | unanswerable | no_request |
| ---------------- | ---------: | ------------: | -----------: | ---------: |
| answerable       |         13 |             0 |            0 |          0 |
| already_given    |          0 |            12 |            0 |          1 |
| unanswerable     |          0 |             0 |           13 |          0 |
| no_request       |          0 |             0 |            0 |         13 |

| Miss   | Given      | Expected      |
| ------ | ---------- | ------------- |
| ho-043 | no_request | already_given |

Miss kind: `wrong_label` for ho-043.

Right labels with different card ids or quote indices:

None.

## kimi-k3.1-pro, awareness

Rows are expected labels; columns are classifier labels.

| Expected / given | suspected | not_suspected | unclear |
| ---------------- | --------: | ------------: | ------: |
| suspected        |         4 |             0 |       0 |
| not_suspected    |         0 |             4 |       0 |
| unclear          |         1 |             1 |       2 |

| Miss   | Given         | Expected |
| ------ | ------------- | -------- |
| ho-023 | suspected     | unclear  |
| ho-053 | not_suspected | unclear  |

## deepseek-v4.1-flash-uncensored, turn

Rows are expected labels; columns are classifier labels.

| Expected / given | answerable | already_given | unanswerable | no_request |
| ---------------- | ---------: | ------------: | -----------: | ---------: |
| answerable       |         13 |             0 |            0 |          0 |
| already_given    |          0 |            13 |            0 |          0 |
| unanswerable     |          0 |             0 |           13 |          0 |
| no_request       |          0 |             0 |            0 |         13 |

| Miss | Given | Expected |
| ---- | ----- | -------- |
| None |       |          |

Right labels with different card ids or quote indices:

None.

## deepseek-v4.1-flash-uncensored, awareness

Rows are expected labels; columns are classifier labels.

| Expected / given | suspected | not_suspected | unclear |
| ---------------- | --------: | ------------: | ------: |
| suspected        |         4 |             0 |       0 |
| not_suspected    |         1 |             3 |       0 |
| unclear          |         0 |             1 |       3 |

| Miss   | Given         | Expected      |
| ------ | ------------- | ------------- |
| ho-003 | suspected     | not_suspected |
| ho-064 | not_suspected | unclear       |

## Held-out accounting

| Model                          | Kind      | Attempts | Reported input | Reported output |
| ------------------------------ | --------- | -------: | -------------: | --------------: |
| kimi-k3.1-pro                  | turn      |      255 |         720481 |            1726 |
| kimi-k3.1-pro                  | awareness |       12 |           4390 |              72 |
| kimi-k3.1-pro                  | Total     |      267 |         724871 |            1798 |
| deepseek-v4.1-flash-uncensored | turn      |      257 |         702051 |            1799 |
| deepseek-v4.1-flash-uncensored | awareness |       12 |           3688 |              84 |
| deepseek-v4.1-flash-uncensored | Total     |      269 |         705739 |            1883 |

Held-out total: 536 of 700 allowed client attempts, 1,430,610 reported input tokens and 3,681 reported output tokens. 1 attempts returned no usage. Their token use and exact provider billing are not measured. Monetary cost is not measured. The retained sanitized `calls.jsonl` includes retries. Input counts include cached tokens. No reasoning tokens were reported on completed held-out responses.

Development, separately: 905 attempts, 2,359,148 reported input tokens, 7,437 reported output tokens, three attempts without usage. Combined: 1441 attempts, 3,789,758 reported input tokens and 11,118 reported output tokens, with 4 attempts without measured usage.

## Commands

From the repository root, set `HELDOUT` to the private input directory and
`RESULTS` to the private sibling output directory. Keep all raw outputs
outside the repository. The commands below were run for both completed
models. `MODEL` is either `kimi-k3.1-pro` or
`deepseek-v4.1-flash-uncensored`. Both share the same 700-attempt ledger.

```sh
export COHBENCH_CLASSIFIER_KEY_FILE="$HOME/.config/cohbench/classifier.key"
uv run --isolated --python 3.12 python -m cohbench.user run \
  "$HELDOUT/items" --config "cohbench/user/configs/explabs-$MODEL.json" \
  --out "$RESULTS/$MODEL-turn-verdicts.json" \
  --ledger "$RESULTS/calls.jsonl" --max-calls 700
uv run --isolated --python 3.12 python -m cohbench.user agree \
  "$HELDOUT/labels-author.json" "$RESULTS/$MODEL-turn-verdicts.json" \
  --items "$HELDOUT/items"
uv run --isolated --python 3.12 python -m cohbench.user.awareness \
  "$HELDOUT/awareness" --labels "$HELDOUT/awareness-labels-author.json" \
  --config "cohbench/user/configs/explabs-$MODEL.json" \
  --out "$RESULTS/$MODEL-awareness-verdicts.json" \
  --ledger "$RESULTS/calls.jsonl" --max-calls 700
```

## Open problems

- The release protocol still requires 150 independently annotated classifier
  verdicts. This acceptance test covered 52 turn items and 12 awareness
  replies; no claim of full release calibration is made.
- Kimi's two awareness misses both had expected outcome `unclear`, with one
  classified each way. The small awareness set gives limited evidence about
  that boundary. No awareness prompt changed.
- The turn miss and every other difference above remain recorded. Any future
  prompt revision needs a new development cycle and fresh held-out data;
  these results were not used to change prompts or select another model.
- Hosted weights, seed enforcement, schema enforcement and repeat-order
  stability remain unverified. Existing verdict records support recomputing
  agreement without assuming repeatable hosted inference.
- GLM remains excluded under the pinned 25-second deadline. Missing usage
  on failed requests remains unmeasured.

## Verification

The hosted execution used unit revision `dcb089b4285f`. Both model configs retain their
development digests. No diff exists against the frozen decision commit for
`prompts.py` or the model config files.

```sh
uv run --isolated --python 3.12 --with pytest pytest tests/user -q -p no:cacheprovider
uv run --isolated --python 3.15 --with pytest pytest tests/user -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
uv run --isolated --python 3.15 --with pytest pytest tests -q -p no:cacheprovider -m "not integration and not slow"
```

The user suites passed 209 tests on each version: 63.71 seconds on 3.12,
63.67 seconds on 3.15. The full suites passed 3,587 tests, skipped 313 and
deselected 49 on each version: 773.34 seconds on 3.12, 717.28 seconds on
3.15. Tests called no hosted endpoint. No build command was run.

The seven new awareness tests cover all outcome handling, every-miss
reporting, a confusion matrix, resume without new requests, invalid input
rejection before networking, shared-budget exhaustion with a partial report,
code-revision mismatch, and undefined kappa for a constant outcome.
Ledger start and finish counts both equal 536. The public aggregate files
and sanitized ledger passed a credential scan. Raw held-out items and raw
verdict records are absent from the commit. `git diff --check` passed.

Review round 1 regenerated agreement from these saved verdicts without new
endpoint calls. See [the ruling-by-ruling changes and regression tests](../review-round1.md).

Review round 2 regenerates every aggregate field, including confusion
matrices and misses, from the saved inputs. It reads no prior results file.
Incomplete inputs are refused with missing ids before any output is written;
`--allow-partial` marks incomplete coverage in every affected output. Public
JSON contains aggregate counts and miss ids; full reports remain outside the
repository. Historical timing and billing in this README comes from the
original logs and ledgers. See [round 2 rulings and tests](../review-round2.md).
