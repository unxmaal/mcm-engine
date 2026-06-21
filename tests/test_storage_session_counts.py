"""Additional StorageBackend count methods needed for session.py rewire.

count_relations + count_snapshots: trivial totals.
count_recent_knowledge: created within last N days.
count_stale_knowledge: matches the v1 session_start stale logic
  (created > threshold_days ago AND no recent hit AND not pinned).
"""
from __future__ import annotations

import time

import pytest

from mcm_engine.backends import (
    EntityType,
    KnowledgeRow,
    RelationRow,
    SessionRow,
    SnapshotRow,
)


@pytest.fixture
def storage(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db_path=str(tmp_path / "x.db"))
    s.ensure_schema()
    return s


# ---- count_relations -----------------------------------------------------


def test_count_relations_zero(storage):
    assert storage.count_relations() == 0


def test_count_relations_increments(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    k2 = storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding"))
    storage.insert_relation(RelationRow(
        id=0,
        source_type=EntityType.KNOWLEDGE, source_id=k,
        target_type=EntityType.KNOWLEDGE, target_id=k2,
        relation="related",
    ))
    assert storage.count_relations() == 1


def test_count_relations_accepts_caller(storage):
    assert storage.count_relations(caller="alice") == 0


# ---- count_snapshots -----------------------------------------------------


def test_count_snapshots_zero(storage):
    assert storage.count_snapshots() == 0


def test_count_snapshots_increments(storage):
    s = storage.insert_session(SessionRow(id=0, status="ok"))
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=s, goal="g"))
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=2, session_id=s, goal="g2"))
    assert storage.count_snapshots() == 2


def test_count_snapshots_accepts_caller(storage):
    assert storage.count_snapshots(caller="bob") == 0


# ---- count_recent_knowledge ----------------------------------------------


def test_count_recent_knowledge_just_inserted(storage):
    """Just-inserted rows count as recent — created_at is now, well within
    even a fractional-day window."""
    storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    assert storage.count_recent_knowledge(since_days=7) == 1
    assert storage.count_recent_knowledge(since_days=1) == 1
    assert storage.count_recent_knowledge(since_days=1.0 / 24.0) == 1  # last hour


def test_count_recent_knowledge_with_zero_window(storage):
    """A zero window excludes everything, including just-inserted."""
    storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    # In practice the call would be `since_days=0` meaning "strictly less than 0
    # days ago" — i.e. nothing.
    assert storage.count_recent_knowledge(since_days=0) == 0


def test_count_recent_knowledge_accepts_caller(storage):
    assert storage.count_recent_knowledge(since_days=7, caller="alice") == 0


# ---- count_stale_knowledge -----------------------------------------------


def test_count_stale_knowledge_just_inserted_not_stale(storage):
    """Just-inserted entries are never stale at the default 90-day threshold."""
    storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    assert storage.count_stale_knowledge() == 0
    assert storage.count_stale_knowledge(threshold_days=30) == 0


def test_count_stale_knowledge_pinned_excluded(storage):
    """Pinned entries never count as stale even after the threshold."""
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    # Force pinned and shove created_at to look old via direct DB poke (test fixture).
    storage._db.execute_write(
        "UPDATE knowledge SET created_at = '2020-01-01', pinned = 1 WHERE id = ?",
        (k,),
    )
    storage._db.commit()
    assert storage.count_stale_knowledge() == 0


def test_count_stale_knowledge_old_unpinned_no_hit_counts(storage):
    """An old unpinned entry with no recent hit counts as stale."""
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    storage._db.execute_write(
        "UPDATE knowledge SET created_at = '2020-01-01', last_hit_at = NULL WHERE id = ?",
        (k,),
    )
    storage._db.commit()
    assert storage.count_stale_knowledge() == 1


def test_count_stale_knowledge_old_with_recent_hit_not_stale(storage):
    """An old entry that has been hit recently is still warm."""
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    storage._db.execute_write(
        "UPDATE knowledge SET created_at = '2020-01-01', "
        "last_hit_at = datetime('now') WHERE id = ?",
        (k,),
    )
    storage._db.commit()
    assert storage.count_stale_knowledge() == 0


def test_count_stale_knowledge_accepts_caller(storage):
    assert storage.count_stale_knowledge(caller="alice") == 0
