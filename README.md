# mcm-engine

Memory Context Management engine for AI coding sessions.

Persistent knowledge management + session handoff + behavioral nudges, delivered as an MCP server.

## Quick Start

```bash
pip install mcm-engine
mcm-engine init --project myproject
mcm-engine run
```

## Configuration

Create `mcm-engine.yaml` in your project root:

```yaml
project_name: myproject
db_path: .claude/knowledge.db
plugins: []
nudges:
  store_reminder_turns: 10
  checkpoint_turns: 25
  mandatory_stop_turns: 50
```

## MCP Integration

Add to `.mcp.json`:

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

| Tool | Purpose |
|------|---------|
| `search` | Unified FTS5 search across all knowledge |
| `add_knowledge` | Store findings, decisions, insights |
| `add_negative` | Store anti-patterns and dead ends |
| `report_error` | Log error + auto-search for fixes |
| `session_start` | Initialize session with context |
| `session_handoff` | Snapshot state for next session |
| `session_summary` | Current session statistics |

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
