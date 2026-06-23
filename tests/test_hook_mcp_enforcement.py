"""Tests for the PreToolUse enforcement hook.

The hook's contract:
  - Built-in tool calls (Edit, Write, NotebookEdit, Bash) increment a
    per-session counter at <cwd>/.claude/mcp-enforcement-state.json.
  - Compliance MCP reads (search, report_error, sync_rules, session_start,
    get_resume_context, read_rule on ANY server name) reset the counter.
  - Warning at 8 built-in calls without a compliance read.
  - Block (exit 2) at 20 for Edit/Write/NotebookEdit. Bash never blocks.
  - Server-name agnostic: works for mcp__mcm-engine__search,
    mcp__knowledge__search, mcp__anything-else__search.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mcm_engine.hooks.mcp_enforcement import (
    BLOCK_THRESHOLD,
    STATE_TTL_SECONDS,
    WARN_THRESHOLD,
    _decide,
    _find_project_root,
    _is_compliance_mcp_tool,
    _normalize_builtin_tool,
    _prune_stale,
    _read_state,
    _state_path,
    main,
)


# ---------------------------------------------------------------------------
# Server-name regex — must handle whatever the user names their MCP server.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", [
    "mcp__mcm-engine__search",
    "mcp__knowledge__search",
    "mcp__my-cool-server__search",
    "mcp__server.with.dots__search",
])
def test_compliance_tool_matches_any_server_name(tool_name):
    assert _is_compliance_mcp_tool(tool_name) is True


@pytest.mark.parametrize("compliance_tool", [
    "search",
    "report_error",
    "sync_rules",
    "session_start",
    "get_resume_context",
    "read_rule",
])
def test_each_documented_compliance_tool_matches(compliance_tool):
    assert _is_compliance_mcp_tool(f"mcp__mcm-engine__{compliance_tool}") is True


@pytest.mark.parametrize("non_compliance", [
    "mcp__mcm-engine__add_rule",         # writes don't reset the counter
    "mcp__mcm-engine__add_knowledge",
    "mcp__mcm-engine__pin_item",
    "mcp__mcm-engine__save_snapshot",
    "mcm-engine_add_rule",               # opencode-style write doesn't reset either
    "mcm-engine_pin_item",
    "Edit",                               # built-in, not MCP
    "Bash",
    "mcp__mcm-engine__",                  # malformed
    "",
])
def test_non_compliance_tools_do_not_match(non_compliance):
    assert _is_compliance_mcp_tool(non_compliance) is False


# ---------------------------------------------------------------------------
# Counter behavior — _decide is the pure-function core.
# ---------------------------------------------------------------------------


def _empty_state():
    return {"builtin_calls": 0, "last_reset_at": 0.0}


def test_compliance_read_resets_counter():
    s = {"builtin_calls": 15, "last_reset_at": 0.0}
    exit_code, msg = _decide("mcp__mcm-engine__search", s)
    assert exit_code == 0
    assert msg == ""
    assert s["builtin_calls"] == 0


def test_compliance_read_resets_even_at_block_threshold():
    """Even past the block point, an MCP read counts. The model can get
    unstuck by calling search."""
    s = {"builtin_calls": BLOCK_THRESHOLD + 5, "last_reset_at": 0.0}
    exit_code, _ = _decide("mcp__mcm-engine__report_error", s)
    assert exit_code == 0
    assert s["builtin_calls"] == 0


def test_edit_below_warn_threshold_is_silent():
    s = _empty_state()
    for _ in range(WARN_THRESHOLD - 1):
        exit_code, msg = _decide("Edit", s)
        assert exit_code == 0
        assert msg == ""
    assert s["builtin_calls"] == WARN_THRESHOLD - 1


def test_edit_at_warn_threshold_emits_warning_but_allows():
    s = {"builtin_calls": WARN_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, msg = _decide("Edit", s)
    assert exit_code == 0
    assert "8/20" in msg or str(WARN_THRESHOLD) in msg
    assert s["builtin_calls"] == WARN_THRESHOLD


def test_edit_at_block_threshold_is_blocked():
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, msg = _decide("Edit", s)
    assert exit_code == 2
    assert "BLOCKED" in msg
    assert s["builtin_calls"] == BLOCK_THRESHOLD


def test_write_blocked_same_as_edit():
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, _ = _decide("Write", s)
    assert exit_code == 2


def test_notebook_edit_blocked_same_as_edit():
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, _ = _decide("NotebookEdit", s)
    assert exit_code == 2


def test_bash_counts_but_never_blocks():
    """Bash-heavy sessions are common (git, grep, ls). Bash contributes
    to the budget but isn't itself subject to the block."""
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, msg = _decide("Bash", s)
    assert exit_code == 0           # not blocked
    assert s["builtin_calls"] == BLOCK_THRESHOLD
    # But the warning message should still appear above WARN_THRESHOLD.
    assert msg != ""


