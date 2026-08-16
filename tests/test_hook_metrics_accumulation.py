"""Per-session code-metric accumulation in the PreToolUse hook (Phase 2).

The hook tallies LOC added/removed, comment-vs-code split, churn (re-edits), and
edit-cycle depth from each mutator's tool_input into the shared state file.
Diagnostic only.
"""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

from mcm_engine.hooks.mcp_enforcement import _read_state, _state_path, main


def _invoke(tool_name, *, tool_input=None, session_id="s1", cwd=None):
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "session_id": session_id,
    }
    if cwd is not None:
        event["cwd"] = str(cwd)
    stdin = io.StringIO(json.dumps(event))
    stderr = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stderr", stderr):
        main()


def _metrics(tmp_path, session_id="s1"):
    return _read_state(_state_path(tmp_path)).get(session_id, {}).get("metrics", {})


def test_write_counts_added_and_comment_split(tmp_path):
    content = "# a comment\nx = 1\n\ny = 2  # trailing not a comment line"
    _invoke("Write", tool_input={"file_path": str(tmp_path / "m.py"), "content": content},
            cwd=tmp_path)
    m = _metrics(tmp_path)
    assert m["loc_added"] == 4              # 3 newlines -> 4 lines
    assert m["comment_lines_added"] == 1   # only the "# a comment" line
    assert m["code_lines_added"] == 2      # x=1 and y=2 (blank line skipped)


def test_edit_counts_added_and_removed(tmp_path):
    _invoke("Edit", tool_input={
        "file_path": str(tmp_path / "m.py"),
        "old_string": "a\nb\nc",     # 3 lines removed
        "new_string": "a\nb",         # 2 lines added
    }, cwd=tmp_path)
    m = _metrics(tmp_path)
    assert m["loc_removed"] == 3
    assert m["loc_added"] == 2


def test_reedit_same_file_counts_as_churn(tmp_path):
    f = str(tmp_path / "m.py")
    # First edit: not churn (file not previously touched this session).
    _invoke("Edit", tool_input={"file_path": f, "old_string": "a", "new_string": "b"},
            cwd=tmp_path)
    assert _metrics(tmp_path).get("loc_churned", 0) == 0
    # Second edit to the SAME file: counts as churn.
    _invoke("Edit", tool_input={"file_path": f, "old_string": "b\nb", "new_string": "c"},
            cwd=tmp_path)
    m = _metrics(tmp_path)
    assert m["loc_churned"] > 0
    assert m["edit_cycles_max"] == 2


def test_distinct_files_are_not_churn(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        _invoke("Edit", tool_input={
            "file_path": str(tmp_path / name), "old_string": "x", "new_string": "y",
        }, cwd=tmp_path)
    m = _metrics(tmp_path)
    assert m.get("loc_churned", 0) == 0
    assert m["edit_cycles_max"] == 1


def test_unknown_extension_counts_all_as_code(tmp_path):
    _invoke("Write", tool_input={
        "file_path": str(tmp_path / "notes.xyz"), "content": "# not a known comment\nline",
    }, cwd=tmp_path)
    m = _metrics(tmp_path)
    assert m["comment_lines_added"] == 0
    assert m["code_lines_added"] == 2


def test_bash_does_not_accumulate(tmp_path):
    _invoke("Bash", tool_input={"command": "pytest"}, cwd=tmp_path)
    assert _metrics(tmp_path) == {}


def test_first_seen_recorded(tmp_path):
    _invoke("Edit", tool_input={"file_path": str(tmp_path / "a.py"),
                                "old_string": "x", "new_string": "y"}, cwd=tmp_path)
    entry = _read_state(_state_path(tmp_path))["s1"]
    assert entry.get("first_seen_at")
