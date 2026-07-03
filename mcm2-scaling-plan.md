# plan.md — mcm-engine 2.0: scale by addition

## Goal (one sentence)

Refactor mcm-engine so the **same engine code** can run with **zero external
dependencies** (in-process, everything embedded) OR with any combination of
**pluggable adapters** for storage / counters / search / session-state — chosen
per-concern by the deployer, not prescribed by the engine — where the only
difference between any two deployments is configuration and which adapters are
loaded.

**The engine ships with embedded reference implementations and a small set of
first-party reference adapters (one per concern). Beyond that, every external
component is a plug-in — written and packaged like a Python library, discovered
by the engine via entry points or import path. Users who want SQLite +
Meilisearch + their existing Memcached, or Postgres-for-everything, or
DuckDB + Tantivy + in-memory counters, never modify the engine to do it.**

---

## Reality audit (what the code actually looks like today)

Before phases, here's what differs between the original draft and the repo
at `src/mcm_engine/`. Each phase below is rewritten against these facts.

- **SQL is NOT centralized.** `db.py` and `schema.py` hold connection setup and
  DDL, but `SELECT`/`INSERT`/`UPDATE` statements live in `tools/search.py`,
  `tools/knowledge.py`, `tools/rules.py`, `tools/relations.py`, `tools/session.py`.
  There is no repository layer. Phase 0 cannot be "extract-and-inject"; it
  must first **extract SQL out of tools into a repository layer**, then put
  the repository behind an interface. This is the dominant cost of Phase 0.
- **Counters live on the entry row.** `schema.py` defines `hit_count`,
  `last_hit_at`, `reinforcement_count`, `pinned` as columns on the `knowledge`
  and `rules` tables. The composite rank in `tools/search.py` is
  `bm25_rank - 0.1*hit_count - 0.3*reinforcement_count - 2.0*pinned - recency_bonus`,
  computed in one SQL query joining ranking against counter columns. Splitting
  counters off-row is not free — see Phase 2.
- **The "files win" two-layer model is partial.** `sync_rules` (in
  `tools/rules.py`) reads `rules/**/*.md` and upserts the DB. It is **manual**
  (a tool call), **one-way** (files → DB), and has **no watcher, no conflict
  detection, no cascade**. "Files win" today means: if you call `sync_rules`,
  files overwrite the DB. That's the entire mechanism.
- **MCP tools are 1:1 with backend functions.** Each `@mcp.tool()` function in
  `tools/*.py` opens a connection and runs SQL directly. There is no service
  layer between dispatch and DB. Composition root work touches every tool
  function's signature.
- **Session/tracker state is in-memory** (`trackers/*.py`). Nudge counters,
  session start/handoff state, mandatory-stop counters, escalation state — none
  of it is persisted today. This is a fourth seam the original plan omits.
- **Multi-hop graph traversal is not implemented.** `tools/relations.py`
  defines a join table with `VALID_TYPES = {knowledge, error, rule, negative}`
  and `VALID_RELATIONS = {fixes, causes, supersedes, contradicts, related}`,
  but `get_related` is single-hop only. Recursive CTE in Postgres is therefore
  a *new feature*, not a port.
- **`search` returns formatted text strings**, not structured data. Output is
  bracketed lines (`[KNOWLEDGE/FINDING][PINNED] topic: summary\n  Detail: ...`)
  for direct LLM consumption. There is no `SearchResult` dataclass. The
  SearchBackend contract has to choose: return structured rows that the caller
  formats, or return rendered text. This shapes how OpenSearch results are
  reconciled with embedded results.
- **No backend parametrization in tests.** `conftest.py` has one SQLite
  fixture. The 11 test files (~2,760 lines) are all SQLite-only.
- **`NudgeConfig` silent-drop bug is real** at `src/mcm_engine/config.py:130`:
  `NudgeConfig(**{k: v for k, v in nudge_raw.items() if k in NudgeConfig.__dataclass_fields__})`.
  Treat this as one instance of a class; audit `load_config` for siblings.
- **Plugins exist** (`MCMPlugin`, `tests/test_plugins.py`). They register
  additional tools and contribute search scopes. The new backend interfaces
  must compose with plugins, not replace them.
- **Schema is at v6.** Live mini installs have data. Any storage swap needs
  a documented data-migration path from SQLite v6 → Postgres.

---

## Environment topology (where work happens)

This refactor is built on a Mac mini with **no AWS access from here**. AWS-only
validation moves to a separate work system. The plan's "scale by addition"
discipline is what makes this split tractable: every interface and adapter is
exercised locally first; only the deployment shape needs the work system.

