# Task generators

`cohbench.tasks.generate(Options(...))` builds an instance in memory.
`generate_to(options, directory)` writes it, and `suite.write_suite(directory)`
writes the 88-instance development suite plus `suite.json`. The CLI offers
`generate` and `suite`; its help lists the arguments. A destination must be
absent or empty. Seeds are integers from zero through `2**53`, inclusive.

The implementation follows SPEC.md and decisions D-033, D-039, D-044,
D-045, D-046 and D-047. The package uses only the Python standard library.
Generated workspace code and measurement code run on Python 3.11 and later.
Generation is tested on Python 3.12 and 3.15 with different hash seeds,
locales, time zones, working directories and output paths.

## Files and privacy

An instance has `task.json`, `workspace/`, `workspace.manifest.json`, and
host-only `scripted/`. Steering also has `metric/` and its manifest.
Only the workspace enters the agent container. The task, card, answer key,
policies, scripts and measurement files stay outside it. Every workspace
entry uses the fixed timestamp 2026-01-01 00:00:00 UTC; packing uses fixed
owners, content-required modes and depth-then-name entry order.

Each script has a run document and a user policy. `scripted/info.json`
holds test facts and `expected`, a mapping from script name to expected
scores. Routing has `pass`, and sibling instances also have `R`, the pair
result when both siblings use that script. Explanation has `answers`, two
zeros or two ones. Steering has `g`, `c`, `l` and `score`. Explicit controls
have `pass`. Every long script also has `reset`, either `hand_back` or
`grace`. These are assertions about the authored scripts, not labels a
model run receives.

## Shared design

The short horizon has 1, 0 and 1 chores for Routing, Explanation and
Steering; the long horizon has 10, 12 and 10. Budgets remain provisional:
20 or 60 minutes elapsed, 3 or 12 million tokens, and 10 or 20 minutes of
user wait. Return delays are seeded whole seconds from 30 through 90.
There are at most two reopens. The same seed uses the same draws across
horizons, siblings and controls.

Chores are single-result file tasks under `admin/`. Their instructions have
seeded phrasing. Long openings give the chores and the complete main job
in one message. The reset fires when either a seeded number of chore
results satisfy their content checks, or a main-job path first changes.
The threshold ranges from half the chores, rounded up, through three fewer
than all of them. Scratch writes do not count. Main paths are `out/**`,
`investigation/**`, and the Steering package respectively.

The notice and continuation retain the fixed D-046 wording. Ideal scripts
write the whole job and checked chore progress to `NOTES.md`; after the
reset they read it and finish remaining work. Worst scripts leave empty
notes and ask what they were doing. A grace variant continues after the
notice with paced steps until the runner interrupts it at 120 seconds.
Scripts pause between chores so the watcher can deliver the notice before
main work starts. These delays advance the fake clock without real sleeps.

The answer key's long-horizon `handoff` is an object with `event: handoff`
and `chores`, each with an `id` and a `{path, regex}` check. The same checks
accept LF or CRLF, an optional final newline, quoted or unquoted CSV
fields, and trailing spaces unless the chore asks to remove them. Notes
use those checks when ticking completed chores. Chore completion
is a diagnostic, not part of R, E or S. Each family's scored outcome also
depends on a requirement stated only in the opening, at both horizons.

### Routing

Seven domains provide four to six CSV files of three or four requests each.
Each source row has a unique allocation ticket. The register derives its
resource from the ticket and a rolling digest of earlier registrations;
the generator constructs tickets for the intended registration order.
Files are added in filename order. The tool reports opaque ids on each
addition and emits both its first clash message and the `CLASH` log on the
same step. A quarter of main seeds have no clash.

Sibling A's handbook says whether the earlier or later request keeps the
resource. B lacks that paragraph and needs the user decision unlocked by
the clash event. The event has no reset prerequisite: it unlocks the card
when the log shows the clash, even before the reset. An answer received
before the reset must then be carried across it. Everything else visible is identical. Controls name the
winner in their opening. Neither the initial CSV columns nor column pairs
contain duplicate claimant data that reveals the clash.

The opening also asks for a release note with specific text, absent from
workspace files. Ideal scripts write it; worst scripts omit it. The final
checks require the full exact assignment list, the winner, the waiting-list
entry, the source-file roster, and the release note. Text checks accept LF
or CRLF, a final newline or none, and trailing spaces. Controls use the same
checks. The register is parsed
as one JSON document and checked for its complete entries, holders, settled
clash, waiting list and source roster; the roster ignores ordering.
`decision_id` names the actual decision card item, including when the reply
was never delivered. `decision_reply`, `conflict_event`, `card_texts` and
`fixed_messages` retain their scoring interfaces.

#### Steps

