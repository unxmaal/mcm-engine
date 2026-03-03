"""Tests for plugin system — schema, tool registration, search scopes, nudges."""
import pytest

from mcm_engine.config import MCMConfig, NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.plugin import MCMPlugin, SearchScope
from mcm_engine.schema import migrate_core, migrate_plugin
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.search import register_search_tools

from .conftest import MockPlugin


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


class TestPluginSchema:
    def test_creates_tables(self, db, mock_plugin):
        schema_sql = mock_plugin.get_schema_sql()
        migrate_plugin(db, mock_plugin.name, schema_sql, mock_plugin.version)

        # Table should exist
        db.execute_write(
            "INSERT INTO mock_data (title, body) VALUES (?, ?)",
            ("test title", "test body"),
        )
        db.commit()
        row = db.execute("SELECT * FROM mock_data LIMIT 1").fetchone()
        assert row["title"] == "test title"

    def test_tracks_version(self, db, mock_plugin):
        schema_sql = mock_plugin.get_schema_sql()
        migrate_plugin(db, mock_plugin.name, schema_sql, mock_plugin.version)

        row = db.execute(
            "SELECT version FROM _mcm_versions WHERE component = ?",
            (f"plugin:{mock_plugin.name}",),
        ).fetchone()
        assert row["version"] == 1


class TestPluginSearchScopes:
    def test_search_finds_plugin_data(self, db, mock_plugin):
        # Set up plugin schema
        schema_sql = mock_plugin.get_schema_sql()
        migrate_plugin(db, mock_plugin.name, schema_sql, mock_plugin.version)

        # Insert test data
        db.execute_write(
            "INSERT INTO mock_data (title, body) VALUES (?, ?)",
            ("WAL journal mode", "SQLite WAL is better for concurrency"),
        )
        db.commit()

        # Set up search with plugin scopes
        mcp = FakeMCP()
        tracker = SessionTracker(NudgeConfig(store_reminder_turns=100))
        scopes = mock_plugin.get_search_scopes()
        search_all_fn = register_search_tools(mcp, db, tracker, scopes)

        # Search should find plugin data
        result = mcp["search"](query="WAL journal")
        assert "MOCK" in result


class TestPluginNudges:
    def test_plugin_nudge_fires(self, mock_plugin):
        tracker = SessionTracker(NudgeConfig(
            store_reminder_turns=100,
            checkpoint_turns=100,
            mandatory_stop_turns=200,
        ))
        tracker.register_plugin_nudge(mock_plugin.get_nudge)

        # Under threshold
        for _ in range(4):
            tracker.record_call("search")
        tracker.record_store()
        assert tracker.get_nudge() is None

        # At threshold
        tracker.record_call("search")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert "MOCK NUDGE" in nudge


class TestPluginSessionStart:
    def test_on_session_start(self, db, mock_plugin):
        schema_sql = mock_plugin.get_schema_sql()
        migrate_plugin(db, mock_plugin.name, schema_sql, mock_plugin.version)

        extra = mock_plugin.on_session_start(db)
        assert "Mock items" in extra
        assert extra["Mock items"] == "0"

        # Add data and check again
        db.execute_write("INSERT INTO mock_data (title, body) VALUES ('a', 'b')")
        db.commit()
        extra = mock_plugin.on_session_start(db)
        assert extra["Mock items"] == "1"


class TestSearchScope:
    def test_like_fallback(self, db, mock_plugin):
        """When FTS5 fails, LIKE fallback should still work."""
        schema_sql = mock_plugin.get_schema_sql()
        migrate_plugin(db, mock_plugin.name, schema_sql, mock_plugin.version)

        db.execute_write(
            "INSERT INTO mock_data (title, body) VALUES (?, ?)",
            ("special-chars: test", "body with colons:and:stuff"),
        )
        db.commit()

        scope = mock_plugin.get_search_scopes()[0]
        # Use a query that won't match FTS but will match LIKE
        results = scope.search(db, "special-chars", '"special-chars"', "%special-chars%", 10)
        assert len(results) > 0
        assert "MOCK" in results[0]
