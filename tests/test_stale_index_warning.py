"""issue #109: a failed startup file->DB sync must be surfaced at the point of
use. The server records it on the shared Context (`stale_index_warning`); search
and session_start prefix their responses with it. Red-proofed both directions:
a set warning MUST appear, an unset one MUST NOT (a stale index that is silent
is the whole defect).
"""
from __future__ import annotations

import pytest

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker
from mcm_engine.wiring import coerce_context


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools_and_ctx(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    # One shared Context passed to BOTH registrations, so a warning the server
    # would set on it is visible to both tools (coerce_context returns the same
    # instance for a Context).
    ctx = coerce_context(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_search_tools(mcp, ctx, tracker, [], "proj")
    register_session_tools(mcp, ctx, tracker, "proj", [])
    return mcp.tools, ctx


def test_search_warns_when_index_stale(tools_and_ctx):
    tools, ctx = tools_and_ctx
    ctx.stale_index_warning = "startup file->DB sync failed (database is locked)"
    out = tools["search"]("anything")
    assert "[STALE INDEX]" in out
    assert "database is locked" in out


def test_search_clean_when_index_trusted(tools_and_ctx):
    tools, ctx = tools_and_ctx
    assert ctx.stale_index_warning is None
    assert "[STALE INDEX]" not in tools["search"]("anything")


def test_session_start_warns_when_index_stale(tools_and_ctx):
    tools, ctx = tools_and_ctx
    ctx.stale_index_warning = "startup file->DB sync failed (disk I/O error)"
    out = tools["session_start"]()
    assert "[STALE INDEX]" in out
    assert "disk I/O error" in out


def test_session_start_clean_when_index_trusted(tools_and_ctx):
    tools, ctx = tools_and_ctx
    assert "[STALE INDEX]" not in tools["session_start"]()
