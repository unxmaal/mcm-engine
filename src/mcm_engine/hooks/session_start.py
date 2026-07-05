"""SessionStart hook for mcm-engine.

Why this exists: nothing automatically orients a fresh agent session with
the engine's resume context. The CLAUDE.md "call session_start" instruction
relies on agent discipline. This hook closes that gap — Claude Code fires
SessionStart on startup/resume/clear, and this command injects the last
handoff + recent-knowledge count straight into the new session as
additionalContext, with zero agent action required.

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

Fail-open by contract: any error (no config, unreadable db, malformed event)
exits 0 with no output. A session must never fail to start because of this
hook.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


def build_session_context(storage: Any, project_name: str) -> str:
    """Render a short resume orientation from storage. Returns "" when there
    is nothing worth injecting. Best-effort: each lookup is guarded so a
    backend missing a method degrades to fewer lines, not a crash."""
    try:
        recent = storage.count_recent_knowledge(since_days=7)
    except Exception:
        recent = None

    try:
        s = storage.get_last_session()
    except Exception:
        s = None

    # Nothing worth injecting: no prior session and no recent knowledge.
    if s is None and not recent:
        return ""

    parts: list[str] = []
    if recent is not None:
        parts.append(f"Recent knowledge (7d): {recent}")
    if s is not None:
        parts.append(f"Last handoff ({s.created_at}): {s.status}")
        if s.current_task:
            parts.append(f"Task: {s.current_task}")
        if s.next_steps:
            parts.append(f"Next: {s.next_steps}")
        if s.blockers:
            parts.append(f"Blockers: {s.blockers}")

    return f"[mcm-engine] Resume context for '{project_name}':\n" + "\n".join(parts)


def _load_storage(cwd: Path) -> tuple[Any, str]:
    """Resolve config under cwd and build the configured storage backend.
    Mirrors the db_path resolution MCMServer / cmd_ingest do so the embedded
    SQLite adapter sees an absolute path."""
    from ..config import load_config
    from ..wiring import build_verified_context

    config = load_config(project_root=cwd)
    resolved_db = config.resolve_db_path(cwd)
    backends = config.backends
    for axis_name, axis_opts in (
        ("storage", backends.storage_options),
        ("counters", backends.counters_options),
        ("search", backends.search_options),
    ):
        if getattr(backends, axis_name) == "embedded" and "db_path" not in axis_opts:
            axis_opts["db_path"] = str(resolved_db)

    ctx = build_verified_context(config)
    return ctx.storage, config.project_name


def main(argv: Optional[list[str]] = None) -> int:
    """Read a SessionStart event from stdin, inject resume context. Always
    exits 0 — see module docstring on the fail-open contract."""
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        event = {}

    cwd = Path(event.get("cwd") or os.getcwd())

    try:
        storage, project_name = _load_storage(cwd)
        text = build_session_context(storage, project_name)
    except Exception:
        return 0

    if text:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
