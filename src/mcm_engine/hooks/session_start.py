"""SessionStart hook for mcm-engine.

Why this exists: nothing automatically orients a fresh agent session with the
engine's resume context, and the CLAUDE.md "call session_start" instruction
relies on agent discipline.

What this hook MUST NOT do: open a database. The KB lives behind the mcm-engine
MCP server (see .mcp.json); a hook that reads a local store injects resume
context from whatever stale/shadow db it happens to find, not the real KB
(issue #58 — this literally shadowed the remote KB with a dead local sqlite).

So this hook does not fetch resume context — it *compels the agent* to fetch it
the one correct way: by calling the mcm-engine `session_start` MCP tool. A hook
that reminds, not one that speaks SQL.

Wire it into settings.json:

    {
      "hooks": {
        "SessionStart": [
          {
            "hooks": [
              {"type": "command", "command": "mcm-engine session-start", "timeout": 5}
            ]
          }
        ]
      }
    }

Fail-open by contract: any error exits 0 with no output. A session must never
fail to start because of this hook.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

# The directive injected as additionalContext. It points the agent at the MCP
# tools that ARE the KB interface — no local state is read here.
RESUME_DIRECTIVE = (
    "[mcm-engine] Load your working context now via the MCP: call the "
    "`session_start` tool (and `get_resume_context` for more) to pull your last "
    "handoff, active tasks, and pinned rules. The knowledge base lives behind "
    "the mcm-engine MCP server — do not read any local database or file for it."
)


def main(argv: Optional[list[str]] = None) -> int:
    """Read (and discard) the SessionStart event, then inject the MCP directive.
    Always exits 0 — see the module docstring's fail-open contract."""
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": RESUME_DIRECTIVE,
            }
        }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