def test_uncounted_tool_does_nothing():
    s = {"builtin_calls": 5, "last_reset_at": 0.0}
    exit_code, msg = _decide("Read", s)
    assert exit_code == 0
    assert msg == ""
    assert s["builtin_calls"] == 5    # untouched


# ---------------------------------------------------------------------------
# Project-root discovery for state file location.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [".git", "pyproject.toml", ".claude"])
def test_find_project_root_walks_up_to_marker(tmp_path, marker):
    """A deep cwd inside a project resolves to the marker's directory."""
    project = tmp_path / "myproject"
    project.mkdir()
    if marker == "pyproject.toml":
        (project / marker).write_text("[project]\nname = 'x'\n", encoding="utf-8")
    else:
        (project / marker).mkdir()
    deep = project / "src" / "nested" / "deeper"
    deep.mkdir(parents=True)

    assert _find_project_root(deep) == project.resolve()


def test_find_project_root_returns_cwd_when_no_marker(tmp_path):
    """Without any marker in the ancestor chain, fall back to the original
    cwd so the state file lands somewhere predictable rather than at /."""
    deep = tmp_path / "no" / "markers" / "here"
    deep.mkdir(parents=True)

    assert _find_project_root(deep) == deep


def test_find_project_root_does_not_ascend_past_home(tmp_path, monkeypatch):
    """Walk must stop at $HOME to avoid clobbering /Users/ or /. Even if a
    marker exists above HOME, the walk must not surface it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Marker placed ABOVE the fake home — it should NOT be discovered.
    (tmp_path / ".git").mkdir()
    deep = fake_home / "projects" / "foo"
    deep.mkdir(parents=True)

    assert _find_project_root(deep) == deep


def test_find_project_root_inner_marker_wins(tmp_path):
    """If an inner repo is nested under an umbrella project that also has a
    marker, the inner one wins — each repo gets its own state."""
    umbrella = tmp_path / "umbrella"
    umbrella.mkdir()
    (umbrella / ".claude").mkdir()
    inner = umbrella / "repo"
    inner.mkdir()
    (inner / ".git").mkdir()
    deep = inner / "src"
    deep.mkdir()

    assert _find_project_root(deep) == inner.resolve()


def test_state_path_uses_discovered_root(tmp_path):
    """End-to-end: state file lands under the project root, not at the
    arbitrary deep cwd where a built-in tool happened to fire."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    deep = project / "themes" / "art-nouveau-jade" / "fonts"
    deep.mkdir(parents=True)

    sp = _state_path(deep)
    assert sp == project.resolve() / ".claude" / "mcp-enforcement-state.json"


# ---------------------------------------------------------------------------
# End-to-end main() via stdin/stdout
# ---------------------------------------------------------------------------


def _invoke(tool_name, *, session_id="test-session-uuid", cwd=None) -> tuple[int, str]:
    """Drive main() with a synthetic PreToolUse event, return (exit, stderr)."""
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {},
        "session_id": session_id,
    }
    if cwd is not None:
        event["cwd"] = str(cwd)

    stdin = io.StringIO(json.dumps(event))
    stderr = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stderr", stderr):
        rc = main()
    return rc, stderr.getvalue()


def test_main_persists_state_to_disk(tmp_path):
    rc, _ = _invoke("Edit", cwd=tmp_path)
    assert rc == 0

    state = _read_state(_state_path(tmp_path))
    assert "test-session-uuid" in state
    assert state["test-session-uuid"]["builtin_calls"] == 1


def test_main_accumulates_across_calls(tmp_path):
    for _ in range(5):
        rc, _ = _invoke("Bash", cwd=tmp_path)
        assert rc == 0
    state = _read_state(_state_path(tmp_path))
    assert state["test-session-uuid"]["builtin_calls"] == 5


def test_main_blocks_after_threshold(tmp_path):
    """Run BLOCK_THRESHOLD Edit calls; the last one should exit 2."""
    rcs = []
    for _ in range(BLOCK_THRESHOLD):
        rc, _ = _invoke("Edit", cwd=tmp_path)
        rcs.append(rc)
    assert rcs[-1] == 2
    # Earlier calls were allowed (possibly with warnings).
    assert all(rc == 0 for rc in rcs[: BLOCK_THRESHOLD - 1])


