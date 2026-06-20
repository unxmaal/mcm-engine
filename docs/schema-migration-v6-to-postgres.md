# Schema migration: SQLite v6 → Postgres

This document maps the SQLite v6 schema (the live shape today, defined in
`src/mcm_engine/schema.py`) to its Postgres equivalent. The
`mcm-engine migrate --from sqlite://path --to postgres://dsn` tool
implements what this document describes; this document explains the why
behind each translation so the tool's behavior can be audited without
reading SQL.

Postgres target version: **15 or newer**. We rely on
generated columns, GIN indexes on `tsvector`, and `text` search with
`english` config (which uses Snowball Porter stemming — equivalent to
SQLite FTS5's `porter unicode61` for our purposes).

## High-level shape

| SQLite v6                          | Postgres                                |
|------------------------------------|-----------------------------------------|
| 6 data tables                      | 6 data tables (same names)              |
| 4 FTS5 virtual tables              | **0** separate FTS tables                |
| 12 FTS-sync triggers               | **0** FTS-sync triggers                  |
| `tsvector` generated columns       | (n/a)                                    |
| `_mcm_versions` (schema tracking)  | `_mcm_versions` (same purpose, kept)    |

Key shift: SQLite v6 keeps lexical search state in *separate* virtual tables
(`knowledge_fts`, etc.) kept in sync via triggers. Postgres folds the
search index into the same table via **generated `tsvector` columns + GIN
indexes** — no separate tables, no triggers, no insert-time race.

## Type mapping

| SQLite type / pattern                          | Postgres equivalent                          |
|------------------------------------------------|----------------------------------------------|
| `INTEGER PRIMARY KEY`                          | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `INTEGER` (a counter, default 0)               | `INTEGER NOT NULL DEFAULT 0`                 |
| `INTEGER` (boolean, 0/1)                       | `BOOLEAN NOT NULL DEFAULT FALSE`             |
| `TEXT NOT NULL`                                | `TEXT NOT NULL`                              |
| `TEXT` (nullable)                              | `TEXT`                                       |
| `TEXT DEFAULT (datetime('now'))`               | `TIMESTAMPTZ NOT NULL DEFAULT now()`         |
| `TEXT` storing comma-separated tags            | `TEXT` (keep as-is for v1 — see "Tags" below)|

**Booleans:** SQLite stores `pinned` as `INTEGER DEFAULT 0`. The migration
converts to `BOOLEAN`. The adapter is responsible for the `int ↔ bool`
mapping at its boundary; the engine surface stays `bool` either way.

**Timestamps:** SQLite stores `datetime('now')` as ISO-8601 text in UTC.
Postgres uses `TIMESTAMPTZ` (with timezone) defaulted to `now()`. The
migration tool parses the SQLite text via `datetime.fromisoformat`,
treats it as UTC, and inserts as `TIMESTAMPTZ`.

**Tags:** SQLite stores `tags` as comma-separated TEXT (e.g.,
`"architecture,decision,postgres"`). Tempting to normalize to a Postgres
`TEXT[]` array column for query power. **We don't, in v1.** The migration
keeps `tags` as `TEXT` for byte-equivalent round-trip. A later schema
version can introduce an array column or a join table; v1 stays cheap.

## Table-by-table mapping

### `knowledge`

```sql
-- Postgres
CREATE TABLE knowledge (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic                TEXT NOT NULL,
    kind                 TEXT NOT NULL DEFAULT 'finding',
    summary              TEXT NOT NULL,
    detail               TEXT,
    tags                 TEXT,
    project              TEXT,
    rationale            TEXT,
    alternatives         TEXT,
    hit_count            INTEGER NOT NULL DEFAULT 0,
    last_hit_at          TIMESTAMPTZ,
    reinforcement_count  INTEGER NOT NULL DEFAULT 0,
    pinned               BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Generated full-text vector
    tsv  tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(topic, '')),   'A') ||
        setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(detail, '')),  'C') ||
        setweight(to_tsvector('english', coalesce(tags, '')),    'D') ||
        setweight(to_tsvector('english', coalesce(kind, '')),    'D')
    ) STORED
);

CREATE INDEX idx_knowledge_tsv      ON knowledge USING GIN (tsv);
CREATE INDEX idx_knowledge_project  ON knowledge (project) WHERE project IS NOT NULL;
CREATE INDEX idx_knowledge_pinned   ON knowledge (pinned) WHERE pinned;
CREATE INDEX idx_knowledge_last_hit ON knowledge (last_hit_at DESC NULLS LAST);
```

**Weighting** (`A` > `B` > `C` > `D`) is the Postgres analogue of the
composite ranking — `topic` matches outrank `tags` matches. The actual
score still goes through the Python scorer (MCM2-14) that composes
`ts_rank_cd` with `hit_count`, `reinforcement_count`, `pinned`, and
recency.

Migration step for the `knowledge` table:
1. Read all rows from SQLite.
2. Insert into Postgres preserving `id` (the migration tool overrides the
   `GENERATED` default; see "Preserving IDs," below).
3. The `tsv` generated column populates automatically — no separate FTS
   sync step needed.

### `negative_knowledge`

```sql
CREATE TABLE negative_knowledge (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category          TEXT NOT NULL,
    what_failed       TEXT NOT NULL,
    why_failed        TEXT,
    correct_approach  TEXT,
    severity          TEXT NOT NULL DEFAULT 'normal',
    project           TEXT,
    pinned            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    tsv  tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(category, '')),         'A') ||
        setweight(to_tsvector('english', coalesce(what_failed, '')),      'B') ||
        setweight(to_tsvector('english', coalesce(why_failed, '')),       'C') ||
        setweight(to_tsvector('english', coalesce(correct_approach, '')), 'C')
    ) STORED
);

CREATE INDEX idx_negative_tsv     ON negative_knowledge USING GIN (tsv);
CREATE INDEX idx_negative_project ON negative_knowledge (project) WHERE project IS NOT NULL;
CREATE INDEX idx_negative_pinned  ON negative_knowledge (pinned) WHERE pinned;
```

Note: no `hit_count` / `reinforcement_count` / `last_hit_at` here — matches
v6 SQLite. Negative knowledge is pin-or-not, not ranked by frequency.

### `errors`

```sql
CREATE TABLE errors (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pattern     TEXT NOT NULL,
    context     TEXT,
    root_cause  TEXT,
    fix         TEXT,
    tags        TEXT,
    project     TEXT,
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    tsv  tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(pattern, '')),    'A') ||
        setweight(to_tsvector('english', coalesce(root_cause, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(fix, '')),        'B') ||
        setweight(to_tsvector('english', coalesce(context, '')),    'C') ||
        setweight(to_tsvector('english', coalesce(tags, '')),       'D')
    ) STORED
);

CREATE INDEX idx_errors_tsv     ON errors USING GIN (tsv);
CREATE INDEX idx_errors_project ON errors (project) WHERE project IS NOT NULL;
CREATE INDEX idx_errors_pinned  ON errors (pinned) WHERE pinned;
```

### `sessions`

```sql
CREATE TABLE sessions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status            TEXT NOT NULL,
    current_task      TEXT,
    findings_summary  TEXT,
    next_steps        TEXT,
    blockers          TEXT,
    context_snapshot  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

No FTS on `sessions` in v6, so no `tsv` column here. Sessions are queried
by id and recency, not by content search.

### `rules`

```sql
CREATE TABLE rules (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title                TEXT NOT NULL,
    keywords             TEXT NOT NULL,
    file_path            TEXT,
    description          TEXT,
    category             TEXT,
    hit_count            INTEGER NOT NULL DEFAULT 0,
    last_hit_at          TIMESTAMPTZ,
    reinforcement_count  INTEGER NOT NULL DEFAULT 0,
    pinned               BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Watcher cascade columns (new in this migration; see watcher-cascade.md)
    content_hash         TEXT,
    archived_at          TIMESTAMPTZ,
    archived             BOOLEAN NOT NULL DEFAULT FALSE,

    tsv  tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')),       'A') ||
        setweight(to_tsvector('english', coalesce(keywords, '')),    'B') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'C') ||
        setweight(to_tsvector('english', coalesce(category, '')),    'D')
    ) STORED
);

