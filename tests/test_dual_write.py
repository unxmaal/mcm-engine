"""Dual-write step (issue #180, 4.0 program #176).

Every mutation lands in the legacy table and in ``entries`` inside one
transaction. The outside check is ``entries.divergence``: after each write
tool and each storage mutation the two sides are identical row for row, a
failed mirror rolls the legacy write back, and a store that was tampered with
while the server was down is reconciled at the next boot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import Entry, EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.entries import divergence, reconcile
from mcm_engine.migrate import migrate
from mcm_engine.schema import migrate_core
from mcm_engine.tools.corpus import register_corpus_tools
from mcm_engine.tools.decisions import register_decisions_tools
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.relations import register_relations_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def store(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "k.db"))
    s.ensure_schema()
    return s


@pytest.fixture
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    s.ensure_schema()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    mcp = _FakeMCP()
    root = tmp_path / "proj"
    (root / "rules").mkdir(parents=True)
    search_all = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "proj", search_all)
    register_session_tools(mcp, db, tracker, "proj", [])
    register_rules_tools(mcp, db, tracker, "proj", [root / "rules"], root,
                         files_authoritative=False)
    register_relations_tools(mcp, db, tracker)
    register_decisions_tools(mcp, db, tracker, "proj")
    register_corpus_tools(mcp, db, tracker)
    return mcp.tools, s


def _seed(s: SqliteStorage) -> dict[str, int]:
    return {
        "k1": s.insert_knowledge(KnowledgeRow(id=0, topic="t1", summary="s1", detail="d")),
        "k2": s.insert_knowledge(KnowledgeRow(id=0, topic="t2", summary="s2")),
        "n": s.insert_negative(NegativeRow(id=0, category="c", what_failed="w")),
        "e": s.insert_error(ErrorRow(id=0, pattern="p", context="c")),
        "r1": s.insert_rule(RuleRow(id=0, title="r1", keywords="k", content="b1")),
        "r2": s.insert_rule(RuleRow(id=0, title="r2", keywords="k", content="b2")),
    }


def test_every_write_tool_keeps_entries_identical_to_legacy(env):
    tools, s = env
    calls = [
        ("add_knowledge", dict(topic="mahjong", summary="one line", detail="body", tags="a,b")),
        ("add_knowledge", dict(topic="second", summary="two")),
        ("add_negative", dict(category="measurement", what_failed="w", why_failed="y",
                              correct_approach="fix", severity="high")),
        ("report_error", dict(error_text="boom", context="ctx", tags="t")),
        ("add_rule", dict(title="rule one", keywords="k1", content="# body one")),
        ("add_rule", dict(title="rule two", keywords="k2", content="# body two")),
        ("record_decision", dict(topic="dec", scenario="s", reasoning="r", outcome="o")),
        ("reinforce_knowledge", dict(entry_id=1)),
        ("reinforce_rule", dict(rule_id=1, actor="eric")),
        ("pin_item", dict(entry_type="knowledge", entry_id=1)),
        ("pin_item", dict(entry_type="negative", entry_id=1)),
        ("pin_item", dict(entry_type="error", entry_id=1)),
        ("pin_item", dict(entry_type="rule", entry_id=1)),
        ("unpin_item", dict(entry_type="rule", entry_id=1)),
        ("search", dict(query="mahjong")),
        ("search", dict(query="rule")),
        ("supersede_knowledge", dict(old_id=1, new_id=2, actor="eric")),
        ("unsupersede_knowledge", dict(knowledge_id=1)),
        ("set_rule_metadata", dict(rule_id=1, importance=2, scope="universal", actor="eric")),
        ("report_outcome", dict(rule_ids=[1], passed=True, actor="judge")),
        ("report_outcome", dict(rule_ids=[1], passed=False, actor="judge")),
        ("supersede_rule", dict(old_id=1, new_id=2, actor="eric")),
        ("unsupersede_rule", dict(rule_id=1, actor="eric")),
        ("promote_to_rule", dict(source_type="knowledge", source_id=2, title="promoted",
                                 keywords="p")),
        ("link_knowledge", dict(source_type="knowledge", source_id=1, target_type="rule",
                                target_id=1, relation="supports")),
    ]
    for name, kwargs in calls:
        tools[name](**kwargs)
        assert divergence(s) == [], f"after {name}({kwargs})"
    assert s.count_entries("knowledge") == 3   # 2 add_knowledge + record_decision
    assert s.count_entries("rule") == 3        # 2 add_rule + promote_to_rule
    assert s.count_entries("negative") == 1 and s.count_entries("error") == 1
    assert s.find_entry("knowledge", 1).hit_count > 0   # search bumped it, mirrored


def test_counter_increments_are_mirrored(store):
    ids = _seed(store)
    c = SqliteCounters(db=store._db)
    c.increment(EntityType.KNOWLEDGE, ids["k1"], "hit_count", by=2)
    c.increment(EntityType.KNOWLEDGE, ids["k1"], "last_hit_at")
    c.increment(EntityType.KNOWLEDGE, ids["k1"], "reinforcement_count")
    c.increment(EntityType.KNOWLEDGE, ids["k1"], "pinned")
    c.increment(EntityType.RULE, ids["r1"], "correct_count")
    c.increment(EntityType.RULE, ids["r1"], "incorrect_count")
    c.increment(EntityType.RULE, ids["r1"], "correct_count")
    c.increment(EntityType.RULE, ids["r1"], "hit_count")
    assert divergence(store) == []
    e = store.find_entry("rule", ids["r1"])
    assert e.facets["correct_count"] == 2 and e.facets["incorrect_count"] == 1
    assert store.find_entry("knowledge", ids["k1"]).hit_count == 2


def test_failed_mirror_rolls_back_the_legacy_write(store, monkeypatch):
    def boom(self, entry, *, if_absent=False):
        raise RuntimeError("mirror down")
    monkeypatch.setattr(SqliteStorage, "insert_entry", boom)
    with pytest.raises(RuntimeError, match="mirror down"):
        store.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s"))
    assert store.count_by_type(EntityType.KNOWLEDGE) == 0
    assert store.count_entries() == 0


def test_boot_reconciles_a_store_tampered_while_down(tmp_path):
    path = str(tmp_path / "k.db")
    s = SqliteStorage(db_path=path)
    s.ensure_schema()
    ids = _seed(s)
    s.update_entry("knowledge", ids["k1"], title="tampered")
    s.delete_entry("rule", ids["r1"])
    s.insert_entry(Entry(kind="error", id=99, title="ghost"))
    assert divergence(s) == [
        "knowledge #1: entries row differs",
        "error: legacy rows 1 != entries rows 2",
        "rule: legacy rows 2 != entries rows 1",
        "rule #1: missing from entries",
    ]
    s2 = SqliteStorage(db_path=path)
    s2.ensure_schema()
    assert divergence(s2) == []
    assert s2.find_entry("error", 99) is None


def test_reconcile_reports_what_it_repaired(store):
    ids = _seed(store)
    store.update_entry("negative", ids["n"], summary="x")
    store.delete_entry("error", ids["e"])
    store.insert_entry(Entry(kind="knowledge", id=500, title="ghost"))
    assert reconcile(store) == {"checked": 6, "inserted": 1, "updated": 1, "deleted": 1}
    assert reconcile(store) == {"checked": 6, "inserted": 0, "updated": 0, "deleted": 0}


def test_migrate_populates_entries_on_the_destination(tmp_path):
    src = SqliteStorage(db_path=str(tmp_path / "src.db"))
    src.ensure_schema()
    _seed(src)
    dest = SqliteStorage(db_path=str(tmp_path / "dest.db"))
    report = migrate(src, dest)
    assert (report.knowledge, report.negative, report.errors, report.rules) == (2, 1, 1, 2)
    assert divergence(dest) == []
    assert dest.count_entries() == 6
    assert dest.next_entry_id("knowledge") == 3


def test_next_entry_id_tracks_legacy_inserts(store):
    ids = _seed(store)
    assert store.next_entry_id("knowledge") == ids["k2"] + 1
    assert store.next_entry_id("rule") == ids["r2"] + 1
