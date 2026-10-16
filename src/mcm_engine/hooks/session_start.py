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

# source=="compact" fires right after a compaction, whose summary has just
# dropped detail (issue #145). PostCompact cannot inject context back to the
# model, so this SessionStart is the one documented channel to steer the
# resumed agent. The PreCompact hook wrote a boundary snapshot; point the agent
# at it rather than trusting the summary alone.
COMPACT_DIRECTIVE = (
    "[mcm-engine] This session was just compacted — the summary above dropped "
    "detail. Before acting on it, call `get_resume_context` (and `session_start`) "
    "via the MCP to recover your last handoff, active tasks, pinned rules, and "
    "the pre-compaction checkpoint the engine saved. Trust the knowledge base "
    "over the summary where they differ; do not read any local database or file."
)


def main(argv: Optional[list[str]] = None) -> int:
    """Read the SessionStart event and inject the MCP directive, tailored to the
    event ``source`` (a compaction gets a recovery-focused directive, issue
    #145). Always exits 0 — see the module docstring's fail-open contract."""
    source = ""
    event: dict = {}
    try:
        raw = sys.stdin.read()
        if raw:
            event = json.loads(raw) or {}
            source = event.get("source", "") or ""
    except Exception:
        source = ""

    if source == "compact":
        # The compaction summary dropped working detail, so prior hook-side
        # coverage is no longer resident (#159). Stamp the boundary; fail-open.
        try:
            from .mcp_enforcement import stamp_compaction_boundary
            stamp_compaction_boundary(event)
        except Exception:
            pass

    directive = COMPACT_DIRECTIVE if source == "compact" else RESUME_DIRECTIVE

    # issue #63: re-surface the positive voice exemplar at session start to
    # fight instruction decay, as a positive target rather than a denylist.
    try:
        from ..style import VOICE_EXEMPLAR
        context = directive + "\n\n" + VOICE_EXEMPLAR
    except Exception:
        context = directive

    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
