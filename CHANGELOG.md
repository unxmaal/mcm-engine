# Changelog

All notable changes to mcm-engine. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning.

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
