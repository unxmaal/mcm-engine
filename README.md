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
  | search | SQLite FTS5 (default), Postgres tsvector, OpenSearch¹ |
  | session | In-memory (default) |

  ¹ The OpenSearch adapter is contract-correct but uses a v1 sync model
  that re-indexes from storage on every `search()` call — O(N) per query.
  Suitable for validating the contract and small datasets; **do not deploy
  to production**. The watcher-cascade-fed OpenSearch path is tracked
  separately and is not landed yet. See [Backend maturity](#backend-maturity).
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

## Making agents actually use it

Wiring the MCP server is necessary but not sufficient. A language model
left to its own devices will happily skip the knowledge layer and just
start editing files. mcm-engine ships three layers of "use the MCP first"
enforcement; pick what suits your project:

| Layer | What it is | Failure mode it catches |
|-------|------------|-------------------------|
| `CLAUDE.md` instructions | Project-root prompt the agent reads on startup | None alone — it's the soft layer. Agents follow it most of the time. |
| In-process MCP nudge (built-in) | The MCP server itself counts MCP-tool turns and emits warnings on `session_start` / tool responses. Tunable via `nudges:` in `mcm-engine.yaml`. | Agent ignores the prompt for several turns of MCP work. |
| **PreToolUse hook** (recommended) | A Python script wired into the agent harness (Claude Code / opencode / similar) that intercepts EVERY built-in tool call and counts toward a budget. | Agent routes around the MCP entirely by using only `Edit`/`Write`/`Bash`. The in-process nudge can't see those calls. |

The PreToolUse hook is the only layer with actual teeth — it can BLOCK a
tool call before it runs. The other two are advisory. If you only set up
one layer, set up the hook.

### Wiring the PreToolUse hook (Claude Code)

Add to `~/.claude/settings.json` (user-level) or your project's
`.claude/settings.local.json` (per-project):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit|Bash|mcp__.+?__(search|report_error|sync_rules|session_start|get_resume_context|read_rule)",
        "hooks": [
          {
            "type": "command",
            "command": "mcm-engine hook",
            "timeout": 2
          }
        ]
      }
    ]
  }
}
```

The `mcm-engine hook` subcommand reads a single PreToolUse event from
stdin, updates the per-session counter, and exits 0 (allow) or 2 (block).
It works under every install path — `uv tool install`, `pip install`,
editable checkout — because the `mcm-engine` binary itself is always on
PATH after install (system `python3 -m mcm_engine.hooks.mcp_enforcement`
would NOT work under `uv tool install`, which sequesters mcm-engine in
an isolated venv).

**Behavior:**
- Counts every `Edit`, `Write`, `NotebookEdit`, `Bash` call.
- At **3** built-in calls without a compliance MCP read, emits a warning. The warn is phrased as a directive ("STOP. The next action should be `search`"), not as a runway counter — agents reliably ignore "you have N calls left" framing.
- At **6** file-mutating calls (`Edit` / `Write` / `NotebookEdit` / `apply_patch`), BLOCKS the call. Bash is exempt from the block — bash-heavy work is too often legitimate — but bash calls still contribute to the warn threshold.
- A compliance MCP read on ANY server name (`search`, `report_error`, `sync_rules`, `session_start`, `get_resume_context`, `read_rule`) RESETS the counter.
- Pure-write MCP tools (`add_knowledge`, `add_rule`, `pin_item`, etc.) do NOT reset the counter — recording after the fact doesn't excuse skipping the look-first step.

The tight thresholds (3 / 6) are deliberate. Earlier values (8 / 20) gave the agent enough rope to do a full mini-task — including describing internal systems from pretrained guesswork — before any nudge fired. Three built-ins is roughly "one sub-task in"; six edits without a single look is a clear contract violation. Tune via `WARN_THRESHOLD` / `BLOCK_THRESHOLD` in `src/mcm_engine/hooks/mcp_enforcement.py` if your usage shape differs.

**State**: `<project>/.claude/mcp-enforcement-state.json`, keyed by the
agent harness's per-session UUID. Entries older than 30 days are pruned
on every hook invocation, so the file doesn't accumulate forever.
Delete the file any time to start fresh. Separate sessions have
independent counters.

### Wiring the PreToolUse hook (opencode)

opencode doesn't use Claude Code's `settings.json` `hooks` schema; it
uses a plugin system that runs JS/TS modules. mcm-engine ships an
opencode adapter that translates the plugin API to the same
`mcm-engine hook` CLI Claude Code calls, so the enforcement logic stays
identical across both harnesses.

Copy [`examples/opencode/mcp-enforcement.js`](examples/opencode/mcp-enforcement.js)
to ONE of:

- `.opencode/plugins/mcp-enforcement.js` — this project only
- `~/.config/opencode/plugins/mcp-enforcement.js` — all opencode projects

That's it. The `mcm-engine` binary must be on PATH (`uv tool install
mcm-engine` handles that); the plugin spawns it via `Bun.spawn`, pipes
the opencode tool event in as JSON, and translates a non-zero exit into
a `throw` that opencode treats as a block.

opencode-specific behavior notes:
- opencode names built-in tools lowercase (`edit`, `write`, `bash`,
  `apply_patch`). The hook recognizes these alongside Claude Code's
  capitalized names — no config needed.
- opencode names MCP tools `<server>_<tool>` (e.g.
  `mcm-engine_search`), not Claude Code's `mcp__server__tool`. The hook
  recognizes both formats.
- `apply_patch` is opencode-only and counts as a file mutation — it's
  subject to the block, same as `edit`/`write`.

### Other harnesses

Tested against Claude Code and opencode. Any harness implementing
either contract should work without changes:
- **stdin/exit-code contract** (Claude Code style): JSON event on stdin
  with `tool_name` / `session_id` / `cwd`; exit 0 = allow, 2 = block.
  Wire your harness directly at `mcm-engine hook`.
- **JS plugin contract** (opencode style): an `input` object with
  `tool` / `sessionID` / `callID` fields, throw to block. Adapt
  `examples/opencode/mcp-enforcement.js`.

For a harness with a different contract, patches welcome — the Python
script is the single source of truth, and the per-harness adapter is
~20 lines of glue.

### Project-root instructions template

Drop this in `CLAUDE.md` (Claude Code) or `AGENTS.md` (opencode) at your
project root, as the soft layer that sits above the hook. Adjust the
server name to match the key under `mcpServers` in your `.mcp.json`.
For opencode users, replace the `mcp__mcm-engine__` prefix with
`mcm-engine_` throughout (opencode's MCP tool naming convention):

```markdown
## MCP-first protocol — non-negotiable

