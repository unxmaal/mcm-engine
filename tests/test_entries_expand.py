"""Unified entries expand step (issue #179, 4.0 program #176).

The outside check: a legacy row projected to Entry and back is equal, and the
backfill proves it per row with entry_hash; a tampered entries row makes the
verify raise. Embedded SQLite here; the Postgres adapter runs the same
contract through the conformance suite.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from mcm_engine.entries import (
    EntriesBackfillError,
    backfill,
    entry_hash,
    from_entry,
    needs_backfill,
    to_entry,
)

T = datetime(2026, 7, 3, 21, 25, 7, tzinfo=timezone.utc)

ROWS = [
    (EntityType.KNOWLEDGE, KnowledgeRow(
        id=8, topic="mahjong timer", summary="one line", kind="context",
        detail="body\n\nmore", tags="a,b", project="unxmaal", rationale="why",
        alternatives=None, references=[{"type": "file", "target": "x.py:1"}],
        hit_count=3, last_hit_at=T, reinforcement_count=1, pinned=True,
        created_at=T, updated_at=T, source_classification="internal",
        status="superseded", superseded_by=9, valid_until=T)),
    (EntityType.NEGATIVE, NegativeRow(
        id=10, category="measurement", what_failed="wrote the opposite",
        why_failed="zero requests", correct_approach="re-read", severity="high",
        project="localharness", pinned=False, created_at=T)),
    (EntityType.ERROR, ErrorRow(
        id=97, pattern="run_path='ok'", context="ctx", root_cause=None, fix=None,
        tags="t", project="localharness", created_at=T)),
    (EntityType.RULE, RuleRow(
        id=92, title="Nudge deadlock fix", keywords="nudge,deadlock",
        file_path="rules/x.md", description="desc", category="mcm-engine",
        hit_count=5, last_hit_at=T, reinforcement_count=2, pinned=True,
        content_hash="abc", archived=True, archived_at=T, created_at=T,
        updated_at=T, content="# body", created_by="eric", updated_by="claude",
        correct_count=2, incorrect_count=1, valid_until=None, superseded_by=None,
        status="active", importance=2, scope="universal", kind="directive",
        source_classification=None)),
]


@pytest.mark.parametrize("etype,row", ROWS, ids=[e.value for e, _ in ROWS])
def test_projection_round_trips_losslessly(etype, row):
    entry = to_entry(etype, row)
    assert (entry.kind, entry.id) == (etype.value, row.id)
    back = from_entry(entry)
    assert back == row
    assert entry_hash(entry) == entry_hash(to_entry(etype, back))


def test_entry_hash_changes_when_a_field_changes():
    e1 = to_entry(ROWS[0][0], ROWS[0][1])
    e2 = to_entry(ROWS[0][0], ROWS[0][1])
    e2.facets = dict(e2.facets, rationale="different")
    assert entry_hash(e1) != entry_hash(e2)


@pytest.fixture
def store(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "k.db"))
    s.ensure_schema()
    return s


def _seed_legacy(s: SqliteStorage) -> None:
    """Legacy rows with no entries mirror: what a pre-4.0 store looks like.
    The dual-write (#180) mirrors every insert, so the mirror is stripped."""
    s.insert_knowledge(KnowledgeRow(id=0, topic="t1", summary="s1", detail="d1", tags="x"))
    s.insert_knowledge(KnowledgeRow(id=0, topic="t2", summary="s2"))
    s.insert_negative(NegativeRow(id=0, category="c", what_failed="w", why_failed="y"))
    s.insert_error(ErrorRow(id=0, pattern="p", context="c"))
    s.insert_rule(RuleRow(id=0, title="r", keywords="k", content="body", description="d"))
    for e in list(s.iter_entries_unified()):
        s.delete_entry(e.kind, e.id)


def test_fresh_store_has_entries_schema_and_nothing_to_backfill(store):
    assert store.count_entries() == 0
    assert needs_backfill(store) is False


def test_backfill_copies_and_verifies_every_legacy_row(store):
    _seed_legacy(store)
    assert needs_backfill(store) is True
    report = store.backfill_entries()
    assert report["knowledge"] == {"legacy": 2, "entries": 2, "inserted": 2}
    assert report["rule"]["inserted"] == 1 and report["error"]["inserted"] == 1
    assert store.count_entries() == 5
    for et in EntityType:
        for row in store.iter_entries(et):
            got = store.find_entry(et.value, row.id)
            assert got is not None
            assert entry_hash(got) == entry_hash(to_entry(et, row))
            assert from_entry(got) == row
    # a rerun is a no-op (ids preserved, nothing re-inserted)
    again = store.backfill_entries()
    assert all(v["inserted"] == 0 for v in again.values())
    assert needs_backfill(store) is False


def test_ensure_schema_runs_the_backfill_on_an_existing_store(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "k.db"))
    s.ensure_schema()
    _seed_legacy(s)
    assert needs_backfill(s)
    s2 = SqliteStorage(db_path=str(tmp_path / "k.db"))
    s2.ensure_schema()  # boots like the server: backfill + verify
    assert s2.count_entries() == 5


def test_backfill_refuses_when_entries_disagree_with_legacy(store):
    _seed_legacy(store)
    store.backfill_entries()
    store.update_entry("knowledge", 1, title="tampered")
    with pytest.raises(EntriesBackfillError):
        backfill(store)


def test_next_entry_id_continues_past_legacy_ids(store):
    _seed_legacy(store)
    store.backfill_entries()
    assert store.next_entry_id("knowledge") == 3
    assert store.next_entry_id("knowledge") == 4
    assert store.next_entry_id("rule") == 2
    # a kind with no rows at all starts at 1
    s = store
    assert s.count_entries("negative") == 1 and s.next_entry_id("negative") == 2


def test_insert_find_update_delete_and_paging(store):
    from mcm_engine.backends import Entry
    eid = store.insert_entry(Entry(kind="knowledge", id=0, title="new", summary="s",
                                   facets={"rationale": "r"}))
    assert eid == 1
    got = store.find_entry("knowledge", eid)
    assert got.title == "new" and got.facets == {"rationale": "r"}
    assert got.created_at is not None
    store.update_entry("knowledge", eid, body="b", facets={"rationale": "r2"}, pinned=True)
    got = store.find_entry("knowledge", eid)
    assert got.body == "b" and got.facets == {"rationale": "r2"} and got.pinned is True
    assert store.insert_entry(Entry(kind="knowledge", id=eid, title="dup"), if_absent=True) is None
    assert [e.id for e in store.page_entries_unified("knowledge", after_id=0, limit=10)] == [1]
    assert [e.kind for e in store.iter_entries_unified()] == ["knowledge"]
    store.delete_entry("knowledge", eid)
    assert store.find_entry("knowledge", eid) is None and store.count_entries() == 0
