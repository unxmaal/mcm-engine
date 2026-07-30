"""source_classification carrier field on all four entity types (issue #105).

The engine stores and returns the label; it never interprets it. Covers
storage round-trip for every type, the ingest tools setting it, and the
scroll_entries reader surfacing it.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import (
    EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow,
)
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.corpus import register_corpus_tools
from mcm_engine.tools.knowledge import register_knowledge_tools
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
def storage(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "sc.db"))
    s.ensure_schema()
    return s


def test_knowledge_round_trips_label(storage):
    kid = storage.insert_knowledge(KnowledgeRow(
        id=0, topic="t", summary="s", source_classification="confidential"))
    assert storage.find_by_id(EntityType.KNOWLEDGE, kid).source_classification == "confidential"


def test_negative_round_trips_label(storage):
    nid = storage.insert_negative(NegativeRow(
        id=0, category="c", what_failed="w", source_classification="internal"))
    assert storage.find_by_id(EntityType.NEGATIVE, nid).source_classification == "internal"


def test_error_round_trips_label(storage):
    eid = storage.insert_error(ErrorRow(
        id=0, pattern="boom", source_classification="restricted"))
    assert storage.find_by_id(EntityType.ERROR, eid).source_classification == "restricted"


def test_rule_round_trips_label(storage):
    rid = storage.insert_rule(RuleRow(
        id=0, title="R", keywords="k", source_classification="public"))
    assert storage.find_by_id(EntityType.RULE, rid).source_classification == "public"


def test_unlabeled_defaults_to_none(storage):
    kid = storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s"))
    assert storage.find_by_id(EntityType.KNOWLEDGE, kid).source_classification is None


def test_add_knowledge_tool_sets_label(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    mcp = _FakeMCP()
    register_knowledge_tools(mcp, db, SessionTracker(NudgeConfig()), "proj",
                             lambda *a, **k: [])
    mcp.tools["add_knowledge"]("secret topic", "summary",
                               source_classification="confidential")
    row = s.find_knowledge_by_topic_kind("secret topic", "finding")
    assert row is not None and row.source_classification == "confidential"


def test_scroll_entries_surfaces_label(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    s.insert_knowledge(KnowledgeRow(
        id=0, topic="t", summary="s", source_classification="restricted"))
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, SessionTracker(NudgeConfig()))
    out = mcp.tools["scroll_entries"]("knowledge")
    assert "class=restricted" in out
