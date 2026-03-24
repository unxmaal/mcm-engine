"""Tests for KnowledgeDB — WAL mode, write retry, reconnect, FTS5."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mcm_engine.db import KnowledgeDB, build_fts_queries, build_like_patterns, sanitize_fts
from mcm_engine.schema import migrate_core


class TestKnowledgeDB:
    def test_creates_db_and_parent_dirs(self, tmp_path):
        db_path = tmp_path / "sub" / "dir" / "test.db"
        db = KnowledgeDB(db_path)
        assert db_path.exists()
        db.close()

    def test_wal_mode(self, db):
        row = db.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_busy_timeout(self, db):
        row = db.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000

    def test_synchronous_normal(self, db):
        row = db.execute("PRAGMA synchronous").fetchone()
        # synchronous=NORMAL returns 1
        assert row[0] == 1

    def test_row_factory(self, db):
        migrate_core(db)
        db.execute_write(
            "INSERT INTO knowledge (topic, kind, summary) VALUES (?, ?, ?)",
            ("test", "finding", "a summary"),
        )
        db.commit()
        row = db.execute("SELECT topic, summary FROM knowledge WHERE topic = 'test'").fetchone()
        assert row["topic"] == "test"
        assert row["summary"] == "a summary"

    def test_execute_write_retry_on_locked(self, db, tmp_path):
        """execute_write reconnects and retries on 'locked' OperationalError."""
        migrate_core(db)

        # Wrap the real connection in a proxy that fails once
        real_conn = db.conn
        call_count = 0

        class FailOnceProxy:
            """Proxy that makes the first INSERT raise, then delegates to real conn."""
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, params=()):
                nonlocal call_count
                if "INSERT" in sql and call_count == 0:
                    call_count += 1
                    raise sqlite3.OperationalError("database is locked")
                return real_conn.execute(sql, params)

        db.conn = FailOnceProxy()

        # execute_write should catch the locked error, call _reconnect (which
        # replaces db.conn with a real connection), then retry successfully
        db.execute_write(
            "INSERT INTO knowledge (topic, kind, summary) VALUES (?, ?, ?)",
            ("retry-test", "finding", "retried"),
        )
        db.commit()
        row = db.execute("SELECT topic FROM knowledge WHERE topic = 'retry-test'").fetchone()
        assert row["topic"] == "retry-test"
        assert call_count == 1  # Confirms the first attempt did fail

    def test_reconnect(self, tmp_path):
        db_path = tmp_path / "reconnect.db"
        db = KnowledgeDB(db_path)
        old_conn = db.conn
        db._reconnect()
        # Connection object should be different
        assert db.conn is not old_conn
        db.close()


class TestSanitizeFts:
    def test_basic_terms(self):
        assert sanitize_fts("hello world") == '"hello" "world"'

    def test_special_chars(self):
        # Hyphens and colons are FTS5 special chars
        assert sanitize_fts("foo-bar baz:qux") == '"foo-bar" "baz:qux"'

    def test_single_term(self):
        assert sanitize_fts("test") == '"test"'

    def test_empty(self):
        assert sanitize_fts("") == ""


class TestBuildFtsQueries:
    def test_single_term(self):
        queries = build_fts_queries("dlmalloc")
        assert len(queries) == 1
        assert queries[0] == '"dlmalloc"'

    def test_multi_term_and_then_or(self):
        queries = build_fts_queries("slang libm")
        assert len(queries) >= 2
        assert queries[0] == '"slang" "libm"'  # AND
        assert queries[1] == '"slang" OR "libm"'  # OR

    def test_noise_words_filtered(self):
        queries = build_fts_queries("how to convert the SRPM")
        # "how", "to", "the" are noise — only "convert" and "SRPM" survive
        assert queries[0] == '"convert" "SRPM"'

    def test_all_noise_uses_raw(self):
        queries = build_fts_queries("the a an")
        # All filtered → falls back to raw terms
        assert len(queries) >= 1

    def test_empty_returns_empty(self):
        assert build_fts_queries("") == []

    def test_prefix_queries_for_multi_term(self):
        queries = build_fts_queries("glib proxy")
        # Should have AND, OR, and prefix queries
        assert any("*" in q for q in queries)


class TestBuildLikePatterns:
    def test_basic(self):
        patterns = build_like_patterns("slang libm convert")
        assert patterns == ["%slang%", "%libm%", "%convert%"]

    def test_short_words_filtered(self):
        patterns = build_like_patterns("to be or not")
        # "to", "be", "or" are ≤2 chars, "not" is 3 chars
        assert "%not%" in patterns

    def test_all_short_uses_raw(self):
        patterns = build_like_patterns("ab cd")
        # All ≤2 chars → fallback uses raw
        assert len(patterns) == 2