CREATE INDEX idx_rules_tsv         ON rules USING GIN (tsv);
CREATE UNIQUE INDEX idx_rules_path ON rules (file_path) WHERE file_path IS NOT NULL;
CREATE INDEX idx_rules_archived    ON rules (archived);
```

**Three columns are new in the v6 → Postgres jump**:
- `content_hash` — populated by the watcher (and by engine-initiated
  writes) so the cascade can short-circuit no-op events. See
  `watcher-cascade.md`.
- `archived_at` / `archived` — supports the watcher's soft-delete on file
  removal. Also queryable by humans via "show archived rules" diagnostics.

These three columns are **also added to the SQLite v6 schema as part of
this refactor** (a new v7 migration in `schema.py`) so the embedded
reference and the Postgres adapter present the same shape. The
`mcm-engine migrate` tool reads from a v7-or-later SQLite source and
writes into the Postgres shape above.

### `relations`

```sql
CREATE TABLE relations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_type  TEXT NOT NULL CHECK (source_type IN ('knowledge','error','rule','negative')),
    source_id    BIGINT NOT NULL,
    target_type  TEXT NOT NULL CHECK (target_type IN ('knowledge','error','rule','negative')),
    target_id    BIGINT NOT NULL,
    relation     TEXT NOT NULL CHECK (relation IN ('fixes','causes','supersedes','contradicts','related')),
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, target_type, target_id, relation)
);

