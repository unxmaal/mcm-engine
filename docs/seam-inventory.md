# Seam inventory — every SQL statement in mcm-engine v1

This is the catalog every Phase 0 task depends on: a complete enumeration of
every SQL statement in `src/mcm_engine/`, classified by table, operation,
and which counter columns it touches.

Compiled from a full read of:

- `src/mcm_engine/db.py` (connection layer)
- `src/mcm_engine/schema.py` (DDL + migrations)
- `src/mcm_engine/plugin.py` (plugin SearchScope SQL)
- `src/mcm_engine/tools/search.py`
- `src/mcm_engine/tools/knowledge.py`
- `src/mcm_engine/tools/rules.py`
- `src/mcm_engine/tools/relations.py`
- `src/mcm_engine/tools/session.py`

The remaining files in the package (`__init__.py`, `cli.py`, `config.py`,
`server.py`, `tracker.py`, `tools/__init__.py`) contain no SQL.

---

## TL;DR

- **74 distinct SQL statements** spread across 8 files.
- **Zero centralization.** Every tool function opens `db.execute(...)` /
  `db.execute_write(...)` directly. There is no repository layer between
  MCP-tool dispatch and SQL.
- **One composite ranking expression** appears twice (knowledge + rules) and
  is the load-bearing piece of the SearchBackend contract. See "High-priority
  surface" below.
- **Counter writes are scattered.** 5 distinct UPDATE sites touch `hit_count`,
  `reinforcement_count`, or `pinned` on the entry rows. Two of them sit
  inline inside SELECT loops in `search.py` (`UPDATE knowledge` and
  `UPDATE rules` after a successful FTS match).
- **Three dynamic-table-name patterns** (`f"... FROM {table} ..."`) in
  `tools/knowledge.py`, `tools/relations.py`, `tools/session.py`. These
  resist a typed repository API; the repository will need a
  `pin_entry(entry_type, entry_id, value)` style method rather than per-table
  `pin_knowledge` / `pin_rule` / etc.
- **One `{LIKE_WHERE}` string-templating idiom** in `tools/search.py` — SQL
  with a placeholder substituted at runtime. Not parameterized; literal
  replace. Must be reimplemented per-backend.
- **One hard `DELETE`** in the entire codebase (`tools/rules.py:389`, orphan
  removal in `sync_rules`). Everything else is INSERT or UPDATE. Watcher
  cascade (MCM2-23) will add a second DELETE path — or, more likely, replace
  the hard delete with a soft-delete via `archived` columns.
- **Three SQLite-only idioms** appear everywhere: `datetime('now')`,
  `julianday('now') - julianday(col)`, FTS5 `MATCH` + `rank`. All must be
  translated per-backend.
- **WAL + busy_timeout + write-retry** in `db.py` encodes the
  single-writer-many-readers assumption. The repository interface needs to
  describe write semantics adapter-agnostically.

---

## High-priority surface: the composite ranking expression

This is the SearchBackend contract's load-bearing claim. It appears
**twice** with identical shape, once for `knowledge` and once for `rules`:

**`tools/search.py:115-117`** (knowledge):
```sql
ORDER BY (rank
          - 0.1 * k.hit_count
          - 0.3 * k.reinforcement_count
          - 2.0 * k.pinned
          - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(k.created_at), 999)) / 30.0)
```

**`tools/search.py:271-273`** (rules):
```sql
ORDER BY (rank
          - 0.1 * r.hit_count
          - 0.3 * r.reinforcement_count
          - 2.0 * r.pinned
          - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(r.created_at), 999)) / 30.0)
```

Five inputs feed the score:

| Input              | Source                               | Weight | Direction       |
|--------------------|--------------------------------------|--------|-----------------|
| `rank`             | FTS5 bm25, negative                  | 1.0    | Lower = better  |
| `hit_count`        | Counter column on the entry row      | 0.1    | More = better   |
| `reinforcement_count` | Counter column on the entry row   | 0.3    | More = better   |
| `pinned`           | Boolean column on the entry row      | 2.0    | True = much better |
| Recency bonus      | `julianday('now') - julianday(created_at)` | 1/30  | Newer (<30d) = better |

Three reduced-shape variants exist for tables without rank-tracking:
- **`tools/search.py:182`** (negative): `ORDER BY (rank - 2.0 * n.pinned)` —
  pinned is the only signal beyond bm25.
