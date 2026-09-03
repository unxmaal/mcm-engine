"""SessionEnd hook: finalize per-session efficiency telemetry (Phase 2).

Runs locally at session end (wired in the editor's settings.json as
``mcm-engine session-end``). It:

  1. parses the session transcript for REAL token totals (in-process, no
     dependency; ccusage-style tools do the same, but we own this),
  2. reads the PreToolUse hook's accumulated code metrics from the shared state
     file, and
  3. writes ONE ``session_metrics`` record — via the ``record_session_metrics``
     MCP tool over HTTP when a server is configured (basement pod), else the
     local embedded store.

DIAGNOSTIC ONLY. Fail-open throughout: a telemetry failure must never disrupt
session teardown, so every step is guarded and the hook always exits 0.
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
    _read_state,
    _state_path,
    mcp_http_call_tool,
)

WRITE_TIMEOUT_S = 5.0

# Argument keys forwarded to record_session_metrics / SessionMetricsRow. Kept
# explicit so a state-shape change can never leak an unexpected kwarg.
_METRIC_KEYS = (
    "loc_added", "loc_removed", "loc_churned",
    "comment_lines_added", "code_lines_added", "edit_cycles_max",
)


def _parse_transcript_tokens(path: str) -> dict[str, int]:
    """Sum usage across the transcript's records. Tolerant of the exact nesting
    (usage may sit at ``record.message.usage`` or ``record.usage``) and of
    malformed lines. Fail-open: returns zeros on any error."""
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                usage = None
                msg = rec.get("message")
                if isinstance(msg, dict):
                    usage = msg.get("usage")
                if usage is None:
                    usage = rec.get("usage")
                if isinstance(usage, dict):
                    totals["in"] += int(usage.get("input_tokens") or 0)
                    totals["out"] += int(usage.get("output_tokens") or 0)
                    totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                    totals["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    except OSError:
        pass
    return totals


def _build_arguments(
    session_id: str, entry: dict[str, Any], tokens: dict[str, int]
) -> dict[str, Any]:
    m = (entry or {}).get("metrics", {}) or {}
    args: dict[str, Any] = {
        "cc_session_id": session_id,
        # "" not None: record_session_metrics types this as `str` and
        # rejects null, so a session with no PreToolUse state would 400 and be
        # silently swallowed by the fail-open guard below.
        "first_seen_at": (entry or {}).get("first_seen_at") or "",
        "in_tokens": tokens["in"],
        "out_tokens": tokens["out"],
        "cache_read_tokens": tokens["cache_read"],
        "cache_write_tokens": tokens["cache_write"],
        "fixation_events": (entry or {}).get("fixation_events", 0),
    }
    for k in _METRIC_KEYS:
        args[k] = m.get(k, 0)
    return args


def _has_signal(args: dict[str, Any]) -> bool:
    """True if there is anything worth recording (any token or code activity)."""
    return any(
        v for k, v in args.items() if k not in ("cc_session_id", "first_seen_at")
    )


def _write_local(cwd: Path, args: dict[str, Any]) -> None:
    from ..backends import SessionMetricsRow
    from ..config import load_config
    from ..wiring import build_verified_context

    config = load_config(project_root=_find_project_root(cwd))
    ctx = build_verified_context(config)
    row = SessionMetricsRow(
        id=0, project=getattr(config, "project_name", None), **args
    )
    ctx.storage.upsert_session_metrics(row)


def main(argv: Optional[list[str]] = None) -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return 0

    session_id = event.get("session_id", "")
    if not session_id:
        return 0
    cwd_raw = event.get("cwd", os.getcwd())
    cwd = Path(cwd_raw) if cwd_raw else Path.cwd()
    transcript = event.get("transcript_path", "")

    tokens = (
        _parse_transcript_tokens(transcript)
        if transcript
        else {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    )
    entry = _read_state(_state_path(cwd)).get(session_id, {})
    args = _build_arguments(session_id, entry, tokens)

    if not _has_signal(args):
        return 0  # empty session — nothing worth a row.

    try:
        url, headers = _mcp_http_endpoint(cwd)
        if url:
            mcp_http_call_tool(
                url, "record_session_metrics", args,
                headers=headers, timeout=WRITE_TIMEOUT_S,
            )
        else:
            _write_local(cwd, args)
    except Exception:
        # Fail-open: never let a telemetry write break session teardown.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
