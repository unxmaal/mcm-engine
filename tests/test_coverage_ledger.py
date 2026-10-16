"""Per-session, per-topic retrieval coverage ledger (issue #156).

The foundation layer of the retrieval-scheduling umbrella (#137): the server
records which topics were retrieved this session and whether that coverage is
still RESIDENT. `resident` is distinct from `searched_at` — a topic stays
covered for the whole session, but its retrieved bodies stop being resident at
a compaction boundary (save_snapshot compaction, session_start), so coverage
never silently masks the anti-amnesia case. No enforcement reads this yet (#159).
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import ErrorRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import ScopedTracker, SessionTracker


# --- pure SessionTracker unit: record ---------------------------------------


def test_record_coverage_stores_topic_query_ids_resident():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("Postgres locking", "postgres lock", [("RULE", 1), ("KNOWLEDGE", 2)])
    entry = t.coverage["postgres locking"]
    assert entry["topic"] == "postgres locking"
    assert entry["query"] == "postgres lock"
    assert entry["result_ids"] == [("RULE", 1), ("KNOWLEDGE", 2)]
    assert entry["resident"] is True
    assert entry["searched_at"] > 0


def test_record_coverage_key_is_lowercased_stripped():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("  Widget Cache  ", "q", [])
    assert "widget cache" in t.coverage


def test_record_coverage_overwrites_same_topic():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("topic", "q1", [("RULE", 1)])
    first = t.coverage["topic"]["searched_at"]
    t.record_coverage("topic", "q2", [("RULE", 9)])
    entry = t.coverage["topic"]
    assert entry["query"] == "q2"
    assert entry["result_ids"] == [("RULE", 9)]
    assert entry["searched_at"] >= first
    assert len(t.coverage) == 1


def test_record_coverage_empty_topic_is_noop():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("", "q", [("RULE", 1)])
    t.record_coverage("   ", "q", [("RULE", 1)])
    assert t.coverage == {}


def test_record_coverage_none_ids_is_empty_list():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("topic", "q", None)
    assert t.coverage["topic"]["result_ids"] == []


# --- pure SessionTracker unit: is_covered + reconcile -----------------------


def test_is_covered_requires_resident_by_default():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("topic", "q", [])
    assert t.is_covered("topic") is True
    assert t.is_covered("TOPIC") is True  # key normalized
    assert t.is_covered("missing") is False


def test_reconcile_flips_resident_false_and_is_covered_follows():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("a", "q", [])
    t.record_coverage("b", "q", [])
    t.reconcile_coverage(resident=False)
    assert t.coverage["a"]["resident"] is False
    assert t.coverage["b"]["resident"] is False
    # non-resident coverage does not satisfy the default (resident) check...
    assert t.is_covered("a") is False
    # ...but the topic was still searched this session.
    assert t.is_covered("a", require_resident=False) is True


def test_reconcile_preserves_searched_at_and_ids():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("a", "q", [("RULE", 1)])
    before = dict(t.coverage["a"])
    t.reconcile_coverage(resident=False)
    after = t.coverage["a"]
    assert after["searched_at"] == before["searched_at"]
    assert after["result_ids"] == before["result_ids"]
    assert after["query"] == before["query"]


def test_reset_all_clears_coverage():
    t = SessionTracker(NudgeConfig())
    t.record_coverage("a", "q", [])
    t.reset_all()
    assert t.coverage == {}


# --- per-session isolation through ScopedTracker ----------------------------


class _Session:
    """Weak-referenceable stand-in for a FastMCP ServerSession."""


class _Provider:
    def __init__(self):
        self.current = None

    def __call__(self):
        return self.current


def test_coverage_is_per_session():
    p = _Provider()
    st = ScopedTracker(NudgeConfig(), key_provider=p)
    a, b = _Session(), _Session()

    p.current = a
    st.record_coverage("shared-topic", "q", [("RULE", 1)])
    p.current = b
    # b has never searched this topic.
    assert st.is_covered("shared-topic") is False

    p.current = a
    assert st.is_covered("shared-topic") is True


def test_reconcile_only_affects_calling_session():
    p = _Provider()
    st = ScopedTracker(NudgeConfig(), key_provider=p)
    a, b = _Session(), _Session()

    p.current = a
    st.record_coverage("t", "q", [])
    p.current = b
    st.record_coverage("t", "q", [])

    p.current = a
    st.reconcile_coverage(resident=False)

    assert st.is_covered("t") is False  # a reconciled
    p.current = b
    assert st.is_covered("t") is True  # b untouched


# --- tool wiring: search / report_error record; session_start reconciles ----


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def wired(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    # register_search_tools returns the internal search_all fn; report_error
    # takes that (not the public search tool), matching server.py wiring.
    search_all_fn = register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", search_all_fn)
    register_session_tools(mcp, db, tracker, "proj", [])
    return mcp.tools, tracker


def test_search_tool_records_resident_coverage_with_ids(wired):
    tools, tracker = wired
    tools["add_knowledge"](topic="Widget cache", summary="LWW per name", detail="x" * 50)
    tools["search"]("Widget")
    entry = tracker.coverage["widget"]
    assert entry["resident"] is True
    assert ("KNOWLEDGE", 1) in entry["result_ids"]


def test_search_with_no_results_still_records_coverage(wired):
    tools, tracker = wired
    tools["search"]("nonexistenttoken")
    entry = tracker.coverage["nonexistenttoken"]
    assert entry["resident"] is True
    assert entry["result_ids"] == []


def test_report_error_records_coverage(wired):
    tools, tracker = wired
    tools["report_error"](error_text="connection timeout to postgres host")
    # topic key is the lowercased error signature (first 50 chars).
    assert any("connection timeout" in k for k in tracker.coverage)


def test_session_start_reconciles_coverage_non_resident(wired):
    tools, tracker = wired
    tools["search"]("Widget")
    assert tracker.is_covered("widget") is True
    tools["session_start"]()
    # a post-compaction session_start marks prior coverage non-resident.
    assert tracker.is_covered("widget") is False
    assert tracker.is_covered("widget", require_resident=False) is True


def test_save_snapshot_compaction_reconciles_routine_does_not(wired):
    tools, tracker = wired
    tools["search"]("Widget")
    tools["save_snapshot"](progress="routine checkpoint")
    assert tracker.is_covered("widget") is True  # routine: untouched
    tools["save_snapshot"](progress="boundary", compaction=True)
    assert tracker.is_covered("widget") is False  # compaction: non-resident