- **`tools/search.py:228`** (errors): `ORDER BY (rank - 2.0 * e.pinned)` —
  same.
- **`tools/search.py:138, 287`** (LIKE fallback for knowledge + rules):
  `ORDER BY (hit_count + MAX(0, 30 - ...) / 30.0) DESC` — no bm25
  available; pure counter + recency.

**Implications for the SearchBackend contract**:

1. The expression must move out of SQL and into a Python scorer
   (MCM2-14). A `SearchBackend` adapter cannot return a single
   composite-ranked list because the score depends on `hit_count` and
   `reinforcement_count`, which live in `CounterStore` — a different
   interface owned by a different (possibly different-process) adapter.
2. The scorer takes `(bm25_or_equivalent_rank, counter_snapshot,
   created_at)` and returns a composite. Different backends can produce
   different `rank` shapes (bm25 negative, `ts_rank_cd` positive, custom
   from Meilisearch); the scorer normalizes.
3. Pinned-only ranking for `negative` and `errors` is a *capability*: an
   adapter that doesn't track counters can still serve these scopes
   correctly with only `rank + pinned`.

---

## File-by-file inventory

### `src/mcm_engine/db.py` — connection layer

| Line | Statement | Notes |
|------|-----------|-------|
| 117  | `PRAGMA journal_mode=WAL` | SQLite-only |
| 118  | `PRAGMA synchronous=NORMAL` | SQLite-only |
| 119  | `PRAGMA busy_timeout=5000` | SQLite-only |

No table reads/writes in this file. `KnowledgeDB.execute`,
`execute_write`, `executescript`, `commit`, `_open_connection`,
`_reconnect` are the only paths to the DB from anywhere; every tool
function calls one of these.

**Helpers that build SQL strings (not SQL themselves, but FTS5 dialect):**

| Function | Lines | Purpose | Backend-specific |
|----------|-------|---------|------------------|
| `sanitize_fts` | 33-36 | Quote terms so hyphens/colons don't break FTS5 | FTS5 syntax |
| `build_fts_queries` | 49-83 | Build AND → OR → prefix-match query series for FTS5 | FTS5 syntax |
| `build_like_patterns` | 86-95 | `%term%` patterns for LIKE fallback | SQL-portable |

These belong with the SearchBackend adapter (FTS5 syntax is SQLite-specific);
`build_like_patterns` is generic and can stay engine-side.

### `src/mcm_engine/schema.py` — DDL + migrations

The entire file is SQL. Notable sections:

- **`CORE_SCHEMA`** (lines 9-225): full DDL for all tables, indexes, FTS5
  virtual tables, and 12 FTS-sync triggers. Each adapter must implement an
  equivalent `ensure_schema()`.
- **`PRAGMA table_info(...)`** in `_has_column` (line 230) — SQLite-only;
  adapters need a portable "column exists" check (Postgres uses
  `information_schema.columns`).
- **Migrations v1→v6** (lines 236-404): ALTER TABLE ADD COLUMN,
  DROP TRIGGER, DROP TABLE, CREATE VIRTUAL TABLE, INSERT INTO {fts}('rebuild')
  + 12 CREATE TRIGGER. The `_MIGRATIONS` list-of-callables pattern (line 407)
  is engine-side, not adapter-side.
