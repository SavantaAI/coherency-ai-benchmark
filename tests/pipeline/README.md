# Scripted suite self check

`run.py` takes a generated suite and a new output directory. It runs all
88 instances with the ideal and worst scripts and the 44 long horizon grace
scripts. Each run uses the real runner, scripted engine, generated tools and
metric. `episode.py` records the simulated harness's messages and tool results,
then calls the production trajectory normalizer, bundle maker, writer,
readback validator and scoring CLI entry. No bundle field is manufactured by
the pipeline and validation is never skipped.

The simulated clock advances without waiting for the scripted user delays.
The scripts still execute local shell commands and metric subprocesses.
The existing fake watcher batches their writes. A full pass took about one
minute on 2026-11-22. The slow pytest checks run the suite twice and compare
all score files and report files byte for byte. Run time under load varies.

```sh
uv run --isolated --python 3.15 python -m cohbench.tasks suite --out runs/self-check-suite
uv run --isolated --python 3.15 python -m tests.pipeline.run runs/self-check-suite runs/self-check
uv run --isolated --python 3.15 --with pytest pytest tests/pipeline -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests/pipeline -q -p no:cacheprovider
```

`results/` contains the two models, `scripted-ideal` and `scripted-worst`, in
the `simulated-pi` column. `results-report/` uses the original `suite.json`
plan. `grace/` and `grace-report/` hold the ideal grace variant against the
long horizon subset of that plan. A report does not permit replicates of a
task in one configuration, so grace runs cannot also appear in the main cell.
`runs.json` records every attempted episode, including rejected exports.
`checks.json` lists exact expected and observed values for all 220 runs.
`timing.json` records total wall time. Pytest keeps each pair of full runs
under a unique `runs/self-check-*` directory. Missing scores are JSON null and mean
not measured. Failed bundles remain beside their raw inputs for diagnosis.

The intended exact endpoints are Routing pass 1 or 0, Explanation accuracy
1 or 0 on each of two answers, and Steering g, c and l all 1 or all 0. This
includes the no valid improvement variant's ideal g and c defaults of 1.
Ideal agents pass all controls. Worst Routing agents choose the loser and
ask an unanswerable question, worst Explanation controls name a red herring,
and worst Steering controls increase round trips. All worst controls should
fail. Ordinary Routing no-conflict items are checked separately from R.

The reader is the production scripted `overlap` rule, with six option orders
and seed 0. It is a lexical test utility, not a semantic reader. Its measured
Explanation results do not meet the intended script endpoints and its buried
cause sanity check fails. The report therefore withholds E and the headline.
This is kept as an acceptance failure, not replaced with a table of desired
answers. The long horizon grace runs also currently fail bundle validation
because their reset wait has no recorded hand-back. Fresh runs also get
different opaque scorer ids from the runner, so byte determinism fails. See `FINDINGS.md` for
reproductions and locations. Tests are deliberately strict and not xfailed.

This does not measure a real harness, a real model, wall time calibration of
the task horizons, or hosted reader quality. Tools and metric subprocesses
run locally, outside Docker. The recorder supplies T3-shaped raw records;
it does not test T3 itself or the driver's capture code. The fake watcher
uses its own batching and file modes. No model API or key file is used.

Part B is gated on Part A passing. With the current acceptance failures, the
three Docker episodes have not run and their times and scores are not measured.
