# Hosted classifier review, round 2

This review made no endpoint calls and added no tokens. Prompts, configs,
calibration inputs, original verdict records and the hosted call ledgers are
unchanged. Every reported statistic below was recomputed from saved labels
and verdicts. The frozen Kimi recommendation remains unchanged.

## Rulings and regression tests

All eight rulings have a regression that failed before implementation.
The initial run of `tests/user/test_hosted_review2.py` had 27 failures and
one passing case. The passing case was interruption after the first flush
when repairing an already truncated tail; the other creation and repair
interruption cases failed. The final user suites include all 28 cases.

| Ruling | Change                                                                                                                                                                                                                                                                                                                 | Regression test                                                                                                                                                   |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R2-B1  | Ledger creation and tail recovery write a same-directory temporary file, flush it, replace the old ledger and flush the directory. A stable sibling lock survives data-file replacement. A lone partial header is treated as not created. Each ordinary append is one write of one complete line.                      | `test_r2_b1_replacement_survives_each_interruption`, 15 cases across creation, partial headers and partial tails; `test_r2_b1_plain_append_is_one_complete_write` |
| R2-B2  | Lock acquisition is bounded by the attempt cutoff. The caller also bounds waiting for reservation flush. No acquired reservation means no request or consumed call. A ticket written before a slow flush is retained, and an explicit `not_sent` entry records a reservation whose transport did not start.            | `test_r2_b2_ledger_contention_does_not_reserve_or_send`; `test_r2_b2_slow_reservation_has_explicit_not_sent`                                                      |
| R2-B3  | The caller computes one absolute cutoff and passes it through reservation and transport. Worker startup delay is charged to that same cutoff, and late results are discarded.                                                                                                                                          | `test_r2_b3_worker_receives_original_absolute_cutoff`                                                                                                             |
| R2-B4  | Regeneration builds every statistic from the current saved inputs, including both confusion matrices and every miss. It reads no prior agreement or summary file. Missing ids cause failure before any report write; explicit partial mode includes incomplete coverage in public JSON, private JSON, text and stdout. | `test_r2_b4_missing_verdicts_refused_or_explicitly_partial`, four turn/awareness cases; `test_r2_b4_all_fields_come_from_current_inputs`                          |
| R2-B5  | Agreement treats null or non-list question records as having no questions.                                                                                                                                                                                                                                             | `test_r2_b5_null_questions_are_tolerated`                                                                                                                         |
| R2-B6  | `set-cap` requires a ledger with an existing complete header. It does not create the ledger.                                                                                                                                                                                                                           | `test_r2_b6_set_cap_requires_existing_ledger`                                                                                                                     |
| R2-S1  | The README limits `served_change` to accepted responses in one client session whose values remain distinguishable after withholding.                                                                                                                                                                                   | `test_r2_s1_documentation_scopes_reported_changes`                                                                                                                |
| R2-V1  | Disclosure is a separate headline, regardless of whether labels agree. Every answerable selection outside the gold answering set counts; a non-answerable gold label has an empty answering set. Right-label extras remain a subset. Reports list every disclosure id and selected item.                               | `test_r2_v1_disclosures_include_wrong_labels`                                                                                                                     |

Interruption tests inject process-loss-style exceptions after temporary-file
creation, file write, file flush, rename and directory flush. Each checks
that the destination retains the old bytes or contains the complete new
content, then retries successfully. The ledger still counts an interrupted
reservation conservatively. Process loss can leave an unfinished reservation;
no claim of complete accounting after a killed process is made.

The request deadline bounds the caller. Python cannot cancel a platform
lookup or a filesystem flush already in progress. A late worker result is
discarded; a reservation that completes without transport gets `not_sent`.
CLI exit waits for accounting outside the verdict deadline. An indefinitely
stalled filesystem can therefore still delay exit.

## Three headlines per model and set

An exact verdict matches label, answer-id set and quoted-message selection.
Kappa is computed on labels with exact rational arithmetic. Disclosure rates
use compared verdicts as the denominator; incomplete results are not full-set
estimates. The full development counts include reused screening verdicts.