- **Migration v7→v8** (issue #10): adds `rules.content` / `created_by` /
  `updated_by`, drops+recreates `rules_fts` with the `content` column and
  its three sync triggers, and creates the append-only `rule_events` audit
  table (+ index). SQL-site count for `schema.py`: 36 → 48.
- **`migrate_core`** (lines 417-455): INSERT/SELECT/UPDATE on
  `_mcm_versions`. Idempotent boot path.
- **`migrate_plugin`** (lines 458-478): same shape for plugin schemas.

The migration *mechanism* (a list of `(from_version, to_version, fn)` tuples,
the `_mcm_versions` table) survives the refactor. The migration *steps*
themselves become adapter-specific — the embedded SQLite adapter keeps its
own list; the Postgres adapter has its own list. Both honor the same
`_mcm_versions.version` semantics.

### `src/mcm_engine/tools/search.py` — search + LIKE fallback + counter increments

| Line | Operation | Table | Notes |
|------|-----------|-------|-------|
| 108-117 | SELECT | `knowledge_fts` JOIN `knowledge` | **High-priority composite rank**. FTS5 MATCH + project filter + counter columns + recency. |
| 131-139 | SELECT | `knowledge` | LIKE fallback. Templated `{LIKE_WHERE}` substitution. |
| 154-157 | UPDATE | `knowledge` | **Counter write: `hit_count += 1`, `last_hit_at = datetime('now')`.** Fired inline per row in the SELECT loop. |
| 176-186 | SELECT | `negative_fts` JOIN `negative_knowledge` | Reduced rank: `rank - 2.0 * pinned`. |
| 195-199 | SELECT | `negative_knowledge` | LIKE fallback. |
| 222-231 | SELECT | `errors_fts` JOIN `errors` | Reduced rank: `rank - 2.0 * pinned`. |
| 241-244 | SELECT | `errors` | LIKE fallback. |
| 262-273 | SELECT | `rules_fts` JOIN `rules` | **High-priority composite rank (duplicate of knowledge shape)**. No project filter — rules have no project column. |
| 280-289 | SELECT | `rules` | LIKE fallback. |
| 307-310 | UPDATE | `rules` | **Counter write: `hit_count += 1`, `last_hit_at = datetime('now')`.** Same pattern as knowledge. |

**The `{LIKE_WHERE}` templating idiom** (line 72 in `_like_search`):
`full_sql = sql.replace("{LIKE_WHERE}", where)`. SQL with a literal-string
placeholder substituted at runtime from a list of column names. The
*column names* are not user-supplied (they come from the per-table list
in the caller); but the pattern is non-parameterized SQL assembly and
each backend's LIKE syntax differs (Postgres `ILIKE`, `pg_trgm`'s `%`
operator, etc.). The repository must own the LIKE-fallback shape.

### `src/mcm_engine/tools/knowledge.py` — add_knowledge / add_negative / report_error / reinforce / pin / unpin

| Line | Operation | Table | Notes |
|------|-----------|-------|-------|
| 80-82 | SELECT | `knowledge` | Exact-topic-match dedup probe. |
| 84-89 | UPDATE | `knowledge` | Update-on-duplicate-topic. Does not touch counter columns. |
| 100-104 | SELECT | `knowledge_fts` JOIN `knowledge` | Fuzzy-dedup probe (`ORDER BY rank LIMIT 1`). |
| 111-115 | INSERT | `knowledge` | New knowledge entry. |
| 143-148 | INSERT | `negative_knowledge` | New negative entry. |
| 177-180 | INSERT | `errors` | New error log. |
| 210 | SELECT | `knowledge` | `reinforce_knowledge` existence check. |
| 214-218 | UPDATE | `knowledge` | **Counter write: `reinforcement_count += 1`, `last_hit_at`, `updated_at`.** |
| 220-222 | SELECT | `knowledge` | Read back `reinforcement_count` for response. |
| 251 | SELECT | `{table}` (dynamic) | **`pin_item` existence check across pinnable tables.** Dynamic table name from `_PINNABLE_TABLES`. |
| 255 | UPDATE | `{table}` (dynamic) | **Counter write: `pinned = 1`.** Dynamic table name. |
| 274 | SELECT | `{table}` (dynamic) | `unpin_item` existence check. |
| 278 | UPDATE | `{table}` (dynamic) | **Counter write: `pinned = 0`.** Dynamic table name. |

The dynamic-table pin/unpin sites are the cleanest example of why the
repository API can't be per-table methods (`pin_knowledge`, `pin_rule`,
…). It needs a single `set_pinned(entry_type, entry_id, value)` method.

### `src/mcm_engine/tools/rules.py` — add_rule / read_rule / promote_to_rule / sync_rules / reinforce_rule

