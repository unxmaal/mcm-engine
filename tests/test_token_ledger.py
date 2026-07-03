"""Issue #37 — token ledger (tokens saved on reads, spent on writes)."""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import CORE_VERSION, migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import SessionTracker


def _storage(tmp_path):
    db = KnowledgeDB(str(tmp_path / "t.db"))
    migrate_core(db)
    return db, SqliteStorage(db=db)


def test_v10_migration_creates_token_ledger(tmp_path):
    db, _ = _storage(tmp_path)
    assert CORE_VERSION >= 10
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='token_ledger'"
    ).fetchone()
    assert row is not None


def test_record_and_totals(tmp_path):
    _, s = _storage(tmp_path)
    assert s.token_totals() == {"saved": 0, "spent": 0}
    s.record_token_event("saved", 100)
    s.record_token_event("saved", 50)
    s.record_token_event("spent", 30)
    assert s.token_totals() == {"saved": 150, "spent": 30}


class FakeMCP:
    def __init__(self):
        self._t = {}

    def tool(self):
        def d(fn):
            self._t[fn.__name__] = fn
            return fn
        return d

    def __getitem__(self, n):
        return self._t[n]


@pytest.fixture
def env(tmp_path):
    db, s = _storage(tmp_path)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tr = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200))
    register_rules_tools(mcp, db, tr, project_name="t", rules_paths=[rules_dir],
                         project_root=tmp_path, files_authoritative=False)
    register_knowledge_tools(mcp, db, tr, "t", [])
    register_session_tools(mcp, db, tr, "t", [])
    return mcp, s


def test_add_rule_logs_spent_and_read_rule_logs_saved(env):
    mcp, s = env
    mcp["add_rule"](title="R", keywords="k", file_path="mem/r.md", content="x" * 400)
    assert s.token_totals()["spent"] >= 1
    mcp["read_rule"](file_path="mem/r.md")
    assert s.token_totals()["saved"] >= 1


def test_add_knowledge_logs_spent(env):
    mcp, s = env
    mcp["add_knowledge"](topic="T", summary="a summary long enough to count here")
    assert s.token_totals()["spent"] >= 1


def test_session_start_reports_ledger(env):
    mcp, s = env
    s.record_token_event("saved", 5000)
    s.record_token_event("spent", 1000)
    out = mcp["session_start"]()
    assert "Token ledger" in out
    assert "net +4k" in out


def test_ledger_failure_never_breaks_the_tool(env):
    mcp, s = env
    # Simulate the ledger being unavailable (old DB / missing table). The
    # tool shares this db handle, so its record_token_event will raise —
    # and the best-effort try/except must swallow it.
    with s.transaction():
        s._db.execute_write("DROP TABLE IF EXISTS token_ledger")
    out = mcp["add_knowledge"](topic="T2", summary="still works despite no ledger")
    assert "T2" in out
