"""PreToolUse enforcement hook for mcm-engine v2.

Why this exists: the in-process MCP nudge system (`mcm_engine.tracker`)
only sees MCP tool calls. The model can call file-mutating built-ins
all day without ever calling `search` or `report_error`, silently
bypassing the MCP-first protocol. This hook fires on every built-in
tool call AND every compliance MCP read, tracks the budget, warns at 8
unanswered built-in calls, and blocks file-mutating tools at 20.

Supports both major agent harnesses:
  - Claude Code: capitalized built-in names (Edit, Write, NotebookEdit,
    Bash); MCP tools as ``mcp__<server>__<tool>``.
  - opencode: lowercase built-in names (edit, write, bash, apply_patch);
    MCP tools as ``<server>_<tool>``.
The hook normalizes both styles internally; users don't have to pick.

Wire it up in `~/.claude/settings.json` or `.claude/settings.local.json`:

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

Using the ``mcm-engine hook`` subcommand (rather than ``python3 -m
mcm_engine.hooks.mcp_enforcement``) means the hook works under every
install path — ``uv tool install`` isolates mcm-engine in its own venv
that system ``python3`` can't see, but the ``mcm-engine`` binary itself
is always on PATH after install.

State lives in ``<project>/.claude/mcp-enforcement-state.json``, keyed
by Claude Code's per-session UUID. The file accumulates one entry per
distinct session; delete it any time to start fresh.

Threshold tuning rationale:
  WARN at 8   — gives 8 successive built-in calls before nagging. Most
                small tasks finish under that budget without any need
                for an MCP read.
  BLOCK at 20 — at 20 unanswered built-in calls, the model has clearly
                drifted into "code first, look later" territory. Block
                forces it back to the MCP layer.
  Bash exempt from BLOCK — bash-heavy sessions (lots of git, grep, ls)
                are common and legitimate. Bash counts toward the
                threshold but isn't itself blocked.

Compliance MCP tools that RESET the counter:
  - search           — the canonical "look first" call.
  - report_error     — auto-searches for a fix; same effect as search.
  - sync_rules       — confirms the agent has the current rule set.
  - session_start    — fresh session always allowed; counter reset.
  - get_resume_context — same as session_start in spirit.
  - read_rule        — direct read against the knowledge base.

Pure writes (add_knowledge, add_rule, etc.) do NOT reset the counter:
recording AFTER the fact is fine, but it doesn't excuse skipping the
look-first step.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


WARN_THRESHOLD = 8
BLOCK_THRESHOLD = 20

# Drop per-session counter entries whose last_reset_at is older than this.
# Each Claude Code session gets a fresh UUID, so without pruning the state
# file accumulates one entry per session forever. 30 days is well past any
# realistic "I want to resume that session" window.
STATE_TTL_SECONDS = 30 * 24 * 3600

# Built-in tools tracked across both Claude Code (capitalized) and
# opencode (lowercase) naming conventions. Stored lowercase; matching is
# case-insensitive so both ``Edit`` (Claude Code) and ``edit`` (opencode)
# count as the same tool.
#
# ``apply_patch`` is opencode-specific; treated as a file mutator and
# subject to blocking, same as Edit/Write.
COUNTED_BUILTIN_TOOLS = frozenset({
    "edit",
    "write",
    "notebookedit",
    "bash",
    "apply_patch",
})

# Subset of the above that are eligible for hard-blocking at the threshold.
# Bash is intentionally NOT here — bash-heavy work is common and legitimate.
BLOCKING_BUILTIN_TOOLS = frozenset({
    "edit",
    "write",
    "notebookedit",
    "apply_patch",
})

# Compliance MCP tools — these are reads against the knowledge layer that
# count as "the agent looked before doing." Matched against the suffix of
# the invocation so the hook works regardless of MCP server name AND
# regardless of the harness's MCP tool naming convention:
#   Claude Code  →  mcp__<server>__search
#   opencode     →  <server>_search   (or sometimes bare ``search``)
COMPLIANCE_TOOL_NAMES = frozenset({
    "search",
    "report_error",
    "sync_rules",
    "session_start",
    "get_resume_context",
    "read_rule",
})

# Claude Code MCP format. Lazy server-name match captures the tool suffix.
_CLAUDE_MCP_TOOL_RE = re.compile(r"^mcp__(.+?)__(.+)$")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _state_path(cwd: Path) -> Path:
    return cwd / ".claude" / "mcp-enforcement-state.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable — start fresh rather than fail the tool call.
        return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prune_stale(state: dict[str, Any], *, now: float, ttl: float = STATE_TTL_SECONDS) -> None:
    """Drop entries whose last_reset_at is older than ``ttl`` seconds."""
    cutoff = now - ttl
    stale = [
        sid for sid, s in state.items()
        if isinstance(s, dict) and s.get("last_reset_at", now) < cutoff
    ]
    for sid in stale:
        del state[sid]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _is_compliance_mcp_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` is a compliance read against the knowledge
    MCP, in any supported naming convention.

    Accepts:
      - ``mcp__<server>__<compliance>``     — Claude Code MCP format
      - ``<server>_<compliance>``           — opencode MCP format
      - ``<compliance>``                     — bare (e.g. via subprocess test)

    The lookup is exact on the trailing compliance-name segment, so a
    third-party MCP server with a tool literally named (say) ``search``
    will register as compliance — which is desirable: any "search" call
    on the knowledge layer counts.
    """
    # Claude Code MCP format.
    m = _CLAUDE_MCP_TOOL_RE.match(tool_name)
    if m is not None and m.group(2) in COMPLIANCE_TOOL_NAMES:
        return True
    # opencode MCP format + bare name.
    for name in COMPLIANCE_TOOL_NAMES:
        if tool_name == name or tool_name.endswith("_" + name):
            return True
    return False


