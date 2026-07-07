# mcm-engine

**A durable, external, cross-session memory service for AI coding agents, served over MCP.**

Coding agents forget. A long session hits its context limit, compacts, and the model
loses what it learned earlier in the session. It then re-derives the same fix, re-reads
the same files, and rewrites a helper it already wrote. mcm-engine stores what an agent
learns so it is not lost: findings, rules, errors, and decisions are captured when they
are confirmed and retrieved before they are re-derived. It is the agent's long-term
memory, and it outlives the context window.

mcm-engine speaks the Model Context Protocol, so any MCP client (Claude Code, opencode,
and others) can use it over stdio or HTTP. It runs on embedded SQLite with no external
services, or scales to Postgres, Redis, or OpenSearch through configuration alone.

> Changes and version history are in [`CHANGELOG.md`](CHANGELOG.md).

---

## Why mcm-engine, and how it differs from a vector-DB memory server

Most memory-for-agents MCP servers are a vector database plus an LLM extraction step.
mcm-engine makes different design choices, tuned for coding agents.

- **Deterministic, embedding-free retrieval.** Search is exact keyword and full-text
  (SQLite FTS5, Postgres `tsvector`), not vector similarity. The same query returns the
  same results every time. For identifier-heavy code queries (function names, error
  strings, flags), BM25-style keyword search beats embeddings. Tradeoff: it will not
  recall a paraphrase that shares no keywords. The mitigations are querying with real
  identifiers and a spreading-activation pass over linked rules (see Features).
- **It enforces use.** A memory that is never consulted has no effect. Most memory
  servers do nothing to make the agent look. mcm-engine ships a PreToolUse hook and
  in-process nudges that make "search before you act" a tracked, visible contract.
- **Confidence from outcomes, not popularity.** Rules carry a correctness signal moved by
  `report_outcome` (whether acting on the rule worked), separate from how often it is
  read. An author≠judge guard stops a rule's own author from self-certifying it.
- **Truth decays without destruction.** `supersede_rule` soft-expires a rule when a newer
  one replaces it (`valid_until`, `superseded_by`, `status`). Nothing is hard-deleted, and
  history stays inspectable.
- **Reviewable.** Rules are authoritative Markdown files, or a source-of-authority
  database. A one-way `export-mirror` renders the DB to a git repo, giving `git blame`,
  diff, and PR review over what agents have learned.
- **Built for coding agents rather than chat.** Rules-as-instructions, error-to-fix
  recall, session handoff across compaction, project-scoped knowledge.
- **Multi-backend and pluggable.** Start on SQLite. Swap any of four axes to Postgres,
  Redis, or OpenSearch with one config line. Extend with plugins that pass the same
  conformance suite.

---

## Features

**Knowledge model**
- **Rules, knowledge, errors, negatives.** Four first-class entry types. Rules are
  persistent instructions; knowledge is findings and decisions; errors log failures and
  auto-recall matching fixes; negatives record dead ends so they are not repeated.
- **Typed relationships.** `link_knowledge` builds `fixes`, `causes`, `supersedes`,
  `contradicts`, and `related` edges; `get_related` traverses them.

**Retrieval**
- **Deterministic FTS search.** One `search` tool across all scopes, ranked by a composite
  of lexical relevance, hit frequency, reinforcement, correctness, recency, and rule
  importance/scope. Relevance is batch-min-max normalized, so ranking behaves identically
  on SQLite bm25 and Postgres `ts_rank_cd`, and is weighted above the other signals so a
  strong match is never out-voted.
- **Spreading activation.** A search hit also surfaces its one-hop linked neighbors
  (`[related]`), so a rule connected to a match appears even when the query missed its
  keywords. Value scales with how many links exist.

**Trust and truth maintenance**
- **Correctness axis.** `report_outcome(rule_ids, passed)` records whether acting on a rule
  worked. An author≠judge guard makes self-reports advisory-only. Correctness is folded
  into ranking (demote, not ban).
