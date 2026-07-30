"""Terminal 'recalled' status is invisible and sync-proof (issue #103).

recall_entry itself is postgres-only, but the enforcement of the terminal
status it sets is backend-agnostic and lives in three places tested here on
SQLite: list_rules excludes it, search (_score_and_format_rule) drops it, and
the watcher cascade never revives it from its file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow, SearchHit
from mcm_engine.files.watcher import RulesWatcher
from mcm_engine.tools.search import _score_and_format_rule


@pytest.fixture
def storage(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "v.db"))
    s.ensure_schema()
    return s


def _recall(storage, rid):
    """Set the terminal status directly (the pg-only recall_entry tool isn't
    exercised here — this test targets the enforcement, not the write path)."""
    storage._db.execute_write(
        "UPDATE rules SET status = 'recalled' WHERE id = ?", (rid,))


def test_list_rules_excludes_recalled(storage):
    live = storage.insert_rule(RuleRow(id=0, title="Live", keywords="k"))
    dead = storage.insert_rule(RuleRow(id=0, title="Dead", keywords="k"))
    _recall(storage, dead)
    ids = {r.id for r in storage.list_rules(include_archived=True)}
    assert live in ids and dead not in ids


def test_search_drops_recalled_even_with_include_archived(storage, tmp_path):
    counters = SqliteCounters(db=storage._db)
    rid = storage.insert_rule(RuleRow(id=0, title="Secret", keywords="k"))
    hit = SearchHit(entity_type=EntityType.RULE, entity_id=rid, score=1.0)
    # Visible before recall.
    assert _score_and_format_rule(hit, storage, counters, include_archived=True) is not None
    _recall(storage, rid)
    # Never surfaced after recall, even with the audit escape hatch.
    assert _score_and_format_rule(hit, storage, counters, include_archived=True) is None
    assert _score_and_format_rule(hit, storage, counters, include_archived=False) is None


def test_watcher_does_not_revive_recalled_rule(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "w.db"))
    s.ensure_schema()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    w = RulesWatcher(s, rules_dir, tmp_path, debounce_ms=50)
    try:
        f = rules_dir / "r.md"
        f.write_text("# Rule R\n\n**Keywords:** kw\n\nbody\n", encoding="utf-8")
        w.sync_once()
        row = s.find_rule_by_file_path("rules/r.md")
        assert row is not None
        _recall(s, row.id)

        # Edit the file (would normally re-mirror) and re-sync.
        f.write_text("# Rule R\n\n**Keywords:** kw\n\nEDITED body\n", encoding="utf-8")
        w.sync_once()

        after = s.find_by_id(EntityType.RULE, row.id)
        # Still recalled, not revived; the file present means it isn't archived
        # as an orphan either.
        assert after.status == "recalled"
        assert not after.archived
    finally:
        w.stop()
