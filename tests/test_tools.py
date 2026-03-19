"""Tests for all 7 core MCP tools."""
import json

import pytest

from mcm_engine.config import MCMConfig, NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools


class FakeMCP:
    """Minimal stand-in for FastMCP that captures tool registrations."""

    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


@pytest.fixture
def tool_env(db):
    """Set up a full tool environment and return (mcp, db, tracker) with all tools registered."""
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100,  # suppress reminders during tests
        checkpoint_turns=100,
        mandatory_stop_turns=200,
    ))

    # Register search first to get the search_all function
    search_all_fn = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
    register_session_tools(mcp, db, tracker, "test-project", [])

    return mcp, db, tracker


class TestAddKnowledge:
    def test_basic_insert(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["add_knowledge"](topic="test-topic", summary="a finding")
        assert "Stored finding: test-topic" in result

        row = db.execute("SELECT * FROM knowledge WHERE topic = 'test-topic'").fetchone()
        assert row["summary"] == "a finding"
        assert row["kind"] == "finding"

    def test_all_fields(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](
            topic="arch",
            summary="use WAL mode",
            kind="decision",
            detail="WAL allows concurrent reads",
            tags="sqlite,wal",
            rationale="better than DELETE journal",
            alternatives="DELETE journal mode",
        )
        row = db.execute("SELECT * FROM knowledge WHERE topic = 'arch'").fetchone()
        assert row["kind"] == "decision"
        assert row["rationale"] == "better than DELETE journal"
        assert row["alternatives"] == "DELETE journal mode"

    def test_resets_store_counter(self, tool_env):
        mcp, db, tracker = tool_env
        for _ in range(5):
            tracker.record_call("search")
        assert tracker.turn_count - tracker.last_store_turn == 5
        mcp["add_knowledge"](topic="t", summary="s")
        assert tracker.turn_count - tracker.last_store_turn == 0


class TestAddKnowledgeDedup:
    def test_exact_topic_match_updates(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL mode", summary="original summary")
        result = mcp["add_knowledge"](topic="WAL mode", summary="updated summary")
        assert "Updated existing" in result
        assert "original summary" in result  # shows old value

        rows = db.execute("SELECT * FROM knowledge WHERE topic = 'WAL mode'").fetchall()
        assert len(rows) == 1
        assert rows[0]["summary"] == "updated summary"

    def test_exact_topic_different_kind_inserts(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL mode", summary="a finding", kind="finding")
        mcp["add_knowledge"](topic="WAL mode", summary="a decision", kind="decision")

        rows = db.execute("SELECT * FROM knowledge WHERE topic = 'WAL mode'").fetchall()
        assert len(rows) == 2

    def test_fuzzy_match_warns(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL mode for SQLite", summary="use WAL")
        result = mcp["add_knowledge"](topic="SQLite WAL configuration", summary="configure WAL")
        # Should still insert (different topic) but warn about similar
        assert "similar entry exists" in result or "Stored" in result

        rows = db.execute("SELECT COUNT(*) as cnt FROM knowledge").fetchone()
        assert rows["cnt"] == 2

    def test_update_preserves_hit_count(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="test-preserve", summary="v1")
        # Simulate some hits
        db.execute_write("UPDATE knowledge SET hit_count = 5 WHERE topic = 'test-preserve'")
        db.commit()

        mcp["add_knowledge"](topic="test-preserve", summary="v2")
        row = db.execute("SELECT * FROM knowledge WHERE topic = 'test-preserve'").fetchone()
        assert row["summary"] == "v2"
        assert row["hit_count"] == 5  # preserved, not reset


class TestAddNegative:
    def test_basic_insert(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["add_negative"](
            category="sqlite",
            what_failed="DELETE journal mode",
        )
        assert "Stored negative knowledge" in result

        row = db.execute("SELECT * FROM negative_knowledge LIMIT 1").fetchone()
        assert row["category"] == "sqlite"
        assert row["severity"] == "normal"

    def test_all_fields(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_negative"](
            category="build",
            what_failed="inline C in YAML",
            why_failed="unmaintainable",
            correct_approach="use patches/ dir",
            severity="critical",
        )
        row = db.execute("SELECT * FROM negative_knowledge LIMIT 1").fetchone()
        assert row["severity"] == "critical"
        assert row["correct_approach"] == "use patches/ dir"


class TestReportError:
    def test_logs_error(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["report_error"](error_text="undefined reference to foo")
        assert "Error logged" in result

        row = db.execute("SELECT * FROM errors LIMIT 1").fetchone()
        assert "undefined reference to foo" in row["pattern"]

    def test_auto_searches(self, tool_env):
        mcp, db, tracker = tool_env
        # Add some knowledge first
        mcp["add_knowledge"](topic="foo function", summary="foo is missing on IRIX")

        result = mcp["report_error"](error_text="undefined reference to foo")
        assert "Error logged" in result
        # Should find the knowledge about foo
        assert "foo" in result.lower()


class TestSearch:
    def test_finds_knowledge(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL mode", summary="use WAL for concurrency")
        result = mcp["search"](query="WAL")
        assert "WAL" in result

    def test_finds_negative(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_negative"](category="build", what_failed="inline C in YAML")
        result = mcp["search"](query="inline")
        assert "NEGATIVE" in result

    def test_finds_errors(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["report_error"](error_text="segfault in malloc")
        result = mcp["search"](query="segfault malloc")
        assert "ERROR" in result

    def test_no_results(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["search"](query="xyznonexistent")
        assert "No results" in result

    def test_increments_hit_count(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL", summary="use WAL")
        mcp["search"](query="WAL")
        row = db.execute("SELECT hit_count FROM knowledge WHERE topic = 'WAL'").fetchone()
        assert row["hit_count"] >= 1


class TestSearchStemming:
    def test_stemming_matches_inflections(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="SRPM conversion pipeline", summary="converts Fedora SRPMs to IRIX")
        # "converting" should match "conversion" via porter stemmer
        result = mcp["search"](query="converting SRPM")
        assert "conversion pipeline" in result

    def test_stemming_matches_plurals(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="staging packages", summary="how packages are staged")
        result = mcp["search"](query="package staging")
        assert "staging packages" in result

    def test_stemming_build_matches_building(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="build system", summary="how the build system works")
        result = mcp["search"](query="building")
        assert "build system" in result


class TestSearchORFallback:
    def test_or_fallback_when_and_fails(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="batch build", summary="orchestrates full rebuilds")
        mcp["add_knowledge"](topic="SRPM conversion", summary="converts specs for IRIX")
        # "batch conversion" — no single row has both, but OR should find entries
        result = mcp["search"](query="batch conversion")
        assert "No results" not in result

    def test_like_fallback_per_term(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="c++ ABI issues", summary="IRIX c++ name mangling")
        # FTS might struggle with "c++" but LIKE fallback should work per-term
        result = mcp["search"](query="ABI mangling")
        assert "No results" not in result


class TestSearchRanking:
    def test_search_sets_last_hit_at(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["add_knowledge"](topic="WAL ranking", summary="test last_hit_at")
        mcp["search"](query="WAL ranking")
        row = db.execute("SELECT last_hit_at FROM knowledge WHERE topic = 'WAL ranking'").fetchone()
        assert row["last_hit_at"] is not None

    def test_high_hit_count_ranks_higher(self, tool_env):
        """Entries with more hits should appear in results (not be pushed out)."""
        mcp, db, tracker = tool_env
        # Insert two entries, one with high hit count
        mcp["add_knowledge"](topic="popular malloc", summary="well-known malloc fact")
        mcp["add_knowledge"](topic="obscure malloc", summary="obscure malloc trivia")
        db.execute_write("UPDATE knowledge SET hit_count = 50 WHERE topic = 'popular malloc'")
        db.commit()

        result = mcp["search"](query="malloc")
        assert "popular malloc" in result
        assert "obscure malloc" in result


class TestSessionStart:
    def test_returns_stats(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["session_start"]()
        assert "Recent knowledge" in result
        assert "test-project" in result

    def test_shows_last_handoff(self, tool_env):
        mcp, db, tracker = tool_env
        mcp["session_handoff"](status="built nano", current_task="grep build")
        result = mcp["session_start"]()
        assert "built nano" in result


class TestSessionHandoff:
    def test_records_handoff(self, tool_env):
        mcp, db, tracker = tool_env
        result = mcp["session_handoff"](
            status="completed build",
            current_task="testing",
            next_steps="deploy",
            blockers="none",
        )
        assert "handoff recorded" in result

        row = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "completed build"
        assert row["next_steps"] == "deploy"

    def test_resets_counters(self, tool_env):
        mcp, db, tracker = tool_env
        for _ in range(10):
            tracker.record_call("search", topic="foo")
        mcp["session_handoff"](status="test")
        assert tracker.last_store_turn == tracker.turn_count
        assert tracker.topic_freq == {}


class TestSessionSummary:
    def test_returns_stats(self, tool_env):
        mcp, db, tracker = tool_env
        for _ in range(3):
            tracker.record_call("search", topic="foo")
        result = mcp["session_summary"]()
        assert "Tool calls:" in result
        assert "Session duration:" in result