- **Graded trust.** An optional `actor→weight` map (`MCM_TRUST_WEIGHTS`,
  `MCM_TRUST_DEFAULT`) weights outcomes by who reported them, applied at rank time
  (late-binding, so retuning re-weights history).
- **Supersession.** `supersede_rule(old, new)` soft-expires the old rule; superseded rules
  drop out of default search but remain for audit.

**Rule hierarchy** (rules are not a flat pile)
- **Three axes** on every rule: `importance` (ordinal 0–2: reference / default /
  invariant), `scope` (`universal` vs `conditional`), and `kind` (`directive` vs `fact`) —
  orthogonal to the correctness/lifecycle axis. Tune them with `set_rule_metadata` or the
  admin UI; `list_rules` returns them all, importance-first.
- **The hierarchy drives behavior.** The invariant tier is injected into every
  `session_start` (in front of the agent, not waiting to be recalled); importance/scope
  lift a rule in search ranking; and `find_conflicting_rules` uses importance as the
  tiebreak — the higher tier is named the keeper, the lower yields.

**KB hygiene** (deterministic, read-only, surfacing-only; nothing auto-mutates)
- **`find_duplicate_rules`.** MinHash/LSH near-duplicate detection.
- **`find_conflicting_rules`.** Topic-similar but body-divergent pairs (same subject,
  opposite story), labeled `contradictory`, `subsumes`, or `subsumed`, with the
  higher-importance rule named as the keeper.
- **`consolidation_report` and the `consolidate` CLI.** One report combining merge
  candidates, conflict candidates, and stale rules. Suitable for a nightly job.

**Making agents use it**
- **Fail-open PreToolUse hook.** Counts built-in tool calls, warns when the agent edits
  without a look-first MCP read, and records a `consultation_gap` event at the threshold.
  It never blocks (fail-open), so it cannot dead-lock the agent when the backend is
  unreachable.
- **In-process nudges.** The server nudges after N tool-turns without a store or a look;
  tunable in `mcm-engine.yaml`.
- **Opt-in ambient recall** (`MCM_AMBIENT_RECALL`). Best-effort: the hook surfaces a
  relevant rule based on what is being edited. Never blocks, tight timeout, rate-limited.

