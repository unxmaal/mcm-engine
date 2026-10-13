"""Progressive disclosure (issue #119): search returns a compact one-line
summary per hit by default and the full body only on detail="full".

Red-proofed both directions per the gauntlet: the body MUST be absent in
summary and MUST be present in full; the identifying one-liner MUST survive in
both. A change that silently reverted to full-by-default would turn these red.
"""
from __future__ import annotations

import inspect

import pytest

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
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
def tools(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", mcp.tools.get("search"))
    return mcp.tools


_DETAIL = "LONGBODYTOKEN " * 200  # a big body, the dominant real token sink


def test_knowledge_summary_omits_detail_keeps_identity_and_tags(tools):
    tools["add_knowledge"](
        topic="Widget cache", summary="LWW per name", detail=_DETAIL,
        tags="cache,policy")
    summ = tools["search"]("Widget", detail="summary")
    # identity + one-line summary + tags survive
    assert "Widget cache" in summ and "LWW per name" in summ
    assert "Tags: cache,policy" in summ
    # the body does not
    assert "LONGBODYTOKEN" not in summ
    assert "Detail:" not in summ


def test_knowledge_full_includes_detail(tools):
    tools["add_knowledge"](
        topic="Widget cache", summary="LWW per name", detail=_DETAIL,
        tags="cache,policy")
    full = tools["search"]("Widget", detail="full")
    assert "LONGBODYTOKEN" in full and "Detail:" in full


def test_default_is_summary(tools):
    tools["add_knowledge"](topic="Widget cache", summary="s", detail=_DETAIL)
    assert "LONGBODYTOKEN" not in tools["search"]("Widget")


def test_negative_summary_omits_why_and_fix(tools):
    tools["add_negative"](
        category="DB pooling", what_failed="shared a connection across threads",
        why_failed="WHYBODYTOKEN interleaved transactions",
        correct_approach="FIXBODYTOKEN one connection per thread")
    summ = tools["search"]("pooling", scope="negative", detail="summary")
    assert "DB pooling" in summ and "shared a connection" in summ
    assert "WHYBODYTOKEN" not in summ and "FIXBODYTOKEN" not in summ
    full = tools["search"]("pooling", scope="negative", detail="full")
    assert "WHYBODYTOKEN" in full and "FIXBODYTOKEN" in full


def test_summary_carries_body_fetch_hint_full_does_not(tools):
    tools["add_knowledge"](topic="Widget cache", summary="s", detail=_DETAIL)
    summ = tools["search"]("Widget", detail="summary")
    full = tools["search"]("Widget", detail="full")
    assert "get_entry" in summ          # tells the caller how to pull the body
    assert "get_entry" not in full      # no footer when bodies are already inline


def test_summary_is_materially_smaller_than_full(tools):
    for i in range(5):
        tools["add_knowledge"](
            topic=f"Widget {i}", summary="one line", detail=_DETAIL, tags="t")
    summ = tools["search"]("Widget", detail="summary", limit=10)
    full = tools["search"]("Widget", detail="full", limit=10)
    # the whole point of #119: the default is a fraction of the full size
    assert len(summ) < 0.5 * len(full)


def test_both_entry_points_default_to_summary(tmp_path):
    # Gauntlet class 1 (one value, many places): the detail default lives in
    # ~8 signatures. The two that are load-bearing are the entry points —
    # the public `search` tool and `search_all`, which report_error calls.
    # If they drift apart, report_error silently reverts to full bodies, which
    # is the regression #119 exists to prevent. Pin both, asserted to agree.
    db = KnowledgeDB(str(tmp_path / "e.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    search_all = register_search_tools(mcp, db, SessionTracker(NudgeConfig()), [], "proj")
    search_default = inspect.signature(mcp.tools["search"]).parameters["detail"].default
    search_all_default = inspect.signature(search_all).parameters["detail"].default
    assert search_default == search_all_default == "summary"
