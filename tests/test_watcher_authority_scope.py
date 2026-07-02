"""Issue #16 — watcher orphan sweep: provenance scope (Layer 2) + circuit
breaker (Layer 3).

The 177-wipe: an always-on pod's watcher walked an empty rules dir and
archived every DB rule whose file_path pointed at a (missing) file. Two
independent barriers:

  Layer 2 — the sweep only reaps rules whose file_path is under the watched
            rules_path. A provenance path (or a DB-native import) is not the
            watcher's to delete, regardless of deployment mode.
  Layer 3 — a sweep that would archive a suspicious fraction of managed rules
            in one pass aborts and logs loudly instead (transient empty dir:
            failed mount, mid-checkout).
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.files.watcher import RulesWatcher
from mcm_engine.schema import migrate_core


@pytest.fixture
def wired(tmp_path):
    db = KnowledgeDB(tmp_path / "w.db")
    migrate_core(db)
    storage = SqliteStorage(db=db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    return storage, rules_dir, tmp_path


def test_sweep_ignores_rules_outside_watched_dir(wired):
    storage, rules_dir, root = wired
    # Provenance / DB-native rule whose file_path points outside rules_dir.
    storage.insert_rule(RuleRow(id=0, title="external", keywords="kw",
                                file_path="somewhere/else/ext.md", content="c"))
    # A genuinely managed rule whose file under rules_dir is missing.
    storage.insert_rule(RuleRow(id=0, title="managed", keywords="kw",
                                file_path="rules/managed.md", content="c"))
    counts = RulesWatcher(storage, rules_dir, root).sync_once()
    assert storage.find_rule_by_title("external").archived is False
    assert storage.find_rule_by_title("managed").archived is True
    assert counts["archived"] == 1


def test_sweep_ignores_db_native_null_file_path(wired):
    storage, rules_dir, root = wired
    storage.insert_rule(RuleRow(id=0, title="dbnative", keywords="kw",
                                file_path=None, content="c"))
    RulesWatcher(storage, rules_dir, root).sync_once()
    assert storage.find_rule_by_title("dbnative").archived is False


def test_circuit_breaker_blocks_mass_archive(wired):
    storage, rules_dir, root = wired
    for i in range(10):
        storage.insert_rule(RuleRow(id=0, title=f"m{i}", keywords="kw",
                                    file_path=f"rules/m{i}.md", content="c"))
    counts = RulesWatcher(storage, rules_dir, root,
                          archive_circuit_floor=2).sync_once()
    assert counts["archive_blocked"] == 10
    assert counts["archived"] == 0
    for i in range(10):
        assert storage.find_rule_by_title(f"m{i}").archived is False


def test_small_archive_not_blocked(wired):
    storage, rules_dir, root = wired
    # One present file (upserted) + one true orphan. Deleting 1 of 2 managed
    # is 50%, but the floor guards small sweeps from tripping the breaker.
    (rules_dir / "present.md").write_text(
        "# Present\n\n**Keywords:** kw\n\nbody\n", encoding="utf-8")
    storage.insert_rule(RuleRow(id=0, title="orphan", keywords="kw",
                                file_path="rules/orphan.md", content="c"))
    counts = RulesWatcher(storage, rules_dir, root).sync_once()
    assert counts["archived"] == 1
    assert counts["archive_blocked"] == 0
    assert storage.find_rule_by_title("orphan").archived is True
