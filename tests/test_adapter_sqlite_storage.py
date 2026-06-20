"""MCM2-02: embedded SQLite StorageBackend conformance.

Exercises the full StorageBackend Protocol against the embedded SQLite
adapter. The same tests will be re-parametrized against Postgres in
Phase 1 — assertions stay shape-based (not bit-identical ordering).
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RelationRow,
    RuleRow,
    SessionRow,
    SnapshotRow,
    StorageBackend,
)


@pytest.fixture
def storage(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    s = SqliteStorage(db_path=str(tmp_path / "store.db"))
    s.ensure_schema()
    return s


# ---- Class-level contract -----------------------------------------------


def test_class_declares_contract_version(storage):
    assert storage.CONTRACT_VERSION == CONTRACT_VERSION


def test_class_declares_capabilities_set(storage):
    assert hasattr(storage, "capabilities")
    assert isinstance(storage.capabilities, set)


def test_protocol_runtime_check(storage):
    """SqliteStorage satisfies the StorageBackend Protocol."""
    assert isinstance(storage, StorageBackend)


# ---- Knowledge CRUD -----------------------------------------------------


def test_insert_then_find_knowledge_by_topic_kind(storage):
    row = KnowledgeRow(
        id=0,  # adapter assigns
        topic="test topic",
        summary="hello",
        kind="finding",
        detail="extended detail",
    )
    new_id = storage.insert_knowledge(row)
    assert new_id > 0

    fetched = storage.find_knowledge_by_topic_kind("test topic", "finding")
    assert fetched is not None
    assert fetched.id == new_id
    assert fetched.topic == "test topic"
    assert fetched.summary == "hello"
    assert fetched.detail == "extended detail"
    assert fetched.kind == "finding"


def test_find_knowledge_by_topic_kind_returns_none_when_absent(storage):
    assert storage.find_knowledge_by_topic_kind("nope", "finding") is None


def test_find_similar_knowledge_returns_top_match(storage):
    storage.insert_knowledge(KnowledgeRow(
        id=0, topic="postgres tsvector setup", summary="howto", kind="finding"
    ))
    storage.insert_knowledge(KnowledgeRow(
        id=0, topic="completely unrelated thing", summary="howto", kind="finding"
    ))
    result = storage.find_similar_knowledge("postgres tsvector")
    assert result is not None
    assert "tsvector" in result.topic


def test_update_knowledge(storage):
    new_id = storage.insert_knowledge(KnowledgeRow(
        id=0, topic="t", summary="old", kind="finding"
    ))
    storage.update_knowledge(new_id, summary="new", detail="added")
    fetched = storage.find_knowledge_by_topic_kind("t", "finding")
    assert fetched.summary == "new"
    assert fetched.detail == "added"


# ---- Negative + Errors --------------------------------------------------


def test_insert_negative(storage):
    new_id = storage.insert_negative(NegativeRow(
        id=0, category="bug", what_failed="thing exploded"
    ))
    assert new_id > 0
    assert storage.entry_exists(EntityType.NEGATIVE, new_id)
    assert storage.count_by_type(EntityType.NEGATIVE) == 1


def test_insert_error(storage):
    new_id = storage.insert_error(ErrorRow(
        id=0, pattern="ZeroDivisionError"
    ))
    assert new_id > 0
    assert storage.entry_exists(EntityType.ERROR, new_id)
    assert storage.count_by_type(EntityType.ERROR) == 1


# ---- Rules --------------------------------------------------------------


def test_insert_then_find_rule_by_title_and_file_path(storage):
    new_id = storage.insert_rule(RuleRow(
        id=0,
        title="my rule",
        keywords="kw1,kw2",
        file_path="rules/methods/my-rule.md",
        description="a description",
        category="methods",
    ))
    by_title = storage.find_rule_by_title("my rule")
    by_path = storage.find_rule_by_file_path("rules/methods/my-rule.md")
    assert by_title is not None and by_title.id == new_id
    assert by_path is not None and by_path.id == new_id
    assert by_title.keywords == "kw1,kw2"


def test_update_rule(storage):
    new_id = storage.insert_rule(RuleRow(
        id=0, title="t", keywords="kw",
    ))
    storage.update_rule(new_id, description="d2", category="c2")
    fetched = storage.find_rule_by_title("t")
    assert fetched.description == "d2"
    assert fetched.category == "c2"


def test_soft_delete_and_restore_rule(storage):
    new_id = storage.insert_rule(RuleRow(id=0, title="t", keywords="kw"))
    storage.soft_delete_rule(new_id)
    fetched = storage.find_rule_by_title("t")
    # Soft-deleted rows are still reachable but flagged.
    assert fetched is not None
    assert fetched.archived is True
    assert fetched.archived_at is not None

    storage.restore_rule(new_id)
    fetched = storage.find_rule_by_title("t")
    assert fetched.archived is False
    assert fetched.archived_at is None


def test_list_rules_with_file_paths_skips_pathless(storage):
    storage.insert_rule(RuleRow(id=0, title="A", keywords="kw", file_path="a.md"))
    storage.insert_rule(RuleRow(id=0, title="B", keywords="kw", file_path=None))
    storage.insert_rule(RuleRow(id=0, title="C", keywords="kw", file_path="c.md"))
    rules = storage.list_rules_with_file_paths()
    titles = sorted(r.title for r in rules)
    assert titles == ["A", "C"]


# ---- Relations ----------------------------------------------------------


def test_insert_relation_and_list_outgoing_incoming(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    r = storage.insert_rule(RuleRow(id=0, title="rule", keywords="kw"))

    rid = storage.insert_relation(RelationRow(
        id=0,
        source_type=EntityType.KNOWLEDGE, source_id=k,
        target_type=EntityType.RULE,      target_id=r,
        relation="supersedes",
    ))
    assert rid is not None

    outgoing = storage.list_outgoing_relations(EntityType.KNOWLEDGE, k)
    incoming = storage.list_incoming_relations(EntityType.RULE, r)
    assert len(outgoing) == 1
    assert outgoing[0].relation == "supersedes"
    assert len(incoming) == 1
    assert incoming[0].source_id == k


def test_insert_relation_duplicate_returns_none(storage):
    """UNIQUE-violation on (source_type, source_id, target_type, target_id,
    relation) returns None per the Protocol, not raises."""
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    r = storage.insert_rule(RuleRow(id=0, title="rule", keywords="kw"))
    args = dict(
        source_type=EntityType.KNOWLEDGE, source_id=k,
        target_type=EntityType.RULE,      target_id=r,
        relation="supersedes",
    )
    first = storage.insert_relation(RelationRow(id=0, **args))
    second = storage.insert_relation(RelationRow(id=0, **args))
    assert first is not None
    assert second is None


# ---- Sessions + snapshots -----------------------------------------------


def test_insert_session_and_get_last(storage):
    new_id = storage.insert_session(SessionRow(
        id=0, status="working", current_task="testing"
    ))
    last = storage.get_last_session()
    assert last is not None
    assert last.id == new_id
    assert last.status == "working"


def test_snapshot_seq_increments_per_session(storage):
    s1 = storage.insert_session(SessionRow(id=0, status="s1"))
    s2 = storage.insert_session(SessionRow(id=0, status="s2"))

    assert storage.next_snapshot_seq(s1) == 1
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=s1, goal="g"))
    assert storage.next_snapshot_seq(s1) == 2

    # Independent counter per session
    assert storage.next_snapshot_seq(s2) == 1


def test_snapshot_seq_for_null_session(storage):
    """Snapshots may exist without a parent session — sequence is computed
    over the orphan group."""
    assert storage.next_snapshot_seq(None) == 1
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=None, goal="g"))
    assert storage.next_snapshot_seq(None) == 2


def test_get_last_snapshot(storage):
    s = storage.insert_session(SessionRow(id=0, status="s"))
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=s, goal="first"))
    storage.insert_snapshot(SnapshotRow(id=0, sequence_num=2, session_id=s, goal="second"))
    last = storage.get_last_snapshot()
    assert last.goal == "second"


# ---- Cross-entity (enum-driven) ----------------------------------------


def test_set_pinned_then_list_pinned(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding"))
    storage.set_pinned(EntityType.KNOWLEDGE, k, True)

    pinned = storage.list_pinned(EntityType.KNOWLEDGE)
    assert len(pinned) == 1
    assert pinned[0].id == k


def test_set_pinned_false_restores(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    storage.set_pinned(EntityType.KNOWLEDGE, k, True)
    storage.set_pinned(EntityType.KNOWLEDGE, k, False)
    assert len(storage.list_pinned(EntityType.KNOWLEDGE)) == 0


@pytest.mark.parametrize("etype", list(EntityType))
def test_count_by_type_zero_initially(storage, etype):
    assert storage.count_by_type(etype) == 0


def test_count_by_type_with_project_filter(storage):
    storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding", project="alpha"))
    storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding", project="beta"))
    storage.insert_knowledge(KnowledgeRow(id=0, topic="C", summary="c", kind="finding", project=None))

    assert storage.count_by_type(EntityType.KNOWLEDGE) == 3
    assert storage.count_by_type(EntityType.KNOWLEDGE, project="alpha") == 1
    assert storage.count_by_type(EntityType.KNOWLEDGE, project="beta") == 1
    assert storage.count_by_type(EntityType.KNOWLEDGE, pinned=False) == 3


def test_entry_exists(storage):
    k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
    assert storage.entry_exists(EntityType.KNOWLEDGE, k) is True
    assert storage.entry_exists(EntityType.KNOWLEDGE, 99999) is False