def test_main_compliance_read_resets_disk_state(tmp_path):
    for _ in range(BLOCK_THRESHOLD - 1):
        _invoke("Edit", cwd=tmp_path)
    _invoke("mcp__mcm-engine__search", cwd=tmp_path)
    state = _read_state(_state_path(tmp_path))
    assert state["test-session-uuid"]["builtin_calls"] == 0


def test_main_separate_sessions_have_separate_counters(tmp_path):
    for _ in range(5):
        _invoke("Edit", cwd=tmp_path, session_id="session-A")
    for _ in range(2):
        _invoke("Edit", cwd=tmp_path, session_id="session-B")

    state = _read_state(_state_path(tmp_path))
    assert state["session-A"]["builtin_calls"] == 5
    assert state["session-B"]["builtin_calls"] == 2


def test_main_handles_malformed_event_gracefully():
    """A garbage stdin payload must fail open — never brick the harness."""
    stdin = io.StringIO("not-valid-json")
    stderr = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stderr", stderr):
        rc = main()
    assert rc == 0


def test_main_handles_empty_stdin():
    stdin = io.StringIO("")
    stderr = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stderr", stderr):
        rc = main()
    assert rc == 0


def test_main_handles_corrupt_state_file(tmp_path):
    """If the state file gets corrupted, the hook should still work."""
    sp = _state_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not valid json {{{", encoding="utf-8")

    rc, _ = _invoke("Edit", cwd=tmp_path)
    assert rc == 0
    # And the state file should have been rebuilt.
    state = _read_state(sp)
    assert state["test-session-uuid"]["builtin_calls"] == 1


# ---------------------------------------------------------------------------
# State file pruning — old session entries get dropped so the file doesn't
# grow forever.
# ---------------------------------------------------------------------------


# A realistic "now" timestamp comfortably past `STATE_TTL_SECONDS` from
# the unix epoch, so `now - ttl` stays positive and an epoch-0 entry
# actually qualifies as stale.
_NOW = 1_700_000_000.0


def test_prune_drops_entries_older_than_ttl():
    state = {
        "fresh-session": {"builtin_calls": 3, "last_reset_at": _NOW - 60},
        "stale-session": {"builtin_calls": 7, "last_reset_at": _NOW - STATE_TTL_SECONDS - 1},
        "ancient-session": {"builtin_calls": 1, "last_reset_at": 0.0},
    }
    _prune_stale(state, now=_NOW)
    assert "fresh-session" in state
    assert "stale-session" not in state
    assert "ancient-session" not in state


def test_prune_keeps_entries_at_ttl_boundary():
    """Entries exactly at the boundary stay — only strictly older drops."""
    state = {
        "boundary-session": {"builtin_calls": 1, "last_reset_at": _NOW - STATE_TTL_SECONDS},
    }
    _prune_stale(state, now=_NOW)
    assert "boundary-session" in state


def test_prune_tolerates_missing_last_reset_at():
    """An entry missing last_reset_at counts as fresh (we don't know when
    it was last touched, so don't penalize it)."""
    state = {"weird-session": {"builtin_calls": 5}}
    _prune_stale(state, now=_NOW)
    assert "weird-session" in state


def test_prune_tolerates_non_dict_entries():
    """If something garbage ended up in the state file, don't blow up."""
    state = {"weird-session": "not-a-dict", "good-session": {"last_reset_at": _NOW}}
    _prune_stale(state, now=_NOW)
    # The non-dict entry is kept untouched (we only know how to evaluate dicts).
    assert "good-session" in state


def test_main_invocation_prunes_stale_entries(tmp_path):
    """End-to-end: a stale entry on disk gets evicted when the hook fires."""
    sp = _state_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({
        "stale": {"builtin_calls": 99, "last_reset_at": 0.0},
        "fresh": {"builtin_calls": 2, "last_reset_at": 9_999_999_999.0},  # year 2286
    }), encoding="utf-8")

    rc, _ = _invoke("Edit", cwd=tmp_path, session_id="brand-new")
    assert rc == 0

    state = _read_state(sp)
    assert "stale" not in state
    assert "fresh" in state
    assert "brand-new" in state


# ---------------------------------------------------------------------------
# CLI subcommand — `mcm-engine hook` must route to the hook's main().
# ---------------------------------------------------------------------------


def test_mcm_engine_hook_subcommand_invokes_hook(tmp_path, monkeypatch):
    """`mcm-engine hook` reads stdin and runs the enforcement script. The
    user-facing wiring in settings.local.json points at this command;
    if it stops dispatching, every hook in the world silently breaks."""
    from mcm_engine.cli import main as cli_main

    event = json.dumps({
        "tool_name": "Edit",
        "session_id": "cli-route-test",
        "cwd": str(tmp_path),
    })
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))

    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0

    state = _read_state(_state_path(tmp_path))
    assert state["cli-route-test"]["builtin_calls"] == 1


