# Scripted suite self check

`run.py` takes a freshly generated suite and a new output directory. It runs
all 88 instances with the ideal and worst scripts, plus the 44 long horizon
grace scripts and 48 Steering variants. Each run uses the real runner, scripted engine, generated
tools and metric. `episode.py` records the simulated harness's messages and
tool results, then calls the production trajectory normalizer, bundle maker,
writer, readback validator and scoring CLI entry. Validation is never skipped.

The clock advances without waiting for user delays. The scripts still run
local shell commands and metric subprocesses. The slow pytest checks execute the suite twice
per interpreter and compare every score and report file byte for byte.
Runtime varies with load. On 2026-11-22, the two-pass tests took 285.42
seconds on Python 3.12 and 260.06 seconds on Python 3.15. Detailed measured
times are in `FINDINGS.md`.

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
The report explicitly says it is not a release. The check does not supply
the full configuration details for release ranking; Python 3.15 also differs
from the release runtime of Python 3.12. Zero bootstrap resamples is also
a non-release option. Scripted reader reports publish no
rank bands. Tests assert the non-release status as well as the numeric values.

Steering logs come from `MiniPi.home`, outside the workspace. Each shell
call's newly appended lines are read after it executes. Only their real
epoch timestamps are translated into ordered slots within that call's
simulated one-second interval. The original log and an export mapping are
retained. The fake collection port captures each segment and concatenates
them across resets. The real bundle maker receives those records and blobs.
Tests require one log observation per executed profile run and require the
loop score's selected observations to be exactly the log observations.
Controls have attempt-count diagnostics but no loop sub-score, so their
loop source is recorded as not measured.

`variants.py` derives two scripts from the worst script for each of the 24
Steering instances. One combines its three identical profile steps into one
shell call. The other filters out the result line from every profile output,
retaining the human-readable captures needed by the script. Each must keep
all nine profile log attempts and match its unmodified script exactly.
These 48 runs have scores in `variants/` and stay out of the two-model report.

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
five live cases include the four original ideal episodes and a long Steering
worst episode. Both live Steering cases require collected log observations
to decide the loop score. Long episodes also check the performed reset.
Results are written after each episode to `results.json`.
On 2026-11-22 all five passed, taking 82.4, 95.7, 106.7, 218.3 and 302.7
seconds in the module's case order. The live Steering ideal and worst
bundles held four and nine log attempts respectively; both loop scores
selected those log observations.

```sh
uv run --isolated --python 3.12 python -m tests.pipeline.real runs/self-check-ROOT/suite runs/self-check-ROOT/first runs/self-check-live
```

Replace `ROOT` with the directory from a passing pytest run. The module
checks the reference expectations, reports and determinism before opening
Docker. Its output directory must be new. It uses only the scripted API,
reads no key file, and relies on the episode environment to remove only its
own resources. See `FINDINGS.md` for the current measured results and limits.
