# Changelog

All notable changes to mcm-engine. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning.

## [Unreleased]

## [3.5.0] — 2026-07-05

The "rule hierarchy" release: rules stop being a flat pile — they gain
importance/scope/kind, those axes drive behavior, and a co-located admin UI lets
you tune them. Plus safer ingestion and fail-closed store integrity.

### Added
- **Rule hierarchy.** Every rule gains `importance` (ordinal 0–2), `scope`
  (`universal`/`conditional`), and `kind` (`directive`/`fact`) — schema v11,
  orthogonal to the correctness/lifecycle axis. `set_rule_metadata` is the audited
  write (validates, stamps `updated_by`, emits a `metadata` event); `list_rules`
  returns every column importance-first. Both mirrored as MCP verbs.
- **The hierarchy drives behavior.** The invariant tier (importance 2) is injected
  into every `session_start`; `importance`/`scope` are weighted into search ranking
  (below relevance, so a strong match still wins); and `find_conflicting_rules` uses
  importance as the tiebreak, naming the higher tier the keeper.
- **Admin tuning UI** (`mcm-engine admin`). A small co-located web app — no external
  dependencies (stdlib server, self-contained pages): an editable rules grid with
  realtime colorize as the KB changes, and a node-graph structure view (rules colored
  by importance, clustered by category, edges from relations). Reads go direct; writes
  go through `set_rule_metadata`. An `mcm-admin` service is wired into the example
  Compose stack (trusted-LAN only).
- **Automatic rules-ingestion funnel.** A model-free mechanical funnel (`rulesift`:
  span extraction → rule-shaped gate → MinHash novelty banding → intra-run dedup) plus
  provider-agnostic adjudication (harness-delegation or a standalone cheap model), so
  `ingest --rules`/`--auto` surface or commit only net-new, rule-shaped candidates.
- **Full-coverage ingestion.** `text-dir` detects text by content-sniff (retires the
  strict extension allowlist), and one `ingest` run unions **all** matching ingesters
  (`find_all`) instead of just the first, so a polyglot repo no longer silently drops
  its Markdown or other-language files.
- **Example Compose stack** (`examples/docker-compose.yml`, Postgres + daemon,
  database-authoritative) with `examples/.env.example`, and a **Helm chart**
  (`deploy/helm/mcm-engine/`) — bundled Postgres StatefulSet by default or an external
  DB via `postgresql.enabled=false`, ClusterIP + optional Ingress, `/healthz`+`/readyz`.

### Changed
- **Migration-framework parity.** Postgres now version-stamps `_mcm_versions` (in
  `ensure_schema`, after its idempotent guarded DDL) the way SQLite's `migrate_core`
  does, so both backends report a legible `CORE_VERSION` (now 11).
- **Fail-closed store integrity.** A `StorageIdentity` plus an `authoritative_store`
  binding (`build_verified_context`, the composition-root choke point) makes every
  entrypoint refuse to run against a store other than the pinned one — closing the
  two-DB "stray database" class of bug.
- **Idempotent ingest + blast-radius guard.** Net-new detection dedups on a content
  hash at the write path (`ingest → commit → ingest` is a no-op), and rule archival
  (`sync_rules` + the watcher) refuses to storm-delete a large fraction of the corpus
  without `force`.
- **`link_knowledge`** relation types are a validated enum with docstring guidance;
  CI bumped to Node 24 actions.

### Fixed
- **Hooks no longer read a shadow database.** `SessionStart` injects a directive to
  call the MCP `session_start` tool instead of reading a local DB; ambient recall is
  transport-adaptive (HTTP → MCP-over-HTTP, stdio → the local authoritative store).
- **`link_knowledge` deadlock** from nudge escalation blocking every tool (including
  the reads needed to recover) — the periodic nudge advises but never hard-blocks.
- **Admin UI defects** — the sticky header no longer overlaps rows (a wrapper's
  `overflow-x` had nested a scroll context), and the 2-second poll no longer clobbers
  an in-progress edit (keyed reconciliation that skips the focused control).

## [3.0.0] — 2026-07-03