- **Mac mini, daily driver** — embedded mode. This is *the* primary use case.
  Not a fallback, not a dev convenience: the engine running here in
  no-backends-configured mode is what the user actually uses day to day. If
  this regresses, everything else is moot.
- **Mac mini, validation rig** — first-party reference adapters run against
  whatever local services they target (e.g. local Postgres / Redis /
  OpenSearch via Docker or OrbStack, see OQ-9). The StorageBackend interface
  is proven by booting both the embedded SQLite reference and the first-party
  Postgres adapter against a local Postgres container and running the
  conformance suite. AWS is not involved. This is where Phases 1–3 finish.
- **Work system** — the only place AWS is actually reached, and only for
  *AWS-coupled* adapters (e.g. an S3-backed durable store, RDS Postgres,
  ElastiCache Redis, OpenSearch Service). Most adapters won't be
  AWS-specific at all. The mini authors the code; the work system runs the
  AWS-touching validation in Phase 4b.

Implication: the entire engine — interfaces, embedded reference, first-party
reference adapters, conformance suite — is a finished, shippable product
**before any AWS account is touched**. The mini owns "does the code work."
The work system owns "does it deploy against AWS-managed services."

---

## Context for the agent (read before touching code)

mcm-engine today is a single-process SQLite/FTS5 application with sophisticated
composite ranking (bm25 + hit-count + reinforcement + pinned + recency), an
implemented typed-edge join table, a manual one-way rule-file sync, in-memory
session/tracker state, and a plugin system. The discipline below is what we are
trying to preserve while making each external concern injectable.

- **One interface per external concern. One embedded reference implementation
  per interface. An open registry of additional adapters.** Nothing above the
  interface ever knows which adapter is loaded.
- **Embedded is the default; any adapter is opt-in; absent config means
  embedded.** A blank/missing backend block selects the in-process
  implementation.
- **The composition root is the only place that resolves config to concrete
  classes.** It reads config strings, looks them up in the adapter registry
  (entry points or explicit import path), instantiates, injects. Everything
  downstream depends on interfaces, never concrete backends. The composition
  root MUST NOT import adapter modules directly — it only imports the registry.
- **The engine core never depends on adapter-specific libraries.** No
  `psycopg`, no `redis`, no `opensearch-py` in the core's `pyproject.toml`.
  Adapter dependencies live in adapter packages or optional extras
  (`pip install mcm-engine[postgres]`).
- **Interfaces guarantee result *shape*, not bit-identical behavior across
  adapters.** `search()` returns ranked entries with scores whether served by
  FTS5, `ts_rank_cd`, Meilisearch, or anything else; it does NOT promise
  identical ordering.
- **Conformance is the contract.** The engine ships a conformance test suite
  (see Phase 1). Any adapter that passes it is a valid implementation. Adapter
  authors run the suite against their adapter; failures are interface bugs to
  report, not adapter-specific quirks to paper over.

## Adapter contract and registry

The four interfaces below (`StorageBackend`, `CounterStore`, `SearchBackend`,
`SessionStore`) are the **public contract**. Anyone — first party or
third party — can implement one.

**Packaging**:
- Engine core: `mcm-engine` on PyPI. Depends on nothing adapter-specific.
- First-party reference adapters: ship as **optional extras** of `mcm-engine`,
  not separate distributions. `pip install mcm-engine[postgres]` pulls in the
  Postgres adapter's deps. The adapter code lives in `mcm_engine/adapters/postgres/`
  but is import-guarded so the core imports cleanly without the extras.
- Third-party adapters: ship as **sibling distributions** (`mcm-engine-myadapter`
  on PyPI or private index). They depend on `mcm-engine` (for the Protocol
  classes) and whatever else they need. They register themselves with the
  engine via Python entry points.

**Discovery**:
- An adapter declares an entry point in its `pyproject.toml`:
  ```toml
  [project.entry-points."mcm_engine.adapters.storage"]
  postgres = "mcm_engine.adapters.postgres:PostgresStorage"
  ```
- The four entry-point groups: `mcm_engine.adapters.storage`,
  `mcm_engine.adapters.counters`, `mcm_engine.adapters.search`,
  `mcm_engine.adapters.session`.
- Config refers to an adapter by its registered name:
  `storage: { adapter: "postgres", dsn: "..." }`. The composition root
  resolves the name via `importlib.metadata.entry_points()`.
- Escape hatch for unregistered/local adapters: `storage: { adapter:
  "module:Class", ... }` falls back to direct import. Useful for in-repo
  dev work before publishing.

