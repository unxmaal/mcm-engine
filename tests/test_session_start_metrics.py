"""session_start efficiency retro block (Phase 3). Read-only, diagnostic."""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import SessionMetricsRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _env(tmp_path, *, enabled=True):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    mcp = _FakeMCP()
    register_session_tools(mcp, db, SessionTracker(NudgeConfig()), "proj", [],
                           metrics_enabled=enabled, metrics_report_limit=3)
    return mcp.tools, s


def _seed(s):
    s.upsert_session_metrics(SessionMetricsRow(
        id=0, cc_session_id="abcd1234ef", out_tokens=1500, loc_added=40,
        loc_removed=5, loc_churned=8, comment_lines_added=10, code_lines_added=30,
        fixation_events=2))


def test_block_appears_when_enabled_and_rows_exist(tmp_path):
    tools, s = _env(tmp_path, enabled=True)
    _seed(s)
    out = tools["session_start"]()
    assert "Efficiency (recent sessions, diagnostic)" in out
    assert "abcd1234" in out
    assert "comments 25%" in out
    assert "Not a score to beat" in out


def test_block_absent_when_disabled(tmp_path):
    tools, s = _env(tmp_path, enabled=False)
    _seed(s)
    out = tools["session_start"]()
    assert "Efficiency (recent sessions" not in out


def test_block_absent_when_no_rows(tmp_path):
    tools, _s = _env(tmp_path, enabled=True)
    out = tools["session_start"]()
    assert "Efficiency (recent sessions" not in out
