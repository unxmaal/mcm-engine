"""StorageBackend.find_by_id — generic entity lookup by (type, id).

Needed by tool functions that need the row back for response messages
(reinforce_knowledge/rule, promote_to_rule, get_related labels, etc.).
Adding generically rather than per-entity-type keeps the API surface
small and matches the dynamic-table pattern from the seam inventory.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import (
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RuleRow,
)


@pytest.fixture
def storage(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db_path=str(tmp_path / "x.db"))
    s.ensure_schema()
    return s


def test_find_by_id_returns_none_when_absent(storage):
    assert storage.find_by_id(EntityType.KNOWLEDGE, 9999) is None


def test_find_by_id_knowledge(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    row = storage.find_by_id(EntityType.KNOWLEDGE, k)
    assert isinstance(row, KnowledgeRow)
    assert row.id == k
    assert row.topic == "A"


def test_find_by_id_negative(storage):
    n = storage.insert_negative(NegativeRow(id=0, category="bug", what_failed="boom"))
    row = storage.find_by_id(EntityType.NEGATIVE, n)
    assert isinstance(row, NegativeRow)
    assert row.what_failed == "boom"


def test_find_by_id_error(storage):
    e = storage.insert_error(ErrorRow(id=0, pattern="ZeroDivisionError"))
    row = storage.find_by_id(EntityType.ERROR, e)
    assert isinstance(row, ErrorRow)
    assert row.pattern == "ZeroDivisionError"


def test_find_by_id_rule(storage):
    r = storage.insert_rule(RuleRow(id=0, title="T", keywords="kw"))
    row = storage.find_by_id(EntityType.RULE, r)
    assert isinstance(row, RuleRow)
    assert row.title == "T"


def test_find_by_id_accepts_caller(storage):
    """No-op caller threading (MCM2-05)."""
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    assert storage.find_by_id(EntityType.KNOWLEDGE, k, caller="alice") is not None
