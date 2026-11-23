# Episode environment

`cohbench/env` builds and removes the containers for one episode. It holds
everything that knows about Docker. `cohbench/driver` holds everything that
knows about T3 Code. Python 3.12, standard library, the `docker` command.
Extracted from `driver_proof/` and generalised; nothing imports from it.

## Images: a separate step

`provision(harness, record_path=...)` builds or reuses every image an episode
needs and returns `Images` plus a record of which it built, pulled or reused:
the harness image (docker/base and docker/<harness>, as the driver proof
builds them), the gateway, scripted server and watcher images from the
working tree, and on Docker before 28.0 the `alpine:3.20` helper that the
bridge flush and `gateway verify` use. Images are shared across episodes and
an episode never removes them. `remove(record)` removes, on request, exactly
the images that record says its call built or pulled.

An episode removes its own containers, networks, volumes and snapshot
(probe copy) images, by exact name.

### Image input fingerprints

Every locally built image carries a SHA-256 recipe fingerprint in the
`cohbench.inputs` image label. Before reuse, `images._ensure_image` compares
that label with `build_inputs.inputs_digest`. A missing image, missing label
or different digest triggers a build under the same tag. `rebuild=True`
still forces the harness and base builds. `provision` also keeps its existing
`rebuild_harness` and `rebuild_services` switches. Both default to false, so
unchanged images are reused. A service that was rebuilt is listed in the
provisioning record's `built` list, including when its tag already existed.

The digest covers the Dockerfile, active ignore file, every context file
after Docker ignore rules, build arguments, and the input digests of locally
built parent images. Both harnesses include their base's digest. Changing
the base therefore invalidates a harness even if its own context excludes
the base files. External parent references remain in the Dockerfile or build
arguments. Resolving changes behind registry tags or remote package versions
is outside this fingerprint.

Context records are sorted by relative POSIX path. They include entry types,
permission bits, file bytes and symlink target strings. They exclude host
paths, owners and timestamps. Symlinks in the context are not followed.
Dockerfile-specific ignore rules take precedence over a context's
`.dockerignore`. The Dockerfile and active ignore file are fingerprinted even
when those rules exclude them. Unsupported special files and invalid ignore
patterns raise an error instead of producing a partial fingerprint.

The label is deliberate. Image labels are Docker metadata, not files or
environment variables visible inside the agent container. No recipe marker
is added to the container's filesystem, and image tags keep their existing
names.

The provisioning record stores `inputs_digests` alongside `ids`, by image
role. At episode start, the environment captures the inspected image IDs
and label values together. `environment.json` saves them as `image_ids` and
`image_inputs_digests`, so later tag changes do not rewrite this record.
A manually supplied image without the label has a null input digest.

Every Dockerfile has an adjacent `Dockerfile.dockerignore` that excludes
everything except its `COPY` sources. The base and harnesses use `docker/`
as the context root. Services use the repository root because their `COPY`
paths and standalone builders use repository-relative source paths. The
ignore files narrow what Docker receives without changing those existing
commands. Gateway and scripted list the exact source files; watcher keeps
the same Python-file glob as its Dockerfile. The Dockerfile and active ignore
file also reach Docker as build controls.

`runs/`, `.git/`, virtual environments, caches, test output, and unrelated
packages are excluded from both the build and the digest. Excluded trees
are skipped before stat calls, so their files can appear or disappear while
provisioning. Actual build inputs must still remain unchanged while Docker
builds. The fingerprint is computed before Docker reads those inputs.

Docker-free coverage lives in `tests/env/test_images.py`. It checks a fixed
digest on Python 3.12 and 3.15, file and mode changes, ignored paths, symlinks,
build arguments, parent propagation, reuse and force decisions, build and
inspect errors, and saved episode metadata against fake Docker calls.
`tests/env/test_build_contexts.py` also checks every real Dockerfile's context
against its `COPY` sources. It creates, changes and deletes its own scratch
output under the real `runs/` directory, and tests unrelated files and copied
source changes in isolated copies of the repository layout.

`tests/env/live_images.py` is an opt-in pytest plugin for live checks. Put
`tests/env` on `PYTHONPATH`, set `PYTEST_PLUGINS=live_images` and select a host
JSON output file with `IMAGE_AUDIT_PATH`. It records successful real build
calls and inspects the five images used by Pi episodes, checking each input
label against the current recipe. `IMAGE_EXPECT_BUILDS` can specify a JSON
list of the exact tags that should build, including an empty list for reuse.