**Safety**
- **Poisoning defense.** Stored rule content is delimited as untrusted data at read time,
  not executed as instructions. `add_rule` flags injection markers ("ignore previous
  instructions", and similar) without rejecting.

**Operations**
- **Admin tuning UI.** `mcm-engine admin` serves a small co-located web app: an editable
  rules grid (tune importance/scope/kind/category, with realtime colorize as the KB
  changes) plus a node-graph structure view (rules colored by importance, clustered by
  category, edges from relations). Reads go direct; writes go through the audited
  `set_rule_metadata` path. No external dependencies (stdlib server, self-contained page).
- **DB→git review mirror.** `export-mirror` renders active rules to a git repo (one-way,
  read-only) for diffable review.
- **Source-of-authority axis.** `source_of_truth: files` (Markdown files win; the DB is a
  cache) or `database` (the DB is authoritative; for fleet and multi-client pods).
- **Multi-backend.** Four independent axes (`storage`, `counters`, `search`, `session`)
  over SQLite, Postgres, Redis, or OpenSearch.
- **Bulk I/O.** `import_rules` (payload to DB), `sync_rules` (Markdown tree to and from
  the DB), `ingest` (import from external corpora).
- **Token ledger.** Estimates tokens saved by recall against tokens spent on stores; the
  net is shown in `session_start`.
- **Pluggable, with a conformance suite.** Third-party adapters and plugins that pass the
  shared conformance tests are contract-correct.

---

## Install

Requires Python **≥ 3.11**. Not yet on PyPI; install from a clone.

### As a local tool (stdio, the Claude Code / opencode spawn flow)

```bash
# Embedded SQLite only, zero external services
uv tool install /path/to/mcm-engine

# With the scaling adapters
uv tool install '/path/to/mcm-engine[postgres,redis,opensearch]'

# Editable: picks up code changes on next spawn
uv tool install -e /path/to/mcm-engine
```

This puts `mcm-engine` on your PATH (used both as the MCP server and the `hook` /
`session-start` CLIs). Then, in your project's `.mcp.json`:

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

Initialize a project:

```bash
cd /path/to/your-project
mcm-engine init --project myproject   # writes mcm-engine.yaml, .claude/knowledge.db, rules/
```

### As an HTTP/SSE daemon (long-lived, shared, enables the watcher)

```bash
mcm-engine serve --project-root /path/to/project \
  --host 0.0.0.0 --port 8080 --transport streamable-http \
  --allowed-host 192.168.8.88          # allow the LAN address clients use
```

- `--transport` is `sse` (endpoint `/sse`) or `streamable-http` (endpoint `/mcp`).
- Reachable over a LAN: name each host clients connect by, via `--allowed-host`
  (repeatable) or `MCM_ALLOWED_HOSTS`. The MCP DNS-rebinding guard otherwise rejects
  unknown `Host` headers with `421`. Disable the guard on a trusted network with
  `--no-dns-rebinding-protection`.
- `GET /healthz` (liveness) and `GET /readyz` (per-adapter readiness) are always mounted.
- Optional bearer-token auth: `MCM_AUTH_REQUIRED=true` (mint tokens with `mcm-engine mint-token`).

Point a client at it:

```json
{ "mcpServers": { "knowledge": { "url": "http://192.168.8.88:8080/mcp" } } }
```

### As a container

```bash
docker build -t mcm-engine:latest .
docker run -d -p 8080:8080 \
  -e MCM_PROJECT_NAME=myproject \
  -e MCM_TRANSPORT=streamable-http \
  -e MCM_ALLOWED_HOSTS=192.168.8.88 \
  -e MCM_BACKENDS_STORAGE=postgres \
  -e MCM_POSTGRES_DSN='postgresql://user:pass@host/db' \
  -e MCM_SOURCE_OF_TRUTH=database \
  mcm-engine:latest
```

The image bundles every adapter extra. `MCM_HOST` / `MCM_PORT` / `MCM_TRANSPORT` control
the bind; `MCM_ALLOWED_HOSTS` is required for non-loopback access, because a container
cannot auto-detect its published address. The reference `terraform/aws/` module provisions
ECR, RDS, ElastiCache, OpenSearch, and App Runner.

### With Docker Compose

[`examples/docker-compose.yml`](examples/docker-compose.yml) brings up Postgres and the
daemon in the database-authoritative posture. Copy `examples/.env.example` to
`examples/.env`, set `POSTGRES_PASSWORD` and `ALLOWED_HOSTS`, then:

```bash
docker compose -f examples/docker-compose.yml --env-file examples/.env up -d
```

### On Kubernetes (Helm)

[`deploy/helm/mcm-engine/`](deploy/helm/mcm-engine/) deploys the daemon with a bundled
Postgres (a first-party StatefulSet plus a PVC), or an external database via
`postgresql.enabled=false`. The service is `ClusterIP` with an optional Ingress.

```bash
helm install mcm deploy/helm/mcm-engine \
  --set postgresql.auth.password=<pick-a-password> \
  --set mcm.allowedHosts={mcm-engine.example.com}
```

Values are documented in [`deploy/helm/mcm-engine/README.md`](deploy/helm/mcm-engine/README.md).

---

## Usage

### The MCP tool surface

27 tools. Names and signatures are stable.

**Search.** `search(query, scope="all"|"knowledge"|"negative"|"errors"|"rules", limit, project, include_archived)`

**Knowledge.** `add_knowledge` (findings/decisions; dedups on topic+kind) · `add_negative`
(anti-patterns) · `report_error` (log + auto-recall matching fixes) · `reinforce_knowledge`
(bump confidence) · `kb_recall` (structured recall)

**Rules.** `add_rule` (create/index a rule; flags injection markers) · `read_rule` ·
`reinforce_rule` · `promote_to_rule` (DB entry → persistent rule) · `import_rules`
(bulk payload) · `sync_rules` (re-index the Markdown tree) · `restore_rule` (un-archive) ·
`report_outcome` (correctness; author≠judge) · `supersede_rule` (soft-expire old→new) ·
`find_duplicate_rules` · `find_conflicting_rules`

**Relationships.** `link_knowledge` (typed edges) · `get_related`

**Session and hygiene.** `session_start` (context + last handoff + token-ledger net) ·
`session_handoff` (snapshot for next session) · `session_summary` · `save_snapshot`
(mid-session checkpoint) · `get_resume_context` · `consolidation_report`

**Pinning.** `pin_item` / `unpin_item` (always loaded, never stale)

**KB-hygiene workflow.** The detectors surface, a human or agent decides, nothing
auto-acts:

```
find_duplicate_rules / find_conflicting_rules   →   review   →   supersede_rule(old, new)
```

### CLI subcommands

| Command | What it does |
|---------|--------------|
| `mcm-engine run` | Run the MCP server over **stdio** (the spawn flow). |
| `mcm-engine serve` | Run the **HTTP/SSE** daemon (`--host/--port/--transport/--allowed-host`). |
| `mcm-engine admin` | Serve the **admin tuning UI** — editable rules grid + structure graph (`--host/--port`). |
| `mcm-engine init --project NAME` | Scaffold `mcm-engine.yaml`, `.claude/knowledge.db`, `rules/`. |
| `mcm-engine hook` | The PreToolUse enforcement hook (reads one event on stdin). |
| `mcm-engine session-start` | The SessionStart hook (prints resume context as `additionalContext`). |
| `mcm-engine migrate --from DSN --to DSN` | Copy every row between backends, IDs preserved. |
| `mcm-engine ingest SOURCE` | Import from an external corpus (e.g. a Markdown vault). |
| `mcm-engine ingest SOURCE --remote` | Sift a **local** codebase against a **remote** KB over MCP — spans (not files) go to `sift_candidates`; no direct DB access. Add `--remote-loose` to also surface descriptive facts (architecture, "X does Y") that carry no normative marker. |
| `mcm-engine export-mirror --from DSN --out DIR` | One-way DB→git review mirror of active rules. |
| `mcm-engine consolidate --from DSN [--max-age-days N]` | Print the KB-hygiene report (cron-friendly). |
| `mcm-engine mint-token --principal NAME` | Mint a bearer token (Postgres storage; HTTP auth). |

**Nightly hygiene and audit (cron / k8s CronJob):**

```bash
mcm-engine consolidate    --from "$DSN"                 # merge/conflict/stale candidates
mcm-engine export-mirror  --from "$DSN" --out /srv/kb-mirror   # git-diffable snapshot
```

### Making agents use it

Wiring the server is not enough on its own. A model will skip the memory and edit files
directly unless something makes it look. Three layers, weakest to strongest:

1. **`CLAUDE.md` / `AGENTS.md` instructions** (soft): a project-root prompt (template below).
2. **In-process nudges** (advisory): the server counts tool-turns and nudges; tune via
   `nudges:` in `mcm-engine.yaml`.
3. **PreToolUse hook** (recommended): sees the built-in `Edit`/`Write`/`Bash` calls the
   in-process nudge cannot, and records a `consultation_gap` when the agent edits without
   looking first.

#### Wire the hook: Claude Code

Add to `~/.claude/settings.json` or a project's `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit|Bash|mcp__.+?__(search|report_error|sync_rules|session_start|get_resume_context|read_rule)",
        "hooks": [{ "type": "command", "command": "mcm-engine hook", "timeout": 2 }]
      }
    ]
  }
}
```

**Behavior:**
- Counts every `Edit` / `Write` / `NotebookEdit` / `Bash`.
- Warns after `WARN_THRESHOLD` built-in calls without a look-first MCP read.
- At `BLOCK_THRESHOLD` file-mutating calls it records a `consultation_gap` event to
  `<project>/.claude/mcp-enforcement-events.jsonl` and always allows the edit (fail-open,
  exit 0). It never blocks, so it cannot dead-lock the agent when the KB backend is down.
- A compliance read on any of these tool names (`search`, `report_error`, `sync_rules`,
  `session_start`, `get_resume_context`, `read_rule`) resets the counter. Pure-write tools
  such as `add_rule` do not.
- State: `<project>/.claude/mcp-enforcement-state.json`, keyed by session UUID, pruned
  after 30 days. Thresholds are constants in `src/mcm_engine/hooks/mcp_enforcement.py`.

#### Wire the hook: opencode

opencode uses a JS plugin, not `settings.json`. Copy
[`examples/opencode/mcp-enforcement.js`](examples/opencode/mcp-enforcement.js) to
`.opencode/plugins/` (this project) or `~/.config/opencode/plugins/` (all projects). It
shells out to the same `mcm-engine hook` and recognizes opencode's lowercase tool names
(`edit`, `write`, `bash`, `apply_patch`) and `<server>_<tool>` MCP naming.

#### Project-root instructions template

Drop into `CLAUDE.md` / `AGENTS.md` (swap `mcp__mcm-engine__` → `mcm-engine_` for opencode):

```markdown
## MCP-first protocol (non-negotiable)
This project uses the `mcm-engine` MCP server. The knowledge base is authoritative for
everything not in your pretrained weights: conventions, systems, decisions, incidents,
people. Treat yourself as ignorant until you have searched.

