"""Issue #16 — tool-layer convergence on source_of_truth (Layer 1) +
restore_rule recovery tool.

database mode: add_rule writes no markdown file and read_rule prefers the
stored body over any local file. files mode keeps the historical behavior.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.rules import register_rules_tools


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def deco(fn):
            self._tools[fn.__name__] = fn
            return fn
        return deco

    def __getitem__(self, name):
        return self._tools[name]


def _wire(tmp_path, files_authoritative):
    db = KnowledgeDB(tmp_path / "r.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000,
        mandatory_stop_turns=100000,
    ))
    register_rules_tools(
        mcp, db, tracker, "t", [rules_dir], tmp_path,
        files_authoritative=files_authoritative,
    )
    return mcp, SqliteStorage(db=db), rules_dir


# ---- add_rule: file write is gated on mode ----

def test_add_rule_database_mode_writes_no_file(tmp_path):
    mcp, storage, rules_dir = _wire(tmp_path, files_authoritative=False)
    mcp["add_rule"](title="DBOnly", keywords="kw", content="body in db")
    assert list(rules_dir.rglob("*.md")) == []          # nothing on disk
    row = storage.find_rule_by_title("DBOnly")
    assert row.content == "body in db"
    assert not row.file_path                              # None or ""


def test_add_rule_files_mode_writes_file(tmp_path):
    mcp, storage, rules_dir = _wire(tmp_path, files_authoritative=True)
    mcp["add_rule"](title="OnDisk", keywords="kw", content="body")
    assert len(list(rules_dir.rglob("*.md"))) == 1
    assert storage.find_rule_by_title("OnDisk").file_path


# ---- read_rule: authority order flips with mode ----

def test_read_rule_database_mode_prefers_db(tmp_path):
    mcp, storage, rules_dir = _wire(tmp_path, files_authoritative=False)
    storage.insert_rule(RuleRow(id=0, title="X", keywords="kw",
                                file_path="rules/x.md", content="DBBODY"))
    (rules_dir / "x.md").write_text("FILEBODY", encoding="utf-8")
    assert "DBBODY" in mcp["read_rule"](file_path="rules/x.md")


def test_read_rule_files_mode_prefers_disk(tmp_path):
    mcp, storage, rules_dir = _wire(tmp_path, files_authoritative=True)
    storage.insert_rule(RuleRow(id=0, title="X", keywords="kw",
                                file_path="rules/x.md", content="DBBODY"))
    (rules_dir / "x.md").write_text("FILEBODY", encoding="utf-8")
    assert "FILEBODY" in mcp["read_rule"](file_path="rules/x.md")


# ---- restore_rule ----

def test_restore_rule_by_ids(tmp_path):
    mcp, storage, _ = _wire(tmp_path, files_authoritative=False)
    rid = storage.insert_rule(RuleRow(id=0, title="R", keywords="kw", content="c"))
    storage.soft_delete_rule(rid)
    res = mcp["restore_rule"](rule_ids=[rid], actor="me")
    assert res["restored"] == 1 and res["rule_ids"] == [rid]
    assert storage.find_rule_by_title("R").archived is False
    assert "restored" in [e.event_type for e in storage.list_rule_events(rid)]


def test_restore_rule_all_archived(tmp_path):
    mcp, storage, _ = _wire(tmp_path, files_authoritative=False)
    ids = [storage.insert_rule(RuleRow(id=0, title=f"R{i}", keywords="kw", content="c"))
           for i in range(3)]
    storage.soft_delete_rule(ids[0])
    storage.soft_delete_rule(ids[1])
    res = mcp["restore_rule"](all_archived=True)
    assert res["restored"] == 2
    assert storage.find_rule_by_title("R0").archived is False
    assert storage.find_rule_by_title("R1").archived is False
    # the never-archived one is untouched (no spurious restore)
    assert storage.find_rule_by_title("R2").archived is False


def test_restore_rule_noop_on_unarchived(tmp_path):
    mcp, storage, _ = _wire(tmp_path, files_authoritative=False)
    rid = storage.insert_rule(RuleRow(id=0, title="Live", keywords="kw", content="c"))
    res = mcp["restore_rule"](rule_ids=[rid])
    assert res["restored"] == 0