Live verification on 2026-11-22 used Docker Desktop 27.4.0 and the runner's
demo integration test on Python 3.12. All three runs passed. The first built
base, Pi, gateway, scripted and watcher. The second made no build calls and
kept all five image IDs and labels. After appending a temporary comment to
the base Dockerfile, the third rebuilt only base and Pi; service IDs and
labels stayed unchanged. Every run inspected all five input labels against
the current recipes. The comment was removed and the original Dockerfile
bytes restored, then base and Pi were refreshed for the restored recipe.
Local audit records are in `runs/image-keying-live-QWSSiO/`. The environment
unit suite passed 71 tests on each of Python 3.12 and 3.15.

## What one episode gets

```
host                                     Docker
----                                     ------
out_dir/site-main/gateway/log  <-bind-   gateway            on: agent network (.10), upstream network
out_dir/site-main/scripted     <-bind-   scripted server    on: upstream network only (scripted upstream)
out_dir/watcher/segment-1      <-bind-   watcher            no network; workspace volume read-only at /srv/w/live
                                         harness container  on: agent network only; workspace volume at /work/project
                                         workspace volume
```

- **Agent network.** Internal, a random `172.31.x.0/24` subnet, no host-side
  address on its bridge: `gateway_mode_ipv4=isolated` on Docker 28 or
  later; otherwise created plainly and given `sysctl disable_ipv6` and `ip
  addr flush` before any container attaches (gateway/README.md, "Docker
  before 28.0"), on Docker Desktop through a privileged `alpine:3.20` helper
  that is registered by name like every other container, and on a Linux host
  through `sudo -n` with a 60 s limit.
- **Gateway.** `gateway/` in transparent mode with one route, bodies stored.
  Its log directory is a host bind mount written by its uid 10001 (the
  scripted server's by uid 10002), which the host user may not read on
  native Linux; nothing here reads it from the host. `usage()` runs `python
  -m gateway summary --episode` inside the gateway's container, and
  `Site.copy_logs(role, dest, keys)` copies a service's log directory out of
  its container through Docker into memory, scans it for every key, and
  unpacks it into a private (0700) directory.
- **Provider.** `Upstream.real(key_file, provider=ANTHROPIC)` or
  `Upstream.scripted(script)`. A `Provider` names the gateway route, the
  API host the harness resolves to the gateway, the upstream URL, and the
  placeholder credential's variable and shape in the harness container.
  Only Anthropic is defined and exercised; another vendor needs a
  `Provider`, a gateway route of that name and the harness's model slug.
- **Real upstream.** The gateway reaches the upstream URL over a plain
  egress network. `Upstream.real` reads the key file once into a
  `KeySource` snapshot. The gateway container is created first, the
  snapshot's bytes are copied into it from memory (`docker cp` of a one-file
  tar) as `/run/secrets/anthropic_key`, owned by the gateway's uid 10001 with
  mode 0400, and only then is it started. So the gateway can read it whatever
  the host user and uid mapping (a bind mount of a host file owned by
  another uid would be unreadable on native Linux), no copy is left on the
  host, and every key scan uses the same bytes, so a key rotated during an
  episode cannot make the two differ. The scripted upstream's placeholder
  key goes in the same way. The key file may not lie inside, or be, the
  workspace directory or the output directory.
- **Scripted upstream.** A scripted server (cohbench/scripted) on a second
  internal network shared with the gateway only; the gateway's route points
  at `http://<server>:8080` with `insecure_allow_http_upstream` and holds a
  random placeholder key, since a route is active only when its key file
  exists. A probe copy's server is seeded from the main server's
  `admin export` through `POST /import`, with the probe's script as JSON
  text in `next_script`; its handler 0 answers the probe question, the first
  main user message after the import.
- **Workspace volume** (D-014): see "The workspace" below. The harness
  container mounts it at `/work/project` with `volume-nocopy`. The path is
  fixed: the harness image's entrypoint starts T3 there, so another path is
  refused. There is no bind mount into the harness container: the CA
  certificate is copied in with `docker cp` before its first start.
- **Watcher** (cohbench/watcher). Started before the harness container, as
  cohbench/watcher/README.md ("Docker") and docker/watcher/Dockerfile
  require: started as root with `--cap-drop ALL --cap-add SETUID --cap-add
  SETGID` (its entry point becomes the reader 1000:1000 and forks the metric
  server as 1500:1500, keeping no capability), no `--init` and no `--user`,
  `--network none`, `--security-opt no-new-privileges`, read-only root, a
  `/tmp` tmpfs with `exec` of `WatcherSpec.tmp_size` (default 256 MiB), the
  volume read-only at `/srv/w/live` with `volume-nocopy`, the output directory
  a host bind mount at `/srv/w/out`, the task's metric files (`task_dir`)
  read-only at `/opt/task`. The metric command runs on a copy at
  `/work/project` as the metric user, who cannot traverse `/srv/w`. Ignore
  patterns are `.git/**` and `**/.git/**`: the root repository is recorded
  coarsely by the watcher, and a nested repository's `.git` holds T3's
  checkpoint objects, which the handoff's cleanup removes, so it is not task
  content. The D-026 lists (`ignore_names`, `ignore_git_refs`) are required
  and passed explicitly; a spec without them is refused. `--workspace-path`
  (`/work/project`) and `--metric-isolation users` are passed too: `users`,
  not `auto`, so a start that cannot isolate the metric fails rather than
  running it as the watcher's user, and with a metric command the heartbeat
  must report `metric_isolation: users`. The image has no container health
  check (D-036: Docker's timed exec walks the volume, which the agent could
  observe), so readiness is read from the heartbeat file in the output
  directory: `running` (after the baseline and its metric reading), fresh
  (`watcher alive`, at most 5 s old), no `event_log_broken`, and a
  successful inspect saying the container runs, before the harness
  container is created. Each of those reads gets the time left, not a
  fixed allowance. A stop is clean only when a successful inspect says
  it exited with status 0 and the heartbeat says `stopped`, with nothing
  `still_changing`, no `late_overflow` or `late_capped`, and `final_settled`
  not false. `mark(label, at=, timeout_s=, limit_s=)` names an instant
  (D-037; `--at`, default the time of the request) and never closes a
  batch: the watcher answers once everything written up to `at` is in a
  committed batch, with `accepted`, `at`, `batch` (in effect at `at`),
  `straddled` (a batch whose span holds `at`, from the lower bound of its
  first write to its last write, D-040, or null)
  and `covered_utc`; this adds `requested_utc` and `acked_utc`. A mark that
  is not accepted names no state and raises. The final marks at a handoff
  and at the export's shutdown follow the stop of the agent's container and
  are strict (`final`): not accepted, or straddled, fails the handoff or
  export with that reason. `WatcherSpec` also
  passes `--metric-cmd`, `--metric-timeout-s` and the D-026 lists as
  `--ignore-names` and `--ignore-git-refs`, which the driver fills from its
  harness descriptions.
- **Names.** Every container, network and volume is `n` plus 12 random hex
  digits. `Environment.names` maps each to its role; it is written to
  `environment.json` on the host and never reaches a container. The
  hostname is `workstation`.

What the harness container sees is the same with both upstreams: the name
`api.anthropic.com` resolving to the gateway, TLS from the gateway's CA,
the same topology, environment and placeholder credential. What differs is
what comes back: the scripted server sends five or fewer response headers
where the real API sends about 25 (`anthropic-ratelimit-*`,
`anthropic-organization-id`, `cf-ray`, `Server: cloudflare`, ...), its token
counts are estimates, and it has no thinking blocks, server tools, rate
limits or overload errors (cohbench/scripted/README.md, "How it differs from
the real API").

## The workspace

A workspace comes as a host directory or a tar, with a manifest: one
`mtime` for the whole workspace (epoch seconds, D-027) and every relative
path meant for the agent, with its type, mode, and size and SHA-256 (files)
or target (symlinks). A workspace without a manifest, or a manifest without
`mtime`, is refused. What the agent sees comes from the manifest alone
(D-031): each entry's type, mode and link target, and the one `mtime`; the
host directory supplies file contents only, which must match the manifest's
sizes and hashes. Every directory must be listed with its mode (a directory
the manifest does not list is an error, never an implied 0755), and every
file and directory entry needs a mode. `workspace.pack` takes exactly the
manifest's entries, fails on any entry in the source that the manifest does
not list and on any listed entry that is missing or whose content or type
differs, never packs host metadata (`.DS_Store`, `._*`, `Thumbs.db` and the
like), and owns everything by uid 1000. Every entry gets the manifest's
`mtime` and mode (the workspace root is 0755); the host's times and modes
never reach the volume, so the same manifest always gives the same tar.
The task's metric directory is mounted as given (`task_dir`); the runner
stages it from its validated manifest. `manifest_for(dir, mtime=)` writes
a manifest from a directory, for task authors and tests; without `mtime` it
takes the newest regular file's time once.

Before population the manifest's names, and the target string of every
declared symlink, are checked once against a narrow pattern for words that
would name the measurement (`workspace.MEASUREMENT_WORDS`: the project and
package names, benchmark, coherency or coherence, eval as a word, answer
key, user card, scorer, probe, test harness, simulated user). A coding task
legitimately has `tests/`, `test_*.py` and `bin/measure`, so the broad
pattern below does not apply to it, and nothing the agent writes later is
checked. A task file with a D-026 reserved basename (any harness's
instruction file, at any depth) is refused.

**Key admission.** With a key, nothing is populated until the workspace has
passed three scans with the snapshot's bytes: the packed tar as bytes; every
member's name and link target, every regular file's content, and one
decoding level down (a gzip, bzip2 or xz stream, and the members of a zip or
tar archive, their names included, so a `.tar.gz` is read to its members);
and every object of every git repository in the workspace, loose and
packed, reachable or not, read decoded by `git cat-file
--batch-all-objects --batch` in a throwaway container with no network that
unpacks the tar. A repository is any directory holding `HEAD`, `objects/`
and `refs/`, found without pruning, so nested and bare repositories,
submodule directories and a repository under a task directory that happens
to be named `objects` are all found. A file that claims a compressed format
and cannot be decoded fails closed, and so does a repository that cannot be
read. Content in any other encoding (zlib outside git, base64, a git
bundle, an encrypted archive, a second level of nesting) is not searched. A
finding names the file, or a member by its position when its name holds the
key, never the key.

**What each scan covers.**

| Path | Literal bytes | Names | One decoding level | Git objects decoded |
|---|---|---|---|---|
| Workspace admission | yes | yes | yes | yes |
| Probe snapshot: committed image (`docker save`) | yes | no | gzip layers only | no |
| Probe snapshot: cloned volume | yes | yes | yes | yes |
| Harness container at handoff and close (`docker export`, the volume by `docker cp`, `docker inspect`) | yes | no | no | no |
| Files written to the host (logs, records, the export) | yes | no | no | no |

**Coached mode.** `instruction=(file name, sentence)` appends the sentence
on its own line to that file at the workspace root, creating it when the
task has none, before population. The file gets the manifest's `mtime`,
like every other entry, not the run's time. In a Git workspace a created file is untracked, which
is what a file the user just added looks like; that is left as it is.

## Checks

The broad pattern (`checks.TELLING`: benchmark, coherency, eval, test,
probe, proof, watch, monitor, measure, score, snapshot, scripted, cohbench,
harness, driver, gateway) applies to what this project creates around the
agent. After each harness container starts, `prepare(container, role)` (the
driver's hook: harness settings written, T3 ready, project created) runs,
then these checks, failing closed, on every segment and every probe copy:

- `python -m gateway verify` against its gateway;
- the bridge address gives `EHOSTUNREACH` or `ENETUNREACH` on a sample of
  ports, and ARP finds no host there;
- reverse DNS of the container's addresses and the gateway's names nothing
  telling, and no `/proc/self/mountinfo` line holds a host path or a telling
  word in its root, mount point or source;
- no telling word in any process's arguments, environment variable names or
  values (no host path in a value either), or the hostname; an unreadable
  process environment fails;
- the container's, gateway's, networks' and volumes' names (a probe
  copy's own volume included);
- every name under the harness's own directories at any depth (for Pi,
  the container user's `.pi` directory), with each symlink's target string, listed
  NUL-delimited; the CA certificate's name; and the text of the files this
  project writes (`/etc/t3-settings.json`, the entrypoint, the harness's
  default-model file, the diagnostic extension when loaded). A JSON file is
  checked by its values; its keys are T3's or the harness's schema (T3's
  settings name a `driver` per provider instance). On a probe copy the
  names under the harness directories are judged only where they were
  present at the segment's startup check (`Segment.name_baseline`): what the
  agent wrote there since is its own content and outside the gate, so a
  file it named `test_notes.md` cannot make a probe unavailable. The same
  holds for the files this project writes: on a copy, a file whose text
  equals its startup snapshot (`Segment.text_baseline`) was checked then,
  and one the agent changed since (Pi's settings, say) is recorded in
  `settings_changed_since_start` and left outside the gate.

What the checks do not cover, by design: while the episode runs the
driver's T3 reads and calls (polling included) are short-lived `node -`
processes started in the harness container by `docker exec`, visible in its
process table; a probe pauses the container and reads its workspace files.

Two exceptions are matched exactly and recorded: T3's runtime helper
`t3-resource-monitor` (`checks.T3_OWN_ARGV`), and `~/.t3`, T3's own state
directory, which is not listed (it holds T3's `caches/pi-status-probe`, as
in the driver proof).

## Operations

| Call | What it does |
|---|---|
| `Environment(images, upstream, workspace=, manifest=, out_dir=, watcher=, instruction=, prepare=, harness_paths=)` | Nothing yet. |
| `start()` | Key-file location, manifest names and reserved names, pack, key admission, volume, site, watcher, harness container, `prepare`, checks. |
| `harness` | The current segment's harness container. |
| `wall_elapsed()`, `started_utc`, `usage()` | The raw wall clock since `start`, and the main gateway's `summary --episode`. The episode clock is the driver's. |
| `mark(label, timeout_s=)` | A watcher batch boundary, acknowledged, with its request and acknowledgement times. |
| `stop_server()`, `stop_container()`, `stop_site()` | Stop `t3 serve`; `docker stop` the harness container; `docker stop` the gateway and the scripted server so their logs are final. A container counts as stopped only when an inspect succeeded and said so (`docker.require_stopped`); anything else raises. |
| `handoff(on_stopped=)` | D-010, below. Raises `HandoffFailed(stage, cause)`. |
| `probe_copy(upstream=)` | D-009, below. Raises `ProbeUnavailable(reason)`, or `MainNotResumed`. |
| `key_scan(container)` | With a key: `docker export`, the volume's content and `docker inspect` must not hold it. |
| `keys()`, `write(path, data)` | Every key snapshot in play; a host file written only after scanning it for all of them, through a temporary file and a rename. |
| `close()`, `left_over` | Below; what is still registered. |

**Deadlines.** `with cohbench.env.docker.deadline(at):` caps every
subprocess inside it at the time left before `at` (monotonic): Docker
commands and `docker cp` (the CA, the credential), the scanners, the
watcher's marks (the whole `watcher mark` subprocess ends by then), the
watcher start loop, `python -m gateway verify`, and the bridge flush by
`sudo -n`. It raises `DockerTimeout` (or `DeadlinePassed` before starting a
command), and a retry loop that catches Docker errors (waiting for T3 or
the scripted server to answer) stops once the deadline has passed
(`deadline_passed()`). The driver wraps each public call in one. Two things are exempt:
removing what was created (`Registry.remove` runs under `no_deadline`), and
a probe's recovery of the paused main container (its own limit,
`Environment.RESUME_S`, 60 s), so neither is ever cut short by a deadline
the work before it used up.

**Nothing reaches the host unscanned.** Server logs (at handoff, close and a
probe copy's close), watcher container logs and `environment.json` go
through `write`: scanned for every key in memory, written to a temporary
file and renamed. A failed `docker logs` raises instead of being saved as
the log.

**Handoff** (D-010; the driver has passed the idle gate). Any failure raises
`HandoffFailed` naming the step (stop server, stop container, final mark,
key scan, capture, server log, remove container, stop watcher, clone, git
cleanup, new segment). In order: stop `t3 serve`;
`docker stop` the harness container, then ask the watcher for a final mark
(nothing can race it); key-scan the container; call `on_stopped(container)`
(the driver captures the segment's T3 record from the stopped container
there, and a failure fails the handoff); save its server log; remove the
container; stop the watcher (it must shut down cleanly); clone the volume
(`cp -a`; the two listings must be equal: types, modes, owners, sizes, link
counts, nanosecond mtimes, SHA-256); in a helper container on the clone,
find every Git repository (`.git` directories and files, so nested
repositories and submodule checkouts) and remove T3's checkpoint refs with
the committed driver proof's procedure (`gitclean.drop_t3_refs`: only T3's
checkpoint shapes, `--no-deref`, holds in a per-invocation namespace,
nothing pruned unless the earlier unreachable set is recorded and held, and
no repack or prune at all in a repository with no T3 ref to remove;
repositories found NUL-delimited, so a name with a newline is one path);
start a watcher on the clone (`watcher/segment-2`); start a fresh harness
container on the same site; `prepare`; checks. The original volume stays as
it was, as evidence, until close. Only the workspace crosses.

**Probe copy** (D-009; the driver has passed the idle gate). The main
harness container is paused (`docker pause` freezes every process in it,
detached ones included), committed with `--pause=false`, its volume cloned,
both volumes listed and compared, and unpaused. So the image and the clone
are one moment, and nothing in the main container ran between them; the
watcher, outside, keeps running but sees no change. This capture has an
observable effect on the main container: its processes are frozen for the
pause (its length is recorded in the copy's `capture.seconds`; not measured
across runs), and its workspace files are read to hash them for the
listing. From the moment a pause is attempted, the main container is
recovered in a `finally`, outside the call's deadline under its own 60 s
limit: `docker unpause`, then a successful inspect must say running and not
paused, up to five tries. That holds also when the pause failed or its
result is unknown. Any path that does not end with that confirmation
raises `MainNotResumed`, whatever else failed, which the driver turns into
an episode failure.

Before the copy starts, the committed image (`docker save`, every layer,
gzip layers decoded) and the cloned volume (every file, one decoding level
down, and every git object) are scanned with each key in play: the main
upstream's and the copy's. The copy has its own registry, site (networks,
gateway, a seeded scripted server or the real upstream), container and
volume, no watcher, and passes the same checks, the name check against the
main segment's startup baseline. Any failure or interruption during setup
removes what the copy made and raises `ProbeUnavailable` with the stage
(capture, snapshot key scan, site, container, prepare, checks); that
cleanup runs outside the expired deadline (`close_quietly`).
`ProbeCopy.close()` key-scans the copy's container, saves its server log
(scanned) and removes the copy. It never raises except `KeyLeak` after
removal; whatever it could not remove stays registered, `left_over` lists
it, and the environment's `close()` retries it.

**Close.** Key-scan and stop the harness containers first, so nothing
writes after the watchers' last batch; stop the watchers; remove the probe
copies' registries (retrying anything an earlier copy close left) and then
everything registered, containers, images, volumes and networks, each on
its own, confirmed, retried once, outside any deadline. The final removal
results go into `environment.json`, and the driver copies them into any
export it published. `close()` can be called again: a later call retries
whatever `left_over` still lists.

## What was exercised

By the integration tests in `tests/driver/test_integration.py` (results
and dates in `cohbench/driver/README.md`): start with the scripted upstream
and every check after `prepare`, the gateway reading its credential copied
into the created container; the watcher under its isolated launch contract
read ready from its heartbeat file, and a metric command that runs as its own user at
`/work/project` and cannot list `/srv/w`; the manifest admission and every
key refusal (bytes, gzip, git history loose and packed); coached and default
instruction files; marks at turn ends and at a correction's delivery, and
settled final marks at a handoff and at the export; handoffs with a nested
repository, an interrupted turn and an interrupted child; a paused probe
capture with its unpause confirmed, and SIGTERM during one; the export's
quiescing and its capture from stopped containers; the key scan with a
stand-in secret; removal after a normal close, an exception and SIGTERM.

Not exercised: the snapshot key scans of a probe copy (they run only with a
key); `MainNotResumed`; a probe copy's cleanup failing and retried at
close; a handoff failing at a named stage; native Linux, where the
credential copy matters (this host is Docker Desktop 27.4.0); the key in a
zip, bzip2 or xz file at admission and a repository under a directory named
`objects` (unit tested, or not at all for the latter); Docker 28 or later;
a provider other than Anthropic; submodules in a handoff; `remove(record)`
for images; and Claude Code's image.
