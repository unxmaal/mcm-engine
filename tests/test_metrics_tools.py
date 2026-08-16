"""record_session_metrics + session_metrics_report MCP tools (Phase 2c)."""
from __future__ import annotations

import pytest

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.metrics import register_metrics_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_metrics_tools(mcp, db, tracker, project_name="mcm")
    return mcp.tools, db, tracker


def test_record_and_report(env):
    tools, db, _t = env
    out = tools["record_session_metrics"](
        cc_session_id="sess-1", out_tokens=1200, loc_added=40, loc_removed=5,
        loc_churned=8, comment_lines_added=10, code_lines_added=30,
        fixation_events=2, edit_cycles_max=3,
    )
    assert "Recorded session_metrics for sess-1" in out
    # project defaulted from project_name
    row = db.execute("SELECT project, out_tokens, ended_at FROM session_metrics "
                     "WHERE cc_session_id='sess-1'").fetchone()
    assert row["project"] == "mcm" and row["out_tokens"] == 1200
    assert row["ended_at"]  # stamped now() by storage, not NULL

    rep = tools["session_metrics_report"]()
    assert "sess-1"[:8] in rep
    assert "out-tok" in rep and "comments 25%" in rep  # 10/(10+30)=25%


def test_record_requires_session_id(env):
    tools, _db, _t = env
    assert "cc_session_id is required" in tools["record_session_metrics"](cc_session_id="")


def test_record_is_upsert(env):
    tools, db, _t = env
    tools["record_session_metrics"](cc_session_id="s", out_tokens=100)
    tools["record_session_metrics"](cc_session_id="s", out_tokens=999, loc_added=7)
    n = db.execute("SELECT COUNT(*) AS c FROM session_metrics").fetchone()["c"]
    assert n == 1
    row = db.execute("SELECT out_tokens, loc_added FROM session_metrics").fetchone()
    assert row["out_tokens"] == 999 and row["loc_added"] == 7


def test_report_empty(env):
    tools, _db, _t = env
    assert "No session metrics recorded yet." in tools["session_metrics_report"]()


def test_report_median_delta_with_several(env):
    tools, _db, _t = env
    for i, tok in enumerate([1000, 1000, 5000]):  # latest (last recorded) = 5000
        tools["record_session_metrics"](cc_session_id=f"s{i}", out_tokens=tok,
                                        loc_added=10)
    rep = tools["session_metrics_report"]()
    assert "latest vs median of prior" in rep


def test_report_is_read_only():
    assert "session_metrics_report" in SessionTracker.READ_ONLY_TOOLS