### kimi-k3.1-pro, development subset

Exact verdicts: 27/30.

Label kappa: 0.866864, exactly 293/338.

Disclosing verdicts: 3/30, rate 0.100000. dev-e1-11: restart; dev-e2-02: rollback; dev-r4-06: purpose.

### kimi-k3.1-pro, development full

Exact verdicts: 84/88.

Label kappa: 0.938006, exactly 2663/2839.

Disclosing verdicts: 4/88, rate 0.045455. dev-e1-06: role; dev-e1-11: restart; dev-e2-02: rollback; dev-r4-06: purpose.

### deepseek-v4.1-flash-uncensored, development subset

Exact verdicts: 25/30.

Label kappa: 0.822485, exactly 139/169.

Disclosing verdicts: 4/30, rate 0.133333. dev-e1-11: restart; dev-e2-02: rollback; dev-r3-07: together; dev-r3-09: deadline.

### deepseek-v4.1-flash-uncensored, development full

Exact verdicts: 82/88.

Label kappa: 0.922371, exactly 1307/1417.

Disclosing verdicts: 4/88, rate 0.045455. dev-e1-11: restart; dev-e2-02: rollback; dev-r3-07: together; dev-r3-09: deadline.

### glm-5.3-flash-uncensored, development subset

INCOMPLETE: 16/30 items. No full 88-item run.

Exact verdicts: 14/16.

Label kappa: 0.835052, exactly 81/97.

Disclosing verdicts: 2/16, rate 0.125000. dev-e1-11: restart; dev-e2-02: rollback.

### kimi-k3.1-pro, held-out turn

Exact verdicts: 51/52.

Label kappa: 0.974359, exactly 38/39.

Disclosing verdicts: 0/52, rate 0.000000, ids none.

### kimi-k3.1-pro, held-out awareness

Exact verdicts: 10/12.

Label kappa: 0.750000, exactly 3/4.

Disclosing verdicts: not applicable; awareness has no card selection or disclosure ids.

### deepseek-v4.1-flash-uncensored, held-out turn

Exact verdicts: 52/52.

Label kappa: 1.000000, exactly 1/1.

Disclosing verdicts: 0/52, rate 0.000000, ids none.

### deepseek-v4.1-flash-uncensored, held-out awareness

Exact verdicts: 10/12.

Label kappa: 0.750000, exactly 3/4.

Disclosing verdicts: not applicable; awareness has no card selection or disclosure ids.

### Sets not run

GLM held-out turn and awareness: exact verdicts not measured; label kappa not measured; disclosing verdicts not measured, ids unavailable.

No labelled awareness development set exists for any of the three models. Exact verdicts and label kappa are not measured. Disclosure is not applicable to awareness.

The external-path command verification uses the same saved Kimi development records: 84/88 exact, label kappa 0.938006 and the same four disclosure ids. It is not a separate model evaluation.

## Every development disclosure and its input

These sentences describe the supplied item and saved selection. They do not
establish the model's internal reason for selecting an item.

Kimi:

- `dev-e1-06`, `role`: the agent asks for a preference between technical and plain explanations, while the card describes the user's non-sysadmin role but supplies no presentation preference; the saved verdict selects that role despite gold `unanswerable`.
- `dev-e1-11`, `restart`: after the cause has been explained and a summary written, the agent asks to restart the machine for confirmation, and the saved verdict selects the restart restriction despite gold `no_request`.
- `dev-e2-02`, `rollback`: the output explains the cause and then offers an immediate rollback, and the saved verdict selects the rollback permission and evening restriction despite gold `no_request`.
- `dev-r4-06`, `purpose`: the agent asks which music track to use, while the card supplies the wedding-slideshow purpose but no track choice; the saved verdict selects `purpose` despite gold `unanswerable`.

DeepSeek:

