# Hosted classifier review, round 1

No endpoint calls were made for this review. The prompts, model configs,
original verdict records, calibration inputs and the two hosted call ledgers
are unchanged. Reports were recomputed from saved verdicts. Full held-out
reports and raw records remain outside the repository.

All 29 new regression cases in `tests/user/test_hosted_review1.py` failed on
the reviewed head before implementation. The cases now pass. The tests use
local stand-ins, including two real CLI processes for output contention.

| Ruling | Change                                                                                                                                                                                                                                                                                                          | Regression test                                                                                                               |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| B1     | New ledgers store their cap in the first line. A mismatched caller is refused with both values. `python -m cohbench.user.ledger set-cap FILE --old N --new M` appends an explicit cap-change event. Historical ledgers without a header remain read-only evidence.                                              | `test_b1_cap_is_persisted_and_changes_are_explicit`                                                                           |
| B2     | Both CLIs lock a sibling output lock file before reading the output and hold it through accounting flush and exit. Contending processes fail before an endpoint call. The lock inode is retained.                                                                                                               | `test_b2_two_processes_cannot_share_output`, turn and awareness cases                                                         |
| B3     | The caller bounds DNS, connect and response work by the attempt cutoff. Late worker results are discarded and cancellation is checked before sending an authenticated request. The accept/reject decision precedes asynchronous completion accounting; CLI flush runs outside the judgement deadline.           | `test_b3_abandons_stalled_setup`, lookup/connect and direct/runner cases; `test_b3_slow_accounting_does_not_reverse_decision` |
| B4     | Resume checks each entry's config, prompt hash, item-set digest, individual item digest and recorded code revision. A mismatch names the item and stops reuse. A truncated last ledger line consumes one unknown attempt, emits a warning and gets a recovery marker; malformed interior lines name their line. | `test_b4_resume_checks_each_entry`, both CLIs and five provenance fields; `test_b4_recovers_partial_tail_once`                |
| B5     | Label agreement, chance and kappa use `Fraction`. Reports include exact numerator/denominator pairs and six-place decimal values. Threshold checks use the rational.                                                                                                                                            | `test_b5_exact_rationals_and_threshold`                                                                                       |
| V1     | Exact verdicts headline agreement. Each miss carries one or more kinds. Correct-label extra-card cases have a separate count, item count and rate. Saved development and held-out reports were regenerated.                                                                                                     | `test_v1_exact_verdicts_are_the_headline`                                                                                     |
| S2     | The README now says the report contains matrices and labels of misses. The held-out report records the driver's authority for the comparison model and that it did not change the recommendation.                                                                                                               | `test_documentation_rulings`, both S2 cases                                                                                   |
| S3     | The development report identifies call 1 as exploratory, preceding required-provider validation, and says its snapshot does not load under the committed client. The snapshot was preserved.                                                                                                                    | `test_documentation_rulings`, S3 case                                                                                         |
| V2     | README and report state that no structured field names the tested harness or model. Agent text and transcript are passed as written and may name either. No sending rule changed.                                                                                                                               | `test_documentation_rulings`, both V2 cases                                                                                   |
| S1     | Sanitized reported revision fields are retained with the fingerprint. Accepted-response changes are recorded, and agreement reports compare metadata variants across each run. The Limits section says reported names do not pin weights and a release needs an identified weight revision.                     | `test_s1_reported_revision_is_recorded_and_changes_are_visible`; `test_documentation_rulings`, S1 case                        |
| V3     | The records section states that unredacted judged text can contain paths from inside the agent's container. No redaction was added.                                                                                                                                                                             | `test_documentation_rulings`, V3 case                                                                                         |

S3 cannot literally say the expected-provider field did not exist: the saved
snapshot contains `expect.provider: null`. The report says the exploratory
probe preceded enforcement of the required-provider rule, explicitly notes
that the field was null rather than absent, and records that the snapshot
cannot load under the committed client.