This project uses mcm-engine via the `mcm-engine` MCP server (see
`.mcp.json`). The knowledge base contains everything you do not know
about this organization: account IDs, identities, permission sets,
team responsibilities, system topology, repo locations, conventions,
prior incidents, prior decisions. Pretrained knowledge does not cover
it. Treat the KB as authoritative and yourself as ignorant until you
have searched.

### Required before *every* substantive action

Before any of the following, you MUST call `mcp__mcm-engine__search`
with a query relevant to the topic:

1. Any `Edit` / `Write` / `NotebookEdit`.
2. Any non-trivial `Bash` — anything beyond `ls`, `cat`, `git status`,
   `pwd`, or equivalent navigation.
3. **Any factual claim about this organization's systems, projects,
   infrastructure, conventions, people, or history.** This is the
   most-missed rule. If you are about to write a sentence describing
   how something works here, search first.

If a search returns nothing relevant, say so explicitly:
*"I checked the knowledge base; nothing matched. Working from
inference — please verify."* Confidently asserting an
organization-specific fact from pretrained memory is the failure mode
this protocol exists to catch.

### Look-first tools (these RESET the enforcement counter)

- `mcp__mcm-engine__search` — first lookup for anything
- `mcp__mcm-engine__report_error` — before any fix attempt
- `mcp__mcm-engine__sync_rules` — to confirm current rule set
- `mcp__mcm-engine__session_start` — at the top of every session
- `mcp__mcm-engine__get_resume_context` — when picking up prior work
- `mcp__mcm-engine__read_rule` — when a rule file is named

### Write tools (record findings; do NOT reset the counter)

- `mcp__mcm-engine__add_rule` — immediately after a fix is confirmed
- `mcp__mcm-engine__add_knowledge` — for non-rule findings
- `mcp__mcm-engine__add_negative` — for dead ends
- `mcp__mcm-engine__session_handoff` — before ending

### The hook is a backstop

A PreToolUse hook warns after 3 built-in calls without a look-first
MCP read and blocks file mutators at 6. A warn means **you have
already violated the rule** — the correct response is to call `search`
immediately, not to keep editing until block.
```

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
- **Production scaled:** `storage=postgres, counters=redis, search=opensearch`. The shape `terraform/aws/` provisions. **Caveat:** the OpenSearch adapter is not yet production-performant — see Backend maturity below.

### Backend maturity

Not every adapter is at the same readiness level. Be honest with yourself
about what each one is for:

| Axis / Adapter | Status | Notes |
|----------------|--------|-------|
| storage / SQLite | Production | Default; battle-tested on the v1 surface. |
| storage / Postgres | Production | Conformance suite green; ID-preserving migrate covered. |
| counters / SQLite | Production | |
| counters / Postgres | Production | Same write-through semantics as SQLite. |
| counters / Redis | Production | ZSET-per-counter; `flush()` is a no-op (Redis IS the live store). Durable write-back daemon is not yet shipped — fine for ephemeral counters, not for long-term reinforcement state without periodic snapshots. |
| search / SQLite FTS5 | Production | |
| search / Postgres tsvector | Production | `ts_rank_cd` + GIN indexes. |
| search / OpenSearch | **Reference / contract validation only** | v1 sync model re-indexes from storage on every `search()` — O(N) per query, slower than the SQLite default it's nominally meant to outscale. Demonstrates the adapter contract works against an external search engine; **do not demo or deploy expecting speed**. The performant version requires a storage→OpenSearch indexer (watcher cascade extension) which is not landed yet. |
| session / In-memory | Production | Today's default. Durable session adapter is a defined extension point; no impl shipped. |

If a row says "Production," storage+counters swaps are safe today. The
search tier has two production-ready impls (SQLite FTS5, Postgres
tsvector). OpenSearch is wired and the conformance suite passes, but
it is not the path to higher search throughput in its current shape.

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