- `dev-e1-11`, `restart`: the restart offer follows a completed explanation and summary, and the saved verdict selects the restart restriction despite gold `no_request`.
- `dev-e2-02`, `rollback`: an explanation is followed by an offer to roll back now, and the saved verdict selects the rollback permission and evening restriction despite gold `no_request`.
- `dev-r3-07`, `together`: the opening message already asks for all three platforms, but the agent asks again which platforms to build and the saved verdict selects the separate shipping-together decision despite gold `already_given`.
- `dev-r3-09`, `deadline`: the agent asks whether to wait for macOS notarization and ship together or release platforms separately; the saved verdict selects both the relevant `together` decision and the extra `deadline` fact, although gold selects only `together`.

GLM, incomplete screening:

- `dev-e1-11`, `restart`: the restart offer follows the completed explanation and summary, and the saved verdict selects the restart restriction despite gold `no_request`.
- `dev-e2-02`, `rollback`: the output explains the cause before offering an immediate rollback, and the saved verdict selects the rollback permission and evening restriction despite gold `no_request`.

Both held-out turn runs have no disclosing ids. The right-label-extra subset
is unchanged: DeepSeek has one development case, `dev-r3-09`; Kimi and GLM
have none. This subset omits wrong-label disclosures and must not substitute
for the disclosure headline.

## Offline regeneration and retained evidence

```sh
uv run --isolated --python 3.12 python calibration/classifier/regenerate_agreement.py \
  --allow-partial \
  --heldout-root "$HELDOUT_ROOT" \
  --private-results "$HELDOUT_RESULTS"
```

`--allow-partial` is necessary for the archived GLM screening's 16/30
coverage. Without it, regeneration names its 14 missing ids and writes no
reports. Screening uses the fixed ids in `subset.json` as its intended set.
Omit both private paths to regenerate development alone.

Every statistical summary is rebuilt from the inputs. Original wall times
and billing are retained in the historical READMEs, logs and ledgers; they
are not carried forward from an old statistics object. The public held-out
JSON contains aggregate statistics and miss ids only. Full held-out reports
are regenerated in the private results directory. No raw held-out input or
verdict was copied into the repository.

The hosted ledgers remain at 905 development calls and 536 held-out calls.
This review adds zero calls and zero tokens. The original four failed
attempts without usage remain not measured.

## Open problems

- The recorded development disclosures need a new prompt-development cycle,
  particularly the distinction between a requested preference and related
  card context, requests after an explanation is complete, and questions
  already answered in the conversation. No prompt was changed here. Any
  revised prompt requires a fresh held-out set.
- Hosted weight revisions remain unidentified; reported names and sanitized
  fingerprints do not establish the deployed weights.
- The larger independent release calibration remains pending.
- Process loss can leave an unfinished reservation. Caller deadlines do not
  cancel an operating-system lookup or filesystem flush already in progress.

## Final verification

Implementation commit: `dff426c9c277167f7ed495a931b32fdd2d20293f`.
All four required suites passed:

| Scope                                                            | Python 3.12                                                | Python 3.15                                                |
| ---------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- |
| `tests/user -q -p no:cacheprovider`                              | 266 passed in 84.86 seconds                                | 266 passed in 84.60 seconds                                |
| `tests -q -p no:cacheprovider -m "not integration and not slow"` | 3,644 passed, 313 skipped, 49 deselected in 816.57 seconds | 3,644 passed, 313 skipped, 49 deselected in 784.90 seconds |

Each command used `uv run --isolated --python VERSION --with pytest pytest`.
The initial new-regression run was 27 failed and one passed in 39.39 seconds.
Every ruling had a failure before implementation. The final suites include
all 28 new regression cases. Tests used synthetic inputs and local stand-ins;
no hosted endpoint was called. No build command ran.

`git diff --check` passed. Markdown and generated JSON were formatted with
Prettier; edited Python files were formatted with Ruff. The diff against
`d043201` contains no prompt, config, original verdict, calibration input or
hosted call-ledger changes. The private held-out ledger still equals the
committed 536-call copy. Public matrices and exact-verdict counts reconcile
with their compared-item and miss counts, and right-label extras are a subset
of the disclosure ids.
