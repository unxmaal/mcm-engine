"""Storage correctness (#221): lifecycle moves guarded by the state they leave,
write pairs that land together, and the newest-row readers."""
from __future__ import annotations

import os
import sqlite3

import pytest

from mcm_engine import entries
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import (
    EntityType, KnowledgeRow, RelationRow, RuleRow, SessionRow, SnapshotRow,
)
from mcm_engine.db import KnowledgeDB
from mcm_engine.ingest import adjudicate
from mcm_engine.ingest.adjudicate import Action, Verdict
from mcm_engine.schema import migrate_core
from mcm_engine.tools.decisions import register_decisions_tools
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker

PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")
SQ = "src/mcm_engine/adapters/sqlite/storage.py:SqliteStorage."
PG = "src/mcm_engine/adapters/postgres/storage.py:PostgresStorage."
LIFE = "a-lifecycle-with-no-declared-state"
LAST = "state-is-whichever-row-came-last"
SCOPE = "a-shared-store-query-with-no-scope-or-a-scope-with-its-own-idea-of-null"
PAIR = "two-durable-writes-with-no-repair-for-the-gap"


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


def _force_rule_status(storage, rule_id, status):
    if isinstance(storage, SqliteStorage):
        storage._db.execute_write("UPDATE rules SET status = ? WHERE id = ?", (status, rule_id))
        storage._db.commit()
    else:
        with storage._conn.cursor() as cur:
            cur.execute("UPDATE rules SET status = %s WHERE id = %s", (status, rule_id))
        storage._commit()


def _rules(storage, n=3):
    return [storage.insert_rule(RuleRow(id=0, title=f"Rule {i}", keywords="k")) for i in range(n)]


def _findings(storage, n=3):
    return [storage.insert_knowledge(KnowledgeRow(id=0, topic=f"t{i}", kind="finding", summary="s"))
            for i in range(n)]


def _rule(storage, rid):
    return storage.find_by_id(EntityType.RULE, rid)


def _events(storage, rid, kind):
    return [e for e in storage.list_rule_events(rid) if e.event_type == kind]


# ---- supersede / unsupersede --------------------------------------------------

@pytest.mark.gauntlet(LIFE, site=SQ + "supersede_rule")
@pytest.mark.gauntlet(LIFE, site=PG + "supersede_rule")
def test_supersede_rule_moves_only_an_active_rule(storage):
    a, b, c = _rules(storage)
    assert storage.supersede_rule(a, b, "t") is True
    assert storage.supersede_rule(a, c, "t") is False
    assert _rule(storage, a).superseded_by == b
    assert len(_events(storage, a, "superseded")) == 1
    _force_rule_status(storage, c, "recalled")
    assert storage.supersede_rule(c, b, "t") is False
    assert _rule(storage, c).status == "recalled"


@pytest.mark.gauntlet(LIFE, site=SQ + "unsupersede_rule")
@pytest.mark.gauntlet(LIFE, site=PG + "unsupersede_rule")
def test_unsupersede_rule_moves_only_a_superseded_rule(storage):
    a, b, c = _rules(storage)
    assert storage.unsupersede_rule(a, "t") is False
    assert _events(storage, a, "unsuperseded") == []
    _force_rule_status(storage, c, "recalled")
    assert storage.unsupersede_rule(c, "t") is False
    assert _rule(storage, c).status == "recalled"
    storage.supersede_rule(a, b, "t")
    assert storage.unsupersede_rule(a, "t") is True
    row = _rule(storage, a)
    assert row.status == "active" and row.superseded_by is None


@pytest.mark.gauntlet(LIFE, site=SQ + "supersede_knowledge")
@pytest.mark.gauntlet(LIFE, site=PG + "supersede_knowledge")
def test_supersede_knowledge_moves_only_an_active_finding(storage):
    a, b, c = _findings(storage)
    assert storage.supersede_knowledge(a, b) is True
    assert storage.supersede_knowledge(a, c) is False
    assert storage.find_by_id(EntityType.KNOWLEDGE, a).superseded_by == b


@pytest.mark.gauntlet(LIFE, site=SQ + "unsupersede_knowledge")
@pytest.mark.gauntlet(LIFE, site=PG + "unsupersede_knowledge")
def test_unsupersede_knowledge_moves_only_a_superseded_finding(storage):
    a, b, _ = _findings(storage)
    assert storage.unsupersede_knowledge(a) is False
    storage.supersede_knowledge(a, b)
    assert storage.unsupersede_knowledge(a) is True
    assert storage.find_by_id(EntityType.KNOWLEDGE, a).status == "active"