# ---------------------------------------------------------------------------
# opencode compatibility — lowercase built-in names + <server>_<tool> MCP.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opencode_tool", [
    "edit",
    "write",
    "bash",
    "apply_patch",
])
def test_opencode_lowercase_builtins_recognized(opencode_tool):
    assert _normalize_builtin_tool(opencode_tool) == opencode_tool


@pytest.mark.parametrize("claude_tool, normalized", [
    ("Edit", "edit"),
    ("Write", "write"),
    ("Bash", "bash"),
    ("NotebookEdit", "notebookedit"),
])
def test_claude_capitalized_builtins_normalize_to_lowercase(claude_tool, normalized):
    assert _normalize_builtin_tool(claude_tool) == normalized


def test_unknown_tool_does_not_normalize():
    assert _normalize_builtin_tool("read") is None       # opencode read is NOT counted
    assert _normalize_builtin_tool("Read") is None       # nor is Claude's Read
    assert _normalize_builtin_tool("grep") is None
    assert _normalize_builtin_tool("") is None


@pytest.mark.parametrize("opencode_mcp_tool", [
    "mcm-engine_search",
    "knowledge_report_error",
    "any-server-name_sync_rules",
    "myserver_session_start",
    "myserver_get_resume_context",
    "myserver_read_rule",
])
def test_opencode_mcp_compliance_format_recognized(opencode_mcp_tool):
    assert _is_compliance_mcp_tool(opencode_mcp_tool) is True


@pytest.mark.parametrize("bare_tool", [
    "search",
    "report_error",
    "sync_rules",
    "session_start",
    "get_resume_context",
    "read_rule",
])
def test_bare_compliance_name_recognized(bare_tool):
    """Useful for tests + harnesses that don't prefix at all."""
    assert _is_compliance_mcp_tool(bare_tool) is True


def test_opencode_apply_patch_blocked_at_threshold():
    """apply_patch is opencode-only and mutates files — it must be subject
    to the block, same as edit/write."""
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, msg = _decide("apply_patch", s)
    assert exit_code == 2
    assert "BLOCKED" in msg


def test_opencode_lowercase_edit_blocked_at_threshold():
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, _ = _decide("edit", s)
    assert exit_code == 2


def test_opencode_lowercase_bash_counts_but_does_not_block():
    s = {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 0.0}
    exit_code, _ = _decide("bash", s)
    assert exit_code == 0
    assert s["builtin_calls"] == BLOCK_THRESHOLD


def test_opencode_mcp_search_resets_counter():
    s = {"builtin_calls": 15, "last_reset_at": 0.0}
    exit_code, _ = _decide("mcm-engine_search", s)
    assert exit_code == 0
    assert s["builtin_calls"] == 0


def test_mixed_session_claude_and_opencode_naming(tmp_path):
    """Pathological scenario: a session that somehow sees both naming
    styles (impossible in practice, but proves the hook is style-agnostic
    end-to-end). All file mutations must count regardless of casing."""
    for tool in ["Edit", "edit", "Write", "write", "Bash", "bash"]:
        _invoke(tool, cwd=tmp_path, session_id="mixed")
    state = _read_state(_state_path(tmp_path))
    assert state["mixed"]["builtin_calls"] == 6


def test_mcm_engine_hook_subcommand_propagates_block_exit(tmp_path, monkeypatch):
    """If the hook decides to block (exit 2), the CLI must surface that
    exit code unchanged — otherwise the agent harness can't tell a block
    from an allow."""
    from mcm_engine.cli import main as cli_main

    # Pre-load state past the block threshold.
    sp = _state_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({
        "blocked-session": {"builtin_calls": BLOCK_THRESHOLD - 1, "last_reset_at": 9e9},
    }), encoding="utf-8")

    event = json.dumps({
        "tool_name": "Edit",
        "session_id": "blocked-session",
        "cwd": str(tmp_path),
    })
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))

    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 2