| Line | Operation | Table | Notes |
|------|-----------|-------|-------|
| 147-149 | SELECT | `rules` | Duplicate-by-title probe. |
| 152-156 | UPDATE | `rules` | Update on duplicate. No counter columns. |
| 196-200 | INSERT | `rules` | New rule indexed. |
| 225-230 | UPDATE | `rules` | **Counter write: `hit_count += 1`, `last_hit_at`, `updated_at` WHERE file_path = ?**. Called by `read_rule`. |
| 261-264 | SELECT | `knowledge` | `promote_to_rule` reads source for knowledge type. |
| 274-277 | SELECT | `negative_knowledge` | Same for negative source. |
| 292-294 | SELECT | `errors` | Same for error source. |
| 362-364 | SELECT | `rules` | `sync_rules` checks if a file's row exists by `file_path`. |
| 367-371 | UPDATE | `rules` | `sync_rules` updates existing rule row. |
| 374-378 | INSERT | `rules` | `sync_rules` inserts new rule row. |
| 382 | SELECT | `rules` | `sync_rules` orphan scan: list all rows with `file_path`. |
| 389 | **DELETE** | `rules` | **The only hard DELETE in the entire codebase.** Orphan row removal. Watcher cascade (MCM2-23) replaces this with a soft-delete via the new `archived` column. |
| 409 | SELECT | `rules` | `reinforce_rule` existence check. |
| 413-417 | UPDATE | `rules` | **Counter write: `reinforcement_count += 1`, `last_hit_at`, `updated_at`.** |
| 419-421 | SELECT | `rules` | Read back `reinforcement_count`. |

`sync_rules` (lines 322-397) is the entire current implementation of
"files win." Reads `.md` files from disk, parses, upserts into `rules`,
deletes orphan rows. Manual, one-way, no watcher. The watcher cascade
(MCM2-23) supersedes it but doesn't remove it — `sync_rules` becomes
the startup-time backstop in both daemon and stdio modes.

### `src/mcm_engine/tools/relations.py` — link_knowledge / get_related

| Line | Operation | Table | Notes |
|------|-----------|-------|-------|
| 23   | SELECT | `knowledge` | Entry-label fetch for output formatting. |
| 26   | SELECT | `errors` | Same. |
| 29   | SELECT | `rules` | Same. |
| 32-34 | SELECT | `negative_knowledge` | Same. |
| 50   | SELECT | `{table}` (dynamic) | **`_entry_exists` across all four entity tables.** Dynamic table name from `table_map`. |
| 104-108 | INSERT | `relations` | New typed edge. Catches UNIQUE violation as "already exists" rather than raising. |
| 149-153 | SELECT | `relations` | Outgoing edges. |
| 156-160 | SELECT | `relations` | Incoming edges. |

`relations` is single-hop only. The two SELECTs at 149 and 156 are the
entire query surface. **No recursive CTE.** Multi-hop is NG-6 — not in
this refactor.

### `src/mcm_engine/tools/session.py` — session_start / session_handoff / session_summary / save_snapshot / get_resume_context

| Line | Operation | Table | Notes |
|------|-----------|-------|-------|
| 39-41 | SELECT COUNT(*) | `knowledge` | Recent (7d) count. Uses `datetime('now', '-7 days')`. |
| 50   | SELECT COUNT(*) | `{table}` (dynamic) | Per-table totals: knowledge, negative_knowledge, errors. |
| 52-55 | SELECT COUNT(*) | `{table}` (dynamic) | Per-table project-filtered counts. |
| 63   | SELECT COUNT(*) | `rules` | Total rule count. |
| 70   | SELECT COUNT(*) | `relations` | Total relations. |
| 77   | SELECT COUNT(*) | `snapshots` | Total snapshots. |
| 84-89 | SELECT COUNT(*) | `knowledge` | **Stale knowledge count** — uses `julianday('now') - julianday(col) > 90` twice and `pinned = 0`. |
| 104-106 | SELECT | `{table}` (dynamic) | Per-table pinned-items enumeration. |
| 115-118 | SELECT | `sessions` | Last handoff. |
| 167-169 | SELECT COUNT(*) | `knowledge` | Last-24h knowledge count for handoff. |
| 177-182 | INSERT | `sessions` | Write handoff row. |
| 186  | SELECT | `sessions` | Get just-written session id. |
| 191-200 | SELECT | `snapshots` | Next sequence number (handles NULL session_id). |
| 204-209 | INSERT | `snapshots` | Auto-snapshot at handoff. |
| 225-227 | SELECT COUNT(*) | `knowledge` | Last-hour count for session_summary. |
| 275-277 | SELECT | `sessions` | Find current session. |
| 282-291 | SELECT | `snapshots` | Next sequence number. |
| 294-301 | INSERT | `snapshots` | Manual snapshot. |
| 319-322 | SELECT | `sessions` | `get_resume_context` last session. |
| 336-338 | SELECT | `snapshots` | Last snapshot. |
| 360-362 | SELECT | `knowledge` | Pinned knowledge list. |
| 371-373 | SELECT | `negative_knowledge` | Pinned negative list. |
| 383-385 | SELECT | `errors` | Pinned errors. |
| 398-400 | SELECT | `rules` | Pinned rules. |