CREATE INDEX idx_relations_source ON relations (source_type, source_id);
CREATE INDEX idx_relations_target ON relations (target_type, target_id);
```

The SQLite v6 schema enforces `source_type`/`target_type`/`relation`
membership in Python (in `tools/relations.py`). Postgres can carry the
constraint at the database boundary via `CHECK`. Adapters that prefer
to keep the check in Python (e.g., if the engine wants to evolve the
sets without a migration) may drop the CHECK clauses; this is a permitted
adapter-level variation.

### `snapshots`

```sql
CREATE TABLE snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      BIGINT REFERENCES sessions(id),
    sequence_num    INTEGER NOT NULL,
    goal            TEXT,
    progress        TEXT,
    open_questions  TEXT,
    blockers        TEXT,
    next_steps      TEXT,
    active_files    TEXT,
    key_decisions   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_snapshots_session ON snapshots (session_id, sequence_num);
```

### `_mcm_versions`

```sql
CREATE TABLE _mcm_versions (
    component   TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Same shape, same purpose: tracks the schema version of the core engine
and each plugin separately. The migration tool seeds this with the
target Postgres schema version after data is moved.

## What does NOT cross over

| SQLite v6 element              | Why it's dropped                          |
|--------------------------------|-------------------------------------------|
| `knowledge_fts`, `negative_fts`, `errors_fts`, `rules_fts` virtual tables | Replaced by `tsvector` generated columns. |
| Triggers `knowledge_ai/ad/au` and siblings | Generated columns auto-update; no triggers needed. |
| `tokenize='porter unicode61'`  | Postgres `english` config provides Snowball Porter + Unicode handling. |

These are not lost; they're folded into the table definitions in a
Postgres-native way. A future migration that wanted to use Postgres
`pg_trgm` for prefix matching or a different language config can ALTER
the generated column expression.

## Preserving IDs

Live mini installs have data that the user (or other clients) may
reference by id. The migration tool **preserves SQLite ids in Postgres**:

```sql
-- Per-table after creating the schema:
ALTER TABLE knowledge ALTER COLUMN id DROP IDENTITY;
ALTER TABLE knowledge ADD COLUMN id_new BIGINT GENERATED ALWAYS AS IDENTITY;
-- migrate data with explicit id
-- then re-attach identity at MAX(id) + 1
SELECT setval(pg_get_serial_sequence('knowledge', 'id'),
              (SELECT max(id) FROM knowledge));
```

Simpler approach the migration tool uses: create tables with
`BIGINT PRIMARY KEY` (no `IDENTITY`), insert all rows with explicit ids,
then attach an identity sequence at the end:

```sql
CREATE TABLE knowledge (
    id BIGINT PRIMARY KEY,
    -- other columns
);
-- INSERT data with explicit ids
-- Then:
CREATE SEQUENCE knowledge_id_seq AS BIGINT OWNED BY knowledge.id;
ALTER TABLE knowledge ALTER COLUMN id SET DEFAULT nextval('knowledge_id_seq');
SELECT setval('knowledge_id_seq', (SELECT max(id) FROM knowledge));
```

Either pattern works; the tool implements the second because it's a
clearer "set up state, fill state, lock state" sequence.

## Migration tool flow

The CLI is `mcm-engine migrate --from sqlite://path --to <adapter>://dsn`.

The tool is **adapter-agnostic** for the destination: it writes via the
destination's `StorageBackend` interface. The Postgres target gets the
schema above before the tool starts because adapter `__init__` is
responsible for schema setup (idempotent on each adapter — see
`StorageBackend.ensure_schema()` in the contract).

Read side:
1. Open the source SQLite DB at the path.
2. Confirm `_mcm_versions.version` for `core` ≥ 7 (the migration tool
   requires the watcher-column migration to have been applied locally
   first).
3. Stream rows from each data table in dependency order: `sessions`,
   `knowledge`, `negative_knowledge`, `errors`, `rules`, `relations`,
   `snapshots`.

Write side:
4. For each row, call `dst.bulk_insert(table, row)` on the destination
   adapter.
5. After all tables, call `dst.reseat_identity_sequences()`
   (adapter-specific; Postgres reseats sequences as shown above, SQLite
   does nothing).
6. Mark the destination's `_mcm_versions` row for `core` at the
   target version.

Verification:
7. Compare `SELECT count(*)` per table between source and destination.
   Mismatch → loud error, leave destination intact for inspection.
8. Run the adapter's conformance suite against the loaded destination
   (`mcm-engine migrate --verify` re-runs without copying).
9. Spot-check: pick 5 random rows from `knowledge`, fetch them from both
   sides by id, assert content equality field-by-field.

Idempotency:
- The tool refuses to migrate into a non-empty destination by default.
- `--force` allows overwrite, deleting the destination's contents first.
- Re-running with `--resume` continues from the last completed table
  (useful for migrating large stores when the source is on a slow disk).

## When this document changes

This document is **frozen to the SQLite v6 (and the v7 watcher-cascade
addition) → Postgres mapping**. When the engine schema evolves (v8, v9,
…), each new core migration adds a section here describing the
Postgres-side equivalent. Adapters in other dialects (e.g., a future
DuckDB adapter) follow this document as the canonical reference for "what
shape do the engine's tables take in a non-SQLite store."