**Conformance**:
- The engine ships `mcm_engine.testing.conformance` — a pytest module that
  takes an adapter factory and runs the full contract suite against it.
- Adapter authors run it in their package's CI. Passing = valid adapter.
- The conformance suite tests shape and presence, not implementation details
  (no "this exact SQL," no "FTS5 produced this rank").

**First-party reference adapters this plan delivers** (one each, to prove the
contract is real; not a comprehensive catalog):

| Concern        | Embedded reference | First-party adapter      |
|----------------|--------------------|--------------------------|
| `StorageBackend` | SQLite + FTS5    | Postgres (Phase 1)       |
| `CounterStore`   | In-process       | Redis (Phase 2)          |
| `SearchBackend`  | SQLite FTS5      | OpenSearch (Phase 3a)    |
| `SearchBackend`  | (same)           | Postgres `ts_rank_cd` (Phase 3b — likely the most-used) |
| `SessionStore`   | In-process       | None (interface only, per OQ-5) |

Everything else — Meilisearch, DuckDB, libSQL, Memcached, Elasticsearch,
Tantivy, Cloudflare D1, MariaDB, Neo4j, an LLM-as-search-rerank, whatever — is
a third-party adapter someone else writes against the contract. We don't
build, ship, or maintain it.

## Non-goals (do NOT do these)

- **NG-1**: Do not build LODESTONE features — no source connectors (GitLab/
  SharePoint/Confluence), no risk sieve, no quarantine, no ingestion pipeline.
- **NG-2**: Do not require any external service for local operation. If `init`
  on a clean machine produces something that needs an external service to
  start, the task has failed.
- **NG-3**: Do not demand identical ranking across backends. Result shape is the
  contract; relevance math may differ.
- **NG-4**: The **first-party reference** storage adapter stays
  relational/SQLite-shaped. We do not ship a Neo4j or wide-column reference
  adapter. (A third party is free to write one if their problem calls for it
  — the engine doesn't forbid it; it just isn't our maintenance burden.)
- **NG-5**: Do not break the existing MCP tool names/signatures. Adoption
  depends on the contract staying stable. (Note: "files win" precedence
  *becomes* a real guarantee in this refactor — see MCM2-23 — and the
  watcher cascade must work against any registered StorageBackend.)
- **NG-6**: Do not introduce multi-hop graph traversal as part of this
  refactor. `get_related` stays single-hop. Recursive CTE is a separate
  feature, not a port-of-existing.
- **NG-7**: Do not *remove* stdio MCP transport. Today's Claude-Code-spawns-
  engine flow on the mini must continue to work unchanged. HTTP/SSE is
  added alongside (MCM2-20), not in place of, stdio.
- **NG-8 (new)**: Do not import adapter-specific libraries from the engine
  core. `psycopg`, `redis`, `opensearch-py`, `meilisearch`, etc. live in
  adapter packages or behind optional-extra import guards. A core
  `import mcm_engine` MUST succeed with nothing installed but the core's own
  deps (Python stdlib + whatever pure-Python deps the embedded path needs).
- **NG-9 (new)**: Do not build first-party adapters for every popular
  product. Three concerns × one or two reference adapters each is the budget
  for this refactor. The contract is what we maintain. Adapter coverage is
  what users build out.

## Conventions

- Task IDs `MCM2-NN`, stable, referenced in commits.
- RFC 2119 keywords: MUST / SHOULD / MAY.
- Each phase has explicit **acceptance criteria** — do not consider a phase done
  until they pass.
- **Checkpoint after Phase 0**: produce the repository extraction + interface
  proposal as a reviewable artifact and STOP for review before any remote work.

---

## Phase 0 — Repository extraction + interfaces + adapter registry (no behavior change)

This is the load-bearing phase and the biggest one. Before any adapter exists
besides the embedded reference, every tool function must stop running SQL
directly and instead call through a repository whose interface is the public
contract third parties implement.

- **MCM2-01**: Produce `docs/seam-inventory.md`: every SQL statement in
  `tools/*.py`, classified by table, operation, and which counter columns it
  touches. Note the composite rank expression in `tools/search.py` as a single
  high-priority item — it dictates the SearchBackend contract. Cite file:line.
- **MCM2-02**: Extract SQL out of tool functions into
  `mcm_engine/backends/embedded/sqlite_repository.py`, exposing query methods
  (e.g. `find_knowledge`, `insert_negative`, `bump_hit_count`, `link_edge`).
  Each method takes parameters, returns rows-or-dataclasses, hides SQL. The
  goal: every existing test passes against this repository with no behavior
  change. SQL stays in SQLite syntax — porting comes later.