The "truth and hygiene" release: memory that knows what's correct, what's stale,
and what conflicts — and that agents can't dead-lock.

### Added
- **Correctness axis** — `report_outcome(rule_ids, passed)` records whether acting on a
  rule actually worked, as a signal separate from popularity. An **author≠judge** guard
  makes a rule author's report on their own rule advisory-only (no self-certification).
- **Graded trust map** — optional `actor→weight` map (`MCM_TRUST_WEIGHTS`,
  `MCM_TRUST_DEFAULT`) weights outcomes by reporter, applied at rank time (late-binding).
- **Supersession** — `supersede_rule(old, new)` soft-expires a rule (`valid_until`,
  `superseded_by`, `status`); superseded rules leave default search but stay for audit.
  Never hard-deletes.
- **KB hygiene tools** (deterministic, read-only, surfacing-only):
  `find_duplicate_rules` (MinHash/LSH), `find_conflicting_rules` (topic-similar/
  body-divergent, typed `contradictory`/`subsumes`/`subsumed`), and `consolidation_report`
  + the `consolidate` CLI (merge/conflict/stale candidates in one report).
- **DB→git review mirror** — `export-mirror` CLI renders active rules to a one-way git
  repo for diffable review.
- **Opt-in ambient recall** (`MCM_AMBIENT_RECALL`) — the PreToolUse hook surfaces a
  relevant rule from what you're editing (best-effort, tight timeout, rate-limited,
  never blocks) — plus one-hop **spreading activation** over linked rules in search.
- **Poisoning defense** — retrieved rule content is delimited as untrusted *data* at read
  time; `add_rule` flags injection markers ("ignore previous instructions", …) without
  rejecting.
- **Token ledger** — estimates tokens saved by recall vs. spent on stores; net shown in
  `session_start`.
- **Source-of-authority axis** — `source_of_truth: files | database` for files-win vs.
  DB-authoritative (fleet/pod) deployments.

### Changed
- **Ranking reformulated** — `compose_rank` is now an additive-hybrid weighted sum of
  normalized signals (relevance, hit, reinforcement, correctness, recency), with relevance
  weighted above the others so counters can't swamp a strong match.
- **Scale-free relevance** — relevance is batch-min-max normalized in the search layer, so
  ranking behaves identically on SQLite bm25 and Postgres `ts_rank_cd` (a fixed-scale
  sigmoid mis-ranked Postgres).
- **Schema** — `CORE_VERSION` 8 → 10: `rule_outcomes` table + correctness/supersession
  columns on `rules` (v9), `token_ledger` table (v10). SQLite and Postgres both migrated;
  migrations are idempotent and run on startup.

### Fixed
- **PreToolUse hook is now fail-open.** It records a `consultation_gap` event and always
  allows the edit (exit 0) instead of blocking — fixing a catch-22 where a blocked agent
  could not call the reset tool because the backend it needed was the thing that was down.
- **FastMCP DNS-rebinding `421` over LAN/Docker.** FastMCP auto-enabled a localhost-only
  host allow-list even when bound to `0.0.0.0`; the daemon now derives the allow-list from
  the real bind host, with `--allowed-host` / `MCM_ALLOWED_HOSTS` escapes.
- **`source_of_truth` default trap** documented — a `database`-intended pod that forgets
  `MCM_SOURCE_OF_TRUTH=database` risks archiving its DB-only rules.

## [2.0.0]

- **Pluggable multi-backend** across four axes (`storage` / `counters` / `search` /
  `session`) — SQLite / Postgres / Redis / OpenSearch, selectable per axis via config.
- **Provenance** — append-only `rule_events` with actor attribution; full rule body stored
  in the DB.
- **Daemon mode** (`serve`) over HTTP/SSE with `/healthz` + `/readyz`, and the files-win
  **watcher cascade**.
- **Backend migration** (`migrate`), a **container image**, and an adapter **conformance
  suite** lifted as a reusable library.

## [1.0.0]

- Initial release: a single-file SQLite knowledge/rules/errors/negatives store with
  session handoff and behavioral nudges, served to coding agents over MCP (stdio).