Before any Edit/Write, any non-trivial Bash, or any factual claim about this
organization's systems, call `mcp__mcm-engine__search` first. If nothing matches, say so
explicitly and work from inference. Immediately after a fix is confirmed, call `add_rule`.
Call `session_start` at the top of a session and `session_handoff` before ending.
```

### Configuration

`mcm-engine.yaml` in the project root. Minimal (embedded SQLite everywhere):

```yaml
project_name: myproject
db_path: .claude/knowledge.db
rules_path: rules/
source_of_truth: files      # or 'database' (DB authoritative; for pods with no rules tree)
plugins: []
nudges:
  store_reminder_turns: 10
  checkpoint_turns: 25
  mandatory_stop_turns: 50
```

**Backends: four independent axes.** Add a `backends:` block to swap any axis:

```yaml
backends:
  storage: postgres
  counters: redis
  search: opensearch
  session: embedded
  storage_options: { dsn: postgresql://user:pass@host:5432/db }
  counters_options: { url: redis://host:6379/0, namespace: "mcm:myproject:" }
  search_options:   { url: http://host:9200, index_prefix: "mcm-myproject-" }
```

Maturity: SQLite and Postgres (storage/counters/search) and Redis counters are
production-ready. OpenSearch search is reference and contract-only: its current sync model
re-indexes on every query (O(N)), so it is not suitable for production query loads.

**Env-var overrides** (for containers; YAML wins on explicit conflict):

| Variable | Effect |
|----------|--------|
| `MCM_PROJECT_NAME` | project name |
| `MCM_DB_PATH` / `MCM_RULES_PATH` | db path / rules path (`:`-separated for multi-path) |
| `MCM_SOURCE_OF_TRUTH` | `files` or `database` |
| `MCM_BACKENDS_{STORAGE,COUNTERS,SEARCH,SESSION}` | per-axis adapter |
| `MCM_POSTGRES_DSN` / `MCM_REDIS_URL` / `MCM_OPENSEARCH_URL` | adapter connection |
| `MCM_ALLOWED_HOSTS` / `MCM_DNS_REBINDING_PROTECTION` | daemon host allow-list / guard toggle |
| `MCM_AUTH_REQUIRED` | require a bearer token on the HTTP transport |
| `MCM_ACTOR` | actor recorded on writes (provenance / author≠judge) |
| `MCM_TRUST_WEIGHTS` / `MCM_TRUST_DEFAULT` | graded `actor→weight` map / default weight |
| `MCM_AMBIENT_RECALL` | enable opt-in ambient recall in the hook |
| `MCM_SERVER_NAME` / `MCM_SERVER_INSTRUCTIONS` / `MCM_CONFIG` / `MCM_LOG_PATH` | server identity / config path / log |

**Shared rules across projects.** `rules_path` accepts a list. The first is where new rules
are written; all are scanned and indexed:

```yaml
rules_path:
  - rules/                          # project-specific (primary)
  - /home/you/shared-rules/infra/   # shared, read across projects
```

### Daemon mode and the watcher cascade

In daemon mode with `source_of_truth: files`, the engine watches `rules/*.md` and mirrors
edits into storage within ~500ms (files win; the DB is a cache). External edits update
rows; deletions soft-delete (`archived`); recreations un-archive. Stdio mode runs a
one-shot `sync_rules` at startup instead. Full spec:
[`docs/watcher-cascade.md`](docs/watcher-cascade.md).

### Plugins

Extend with domain tables and tools that pass the shared conformance suite:

```python
from mcm_engine import MCMPlugin, SearchScope

class MyPlugin(MCMPlugin):
    name = "my-plugin"
    def get_schema_sql(self): return "CREATE TABLE IF NOT EXISTS my_data (...)"
    def register_tools(self, server):
        @server.mcp.tool()
        def my_tool(): ...
    def get_search_scopes(self): return [SearchScope(name="my_data", ...)]
```

Register via entry point or `plugins:` in config. Plugins use embedded SQLite for their
own tables. Third-party adapters subclass `StorageConformance` / `CounterConformance` /
`SearchConformance` / `SessionConformance` from `mcm_engine.testing.conformance`.

---

## Upgrade

### The local tool and hook

```bash
uv tool install --reinstall --from /path/to/mcm-engine mcm-engine
```

The PreToolUse and SessionStart hooks run from this installed binary, so hook changes take
effect only after a reinstall. A `uv tool` install is sequestered from your working tree.

### The daemon and container

Rebuild and redeploy the image, or restart `serve` from the updated code. Schema
migrations run automatically on startup.

### Schema migrations: back up first

Migrations are automatic on startup, idempotent, and `IF NOT EXISTS`-guarded. Back up
before any schema change on live data:

- **SQLite:** copy the `.db` file.
- **Postgres:** `docker compose exec -T <pg> pg_dump -U <user> -d <db> -Fc > backup.dump`.

Verify after upgrading:

- **SQLite** tracks the schema version: `SELECT version FROM _mcm_versions WHERE component='core';`
  should equal the current `CORE_VERSION`.
- **Postgres** also stamps that row as of 3.5.0 (its `ensure_schema` upserts
  `_mcm_versions` after applying the idempotent guarded DDL), so the same query works.
  Column existence still confirms a specific migration, e.g.
  `SELECT to_regclass('public.token_ledger');` (non-null).

### Moving between backends

```bash
mcm-engine migrate --from sqlite:///local.db --to "postgresql://user:pass@host:5432/db"
```

Copies every entity table, preserves IDs, bumps destination sequences; refuses a non-empty
destination unless `--force`.

---

## Documentation

| File | Topic |
|------|-------|
| [`CHANGELOG.md`](CHANGELOG.md) | Version history and changes |
| [`examples/docker-compose.yml`](examples/docker-compose.yml) | Compose deployment example (Postgres + daemon) |
| [`deploy/helm/mcm-engine/`](deploy/helm/mcm-engine/) | Helm chart for Kubernetes |
| `docs/watcher-cascade.md` | Files-win conflict resolution, debounce, atomic-rename handling |
| `docs/capabilities.md` | Adapter capability flags + honest degradation |
| `docs/contract-versioning.md` | When to bump `CONTRACT_VERSION` |
| `docs/seam-inventory.md` | Every SQL site in the engine, by file |
| `terraform/aws/README.md` | Reference AWS deployment |