`session.py` is **the densest concentration of dynamic-table-name patterns** —
five distinct call sites loop over a list of table-label tuples and
interpolate the table name into the SQL string. The repository API needs
a `count_by_type(entity_type, *, project=..., pinned=..., ...)` shape.

### `src/mcm_engine/plugin.py` — plugin SearchScope SQL (RESOLVED in MCM2-07)

Post-MCM2-07: `plugin.py` holds **zero** SQL execution sites. The two
formerly-here sites (FTS path + LIKE fallback) live in
`adapters/sqlite/search.py::SqliteSearch.search_plugin`, which the engine
calls with the plugin's `SearchScope` descriptor. The descriptor is now
passive — table/column names only, no SQL behavior.

Plugins still own their FTS5 table layout via `MCMPlugin.get_schema_sql()`
(arbitrary DDL); that's expected and the only remaining SQL surface the
plugin layer carries, and it runs through `migrate_plugin` rather than the
adapter contract.

---

## Counter-touching SQL — the CounterStore split frontier

Splitting counters off the entry row (Phase 2) means every write below
moves from a SQL `UPDATE` to a `CounterStore.increment()` call, and every
SELECT that *reads* counter columns either fetches them from CounterStore
or reads a flushed snapshot column.

**Writes (5 sites, all in `search.py`, `knowledge.py`, `rules.py`):**

| File:Line | Counter | Trigger |
|-----------|---------|---------|
| `search.py:154-157` | `knowledge.hit_count`, `knowledge.last_hit_at` | Successful FTS hit on knowledge. |
| `search.py:307-310` | `rules.hit_count`, `rules.last_hit_at` | Successful FTS hit on rules. |
| `knowledge.py:214-218` | `knowledge.reinforcement_count`, `last_hit_at`, `updated_at` | `reinforce_knowledge` tool. |
| `knowledge.py:255, 278` | `{table}.pinned` | `pin_item` / `unpin_item` (dynamic table). |
| `rules.py:225-230` | `rules.hit_count`, `last_hit_at`, `updated_at` | `read_rule` tool. |
| `rules.py:413-417` | `rules.reinforcement_count`, `last_hit_at`, `updated_at` | `reinforce_rule` tool. |

**Reads (5 sites):**

| File:Line | Counter columns read | Purpose |
|-----------|----------------------|---------|
| `search.py:108-117` | `hit_count`, `reinforcement_count`, `pinned` (on `knowledge`) | Composite rank. |
| `search.py:176-186` | `pinned` (on `negative_knowledge`) | Reduced rank. |
| `search.py:222-231` | `pinned` (on `errors`) | Reduced rank. |
| `search.py:262-273` | `hit_count`, `reinforcement_count`, `pinned` (on `rules`) | Composite rank. |
| `session.py:84-89, 104-106, 360+, 371+, 383+, 398+` | `pinned` across all four tables | Pinned-item enumerations. |

**Implication.** `pinned` is a counter (boolean, but it's manipulated by
`CounterStore.set_pinned`/`pin_entry` and read in ranking). If we model
`pinned` as part of CounterStore, the entry-row column becomes a
"last-flushed snapshot" — which is consistent with the staleness-window
model OQ-3 resolved at minutes-scale.

---

## SQLite-only idioms (must be translated per backend)

### Time functions

- **`datetime('now')`** — appears in 14 INSERT/UPDATE statements as the
  default for `last_hit_at`, `updated_at`. Postgres equivalent: `now()`.
- **`datetime('now', '-7 days')` / `-1 day` / `-1 hour`** — relative-time
  windows in 4 count queries (`session.py`). Postgres equivalent:
  `now() - interval '7 days'`.
- **`julianday('now') - julianday(col)`** — age-in-days computation. Used
  6 times: 4 inside the composite-rank ORDER BY, 2 in the stale-knowledge
  count. Postgres equivalent: `extract(epoch from now() - col) / 86400.0`,
  or `(now()::date - col::date)` for integer-day precision.

