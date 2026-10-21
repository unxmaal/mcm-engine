"""Retrieval output caps (issue #171): in summary mode the inline NEGATIVE/ERROR
body fields are length-capped, and the multi-scope "all" path bounds how many
NEGATIVE/ERROR hits surface. detail="full" and scoped queries are uncapped.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import ErrorRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
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
def tools(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", mcp.tools.get("search"))
    return mcp.tools


_LONG = "WORDA " * 200  # ~1200 chars, well over the 320 inline cap


def test_negative_inline_fields_capped_in_summary(tools):
    tools["add_negative"](
        category="pooling", what_failed="shared a connection across threads",
        why_failed="WHYHEAD " + _LONG, correct_approach="FIXHEAD " + _LONG)
    summ = tools["search"]("pooling", scope="negative", detail="summary")
    # head of each field survives; the tail is truncated with a get_entry pointer
    assert "WHYHEAD" in summ and "FIXHEAD" in summ
    assert "get_entry('negative'," in summ
    assert summ.count("WORDA") < 150   # two capped fields, far below the ~400 full
    # full stays uncapped
    full = tools["search"]("pooling", scope="negative", detail="full")
    assert full.count("WORDA") >= 200
    assert "get_entry('negative'," not in full


def test_error_inline_fields_capped_in_summary(tmp_path):
    db = KnowledgeDB(str(tmp_path / "e.db"))
    migrate_core(db)
    ctx = coerce_context(db)
    ctx.storage.insert_error(ErrorRow(
        id=0, pattern="CONNREFUSED on boot",
        root_cause="ROOTHEAD " + _LONG, fix="FIXHEAD " + _LONG))
    mcp = _FakeMCP()
    register_search_tools(mcp, ctx, SessionTracker(NudgeConfig()), [], "proj")
    summ = mcp.tools["search"]("CONNREFUSED", scope="errors", detail="summary")
    assert "ROOTHEAD" in summ and "FIXHEAD" in summ
    assert "get_entry('error'," in summ
    assert summ.count("WORDA") < 150   # two capped fields, far below the ~400 full


def test_neg_err_count_capped_in_all_summary_but_not_scoped(tools):
    # Six NEGATIVEs that all match the query token.
    for i in range(6):
        tools["add_negative"](
            category=f"c{i}", what_failed=f"deploytoken failure number {i}",
            why_failed="short why", correct_approach="short fix")
    all_summ = tools["search"]("deploytoken", scope="all", detail="summary", limit=10)
    assert all_summ.count("[NEGATIVE #") <= 3     # bounded in the multi-scope path
    # A scoped query keeps the caller's limit (no multi-scope crowding concern).
    neg_only = tools["search"]("deploytoken", scope="negative", detail="summary", limit=10)
    assert neg_only.count("[NEGATIVE #") == 6


def test_clean_knowledge_rule_hits_not_capped(tools):
    # KNOWLEDGE one-liners are cheap and must stay complete in the "all" path.
    for i in range(6):
        tools["add_knowledge"](topic=f"kdeploytoken {i}", summary=f"line {i}")
    out = tools["search"]("kdeploytoken", scope="all", detail="summary", limit=10)
    assert out.count("[KNOWLEDGE/") == 6
