"""Fixation breaker in the PreToolUse hook (session-metrics work).

An advisory per-turn rabbit-hole detector: after N consecutive edits to the same
file with no compliance read in between, the hook emits a reframe on stderr.
Fail-open — it never changes the exit code.
"""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import pytest

from mcm_engine.hooks.mcp_enforcement import (
    _events_path,
    _read_state,
    _state_path,
    main,
)


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _invoke(tool_name, *, file_path=None, session_id="s1", cwd=None):
    """Drive main() with a synthetic PreToolUse event; return (exit, stderr)."""
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)} if file_path else {},
        "session_id": session_id,
    }
    if cwd is not None:
        event["cwd"] = str(cwd)
    stdin = io.StringIO(json.dumps(event))
    stderr = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stderr", stderr):
        rc = main()
    return rc, stderr.getvalue()


@pytest.fixture(autouse=True)
def _threshold_3(monkeypatch):
    # A low threshold keeps the tests fast and explicit.
    monkeypatch.setenv("MCM_FIXATION_THRESHOLD", "3")


def test_fires_at_threshold_and_is_advisory(tmp_path):
    f = tmp_path / "widget.py"
    outs = [_invoke("Edit", file_path=f, cwd=tmp_path) for _ in range(3)]
    # Always fail-open.
    assert all(rc == 0 for rc, _ in outs)
    # First two edits: no reframe. Third (== threshold): reframe.
    assert "FIXATION CHECK" not in outs[0][1]
    assert "FIXATION CHECK" not in outs[1][1]
    assert "FIXATION CHECK" in outs[2][1]
    assert str(f) in outs[2][1]


def test_below_threshold_is_silent(tmp_path):
    f = tmp_path / "a.py"
    for _ in range(2):
        _, err = _invoke("Edit", file_path=f, cwd=tmp_path)
        assert "FIXATION CHECK" not in err


def test_compliance_read_resets_the_run(tmp_path):
    f = tmp_path / "a.py"
    _invoke("Edit", file_path=f, cwd=tmp_path)
    _invoke("Edit", file_path=f, cwd=tmp_path)
    _invoke("mcp__mcm-engine__search", cwd=tmp_path)  # look-first read
    # Two more edits should NOT fire yet (run restarted after the read).
    _, e1 = _invoke("Edit", file_path=f, cwd=tmp_path)
    _, e2 = _invoke("Edit", file_path=f, cwd=tmp_path)
    assert "FIXATION CHECK" not in e1
    assert "FIXATION CHECK" not in e2
    # The third post-reset edit fires.
    _, e3 = _invoke("Edit", file_path=f, cwd=tmp_path)
    assert "FIXATION CHECK" in e3


def test_switching_files_breaks_the_streak(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    seq = [a, a, b, a, b, a]  # never 3 consecutive on one file
    fired = False
    for f in seq:
        _, err = _invoke("Edit", file_path=f, cwd=tmp_path)
        fired = fired or ("FIXATION CHECK" in err)
    assert not fired


def test_bash_between_edits_does_not_break_streak(tmp_path):
    f = tmp_path / "a.py"
    _invoke("Edit", file_path=f, cwd=tmp_path)
    _invoke("Bash", cwd=tmp_path)  # a test run between edits: same locus focus
    _invoke("Edit", file_path=f, cwd=tmp_path)
    _, err = _invoke("Edit", file_path=f, cwd=tmp_path)  # 3rd edit to a.py
    assert "FIXATION CHECK" in err


def test_disabled_when_threshold_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_FIXATION_THRESHOLD", "0")
    f = tmp_path / "a.py"
    fired = False
    for _ in range(6):
        _, err = _invoke("Edit", file_path=f, cwd=tmp_path)
        fired = fired or ("FIXATION CHECK" in err)
    assert not fired


def test_fixation_event_is_logged(tmp_path):
    f = tmp_path / "a.py"
    for _ in range(3):
        _invoke("Edit", file_path=f, cwd=tmp_path)
    events = _read_events(_events_path(tmp_path))
    fix = [e for e in events if e.get("action") == "fixation"]
    assert len(fix) == 1
    assert fix[0]["run"] == 3 and fix[0]["locus"] == str(f)


def test_bash_only_never_fixates(tmp_path):
    for _ in range(6):
        _, err = _invoke("Bash", cwd=tmp_path)
        assert "FIXATION CHECK" not in err
    state = _read_state(_state_path(tmp_path))
    assert state["s1"].get("fixation_run", 0) == 0
