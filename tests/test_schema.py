"""Tests for schema migrations."""
import pytest

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import (
    CORE_VERSION,
    _has_column,
    migrate_core,
)


class TestMigrationFramework:
    def test_fresh_install_at_latest_version(self, tmp_path):
        db = KnowledgeDB(tmp_path / "fresh.db")
        migrate_core(db)

        row = db.execute(
            "SELECT version FROM _mcm_versions WHERE component = 'core'"
        ).fetchone()
        assert row["version"] == CORE_VERSION

    def test_fresh_install_has_last_hit_at(self, tmp_path):
        """Fresh install should have last_hit_at on knowledge and rules."""
        db = KnowledgeDB(tmp_path / "fresh.db")
        migrate_core(db)

        assert _has_column(db, "knowledge", "last_hit_at")
        assert _has_column(db, "rules", "last_hit_at")

    def test_v2_to_v3_migration(self, tmp_path):
        """Simulate a v2 database and verify migration adds last_hit_at."""
        db = KnowledgeDB(tmp_path / "v2.db")

        # Create a v2 schema manually (without last_hit_at columns)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY,
                topic TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'finding',
                summary TEXT NOT NULL,
                detail TEXT,
                tags TEXT,
                project TEXT,
                rationale TEXT,
                alternatives TEXT,
                hit_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS negative_knowledge (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                what_failed TEXT NOT NULL,
                why_failed TEXT,
                correct_approach TEXT,
                severity TEXT DEFAULT 'normal',
                project TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY,
                pattern TEXT NOT NULL,
                context TEXT,
                root_cause TEXT,
                fix TEXT,
                tags TEXT,
                project TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                current_task TEXT,
                findings_summary TEXT,
                next_steps TEXT,
                blockers TEXT,
                context_snapshot TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS _mcm_versions (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                keywords TEXT NOT NULL,
                file_path TEXT,
                description TEXT,
                category TEXT,
                hit_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        db.execute_write(
            "INSERT INTO _mcm_versions (component, version) VALUES ('core', 2)"
        )
        # Insert some data to verify it survives migration
        db.execute_write(
            "INSERT INTO knowledge (topic, summary) VALUES ('test', 'survives migration')"
        )
        db.commit()

        # Verify no last_hit_at yet
        assert not _has_column(db, "knowledge", "last_hit_at")
        assert not _has_column(db, "rules", "last_hit_at")

        # Run migration
        migrate_core(db)

        # Verify migration happened
        assert _has_column(db, "knowledge", "last_hit_at")
        assert _has_column(db, "rules", "last_hit_at")

        # Verify version bumped
        row = db.execute(
            "SELECT version FROM _mcm_versions WHERE component = 'core'"
        ).fetchone()
        assert row["version"] == CORE_VERSION

        # Verify data survived
        row = db.execute("SELECT * FROM knowledge WHERE topic = 'test'").fetchone()
        assert row["summary"] == "survives migration"
        assert row["last_hit_at"] is None  # new column, not yet hit

    def test_idempotent_migration(self, tmp_path):
        """Running migrate_core twice should be safe."""
        db = KnowledgeDB(tmp_path / "idempotent.db")
        migrate_core(db)
        migrate_core(db)  # Should not error

        row = db.execute(
            "SELECT version FROM _mcm_versions WHERE component = 'core'"
        ).fetchone()
        assert row["version"] == CORE_VERSION

    def test_has_column_utility(self, tmp_path):
        db = KnowledgeDB(tmp_path / "util.db")
        db.executescript("CREATE TABLE test_table (id INTEGER, name TEXT)")
        assert _has_column(db, "test_table", "id")
        assert _has_column(db, "test_table", "name")
        assert not _has_column(db, "test_table", "nonexistent")