A small **time helper** in the repository / scorer layer absorbs these.
Adapters never see the dialect-specific expressions.

### FTS5 surface

- **`{fts_table} MATCH ?`** — full-text query operator. Postgres
  equivalent: `tsv @@ plainto_tsquery('english', ?)` (or
  `websearch_to_tsquery` for a more forgiving syntax).
- **`rank` as an output column from FTS5 JOIN** — implicit bm25 score,
  negative. Postgres equivalent: `ts_rank_cd(tsv, query)` as an explicit
  output, positive. **Sign flips.**
- **`ORDER BY rank`** with no column qualifier — relies on FTS5's
  implicit-column behavior. Postgres requires the explicit `ts_rank_cd(...)`.
- **`INSERT INTO {fts}('rebuild')`** — FTS5 control statement for
  rebuilding the index. Postgres equivalent: `REINDEX INDEX idx_*_tsv`
  (cheaper because the index is on a stored generated column).

### Dynamic-table-name interpolation

Three occurrence sites:

- `knowledge.py:251, 255, 274, 278` — `_PINNABLE_TABLES` keys to table
  names for pin/unpin.
- `relations.py:50` — `table_map` for entry-existence checks.
- `session.py:50, 52-55, 104-106` — table-label loops for counts and
  pinned-item enumeration.

These cannot be parameterized via `?` placeholders (SQL identifiers can't
be bound). The repository API needs **enum-typed methods**:

```python
class EntityType(StrEnum):
    KNOWLEDGE = "knowledge"
    NEGATIVE = "negative"
    ERROR = "error"
    RULE = "rule"

def set_pinned(self, entity_type: EntityType, entity_id: int, value: bool) -> None: ...
def count_by_type(self, entity_type: EntityType, *, project: str | None = None, pinned: bool | None = None) -> int: ...
def list_pinned(self, entity_type: EntityType) -> list[Row]: ...
```

This way the repository implementation maps the enum to a table name
internally; the SQL stays parameterized at every call site.

### Connection management assumptions

`db.py:108-153` encodes a single-writer, many-readers model:
WAL journal mode, `busy_timeout=5000`, one-retry on `OperationalError`
matching `"readonly"` or `"locked"`. The retry path closes and reopens
the connection (`_reconnect`), which makes sense for SQLite's per-process
file lock but is meaningless for a Postgres connection.

The repository interface MUST NOT expose retry semantics — adapters own
their own retry policies. The interface promises an operation is "either
applied or raises"; how that's achieved is adapter-internal.

---

## Implications for the StorageBackend contract (input to MCM2-03)

Based on this inventory, the `StorageBackend` Protocol needs these
method shapes (preliminary — Phase 0 will refine):

**Knowledge CRUD:**
- `find_knowledge_by_topic_kind(topic, kind) -> Row | None`
- `insert_knowledge(KnowledgeRow) -> int`  (returns new id)
- `update_knowledge(id, **fields) -> None`

**Negative knowledge:**
- `insert_negative(NegativeRow) -> int`

**Errors:**
- `insert_error(ErrorRow) -> int`

