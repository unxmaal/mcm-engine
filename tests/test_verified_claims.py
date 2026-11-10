"""issue #206: handoffs and snapshots carry verified and unverified claims as
structured state, and resume prints the unverified ones first."""
from __future__ import annotations

import io
import json
import os

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import SessionRow, SnapshotRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.hooks import session_start as ss
from mcm_engine.schema import CORE_VERSION, migrate_core
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import SessionTracker


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


@pytest.fixture
def env(db):
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200))
    register_session_tools(mcp, db, tracker, "test-project", [])
    return mcp, db, tracker


VERIFIED = [{"claim": "PR 12 merged", "evidence": "gh pr view 12: MERGED",
             "at": "2026-10-30T10:00:00Z"}]
UNVERIFIED = [{"claim": "run finishes at 14:00",
               "check": "read the job status for run 7"}]


def test_handoff_round_trips_both_lists(env):
    mcp, db, _ = env
    mcp["session_handoff"](status="mid-task", verified=VERIFIED, unverified=UNVERIFIED)
    row = db.execute("SELECT claims FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    claims = json.loads(row["claims"])
    assert claims["verified"][0]["claim"] == "PR 12 merged"
    assert claims["verified"][0]["evidence"] == "gh pr view 12: MERGED"
    assert claims["unverified"] == [UNVERIFIED[0]]
    snap = db.execute("SELECT claims FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(snap["claims"]) == claims


def test_resume_prints_unverified_before_everything(env):
    mcp, _, _ = env
    mcp["session_handoff"](status="mid-task", current_task="ship it",
                           verified=VERIFIED, unverified=UNVERIFIED)
    out = mcp["get_resume_context"]()
    assert out.startswith("=== UNVERIFIED")
    u = out.index("run finishes at 14:00")
    assert out.index("read the job status for run 7") > u
    v = out.index("PR 12 merged")
    assert u < v < out.index("--- Last Session ---")
    assert "gh pr view 12: MERGED" in out
    assert "age" in out


def test_session_start_prints_unverified_first(env):
    mcp, _, _ = env
    mcp["session_handoff"](status="mid-task", verified=VERIFIED, unverified=UNVERIFIED)
    out = mcp["session_start"]()
    assert out.startswith("=== UNVERIFIED")
    assert out.index("run finishes at 14:00") < out.index("PR 12 merged")


def test_verified_without_evidence_is_stored_unverified(env):
    mcp, db, _ = env
    mcp["save_snapshot"](goal="g", verified=[{"claim": "tests pass", "evidence": "  "}])
    claims = json.loads(db.execute(
        "SELECT claims FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()["claims"])
    assert claims["verified"] == []
    assert [c["claim"] for c in claims["unverified"]] == ["tests pass"]
    assert claims["unverified"][0]["check"]
    out = mcp["get_resume_context"]()
    assert out.startswith("=== UNVERIFIED")
    assert "tests pass" in out


def test_snapshot_claims_round_trip(env):
    mcp, _, _ = env
    mcp["session_handoff"](status="s")
    mcp["save_snapshot"](goal="g", verified=VERIFIED, unverified=UNVERIFIED)
    out = mcp["get_resume_context"]()
    assert out.startswith("=== UNVERIFIED")
    assert "PR 12 merged" in out


def test_json_string_lists_accepted(env):
    mcp, db, _ = env
    mcp["session_handoff"](status="s", unverified=json.dumps(UNVERIFIED))
    claims = json.loads(db.execute(
        "SELECT claims FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["claims"])
    assert claims["unverified"] == UNVERIFIED


def test_old_signature_calls_unchanged(env):
    mcp, db, _ = env
    r = mcp["session_handoff"](status="done", current_task="t", next_steps="n", blockers="b")
    assert r.startswith("Session handoff recorded.")
    assert db.execute("SELECT claims FROM sessions").fetchone()["claims"] is None
    r = mcp["save_snapshot"](goal="g", progress="p")
    assert r.startswith("Snapshot #")
    out = mcp["get_resume_context"]()
    assert out.startswith("--- Last Session ---")
    assert "UNVERIFIED" not in out
    assert "UNVERIFIED" not in mcp["session_start"]()


def test_compact_directive_has_new_sentence(monkeypatch, capsys):
    assert ("treat every number, state and finish time in the summary as "
            "unverified until re-read from its source") in ss.COMPACT_DIRECTIVE.lower()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "compact"})))
    ss.main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "finish time" in ctx


def test_checkpoint_nudge_asks_for_both_lists():
    t = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=2, mandatory_stop_turns=1000))
    for _ in range(3):
        t.record_call("search")
    nudge = t.get_nudge() or ""
    assert "CHECKPOINT" in nudge
    assert "verified" in nudge and "unverified" in nudge


def _cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v19_to_v20_adds_claims_columns(tmp_path):
    db = KnowledgeDB(str(tmp_path / "old.db"))
    db.executescript(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
        "current_task TEXT, findings_summary TEXT, next_steps TEXT, blockers TEXT, "
        "context_snapshot TEXT, created_at TEXT, principal TEXT, workspace TEXT);"
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, session_id INTEGER, "
        "sequence_num INTEGER NOT NULL, goal TEXT, progress TEXT, open_questions TEXT, "
        "blockers TEXT, next_steps TEXT, active_files TEXT, key_decisions TEXT, "
        "created_at TEXT);"
        "CREATE TABLE IF NOT EXISTS _mcm_versions (component TEXT PRIMARY KEY, "
        "version INTEGER, updated_at TEXT);"
    )
    db.execute_write("INSERT INTO _mcm_versions (component, version) VALUES ('core', 19)")
    db.commit()
    migrate_core(db)
    assert "claims" in _cols(db, "sessions")
    assert "claims" in _cols(db, "snapshots")
    ver = db.execute("SELECT version FROM _mcm_versions WHERE component='core'").fetchone()
    assert ver["version"] == CORE_VERSION == 20


def _storage_round_trip(store):
    payload = json.dumps({"verified": [], "unverified": UNVERIFIED})
    sid = store.insert_session(SessionRow(id=0, status="s", claims=payload))
    store.insert_snapshot(SnapshotRow(id=0, session_id=sid, sequence_num=1, claims=payload))
    assert store.get_last_session().claims == payload
    assert store.get_last_snapshot().claims == payload


def test_sqlite_storage_round_trip(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "s.db"))
    s.ensure_schema()
    _storage_round_trip(s)


def _pg_dsn():
    return os.environ.get("MCM_TEST_POSTGRES_DSN", "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_up():
    try:
        import psycopg
        with psycopg.connect(_pg_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_up(), reason="Postgres not reachable")
def test_postgres_storage_round_trip():
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    s = PostgresStorage(dsn=_pg_dsn())
    s.ensure_schema()
    s.truncate_all()
    try:
        _storage_round_trip(s)
    finally:
        s.close()
