"""PreToolUse enforcement hook for mcm-engine v2.

Why this exists: the in-process MCP nudge system (`mcm_engine.tracker`)
only sees MCP tool calls. The model can call file-mutating built-ins
all day without ever calling `search` or `report_error`, silently
bypassing the MCP-first protocol. This hook fires on every built-in
tool call AND every compliance MCP read, tracks the budget, warns
after 3 unanswered built-in calls, and blocks file-mutating tools at
6.

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

State lives in ``<project-root>/.claude/mcp-enforcement-state.json``,
keyed by Claude Code's per-session UUID. The project root is discovered
by walking up from the event's ``cwd`` looking for a ``.git`` directory,
``pyproject.toml``, or an existing ``.claude`` directory; the search
never ascends past ``$HOME``. If no marker is found, state falls back to
``<cwd>/.claude/...`` for backwards compatibility. The file accumulates
one entry per distinct session; delete it any time to start fresh.

Threshold tuning rationale:
  WARN at 3   — three successive built-in calls without an MCP read
                already means the agent has likely drifted past at
                least one moment where the KB should have been
                consulted. The warn is a directive ("look NOW"), not
                a runway counter ("you have N calls left"); a tight
                threshold reinforces that framing.
  BLOCK at 6  — at six file-mutating calls without a single look-first
                call, the project contract is materially violated.
                Block, in language that names the violation.
  Bash exempt from BLOCK — bash-heavy sessions (lots of git, grep, ls)
                are common and legitimate. Bash counts toward the warn
                threshold but never triggers the block.

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
import threading
import time
from pathlib import Path
from typing import Any, Optional


WARN_THRESHOLD = 3
BLOCK_THRESHOLD = 6

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


# Files/directories that mark a project root. Inner-most marker wins so each
# repo gets its own state file even when nested under an umbrella project.
_PROJECT_MARKERS = (".git", "pyproject.toml", ".claude")


def _find_project_root(cwd: Path) -> Path:
    """Walk up from ``cwd`` looking for a project-root marker. Stop at
    ``$HOME`` and at the filesystem root. Return the original ``cwd`` if
    no marker is found within range — preserves backwards compatibility
    with callers and tests that pass an arbitrary directory.
    """
    try:
        start = cwd.resolve()
    except (OSError, RuntimeError):
        return cwd
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None

    cur = start
    while True:
        for marker in _PROJECT_MARKERS:
            if (cur / marker).exists():
                return cur
        if home is not None and cur == home:
            return cwd
        parent = cur.parent
        if parent == cur:
            return cwd
        cur = parent


def _state_path(cwd: Path) -> Path:
    return _find_project_root(cwd) / ".claude" / "mcp-enforcement-state.json"


def _events_path(cwd: Path) -> Path:
    """Append-only JSONL diagnostic log of warn/block events, alongside the
    state file. Lets thresholds be tuned from real block-rate data rather
    than guesswork. One line per warn/block; silent allows aren't logged."""
    return _find_project_root(cwd) / ".claude" / "mcp-enforcement-events.jsonl"


