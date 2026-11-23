# Classifier development set

88 items for tuning the simulated user's classifier (`cohbench/user`).
Written and labelled while the prompts were written, so they are not a
measure of agreement with people. The held-out set is kept elsewhere.

| Path                    | Holds                                                                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `items/*.json`          | One item each, in the `cohbench-classifier-item` format: the card as `judge` receives it, the transcript, the message                                                                                                                                                                                                                                                                                              |
| `labels.json`           | The author's labels (`cohbench-classifier-labels`)                                                                                                                                                                                                                                                                                                                                                                 |
| `notes.json`            | For each item, its task family and the case it tests. Not shown by the labelling tool                                                                                                                                                                                                                                                                                                                              |
| `local-run-2026-11-22/` | The local Qwen3.6-27B run of 2026-11-22: verdicts for prompts p1, p1-revised, p2, p3 and p4; the item set and labels that p1-revised to p4 ran against (`items/`, `labels-2026-11-22.json`); the config that produced p4; `calculations.py` and its output `calculations.txt`, which recompute p4 against the current labels and estimate tokens per verdict. That runtime is gone; the files are kept as evidence |

Families: 47 Routing, 19 Explanation, 22 Steering items. Eight invented cards: four Routing (batch jobs and a nightly slot, merging
customer exports, release packaging, wedding clips), two Explanation (a slow
build machine, a slow API server), two Steering (an import job measured by an
operation counter, a search service measured in queries per second). Each has
facts, including that the user cannot see the workspace, and one to three
decisions, some with `available_after` (those items appear on the card only
in items set after the event).

Labels: 22 answerable, 27 unanswerable, 27 no_request, 12 already_given
(after one correction during tuning, dev-r4-03, two in review round 1,
dev-e1-06 and dev-e2-02, and one in review round 2, dev-e1-11; see
`cohbench/user/README.md`). Items reworded in review so each settles its own
label: dev-r3-07, the R4 card and dev-r4-07 (round 1), the R2 card and
dev-r4-07 again (round 2), dev-e1-08 and dev-e2-08 (round 3), the R3 card
(a release-together decision, for dev-r3-09) and dev-r2-02 (final pass). Cases covered include: a message mixing an
answerable and an unanswerable ask; the same decision asked in the user's
terms and through private names; questions about something the opening
prompt said; repeats of answered questions; courtesy closings; confirmations
before destructive steps; status reports with question marks that ask
nothing; questions about a decision whose event has not fired; requests to
look at a workspace file; a request for a password; long transcripts with
filler tasks and a controlled reset.

Items marked "ambiguous" in `notes.json` are ones where a second label is
defensible.

```sh
python -m cohbench.user check calibration/classifier/dev/items --labels calibration/classifier/dev/labels.json
```

## Hosted run, 2026-11-23

The unchanged p9 prompts were measured on Experiential Labs. Screening used
30 fixed development ids stratified by family and label, seed 20261123.
Kimi and DeepSeek then completed all 88, reusing the screening verdicts.
GLM stopped after 16 screening items when the 25-second deadline expired.

| Model                          |                    Coverage | Exact verdicts | Label kappa | All misses |
| ------------------------------ | --------------------------: | -------------: | ----------: | ---------: |
| kimi-k3.1-pro                  |                       88/88 |          84/88 |    0.938006 |          4 |
| deepseek-v4.1-flash-uncensored |                       88/88 |          82/88 |    0.922371 |          6 |
| glm-5.3-flash-uncensored       | 16/30 screening, incomplete |          14/16 |    0.835052 |          2 |

Kimi misses: dev-e1-06, dev-e1-11, dev-e2-02, dev-r4-06.
DeepSeek misses: dev-e1-02, dev-e1-03, dev-e1-11, dev-e2-02, dev-r3-07.
GLM completed-item misses: dev-e1-11, dev-e2-02. DeepSeek also misses dev-r3-09 with the right label and an extra card item.
That is included in its six exact-verdict misses. Its right-label extra-card
count is 1/88, rate 0.011364, with one extra item; Kimi and GLM have none.

[Inference] Kimi is the recommended held-out config, based on its higher
development agreement and exact card-id and quote choices where labels
agree. Model behavior is not guaranteed. There is no awareness development
set, so awareness agreement is not measured. The held-out set was not read.

The run used 905 client attempts and reported 2,359,148 input and 7,437
output tokens. Three failed attempts supplied no usage; their tokens and
exact provider billing are not measured. No prompts or labels changed.

See [the full report](hosted-run-2026-11-23/README.md) for matrices, every miss,
per-model calls and timing, the held-out command and endpoint limitations.

The later [held-out evaluation](../heldout-run-2026-11-23/README.md) was
authorized after the development recommendation was committed. It changes
none of the development results above.

Review round 2 on 2026-11-23 regenerated saved verdicts without endpoint
calls or prompt changes. Kimi remains 84/88 exact with label kappa 0.938006;
DeepSeek remains 82/88 exact with label kappa 0.922371. Both have four
disclosing verdicts among 88, counting wrong-label selections as well as
right-label extras. GLM's incomplete screening has 14/16 exact, label kappa
0.835052 and 2/16 disclosures, with 16/30 coverage. Every disclosure id and
selected item appears in the [hosted report](hosted-run-2026-11-23/README.md)
and the [round 2 review record](../review-round2.md).
