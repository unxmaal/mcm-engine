"""Issue #31: read-only consolidation report — merge + conflict + stale candidates.

Composes #30 (find_near_duplicates) + #32 (find_conflicts) + a staleness
heuristic into one report. Read-only; mutates nothing.
"""
from __future__ import annotations

import sys
from datetime import datetime

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.consolidate import consolidation_report
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core

NOW = datetime(2026, 6, 1, 12, 0, 0)
RECENT = "2026-05-30 12:00:00"   # ~2 days before NOW
OLD = "2026-01-01 12:00:00"      # ~151 days before NOW


def _env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "c.db"))
    migrate_core(db)
    return db, SqliteStorage(db=db)


def _seed(db, storage):
    ids = {}
    # near-duplicate pair (identical whole text) -> merge bucket only
    for k in ("dup_a", "dup_b"):
        ids[k] = storage.insert_rule(RuleRow(
            id=0, title="Widget carb ratio", keywords="widget ratio carb",
            content="The ratio is computed from the reference amount and carbs."))
    # same-topic / divergent-body pair -> conflict bucket only
    ids["conf_a"] = storage.insert_rule(RuleRow(
        id=0, title="Sync convergence policy", keywords="sync convergence lww replicas",
        content="Use last-writer-wins to converge replicas quickly and simply."))
    ids["conf_b"] = storage.insert_rule(RuleRow(
        id=0, title="Sync convergence policy", keywords="sync convergence lww replicas",
        content="Never use last-writer-wins; surface conflicts for review — it silently drops data."))
    # lonely, aged, unreinforced -> stale bucket only
    ids["stale"] = storage.insert_rule(RuleRow(
        id=0, title="Ancient forgotten note", keywords="ancient forgotten obscure legacy",
        content="Some old content nobody references anymore about a legacy subsystem."))
    # fresh + distinct -> no bucket
    ids["healthy"] = storage.insert_rule(RuleRow(
        id=0, title="Fresh useful guidance", keywords="fresh useful current design",
        content="Actively relevant guidance about the current system architecture."))
    for k, rid in ids.items():
        db.execute_write("UPDATE rules SET created_at=? WHERE id=?",
                         (OLD if k == "stale" else RECENT, rid))
    return ids


def test_report_sorts_candidates_into_the_right_buckets(tmp_path):
    db, storage = _env(tmp_path)
    ids = _seed(db, storage)
    rep = consolidation_report(storage, max_age_days=90, now=NOW)

    merge_ids = {m["id"] for cluster in rep["merge_candidates"] for m in cluster}
    assert {ids["dup_a"], ids["dup_b"]} <= merge_ids

    conflict_ids = ({c["a"]["id"] for c in rep["conflict_candidates"]}
                    | {c["b"]["id"] for c in rep["conflict_candidates"]})
    assert {ids["conf_a"], ids["conf_b"]} <= conflict_ids

    stale_ids = {s["id"] for s in rep["stale_candidates"]}
    assert stale_ids == {ids["stale"]}          # exactly the aged, unreinforced one

    # the healthy rule appears in no bucket
    assert ids["healthy"] not in merge_ids
    assert ids["healthy"] not in conflict_ids
    assert ids["healthy"] not in stale_ids

    assert rep["summary"]["active_rules"] == 6
    assert rep["summary"]["stale_candidates"] == 1


def test_near_duplicate_is_not_also_a_conflict(tmp_path):
    db, storage = _env(tmp_path)
    ids = _seed(db, storage)
    rep = consolidation_report(storage, max_age_days=90, now=NOW)
    conflict_ids = ({c["a"]["id"] for c in rep["conflict_candidates"]}
                    | {c["b"]["id"] for c in rep["conflict_candidates"]})
    assert ids["dup_a"] not in conflict_ids and ids["dup_b"] not in conflict_ids


def test_report_is_deterministic(tmp_path):
    db, storage = _env(tmp_path)
    _seed(db, storage)
    r1 = consolidation_report(storage, max_age_days=90, now=NOW)
    r2 = consolidation_report(storage, max_age_days=90, now=NOW)
    assert r1 == r2


def test_report_mutates_nothing(tmp_path):
    db, storage = _env(tmp_path)
    _seed(db, storage)
    before = [(r.id, r.title, r.status if hasattr(r, "status") else None)
              for r in storage.iter_entries(EntityType.RULE)]
    consolidation_report(storage, now=NOW)
    after = [(r.id, r.title, r.status if hasattr(r, "status") else None)
             for r in storage.iter_entries(EntityType.RULE)]
    assert before == after


def test_superseded_and_archived_excluded(tmp_path):
    db, storage = _env(tmp_path)
    a = storage.insert_rule(RuleRow(id=0, title="Old approach", keywords="approach x",
                                    content="do it the old way"))
    b = storage.insert_rule(RuleRow(id=0, title="New approach", keywords="approach y",
                                    content="do it the new way"))
    storage.supersede_rule(a, b, "tester")
    rep = consolidation_report(storage, now=NOW)
    assert rep["summary"]["active_rules"] == 1   # superseded 'a' excluded


def test_cli_consolidate_dispatch(tmp_path, monkeypatch, capsys):
    dbp = tmp_path / "cli.db"
    db = KnowledgeDB(str(dbp))
    migrate_core(db)
    storage = SqliteStorage(db=db)
    storage.insert_rule(RuleRow(id=0, title="Solo rule", keywords="solo",
                                content="only one rule in this store"))

    from mcm_engine.cli import main as cli_main
    monkeypatch.setattr(sys, "argv",
                        ["mcm-engine", "consolidate", "--from", f"sqlite:///{dbp}"])
    cli_main()
    out = capsys.readouterr().out
    assert "Consolidation report" in out
    assert "read-only" in out.lower()
