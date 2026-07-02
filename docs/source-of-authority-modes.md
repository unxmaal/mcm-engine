# Source-of-authority modes

mcm-engine runs one codebase across two deployments with opposite notions of
truth. Issue #16 makes that difference a single explicit axis rather than a
scatter of file-vs-DB assumptions.

## The axis

`config.source_of_truth` (YAML) / `MCM_SOURCE_OF_TRUTH` (env), default `files`.
The env value overrides YAML; a malformed value falls back to `files` with a
warning (the historical always-files direction is the fail-safe). Unknown
config *keys* still fail closed — this is value-level fail-safe for a known key.

| | `files` (World A, default) | `database` (World B) |
|---|---|---|
| Authority | markdown under `rules_path` | the DB |
| Typical deploy | local stdio, ephemeral per session | always-on pod, Postgres |
| Startup file→DB sync (`watcher.sync_once`) | runs | **skipped** |
| Background file observer | runs (daemon mode) | **not started** |
| `add_rule` with no `file_path` | writes a `.md` file, then the row | DB row only, no file |
| `read_rule` | disk first, DB fallback | DB first, disk fallback |
| Rules loaded via `import_rules` | (unusual) | the normal load path |

`config.files_are_authoritative` is the boolean the code reads. The gate lives
inside `MCMServer.start_watcher()` and `MCMServer.run()` (both hold the config),
so `transport.py` calls them unconditionally and stays mode-agnostic.

## Defense in depth against the archive-storm

The motivating incident: an always-on pod's watcher walked an empty rules dir,
declared every DB rule an orphan, and archived 177 of them. Three independent
barriers now prevent that, each sufficient on its own:

1. **Mode gate (Layer 1).** In `database` mode the file→DB sync never runs.
2. **Provenance scope (Layer 2).** Even in `files` mode, the orphan sweep only
   archives rules whose `file_path` is under the watched `rules_path`
   (`RulesWatcher._is_managed_path`). A provenance path or a DB-native import
   (`file_path` elsewhere or `None`) is never the watcher's to reap.
3. **Circuit breaker (Layer 3).** A sweep that would archive more than
   `archive_circuit_fraction` (0.5) of managed rules once the count exceeds
   `archive_circuit_floor` (5) refuses and logs loudly instead. This also
   protects `files` mode against a transiently-empty dir (failed mount,
   mid-checkout). `sync_once` reports `archive_blocked` in its counts.

Recovery: `restore_rule(rule_ids=[...])` or `restore_rule(all_archived=True)`
un-archives soft-deleted rules and emits a `restored` event each. Archived rows
are invisible to search but never destroyed, so recovery is always possible.

## North star (not built)

The append-only `rule_events` log (issue #10) is the seed of a cleaner model
where the log is the source of truth and both files and the DB are materialized
projections of it — dissolving the files-vs-DB dichotomy entirely rather than
switching its polarity. The mode axis above is the pragmatic step; event
sourcing is the end-state if the coupling ever needs to go away for good.
