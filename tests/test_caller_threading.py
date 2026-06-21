"""MCM2-05: identity+scope no-op threading on read methods.

Read methods on StorageBackend (and the already-present caller param on
SearchBackend.search) MUST accept a `caller` kwarg now, even though the
embedded reference ignores it. This avoids a future contract break when
multi-tenant access control lands.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, KnowledgeRow, RuleRow


@pytest.fixture
def storage(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db_path=str(tmp_path / "x.db"))
    s.ensure_schema()
    return s


@pytest.fixture
def search(tmp_path, storage):
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    # Share the file with storage so the FTS index sees its inserts.
    return SqliteSearch(db=storage._db)


def test_find_knowledge_accepts_caller(storage):
    storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    assert storage.find_knowledge_by_topic_kind("t", "finding", caller="alice") is not None


def test_find_rule_accepts_caller(storage):
    storage.insert_rule(RuleRow(id=0, title="t", keywords="kw"))
    assert storage.find_rule_by_title("t", caller="bob") is not None


def test_list_rules_with_file_paths_accepts_caller(storage):
    storage.insert_rule(RuleRow(id=0, title="A", keywords="kw", file_path="a.md"))
    assert storage.list_rules_with_file_paths(caller="alice") != []


def test_count_by_type_accepts_caller(storage):
    storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    assert storage.count_by_type(EntityType.KNOWLEDGE, caller="alice") == 1


def test_list_pinned_accepts_caller(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    storage.set_pinned(EntityType.KNOWLEDGE, k, True)
    assert len(storage.list_pinned(EntityType.KNOWLEDGE, caller="alice")) == 1


def test_entry_exists_accepts_caller(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    assert storage.entry_exists(EntityType.KNOWLEDGE, k, caller="alice") is True


def test_get_last_session_accepts_caller(storage):
    from mcm_engine.backends import SessionRow
    storage.insert_session(SessionRow(id=0, status="ok"))
    assert storage.get_last_session(caller="alice") is not None


def test_search_accepts_caller_already(search):
    """SearchBackend.search already carries the caller kwarg in the
    Protocol; sanity check the impl propagates."""
    hits = search.search("anything", caller="alice")
    # No data — empty is fine; the call must not error.
    assert hits == []