**Rules:**
- `find_rule_by_title(title) -> Row | None`
- `find_rule_by_file_path(path) -> Row | None`
- `insert_rule(RuleRow) -> int`
- `update_rule(id, **fields) -> None`
- `list_rules_with_file_paths() -> list[Row]`  (for sync_rules orphan scan)
- `soft_delete_rule(id) -> None`  (replaces today's hard DELETE)
- `restore_rule(id) -> None`  (for the watcher's create-after-delete path)

**Relations:**
- `insert_relation(RelationRow) -> int | None`  (None on UNIQUE violation)
- `list_outgoing_relations(source_type, source_id) -> list[Row]`
- `list_incoming_relations(target_type, target_id) -> list[Row]`

**Sessions + snapshots:**
- `insert_session(SessionRow) -> int`
- `get_last_session() -> Row | None`
- `next_snapshot_seq(session_id: int | None) -> int`
- `insert_snapshot(SnapshotRow) -> int`
- `get_last_snapshot() -> Row | None`

**Enum-typed cross-table ops** (driven by dynamic-table sites above):
- `set_pinned(entity_type, entity_id, value) -> None`
- `count_by_type(entity_type, *, project=None, pinned=None) -> int`
- `list_pinned(entity_type) -> list[Row]`
- `entry_exists(entity_type, entity_id) -> bool`

**Fuzzy dedup probe** (used by `add_knowledge`):
- `find_similar_knowledge(topic) -> Row | None`

The remaining concerns split off to other interfaces:

- **`SearchBackend`** owns the four FTS surfaces (knowledge_fts,
  negative_fts, errors_fts, rules_fts) plus the LIKE fallback. Returns
  `SearchHit` dataclasses; the Python scorer composes the final rank.
- **`CounterStore`** owns `hit_count`, `reinforcement_count`,
  `last_hit_at`, and possibly `pinned`. Inline UPDATE-after-SELECT (the
  hit-counting pattern in `search.py`) becomes `CounterStore.increment()`.
- **`SessionStore`** — interface exists per OQ-5 but no non-embedded
  reference adapter ships. The embedded reference may keep using the same
  `sessions`/`snapshots` tables for now.

---

## Notes for downstream tasks

- **MCM2-02 (extract SQL into repository):** Start with the highest-fanout
  files first. Order suggested: `search.py` → `knowledge.py` → `rules.py`
  → `session.py` → `relations.py`. `search.py` is hardest because the
  composite-rank scorer also has to move out; doing it first sets the
  pattern.
- **MCM2-04 (composition root):** Tool functions today receive
  `(mcp, db, tracker, project_name, ...)`. After the refactor they
  receive `(mcp, ctx)` where `ctx` exposes
  `ctx.storage` / `ctx.counters` / `ctx.search` / `ctx.session`. The
  `tracker` stays in-process (per OQ-5).
- **MCM2-06 (config hygiene):** Audit `src/mcm_engine/config.py` for
  `**{k: v for k, v in ... if k in fields}` patterns. The known instance
  at line 130 is one of possibly several.
- **MCM2-07 (plugins):** `MCMPlugin.get_search_scopes()` returning a list
  of `SearchScope` objects that carry SQL is the contract to refactor.
  Replacement shape: a plugin declares a *table descriptor* (table name,
  searchable columns, display columns) and the engine's SearchBackend
  builds the search itself.
- **Watcher cascade (MCM2-23):** the new `rules.content_hash`,
  `rules.archived`, `rules.archived_at` columns require a v7 migration in
  `schema.py` *before* the storage interface is finalized — so the
  embedded SQLite reference and the Postgres adapter agree on the rules
  table shape.

## Addendum — session-end candidate queries (per-tool-nudge feature)

`adapters/sqlite/storage.py` gained two read-only SQL sites (count 42 → 44)
backing the session-end suggestion surface in `tools/session.py::session_handoff`:

- `list_unlinked_knowledge(limit)` — `SELECT ... FROM knowledge WHERE NOT EXISTS
  (relation referencing this id)` ordered by recency. Powers the
  `link_knowledge` suggestions.
- `list_promotable_knowledge(min_hits, limit)` — `SELECT ... FROM knowledge
  WHERE hit_count >= ?` ordered by hits. Powers the `promote_to_rule`
  suggestions.

These are deliberately NOT on the `StorageBackend` Protocol (no contract-version
bump): `session_handoff` calls them best-effort inside `try/except`, per the
"adapters declare honest capabilities, callers degrade gracefully" rule. A
backend lacking them simply surfaces no suggestions.

## Addendum — rule provenance events (issue #10)

Both storage adapters gained two SQL sites for the append-only `rule_events`
audit log (`adapters/sqlite/storage.py`: 44 → 46; `adapters/postgres/storage.py`:
45 → 47):

- `insert_rule_event(rule_id, event_type, actor, ...)` — `INSERT INTO rule_events
  (...) VALUES (...)`. Emitted from the tool layer (`add_rule` / `sync_rules` /
  `reinforce_rule` / `promote_to_rule`), so bulk paths (migrate CLI, watcher)
  that call `insert_rule` directly do not invent history.
- `list_rule_events(rule_id, limit=None)` — `SELECT ... FROM rule_events WHERE
  rule_id = ? ORDER BY at DESC`. `rule_id` is intentionally not a foreign key —
  events outlive their rule.

These ARE on the `StorageBackend` Protocol (additive methods, no
CONTRACT_VERSION bump — same precedent as the Phase 1 `iter_*` additions).


## LODESTONE additive surface (POC)

`src/mcm_engine/tokens.py` — 4 sites
- `mint_token` — `INSERT INTO tokens (token_hash, principal) VALUES (?, ?)`
- `validate_token` — `SELECT id, principal FROM tokens WHERE token_hash = ?
  AND revoked_at IS NULL`
- `validate_token` — `UPDATE tokens SET last_used_at = now() WHERE id = ?`
- `revoke_token` — `UPDATE tokens SET revoked_at = now() WHERE token_hash = ?
  AND revoked_at IS NULL`

`src/mcm_engine/transport.py` — 1 site
- `_make_claims_endpoint` — `INSERT INTO knowledge (topic, kind, summary,
  detail, tags, project, subject_keys, governance_tags, scope, status,
  provenance) VALUES (...) RETURNING id`. The `/v1/claims` REST shim the
  sieve POSTs to after the regex pass clears.

`src/mcm_engine/tools/knowledge.py` — 3 sites (LODESTONE additions only;
the rest of the file is rewired through `ctx.storage`)
- `kb_recall` — `SELECT id, topic FROM knowledge WHERE id = ?`
- `kb_recall` — `INSERT INTO recall_log (claim_id, principal, reason)
  VALUES (?, ?, ?)`
- `kb_recall` — `DELETE FROM knowledge WHERE id = ?`

These three files are the only places LODESTONE-specific SQL lives. Tokens
and `/v1/claims` are HTTP-transport concerns; `kb_recall` is an MCP tool.
The Claim-shaped columns on `knowledge` (subject_keys, governance_tags,
scope, status, provenance) are populated through normal `insert_knowledge`
machinery from the adapter; only the `/v1/claims` shim writes them directly,
since the existing `insert_knowledge` doesn't take them as parameters.

## Addendum — net-new content-hash guard (issue #54)

Both storage adapters gained one read-only SQL site
(`adapters/sqlite/storage.py`: 55 → 56; `adapters/postgres/storage.py`: 56 → 57):

- `find_rule_by_content_hash(content_hash)` — `SELECT ... FROM rules WHERE
  content_hash = ? AND NOT archived AND status='active' LIMIT 1`. The ingest
  write path (`commit_verdicts`) consults it so the same rule body under a
  different title is deduped, not minted as net-new — making
  ingest→commit→ingest idempotent.

## Addendum — rule hierarchy axes + postgres version tracking (issue #64)

Phase 1 adds three additive columns to `rules` — `importance` (ordinal
blast-radius rank), `scope` (universal/conditional), `kind` (directive/fact).
Not FTS/tsv-indexed, so no index rebuild.

- `schema.py`: 56 → 59. The v10→v11 migration `_migrate_v10_to_v11` adds the
  three columns via three guarded `ALTER TABLE rules ADD COLUMN ...`
  `execute_write` sites (CORE_VERSION bumped 10 → 11). Fresh installs get the
  columns from CORE_SCHEMA.
- `adapters/postgres/storage.py`: 57 → 58. The three columns are added to the
  `CREATE TABLE rules` DDL and to a guarded `DO $$ ... $$` block for existing
  deployments (both inside `_DDL_STATEMENTS`, no new `.execute` site). The +1
  site is a new `_mcm_versions` upsert in `ensure_schema` (issue #64 Phase 0):
  postgres now records its schema version the way sqlite's `migrate_core`
  does. The guarded DDL remains the actual migration mechanism; the stamp only
  makes the version legible.

## Addendum — hierarchy read/write surface (issue #64, Phase 2)

Both adapters gain the tuning surface the admin UI and MCP verbs sit on
(`adapters/sqlite/storage.py`: 56 → 59; `adapters/postgres/storage.py`: 58 → 61):

- `list_rules(include_archived, min_importance, limit)` — one `SELECT * FROM
  rules ... ORDER BY importance DESC, id ASC` read site. RuleRow already carries
  the hierarchy axes + derived signals, so no projection change.
- `set_rule_metadata(rule_id, importance, scope, kind, category, actor)` — two
  write sites (an `UPDATE rules SET ...` of the validated provided fields +
  updated_by/updated_at, and an `INSERT INTO rule_events` 'metadata' audit row),
  run atomically. Vocab validation happens before either write.
