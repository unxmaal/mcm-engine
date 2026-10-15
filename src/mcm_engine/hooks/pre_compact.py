"""PreCompact hook: checkpoint session state before a compaction summary
replaces it.

Long sessions compact often, and each compaction's summary drops detail.
Saving state before the summary lands depends on the agent remembering to call
``session_handoff`` — exactly the "good intentions fail" case the engine exists
to defeat. This hook makes the checkpoint automatic.

PreCompact carries only ``session_id``, ``trigger`` ("manual"|"auto") and
``custom_instructions`` (the text passed to ``/compact``, else null). It cannot
see the session's working goals, so the checkpoint it writes is a boundary
anchor: it records that a compaction happened and preserves any ``/compact``
instructions (the user's stated focus). The restore half runs at SessionStart
with ``source == "compact"`` — see ``session_start.py`` — because PostCompact
cannot inject context back to the model.

The write goes through the ``save_snapshot`` MCP tool over HTTP when a server is
configured (the authoritative basement pod), else the local embedded store —
the same remote-or-local path ``session_end`` uses. Writing the local embedded
store is writing the authoritative store, not a shadow read (issue #58 is about
READING a stale local db; this is a write on the machine that owns it).

Fail-open by contract: any error exits 0. A hook must never block or delay a
compaction the harness needs to do.

Wire it into settings.json:

    {
      "hooks": {
        "PreCompact": [
          {
            "hooks": [
              {"type": "command", "command": "mcm-engine pre-compact", "timeout": 5}
            ]
          }
        ]
      }
    }
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .mcp_enforcement import (
    _find_project_root,
    _mcp_http_endpoint,
    mcp_http_call_tool,
)

WRITE_TIMEOUT_S = 5.0


def _build_arguments(trigger: str, custom_instructions: str) -> dict[str, Any]:
    """save_snapshot kwargs for the compaction boundary. Only the fields the
    hook can actually know are populated; the rest the tool defaults to empty."""
    progress = f"Auto-checkpoint before {trigger or 'auto'} compaction."
    args: dict[str, Any] = {"progress": progress}
    # A manual /compact may carry the user's stated focus; keep it.
    if custom_instructions:
        args["open_questions"] = custom_instructions
    return args


def _write_local(cwd: Path, args: dict[str, Any]) -> None:
    """Local fallback (stdio / embedded): append a snapshot row to the last
    session, mirroring the save_snapshot tool's own local logic."""
    from ..backends import SnapshotRow
    from ..config import load_config
    from ..wiring import build_verified_context

    config = load_config(project_root=_find_project_root(cwd))
    ctx = build_verified_context(config)
    last = ctx.storage.get_last_session(principal=None)
    session_id = last.id if last else None
    seq = ctx.storage.next_snapshot_seq(session_id)
    ctx.storage.insert_snapshot(SnapshotRow(
        id=0,
        session_id=session_id,
        sequence_num=seq,
        progress=args.get("progress") or None,
        open_questions=args.get("open_questions") or None,
    ))


def main(argv: Optional[list[str]] = None) -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return 0

    cwd_raw = event.get("cwd", os.getcwd())
    cwd = Path(cwd_raw) if cwd_raw else Path.cwd()
    trigger = event.get("trigger", "") or ""
    custom_instructions = event.get("custom_instructions") or ""

    args = _build_arguments(trigger, custom_instructions)

    try:
        url, headers = _mcp_http_endpoint(cwd)
        if url:
            mcp_http_call_tool(
                url, "save_snapshot", args,
                headers=headers, timeout=WRITE_TIMEOUT_S,
            )
        else:
            _write_local(cwd, args)
    except Exception:
        # Fail-open: never let a checkpoint write block a compaction.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