- **MCM2-03**: Define the **public adapter contract** as four
  `Protocol`/`ABC` classes in `mcm_engine/backends/__init__.py`. These are
  what third parties implement. Stable. Versioned via a `CONTRACT_VERSION`
  constant adapters declare compatibility with.
  - `StorageBackend` — knowledge/rules/negative/errors/relations/sessions CRUD.
    Graph methods are single-hop only (NG-6). Returns rows as dataclasses,
    not formatted strings.
  - `CounterStore` — `increment(entity_type, entity_id, counter_name)`,
    `get(entity_type, entity_id) -> dict`, `top_by(scope, counter_name, k)`.
    The embedded reference reads/writes the same row columns the engine does
    today. The split is at the *call site* — the repository asks the
    CounterStore, never reads counter columns by SQL alone. This is what
    lets a user wire counters to anything (Redis, Memcached, Postgres rows,
    a dict, /dev/null) without the engine knowing.
  - `SearchBackend` — `search(query, scope, k, caller=None) -> list[SearchHit]`
    where `SearchHit` is a dataclass with `entity_type`, `entity_id`, `score`,
    `counters_snapshot`, `is_pinned`, `is_stale`. **Rendering to text moves
    out of `tools/search.py` into a small formatter module** so any adapter
    produces identical output shape.
  - `SessionStore` — persist tracker/nudge state. Embedded reference =
    in-memory (today's behavior). Per OQ-5, no first-party non-embedded
    adapter ships; the interface exists for third-party extensibility.
  - (Defer `QueueBackend` — note it, don't build it.)
- **MCM2-04**: Build the **composition root** (`mcm_engine/wiring.py`): reads
  config, looks up each named adapter in the registry (MCM2-04b),
  instantiates, injects into the tool layer. Tool functions receive their
  dependencies via a `Context` parameter (or module-level injection), not by
  opening connections. The composition root MUST NOT import any concrete
  adapter module directly — only the registry.
- **MCM2-04b (new)**: Build the **adapter registry**
  (`mcm_engine/registry.py`). Discovers adapters via
  `importlib.metadata.entry_points(group=...)` for the four groups
  (`mcm_engine.adapters.storage|counters|search|session`). Also supports a
  `module:Class` escape hatch for in-repo dev work. Includes a startup-time
  contract-version check: an adapter declaring a different
  `CONTRACT_VERSION` raises a clear error, doesn't silently misbehave.
- **MCM2-05 (cannot be retrofitted — do it now)**: Thread an **identity + scope
  filter through every read path**, as a no-op. Add `caller`/`scope` to read
  methods on `StorageBackend` and `SearchBackend`; local default resolves to
  "single user, sees everything" and the filter always returns true. Same
  logic as governance tags present-but-permissive.
- **MCM2-06 (config hygiene)**: Replace the silent-drop pattern at
  `src/mcm_engine/config.py:130` with a **fail-closed** parser. Sweep
  `load_config` for siblings (any `**{k:v for k,v in ... if k in fields}`
  pattern). Unknown declared keys MUST warn or error, never silently drop.
- **MCM2-07**: Refactor the plugin internals so plugins receive a
  `StorageBackend` reference instead of opening their own connections.
  No backward-compat burden (OQ-6: no external plugin users), so the
  plugin contract can change in lockstep with the rest of this refactor.

**Acceptance (Phase 0):**
- Full existing test suite passes unchanged (still SQLite-only at this point).
- `grep` confirms no `cursor.execute` outside `mcm_engine/adapters/sqlite/`
  (or wherever the embedded SQLite reference ends up).
- `grep` confirms no concrete adapter import outside the registry or the
  adapter packages themselves.
- `seam-inventory.md` exists and is complete.
- Adapter registry resolves a config-string name to a class via entry points;
  a trivial dummy adapter (e.g. a `RecordingStorage` that just logs calls)
  can be loaded by registering it and pointing config at it. **This is the
  proof that the registry is real before Phase 1 invests in Postgres.**
- Contract-version mismatch produces a clear error at startup, not a
  runtime AttributeError.
- Running with no backend config behaves exactly as today, byte-for-byte on
  search output (formatter parity).
- Config with an unknown nudge key now errors or warns, doesn't drop silently.
- Engine core `pyproject.toml` has zero adapter-specific dependencies
  (no `psycopg`, no `redis`, no `opensearch-py`).
- **STOP and request review of the interfaces + repository + registry +
  inventory before Phase 1.**

---

## Phase 1 — First reference adapter: Postgres storage (the spike)

Postgres is the first non-embedded adapter not because the engine prescribes
Postgres but because **Postgres is demanding enough that if the contract
holds for it, the contract holds.** Different SQL dialect, different
search-ranking API, different upsert semantics, server-process boundary
instead of in-process. If the contract leaks anywhere, it leaks here. Phase 1
is also where the **conformance test suite** becomes real, because Postgres
is the first thing that has to pass it without being the engine's own embedded
reference.

- **MCM2-08**: Implement `mcm_engine/adapters/postgres/storage.py` satisfying
  `StorageBackend`. Ships as the `[postgres]` extra of `mcm-engine`
  (`pip install mcm-engine[postgres]` pulls `psycopg` or equivalent).
  Registered via entry point. Lexical search via `tsvector`/`tsquery` +
  `pg_trgm`; upserts via Postgres `ON CONFLICT`. Connection pooling assumed
  external (PgBouncer); take a DSN.
- **MCM2-09**: Build `mcm_engine.testing.conformance` — the **conformance
  test suite** any storage adapter must pass. Lives in the core distribution
  (with `pytest` as a test extra). Tests assert result *shape* and presence,
  not cross-backend ordering (NG-3). Backend-specific behaviors (e.g. FTS5
  stemming vs `tsvector` config) are explicitly NOT in conformance — they
  are in per-adapter tests.
- **MCM2-09b (new)**: Run the conformance suite against both the embedded
  SQLite reference AND the Postgres adapter. Both green. Postgres runs
  **locally on the mini** via `tests/docker-compose.yml` (OQ-4) — not RDS.
- **MCM2-10**: Document the ranking-equivalence reality in `docs/`: FTS5 bm25
  composite vs Postgres `ts_rank_cd` composite produce coherent-but-different
  orderings. Both must be "good," neither is canonical. **This document is
  also the guide third-party adapter authors read** so they know what
  "passing conformance" means in practice.
- **MCM2-11**: **Data migration path.** Provide `mcm-engine migrate
  --from sqlite://path --to <adapter>://dsn` as an adapter-agnostic CLI.
  The first implementation reads SQLite v6 and writes via the *destination
  adapter's* `StorageBackend` interface — meaning the same migration tool
  works for any future storage adapter without code changes. Verify on a
  known-shape fixture before declaring done.

**Acceptance (Phase 1):**
- Same engine binary runs on SQLite (no config) and Postgres (DSN in config),
  zero code change between them.
- Backend-parametrized suite green on both, **on the mini, against a local
  Postgres container**. AWS RDS is not required to call this phase done.
- Switching is purely `config` — verified by a test that boots both wirings.
- v6 SQLite → Postgres migration tool exists and round-trips a fixture.

---

## Phase 2 — Counter store interface + first non-embedded counter adapter

The "counters live on the entry row" reality bites here. Splitting counters
off-row means the repository can no longer read counter columns directly in
SQL — every read either (a) does two round-trips (durable row + counter
lookup) or (b) gets a counter snapshot pushed back into the durable store on
a flush cadence and SQL reads stale-but-recent values.

Redis is a reasonable first reference because it has the right primitives
(`INCR`, sorted sets) and is the most-named choice for this kind of
workload. But it is **not the prescribed answer**. Many deployments will
want counters in their existing Postgres (one less external service), or
in-process even with Postgres storage (low write volume, single instance,
no need). The interface has to make those choices cheap.

- **MCM2-12**: Implement the flush policy as part of the CounterStore
  contract. The contract defines `flush()` and `last_flushed_snapshot()`;
  embedded reference flushes inline; adapters MAY batch and MUST declare
  their staleness window, **not to exceed a few minutes** (OQ-3).
  Composite ranking in search uses the counter snapshot on the durable row
  when reading durable, and live counts when reading search-backend results
  joined with CounterStore.
- **MCM2-13**: Implement `mcm_engine/adapters/redis/counters.py` satisfying
  `CounterStore` (atomic `INCR`, sorted-set `ZADD/ZRANGE`). Ships as the
  `[redis]` extra. Registered via entry point.
- **MCM2-13b (new)**: Demonstrate the contract is product-agnostic by
  shipping a **second** reference counter adapter:
  `mcm_engine/adapters/postgres/counters.py` — counters stored as rows in
  the same Postgres the storage adapter already uses. No second service to
  run. This is the answer for "I want Postgres + nothing else." Reuses the
  Postgres dependency already pulled in by `[postgres]`.
- **MCM2-14**: Move the ranking expression out of SQL and into a Python
  scorer that takes `bm25_rank` (or `ts_rank_cd`, or whatever the
  SearchBackend returns) and counter values from CounterStore. This severs
  ranking from any single SQL query and makes the composite formula
  testable in isolation. **Likely promoted to Phase 0** — it makes the
  CounterStore split mechanical.
- **MCM2-14b (new)**: Conformance suite for CounterStore. Same shape as
  Phase 1's storage conformance: any adapter implementing CounterStore can
  run this suite to verify correctness.

**Acceptance (Phase 2):**
- CounterStore conformance suite passes against embedded reference, Redis
  adapter, and Postgres-rows adapter — **all three, on the mini, against
  local containers**.
- Composite ranking shape identical regardless of which CounterStore is
  loaded (modulo documented staleness window).
- A user can wire `storage: postgres` + `counters: postgres` and run with
  zero non-Python services besides Postgres. **No Redis required.**
- The scorer is tested independently of any backend.

---

## Phase 3 — Search backend interface + reference adapters

The SearchBackend interface is the loosest of the four because "search" means
the most different things across products: bm25, `ts_rank_cd`, BM25F,
HNSW vector, hybrid, learned-to-rank. The contract must be permissive enough
to accommodate all of those, prescriptive enough that any adapter produces a
usable `SearchHit` list.

Two first-party reference adapters demonstrate that range:

- **MCM2-15a (Postgres-as-search — likely most common)**: Implement
  `mcm_engine/adapters/postgres/search.py` satisfying `SearchBackend`,
  reusing the Postgres storage adapter's connection. `ts_rank_cd` + `pg_trgm`,
  rank composed in Python (MCM2-14). This is the answer for "Postgres for
  everything" — the deployment that needs zero managed services beyond a
  database. Ships under the `[postgres]` extra.
- **MCM2-15b (OpenSearch — demanding case)**: Implement
  `mcm_engine/adapters/opensearch/search.py` satisfying `SearchBackend`
  (lexical, optionally vector). Treat the index as a **rebuildable
  projection** of the durable store, not a system of record — provide a
  reindex path. Ships under the `[opensearch]` extra.
- **MCM2-15c (new)**: Conformance suite for SearchBackend. Tests
  shape — `SearchHit` fields populated, ordering monotonic by score, scope
  filtering honored, caller filter honored.
- **MCM2-16 (embedding model)**: Vector search is the feature that doesn't
  embed cleanly. **Defer to a follow-up phase.** Phase 3 ships lexical only
  for both reference adapters. When vector search is added, the embedding
  source itself becomes a pluggable interface (`EmbeddingBackend`?) —
  defining it now would lock in a guess. (Local sentence-transformer vs
  remote API stays as OQ-1 for the day this matters.)
- **MCM2-17**: **Capability flags + honest degradation.** Embedded mode may
  be *less capable*, never *broken*. If an adapter doesn't support a
  capability the engine asked for, it reports that cleanly rather than
  erroring. Same applies to third-party adapters: declare capabilities,
  degrade gracefully.

**Acceptance (Phase 3):**
- Lexical search works on the embedded SQLite reference, Postgres adapter,
  and OpenSearch adapter behind one interface, **proven against local
  containers on the mini**. AWS OpenSearch Service is not required.
- A user can deploy with storage→Postgres + search→Postgres and no other
  services besides Postgres. **No OpenSearch required.**
- SearchBackend conformance suite passes against all three reference
  implementations.
- Reindex-from-durable-store path exists and is tested.

---

## Phase 4 — Packaging the two deployments

Phase 4 splits cleanly along the topology line. **4a is mini-side** and can
finish without ever touching AWS. **4b is work-system-side** and is the only
phase that requires an AWS account.

### Phase 4a — Local packaging + transport + config shape (mini)

- **MCM2-18**: `mcm-engine init` on a clean machine writes a config with **no
  backends declared** → everything in-process. Verify on a container with only
  Python and SQLite (system library). This command does not exist today; add it.
- **MCM2-19**: Make the **four scaling axes orthogonal config switches**:
  `store` (sqlite|postgres), `lifetime` (ephemeral|daemon), `tenancy`
  (single|multi), `trust` (open|scoped). Defaults =
  sqlite/ephemeral/single/open. Persistent-local and trusted-team-Postgres-
  without-full-scoping must both be expressible. Each combination has at least
  one boot test on the mini.
- **MCM2-20 (transport — resolved per OQ-2)**: Implement **HTTP/SSE via
  FastMCP** as a second transport alongside the existing stdio mode.
  - `mcm-engine` (no subcommand) keeps today's stdio behavior — what
    Claude Code on the mini spawns for daily use.
  - `mcm-engine serve --host 0.0.0.0 --port 8080` starts the long-lived
    HTTP/SSE daemon. Exposes `/healthz` (process alive) and `/readyz`
    (adapters connected). Runs the watcher cascade (MCM2-23).
  - Auth surface for the HTTP daemon: a shared bearer token in env (kept
    simple for v1; revisit when multi-tenancy lands per MCM2-05's identity
    threading).
  - Same composition root; the transport is the only thing that differs.
  Prototype on the mini against local Postgres+Redis+OpenSearch containers
  — the daemon is provably working before any AWS exposure in Phase 4b.
- **MCM2-21**: Build the **deployable container image** locally
  (multi-arch `linux/amd64` + `linux/arm64`). The image bakes the engine
  binary and entry point but takes all backend wiring from env-injected
  config. Validate on the mini by running the image against the local
  Postgres/Redis/OpenSearch containers — full scaled wiring, on one machine.
- **MCM2-23 (new — files-win watcher cascade, per OQ-8)**: Build the
  rules-file watcher. Uses `watchdog` (or equivalent) to monitor
  `rules/**/*.md`. On file change/create/delete, reparse and cascade into
  the loaded `StorageBackend` so the DB reflects the file. Conflict
  resolution rule: **files win over concurrent in-process DB writes**.
  - **Engine-initiated writes** (`add_rule` and friends) write the file
    FIRST, then update the DB; the watcher's debouncer + content-hash
    check makes the cascade a no-op when the in-process write already
    matched.
  - **External edits** (user opens an editor and saves a rule) fire the
    watcher; the watcher updates the DB; the next search reflects the file.
  - **Deletion** of a rule file marks the corresponding row archived (or
    removed — settle this when implementing).
  - **Mode interaction**: In **daemon mode**, the watcher runs as a
    background thread alongside the request handler. In **stdio mode**,
    where the engine process lifecycle is per-session, the watcher is not
    started; instead the engine calls `sync_rules` once at startup so the
    DB is current as of process start. Stdio mode therefore offers a
    degraded form of "files win" — sync at startup, not live. This is
    the right trade-off for the embedded use case.
  - Watcher lives in the engine core (`mcm_engine/files/watcher.py`) — it
    talks to `StorageBackend` through the contract, so it works against
    any adapter. NG-8 still holds: the core can import `watchdog`
    (pure-Python, no external service); that's not an adapter dep.

**Acceptance (Phase 4a):**
- Clean-machine `init` + run with zero external services succeeds.
- Container image runs locally against local containers in **at least three
  meaningful wirings**: (1) full Postgres+Redis+OpenSearch, (2)
  Postgres-only (storage + counters + search all served by one Postgres),
  (3) embedded-everything. **This is the shippable artifact.**
- A third-party adapter — registered via entry point in a sibling package
  installed separately — can be loaded by name from config and used without
  modifying the engine. Validated with a trivial in-repo sibling package
  (`packages/example-adapter/`) that the test harness installs and loads.
- Watcher cascade (MCM2-23) works against the embedded SQLite reference
  AND the Postgres adapter: editing a rule `.md` file externally causes
  the next search to reflect the new content within a debounce window.
- Health/readiness endpoints respond correctly (if HTTP transport chosen).
- All four scaling-axis combinations have a passing boot test.

### Phase 4b — AWS deployment validation (work system)

This is the only phase that requires AWS access, and only because the user
has chosen AWS as their first scaled deployment. **The engine doesn't care.**
A different user picking GCP, Fly.io, Hetzner, Render, or a bare VM with
docker compose would run an equivalent phase against their target without
any engine changes. What follows is the **AWS instance** of this generic
"validate the same image against managed/external services" phase.

Code from 4a is what gets deployed; nothing here changes the engine. Work
is terraform + IAM + DNS + operational validation, not application code.

- **MCM2-22**: Push the 4a container image to ECR on the work system.
- **MCM2-23**: Provision **storage→RDS Postgres, counters→ElastiCache Redis,
  search→OpenSearch Service** via terraform. Reuse the connection strings the
  local validation already proved work.
- **MCM2-24**: Run the engine on App Runner (or ECS, decide on the work
  system based on what's already standardized there). Configure via env vars
  pointing at the three managed services.
- **MCM2-25**: Run the same backend-parametrized suite from Phase 1 against
  RDS instead of local Postgres. Result: identical green. If it isn't, the
  StorageBackend abstraction leaked something AWS-specific and we fix the
  interface, not paper it over.
- **MCM2-26**: CI runs the **embedded configuration by default on every
  change** (on whichever runner CI uses — works from anywhere). The
  remote-backend matrix runs on a schedule or pre-release, against either
  local containers (cheap, on every PR) or the work-system AWS stack
  (expensive, pre-release only).

**Acceptance (Phase 4b):**
- Same image runs on App Runner/ECS with RDS+ElastiCache+OpenSearch via
  config only.
- Backend-parametrized suite passes against RDS, not just local Postgres.
- CI proves both paths; embedded path runs on every commit.

---

## Build order rationale

Repository extraction (Phase 0) is the bulk of the work, because SQL today
lives inside tool functions and there is no service layer to drop interfaces
behind. Once SQL is behind a repository, swapping SQLite for local Postgres
(Phase 1) is the spike that proves the seam. Counter split (Phase 2) requires
the scorer to be pulled out of SQL first — possibly in Phase 0. Search
backend (Phase 3) is the loosest interface but has the embedding-model
question attached.

Phases 0–3 and Phase 4a all run on the mini, against local services. Only
Phase 4b requires AWS. So the work has a natural milestone shape: **at end
of Phase 4a, the engine is a finished, scaled, locally-validated product.**
Phase 4b is "ship it to AWS." That ordering means we never block local
progress on AWS access, and Phase 4b becomes a small, focused trip to the
work system rather than ongoing back-and-forth.

If Phase 1 swaps cleanly (against local Postgres on the mini), the rest is
low-risk. If Phase 1 fights the interface, the interface is wrong — fix the
interface before continuing.

## Open questions for the owner (flag, don't guess)

- **OQ-1 (resolved)**: When vector search is added in a follow-up phase,
  the **embedding source is its own pluggable `EmbeddingBackend`
  interface**, same model as the four contracts in this plan. Local
  sentence-transformer, remote API, on-prem inference server — all are
  adapter choices, not engine prescriptions. Not blocking this refactor.
- **OQ-2 → MCM2-20 (resolved)**: Daemon-mode transport is **HTTP/SSE via
  FastMCP**. Stdio stays in place for today's local-MCP-client usage
  (Claude Code spawning the engine on the mini); HTTP/SSE is the new
  transport for the long-lived daemon used in scaled deployments and
  enables the watcher cascade (MCM2-23). Same composition root, two
  transport entry points: `mcm-engine` (stdio) and `mcm-engine serve`
  (HTTP/SSE with `/healthz` + `/readyz`).
- **OQ-3 (resolved)**: Counter staleness window is **minutes**. The
  CounterStore contract says: embedded reference writes through
  synchronously; adapters MAY batch with a declared staleness window not
  exceeding a few minutes. Loss window on adapter crash is bounded to that
  window.
- **OQ-4 (resolved)**: Postgres (and Redis, OpenSearch) test harness uses
  `docker compose`. A `tests/docker-compose.yml` declares the services on
  known ports; developer runs `docker compose up -d` once, tests connect to
  the running services. Same compose file is reused for manual local
  poking. CI runs `docker compose up -d` as a step before `pytest`.
- **OQ-5 (resolved)**: Session/tracker state stays **in-memory only**.
  The `SessionStore` interface still exists for plug-in extensibility, but
  no first-party non-embedded session adapter ships. "Lose tracker state
  on restart" is the accepted behavior.
- **OQ-6 (resolved)**: No external plugin users exist, so there is no
  backward-compatibility window. Refactor the plugin internals freely so
  plugins receive a `StorageBackend` reference cleanly. No migration
  burden. MCM2-07 simplifies accordingly.
- **OQ-7 (resolved)**: `SearchBackend.search()` returns **structured**
  `SearchHit` dataclasses; a separate formatter module renders them to
  the bracketed-text shape MCP clients consume today. The formatter is
  shared across all adapters, so output is identical regardless of which
  search adapter is loaded. Already baked into MCM2-03.
- **OQ-8 (resolved)**: **Build the watcher cascade as part of this
  refactor.** A filesystem watcher monitors `rules/**/*.md`; file changes
  cascade into the loaded `StorageBackend` automatically. "Files win"
  becomes a real guarantee, not just an aspiration. See MCM2-23 in Phase 4a.
- **OQ-9 (resolved)**: Container runtime on the mini — **Docker Desktop**
  is installed (`docker` v28.5.1 confirmed). Phases 1–3 and 4a use
  `docker compose` / testcontainers against the local Docker daemon. No
  additional install needed.
- **OQ-10 (resolved)**: Contract versioning is a **single integer
  `CONTRACT_VERSION`**, bumped on any breaking change. Adapters declare
  the version they were built against; mismatch fails loudly at startup.
  Revisit if/when third-party adapter count grows enough to need semver.
