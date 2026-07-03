"""Issue #35 — ambient recall daemon (hook-as-muse), OPT-IN.

When MCM_AMBIENT_RECALL is set, a file-mutator event triggers a best-effort KB
search keyed on the edited path and surfaces the top rule hit as an advisory
stderr line. It never blocks, never changes the exit code, and is silent on any
error/timeout. Default OFF -> hook behavior is unchanged. Search is stubbed here
so no real backend is touched.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

from mcm_engine.hooks import mcp_enforcement as hook


def _event(tool="Edit", file_path="src/mcm_engine/scoring.py"):
    return {"tool_name": tool, "session_id": "s1",
            "tool_input": {"file_path": file_path}}


def test_disabled_by_default_is_a_noop(monkeypatch):
    monkeypatch.delenv(hook.AMBIENT_ENV, raising=False)
    s = {}
    out = hook._ambient_recall("Edit", _event(), s, Path("/tmp"),
                               search=lambda q, c: ("X", "cat/x.md"))
    assert out is None
    assert s == {}  # no state mutation when disabled


def test_enabled_surfaces_top_hit(monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")
    out = hook._ambient_recall(
        "Edit", _event(), {}, Path("/tmp"),
        search=lambda q, c: ("Scoring rule", "mcm-engine/scoring.md"))
    assert out and "relevant memory: Scoring rule" in out
    assert "read_rule mcm-engine/scoring.md" in out


def test_query_is_derived_from_the_path(monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")
    seen = {}

    def stub(q, c):
        seen["q"] = q
        return None

    hook._ambient_recall("Edit", _event(file_path="a/b/carb_ratio_sync.py"),
                         {}, Path("/tmp"), search=stub)
    assert {"carb", "ratio", "sync"} <= set(seen["q"].split())


def test_non_mutator_event_does_not_search(monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")
    called = {"n": 0}

    def stub(q, c):
        called["n"] += 1
        return ("X", "x")

    assert hook._ambient_recall(
        "Bash", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {}, Path("/tmp"), search=stub) is None
    assert called["n"] == 0


def test_search_error_is_silent(monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")

    def boom(q, c):
        raise RuntimeError("backend down")

    assert hook._ambient_recall("Edit", _event(), {}, Path("/tmp"), search=boom) is None


def test_rate_limited_no_repeat_suggestion(monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")
    s = {}
    stub = lambda q, c: ("Rule", "cat/same.md")
    first = hook._ambient_recall("Edit", _event(), s, Path("/tmp"), search=stub)
    second = hook._ambient_recall("Edit", _event(), s, Path("/tmp"), search=stub)
    assert first is not None and second is None


def test_main_appends_advisory_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(hook.AMBIENT_ENV, "1")
    monkeypatch.setattr(hook, "_default_ambient_search",
                        lambda q, c: ("Cache rule", "mcm-engine/cache.md"))
    event = json.dumps({"tool_name": "Edit", "session_id": "s",
                        "cwd": str(tmp_path),
                        "tool_input": {"file_path": str(tmp_path / "cache_thing.py")}})
    stderr = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(event)), patch.object(sys, "stderr", stderr):
        rc = hook.main()
    assert rc == 0
    assert "💡 relevant memory: Cache rule" in stderr.getvalue()


def test_main_no_advisory_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(hook.AMBIENT_ENV, raising=False)
    monkeypatch.setattr(hook, "_default_ambient_search", lambda q, c: ("X", "x"))
    event = json.dumps({"tool_name": "Edit", "session_id": "s",
                        "cwd": str(tmp_path),
                        "tool_input": {"file_path": str(tmp_path / "x.py")}})
    stderr = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(event)), patch.object(sys, "stderr", stderr):
        rc = hook.main()
    assert rc == 0
    assert "relevant memory" not in stderr.getvalue()
