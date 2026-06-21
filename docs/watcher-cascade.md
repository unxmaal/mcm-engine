# Files-win watcher cascade

The watcher is the mechanism that makes "files are authoritative, the DB is
a cache" a real guarantee instead of an aspiration. It watches `rules/**/*.md`
on disk and cascades changes into whatever `StorageBackend` is currently
loaded. This document specifies its behavior — particularly the conflict
resolution rules, which are subtle enough that they would be impossible to
reconstruct from the code six months from now.

Implemented in `mcm_engine/files/watcher.py`. See MCM2-23.

## What the watcher watches

- Every `.md` file under each configured `rules_path` (typically `rules/`
  in the project root).
- Subdirectories nested arbitrarily — recursive.
- The watcher does **not** watch knowledge files, because knowledge does not
  currently have a file representation. If that changes, this spec must change
  with it.

Implementation note: the watcher uses [`watchdog`](https://pypi.org/project/watchdog/),
which is a pure-Python cross-platform wrapper around fsevents (macOS), inotify
(Linux), and ReadDirectoryChangesW (Windows). It is a core dependency, not an
adapter dependency — `watchdog` does not violate NG-8 because it talks to the
operating system, not to an adapter-specific external service.

## Modes: daemon vs stdio

The watcher's behavior depends on which MCP transport is in use.

**Daemon mode** (`mcm-engine serve`, HTTP/SSE long-lived process):
- Watcher runs as a background thread for the lifetime of the process.
- File changes propagate to the DB within the debounce window (sub-second
  in practice).
- This is the mode where "files win" is a live guarantee.

**Stdio mode** (`mcm-engine`, today's Claude-Code-spawns-engine flow):
- The watcher is **not started**. Process lifetime is too short for a
  long-running watcher to be useful.
- Instead, the engine runs `sync_rules` once at startup to bring the DB
  current as of process start.
- "Files win" in stdio mode is therefore a *startup-time* guarantee, not a
  live one. If a user edits a rule file while a stdio session is running,
  that change is not seen until the next session.
- This is the correct trade-off — stdio sessions are usually short and
  the user is the one driving them; live file watching would be wasted
  work.

## Conflict resolution: the "files win" rule

When the engine and a human are both modifying the same rule, who wins?

**The file wins.** Always.

This is not a default that can be configured. It is a structural property
of the cascade: the watcher is the single writer that mirrors disk into
the DB. The engine writes to disk and the watcher (or startup sync) mirrors
that write into the DB. There is no path where the DB diverges from disk
and stays diverged.

### Engine-initiated writes (the "no-op cascade" path)

When the engine itself writes a rule (`add_rule`, `promote_to_rule`, etc.):

1. The engine writes the **file first**. (`rules/<category>/<slug>.md`.)
2. The engine then writes the **row** into the loaded `StorageBackend`.
3. Both writes record the same content hash on the row.
4. Moments later, the watcher fires for the file write the engine just
   completed.
5. The watcher reparses the file, computes its content hash, and compares
   against the row's hash. **They match. The cascade is a no-op.**

The content-hash check is what prevents engine-initiated writes from causing
a wasteful re-parse and re-write through the StorageBackend. Without it,
every `add_rule` call would cost two writes and a redundant parse.

### External edits (the cascade path)

When a human opens `rules/methods/foo.md` in an editor and saves:

1. The editor writes the file. Most editors write multiple times during
   save (atomic-rename, temp-file dance) — see "Debouncing" below.
2. The watcher fires.
3. The watcher reparses the file, computes its content hash, compares
   against the row's hash. **They differ.** Proceed.
4. The watcher calls `StorageBackend.upsert_rule(...)` with the parsed
   contents.
5. Next search reflects the new content.

### Race condition: engine + human writing simultaneously

If the engine is calling `add_rule` while the user is editing the same
file in vim, the last writer wins on disk, and the watcher will sync
whatever ends up there. This is not a guarantee that the engine's
intended write survives — the human's editor save can land after the
engine's write and overwrite it.

This is acceptable behavior. The human is the authoritative source; if
they overwrote the engine's add_rule, that was their choice. The engine
does not try to "win" against the human.

If this becomes a real problem in practice (it shouldn't, since add_rule
calls are infrequent and the human is unlikely to be saving the same
file at the same moment), we can add a brief file lock. Until then,
last-writer-wins is fine.

## Deletion semantics

When a rule file is deleted from disk:

- The watcher fires a `deleted` event.
- The corresponding row is **soft-deleted** (an `archived_at` timestamp is
  set, `archived = TRUE`), not hard-deleted.
- Search continues to exclude archived rows from default scope.
- A row remains queryable via explicit "show archived" — useful for
  diagnostics, never via the normal LLM-facing surface.
- If a file with the same slug is **re-created** later, the soft-delete is
  reversed (`archived_at` cleared, content re-synced). This handles the
  common case of "I deleted that by mistake, let me restore it from git."

Hard deletion of rule rows is not exposed as a tool. The only path to a
truly-gone row is direct DB manipulation, which is out of scope for the
engine's contract.

## Rename semantics

A rule file moved from `rules/old/foo.md` to `rules/new/foo.md`:

- Watchdog reports this as a `moved` event with both paths.
- The watcher treats this as **delete-old + create-new** rather than
  trying to update the row's path in place. Rationale: the slug
  (`<category>/<filename>`) is the row's identity. Changing the category
  or filename is a row identity change; safer to archive the old and
  create the new than to mutate identity.
- Net effect: the old row is soft-deleted, a new row is created with the
  new path. Content hash transfers correctly.

If a third party builds an adapter that wants different rename semantics
(e.g., update in place to preserve hit counts), they can — but the
in-engine watcher uses delete-old + create-new.

## Backend failures during cascade

The cascade depends on the loaded `StorageBackend` being available. If
Postgres is down when a file event fires:

- The watcher catches the connection error and **buffers the event** in an
  in-memory FIFO queue (bounded — see "Bounded buffer" below).
- The watcher retries the cascade with exponential backoff (1s, 2s, 4s,
  8s, 16s, capped at 60s).
- On success, the buffered queue drains in order.
- If the backend stays down past a configurable timeout (default: 10
  minutes), the watcher logs a loud error and **continues processing new
  events**, dropping the oldest buffered ones. The engine does not crash;
  the in-memory state of the buffered queue is lost; the next successful
  `sync_rules` (manual or at next restart) reconciles disk against DB.

**Bounded buffer:** the queue is capped at 1000 events. If the cap is hit
during a backend outage, the oldest events are dropped (with a log entry).
A drop is acceptable because the next `sync_rules` will pick up whatever
state the file ended in — the watcher's job is delivering "what changed,"
but `sync_rules` is always the backstop for "what is."

## Debouncing

Editors often write files multiple times during a single save: atomic-write
to `.foo.md.swp`, fsync, rename to `foo.md`, sometimes truncate+rewrite.
Each of these can fire a separate watchdog event.

The debouncer coalesces events per-path within a 500ms window. Behavior:

- First event for a path starts a 500ms timer.
- Subsequent events for the same path within that window reset the timer.
- When the timer fires without further events, the cascade runs once with
  the *current* on-disk content.

500ms is fast enough to feel live to a human (typically <1s from save to
search-reflects-change) and slow enough to absorb editor write churn.

## Startup behavior

When the daemon starts (or restarts):

1. Read configured `rules_path` directories.
2. Run a one-shot `sync_rules` pass: for each `.md` file on disk, ensure a
   row exists with matching content. For each row in the DB without a
   corresponding file, soft-delete it.
3. **Then** start the watcher thread.
4. The watcher's first cascade for any file is the result of comparing
   on-disk content to whatever `sync_rules` just wrote.

Order matters: starting the watcher first would race with `sync_rules` —
the watcher might see fresh events for files that `sync_rules` is about to
read. Doing `sync_rules` first, then watcher, gives a clean baseline.

In stdio mode: only step 2 runs. There is no daemon-thread watcher to
start.

## What the watcher does **not** do

- It does **not** watch knowledge files (none exist on disk).
- It does **not** call into adapter-specific code. It only calls
  `StorageBackend` methods — same as any other engine component.
- It does **not** try to merge concurrent edits. Last writer wins.
- It does **not** attempt to detect *content equivalence* across whitespace
  or formatting differences. Two files are "the same" iff their content
  hashes match.
- It does **not** sync the other direction — DB writes never produce file
  writes through the watcher path. The engine's tools (`add_rule`,
  `promote_to_rule`) write files directly; the watcher mirrors disk into
  DB, not DB into disk.

## Testing

The watcher conformance lives in `tests/test_watcher.py`. Key scenarios:

- **`test_external_edit_cascades_within_debounce`**: write to a rule file
  via raw filesystem; assert that the StorageBackend sees the change
  within 1 second.
- **`test_engine_write_does_not_double_cascade`**: call `add_rule`; assert
  that the StorageBackend sees exactly one upsert, not two.
- **`test_deletion_soft_deletes_row`**: delete a rule file; assert
  `archived_at` is set, row still queryable with `include_archived=True`.
- **`test_recreate_after_delete_restores_row`**: delete then re-create the
  same slug; assert row is unarchived and content matches new file.
- **`test_rename_creates_new_row_archives_old`**: move file; assert old
  slug archived, new slug created.
- **`test_backend_down_buffers_then_drains`**: simulate backend
  unavailability; cause file events; bring backend back; assert events
  drain in order.
- **`test_stdio_mode_does_not_start_watcher`**: assert that
  `mcm-engine` (stdio entry point) does not spawn a watcher thread; only
  `mcm-engine serve` (daemon entry point) does.

Conformance is exercised against both the embedded SQLite reference and
the Postgres adapter, since the watcher cascades through whatever
`StorageBackend` is loaded.
