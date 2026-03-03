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
