"""Embedded SQLite SearchBackend — pinned to the shared SearchConformance,
plus the SQLite-specific plugin-search tests that exercise FTS5 virtual
tables and the LIKE fallback.

The MCM2-07 ``search_plugin`` tests are intentionally not part of the
shared conformance: each non-SQLite adapter will interpret SearchScope's
table-descriptor metadata against its own index (Postgres tsvector,
Meilisearch, etc.), and the seed mechanism differs per backend.
"""
from __future__ import annotations

import pytest

from mcm_engine.testing.conformance import SearchConformance


class TestSqliteSearch(SearchConformance):
    @pytest.fixture
    def _shared(self, tmp_path):
        from mcm_engine.adapters.sqlite.search import SqliteSearch
        from mcm_engine.adapters.sqlite.storage import SqliteStorage

        db_path = str(tmp_path / "search.db")
        storage = SqliteStorage(db_path=db_path)
        storage.ensure_schema()
        search = SqliteSearch(db_path=db_path)
        return storage, search

    @pytest.fixture
    def storage(self, _shared):
        return _shared[0]

    @pytest.fixture
    def search(self, _shared):
        return _shared[1]


# ---------------------------------------------------------------------------
# search_plugin — MCM2-07. Plugin-side SQL moved here from plugin.py.
# These are SQLite-specific because the descriptor names FTS5 virtual
# tables; other adapters provide their own plugin-search tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_db(tmp_path):
    """A KnowledgeDB with the mock plugin table + FTS table set up."""
    from mcm_engine.db import KnowledgeDB
    from mcm_engine.schema import migrate_core, migrate_plugin

    db = KnowledgeDB(tmp_path / "plugin.db")
    migrate_core(db)
    migrate_plugin(db, "mock", """
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
    """, 1)
    return db


def _make_scope():
    from mcm_engine.plugin import SearchScope
    return SearchScope(
        name="mock",
        label="MOCK",
        fts_table="mock_fts",
        base_table="mock_data",
        fts_columns=["title", "body"],
        display_columns=["title", "body"],
        like_columns=["title", "body"],
    )


def test_search_plugin_fts_path(plugin_db):
    """search_plugin runs FTS against the descriptor's fts_table."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch

    plugin_db.execute_write(
        "INSERT INTO mock_data (title, body) VALUES (?, ?)",
        ("WAL journal mode", "SQLite WAL is better for concurrency"),
    )
    plugin_db.commit()

    backend = SqliteSearch(db=plugin_db)
    results = backend.search_plugin(_make_scope(), "WAL journal", 10)
    assert results
    assert any("MOCK" in r for r in results)


def test_search_plugin_like_fallback(plugin_db):
    """When FTS5 chokes on hostile chars, LIKE fallback still finds the row."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch

    plugin_db.execute_write(
        "INSERT INTO mock_data (title, body) VALUES (?, ?)",
        ("special-chars: test", "body with colons:and:stuff"),
    )
    plugin_db.commit()

    backend = SqliteSearch(db=plugin_db)
    results = backend.search_plugin(_make_scope(), "special-chars", 10)
    assert results
    assert "MOCK" in results[0]


def test_search_plugin_default_formatter(plugin_db):
    """With format_fn=None the default '[LABEL] col1 | col2' format is used."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch

    plugin_db.execute_write(
        "INSERT INTO mock_data (title, body) VALUES (?, ?)",
        ("alpha", "beta"),
    )
    plugin_db.commit()

    backend = SqliteSearch(db=plugin_db)
    results = backend.search_plugin(_make_scope(), "alpha", 10)
    assert results
    assert results[0].startswith("[MOCK]")
    assert "alpha" in results[0]
    assert "beta" in results[0]


def test_search_plugin_custom_formatter(plugin_db):
    """When format_fn is provided the adapter delegates formatting to it."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    from mcm_engine.plugin import SearchScope

    plugin_db.execute_write(
        "INSERT INTO mock_data (title, body) VALUES (?, ?)",
        ("alpha", "beta"),
    )
    plugin_db.commit()

    scope = SearchScope(
        name="mock",
        label="MOCK",
        fts_table="mock_fts",
        base_table="mock_data",
        fts_columns=["title", "body"],
        display_columns=["title", "body"],
        like_columns=["title", "body"],
        format_fn=lambda r: f"CUSTOM::{r['title']}",
    )
    backend = SqliteSearch(db=plugin_db)
    results = backend.search_plugin(scope, "alpha", 10)
    assert results == ["CUSTOM::alpha"]


def test_search_plugin_no_match_returns_empty(plugin_db):
    from mcm_engine.adapters.sqlite.search import SqliteSearch

    backend = SqliteSearch(db=plugin_db)
    assert backend.search_plugin(_make_scope(), "zzqqnotanything", 10) == []


def test_search_scope_has_no_search_method():
    """SearchScope is a passive descriptor — no SQL on it post-MCM2-07."""
    from mcm_engine.plugin import SearchScope

    scope = SearchScope(
        name="mock",
        label="MOCK",
        fts_table="mock_fts",
        base_table="mock_data",
    )
    assert not hasattr(scope, "search"), (
        "SearchScope.search method removed in MCM2-07 — SQL moved to "
        "SearchBackend.search_plugin."
    )
