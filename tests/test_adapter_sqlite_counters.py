"""Conformance for the embedded SQLite CounterStore."""
from __future__ import annotations

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    CounterStore,
    EntityType,
    KnowledgeRow,
    RuleRow,
)


@pytest.fixture
def db_path(tmp_path):
    """A SQLite file with the core schema applied."""
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    p = str(tmp_path / "counters.db")
    storage = SqliteStorage(db_path=p)
    storage.ensure_schema()
    return p, storage


def test_protocol_runtime_check(db_path):
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, _ = db_path
    counters = SqliteCounters(db_path=p)
    assert isinstance(counters, CounterStore)
    assert counters.CONTRACT_VERSION == CONTRACT_VERSION


def test_increment_writes_to_entry_row(db_path):
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, storage = db_path
    counters = SqliteCounters(db_path=p)
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
    counters.increment(EntityType.KNOWLEDGE, k, "hit_count", by=3)
    snapshot = counters.get(EntityType.KNOWLEDGE, k)
    assert snapshot["hit_count"] == 3


def test_increment_last_hit_at_sets_timestamp(db_path):
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, storage = db_path
    counters = SqliteCounters(db_path=p)
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
    counters.increment(EntityType.KNOWLEDGE, k, "last_hit_at")
    snapshot = counters.get(EntityType.KNOWLEDGE, k)
    # SQLite returns an ISO-ish text; we just check it's now non-null.
    assert snapshot["last_hit_at"] is not None


def test_unknown_counter_raises(db_path):
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, storage = db_path
    counters = SqliteCounters(db_path=p)
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
    with pytest.raises(ValueError, match="no counter column"):
        counters.increment(EntityType.KNOWLEDGE, k, "made_up_counter")


def test_top_by_returns_descending(db_path):
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, storage = db_path
    counters = SqliteCounters(db_path=p)
    a = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    b = storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding"))
    counters.increment(EntityType.KNOWLEDGE, a, "hit_count", by=2)
    counters.increment(EntityType.KNOWLEDGE, b, "hit_count", by=5)
    top = counters.top_by(EntityType.KNOWLEDGE, "hit_count", 10)
    # Top entry has the higher count, regardless of insertion order.
    assert top[0] == (b, 5.0)
    assert top[1] == (a, 2.0)


def test_negative_only_pinned(db_path):
    """negative_knowledge has no hit_count column — increment must fail."""
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, _ = db_path
    counters = SqliteCounters(db_path=p)
    with pytest.raises(ValueError, match="hit_count"):
        counters.increment(EntityType.NEGATIVE, 1, "hit_count")


def test_flush_is_noop(db_path):
    """Embedded reference is write-through; flush returns None without error."""
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, _ = db_path
    counters = SqliteCounters(db_path=p)
    assert counters.flush() is None


def test_last_flushed_snapshot_equals_live(db_path):
    """Embedded counters have no staleness window."""
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    p, storage = db_path
    counters = SqliteCounters(db_path=p)
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
    counters.increment(EntityType.KNOWLEDGE, k, "hit_count")
    assert counters.last_flushed_snapshot(EntityType.KNOWLEDGE, k) == counters.get(EntityType.KNOWLEDGE, k)