def test_a_noop_supersede_leaves_the_entries_row_matching(storage):
    a, b, c = _rules(storage)
    storage.supersede_rule(a, b, "t")
    storage.supersede_rule(a, c, "t")
    assert entries.divergence(storage) == []


# ---- newest-row readers -------------------------------------------------------

@pytest.mark.gauntlet(LAST, site=SQ + "get_last_session")
@pytest.mark.gauntlet(LAST, site=PG + "get_last_session")
@pytest.mark.gauntlet(SCOPE, site=SQ + "get_last_session:sessions")
@pytest.mark.gauntlet(SCOPE, site=PG + "get_last_session:sessions")
def test_unscoped_last_session_is_the_newest_row(storage):
    for status in ("first", "second", "third"):
        storage.insert_session(SessionRow(id=0, status=status, principal="p"))
    assert storage.get_last_session().status == "third"


@pytest.mark.gauntlet(SCOPE, site=SQ + "get_last_session:null-scope")
@pytest.mark.gauntlet(SCOPE, site=PG + "get_last_session:null-scope")
def test_scoped_last_session_prefers_its_own_row_over_the_null_scope_fallback(storage):
    storage.insert_session(SessionRow(id=0, status="alice-1", principal="alice"))
    storage.insert_session(SessionRow(id=0, status="alice-2", principal="alice"))
    storage.insert_session(SessionRow(id=0, status="bob", principal="bob"))
    storage.insert_session(SessionRow(id=0, status="legacy", principal=None))
    assert storage.get_last_session(principal="alice").status == "alice-2"
    assert storage.get_last_session(principal="carol").status == "legacy"
    storage.insert_session(SessionRow(id=0, status="wA", principal="dave", workspace="A"))
    storage.insert_session(SessionRow(id=0, status="wB", principal="dave", workspace="B"))
    assert storage.get_last_session(principal="dave", workspace="A").status == "wA"


@pytest.mark.gauntlet(LAST, site=SQ + "get_last_snapshot")
@pytest.mark.gauntlet(LAST, site=PG + "get_last_snapshot")
def test_last_snapshot_is_the_newest_row(storage):
    sid = storage.insert_session(SessionRow(id=0, status="s"))
    for n, goal in enumerate(("first", "second", "third"), 1):
        storage.insert_snapshot(SnapshotRow(id=0, sequence_num=n, session_id=sid, goal=goal))
    assert storage.get_last_snapshot().goal == "third"


# ---- one definition of the updatable entry fields -----------------------------

def test_entry_updatable_fields_are_defined_once():
    from mcm_engine.adapters.sqlite import storage as sq
    assert sq._ENTRY_UPDATABLE is entries.ENTRY_UPDATABLE
    pytest.importorskip("psycopg")
    from mcm_engine.adapters.postgres import storage as pg
    assert pg._ENTRY_UPDATABLE is entries.ENTRY_UPDATABLE


# ---- find_similar_knowledge ---------------------------------------------------

class _Failing:
    def __init__(self, db, exc):
        self._db, self._exc = db, exc

    def execute(self, *a, **kw):
        raise self._exc

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_find_similar_knowledge_raises_when_the_store_fails(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "store.db"))
    s.ensure_schema()
    s._db = _Failing(s._db, sqlite3.OperationalError("database is locked"))
    with pytest.raises(sqlite3.OperationalError):
        s.find_similar_knowledge("anything")


def test_find_similar_knowledge_treats_an_fts_syntax_error_as_no_match(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "store.db"))
    s.ensure_schema()
    s._db = _Failing(s._db, sqlite3.OperationalError('fts5: syntax error near "("'))
    assert s.find_similar_knowledge("anything") is None


# ---- tools --------------------------------------------------------------------

