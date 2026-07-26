"""Resume-context budget (c5 Phase 4).

The session_start / get_resume_context payload is already a summary, so the
budget is opt-in: defaults preserve full output; field_chars clips free-text
fields and max_pinned caps pinned lists when configured.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, KnowledgeRow
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


def _tools(db, **budget):
    mcp = _FakeMCP()
    register_session_tools(mcp, db, SessionTracker(NudgeConfig()), "proj", [],
                           plugin_db=db, **budget)
    return mcp.tools


@pytest.fixture
def db(tmp_path):
    d = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(d)
    return d


LONG = "x" * 200


def test_field_chars_clips_handoff_in_session_start(db):
    tools = _tools(db, field_chars=20)
    tools["session_handoff"](status="ok", current_task=LONG, next_steps="", blockers="")
    out = tools["session_start"]()
    assert "…[clipped]" in out
    assert LONG not in out


def test_default_preserves_full_handoff(db):
    tools = _tools(db)  # no budget
    tools["session_handoff"](status="ok", current_task=LONG, next_steps="", blockers="")
    out = tools["session_start"]()
    assert LONG in out and "…[clipped]" not in out


def test_max_pinned_caps_resume_pinned_list(db):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db=db)
    for i in range(3):
        kid = s.insert_knowledge(KnowledgeRow(id=0, topic=f"T{i}", summary="s"))
        s.set_pinned(EntityType.KNOWLEDGE, kid, True)
    tools = _tools(db, max_pinned=1)
    out = tools["get_resume_context"]()
    assert "and 2 more" in out


def test_default_shows_all_pinned(db):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db=db)
    for i in range(3):
        kid = s.insert_knowledge(KnowledgeRow(id=0, topic=f"T{i}", summary="s"))
        s.set_pinned(EntityType.KNOWLEDGE, kid, True)
    tools = _tools(db)
    out = tools["get_resume_context"]()
    assert "more" not in out
    assert out.count("[FINDING]") == 3
