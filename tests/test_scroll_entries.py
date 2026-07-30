"""scroll_entries — paged, read-only corpus enumerate (issue #104).

Covers keyset paging (after_id cursor), the per-call cap, read-only nudge
accounting, and that all four entity types enumerate.
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
    s = SqliteStorage(db=db)  # shares the connection with the tool's context
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, tracker)
    return mcp.tools, s, tracker


def _seed_knowledge(s, n):
    ids = []
    for i in range(n):
        ids.append(s.insert_knowledge(
            KnowledgeRow(id=0, topic=f"topic {i}", summary=f"summary {i}")))
    return ids


def test_empty_corpus(env):
    tools, _s, _t = env
    out = tools["scroll_entries"]("knowledge")
    assert "No knowledge entries" in out and "End of corpus" in out


def test_enumerates_all_four_types(env):
    tools, s, _t = env
    s.insert_knowledge(KnowledgeRow(id=0, topic="k", summary="ks"))
    s.insert_negative(NegativeRow(id=0, category="c", what_failed="wf"))
    s.insert_error(ErrorRow(id=0, pattern="boom"))
    s.insert_rule(RuleRow(id=0, title="R", keywords="r"))
    for et, needle in [("knowledge", "[knowledge] k"),
                       ("negative", "[negative] c"),
                       ("error", "[error] boom"),
                       ("rule", "[rule] R")]:
        out = tools["scroll_entries"](et)
        assert needle in out, f"{et}: {out}"


def test_keyset_paging(env):
    tools, s, _t = env
    ids = _seed_knowledge(s, 5)
    page1 = tools["scroll_entries"]("knowledge", after_id=0, limit=2)
    assert "topic 0" in page1 and "topic 1" in page1 and "topic 2" not in page1
    assert f"after_id={ids[1]}" in page1  # cursor is the last id on the page
    page2 = tools["scroll_entries"]("knowledge", after_id=ids[1], limit=2)
    assert "topic 2" in page2 and "topic 3" in page2 and "topic 1" not in page2
    page3 = tools["scroll_entries"]("knowledge", after_id=ids[3], limit=2)
    assert "topic 4" in page3 and "likely last page" in page3


def test_limit_capped_by_env(env, monkeypatch):
    tools, s, _t = env
    _seed_knowledge(s, 10)
    monkeypatch.setenv("MCM_SCROLL_PAGE_MAX", "3")
    out = tools["scroll_entries"]("knowledge", after_id=0, limit=100)
    # Cap 3 wins over the requested 100.
    assert "cap 3" in out
    assert out.count("[knowledge]") == 3


def test_content_hash_present_and_stable(env):
    tools, s, _t = env
    _seed_knowledge(s, 1)
    a = tools["scroll_entries"]("knowledge")
    b = tools["scroll_entries"]("knowledge")
    assert "hash=" in a and a == b


def test_read_only_does_not_advance_store_deficit(env):
    tools, _s, tracker = env
    before = dict(tracker.calls_since)
    tools["scroll_entries"]("knowledge")
    # A read-only tool resets its own counter and must not push the store
    # deficit toward a block; it should behave like search/list_rules.
    assert "scroll_entries" in tracker.READ_ONLY_TOOLS
    # calls_since for scroll_entries is 0 right after firing.
    assert tracker.calls_since.get("scroll_entries", 0) == 0
    _ = before
