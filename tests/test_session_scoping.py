"""issue #116: sessions carry a scope (principal + optional workspace) so a
shared store resumes the right session instead of the global latest. Covers the
v16→v17 migration, fresh-install columns, and the scoped read with NULL-scope
fallback. SQLite is authoritative here (the migration + query logic); the
Postgres path mirrors it and is exercised in CI.
"""
from __future__ import annotations

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import SessionRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import CORE_VERSION, migrate_core


def _cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v16_to_v17_adds_sessions_scope_columns(tmp_path):
    db = KnowledgeDB(str(tmp_path / "old.db"))
    # Simulate a pre-v17 store: a sessions table without the scope columns,
    # stamped at core v16 (sqlite cannot DROP COLUMN, so build it bare).
    db.executescript(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
        "current_task TEXT, findings_summary TEXT, next_steps TEXT, blockers TEXT, "
        "context_snapshot TEXT, created_at TEXT);"
        "CREATE TABLE IF NOT EXISTS _mcm_versions (component TEXT PRIMARY KEY, "
        "version INTEGER, updated_at TEXT);"
    )
    db.execute_write(
        "INSERT INTO _mcm_versions (component, version) VALUES ('core', 16)")
    db.commit()
    assert "principal" not in _cols(db, "sessions")

    migrate_core(db)

    cols = _cols(db, "sessions")
    assert "principal" in cols and "workspace" in cols
    ver = db.execute(
        "SELECT version FROM _mcm_versions WHERE component='core'").fetchone()
    assert ver["version"] == CORE_VERSION == 17


def test_fresh_install_has_sessions_scope_columns(tmp_path):
    db = KnowledgeDB(str(tmp_path / "fresh.db"))
    migrate_core(db)
    assert {"principal", "workspace"} <= _cols(db, "sessions")


def _store(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "s.db"))
    s.ensure_schema()
    return s


def _handoff(store, status, principal=None, workspace=None):
    return store.insert_session(SessionRow(
        id=0, status=status, principal=principal, workspace=workspace))


def test_scoped_read_returns_this_principals_latest(tmp_path):
    store = _store(tmp_path)
    _handoff(store, "alice-1", principal="alice")
    _handoff(store, "bob-latest", principal="bob")  # globally newest
    _handoff(store, "alice-2", principal="alice")
    # bob is the global latest, but alice must resume alice's own session.
    got = store.get_last_session(principal="alice")
    assert got is not None and got.status == "alice-2"
    assert store.get_last_session(principal="bob").status == "bob-latest"


def test_null_scope_is_a_fallback_only_when_no_scoped_row(tmp_path):
    store = _store(tmp_path)
    old_global = _handoff(store, "legacy", principal=None)  # pre-migration row
    # A principal with no row of their own falls back to the global/unscoped row.
    got = store.get_last_session(principal="carol")
    assert got is not None and got.status == "legacy"
    # But once carol has her own, the scoped row wins over the NULL fallback.
    _handoff(store, "carol-1", principal="carol")
    assert store.get_last_session(principal="carol").status == "carol-1"
    assert old_global  # (ids are truthy)


def test_unscoped_read_is_global_latest(tmp_path):
    store = _store(tmp_path)
    _handoff(store, "a", principal="alice")
    _handoff(store, "b", principal="bob")
    # No scope key (local / unauthenticated) keeps the original behavior.
    assert store.get_last_session().status == "b"


def test_workspace_separates_one_principals_projects(tmp_path):
    store = _store(tmp_path)
    _handoff(store, "projA", principal="alice", workspace="projA")
    _handoff(store, "projB", principal="alice", workspace="projB")
    got = store.get_last_session(principal="alice", workspace="projA")
    assert got is not None and got.status == "projA"


def test_scope_round_trips_through_hydration(tmp_path):
    store = _store(tmp_path)
    _handoff(store, "s", principal="dave", workspace="w1")
    got = store.get_last_session(principal="dave")
    assert got.principal == "dave" and got.workspace == "w1"
