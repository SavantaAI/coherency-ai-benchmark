# Scripted suite self check

`run.py` takes a freshly generated suite and a new output directory. It runs
all 88 instances with the ideal and worst scripts, plus the 44 long horizon
grace scripts. Each run uses the real runner, scripted engine, generated
tools and metric. `episode.py` records the simulated harness's messages and
tool results, then calls the production trajectory normalizer, bundle maker,
writer, readback validator and scoring CLI entry. Validation is never skipped.

The clock advances without waiting for user delays. The scripts still run
local shell commands and metric subprocesses. A single full pass took
93.635 seconds on 2026-11-22. The slow pytest checks execute the suite twice
per interpreter and compare every score and report file byte for byte.
Runtime varies with load.

```sh
uv run --isolated --python 3.15 python -m cohbench.tasks suite --out runs/self-check-suite
uv run --isolated --python 3.15 python -m tests.pipeline.run runs/self-check-suite runs/self-check
uv run --isolated --python 3.15 --with pytest pytest tests/pipeline -q -p no:cacheprovider
uv run --isolated --python 3.12 --with pytest pytest tests/pipeline -q -p no:cacheprovider
```

The tests retain their freshly generated suite and both executions under a
unique `runs/self-check-*` directory. `results/` holds `scripted-ideal` and
`scripted-worst` in one `simulated-pi` column. `results-report/` checks the
original `suite.json` plan. The grace variants use `grace/`, `grace-report/`
and the h80 subset of that plan because a cell permits one run per instance.
The self check runs Default mode only. It supplies a plan without the
require-plan flag, which also plans Coached mode for a release report.

`checks.json` compares exact values from `scripted/info.json.expected` with
real scores, Routing pairs and recorded resets. Explanation's episode
accuracy is additionally checked against the mean of its expected answers.
`runs.json` records every attempted episode. `timing.json` records wall time.
`determinism.json`, beside the two executions, compares their untouched score
and report bytes. A fixed opaque scorer id is derived from the task digest,
script, policy and configuration through `RunConfig.scorer_id`.

The reader is the production scripted `earliest_statement` rule with six
option orders and seed 0. It matches authored option phrases. It is not a
semantic model or a substitute for hosted reader calibration. Part A does
not measure a real harness, a real model, wall time calibration of task
horizons, T3 capture behavior, or watcher behavior in Docker. Local tools
run outside Docker. The fake watcher uses its own batching and file modes.

## Real harness check

After Part A passes, `real.py` uses the production adapter and Docker with
the scripted model API. It runs one short ideal script from each family and
one long Routing ideal script. It validates each export, rebuilds the saved
episode through the trajectory CLI on Python 3.12 with stderr discarded,
then compares each sub-score with Part A for that exact task digest. The
four live episodes took 82.546, 96.022, 101.163 and 216.346 seconds on
2026-11-22, and all four passed validation, rebuild and score comparison. The
long episode also checks the performed reset. Results are written after
each episode to `results.json`.

```sh
uv run --isolated --python 3.12 python -m tests.pipeline.real runs/self-check-ROOT/suite runs/self-check-ROOT/first runs/self-check-live
```

Replace `ROOT` with the directory from a passing pytest run. The module
checks the reference expectations, reports and determinism before opening
Docker. Its output directory must be new. It uses only the scripted API,
reads no key file, and relies on the episode environment to remove only its
own resources. See `FINDINGS.md` for the current measured results and limits.
