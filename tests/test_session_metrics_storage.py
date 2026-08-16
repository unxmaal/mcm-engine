"""session_metrics storage: upsert + list round-trip on both adapters (v15).

Diagnostic-only per-session efficiency telemetry keyed on cc_session_id.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import SessionMetricsRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core


def _sqlite_storage(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    return SqliteStorage(db=db)


def _row(sid, **kw):
    base = dict(
        id=0, cc_session_id=sid, project="mcm", first_seen_at="2026-08-16T00:00:00",
        out_tokens=1000, in_tokens=200, cache_read_tokens=50, cache_write_tokens=10,
        loc_added=40, loc_removed=12, loc_churned=8, comment_lines_added=5,
        code_lines_added=35, edit_cycles_max=4, fixation_events=1, tool_failures=2,
        extras_json=None,
    )
    base.update(kw)
    return SessionMetricsRow(**base)


def test_sqlite_upsert_and_list(tmp_path):
    s = _sqlite_storage(tmp_path)
    s.upsert_session_metrics(_row("sess-1"))
    rows = s.list_session_metrics()
    assert len(rows) == 1
    r = rows[0]
    assert r.cc_session_id == "sess-1"
    assert r.out_tokens == 1000 and r.loc_churned == 8 and r.fixation_events == 1


def test_sqlite_upsert_is_last_write_wins(tmp_path):
    s = _sqlite_storage(tmp_path)
    s.upsert_session_metrics(_row("sess-1", out_tokens=1000))
    s.upsert_session_metrics(_row("sess-1", out_tokens=5000, loc_added=99))
    rows = s.list_session_metrics()
    assert len(rows) == 1  # same cc_session_id -> updated, not duplicated
    assert rows[0].out_tokens == 5000 and rows[0].loc_added == 99


def test_sqlite_list_newest_first_and_project_filter(tmp_path):
    s = _sqlite_storage(tmp_path)
    s.upsert_session_metrics(_row("a", project="mcm"))
    s.upsert_session_metrics(_row("b", project="other"))
    s.upsert_session_metrics(_row("c", project="mcm"))
    all_rows = s.list_session_metrics()
    assert [r.cc_session_id for r in all_rows] == ["c", "b", "a"]  # newest first
    mcm_only = s.list_session_metrics(project="mcm")
    assert {r.cc_session_id for r in mcm_only} == {"a", "c"}


# ---- Postgres parity (gated) ----

DSN = os.environ.get("MCM_TEST_POSTGRES_DSN",
                     "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_available() -> bool:
    try:
        import psycopg
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pg = pytest.mark.skipif(not _pg_available(), reason="no postgres at DSN")


@pg
def test_postgres_upsert_and_list(tmp_path):
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    s = PostgresStorage(DSN)
    s.ensure_schema()
    sid = "pg-sess-metrics-1"
    s.upsert_session_metrics(_row(sid, out_tokens=1234))
    s.upsert_session_metrics(_row(sid, out_tokens=9999, loc_churned=7))  # upsert
    rows = [r for r in s.list_session_metrics(limit=100) if r.cc_session_id == sid]
    assert len(rows) == 1
    assert rows[0].out_tokens == 9999 and rows[0].loc_churned == 7
