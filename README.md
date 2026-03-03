# mcm-engine

Memory Context Management engine for AI coding sessions.

Persistent knowledge management + session handoff + behavioral nudges, delivered as an MCP server.

## Install

mcm-engine is not yet published to PyPI. Install from a local clone:

```bash
# Standard install
pip install /path/to/mcm-engine

# Editable install (changes take effect immediately)
pip install -e /path/to/mcm-engine

# Or install globally with uv/pipx (no venv needed)
uv tool install /path/to/mcm-engine
```

## Quick Start

```bash
cd /path/to/your-project
mcm-engine init --project myproject
mcm-engine run
```

`init` creates:
- `mcm-engine.yaml` — project configuration
- `.claude/knowledge.db` — knowledge database
- `rules/` — directory for persistent rule files

## Configuration

`mcm-engine.yaml` in your project root:

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

### Shared Rules Across Projects

`rules_path` accepts a list. The first entry is the primary directory where
new rule files are created. All entries are scanned by `sync_rules` and
indexed for search.

```yaml
rules_path:
  - rules/                                # project-specific (primary)
  - /home/you/shared-rules/bigcorp/       # shared business logic
  - /home/you/shared-rules/infra/         # shared infra patterns
```

Or via environment variable (colon-separated):

```bash
export MCM_RULES_PATH="rules/:/home/you/shared-rules/bigcorp"
```

External rules outside the project root are stored with absolute paths in the
DB index. Run `sync_rules` after adding or modifying shared rule files.

## MCP Integration

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "mcm-engine",
      "args": ["run"]
    }
  }
}
```

## Tools

### Knowledge Management

| Tool | Purpose |
|------|---------|
| `search` | Unified FTS5 search across all knowledge, rules, errors |
| `add_knowledge` | Store findings, decisions, insights (deduplicates by topic) |
| `add_negative` | Store anti-patterns and dead ends |
| `report_error` | Log error + auto-search for fixes (with quality gate) |

### Rules (Persistent Knowledge)

| Tool | Purpose |
|------|---------|
| `add_rule` | Create/index a rule file in `rules/` |
| `read_rule` | Read a rule file's contents |
| `promote_to_rule` | Promote a DB entry to a persistent rule file |
| `sync_rules` | Re-index all rule files after manual edits |

### Relationships

| Tool | Purpose |
|------|---------|
| `link_knowledge` | Create typed edges (fixes, causes, supersedes, contradicts, related) |
| `get_related` | Show all relationships for an entry |

### Session Management

| Tool | Purpose |
|------|---------|
| `session_start` | Initialize session with context + stale knowledge report |
| `session_handoff` | Snapshot state for next session |
| `session_summary` | Current session statistics |

## Architecture

Two-layer knowledge system:

- **Rule files** (`rules/*.md`) — authoritative, human-readable, version-controlled
- **Knowledge DB** (`.claude/knowledge.db`) — fast FTS5 lookup cache + agent memory

The DB indexes rule files for search. If DB and files disagree, files win.

### Search Features

- **FTS5 full-text search** with LIKE fallback across all scopes (knowledge, rules, errors, negative knowledge)
- **Composite ranking** combining text relevance, hit frequency, and recency
- **Quality gate** on auto-search (report_error) to filter weak matches
- **Staleness detection** — entries >90 days old without recent hits tagged `[STALE]`
- **Deduplication** — add_knowledge updates existing entries with matching topic+kind

## Plugins

Extend with domain-specific knowledge:

```python
from mcm_engine import MCMPlugin, SearchScope

class MyPlugin(MCMPlugin):
    name = "my-plugin"

    def get_schema_sql(self):
        return "CREATE TABLE IF NOT EXISTS my_data (...)"

    def register_tools(self, server):
        @server.mcp.tool()
        def my_tool(): ...

    def get_search_scopes(self):
        return [SearchScope(name="my_data", ...)]
```

Register via entry points or config:

```yaml
plugins:
  - my-plugin          # entry point
  - mymodule:MyPlugin  # direct import
```
