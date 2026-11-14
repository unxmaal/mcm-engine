"""get_last_snapshot is scoped like get_last_session, and the resume tools take a workspace (#222)."""
from __future__ import annotations

import os

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import SessionRow, SnapshotRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker

SCOPE = "a-shared-store-query-with-no-scope-or-a-scope-with-its-own-idea-of-null"
PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_available() -> bool:
    try:
        import psycopg
        psycopg.connect(PG_DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStorage(db_path=str(tmp_path / "store.db"))
        s.ensure_schema()
        yield s
        return
    if not _pg_available():
        pytest.skip(f"no postgres at {PG_DSN}")
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    s = PostgresStorage(dsn=PG_DSN)
    s.ensure_schema()
    s.truncate_all()
    try:
        yield s
    finally:
        s.close()


def _snap(storage, goal, principal=None, workspace=None, session=True):
    sid = storage.insert_session(SessionRow(id=0, status=goal, principal=principal, workspace=workspace)) \
        if session else None
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=sid, goal=goal))


def _goal(snap):
    return snap.goal if snap is not None else None


def test_scoped_last_snapshot_is_the_callers_own(storage):
    _snap(storage, "alice-1", principal="alice")
    _snap(storage, "alice-2", principal="alice")
    _snap(storage, "bob", principal="bob")
    assert _goal(storage.get_last_snapshot(principal="alice")) == "alice-2"
    assert _goal(storage.get_last_snapshot(principal="bob")) == "bob"
    assert _goal(storage.get_last_snapshot()) == "bob"


@pytest.mark.gauntlet(SCOPE, site="src/mcm_engine/adapters/sqlite/storage.py:SqliteStorage.get_last_snapshot:sessions")
@pytest.mark.gauntlet(SCOPE, site="src/mcm_engine/adapters/postgres/storage.py:PostgresStorage.get_last_snapshot:sessions")
def test_scoped_last_snapshot_never_returns_another_principals(storage):
    _snap(storage, "bob", principal="bob")
    assert storage.get_last_snapshot(principal="carol") is None


def test_null_scope_snapshot_is_a_fallback_only(storage):
    _snap(storage, "legacy", principal=None)
    _snap(storage, "no-session", session=False)
    assert _goal(storage.get_last_snapshot(principal="carol")) == "no-session"
    _snap(storage, "carol", principal="carol")
    _snap(storage, "later-legacy", principal=None)
    assert _goal(storage.get_last_snapshot(principal="carol")) == "carol"


def test_workspace_separates_one_principals_snapshots(storage):
    _snap(storage, "wA", principal="dave", workspace="A")
    _snap(storage, "wB", principal="dave", workspace="B")
    assert _goal(storage.get_last_snapshot(principal="dave", workspace="A")) == "wA"
    assert _goal(storage.get_last_snapshot(workspace="A")) == "wA"
    assert _goal(storage.get_last_snapshot(principal="dave")) == "wB"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    register_session_tools(mcp, db, SessionTracker(NudgeConfig()), "proj", [])
    return mcp.tools


def _two_agents(tools):
    tools["session_handoff"](status="A handoff", current_task="A task", workspace="A")
    tools["save_snapshot"](goal="A checkpoint", workspace="A")
    tools["session_handoff"](status="B handoff", current_task="B task", workspace="B")
    tools["save_snapshot"](goal="B checkpoint", workspace="B")


def test_resume_context_with_a_workspace_shows_only_that_agents_state(tools):
    _two_agents(tools)
    out = tools["get_resume_context"](workspace="A")
    assert "A handoff" in out and "A checkpoint" in out
    assert "B handoff" not in out and "B checkpoint" not in out and "B task" not in out


def test_session_start_with_a_workspace_shows_that_agents_handoff(tools):
    _two_agents(tools)
    out = tools["session_start"](workspace="A")
    assert "A handoff" in out and "B handoff" not in out


def test_save_snapshot_with_a_workspace_attaches_to_that_agents_session(tools):
    _two_agents(tools)
    tools["save_snapshot"](goal="A again", workspace="A")
    assert "A again" in tools["get_resume_context"](workspace="A")
    assert "A again" not in tools["get_resume_context"](workspace="B")


def test_resume_without_a_workspace_is_unchanged(tools):
    _two_agents(tools)
    out = tools["get_resume_context"]()
    assert "B handoff" in out and "B checkpoint" in out
