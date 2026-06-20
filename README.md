# mcm-engine

Memory Context Management engine for AI coding sessions.

Persistent knowledge management + session handoff + behavioral nudges,
delivered as an MCP server. v2 is pluggable across four adapter axes —
the same engine runs on local SQLite, on AWS-managed services, or any
mix of the two through config alone.

## What's in v2

- **Pluggable adapters** across four axes: `storage`, `counters`, `search`,
  `session`. Swap any axis via a single line in `mcm-engine.yaml` (or one
  env var) without touching code.
- **Reference adapters shipped:**
  | Axis | Adapters |
  |------|----------|
  | storage | SQLite (default), Postgres |
  | counters | SQLite (default), Postgres, Redis |
  | search | SQLite FTS5 (default), Postgres tsvector, OpenSearch |
  | session | In-memory (default) |
- **Two transports:** `mcm-engine run` (stdio, today's Claude Code flow)
  and `mcm-engine serve` (HTTP/SSE daemon with `/healthz` + `/readyz`).
- **Files-win watcher cascade:** in daemon mode, edits to `rules/*.md`
  cascade into the storage backend within ~500ms. Files are always the
  source of truth; the DB is a cache.
- **Adapter-agnostic data migration:** `mcm-engine migrate --from sqlite://… --to postgresql://…`
  copies every row, preserves IDs, bumps destination sequences.
- **Container image:** the published Dockerfile builds a runnable
  service image with every adapter extra preinstalled.

The simple "drop-in for v1" case (embedded SQLite, stdio) still works
with zero config and no external services — see [Quick start](#quick-start)
below.

## Install

mcm-engine is not yet published to PyPI. Install from a local clone:

```bash
# Standard install (embedded SQLite only)
uv tool install /path/to/mcm-engine

# With all reference adapters
uv tool install /path/to/mcm-engine --with 'psycopg[binary]' --with redis --with opensearch-py
# (or, equivalently, `uv tool install '/path/to/mcm-engine[postgres,redis,opensearch]'`)

# Editable (picks up code changes on next spawn)
uv tool install -e /path/to/mcm-engine
```

## Quick start

```bash
cd /path/to/your-project
mcm-engine init --project myproject
mcm-engine run
```

`init` creates:
- `mcm-engine.yaml` — project configuration
- `.claude/knowledge.db` — knowledge database
- `rules/` — directory for persistent rule files

With no `backends:` block, the engine uses embedded SQLite across all
four axes. No Docker, no Postgres, no Redis, no OpenSearch. Same
behavior v1 had.

## MCP integration

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "mcm-engine",
      "args": ["run", "--project-root", "/path/to/your-project"]
    }
  }
}
```

For the HTTP/SSE daemon variant, see [Daemon mode](#daemon-mode-http--sse).

## Configuration

`mcm-engine.yaml` in your project root. Embedded-only minimal config:

```yaml
project_name: myproject
db_path: .claude/knowledge.db
rules_path: rules/
plugins: []
nudges:
  store_reminder_turns: 10
  checkpoint_turns: 25
  mandatory_stop_turns: 50
```

### Backends — the four axes

Add a `backends:` block to select non-default adapters per axis. Each
axis is independent:

```yaml
backends:
  storage: postgres            # storage axis
  counters: redis              # counters axis — independent of storage
  search: opensearch           # search axis — independent of both
  session: embedded            # session axis (in-memory)
  storage_options:
    dsn: postgresql://user:pass@host:5432/dbname
  counters_options:
    url: redis://host:6379/0
    namespace: "mcm:myproject:"
  search_options:
    url: http://host:9200
    index_prefix: "mcm-myproject-"
```

You can mix freely. Common shapes:
- **Default embedded:** no `backends:` block. SQLite all the way down.
- **Postgres-everything:** `storage=postgres, counters=postgres, search=postgres`. One DSN, no Redis, no OpenSearch.
- **Production scaled:** `storage=postgres, counters=redis, search=opensearch`. The shape `terraform/aws/` provisions.

### Env-var overrides

For container deployments where mounting a YAML is awkward, every axis
can be overridden via env var:

| Variable | Effect |
|----------|--------|
| `MCM_PROJECT_NAME` | overrides `project_name` |
| `MCM_BACKENDS_STORAGE` | overrides `backends.storage` |
| `MCM_BACKENDS_COUNTERS` | overrides `backends.counters` |
| `MCM_BACKENDS_SEARCH` | overrides `backends.search` |
| `MCM_BACKENDS_SESSION` | overrides `backends.session` |
| `MCM_POSTGRES_DSN` | populates `*_options.dsn` for any Postgres axis |
| `MCM_REDIS_URL` | populates `counters_options.url` when counters=redis |
| `MCM_OPENSEARCH_URL` | populates `search_options.url` when search=opensearch |
| `MCM_RULES_PATH` | overrides `rules_path` (colon-separated for multi-path) |

YAML always wins on explicit conflicts; env vars use `setdefault`
semantics for `*_options`.

### Shared rules across projects

`rules_path` accepts a list. The first entry is the primary directory
where new rule files are created. All entries are scanned by
`sync_rules` and indexed for search.

```yaml
rules_path:
  - rules/                                # project-specific (primary)
  - /home/you/shared-rules/bigcorp/       # shared business logic
  - /home/you/shared-rules/infra/         # shared infra patterns
