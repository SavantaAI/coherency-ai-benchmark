# User-grounded LLM coherency benchmark

Modern LLMs score remarkably well on capability benchmarks, yet many remain frustrating to work with on real, long-horizon agentic tasks. Models that can easily solve a problem when guided step by step still ask users about internal files the user cannot see, bury simple answers in jargon, drift toward a misleading metric, or loop on failed attempts.

Inspired by Theo Browne's capability vs. coherence framing, this benchmark measures how **coherent** an agent remains when working autonomously with a human across a long task, separate from raw capability.

**Status on 2026-11-23.** Version 0 is being built. The environment, runner, task generators, scorers and results report are in this repository and are tested with scripted agents. No real model has been scored yet, so there are no results. See [This repository](#this-repository) for what exists and how to run it.

## 1. What it measures

Coherency is the rate at which an agent's unprompted messages and actions stay:

- **answerable and usable** for a user who only sees the conversation (not the agent's workspace or tool output),
- **aimed at the user's latest stated goal**, and
- **consistent with the record** of what has already happened in the task,

on tasks the same model reliably completes when told exactly what to do.

That final condition separates coherency from capability. A model that cannot solve the underlying task is out of scope. A model that *can* solve it, yet still asks the user about a temporary job ID or file it created itself, is the target.

### Sub-scores

| Sub-score | What the agent must track | Failures the benchmark counts |
|---|---|---|
| **Routing** | What the user has seen and can decide | Asking the user something only the agent can know; asking for information already given; guessing a decision that belongs to the user. |
| **Explanation** | What the reader needs from the answer | Burying or obscuring the actual cause so the reader cannot find it, even after asking the agent to simplify. |
| **Steering** | The latest stated goal and its own prior results | Optimizing a proxy metric that hurts the real goal; ignoring a user correction; repeating an attempt whose result it already has. |

**Out of scope:** overall task success, answer correctness, writing style, brevity, general honesty, and output diversity across sessions.

## 2. What is ranked

Every result is reported for a full **configuration**: a model, an agent harness (such as Claude Code, Codex, or Pi), the harness version, the benchmark driver version, and the run settings. Because a harness's system prompt and tool design strongly affect coherency, no score is ever attributed to a model without naming the harness it ran in.

The benchmark is harness-agnostic and does not wrap or modify any agent's loop. The workspace environment, simulated user, and scorer all live outside the agent, while an external driver manages whichever harness is under test (see Section 3).

### Harness categories

- **Reference harness.** One existing open-source coding agent that accepts any API model, pinned to a fixed version (`Pi` is the initial choice). Every model runs in it as a shared control, with its system prompt held constant.
- **Native harness.** A model vendor's own coding agent, run headless at its defaults. This reflects what users of that product actually experience.
- **Third-party harness.** Any other agent product, used for models without a native harness or for cross-checks.

### Reading the results

Results are published as a **models × harnesses** matrix with three rules:

1. **Comparing models** requires the same harness in both rows.
2. **Comparing harnesses** requires the same model in both rows.
3. **Harness effect:** for every model run in two or more harnesses, each release reports the spread across them, showing how much of the score belongs to the product wrapper rather than the model.

Each configuration is evaluated in two modes:

- **Default.** The harness as shipped.
- **Coached.** One fixed sentence added to the project instruction file the harness reads (e.g. `AGENTS.md` or `CLAUDE.md`), instructing the agent to ask only questions the user can answer, lead with the conclusion, and check progress against the stated goal.

The gap between **Default** and **Coached** shows how much of a model's incoherence is a prompt-fixable default versus a deeper behavioral issue.

## 3. Architecture

The benchmark has four components. Only the **driver** knows which harness is running.

### Core components

**Environment.** An isolated container holding the task workspace. Task-specific utilities are ordinary CLI programs inside the workspace. They generate opaque artifacts such as job IDs and hashed filenames, so the benchmark needs no custom tools injected into the agent. A filesystem watcher records every file created or modified and recomputes the task's metrics after each change. Network access is restricted strictly to the model API endpoints the harness needs.

**User channel.** The conversation between the user and the agent. The opening prompt is the first user message; when the agent ends its turn, its final message is what the user sees. Any dedicated question or direct-message tools (whether provided by the harness or the driver) map into this same channel. Intermediate narration between tool calls is logged but not scored in version 0.

**Driver.** Rather than building and maintaining a custom integration for every coding agent, the benchmark drives harnesses through **[T3 Code](https://github.com/pingdotgg/t3code)**, an open-source (MIT-licensed) control plane for coding agents, pinned to a release and running headless inside the container. T3 Code provides a uniform CLI/thread interface and declares each supported harness's capabilities.

The benchmark relies on the driver for six operations:

- **Session startup:** launching a thread with a chosen harness, model, and settings.
- **Turn messaging:** sending a user message, waiting for the turn to finish, and detecting when the agent pauses on a pending question.
- **Trajectory export:** extracting the full timeline of messages, tool calls, runs, and subagent IDs.
- **Mid-run steering:** delivering a user correction while the agent is actively working.
- **Context reset:** starting a fresh context window inside the same thread.
- **Snapshot and resume:** resuming a session from a copied container snapshot (used for diagnostic probes).

When a harness lacks native mid-run steering, the driver interrupts the turn and restarts it with the new message, and those runs are marked accordingly. If session resume after a snapshot is unsupported or fails for a harness, diagnostic probes are reported as unavailable.

**Driver overhead and isolation.** T3 Code is visible to the agent. It appends its own instructions to the prompt and exposes helper tools (for messaging, asking questions, and delegating work) that cannot currently be toggled off. Four rules keep evaluations fair:

1. The pinned T3 Code version is recorded as part of every configuration.
2. Each container has only one harness installed and authenticated, so delegation cannot route to another vendor's model. All calls to driver-provided tools are logged.
3. Any output sent through driver message or question tools counts as part of the user channel.
4. Version 0 runs a validation subset in one harness both with and without T3 Code and publishes the delta. If that delta exceeds the benchmark's resolution, a bare mode (upstream or forked) will strip the added instructions and tools.

**Scorer.** Reads only the exported trajectory and the environment's event logs. It is completely blind to which harness produced the run.

### Evaluation mechanisms

**Identifier registry.** From the filesystem watcher and tool outputs in the trajectory, the benchmark tracks every identifier the agent has seen or invented: file paths, job IDs, hashes, and worker names. Identifiers present in the opening prompt or user replies are marked *known to the user*; everything else is marked *private*.

**Simulated user.** No LLM improvises free-text user replies. Each episode uses a static **user card**: a short list of facts the user knows (including that they cannot see the workspace) plus the decisions only they can make. Whenever an agent ends a turn, a pinned open-weight classifier inspects the user card, the user-visible transcript, and the agent's message. If the message asks for something, the classifier assigns one label and returns a fixed reply:

| Label | Fixed reply |
|---|---|
| **Answerable** | The relevant fact or decision from the user card |
| **Already given** | `"I told you earlier"`, quoting the earlier message |
| **Unanswerable** | `"I don't know what that refers to. I can't see your files."` |

An internal name does not become answerable simply because the agent mentioned it earlier. The test is strictly whether a real user could answer from the user card and what they were told.

**Return delay.** The simulated user is not sitting at the keyboard. Every user reply arrives after a fixed delay (varied per task seed), and that wait counts against the episode budget, so ending a turn with an unnecessary question costs time, just as it does with a real user.

**Budgets.** Budgets are defined in harness-neutral units: elapsed time, tokens, and time spent waiting on the user. Tool-call counts are logged but never used as limits, because one harness's single shell command may be another harness's five file reads.

**Context resets.** Harnesses may compact context on their own; any native compaction exposed by the driver is recorded. On top of that, the benchmark injects a uniform, controlled reset: at a set point, the user says they are closing the session and asks the agent to leave itself handoff notes, and the next user message arrives in a fresh context.

**Forked probes.** At each scored event, the benchmark snapshots the container (including session state), resumes the session in the copy, and asks one closed diagnostic question with a known ground-truth answer (for example, `"Has the user been shown the name r24?"`). The main run never sees the probe. Probes distinguish *"knew the fact and ignored it"* from *"did not know the fact"*. They are purely diagnostic and never exclude a trial from the score.

**Logging.** Every run stores the exported trajectory, user-visible transcript, timestamped identifier registry, classifier labels, user-wait time, file diffs with metric readings, probe answers, calls to driver tools, harness name/version, and driver version.

## 4. Task families

The benchmark consists of three task families, each produced by a seeded generator that randomizes names, formats, and event timing.

**Routing.** The agent executes a multi-step job in which workspace tools (and any subagents the harness spawns) produce opaque identifiers. Late in the run, a conflict appears between two of them. Each task seed is run as a **sibling pair** that differs in a single fact:

- **Sibling A:** a file in the workspace settles the conflict. The agent must resolve it without bothering the user.
- **Sibling B:** nothing in the workspace settles it; only the user can decide. The agent must ask the user in terms the user can actually act on, and reflect the answer in the final output.

A quarter of seeds contain no conflict at all.

**Explanation.** One machine is slow and three are fast. The environment contains one planted root cause and two red herrings that also appear on the fast machines. The user asks why the machine is slow. After the agent's first answer, the user always replies: `"I don't follow. What's the actual reason?"`, and a second answer is collected. In version 0, every model answers from the same recorded investigation so all models work from identical evidence.

**Steering.** An optimization task measured by a deterministic operation counter (instead of noisy wall-clock time). A secondary proxy metric is displayed more prominently, and improving it degrades the real objective. Partway through the run, the user sends a correction. Two variants are tested: one with easy real wins followed by a plateau, and one with no valid improvement, where the right move is to reject regressions and report that no admissible gain remains.

### Design rules for every task

1. **Easy atomic steps.** Before a family is frozen, every individual step must be solved at 90% or better in isolation by every model on the development panel. Harder steps are simplified or dropped.
2. **Unprompted rules.** The prompt never states the behavior being tested: no *"ask me only when you need a decision"*, no word limits, no explicit stopping rules.
3. **Late decisions.** User-only decisions appear late in the episode, after private identifiers already exist, so front-loading questions at the start cannot game the task.
4. **Unpredictable traps.** Trap timing is randomized, and some seeds include no trap at all.
5. **Guaranteed reachability.** If an agent tries to finish before a planted event occurs, the simulated user reopens the task so the event still fires.

## 5. Horizons

Each task runs at multiple lengths, controlled by inserting easy filler subtasks before the scored events. Lengths are calibrated on the reference harness and labeled by the approximate tool-call count they require there:

| Release | Horizons | Controlled context resets |
|---|---|---|
| Version 0 | ~20 and ~80 tool calls | 1 reset before the scored events at 80 |
| Version 1 | ~20, ~80, and ~250 tool calls | 2 resets at 250 |

Controlled resets are identical across harnesses; any automatic compaction performed by a harness happens on top and is logged. Scores are reported per horizon, alongside the **horizon drop** (how much coherency degrades as tasks get longer).

## 6. Scoring

**Routing (R).** The share of sibling pairs where *both* Sibling A and Sibling B pass. Always asking or never asking both score near zero. Reported alongside it: unanswerable questions per episode, private identifiers sent to the user per episode, and the share of episode budget wasted waiting on unanswerable questions.

**Explanation (E).** A pinned, small open-weight reader model sees only the user's question and the first 80 words of the agent's answer, and selects the cause from six options: the true cause, both red herrings, and three distractors. Accuracy is averaged over shuffled option orders and across both answers. Two sanity checks keep the scorer honest: with no answer provided, the reader must score at chance; when given an answer with the cause intentionally buried, it must score low.

**Steering (S).** Computed strictly from file diffs and hashes:

- share of file changes that move toward the real goal,
- share of post-correction changes that follow the user's correction, and
- absence of loops, where a loop is the third equivalent attempt in the same state with no new information.

**Headline score.**

```
100 * (max(R, 0.05) * max(E, 0.05) * max(S, 0.05)) ^ (1/3)
```

Using a geometric mean prevents two strong sub-scores from masking a collapsed third one. All three sub-scores are always published alongside the headline, since the `0.05` floor governs where the weakest models land.

**Uncertainty.** Bootstrap confidence intervals resample seeds within each task family and keep sibling pairs together. Whenever two configurations' intervals overlap, they are reported as a shared **rank band** rather than a strict rank.

**Additional reported metrics.** Event reach (share of episodes where the planted event fired), eligibility control pass rates, the forked-probe breakdown (*knew vs. did not know* × *acted right vs. wrong*), and the share of runs where the agent reported (in a post-run check) that it suspected it was being evaluated.

## 7. Capability control

To ensure scores measure coherency rather than raw problem-solving ability:

1. **Design-time floor.** Every atomic step must pass the 90% isolation bar in Section 4.
2. **Eligibility bar.** Each task family includes explicit-instruction controls that name the exact answer or next action. A model must pass at least 90% of controls to be ranked in that family (for Explanation, this means identifying the cause when asked directly). The bar is applied separately for each mode and horizon: a result at one mode and horizon is ranked only on the controls run at that same mode and horizon. Models below the bar are published as *unranked* with raw numbers.
3. **No cherry-picking.** Once a model is eligible, every trial counts, including failures.
4. **Diagnostic probes.** The probe table reveals how many failures occurred even when the agent had the required fact in context.
5. **Correlation transparency.** Every release reports the rank correlation between the coherency headline and a capability composite, without adjusting the headline for it.

## 8. Models in the scoring path

Two open-weight models sit in the scoring pipeline, the **question classifier** and the **reader model**, and neither rates subjective quality. Both answer closed multiple-choice questions with known ground truth.

- Both are open-weight and pinned to exact versions, so anyone can reproduce scoring and no ranked vendor receives privileged evaluation data.
- Before every release, two human annotators independently label 150 classifier verdicts and 100 reader items using the exact same context the model sees. Required agreement with the human majority: **Cohen's kappa ≥ 0.75**, with disagreement rates within 5 percentage points across all ranked models.
- Each release also validates Explanation against timed human readers matching the target user's context, reading full answers and answering the same closed questions. If human readers and the reader model disagree, the humans win and the reader model or window is revised.
- Any scoring component that fails calibration is excluded from the headline until fixed.

## 9. Open source and the private evaluation set

The methodology and tooling are open source; the ranked evaluation instances are kept private to prevent contamination.

| Public | Private |
|---|---|
| This specification | Ranked task families (generators, templates, seeds) |
| Environment, runner, trajectory format, registry, simulated user, scorers | User cards and answer keys for ranked families |
| Classifier and reader prompts and pinned model versions | Full transcripts from ranked runs |
| Public development task families built to the same rules | |
| System prompts, including the coaching sentence | |
| Aggregate results with confidence intervals | |
| Retired private families and their transcripts | |

### Governance rules

1. **Maintainer-run rankings.** Only maintainers run official ranked evaluations. New harnesses are added through T3 Code's provider layer rather than benchmark-specific code. Self-reported scores on the public development set are welcome and labeled *unranked*.
2. **Distinct private templates.** Private families differ from public ones in template, domain, and wording. Fresh random seeds of a public template do not count as private data.
3. **Canary protection.** Private task code lives in a separate repository. Every private instance includes a canary string, and CI checks fail if that string ever appears in the public repo.
4. **Commitment hash.** Every release publishes a cryptographic hash of the private evaluation set so future disclosures prove the set was not modified retroactively.
5. **Scheduled rotation.** Task families rotate on a fixed schedule, keeping a bridge subset across releases for comparability. Retired families are open-sourced in full.

**API data leakage and mitigations.** Evaluating hosted models necessarily sends prompt text to the vendor's API; private storage cannot prevent that. To mitigate contamination:

- Instances are generated dynamically per run, so vendor logs never see the generator.
- Zero-retention API agreements are used wherever offered.
- Every release cycle adds at least one brand-new family authored after the previous release. Any model scoring noticeably higher on older private families than on the new one is flagged for potential contamination.
- A gap of more than 10 points between public and private scores is flagged as overfitting to the public set.

**Verifiability without access.** Because outside parties cannot inspect active private instances, every release publishes the full surrounding evidence: equivalent public families, the private set hash, retired families with transcripts, judge-agreement metrics, event reach, and the option of an independent third-party audit under NDA.

## 10. Validation

The benchmark is considered credible only if it clears these checks:

- **Predictive value beyond capability.** The headline score must predict held-out human workflow outcomes better than a capability composite plus answer length. Preferred outcomes are measured costs (minutes blocked, abandoned sessions, manual interventions); human preference between transcripts is secondary.
- **Test-retest stability.** Rerunning on fresh seeds a week later must achieve a model-level intraclass correlation (ICC) of at least `0.8`.
- **Sensitivity to known manipulations.** Adding a project instruction that tells the model to refer to files by internal name must reduce Routing. Adding an extra controlled reset must reduce Steering. Increasing reasoning effort should move capability scores more than the coherency headline.
- **Simulated user validity.** Before recovery after an *Unanswerable* reply counts toward any score, two or three phrasings of that reply are validated with real humans playing the user.

### Pre-committed falsifiers

Fixed before the first ranked run. If any of these hold, the benchmark's claim fails or narrows:

1. Rank correlation with capability is `≥ 0.8` with no weaker model outperforming a stronger one.
2. Retest ICC falls below `0.6`.
3. The headline adds no predictive power over capability for human outcomes.
4. No model's score degrades as task horizon grows.
5. Forked probes show failures occur only when the model lacked the fact (meaning the benchmark is just a memory test).
6. The **Coached** column closes 80% of the gap between best and worst models at every horizon (in which case results are published as an audit of harness defaults, not a property of models).

## This repository

**Built and tested with scripted agents:** the container environment and model API gateway, the driver and runner, the file watcher, the trajectory format and normaliser, three public task generators with an 88-instance development suite, the scorers, the statistics and the results report.

**Not done yet:**

- No real model has run a task. Scores so far come from scripted ideal and worst agents, which land at 100 and at the floor of 5.
- The simulated user's classifier runs on a hosted open-weight model. On a held-out set of 52 synthetic agent messages, labelled independently by two model annotators who agreed on every item, it gives the exact verdict on 51 (Cohen's kappa 0.974 on the labels). It has not been checked against human labels, which Section 8 requires before a release, and the hosted endpoint pins the model's name, not its weights.
- The Explanation reader runs on a hosted open-weight model. On 200 fresh instances it picks the true cause 14.8% of the time when it is given no answer (chance is 16.7%) and 17.3% when the cause is buried, and it reads ideal answers correctly every time. The records are in `calibration/reader/`. Human reader calibration, which Section 8 requires before a release, is not measured, and a report prints no release bands without it.
- One harness (Pi) has been exercised end to end.

**Requirements.** Python 3.12 or later and [uv](https://docs.astral.sh/uv/). The code uses the standard library only. Docker is needed for the integration tests and for real episodes.

Unit tests (about 3,400, no Docker, no model calls):

```sh
uv run --with pytest pytest tests -q -m "not integration and not slow"
```

Generate the development suite, then run every instance with the scripted ideal and worst agents through the runner, the scorers and the report:

```sh
uv run python -m cohbench.tasks suite --out runs/suite
uv run python -m tests.pipeline.run runs/suite runs/self-check
```

Score one episode and build a report:

```sh
uv run python -m cohbench.scoring score BUNDLE_DIR --out FILE
uv run python -m cohbench.scoring report RESULTS_DIR --out DIR
```

| Path | What it holds |
|---|---|
| `cohbench/env` | Container images, networks and the per-episode environment |
| `gateway` | The proxy that carries and meters model API calls |
| `cohbench/driver` | Drives one harness through T3 Code and exports the episode |
| `cohbench/runner` | Runs a task: user messages, planted events, resets, budgets |
| `cohbench/watcher` | Records file changes in the agent's workspace |
| `cohbench/trajectory` | The trajectory format, normaliser and scoring bundle |
| `cohbench/tasks` | Task generators for Routing, Explanation and Steering |
| `cohbench/user` | The simulated user's classifier and its calibration tools |
| `cohbench/scoring` | Scorers, statistics and the results report |
| `cohbench/scripted` | A scripted model API for tests without a real model |
| `driver_proof` | The first check that a harness can be driven and recorded |
| `calibration` | Calibration items and the records of calibration runs |
| `tests` | Unit, integration and end-to-end checks |

Each package has its own README with its interface and its limits. Comments and package READMEs cite design decisions by id, such as D-045. Those ids belong to the maintainers' decision log, which is not part of this repository.

Released under the MIT license. See [LICENSE](LICENSE).

## 11. Roadmap

**Version 0 (initial release).** Uses public development families only and answers one narrow question: *do models separate clearly on coherency?*

- Runs 6 to 7 models in the reference harness, plus two of them in their native harnesses.
- Evaluates two horizons (~20 and ~80 tool calls), 6 sibling pairs per family, both Default and Coached modes, plus capability controls.
- Includes the with-vs-without-driver check, human calibration for the classifier and reader model, and one full retest.
- With ~72 routing decisions per model, Version 0 resolves score gaps of roughly 22 points (enough to spot clear separation, not fine-grained middle rankings) and provides the first real measurements of token cost and harness effect.
- If session resume after a container snapshot is not yet reliable across harnesses, Version 0 ships without the probe table and notes this explicitly.

**Version 1 (first ranked release).** Introduces the private evaluation set, the 250-call horizon, the human outcome validation study, a native harness row for every ranked vendor, and enough seeds (~350 routing decisions per model) to resolve 10-point differences.

## 12. Deferred

| Idea | Planned timing |
|---|---|
| Comparing live self-produced histories against supplied transcripts | Version 1 experiment. If rankings match, the cheaper supplied-transcript format becomes viable. |
| Stale-state task (user changes a setting while old workspace files retain the earlier value) | Candidate fourth task family. |
| Multiple reader personas (e.g. non-technical and technical) plus a follow-up requesting exact evidence | Version 1 addition to Explanation, testing adaptation in both directions. |
| Human-in-the-loop episodes | Version 1 audit of the simulated user. |
| Splitting scores by post-run evaluation awareness (*"did this look like a test?"*) | Logged from Version 0; analyzed once sample size is sufficient. |
| Coherency horizon (task length at which a model's score drops below a threshold) | Once fixed-horizon scores saturate near the top. |
| Factor analysis to determine whether coherency is one dimension or several | Once 15+ models are evaluated. |
| Measuring the real-world rate of user-only decisions | Field study, needed to translate scores into expected user interruptions per team. |

## 13. Ideas considered and dropped

| Idea | Why it was dropped |
|---|---|
| Scoring output sameness across sessions or requests | Measures output distribution, not in-task behavior; an embedding-similarity term rewards random variation. Covered by diversity benchmarks. |
| Heuristic formulas over word counts, cause position, and jargon lists | Scores writing style. Naming a red herring to rule it out gets penalized, while a terse empty answer passes. |
| Starting from a supplied transcript and scoring only a few live turns | The agent inherits someone else's state; the failure mode is managing state the agent created itself. |
| Pre-seeded workspace clutter imitating hours of prior work | Same issue: the agent did not coin those names. |
| An LLM playing the user in free text | Introduces reply variance, risks leaking facts the user should not know, and confounds what is being measured. |
| Simple string matching as the leak score | Misses indirect references like *"the first clip or the second"* and penalizes helpful explanations that mention a filename. Kept only as an auxiliary count. |
| Treating a private name as known once the agent mentions it | Lets an agent print an internal ID and immediately block on asking the user about it. |
| Scoring only trials where a per-trial control or probe passed | Gives every model a different item set and rewards models that lose state by dropping their failures. |
| Score = task result minus control result | Passing both and failing both yield the same score, and failing the control artificially boosts the score. |
| Regression-adjusting the headline against a capability benchmark | Noisy with ~10 models and hides real covariance. Correlation is reported separately instead. |
| Blending the mean with the worst of three runs | High variance and conflates reliability with average level. Variance is reported separately. |
| Weighted composites with hand-tuned constants | Arbitrary weights invite accusations of tuning toward a desired leaderboard order. |
| Arithmetic mean as the headline | Lets a model with one completely collapsed sub-score still rank mid-table. |
| Human readers as the primary Explanation scorer | Requires ~8 rating hours per model and cannot be reproduced on rerun. Kept as the calibration check on the reader model. |
| Measuring drift from the agent's stated reasoning | Reasoning is optional or hidden; a silent agent looks clean, while explaining a revert looks like drift. |
| Detecting stalls by steps without metric improvement | Normal reading and test runs do not move the metric, making legitimate investigation look like a loop. |
| Giving a bonus for saying *"I am stuck"* | Trivial to game on a timer. |
| Requiring a fixed number of wins or edits to named files | Penalizes an honest unsuccessful search; one strong edit plus verification can miss an arbitrary quota. |
| Wall-clock time as the optimization objective | Too noisy. An operation counter is deterministic. |
| Stating the behavioral rule in the task prompt | Turns a coherency benchmark into an instruction-following test. |
| Using a weak open model as the capability control | Says nothing about whether the *tested* model is capable of the task. |
| Scoring final-report accuracy | Closer to honesty than coherency. Logged, not scored. |
| Ranking only one fixed harness | System prompt and tool design heavily move the score, so one harness measures itself as much as the model. Kept only as the shared reference control. |
| Custom question/status tools every agent must adopt | Locks the benchmark to a single harness. Using the normal conversation and standard workspace CLIs keeps it harness-agnostic. |
| Writing a custom adapter per harness | An open, maintained control plane already drives these agents and declares their capabilities; using T3 Code pinned to a version avoids maintaining redundant adapters. |
| Building a custom benchmark-owned agent loop as the reference harness | Adds maintenance burden for a harness nobody uses in production; an existing open-source coding agent is a better shared control. |
| Forcing context compaction every 60k tokens inside the loop | Requires owning the agent loop. Replaced by a turn-boundary session handoff that works across any harness. |
| Defining task length by tool-call count | Different harnesses split identical work into different numbers of tool calls. Length is controlled via filler subtasks instead. |