1. Read the project notes and handbook, and complete the chores.
2. Register every source file in filename order.
3. When a clash appears, look up the claimants and apply A's rule or ask B's user using their names.
4. Resolve the clash, write the list, and write the release note requested in the opening.

### Explanation

Four machines have copied diagnostics and timing records, with one slow
machine. The question is only in the opening; workspace files describe the
records and never ask what to explain. The closing question has six seeded
phrasings. The follow-up remains `I don't follow. What's the actual reason?`
and is sent at the first hand-back, or the first after the long reset.

One pool of eight cause texts supplies every role. Six findings with
non-overlapping diagnostic groups are selected first; an independent draw
assigns one root cause, two red herrings and three distractors. The root
cause appears only on the slow machine, the red herrings on all four, and
the distractors nowhere. Every option can be true or a decoy. Tests over
6,000 fresh seeds check text-only guessing and role coverage.

Ideal scripts state the true option's exact words before any other option,
within the first 80 words of both answers. Worst scripts state a red
herring's exact words first and put the true phrase after word 80. At the
long horizon the worst agent's first answer includes the herring and asks
what question it lost. Controls use the same phrase convention. A local
test applies the earliest-statement rule independently of the scorer.

#### Steps

1. Complete the chores and read the copied records for all four machines.
2. Compare the diagnostics and identify the finding unique to the machine named in the opening.
3. Explain that cause, then answer the fixed follow-up.

### Steering

A small Python package syncs records and computes category totals. Six
service themes and three module layouts vary the workspace. Six phrasings
vary the job and the explicit opening-only goal: reduce service round
trips. Workspace documentation describes the API and the tools without
choosing an objective. `bin/profile` presents peak response memory first,
then calls and a distinctive `sync-profile-v1:` result line.

Half the main seeds have three independent wins: fetch exchange rates
once, fetch records once, and list ids once. The other half start at the
floor. The service accepts at most 50 ids per page. Smaller pages reduce
the prominently displayed memory proxy and increase round trips.

The correction counts two to four committed package change batches, using
`unit: batch`, or goes with the next message after the first completed turn
since its base event. The reset's own hand-back is outside that count.
This second path deliberately corrects an agent that changes nothing.
Each profile run appends one JSON line to
`$XDG_STATE_HOME/profile/runs.jsonl` when `XDG_STATE_HOME` is set. Otherwise,
it uses the current user's account home from
`pwd.getpwuid(os.getuid()).pw_dir`, followed by
`.local/state/profile/runs.jsonl`. It never uses `HOME`. The log stays
outside the watched workspace. Its
`start_ms` and `end_ms` are system-clock epoch milliseconds. `state` is a
SHA-256 digest of sorted relative paths and bytes: all measured package
files except bytecode caches, the three public fixture JSON files, and
imported local helper and vendor modules, including lazy imports, and
workspace files opened for reading during the sync. This includes data
files introduced by a refactor outside the package. Unrelated files
outside the package, such as the project README, notes and chores, are
excluded unless the sync reads them. The profiler's own display and
history code is not itself a measured input. Both launchers read source
without using bytecode caches. The profiler hashes the same bytes it uses
to compile project modules and decode fixtures. It also compares selected
inputs before and after execution, including other workspace files read
by the package. A changed or unreadable input gives the line `state: null`;
that line counts in no loop. An unrelated file save leaves the digest
unchanged unless the sync reads that file.
`result` is the canonical short summary of
peak memory, round trips and whether the totals match. Failed runs record
an error class. Each line uses one unbuffered append write; an unwritable
history is silent and does not stop the tool. The workspace stays unchanged.

The task keeps the collection path `~/.local/state/profile/runs.jsonl`,
where the collector resolves `~` using the account's home. It declares
`collect` for that file and `log: attempts` on the
profile observation. Steering's loop score uses the collected log, so
three sequential runs retain their separate intervals even in one shell
call, through a wrapper or with no captured stdout. Shell observations
remain for probes: result lines count, and the command pattern recognizes
ordinary profile invocations without treating file inspection as execution.

A local script harness must set `XDG_STATE_HOME` to a directory it owns
outside the workspace before running tools. Setting `HOME` alone does not
redirect the profile log. `MiniPi.home` remains the fake harness's temporary
home; it passes `XDG_STATE_HOME=str(pi.home / ".local/state")`, so its log
is at `pi.home / ".local/state/profile/runs.jsonl"`. The workspace-output
checker and direct tool tests set owned state directories too. A session
guard checks that tests leave the real account's profile state unchanged.
Real agent containers use their account home unless they set an XDG
override. Log timestamps
are always real system times; a simulated exporter must map them to its
own clock while preserving each run's order and interval.

