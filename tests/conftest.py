"""Shared fixtures for mcm-engine tests."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mcm_engine.config import MCMConfig, NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.plugin import MCMPlugin, SearchScope
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker


@pytest.fixture
def tmp_dir(tmp_path):
    """A temporary directory for test files."""
    return tmp_path


@pytest.fixture
def db(tmp_path):
    """A KnowledgeDB instance with core schema applied."""
    db_path = tmp_path / "test.db"
    database = KnowledgeDB(db_path)
    migrate_core(database)
    return database


@pytest.fixture
def tracker():
    """A SessionTracker with default config."""
    return SessionTracker(NudgeConfig())


@pytest.fixture
def config(tmp_path):
    """A basic MCMConfig."""
    return MCMConfig(
        project_name="test-project",
        db_path=str(tmp_path / "test.db"),
    )


class MockPlugin(MCMPlugin):
    """A test plugin that creates a custom table and search scope."""

    @property
    def name(self) -> str:
        return "mock-plugin"

    @property
    def version(self) -> int:
        return 1

    def get_schema_sql(self) -> str:
        return """
        CREATE TABLE IF NOT EXISTS mock_data (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS mock_fts USING fts5(
            title, body,
            content='mock_data',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS mock_ai AFTER INSERT ON mock_data BEGIN
            INSERT INTO mock_fts(rowid, title, body)
            VALUES (new.id, new.title, new.body);
        END;
        """

    def get_search_scopes(self) -> list[SearchScope]:
        return [
            SearchScope(
                name="mock",
                label="MOCK",
                fts_table="mock_fts",
                base_table="mock_data",
                fts_columns=["title", "body"],
                display_columns=["title", "body"],
                like_columns=["title", "body"],
            )
        ]

    def on_session_start(self, db) -> dict[str, str]:
        row = db.execute("SELECT COUNT(*) as cnt FROM mock_data").fetchone()
        return {"Mock items": str(row["cnt"])}

    def get_nudge(self, tracker) -> str | None:
        if tracker.turn_count >= 5:
            return "MOCK NUDGE: Plugin says hello"
        return None


@pytest.fixture
def mock_plugin():
    """A MockPlugin instance."""
    return MockPlugin()


@pytest.fixture
def rules_dir(tmp_path):
    """A temporary rules directory."""
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def project_root(tmp_path):
    """A temporary project root with rules dir."""
    rules = tmp_path / "rules"
    rules.mkdir()
    return tmp_path
