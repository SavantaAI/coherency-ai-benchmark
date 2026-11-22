# Filesystem watcher

The watcher records every change to the task workspace as a sequence of
verified batches, stores each distinct file content once, and takes a
metric reading of every committed batch. Scoring rebuilds the tree at any batch boundary
and diffs any two boundaries, on the host, from the output directory alone.

It runs in its own container (decision D-014). The workspace is a Docker
volume. The agent's container mounts it read-write. The watcher container
mounts the same volume read-only, has no network, and writes to an output
directory that only the host and the watcher can see.

Python 3.12, standard library only. inotify and openat2 are called through
ctypes.

## Design

inotify events are hints. They say which paths may have changed and which
renames happened; they never decide what is recorded. When the pending batch
is due, the watcher:

1. Scans every dirty path (see "Reading the tree") on top of its last
   manifest. Hints that arrived on a watch are also resolved by that
   directory's identity in the scanned tree, and a directory whose watch
   path went stale through an unpaired move, or that arrived by an unpaired
   `IN_MOVED_TO`, is listed again.
2. Validates the scan: re-lstats every path of the scanned manifest and
   every other path the scan lstat'ed (the paths it saw), through the
   resolver, and reads the inotify queue. If anything moved, raced the scan or was reported, those paths
   (carried through any renames reported meanwhile) are scanned again, with
   no cached read reused for a path reported or seen moving, and the scan is
   validated again, up to 4 rounds. A batch whose last round came back clean
   is `settled`: at the start of that round every entry had the value
   recorded, so the boundary is a state the tree had at one instant. If the
   tree did not hold still, the batch is committed with `settled: false`
   and the paths still moving in `unsettled`; that boundary is not
   necessarily a state the tree had. Hints that arrive and cannot be acted
   on in the batch (a change of the root's own attributes, an overflow) go
   back to the pending set with a deadline; after every batch the watcher
   checks that nothing outstanding lacks one.

   What validation re-checks: the lstat signature (inode, mode, owner,
   size, mtime, ctime) of every path of the scanned manifest and of every
   other path the scan saw, and the event queue. What it does not re-check: the content of
   a file whose signature is unchanged and for which no event arrived (a
   write in the same clock tick that raises no event would not be seen;
   every ordinary write raises one), the full listing of a directory that
   was not listed in this batch (a new entry in it raises an event), and the
   coarse git state (read once per batch when `.git` had an event).
3. Derives records from the difference between the last manifest and the
   scanned one. An entry's identity is its device, inode and type. An entry
   is kept where its identity appears at the path its parent now has; it
   moved where its identity appears at the path the batch's renames map it
   to; a file or symlink replaced in place is modified; everything else is
   deleted or created. Renames only confirm a move the identities already
   show. One more confirmation counts: an `IN_MOVED_FROM` whose
   `IN_MOVED_TO` never arrived (the destination was a directory created in
   the same batch, not yet watched) confirms a move to the one new path
   where that inode now appears, since a renamed inode stays allocated.
4. Orders the records so that applying them in order is legal: deletions
   deepest first, moves, creations shallowest first, applied in rounds
   until none is left, then in-place changes.
5. Self-check, before writing anything: applies the records to the last
   manifest with the same code `state_at` uses (`manifest.apply_record`)
   and compares the result with the scanned manifest. If they differ, or no
   legal order exists (a cycle of renames), the batch is written as a
   `resync` (deletions and creations from the plain difference) with
   `"check": "failed"` and the reason. A resync that still does not
   reproduce the scanned manifest stops the watcher with an error.
6. Writes the batch's records and its `batch` record in one write.

The log therefore always replays, and it replays to what was scanned.

### What the log can and cannot tell

- A batch is the unit of truth. Changes inside one batch merge; the order of
  operations inside a batch is not recorded; no point inside a batch is
  claimed to be a state the tree had. `state_at` and `diff` take batch
  boundaries only. Scoring should use settled boundaries. Batches are cut
  only by the file activity (the quiet period and the hold limit) and by
  the final scan at a segment's end, never by a mark (D-037).
- For the two Steering shares, a file change is the net change of one path
  between two consecutive batch boundaries (decision D-025). Writes that
  land in the same batch (by default within 100 ms of each other) count
  once, by their net effect; a file created and removed inside one batch is
  not a change. A correction's delivery is an instant: a mark for it names
  the batch in effect then, and a batch that began before it and ended after
  it is reported as `straddled`, its changes on neither side.
- Loops are counted from attempt records (decisions D-022 and D-025), not
  from this log. The watcher supplies the validated state boundary that the
  attempt records are joined to.
- A temp file that is created and renamed within one batch never appears as
  an entry. The target's record says `via_rename`, and the batch record
  lists the observed renames.
- Three identical writes inside one settling window are one `rewritten`
  record, attached to the file that received the write. A write hint
  belongs to a file lifetime, not a path: it follows its file through later
  renames and is dropped when the file is deleted or replaced, so a later
  file at the same path never inherits it. An open and close without a
  write records nothing.
- A file whose content cannot be read (decision D-032): once it has been
  written, every record of its inode carries `content_unknown: true` for the
  rest of the inode's life, on every name, also after the file becomes
  readable again (what was written while it could not be read is never
  known), and every later write is a `modified` record. The guarantee is that such a file is never reported as unchanged
  content after a write on any of its names, and never as `rewritten`.
  `epoch` counts the writes seen to the inode as audit detail, best effort:
  writes that land in one batch, or arrive through a name the watcher did
  not hear about, can be counted once.
- A change that raises no inotify event (a write through a shared `mmap`,
  say) waits for the next batch, which another event starts, or for the
  final scan at stop: the lstat sweep runs only inside a batch, and a mark
  never scans.
- A batch with no change is not written, with two exceptions: the full
  rescan after an inotify queue overflow, and the full rescan after a batch
  could not be written (`reason: "recovery"`), are committed even when they
  find nothing, as a record that it happened.
- The root `.git` contributes HEAD and the refs only (the coarse git
  state); every ordinary file under `.git` (index, objects, config, logs,
  `packed-refs` itself) is left out. There is no mode that records `.git`
  as ordinary files.
- Left out at the source (decision D-026): files and directories named
  exactly `CLAUDE.md` or `AGENTS.md` at any depth, everything below such a
  directory, the temporary forms of those names listed below, and git refs
  under `refs/t3/` (loose and packed). The temporary forms, with `N` standing for the
  name, `*` for any characters and `?` for one: `.N.tmp`, `.N.tmp.*`,
  `.N.tmp-*`, `.N.??????` (Python's `tempfile` and similar), `N.tmp`,
  `N.tmp.*`, `N.tmp-*`, `N~`, `.N.swp`, `.N.swo`, `.N.swx`, `N.swp`, `.#N`,
  `#N#`. Other names that contain an ignored name, such as
  `docs/AGENTS.md.example` or `notCLAUDE.md`, are recorded.
  Nothing the watcher writes names them: no record, `renames` entry,
  `via_rename`, `unsettled`, `unstable`, hint, heartbeat field or blob, and
  `config.json` lists the configured names only as SHA-256 hashes. Activity
  on them is invisible to batching too: it moves no batch clock, first or
  last event or write time or event count, never cuts a batch and never
  makes one unsettled, so the same task writes give the same batches with or
  without it. Two rules make that hold. A change under `.git` touches the
  clock only when the git state with the ignored refs filtered out differs
  from the last one read, so a `refs/t3/` update in `packed-refs`, or a
  repack that only replaces `packed-refs` with a new inode through
  `packed-refs.lock`, moves nothing. And when validation finds a directory whose inode, mode and owner
  are unchanged but whose timestamps moved, it lists the directory and
  compares its tracked children with the manifest; an ignored file created
  in a tracked directory therefore does not unsettle the batch. A rename
  with one ignored end is a creation or deletion at the other end; with both
  ends ignored it is nothing. Paths under the `ignore` globs are treated the
  same way. Tasks may not contain files with those names. The lists are
  configuration (`ignore_names`, `ignore_name_forms`, `ignore_git_refs`).

### When a batch is due

When no event has arrived for `quiet_ms` and no file is open for writing (an
`IN_MODIFY` not yet followed by `IN_CLOSE_WRITE`), or when the batch's first
event is `max_delay_ms` old. A writer silent for `max_delay_ms` stops
holding batches at that instant (its last write plus `max_delay_ms`), with
no further event needed: the deadline is the later of the quiet time and
the open writers' expiry times, capped at `max_delay_ms` after the first
event. A batch already due is committed before the next queue read, so
events read after its deadline never join it. An open writer also ends when its file is deleted, replaced
by a create or a rename onto its path, or moved out of the tree, and when an
ancestor directory is removed; when an ancestor is renamed, the writer
follows it to the new path. The two halves of a move are paired by their
cookie within one queue read, whatever events lie between them (an
instruction file's, say). When a directory leaves the tracked tree (an
`IN_MOVED_FROM` whose `IN_MOVED_TO` is not in the same read, as when it
moves outside or under an ignored or unwatched directory, or its watch is
removed), the watches of its subtree are dropped at once together with
every writer below it, so a later event on a stale watch cannot bring a
writer back; the next scan reconciles. A move whose two halves arrive in
different reads is handled the same way and costs a fresh listing of the
destination. A batch taken while still busy is marked `forced`. At most four 1 MiB buffers of events are read per loop step, so a
flood cannot starve deadlines, the heartbeat or a stop request. Events on
ignored paths, and `.git` events that leave the filtered git state
unchanged, are dropped before they reach this clock.

### Reading the tree

- Every lstat, readlink, open and listing goes through a descriptor for the
  root, resolving each component without following symlinks: openat2 with
  `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` when the
  kernel has it (the heartbeat field `resolve` says `openat2`), otherwise
  openat one component at a time with `O_NOFOLLOW | O_DIRECTORY`. No path
  with a slash is ever handed to a plain stat or open, in the scan, the
  sweep, the validation or the git reader. A path that can only be reached
  through a symlink is absent from the tree. Watches are added through
  `/proc/self/fd/N` of the safely opened directory.
- A dirty path is lstat'ed. A directory is listed (recursively) only when it
  is new at that path, has another identity than before, was inaccessible,
  has no watch, or is listed again by step 1 above; its watch is added
  before it is listed. A directory renamed within the tree is carried over
  with its subtree instead of being listed, unless an ignore pattern could
  treat the old and new locations differently.
- File content is read only when the inode's size, mtime or ctime changed
  since the last read of it, with one content open per changed inode per
  scan (hard links share it) and a single read call for files under 64 MiB.
  A reading is reused across scans only if the file's timestamps were more
  than 2 s older than the read, because timestamps are coarse and a write
  right after a read could otherwise leave them unchanged.
- Each batch also lstats every known path, directory by directory through
  the resolver, and rescans those whose inode, mode, owner, size, mtime or
  ctime moved. This catches changes no hint names, such as a chmod through a
  hard link that was itself replaced in the same batch. Every other link of
  a rescanned file is rescanned too.
- Coarse git is read in one traversal per scan of `.git` and `.git/refs/**`,
  each directory opened through the resolver.
- Ignore rules are applied to the current paths during the scan, so a move
  into or out of an ignored location is a deletion or a creation on the
  tracked side.
- A directory this process cannot list or search is recorded with
  `content: "inaccessible"` and no children; when its mode changes back it
  is listed again. The root itself is tracked the same way: an attribute
  change on the root triggers a full listing, which finds nothing while the
  root is inaccessible (`root_accessible: false` in the batch record) and
  everything once it is accessible again.

## Output directory

```
events.jsonl     records, one JSON object per line
metrics.jsonl    one JSON object per metric reading (absent without a metric command)
blobs/ab/abcd... file contents and symlink targets by SHA-256, each stored once
heartbeat.json   rewritten every heartbeat_s seconds
config.json      the effective configuration (metric_isolation as the metric actually runs)
control/         mark requests and acknowledgements (see Marks)
```

The watcher refuses an `events.jsonl` that already holds records, and an
output or work directory inside the watched root.

### events.jsonl

Every record has `seq` (1, 2, ..., no gaps), `kind`, `wall` (UTC, ISO 8601
with microseconds) and `mono` (`CLOCK_MONOTONIC` seconds in the watcher
container). State records and batch records have `batch`.

State records (`created`, `deleted`, `moved`, `modified`, `attrib`,
`rewritten`) carry the entry:

| key | meaning |
|---|---|
| `path` | relative to the root, `/` separated; for `moved`, the destination |
| `type` | `file`, `dir`, `symlink`, `fifo`, `socket`, `char`, `block`, `other` |
| `mode` | permission bits as 4 octal digits, `"0600"` |
| `uid`, `gid` | owner |
| `dev`, `ino` | identity; paths with the same pair are hard links |
| `size` | bytes for files, target length for symlinks, null otherwise |
| `sha256` | content hash; for a symlink, the hash of its target string |
| `content` | `stored` (in blobs/), `oversize` (hashed, not stored), `unreadable`, `inaccessible` (directories), `none` |
| `target` | symlink target, never followed |
| `error` | errno name when content could not be read, `EACCES` for example |
| `epoch` | only for an unreadable file that has been written: writes seen to the inode, best effort |

Extra keys: `from` on `moved`; `changed` (a list of `content`, `mode`,
`owner`, `identity`, `type`, `access`) and `prev_sha256` on `modified` and
`attrib`; `prev_type` on a type change; `via_rename` when the new content
arrived by renaming a temp file over the path; `content_unknown: true` on
every record of an entry with a nonzero `epoch`, and on a `modified` whose
old and new content are both unknown.

Kinds: `created`; `deleted` (a directory only once it is empty); `moved` (a
directory carries its subtree; the entry is unchanged by the move, changes
follow as separate records); `modified` (content); `attrib` (mode, owner,
identity, accessibility, or a type change between non-directories; a change
to or from a directory is a deletion and a creation); `rewritten` (a write
that left a readable file's entry identical); `git` (coarse git state with
`head`, `refs`, `backend`; a symbolic ref whose target lies in an ignored
namespace is left out like the ignored ref itself, and a `head` pointing
into one is null).

The `batch` record ends each batch: `batch`, `records` (the count before
it), `reason` (`baseline`, `settled`, `forced`, `overflow`, `recovery`,
`final`), `settled`, `unsettled` (at most 200 paths), `validation_rounds`,
`check` (`ok` or `failed`), `check_error`, `resync`, `recovered` (a full
rescan: after an inotify queue overflow or a failed batch write, with
renames not trusted, or after an attribute change on the root, with
renames trusted), `forced`, `root_accessible`, `events` (inotify events in
the batch), `first_event` and `last_event` (when the watcher began the
queue reads that delivered the first and last of them), the write times
(below), `scan_ms`, `reads` (content opens), `listed` (directories listed),
`validation_listed` (directories validation listed to compare their
tracked children), `swept_dirty` (paths the lstat sweep flagged; a tracked
directory whose timestamps moved only because an ignored file was created
in it is counted too, and then found unchanged),
`renames` (observed, at most 200), `unstable` (files that changed while
read), `unwatched` (directories without a watch and why), `entries`.

The baseline is batch 0.

Write times are timed by read (D-037): every event of one queue read is
stamped with the instant that read returned, and it happened after the
previous read began. So for a batch:

- `write_times`: `known` (from change notifications: create, modify, move,
  delete, attribute change; an open or close alone is not one), `none` (no
  change notification: the baseline, or a change only the lstat sweep
  found) or `unknown` (notifications were lost: an overflow or a failed
  batch write).
- `first_write_after`: a lower bound. Every write of the batch happened
  after it: the start of the last queue read before the first change
  notification that emptied the queue (a read that stopped at its buffer
  cap proves nothing about what is still waiting, so it never moves this
  bound), or, when the batch holds a change no notification covered or has
  no known write times, at most the start of the previous batch's scan (the
  last moment the watcher is known to have looked at the path). Null for
  the baseline.
- `first_write` and `last_write`: upper bounds, the times the reads that
  delivered the first and last change notifications returned. When a pass
  found a change no notification covered (an entry of a directory listed
  in this scan, which can appear before the directory's watch exists, a
  path the lstat sweep flagged that no hint named, or a path validation
  found changed that no hint named), `last_write` is the time the scanning
  ended instead. Null unless `write_times` is `known`.
- `wall` of the batch record is its commit time: the upper bound of a batch
  whose write times are `none` or `unknown`.

Validation reads the queue until a read empties it before it calls a batch
settled; a queue still not empty after 64 reads leaves the batch
unsettled. Every bound is recorded in microseconds, and the watcher answers
marks from the recorded values.

The watcher reads the queue as soon as inotify has something (0.2 to 1.6
ms after a write in the two-container check runs of 2026-11-22), so the
bounds are usually a millisecond or two apart; a watcher busy scanning
reads later and widens them. Before 2026-11-22 (round 7) `first_write` and
`last_write` were the times the reads began.

The `mark` record: `label`, `request` (the control file id), `accepted`,
`at`, `batch`, `straddled`, `straddled_last`, `covered_utc` and `settled`
(the named batch's last validation round came back clean); see Marks. A mark that was not
accepted names no batch (`batch: null`) and is not a boundary. A mark
record never closes a batch, and the batch it names can be earlier than
the last committed one.

### metrics.jsonl

The command never sees the live workspace. Every committed batch gets one
reading, in batch order, taken from that batch's own manifest (a snapshot
made at its commit and never changed afterwards) and the blob store: a
mirror updated incrementally, then a fresh copy per run, in which the
command runs. Which batches are measured therefore does not depend on how
long a reading takes; a slow command only makes the queue longer (the
heartbeat's `metric_queue`). A batch whose copy would be identical to one
already measured (the same `tree_sha256`, a digest over every path's type,
mode, content hash, link target, content state and blob presence, whether
a git state exists, and the effective run settings: command, timeout,
output limits, isolation, workspace path and copy mtime, numbers
normalised so `7` and `7.0` give the same key) reuses that reading: the record carries
`reused_from` (the batch measured) and `reused_run`, the same values, and
no run of its own. A reading that timed out is never reused.

In the container image (`isolation: "users"`) the copy is built and the
command runs as a separate user, 1500:1500, at the workspace's own path
`/work/project` (a link in the image to that user's private directory under
`/tmp`), so an absolute link into the workspace resolves into the copy. That
user cannot read or traverse `/srv/w`, which holds the live volume and the
output directory. Link targets are resolved by one resolver, component by
component in filesystem order through the links the batch records, whether
the target is absolute or relative and whether it is reached directly or
through another link. A symlink in the copy whose target lies outside the
workspace is created dangling (under `/.outside-workspace/`) and listed.
Cleanup removes the copy by descriptor, never following a link, and kills
any process of the metric user the command left behind. A directory's mode
change updates the copy in place; a removed, retyped or renamed entry is
rebuilt with everything below it, and the blobs requested from the watcher
are computed after that invalidation. Every entry of the copy (files,
directories, links, the copy's top) gets one fixed mtime,
`metric_copy_mtime` (default 946684800, 2000-01-01T00:00:00Z; the launch
can pass the task manifest's), so the same tree always looks the same to
the command whatever the mirror held before. Before the command runs, the
copy is verified against the batch's manifest: every expected entry present
with its type, mode, size, mtime, content hash or link target, and nothing
else; a mismatch is a failed reading (`error: "copy verification failed:
..."`), never a successful one. Blob frames to the metric server carry a
presence byte, so no content is special, and the watcher serves only
contents the submitted manifest names: a request for any other digest
breaks the server connection and fails the reading. Without root (tests,
`isolation: "none"`), the copy is built in `work_dir` (a temporary
directory, removed at shutdown, when none is given) and run as the
watcher's own user with the same clean environment; absolute links into
the workspace are pointed at the copy, keeping every component of the
target so a link that is broken in the batch stays broken; this mode is
not isolated.

Every executed reading gets a fresh home and temporary storage. `HOME`,
`TMPDIR`, `TMP`, `TEMP` and the XDG home variables point into its private
run directory, removed after the command. On Linux both executors enter
private user and mount namespaces with the existing container permissions.
All existing mounts become read-only; `/tmp`, `/var/tmp` and `/dev/shm`
receive fresh tmpfs mounts; only this invocation's home and workspace are
attached as writable copies. Namespace capabilities are dropped before
executing the command. These mounts disappear when its processes exit.
No additional container capability or security option is used. The kernel
must support unprivileged user namespaces and the `open_tree`, `move_mount`
and `mount_setattr` APIs; failure starts no measured command and records a
failed reading. macOS development runs use its system sandbox to restrict
writes to the fresh home and workspace. Offline `complete-metrics` uses
the same executor and storage rules. Reused readings execute no command.

The environment passes the metric argv in a one-shot JSON file under the
watcher's private `/srv/w/out`. The watcher reads and removes it before
starting metrics. The metric user cannot traverse `/srv/w`. The server
re-executes with a clean argv and environment before receiving commands
over its private pipes. Thus the startup command is not in its ancestors'
argv or environment. `metric_cmd_file` (`--metric-cmd-file`) exposes this
startup channel; `--metric-cmd` remains available for tests and offline
invocations, but is not the environment's route. A measurement service
that imports agent code must also consume its private arguments and clear
its own argv before that import.

A reading is a reading of the tree only when it is `complete`: the command
ran and the copy held every readable tracked file. A file left out because
it is larger than `max_blob_bytes` (5 MiB by default), or whose blob is
missing, makes the reading `complete: false`, with those paths in
`lacking`; the values and the `absent` list are kept for audit, and
consumers treat such a reading as no reading. What the workspace itself
does not give is not a reason: a file the watcher cannot read, an
inaccessible directory and the coarse git state are listed in `absent` and
leave `complete` true. The size limit stays: a task that expects large
generated files puts them under an ignored path.

| key | meaning |
|---|---|
| `run` | 1, 2, ... for each run of the command, continued by `complete-metrics`; null for a reused reading and for a `backlog` record |
| `batch` | the batch measured |
| `reason` | `baseline` (batch 0), `change` (every later batch), `backlog` (not measured, see below), `internal` (a worker exception) |
| `measured` | the command ran on this batch's tree (it exited or timed out) and the reading was written, or the reading was reused from one that did |
| `complete` | see above |
| `lacking` | readable tracked files the copy did not hold (first 1,000) |
| `tree_sha256` | the reuse key: digest of what the copy holds and the run settings |
| `reused_from`, `reused_run` | the batch and run whose reading this is; null when the command ran for this batch |
| `offline` | true for a reading added after the episode by `complete-metrics` |
| `isolation` | `users` or `none` |
| `absent`, `absent_count` | paths missing from the copy and why (oversize, unreadable, inaccessible directory, missing blob, coarse git state as `.git`), for the whole snapshot on every reading (first 1,000 listed) |
| `links_outside`, `links_outside_count` | symlinks whose target is outside the workspace, created dangling, with their targets |
| `cwd` | where the command ran (`/work/project` when isolated) |
| `stragglers_killed` | isolated only: leftover metric-user processes killed after the run |
| `start`, `end`, `duration_s` | |
| `exit`, `timed_out` | negative exit for a signal; the whole process group is killed at `metric_timeout_s` |
| `ok` | exit 0, stdout exactly one strict JSON object, and the reading written |
| `metrics` | the parsed object or null |
| `error`, `parse_error` | `timeout`, `exit status N`, `could not start: ...`, `internal: ...`, `not measured: ...`, or the parse error |
| `stderr`, `stderr_truncated` | cut at `metric_stderr_max` bytes |

Strict JSON: no `NaN` or `Infinity`, every number at most 64 characters,
finite. Any exception while preparing the copy, running or parsing becomes
a failed reading, and the worker keeps going. Readings are serial. The
baseline reading finishes before the heartbeat says `running`. A mark's
reading is the reading of the batch the mark names. The copy does not
preserve hard links.

At shutdown one deadline covers the whole metric job: the queue is drained
for at most `metric_drain_s` seconds (45 by default), and the job in
progress then has until `metric_drain_s` plus `metric_timeout_s` (75 s by
default) to finish, its copy and protocol work included. A job still
running at that deadline is given up (its reading is never written). It and
every batch still queued get a record with `reason: "backlog"`,
`measured: false`, `ok: false` and null `metrics`. The readings of a
finished log can be completed afterwards, from its blobs, exactly as the
live worker would have taken them:

```
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=<two copies of the tree> \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges \
  --mount type=bind,src=<host-output-dir>,dst=/srv/w/out \
  --mount type=bind,src=<task-metric-dir>,dst=/opt/task,readonly \
  <image> complete-metrics /srv/w/out
```

It runs only in the watcher image, started as root like `run`: it checks
the same layout, forks the same metric server as 1500:1500 and runs the
command with the same isolated executor, path (`/work/project`), clean
environment and link handling. Every setting comes from the run's recorded
`config.json`, read after the privilege drop (root without capabilities
cannot enter the output directory): the command, timeout and output limits
for the runner and the copy mtime for the executor. It fails if the config
cannot be read or lacks one of them, if the recorded metric isolation (the
one the run actually used) is not `users`, and if the recorded users or
workspace path differ from the image's (1000:1000, 1500:1500, `/work/project`), with
which the metric server was started. Anywhere else it refuses (exit 2,
`"only in the watcher image"`). It measures every
committed batch without a measured reading, appends the readings to
`metrics.jsonl` with `offline: true` and run numbers continuing after the
last, and reuses an earlier reading when the reuse key matches. It refuses
while the heartbeat says the watcher is starting, running or stopping.

### heartbeat.json

`status` (`starting`, `running`, `stopping`, `stopped`, `failed`), `error`
(with a traceback tail; any unexpected exception, or a fatal root event
seen during shutdown, makes the status `failed`), `wall`, `mono`, `batch`,
`last_seq`, `records`, `entries`, `watches`, `pending_paths`, `overflows`,
`resyncs`, `marks`, `unwatched`, `metric_runs`, `metric_failures`,
`metric_reused`, `metric_queue` (batches waiting for a reading, the one in
progress included), `metric_backlog` (batches recorded as `backlog`),
`metric_isolation`, `resolve`, `event_log_broken`, and after shutdown
`final_settled`,
`late_events`, `late_overflow`, `late_capped` and `still_changing`
(tracked-path inotify events, including an overflow, that arrived after the
final batch was closed, read for at most 16 buffers; `final_settled` comes
from the final scan, taken after any outstanding marks). The file is
mode 0644. `python -m cohbench.watcher alive OUT [--max-age S]` exits 0 only
when the status is `running` and the heartbeat is at most `--max-age`
seconds old. The image has no health check; the launcher reads the
heartbeat from its side of the output bind (or runs `alive` there).

A failed write to `events.jsonl` (a full disk, say) stops the watcher with
status `failed` and the error. Each batch is one write call; on a failure
the file is cut back to the end of the last complete batch and the sequence
counters go back with it, so the shutdown drain appends after it with no
gap. The failed batch's scan has already consumed its hints and moved the
scanner's signatures, so the next batch (the shutdown drain's, here) is a
full rescan against the last committed manifest (`reason: "recovery"`). If the file cannot be cut back, `event_log_broken` names the failure
and every later write raises: nothing more is appended.

## Marks

```
python -m cohbench.watcher mark OUT LABEL [--at UTC] [--limit-s S] [--timeout S]
```

writes `control/<id>.request.json` with `{"label": ..., "at": ...,
"limit_s": ...}` (the last two optional) and waits up to `--timeout` for
`control/<id>.ack.json`. Decision D-037: a mark never closes an open batch,
so the batches of an episode do not depend on when or how often anyone
marks.

- `at` is the instant the mark names, UTC ISO 8601 with an offset; without
  it, the time the watcher took the request.
- A mark is a pure observer: registering it reads nothing, scans nothing,
  commits nothing and wakes nothing. The control directory is polled every
  0.25 s, not watched, because a watch on it would share the workspace's
  inotify queue and a request would cause a workspace read. The watcher
  answers from its regular reads, once every write up to `at` is in a
  committed batch: once a regular queue read that began at or after `at`
  has returned without stopping at its buffer cap (a write that completed
  before a read begins is in that read, because inotify queues the event
  before the write returns; a read that stopped at the cap may have left
  events queued and confirms nothing) and no open batch holds a
  notification from that read or an earlier one. It waits for its own
  batching, the quiet period and the hold limit, to close
  that batch; it does not shorten the quiet period or scan an open batch.
  Writes after `at` that keep the batch open are waited for too, up to the
  hold limit: the batch closes when it would have closed with no mark.
- `limit_s` (default 10 s) bounds the wait and must exceed the hold limit
  (`max_delay_ms`); a request that does not is refused as a bad request.
  The limit runs from the request to when coverage first held: a mark not
  covered within it, even one covered later, is recorded with
  `accepted: false` and names nothing. So is a mark still waiting when the
  watcher stops and its final batch did not cover it.
- Each committed batch has bounds: its writes happened after
  `first_write_after` (lo) and at or before `last_write` (hi; the commit
  time when write times are `none` or `unknown`). Honest bounds can
  overlap, so a run of batches can straddle an instant. `straddled` is the
  first (lowest-numbered) batch with lo < `at` < hi and `straddled_last`
  the last such batch (equal to `straddled` when only one, both null when
  none): timing by read cannot place those batches' writes on either side
  of the instant, so their changes belong to neither side. `batch` is the
  last batch with hi at or before `at` that comes before `straddled` (any
  batch when nothing straddles), the baseline when there is none. The rule
  is `replay.answer_for`; the watcher answers with it over the batches
  committed when the mark is answered, and replay checks every mark with
  it. `covered_utc` is when coverage was confirmed.
- These answers are conservative where the watcher cannot know the time:
  all events of one queue read share one stamp. So two writes closer
  together than the watcher's read interval can share a batch, and a mark
  between them reports that batch as straddling. The read follows a write
  by 0.2 to 1.6 ms when the watcher is idle (two-container check runs of
  2026-11-22); while it scans a large batch the interval is the scan time.
- A change that raised no notification (a write through a shared `mmap`,
  say) is not found by a mark: it is found by the next batch's lstat sweep
  or by the final scan at stop.

The acknowledgement has `ok`, `accepted`, `label`, `at`, `batch`,
`straddled`, `straddled_last`, `covered_utc`, `settled`, `mark_seq`, `wall`
and `metric`. A
mark creates no reading of its own (D-036), and the acknowledgement never
waits for one: it is written as soon as coverage is established, so
`limit_s` covers batching only. `metric` is the named batch's reading
(`run`, `batch`, `ok`, `error`, `metrics`, `logged`, `measured`,
`complete`, `reused_from`) only if it is already written, else null;
consumers find readings by batch in metrics.jsonl. `ok` is false when the
mark was not accepted (with an `error`). The command exits non-zero when
`ok` is false. There is no `accept_unquiescent` request field and no
`--accept-unquiescent` flag: a mark does not drain, so there is no
unquiescent form.

`Watcher.mark(label, at=None, limit_s=None)` does the same in process and
returns the mark record.

`replay.mark_batch(out, label)` finds the batch of the last accepted mark
with that label and raises `ReplayError` when there is none.
`replay.marks(out)` lists accepted marks only; `include_rejected=True` adds
the rejected ones. A mark record without the `accepted` field (written
before 2026-11-22) counts as accepted.

## Shutdown

The contract: the runner stops or pauses the agent's container, asks for a
final mark, then stops the watcher (`docker stop -t 90`). On SIGTERM the
watcher takes outstanding mark requests, reads the queue, commits a final
batch (the only forced close of a batch, the segment's end), answers the
marks that batch covered, drains the metric queue within one deadline of
`metric_drain_s` plus `metric_timeout_s` (75 s by default, the job in
progress included), records what is left as `backlog`, and writes
the heartbeat with `final_settled` and `still_changing`. With the defaults
that fits in the 90 s stop timeout when the final scan is quick; a longer
`metric_timeout_s` needs a longer stop timeout or a shorter drain.

## Configuration

A JSON file (`--config`) with these keys, or the flags. Flags win.

| key | flag | default | |
|---|---|---|---|
| `root` | `--root` | required | watched tree |
| `out` | `--out` | required | output directory outside the root |
| `ignore` | `--ignore` (repeatable) | `[]` | globs relative to the root |
| `ignore_names` | `--ignore-names` (JSON list) | `["CLAUDE.md", "AGENTS.md"]` | exact basenames left out at any depth |
| `ignore_name_forms` | `--ignore-name-forms` (JSON list) | the forms listed under "What the log can and cannot tell" | temporary forms of those names, `{name}` standing for the name, fnmatch syntax; must include `"{name}"` |
| `ignore_git_refs` | `--ignore-git-refs` (JSON list) | `["refs/t3/"]` | ref namespaces left out of the git state; each a whole path prefix (`refs/t3` never matches `refs/t30/x`) |
| `max_blob_bytes` | `--max-blob-bytes` | 5 MiB | larger files are hashed, not stored |
| `quiet_ms` | `--quiet-ms` | 100 | |
| `max_delay_ms` | `--max-delay-ms` | 2000 | |
| `metric_cmd` | `--metric-cmd` (JSON list) | null | argv for tests and offline use |
| `metric_cmd_file` | `--metric-cmd-file` | null | one-shot private JSON argv file, removed after reading |
| `metric_timeout_s` | `--metric-timeout-s` | 30 | |
| `metric_drain_s` | `--metric-drain-s` | 45 | shutdown drain of the metric queue; the job in progress gets `metric_timeout_s` more |
| `metric_copy_mtime` | `--metric-copy-mtime` | 946684800 | mtime of every entry in a metric copy |
| `metric_stdout_max` | `--metric-stdout-max` | 1 MiB | more is a failed reading |
| `metric_stderr_max` | `--metric-stderr-max` | 4096 | |
| `heartbeat_s` | `--heartbeat-s` | 1.0 | |
| `work_dir` | `--work-dir` | a new directory in `/tmp` | copies for `isolation: none` |
| `workspace_path` | `--workspace-path` | `/work/project` | where the agent sees the workspace |
| `metric_isolation` | `--metric-isolation` | `auto` | `users` when started as root, else `none` |
| `watcher_user` | `--watcher-user` | `1000:1000` | |
| `metric_user` | `--metric-user` | `1500:1500` | |

Ignore patterns: `*` and `?` stay inside one path segment, `[...]` is a
character class, `**` as a whole segment matches any number of segments. A
path is ignored when a pattern matches it or any ancestor, or when any of
its components is an ignored name or one of its forms. A pattern matches the whole relative
path, so `*.log` matches only at the top level and `**/*.log` at every
depth.

The watcher watches the root `.git` and `.git/refs/**` (not the ignored
namespaces), records nothing inside `.git`, stores no blob from it, and
records HEAD and the refs as `git` records. A reftable repository reports
`"backend": "reftable"` and no refs. The `git_coarse` key and its flags were
removed on 2026-11-22: a config file that still names `git_coarse` is
refused as an unknown key.

## Commands on the host

```
python -m cohbench.watcher state-at OUT BATCH|last [--json]
python -m cohbench.watcher diff OUT BATCH_A BATCH_B [--json]
python -m cohbench.watcher mark OUT LABEL [--at UTC] [--limit-s S]
python -m cohbench.watcher alive OUT [--max-age S]
python -m cohbench.watcher complete-metrics OUT    # in the watcher image only (see metrics.jsonl)
```

`cohbench.watcher.state_at(out, batch, verify_blobs=False)` returns a
`State` with `entries` (path to `Entry`, including owner and identity),
`git` (with the backend) and `links()` (groups of hard-linked paths, files,
symlinks and specials). With `verify_blobs`, every stored entry's blob must
exist and hash to its sha256. `diff(out, a, b)` returns one `FileDiff` for
every path whose entry differs and one for the git state if it differs:
`added`, `removed`, `modified`, `mode`, `owner`, `identity`, `type`,
`renamed`, `git`. A rename is reported only where the log recorded moves
taking one path to the other (equal inode numbers are not evidence: they
are reused), and a replacement in place ends the lineage: when another
inode takes a path (a `modified`, `attrib` or `created` record with a new
identity, or a move onto it), the earlier file there is not followed
further, so its later moves are not reported as its rename. Text content gets a unified diff; binary, oversize and unknown
content is reported as differing or unknown, with hashes where known. It
raises rather than return nothing for two states that differ.

`diff` rows are a presentation: a rename is one row for two paths, and a
resync batch shows deletion and creation of descendants whose entries did
not change. D-025's file-change shares are computed per path from adjacent
replayed manifests (`state_at` of consecutive batch boundaries), not from
`diff` rows.

Replay is strict. It raises `ReplayError` on a sequence gap, a batch whose
record count is wrong, a record that is not legal on the state built so
far, or a mark whose `batch`, `straddled` and `straddled_last` differ from
what `replay.answer_for` gives over the recorded bounds of the batches
committed before it (checked when every batch records its write times). A missing or corrupt blob raises only where content is needed: in
`state_at` with `verify_blobs=True`, and in `diff` for any blob it reads,
whose SHA-256 is checked against the entry; plain `state_at` does not read
blobs. Records after the last committed
`batch` record are a write in progress and are left out.

## Docker

```
docker build -f docker/watcher/Dockerfile -t <image> .

docker run -d --name <opaque-name> \
  --network none --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=<two copies of the tree> \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges \
  --mount type=volume,src=<workspace-volume>,dst=/srv/w/live,readonly,volume-nocopy \
  --mount type=bind,src=<host-output-dir>,dst=/srv/w/out \
  --mount type=bind,src=<task-metric-dir>,dst=/opt/task,readonly \
  <image> run --root /srv/w/live --out /srv/w/out --ignore '.git/**' \
    --metric-cmd '["python", "/opt/task/metric.py"]'
```

What a driver that used the earlier command changes:

1. Mount the volume at `/srv/w/live` and the output at `/srv/w/out` (were
   `/src` and `/out`), and pass those paths.
2. Start as root: drop `--user 1000:1000`, and add `--cap-add SETUID
   --cap-add SETGID` after `--cap-drop ALL`. The entry point forks the
   metric server as 1500:1500, drops itself to 1000:1000 before reading
   anything, and neither keeps a capability.
3. Drop `--init`: an init running as root without `CAP_KILL` cannot forward
   SIGTERM to the unprivileged watcher. The watcher runs as PID 1, handles
   SIGTERM itself and reaps orphans.
4. Add `exec` to the `/tmp` tmpfs options (the metric command runs from
   its copy there) and size it for a mirror and one copy of the tree.
5. The task's metric files (`/opt/task` here) must be readable by others
   (the metric user is not their owner).
6. Do not wait for Docker's health status: the image has no `HEALTHCHECK`
   (a health check execs into the container every few seconds, and on
   Docker Desktop each exec walks the workspace volume). Read
   `<host-output-dir>/heartbeat.json` instead: up when `status` is
   `running` and its `wall` is recent (or run `python -m cohbench.watcher
   alive <host-output-dir>` on the host), down when the status is `failed`
   or the container is no longer running.

Start it after the volume is populated and before the agent's container.

Before it reads anything, the entry point checks the documented layout and
refuses to start otherwise: `--root` and `--out` are directories directly
inside one private directory, given without links or dot components; that
directory is owned by the watcher user, mode exactly 0700, with no POSIX
ACL (a failure to inspect it for one is a refusal, except on a filesystem
without extended attributes, which cannot carry one); every directory above it is owned by root and writable by nobody
else; and the metric user can traverse neither path. The image's `/srv/w`
is that layout.

Why these users: the agent writes as uid 1000 (`node`) and its files can be
mode 600 in mode 700 directories; only the owner can read those, and as the
owner the watcher can open them with `O_NOATIME`. The read-only mount stops
it writing to the workspace. The metric user is distinct, so it cannot read
the agent's private files in the live tree even if it could reach them, and
`/srv/w` (mode 0700, owned by the watcher user) keeps it from reaching them.
The metric user could read the memory of its own server process, which
holds only the batch being measured.

`volume-nocopy`: Docker copies an image directory's contents and ownership
onto an empty named volume mounted over it, which would be a write into the
workspace on the watcher's behalf.

Name the container with something opaque. `--network none` keeps it off
every Docker network, so no container can resolve its name.

## What the agent's container can see

Measured by `tests/watcher/two_container_check.py` on Docker Desktop
(kernel 6.10.14-linuxkit) on 2026-11-22, with the image's isolated metric
runs. The writer container ran `inotifywait -m -r -e
open,access,close_nowrite` on the whole workspace for the entire run, which
is what an agent that installed inotify-tools would do.

An agent subscribed to those notifications sees the watcher's reads:

- For every file whose content the watcher reads: one `OPEN`, one `ACCESS`
  and one `CLOSE_NOWRITE` on it, about 100 ms after the agent's change. A
  file is read only when its inode's size, mtime, ctime, mode or owner
  changed, once per scan even when hard-linked.
- For every directory the watcher lists (a new directory, one moved in from
  outside the tree, one whose permissions came back, one whose listing
  raced a move, or one whose timestamps moved during the scan while its
  inode, mode and owner did not, which validation lists to compare its
  tracked children): one `OPEN,ISDIR`, two `ACCESS,ISDIR` and one
  `CLOSE_NOWRITE,ISDIR`, which a recursive subscriber receives twice (from
  the parent's watch and from the directory's own).
- At watcher start, the baseline: every directory listed and every file read
  once. Starting the watcher before the agent's container keeps this out of
  the agent's view.
- After a change under `.git`: one opening of `.git`,
  `HEAD`, `packed-refs` and each loose ref directory and ref outside the
  ignored namespaces (from the code; not measured by the check).

What it does not see: the lstat sweep, every path resolution (fstatat and
openat2 with `O_PATH`) and the validation pass raise no notification,
except the validation listing named above; the metric command runs on a
private copy in the watcher's container.

Counts in the three probe windows of the same series (`printf > X.txt;
printf >> X.txt; mkdir X.d; printf > X.d/f`, the name normalised to `P`;
`P.d/` is the directory reported by its own watch), from the report:

| notification | watcher off | watcher on | on, with a mark during the series |
|---|---|---|---|
| `P.txt OPEN` | 1 | 2 | 2 |
| `P.txt ACCESS` | 0 | 1 | 1 |
| `P.txt CLOSE_NOWRITE` | 0 | 1 | 1 |
| `P.d OPEN,ISDIR` | 1 | 2 | 2 |
| `P.d ACCESS,ISDIR` | 1 | 3 | 3 |
| `P.d CLOSE_NOWRITE,ISDIR` | 1 | 2 | 2 |
| `P.d/ OPEN,ISDIR` | 0 | 1 | 1 |
| `P.d/ ACCESS,ISDIR` | 0 | 2 | 2 |
| `P.d/ CLOSE_NOWRITE,ISDIR` | 0 | 1 | 1 |
| `P.d/f OPEN` | 1 | 2 | 2 |
| `P.d/f ACCESS` | 0 | 1 | 1 |
| `P.d/f CLOSE_NOWRITE` | 0 | 1 | 1 |

The table is from a run of 2026-11-22 after the fifth review's changes and
D-037; the sixth review's run the same day gave the same counts except
`P.txt OPEN` 3 with the watcher on (the writer's two opens arriving
unmerged), the watcher's own share unchanged. Neither run had a Docker
Desktop walk in any of the three windows.

The watcher-off column is the writer's own opens and the subscriber's own
listing of the new directory. inotify merges identical consecutive
notifications in a subscriber's queue, so the writer's two opens of `P.txt`
can arrive as one; the counts are what the subscriber received. The mark
added no notification. (In the runs before D-037 it made the watcher take
the same reads earlier; since D-037 it waits for the batch to close.)

Not caused by the watcher, but visible the same way: on Docker Desktop,
starting a container or exec'ing into one walks every directory of the
volume (the image had a health check that exec'ed every 5 s until
2026-11-22; it has none now, and the check's own helper containers still
cause walks), which the subscriber sees as
`OPEN,ISDIR`, `ACCESS,ISDIR`, `CLOSE_NOWRITE,ISDIR` on each directory, and
which updates directory atimes. Not measured on a Linux host with plain
dockerd.

Everything else the check found:

- Its process list holds only its own processes.
- Its mount table is identical, line for line, before and after the watcher
  started. The volume line was
  `228 220 254:1 /docker/volumes/wdev-7fa811/_data /work/project rw,relatime master:26 - ext4 /dev/vda1 rw,discard`;
  it names the volume, so the volume name must be neutral.
- No watcher file anywhere it can read, nothing in the volume but its own
  files, and no inotify descriptor but its own subscriber's.
- `getent hosts <watcher-name>` from a container on its network fails.
- Access times: the watcher's reads left file atimes unchanged; its listing
  of a new directory left that directory's atime unchanged in all three
  windows (the check accepts a change only in a window that also had a
  Docker Desktop walk; there was none). A plain
  `cat` through a read-only mount also left atime unchanged, while a `cat`
  through a read-write mount changed it.
- In principle, not tested: inotify limits are counted per uid across
  containers in one user namespace, so the watcher uses one of uid 1000's
  8192 instances and one of its 1048576 watches per directory.

The metric isolation, from the same run: every one of the run's readings
read `data` and the absolute link `shortcut` (pointing at
`/work/project/data`) as the bytes of the batch it named, including a
reading of an old batch that ended after the live file had changed; every
attempt of the command to list or read `/srv/w`, `/srv/w/live` or
`/srv/w/out/events.jsonl` failed with `PermissionError`; it ran as uid 1500
in `/tmp/m/project`, the target of `/work/project`.

## Limits

The watcher learns of writes by reading the inotify queue, so which batch a
write lands in, and how tight a batch's time bounds are, depend on when
reads, scans and writes fall within a fraction of a second. The items in
the first list follow from that. The bounds it records are true; their
tightness, and batch membership at that grain, are not stable. The bundle
and the scorers must not depend on any of them.

- Timing by read: all events of one queue read share one upper bound, the
  time that read returned, and are known only to have happened after the
  previous read began. Two writes closer together than the watcher's read
  interval can share a batch, and a mark between them reports that batch
  as `straddled`. When the watcher is idle the read follows a write by 0.2
  to 1.6 ms (two-container check runs of 2026-11-22); during a scan the
  interval is the scan time (`scan_ms`).
- A write that lands while a due batch is being scanned joins that batch:
  the validation reads the queue and takes the new hints in, because the
  scan must describe one tree. So a write a fraction of a second after a
  batch became due can count with it instead of starting the next. In the
  two-container check a 200-file batch scanned in 205 to 358 ms, and once,
  on 2026-11-22, in 2,578 ms, which took in the writes of the next four
  steps.
- A directory move whose two notifications arrive in different reads, or
  whose destination's parent is not yet watched (a directory created
  moments before), drops the writers under it at once. A file held open
  across such a move can therefore count one change more. The move itself
  is still recorded as a move when the inode appears at one new path.
- A writable close from a second handle on a path ends the hold of a first
  handle still open on the same path: holds are kept per path, not per
  handle.
- The lower bound of a batch's first write, `first_write_after`, is the
  start of the previous queue read, and any event, an ignored one
  included, causes a read. So the bound is tighter in a run with more
  ignored activity (an instruction file being rewritten, say). Both bounds
  are true in every run; only their tightness differs.
- The two-container check takes its archived mark after the writer has
  finished: it shows a mark on a settled tree (covered at the next regular
  read, naming the last batch, nothing straddled), not a mark taken during
  writes. Marks during writes are tested only on the queue backend under a
  controlled clock (`test_round5_host.py`, `test_round7_host.py`).

Other limits:

- Changes inside one batch merge, and the order inside a batch is lost.
- A change that raises no notification (a write through a shared `mmap`,
  a write through a hard link in an ignored directory) is found only by the
  next batch's lstat sweep (once the kernel updates mtime) or by the final
  scan at stop. A mark never finds it: until another batch, a mark names
  the tree without it.
- After a queue overflow the batch is a full rescan: moves come out as a
  deletion and a creation.
- A rename cycle inside one batch (a swap through a temp name) has no legal
  order and is written as a resync.
- An inode number reused within one batch after a rename-out could be taken
  for the renamed file.
- Validation compares lstat signatures and the inotify queue; a write in the
  same coarse clock tick as the read, that also raises no event, would not
  be seen. Every ordinary write raises one.
- A `.git` file (a worktree) is not recorded.
- Directories the watcher cannot watch (`ENOSPC`, `EACCES`) are listed in
  `unwatched`; changes in them are found only by the lstat sweep of paths
  already known.
- Raw records carry physical device and inode numbers and depend on
  scheduling; scoring should use normalised tree digests and named
  readings.
- The metric queue holds one manifest snapshot per batch waiting for a
  reading; a command slower than the commit rate makes it grow without
  bound (memory not measured). What the shutdown drain does not reach is
  `backlog`, for `complete-metrics`.
- Accepted by decision D-032, with no further bookkeeping: the `epoch` of an
  unreadable file is best effort (see above; only `content_unknown` is
  guaranteed). A write through a hard link whose other name lies inside an
  ignored directory raises no event the watcher hears; it is recorded by
  the next batch's lstat sweep or by the final scan at stop, not by a mark.
  Tasks contain neither unreadable files nor hard links.

## Tests

On any host, both interpreters (`uv run --isolated pytest tests/watcher`
and the same with `--python 3.12`): ignore rules, the scheduler, the delta
planner and the strict replay rules on hand-made manifests (every sequence
quoted in the first review), replay and diff on hand-written logs (blob
verification, renames only by recorded moves, linked symlinks), the metric
runner (strict JSON, copies at a workspace path, dangling outside links,
omissions on every reading, an unwritable reading, cleanup that leaves an
outside directory's mode alone), and the core against real
files with hand-given hints, including the second review's findings: a
write during a scan (B2) and a tree that never holds still, the sweep
handing no slashed path to stat and a middle component swapped for a
symlink, the git reader refusing a refs directory swapped between discovery
and open (B3), write hints by identity and unknown content (B5), root
permission loss and recovery (B6), late overflow and a fatal root event
during shutdown (B8), one read per inode, and the D-026 ignores. From the
third review: the metric copy through both executors, the isolated one with
its server in a thread (`test_metric_copies.py`: a directory's mode change,
a renamed directory, type changes, an unchanged file under a changed
directory, ignored names next to tracked files, batches read out of order,
the old in-band marker as content, a corrupt blob failing the reading), and
`test_round3_host.py`: a same-length overwrite in one clock tick during a
scan (timestamps held still), a root recovery arriving during validation,
the outstanding-work invariant, write hints per file lifetime and
unknown-content versions per inode, a grep of every output file for the
ignored names and refs after writing, renaming and deleting the
instruction file, the same batches with and without ignored activity under
a controlled clock. From the fourth review, `test_round4_host.py`: the reserved names and their
temporary forms ignored while `docs/AGENTS.md.example` and `notCLAUDE.md`
are recorded, and the forms validated; write times not moved by a close one
second later and null for a sweep-only batch; a deleted open file that no
longer holds the batch and an open writer below a renamed or removed
directory; a rejected mark (`accepted: false` in the record and the
acknowledgement, skipped by `marks` and `mark_batch`); a `refs/t3/` update
in `packed-refs`, and a repack through `packed-refs.lock` that replaces it
with a new inode, leaving the clock alone and writing no record, while a
tracked ref change moves it; ignored
files created under a tracked directory during validation leaving the batch
settled in one round; the same two batches, with the same write times, with
and without ignored `packed-refs` updates and ignored files under the
tracked directory, under a controlled clock; `content_unknown` on every
record of a written unreadable file and no `rewritten` for it; an event log
write failure cut back with no sequence gap, a log that cannot be cut back
refusing later writes, and the watcher stopping with a `failed` heartbeat;
`diff` refusing a blob whose hash is wrong; a copy that fails verification
leaving its batch unmeasured. `test_metric_copies.py` adds a link through a
symlinked directory followed by `..`, in both executors, and the link
classification cases.

From the fifth review and D-036/D-037, `test_round5_host.py`,
`test_metrics.py` and `test_metric_copies.py`:

- B2a: a directory moved under an unwatched ignored directory while a
  writer inside it is open, then a write on its stale watch and a task
  edit: the edit is due after the quiet period (1.14 s), not the hold limit;
  a paired move keeps its writer; a removed watch drops the writers below
  it; a move pair split across two reads is still a move and the
  destination is watched again.
- M1: `l -> <workspace>/./a` and `x -> l/b`, opened through the copy in
  both executors (both read `a/b`), and more resolver cases.
- M2: a reading whose copy lacks an oversize file is `complete: false` with
  that path in `lacking`; an inaccessible directory does not count.
- M3: five batches queued while the first reading runs are all measured, in
  order, each from its own manifest; a tree that differs only in owner and
  inode reuses the reading (`reused_from`); a drain of 0 s leaves the queued
  batches as `backlog` records; batches must be queued in order; a reading
  is found by batch; a mark is answered at once while a 30 s metric run is
  in progress, with `metric: null`.
- B7a: after a failed batch write, with no new hint, the watcher is due and
  its next batch is a full rescan (`reason: "recovery"`) that records the
  change.
- `diff` lineage: a file replaced in place by another inode, which then
  moves, is a removal and an addition, not a rename; the same move without
  the replacement is a rename.
- The unknown flag stays after the file becomes readable, and a later
  identical write is `modified`, not `rewritten`.
- The isolation preflight accepts the documented layout and refuses a
  private directory with another mode or owner, the two directories in
  different parents, a link, and one directory for both.
- D-037, rewritten in round 6 on a queue backend (events wait until the
  watcher reads them) under a controlled clock: two writes to one path at
  2.000 s and 2.080 s and a mark for 2.030 s arriving at 2.03, 2.05, 2.1,
  2.3, 5 or 9.9 s give the same single batch and the same mark record
  (batch 0, straddled 1); a mark during writes every 50 ms is answered at
  the hold limit (3.0 s) with `straddled` set; a mark on a quiet tree is
  answered at once and commits nothing, and a change no event reported is
  found by the next batch's sweep; a mark first covered after its limit is
  not accepted; a mark for an instant never reached is not accepted and
  names nothing; a `limit_s` at or below the hold limit is refused; writes
  120 ms apart read together by a late read share a batch and a mark
  between them says `straddled`; and a seeded property (1,000 seeds on
  2026-11-22) of random write bursts and writers that never pause, each
  write its own file, read at random intervals (2 to 80 ms), with 3 to 10
  marks at random instants arriving at random later times: the batches
  equal those of the same writes with no marks, every mark is accepted,
  every write in the named batch or earlier happened at or before the
  instant, every write at or before it is in the named batch or earlier or
  in the straddled batch, and a batch with writes on both sides of the
  instant is the straddled one. The real-inotify property test below has no
  marks, because two runs against the kernel do not give the same batches
  to compare.

From the sixth review, `test_round6_host.py`:

- B1: a writer carried over from the previous batch that expires at
  3.90 s while a closed edit is quiet at 3.95 s: the batch is due at
  3.95 s with no further event; a live writer still holds until its expiry,
  capped by the hold limit.
- V1: no ignored event, one, or two (`AGENTS.md`, `.CLAUDE.md.tmp.1`)
  between the halves of a directory move with an open writer inside leave
  the writer at its new path and the watch relocated; a tracked event
  between them keeps the pair.
- Timing by read: a batch's `first_write_after` is the start of the read
  before the one that delivered its first write, and `first_write` and
  `last_write` the starts of the delivering reads; a mark without `at`
  counts a write already queued when it was requested (reviewer 2 N1); a
  recovery batch says `write_times: "unknown"` and a sweep-only batch
  `"none"`.
- Replay refuses a `straddled` that is not a later committed batch.
- A file created and written while unreadable in one batch is
  `content_unknown` from its first record.
- Metrics: a link through a file keeps its remaining components and stays
  broken in the local copy; a metric server asking for a digest outside
  the submitted manifest is refused; the reuse key changes with the copy
  mtime and the command; a job stuck in its copy or protocol is given up at
  the shutdown deadline and recorded as `backlog`, its late reading never
  written; a copy mismatch on the first local run is a failed reading;
  offline completion continues the run numbers; `complete-metrics` refuses
  to run outside the image.
- A failed ACL inspection refuses the layout; an ignored ref namespace
  `refs/t3` skips `refs/t3/x` and not `refs/t30/y`.

From the seventh review, `test_round7_host.py`:

- W2: the reviewer's writes to one path at 1.000 s and 1.140 s with
  regular reads every 50 ms give the same batches with and without a mark
  at 1.010 s; and a seeded property (150 seeds by default, 1,000 on
  2026-11-22) of repeated writes to one path, random read intervals (2 to
  110 ms) and 3 to 12 marks at random instants, with and without `at`,
  arriving at random later times: the batches, write times included, are
  the same with and without the marks; every write lies inside its batch's
  recorded bounds; every mark is accepted and agrees with the bounds; and
  replay's own bounds check passes.
- W3: a write delivered during a read begun at 1.000 s and returning at
  1.001 s gets `last_write` 1.001 s, and a mark for 1.000 s says that batch
  straddles it; a read that stopped at its buffer cap does not confirm a
  mark's coverage.
- W4: a child created 80 ms after its directory's create was read, before
  the directory had a watch, puts the scan's end in `last_write`; a
  notified write keeps its read's bound.
- W7: a completion takes the recorded copy mtime (1234567890 here) over the
  executor's default and gives the same values as the live readings; it
  refuses without a readable config and with another workspace path; the
  reuse key includes the timeout and output limits.
- Replay refuses a mark that disagrees with the recorded bounds (a missed
  straddle, a named batch ending after the instant, a straddle outside the
  bounds); a symbolic ref and a HEAD pointing into `refs/t3/` leave no
  trace in any output file.

From the bounded check after round 7, `test_round8_host.py`: a write
queued through a capped read keeps the batch's lower bound at the last read
that emptied the queue, and a mark for an instant before the delivering
read counts it as straddling; validation that hits the cap reads on and
records a write whose notification the capped read missed; a change found
by validation through an ignored hard-link name ends at the scan's end; a
change made between a scan's validation and its commit, swept up by the
next batch, is bounded below by the previous scan's start; overlapping
bounds give a straddled run (`straddled` 1, `straddled_last` 2 for the
checker's case), and replay accepts it; the offline completion refuses a
config whose isolation differs; `7` and `7.0` give the same reuse key; a
symbolic ref and HEAD pointing at an ignored name that also has a packed
ref leave no trace. And a seeded property (150 seeds by default, 1,000 on
2026-11-22) with reads at random intervals that sometimes stop at a random
cap, silent writes (no notification) to files that exist from the start,
notified writes each to its own file, and 3 to 12 marks: the batches are
the same with and without marks; every write, silent ones included, lies
inside its batch's recorded bounds; every mark's answer is
`replay.answer_for` over the batches committed before it; against the true
times, no notified write in the named batch or earlier is after the
instant, every notified write at or before it is in the named batch or
earlier or in the straddled run, and none after the run is before it; and
replay's check passes.

With real inotify and openat2, inside a Linux container, as uid 1000,
repository mounted read-only, no network:

```
tests/watcher/run_linux.sh                       # everything
tests/watcher/run_linux.sh tests/watcher/test_regressions.py
WATCHER_PROPERTY_SEEDS=50 WATCHER_PROPERTY_SEED_START=100 \
  tests/watcher/run_linux.sh tests/watcher/test_property.py
```

`test_linux_inotify.py` covers ordinary changes, moves, editor patterns,
1,000 files, a 6 MiB file, permissions, symlinks, hard links, coarse git,
metric readings matched against `state_at` of their batch, every batch
measured in order behind a slow command, a backlog left at shutdown and
completed by the offline completion with the live run's executor and the
same values, marks answered after the batch's own quiet period (the
batch's reason stays `settled`),
overflow, the heartbeat, a failed heartbeat after an injected exception,
SIGTERM, a write racing shutdown, and a write through a hard link inside an
ignored directory that a mark does not find and the next batch's sweep
does. `test_regressions.py` has one named
test per sequence from both reviews, among them the B1 sequence (a directory
returning through an unwatched parent), root and nested permission loss and
recovery, a move chain that leaves and re-enters the workspace, the D-026
ignores on real inotify and real git (with a grep of every output file), a
scan racing a writer, and same-length overwrites from a thread while marks
are taken, the last of them settled and equal to the disk. Every
test that stops a watcher normally checks that `state_at(last)` equals an
independent walk and that every batch passed its self-check.

The property tests (`test_property.py`), on 2026-11-22 after the seventh
review's changes, 200 fresh seeds per mode (13000 to 13199), 25 bursts per
seed. Every tree is a git repository. The operations include a directory
returning through an unwatched parent, permission loss on the root and on
nested directories that often stays across batch boundaries, files held
open for writing across boundaries, moves that leave the workspace and come
back, and git commits, branch updates, `refs/t3/` updates, `pack-refs` and
detaching and reattaching HEAD. The oracle compares type, mode, owner,
device, inode number, size, content hash, access status and link groups
from a walk that leaves out `.git`, plus HEAD (a ref, or the commit when
detached) and every ref outside `refs/t3/` as `git symbolic-ref`, `git
rev-parse` and `git for-each-ref` report them; every referenced blob is
verified. `epoch` is not compared: under D-032 it is best effort. In
interleaved mode the baseline must equal the starting tree, the
permissions are restored while the scan hooks are still in place, and the
shutdown batches are checked against their windows like the others.

- bursts: 5,000 bursts, 22,584 operations attempted of which 13,907 applied
  (the rest were impossible as chosen, many because the root was locked),
  2,939 of them git operations; 4,093 batches, all settled, 2 resyncs
  (rename cycles); after every burst the replay equalled the oracle.
- interleaved: the same operations, also run from inside content reads,
  directory listings and validation passes. 8,423 batches; the baseline and
  7,826 settled batches each equalled, in order, a state the writer
  produced during that batch's own commit; 596 were committed unsettled; 5
  resyncs; at the end of every seed the replay equalled the oracle.

The real two-container check:

```
uv run --isolated python tests/watcher/two_container_check.py --build --out <dir>
```

It names everything it creates `wdev-*` and removes the containers, network
and volume at the end. On 2026-11-22, after the seventh review's changes,
on a machine with no other container of this project running (checked
every 2 s through each run), it ran twice. The first run passed 41 of 43:
the scan of the 200-file burst took 2,578 ms (205 to 358 ms in every other
run that day), so the writes of the next four steps landed while that due
batch was being scanned and joined it (the limit on writes during a scan),
and the two checks that expect each step's changes in its own batch
failed. The second run passed all 43, among them: the image has no health
check and `alive` on the host reads the heartbeat as running; every
committed batch has a measured reading, in batch order; the readings are
`complete` until the oversize `big.bin` appears and lack exactly that file
from then on; the host's mark (taken after the writer finished) is
acknowledged with `at`, `straddled` and `covered_utc`, and its batch has an
ok reading in metrics.jsonl; the entry point's layout check passed in the
image; `complete-metrics` refused to run on the host, and in the image
remeasured three removed readings with the same values, isolated, run
numbers continuing; no Docker Desktop walk in any atime window and every
directory atime kept. For single small writes, the delay from the
writer's timestamp before the write to the record was 102.6 ms minimum,
105.8 ms median and 118.2 ms maximum, of which `quiet_ms` is 100 ms; the
read that delivered the first inotify event of each write began 0.3 to
1.4 ms after it.

Untested: Linux hosts outside Docker Desktop; kernels without openat2 (the
fallback walk runs only in the host tests on macOS); reftable repositories;
`ENOSPC` on the watch limit; `mmap` writes; filenames that are not UTF-8;
very large trees, memory use and the cost of the sweep and validation on
them; a metric copy larger than its tmpfs; the isolated metric mode outside
the two-container check (the pytest suites run the isolated executor and
its server as the same user, in a thread, without the privilege drop); the
`--ignore-name-forms`, `--metric-drain-s` and `--metric-copy-mtime` flags
(the keys are tested through `Config`); the layout check's ACL refusal on
a real ACL (no test sets one; a failed inspection is tested with a stub);
`complete-metrics` with a real backlog in the image (the check removes
three readings from a copy of a finished log); marks with `at` against the
real watcher from the command line (the CLI test and the two-container
check send none); marks against real inotify timing (the mark property
test runs on a queue backend under a controlled clock); the shutdown
deadline giving up a job in the isolated executor (tested with the local
one); the metric queue's memory with a long backlog.