class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def wired(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    search_all = register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", search_all)
    register_rules_tools(mcp, db, tracker, "proj", [rules_dir], tmp_path, files_authoritative=False)
    register_decisions_tools(mcp, db, tracker, project_name="proj")
    register_session_tools(mcp, db, tracker, "proj", [])
    return mcp.tools, SqliteStorage(db=db), rules_dir


def _boom(*a, **kw):
    raise RuntimeError("boom")


def test_supersede_rule_tool_reports_an_already_superseded_rule(wired):
    tools, s, _ = wired
    a, b, c = _rules(s)
    assert "Superseded: Rule 0" in tools["supersede_rule"](a, b)
    out = tools["supersede_rule"](a, c)
    assert f"already superseded by {b}" in out
    assert _rule(s, a).superseded_by == b


def test_supersede_knowledge_tool_reports_an_already_superseded_finding(wired):
    tools, s, _ = wired
    a, b, c = _findings(s)
    assert "Superseded knowledge" in tools["supersede_knowledge"](a, b)
    out = tools["supersede_knowledge"](a, c)
    assert f"already superseded by #{b}" in out
    assert s.find_by_id(EntityType.KNOWLEDGE, a).superseded_by == b
    assert not [r for r in s.iter_relations() if r.source_id == c and r.relation == "supersedes"]


@pytest.mark.gauntlet(PAIR, site="src/mcm_engine/tools/rules.py:register_rules_tools.add_rule")
def test_add_rule_leaves_no_rule_when_its_event_fails(wired, monkeypatch):
    tools, s, _ = wired
    monkeypatch.setattr(SqliteStorage, "insert_rule_event", _boom)
    with pytest.raises(RuntimeError):
        tools["add_rule"](title="Half written", keywords="k", content="body")
    assert s.find_rule_by_title("Half written") is None


def test_add_rule_update_is_undone_when_its_event_fails(wired, monkeypatch):
    tools, s, _ = wired
    tools["add_rule"](title="Stable", keywords="k", content="old body")
    monkeypatch.setattr(SqliteStorage, "insert_rule_event", _boom)
    with pytest.raises(RuntimeError):
        tools["add_rule"](title="Stable", keywords="k", content="new body")
    assert s.find_rule_by_title("Stable").content == "old body"


@pytest.mark.gauntlet(PAIR, site="src/mcm_engine/tools/rules.py:register_rules_tools.sync_rules")
def test_sync_rules_leaves_no_rule_when_its_event_fails(wired, monkeypatch):
    tools, s, rules_dir = wired
    (rules_dir / "one.md").write_text("# Synced rule\n\n**Keywords:** k\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(SqliteStorage, "insert_rule_event", _boom)
    with pytest.raises(RuntimeError):
        tools["sync_rules"]()
    assert s.find_rule_by_title("Synced rule") is None


@pytest.mark.gauntlet(PAIR, site="src/mcm_engine/tools/rules.py:_apply_import_batch")
def test_import_rules_leaves_no_rule_when_an_event_fails(wired, monkeypatch):
    tools, s, _ = wired
    monkeypatch.setattr(SqliteStorage, "insert_rule_event", _boom)
    try:
        tools["import_rules"](rules=[{"title": "Imported", "keywords": "k", "content": "body"}])
    except RuntimeError:
        pass
    assert s.find_rule_by_title("Imported") is None


@pytest.mark.gauntlet(PAIR, site="src/mcm_engine/ingest/adjudicate.py:_insert_rule")
def test_adjudicate_add_leaves_no_rule_when_its_event_fails(wired, monkeypatch):
    _, s, _ = wired
    monkeypatch.setattr(SqliteStorage, "insert_rule_event", _boom)
    report = adjudicate.commit_verdicts(
        s, None, [Verdict(action=Action.ADD, title="Adjudicated", keywords="k", content="some body text")])
    assert report.errors == 1
    assert s.find_rule_by_title("Adjudicated") is None


@pytest.mark.gauntlet(PAIR, site="src/mcm_engine/tools/decisions.py:register_decisions_tools.record_decision")
def test_record_decision_leaves_no_decision_when_a_link_fails(wired, monkeypatch):
    tools, s, _ = wired
    evidence = _findings(s, 1)[0]
    monkeypatch.setattr(SqliteStorage, "insert_relation", _boom)
    with pytest.raises(RuntimeError):
        tools["record_decision"](topic="Pick one", scenario="s", reasoning="r", outcome="chose A",
                                 based_on=[f"knowledge#{evidence}"])
    assert s.find_knowledge_by_topic_kind("Pick one", "decision") is None


def test_session_handoff_is_kept_when_its_snapshot_fails(wired, monkeypatch):
    tools, s, _ = wired
    monkeypatch.setattr(SqliteStorage, "insert_snapshot", _boom)
    assert "Session handoff recorded" in tools["session_handoff"](status="kept despite snapshot")
    assert s.get_last_session().status == "kept despite snapshot"
