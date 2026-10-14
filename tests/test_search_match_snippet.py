"""Match snippet in summary mode (issue #135): a search hit carries a short
highlight of WHY it matched (the matched passage), so summary-mode relevance is
legible without fetching the body. Follow-up to #119.

The load-bearing property: a term that lives ONLY in the deferred body still
shows up in summary mode, via the snippet — without #135 the body is omitted
entirely, so the term is absent and these go red.
"""
from __future__ import annotations

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


def test_summary_surfaces_a_body_only_match_via_snippet(tools):
    # SNIPPETTERM is only in the detail body, which summary mode omits. The
    # snippet is the only way it can appear in a summary result.
    tools["add_knowledge"](
        topic="Widget cache", summary="LWW per name",
        detail="background prose then the SNIPPETTERM constraint then more prose")
    summ = tools["search"]("SNIPPETTERM", detail="summary")
    assert "Widget cache" in summ       # identity still there
    assert "SNIPPETTERM" in summ        # the body match is surfaced by the snippet


def test_snippet_is_length_bounded(tools):
    # A huge body must not drag a huge snippet back into summary mode (that
    # would undo the #119 token win). The snippet is capped.
    big = "FILLERWORD " * 500
    tools["add_knowledge"](
        topic="Big", summary="s",
        detail=f"{big} SNIPPETTERM {big}")
    summ = tools["search"]("SNIPPETTERM", detail="summary")
    # the hit line carries a snippet, but nowhere near the full body
    assert "SNIPPETTERM" in summ
    assert summ.count("FILLERWORD") < 60   # bounded, not the whole 500-word body


def test_like_fallback_hit_has_no_snippet_and_still_renders(tools):
    # A query that matches only via the LIKE fallback (no FTS rank) produces no
    # snippet; the hit must still render its identity cleanly (snippet is
    # optional, issue #135).
    tools["add_knowledge"](topic="EdgeCaseTopic", summary="plain", detail="body")
    summ = tools["search"]("EdgeCaseTopic", detail="summary")
    assert "EdgeCaseTopic" in summ