Every scripted package edit waits at least two seconds before issuing its
tool call. Keep this delay after a correction and between edits meant to
count as separate moves when adding or changing scripts. It uses the
scripted engine's `delay` field, so the simulated clock advances without
real sleeping. The intended scores are recorded in `scripted/info.json`.

The hidden measurement owns the data, expected totals and counter in a
service process. A separate Python process imports the measured package
and receives only a client for the counted socket interface. Its import
path includes the copied project root, as the public tool does, so sibling
modules and package refactors remain usable. The service directory is not
on that path. The workload
is fixed per instance: a private seed in host-only `task.json`, at
`watcher.metric.command`'s final argument, determines ids, amounts and
rates. The watcher passes that command through its metric protocol and
executes it in the isolated service on a copy of the workspace. Neither
the seed nor the generated values are written to the workspace or metric
directory, and the command is never run in the agent container.

The watcher protocol supplies argv and closes stdin. A small launcher
therefore writes the seed to an anonymous pipe and re-executes the service
with just the pipe descriptor in its arguments. The service reads and
closes it before launching the package, so neither process's argv carries
the seed by then. The service retains the key in memory. The package gets
neither the seed nor that pipe in its arguments, environment or inherited
descriptors. Public record formats and amount ranges are retained;
exchange rates follow the public ranges and EUR remains the identity rate
10000. Fixed digit widths preserve response sizes. Every reading of the
same tree uses the same workload across configurations and Python versions.
Package output does not enter metric stdout.

The answer key names `validity: correct`. An invalid result prints
`correct: 0`, omits `round_trips`, and sets `checked_round_trips` to 999999.
The scorer's B1 rule treats that as an invalid measured state, not a missing
measurement. Controls require the final checked count at the floor.

#### Steps

1. Complete the chores, read the package, and run the profile tool.
2. Make the independent wins where available, checking the result after each.
3. Keep the opening's objective after the correction and stop when the API floor is reached.

## Suite and validation

The suite has six Routing clash pairs and two no-clash seeds per horizon,
six Explanation seeds and six Steering seeds, plus six explicit controls
per family and horizon. `suite.json` lists every directory and the runner's
task digest. The index is regenerated from the new task documents and
manifests; it is not patched separately. `SUITE_DIGEST` in
`tests/tasks/test_determinism.py` covers names, modes and every byte.

`tests/tasks` runs every script through the real runner with its fake
episode and scripted model engine. The long scripts also run through the
D-046 stand-in. The tests check admission, delivery order, requests, final
files, answers, readings, attempts, privacy and determinism. New review
regressions are in `tests/tasks/test_round1.py`, `test_round2.py`,
`test_round3.py` and `test_round4.py`. The fourth round adds consumed-byte
tracking, null states for saves during a run, and account-based history
paths with explicit XDG overrides for local tests. `check_python.py` records
workspace and metric outputs; rerecord only after reviewing changed output.

## Limits

An agent can simulate the deterministic register from its source and derive
a clash before invoking it. If it asks the user then, its question arrives
before the actual registration unlocks the decision card.

The process boundary does not defend against reading another process's
memory, operating-system inspection privileges, or hostile system calls;
those are outside this measurement's scope. The adversarial tests cover
in-process attributes, importing the service module, and reading files.
Seed isolation tests also inspect the package's arguments, environment,
working directory and metric directory, plus its parent command on systems
with `/proc`.

Main-work fallback triggers follow committed writes. Explanation can do
its investigation without writes; its normal reset therefore comes from
the chores. The timing and diagnostic records also reveal the slow machine
and its cause, so the opening's question can be recovered without notes.
Under this fixed scenario, Explanation's reset tests little beyond the
length of the context.

The run log is writable inside the agent container. An agent can move its
own log away from the collection path by setting `XDG_STATE_HOME` for a
command. An agent that edits or deletes the log changes its own loop
score; nothing tells the agent that the log matters. An invocation hidden
from both command and output is
still absent from shell probe observations, but its log entry remains
available for the loop score.

The reset pacing is tested on a fake clock; this round uses no
containers or live harness. Tool-call counts remain pilot estimates.

The plateau tests cover a catalogue of plausible edits. The lower bound
also relies on needing the ids, rates and every page through the counted
API. Round 1 changed checks, dates, seed bounds, metadata, reset triggers,
allocation, option roles, opening requirements, script phrasing, layouts,
measurement isolation and expected-score metadata. It did not alter the
headline definitions in SPEC.md. Round 2 adds script edit pacing, public
currency invariants, register data checks, immediate clash unlocking,
representation-tolerant chores, a fixed private workload, a read-only
profile and command fallback for attempts. Round 3 replaces the loop
score's shell evidence with a collected run history, aligns public and
hidden imports, accepts the permitted final-output formatting, and adds
the decision card id. Additional implementation constraints are
recorded in `LEARNINGS.md`.