```

Or via env: `export MCM_RULES_PATH="rules/:/home/you/shared-rules/bigcorp"`.

## Daemon mode (HTTP / SSE)

For long-lived deployments and the watcher cascade.

```bash
mcm-engine serve --project-root /path/to/project \
  --host 0.0.0.0 --port 8080 --transport sse
```

Endpoints:
- `GET /healthz` — liveness probe. Always returns 200 if the process is up.
- `GET /readyz` — readiness probe. Pings every wired adapter; returns
  503 with per-adapter status if any is unreachable.
- The MCP transport surface (`/sse` or `/mcp` depending on `--transport`)
  is mounted at the root.

Wire Claude Code to the daemon via SSE:

```json
{
  "mcpServers": {
    "knowledge": {
      "url": "http://127.0.0.1:8080/sse"
    }
  }
}
```

The HTTP transport unlocks the watcher cascade described below.

## Watcher cascade

In daemon mode, the engine watches `rules/*.md` and mirrors changes into
the storage backend. The contract:

- **External edit** → file written → watcher fires after a 500ms debounce
  → row updated with new content + new `content_hash`.
- **Deletion** → file unlinked → row soft-deleted (`archived=true`,
  `archived_at=now()`). Hard deletion is never exposed.
- **Recreation** → file reappears at an archived path → row unarchived
  with the new content.
- **Rename** → treated as delete-old + create-new. Slug is the row's
  identity.
- **Engine-initiated write** (`add_rule`) → file written, row inserted
  with matching `content_hash` → watcher sees the event, hash matches,
  no-op cascade.
- **Atomic-rename saves** (`sed -i`, vim, most modern editors) → the
  trailing spurious `FileDeletedEvent` is filtered by checking whether
  the file still exists on disk.

Stdio mode runs a one-shot `sync_rules` at startup instead of a live
watcher (process lifetime is too short to pay for the observer thread).

Full spec: [`docs/watcher-cascade.md`](docs/watcher-cascade.md).

## Migration between backends

```bash
# v1 SQLite → v2 SQLite (e.g., cutover prep)
mcm-engine migrate \
  --from sqlite:///path/to/v1/knowledge.db \
  --to sqlite:///path/to/v2/knowledge.db

# SQLite → Postgres (e.g., scaling up)
mcm-engine migrate \
  --from sqlite:///path/to/local.db \
  --to "postgresql://user:pass@host:5432/dbname"
```

The migrate command:
- Copies every entity table (knowledge, negative, errors, rules,
  sessions, snapshots, relations).
- Preserves all row IDs — cross-references in existing data stay valid.
- Bumps the destination's identity sequences past `MAX(id)` so
  subsequent inserts don't collide.
- Refuses a non-empty destination by default. Pass `--force` to append
  (note: this does NOT truncate; use `PostgresStorage.truncate_all()` or
  `psql TRUNCATE` first for a clean slate).

## Container image

```bash
docker build -t mcm-engine:latest .
docker run -d -p 8080:8080 \
  -e MCM_PROJECT_NAME=myproject \
  -e MCM_BACKENDS_STORAGE=postgres \
  -e MCM_POSTGRES_DSN='postgresql://user:pass@host/db' \
  mcm-engine:latest
```

The image bundles every adapter extra. Operational env vars
(`MCM_HOST`, `MCM_PORT`, `MCM_TRANSPORT`) control bind. Health check
hits `/healthz` automatically.

For AWS, the reference Terraform module (`terraform/aws/`) provisions
ECR + RDS + ElastiCache + OpenSearch + App Runner end-to-end. See
[`terraform/aws/README.md`](terraform/aws/README.md).

## Tools

The MCP tool surface is unchanged from v1. Tool names and signatures
are stable (`NG-5`).

### Knowledge management

| Tool | Purpose |
|------|---------|
| `search` | Unified search across all scopes. Accepts `scope` ("all"/"knowledge"/"negative"/"errors"/"rules"), `limit`, `project`, `include_archived`. |
| `add_knowledge` | Store findings, decisions, insights. Deduplicates by exact `topic`+`kind`; warns on fuzzy match. |
| `add_negative` | Store anti-patterns and dead ends. |
| `report_error` | Log error + auto-search for matching fixes (quality-gated). |

### Rules

| Tool | Purpose |
|------|---------|
| `add_rule` | Create / index a rule file in `rules/`. Populates `content_hash` for watcher dedup. |
| `read_rule` | Read a rule file. Increments `hit_count`. |
| `promote_to_rule` | Promote a DB entry (knowledge/negative/error) to a persistent rule file. |
| `sync_rules` | Re-index every `.md` under `rules_path`. Soft-deletes orphans; restores reappeared files. |
| `reinforce_rule` | Bump a rule's `reinforcement_count`. |

### Relationships

| Tool | Purpose |
|------|---------|
| `link_knowledge` | Typed edges: `fixes`, `causes`, `supersedes`, `contradicts`, `related`. |
| `get_related` | Show all incoming + outgoing edges for an entry. |

### Session management

| Tool | Purpose |
|------|---------|
| `session_start` | Init session. Returns counts, last handoff, pinned items. |
| `session_handoff` | Snapshot state for the next session. Resets nudge counters. |
| `session_summary` | Current session statistics. |
| `save_snapshot` | Numbered mid-session checkpoint. |
| `get_resume_context` | Structured resume payload. |
| `pin_item` / `unpin_item` | Mark an entry "always loaded; never stale." |

## Architecture

Two-layer model:
- **Rule files** (`rules/*.md`) — authoritative, human-readable, version-controlled.
- **Storage backend** — fast lookup cache + agent memory. SQLite or Postgres.

If the DB and files disagree, files win (`docs/watcher-cascade.md`).

Search ranking is a composite of lexical relevance (FTS5 / `ts_rank_cd` /
OpenSearch BM25 — normalized higher-better at the adapter boundary),
hit frequency, reinforcement, and recency. Constants live in
[`src/mcm_engine/scoring.py`](src/mcm_engine/scoring.py).

### Conformance suite

Every adapter passes the same suite, lifted as a library:

```python
# In your third-party adapter's tests/
from mcm_engine.testing.conformance import StorageConformance

class TestMyAdapter(StorageConformance):
    @pytest.fixture
    def storage(self):
        return MyStorage(...)
```

The same applies for `CounterConformance`, `SearchConformance`, and
`SessionConformance`. Pass the suite, you've implemented the contract.

## Plugins

Extend with domain-specific knowledge:

```python
from mcm_engine import MCMPlugin, SearchScope

class MyPlugin(MCMPlugin):
    name = "my-plugin"

    def get_schema_sql(self):
        return "CREATE TABLE IF NOT EXISTS my_data (...)"

    def register_tools(self, server):
        # Use server.ctx for engine-managed entities (storage/counters/search)
        # Use server.db for raw SQL against your own plugin tables.
        @server.mcp.tool()
        def my_tool(): ...

    def get_search_scopes(self):
        # SearchScope is a passive table descriptor in v2 — the engine's
        # SearchBackend interprets it, the plugin no longer carries SQL.
        return [SearchScope(name="my_data", ...)]
```

Register via entry points or config:

```yaml
plugins:
  - my-plugin          # entry point
  - mymodule:MyPlugin  # direct import
```

Note: plugins assume embedded SQLite for their own tables (the
`get_schema_sql()` path runs against `server.db`). Plugin-on-Postgres
isn't supported in v2.

## Documentation

| File | Topic |
|------|-------|
| `docs/watcher-cascade.md` | Files-win conflict resolution, debounce semantics, atomic-rename handling |
| `docs/counter-flush-policy.md` | CounterStore staleness window, write-through vs batched |
| `docs/capabilities.md` | Adapter capability flags + honest degradation |
| `docs/ranking-equivalence.md` | Why BM25 ≠ ts_rank_cd is OK |
| `docs/schema-migration-v6-to-postgres.md` | SQLite-to-Postgres DDL mapping |
| `docs/contract-versioning.md` | When to bump `CONTRACT_VERSION` |
| `docs/seam-inventory.md` | Every SQL site in the engine, by file |
| `terraform/aws/README.md` | Reference AWS deployment (Postgres + Redis + OpenSearch + App Runner) |
| `mcmv2_test_plan.md` | Cutover validation test plan |
