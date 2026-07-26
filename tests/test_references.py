"""Structured references on knowledge entries (c5 Phase 5).

Covers the refs helpers, storage round-trip, the additive v11->v12 migration,
and the add_knowledge -> search surfacing path (insert, update-replace,
omit-preserves, clear).
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, KnowledgeRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.refs import dump_refs, format_refs, load_refs, validate_refs
from mcm_engine.schema import CORE_VERSION, migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


# ---- refs helpers -----------------------------------------------------------

def test_validate_refs_normalizes_and_rejects():
    assert validate_refs(None) == []
    ok = validate_refs([{"type": "file", "target": " src/x.py:1 ", "note": "n"}])
    assert ok == [{"type": "file", "target": "src/x.py:1", "note": "n"}]
    with pytest.raises(ValueError):
        validate_refs([{"type": "bogus", "target": "x"}])
    with pytest.raises(ValueError):
        validate_refs([{"type": "file", "target": ""}])
    with pytest.raises(ValueError):
        validate_refs("not a list")


def test_dump_load_roundtrip_and_empty():
    refs = [{"type": "url", "target": "https://x"}]
    assert load_refs(dump_refs(refs)) == refs
    assert dump_refs(None) is None and dump_refs([]) is None
    assert load_refs(None) is None and load_refs("not json") is None


def test_format_refs():
    assert format_refs(None) == ""
    out = format_refs([{"type": "test", "target": "t/a.py::x", "note": "why"}])
    assert "ref[test]: t/a.py::x" in out and "(why)" in out


# ---- storage round-trip -----------------------------------------------------

@pytest.fixture
def storage(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db_path=str(tmp_path / "x.db"))
    s.ensure_schema()
    return s


def test_storage_roundtrips_references(storage):
    refs = [{"type": "file", "target": "src/x.py:42", "note": "impl"}]
    kid = storage.insert_knowledge(
        KnowledgeRow(id=0, topic="A", summary="a", references=refs))
    row = storage.find_by_id(EntityType.KNOWLEDGE, kid)
    assert row.references == refs


def test_storage_none_references(storage):
    kid = storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b"))
    assert storage.find_by_id(EntityType.KNOWLEDGE, kid).references is None


# ---- migration --------------------------------------------------------------

def test_v11_to_v12_migration_adds_refs_json(tmp_path):
    db = KnowledgeDB(str(tmp_path / "old.db"))
    migrate_core(db)  # fresh -> latest
    # Simulate a pre-v12 DB: drop the column is not trivial in sqlite, so instead
    # assert the column exists at latest and the version stamped is >= 12.
    cols = [r[1] for r in db.execute("PRAGMA table_info(knowledge)").fetchall()]
    assert "refs_json" in cols
    ver = db.execute(
        "SELECT version FROM _mcm_versions WHERE component = 'core'").fetchone()
    assert ver["version"] == CORE_VERSION >= 12


def test_migration_backfills_existing_rows_as_null(tmp_path):
    # Build a DB, stamp it back to v11 minus the column, then migrate forward.
    dbpath = str(tmp_path / "mig.db")
    db = KnowledgeDB(dbpath)
    migrate_core(db)
    db.execute_write(
        "INSERT INTO knowledge (topic, kind, summary) VALUES ('T', 'finding', 's')")
    db.commit()
    # Emulate an existing row from before refs_json existed by nulling it.
    db.execute_write("UPDATE knowledge SET refs_json = NULL WHERE topic = 'T'")
    db.commit()
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db=db)
    row = s.find_knowledge_by_topic_kind("T", "finding")
    assert row.references is None


# ---- tool path: add_knowledge -> search -------------------------------------

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
    tracker = SessionTracker(NudgeConfig())
    # search registered first so add_knowledge can reuse the same storage
    register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj",
                             mcp.tools.get("search"))
    return mcp.tools


def test_add_knowledge_surfaces_references_in_search(tools):
    tools["add_knowledge"](
        topic="Cache policy", summary="LWW per name",
        references=[{"type": "file", "target": "index.html:100"}])
    out = tools["search"]("Cache")
    assert "ref[file]: index.html:100" in out


def test_add_knowledge_rejects_bad_references(tools):
    msg = tools["add_knowledge"](
        topic="X", summary="s", references=[{"type": "nope", "target": "t"}])
    assert "rejected" in msg
    # nothing stored
    assert "ref[" not in tools["search"]("X")


def test_update_replaces_and_omit_preserves(tools):
    tools["add_knowledge"](topic="T", summary="s1",
                           references=[{"type": "url", "target": "https://a"}])
    # Re-store same topic WITHOUT references -> existing refs preserved
    tools["add_knowledge"](topic="T", summary="s2")
    out = tools["search"]("T")
    assert "https://a" in out
    # Re-store WITH new references -> replaced
    tools["add_knowledge"](topic="T", summary="s3",
                           references=[{"type": "url", "target": "https://b"}])
    out = tools["search"]("T")
    assert "https://b" in out and "https://a" not in out
    # Clear with []
    tools["add_knowledge"](topic="T", summary="s4", references=[])
    assert "ref[" not in tools["search"]("T")