B3's platform limit is explicit: Python cannot cancel an in-progress platform
name lookup. The caller abandons it at the cutoff, its daemon may finish
later, and its result is not used. Completion writes are non-daemon and the
CLIs flush them before exit. Accounting failures are reported separately from
the deadline decision. The same transport path is used by the runner.

## Regenerated headlines

Kappa remains defined on labels. An exact verdict matches the label, the set
of answering card items and the quoted earlier message. Card-item order does
not change this headline; the existing whole-output comparison still reports
reply-order and quote-span differences separately when comparing two verdict
files.

| Model    |                      Development exact verdicts | Development label kappa | Held-out exact verdicts | Held-out label kappa |
| -------- | ----------------------------------------------: | ----------------------: | ----------------------: | -------------------: |
| Kimi     |                                           84/88 |    0.938006 = 2663/2839 |                   51/52 |     0.974359 = 38/39 |
| DeepSeek |                                           82/88 |    0.922371 = 1307/1417 |                   52/52 |       1.000000 = 1/1 |
| GLM      | 14/16 completed screening items; 16/30 coverage |        0.835052 = 81/97 |                 Not run |         Not measured |

Screening exact counts: Kimi 27/30, label kappa 293/338 = 0.866864;
DeepSeek 25/30, label kappa 139/169 = 0.822485. GLM did not finish screening.

The additional development miss is `dev-r3-09` for DeepSeek: the label is
correct, but the selected set contains the extra card item `deadline`.
Expected set: `together`. Given set: `deadline,together`. This changes the
headline from 83 label agreements to 82 exact verdicts. Correct-label
extra-card count is 1/88, rate 0.011364, one extra item. Both held-out runs
have zero correct-label extra-card cases, rate 0.000000.

Both awareness runs remain 10/12 exact outcomes, with kappa exactly 3/4,
serialized as 0.75. No gold label or classifier choice was changed.

Every miss and confusion matrix is in the regenerated
[development report](dev/hosted-run-2026-11-23/README.md) and
[held-out report](heldout-run-2026-11-23/README.md).

## Offline regeneration

```sh
uv run --isolated --python 3.12 python calibration/classifier/regenerate_agreement.py
uv run --isolated --python 3.12 python calibration/classifier/regenerate_agreement.py \
  --heldout-root "$HELDOUT_ROOT" \
  --private-results "$HELDOUT_RESULTS"
```

The first command regenerates development machine-readable reports and text
reports. The second also regenerates private held-out reports and the public
aggregate results. Neither command constructs a classifier or calls an
endpoint. Original verdict headers and records remain untouched.

## Open problems

Hosted names and unchanged fingerprints cannot establish an exact weight
revision. The driver raises identified deployment revisions with the owner.
The release protocol's larger independent calibration is still pending.
The four historical failed attempts without usage still have unmeasured
billing. This review adds no calls or measured tokens.

## Final verification

All four required suites passed on the reviewed implementation:

| Command scope                                                    | Python 3.12                                          | Python 3.15                                          |
| ---------------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------- |
| `tests/user -q -p no:cacheprovider`                              | 238 passed in 75.00 s                                | 238 passed in 74.91 s                                |
| `tests -q -p no:cacheprovider -m "not integration and not slow"` | 3,616 passed, 313 skipped, 49 deselected in 876.82 s | 3,616 passed, 313 skipped, 49 deselected in 806.23 s |

Each command used `uv run --isolated --python VERSION --with pytest pytest`.
No build command ran. All new regression cases failed on the old code before
changes: 29 failed in 14.10 seconds. The final suites include all 29 cases.
`git diff --check` passed. Markdown and generated JSON were formatted with
Prettier; Python changes were formatted with Ruff where edited.

A diff against `5bffd96` confirms no prompt, config, original verdict record
or archived hosted ledger changed. The private held-out ledger still equals
the committed 536-call copy; the development ledger remains at 905 calls.
Full held-out reports were regenerated only in the private results directory.
The public results contain aggregate counts and requested differences by id.