def _append_event(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record. Best-effort: a logging failure must never
    break the user's tool call."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


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
        session_state["mutator_calls"] = 0
        session_state["last_reset_at"] = time.time()
        return 0, ""

    normalized = _normalize_builtin_tool(tool_name)
    if normalized is None:
        return 0, ""

    # `builtin_calls` is the total (mutators + bash), and drives the warning.
    # `mutator_calls` counts only file mutators, and is the ONLY thing that
    # drives the block. Read-only Bash inflates the total (so a bash-heavy
    # stretch still gets nudged) but must never push an edit into a block —
    # bash IS looking, not leaping.
    session_state["builtin_calls"] = session_state.get("builtin_calls", 0) + 1
    is_mutator = normalized in BLOCKING_BUILTIN_TOOLS
    if is_mutator:
        session_state["mutator_calls"] = session_state.get("mutator_calls", 0) + 1

    total = session_state["builtin_calls"]
    mutators = session_state.get("mutator_calls", 0)

    if is_mutator and mutators >= BLOCK_THRESHOLD:
        # Fail-open (issue #19): this is a CONSULTATION GAP, not a block. The
        # edit is ALWAYS allowed (return 0) — after this change the hook is
        # accountability telemetry, not a gate. main() records a
        # `consultation_gap` event. A hard block here dead-locks the agent
        # exactly when the knowledge backend is unreachable (the one moment it
        # cannot call a reset tool to clear the counter).
        msg = (
            "[mcm-engine] CONSULTATION GAP recorded — "
            f"{mutators} file edits this session with no look-first MCP read. "
            "This edit is ALLOWED (fail-open); the gap is logged to "
            ".claude/mcp-enforcement-events.jsonl and counts against this "
            "session's informed-vs-blind ratio. The knowledge base is "
            "authoritative for project specifics, not your pretrained weights "
            "— strongly recommended next: call a KB search before continuing. "
            f"Reset tools: {', '.join(sorted(COMPLIANCE_TOOL_NAMES))}."
        )
        return 0, msg

    if total >= WARN_THRESHOLD:
        msg = (
            "[mcm-engine] STOP. Project contract requires a knowledge-base "
            "search before further work.\n"
            f"You have made {total} built-in tool calls "
            f"({mutators} edits) this session with no look-first MCP read. "
            "This is not a runway counter — it is a directive. The next "
            "action should be `mcp__knowledge__search` with a query matching "
            "the topic at hand, NOT another Edit/Write/Bash. If the search "
            "returns nothing relevant, say so explicitly in your reply and "
            "then continue. Confidently asserting Corning-specific facts "
            "from pretrained memory is the failure mode this hook exists to "
            "catch.\n"
            "\n"
            f"Mutator block fires at {BLOCK_THRESHOLD} edits. Reset tools: "
            f"{', '.join(sorted(COMPLIANCE_TOOL_NAMES))}."
        )
        return 0, msg

    return 0, ""


# ---------------------------------------------------------------------------
# Ambient recall (issue #35) — OPT-IN, default OFF, experimental.
#
# When MCM_AMBIENT_RECALL is truthy, a file-mutator event triggers a BEST-EFFORT
# KB search keyed on the edited file path, and the top rule hit is surfaced as an
# advisory stderr line ("hook as muse"). It NEVER blocks, never changes the exit
# code, and silently skips on ANY error/timeout/backend-unavailable. Enabling it
# needs `uv tool install --reinstall` to take effect in the live hook, and it
# should be tuned before broad use — it adds a search to every qualifying call,
# and which backend it hits depends on the cwd's config. Default OFF so the
# hook's behavior is byte-identical to today unless you opt in.
# ---------------------------------------------------------------------------

AMBIENT_ENV = "MCM_AMBIENT_RECALL"
AMBIENT_MAX_PER_SESSION = 8
AMBIENT_TIMEOUT_S = 1.0
_AMBIENT_TRUTHY = {"1", "true", "on", "yes"}


def _ambient_enabled() -> bool:
    return os.environ.get(AMBIENT_ENV, "").strip().lower() in _AMBIENT_TRUTHY


def _ambient_query(tool_name: str, event: dict) -> Optional[str]:
    """Implicit query from a file-mutator event's path (basename tokens); None
    for non-mutator events or when no path is present."""
    if _normalize_builtin_tool(tool_name) not in BLOCKING_BUILTIN_TOOLS:
        return None
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    if not path:
        return None
    stem = Path(str(path)).stem
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", stem) if len(t) > 2]
    return " ".join(toks) if toks else None


def _default_ambient_search(query: str, cwd: Path):
    """Best-effort KB rule search from the hook. Returns ``(title, file_path)``
    of the top hit or None. HEAVY (loads config + opens storage) — the caller
    runs it under a timeout and swallows every exception, so it must be safe to
    abandon mid-flight."""
    from ..backends import EntityType
    from ..config import load_config
    from ..wiring import build_verified_context

    config = load_config(project_root=_find_project_root(cwd))
    ctx = build_verified_context(config)
    hits = ctx.search.search(query, entity_types={EntityType.RULE}, limit=1)
    if not hits:
        return None
    row = ctx.storage.find_by_id(EntityType.RULE, hits[0].entity_id)
    if row is None:
        return None
    return (row.title, row.file_path)


def _ambient_recall(tool_name, event, session_state, cwd, *, search=None) -> Optional[str]:
    """Opt-in ambient recall. Returns one advisory line or None. NEVER raises,
    never blocks longer than AMBIENT_TIMEOUT_S. Mutates ``session_state`` (the
    per-session dedup list) only when enabled."""
    if not _ambient_enabled():
        return None
    query = _ambient_query(tool_name, event)
    if not query:
        return None
    suggested = session_state.setdefault("ambient_suggested", [])
    if len(suggested) >= AMBIENT_MAX_PER_SESSION:
        return None
    run = search or _default_ambient_search
    box: dict = {}

    def _work():
        try:
            box["r"] = run(query, cwd)
        except Exception:
            box["r"] = None

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(AMBIENT_TIMEOUT_S)
    hit = box.get("r")
    if not hit:
        return None
    title, file_path = hit
    key = file_path or title
    if not key or key in suggested:
        return None
    suggested.append(key)
    tail = f" (read_rule {file_path})" if file_path else ""
    return f"💡 relevant memory: {title}{tail}"


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

    # Opt-in ambient recall (#35). Never raises, never blocks; mutates `s` (the
    # dedup list) only when MCM_AMBIENT_RECALL is set, so it's a no-op otherwise.
    # Computed before _write_state so the per-session dedup list persists.
    try:
        ambient = _ambient_recall(tool_name, event, s, cwd)
    except Exception:
        ambient = None

    try:
        _write_state(sp, state)
    except OSError:
        # State write failures shouldn't block the user's tool call.
        pass

    # Diagnostic event log: record consultation-gaps + warns (not silent
    # allows or compliance resets) so the thresholds can be tuned from real
    # data. A gap is a would-have-blocked mutator at/over BLOCK_THRESHOLD;
    # under fail-open (#19) it is recorded, not blocked — exit stays 0.
    normalized = _normalize_builtin_tool(tool_name)
    at_gap = (
        normalized in BLOCKING_BUILTIN_TOOLS
        and s.get("mutator_calls", 0) >= BLOCK_THRESHOLD
    )
    action = "consultation_gap" if at_gap else ("warn" if message else "")
    if action:
        _append_event(_events_path(cwd), {
            "ts": time.time(),
            "session_id": session_id,
            "tool": tool_name,
            "action": action,
            "builtin_calls": s.get("builtin_calls", 0),
            "mutator_calls": s.get("mutator_calls", 0),
        })

    out = message
    if ambient:
        out = f"{out}\n{ambient}" if out else ambient
    if out:
        print(out, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