def _normalize_builtin_tool(tool_name: str) -> str | None:
    """Return the canonical lowercase form if ``tool_name`` is a tracked
    built-in (across Claude Code and opencode naming), else None."""
    lowered = tool_name.lower()
    return lowered if lowered in COUNTED_BUILTIN_TOOLS else None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _decide(
    tool_name: str,
    session_state: dict[str, Any],
) -> tuple[int, str]:
    """Return (exit_code, message_for_stderr) for a single tool call.

    Mutates ``session_state`` in place so the caller can persist it.
    """
    if _is_compliance_mcp_tool(tool_name):
        session_state["builtin_calls"] = 0
        session_state["last_reset_at"] = time.time()
        return 0, ""

    normalized = _normalize_builtin_tool(tool_name)
    if normalized is None:
        return 0, ""

    session_state["builtin_calls"] = session_state.get("builtin_calls", 0) + 1
    n = session_state["builtin_calls"]

    if n >= BLOCK_THRESHOLD and normalized in BLOCKING_BUILTIN_TOOLS:
        msg = (
            f"[mcm-engine] BLOCKED: {n} built-in tool calls in this session "
            f"without a compliance MCP read. Call one of "
            f"{', '.join(sorted(COMPLIANCE_TOOL_NAMES))} on the mcm-engine "
            f"MCP server before continuing — the DB-as-cache contract "
            f"requires you to look before you leap."
        )
        return 2, msg

    if n >= WARN_THRESHOLD:
        msg = (
            f"[mcm-engine] {n}/{BLOCK_THRESHOLD} built-in calls without an "
            f"MCP read. At {BLOCK_THRESHOLD}, file-mutating tools will be "
            f"BLOCKED. Call search / report_error / sync_rules / "
            f"session_start on the mcm-engine MCP to reset the counter."
        )
        return 0, msg

    return 0, ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Read a PreToolUse event from stdin and decide whether to allow it.

    Exit codes follow Claude Code's hook convention:
      0 — allow (may emit a warning via stderr; tool proceeds).
      2 — block (stderr text is shown to the model as feedback).
      Any other non-zero is treated by the harness as an unexpected error.
    """
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        # If the event payload is malformed, fail open. Better to over-allow
        # than to brick the harness on a contract drift.
        return 0

    tool_name = event.get("tool_name", "")
    session_id = event.get("session_id", "default")
    cwd_raw = event.get("cwd", os.getcwd())
    cwd = Path(cwd_raw) if cwd_raw else Path.cwd()

    sp = _state_path(cwd)
    state = _read_state(sp)
    _prune_stale(state, now=time.time())
    s = state.setdefault(session_id, {
        "builtin_calls": 0,
        "last_reset_at": time.time(),
    })

    exit_code, message = _decide(tool_name, s)

    try:
        _write_state(sp, state)
    except OSError:
        # State write failures shouldn't block the user's tool call.
        pass

    if message:
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
