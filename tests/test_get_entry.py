"""get_entry — point read of one entry by id (issue #112).

Covers hit (rendered like a scroll_entries block), NOT_FOUND, all four types,
and read-only nudge accounting.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.corpus import register_corpus_tools
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
    s = SqliteStorage(db=db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, tracker)
    return mcp.tools, s, tracker


def test_hit_renders_like_a_scroll_block(env):
    tools, s, _t = env
    kid = s.insert_knowledge(
        KnowledgeRow(id=0, topic="widget", summary="a widget finding"))
    out = tools["get_entry"]("knowledge", kid)
    assert f"#{kid} [knowledge] widget" in out
    assert "summary: a widget finding" in out
    assert "hash=" in out


def test_not_found(env):
    tools, _s, _t = env
    out = tools["get_entry"]("knowledge", 999)
    assert "NOT_FOUND" in out and "id=999" in out


def test_all_four_types(env):
    tools, s, _t = env
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="k", summary="ks"))
    nid = s.insert_negative(NegativeRow(id=0, category="c", what_failed="wf"))
    eid = s.insert_error(ErrorRow(id=0, pattern="boom"))
    rid = s.insert_rule(RuleRow(id=0, title="R", keywords="r"))
    assert "[knowledge] k" in tools["get_entry"]("knowledge", kid)
    assert "[negative] c" in tools["get_entry"]("negative", nid)
    assert "[error] boom" in tools["get_entry"]("error", eid)
    assert "[rule] R" in tools["get_entry"]("rule", rid)


def test_read_only_accounting(env):
    tools, s, tracker = env
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="k", summary="ks"))
    tools["get_entry"]("knowledge", kid)
    assert "get_entry" in tracker.READ_ONLY_TOOLS
    assert tracker.calls_since.get("get_entry", 0) == 0
