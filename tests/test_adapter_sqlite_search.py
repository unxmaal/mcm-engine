"""Conformance for the embedded SQLite SearchBackend."""
from __future__ import annotations

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    EntityType,
    KnowledgeRow,
    NegativeRow,
    RuleRow,
    SearchBackend,
    SearchHit,
)


@pytest.fixture
def store(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    p = str(tmp_path / "search.db")
    s = SqliteStorage(db_path=p)
    s.ensure_schema()
    return p, s


@pytest.fixture
def search(store):
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    p, _ = store
    return SqliteSearch(db_path=p)


def test_protocol_runtime_check(search):
    assert isinstance(search, SearchBackend)
    assert search.CONTRACT_VERSION == CONTRACT_VERSION


def test_search_finds_inserted_knowledge(store, search):
    _, s = store
    s.insert_knowledge(KnowledgeRow(
        id=0, topic="postgres tsvector setup", summary="how-to", kind="finding",
    ))
    hits = search.search("tsvector")
    assert len(hits) >= 1
    assert hits[0].entity_type == EntityType.KNOWLEDGE
    assert hits[0].score > 0  # higher = better


def test_search_returns_search_hits(store, search):
    _, s = store
    s.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="hello world", kind="finding"))
    hits = search.search("hello")
    assert all(isinstance(h, SearchHit) for h in hits)


def test_search_respects_entity_types(store, search):
    _, s = store
    s.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="alpha unique-token", kind="finding"))
    s.insert_negative(NegativeRow(id=0, category="C", what_failed="alpha unique-token"))
    hits_k = search.search("unique-token", entity_types={EntityType.KNOWLEDGE})
    hits_n = search.search("unique-token", entity_types={EntityType.NEGATIVE})
    assert all(h.entity_type == EntityType.KNOWLEDGE for h in hits_k)
    assert all(h.entity_type == EntityType.NEGATIVE for h in hits_n)


def test_search_empty_result_for_unknown_query(search):
    assert search.search("zzqqxxnotanything") == []


def test_search_pinned_flag_propagates(store, search):
    _, s = store
    k = s.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="pinned-tag", kind="finding"))
    s.set_pinned(EntityType.KNOWLEDGE, k, True)
    hits = search.search("pinned-tag")
    assert hits
    assert hits[0].is_pinned is True


def test_search_higher_score_is_better(store, search):
    """SearchHit.score is normalized to 'higher = better' across adapters."""
    _, s = store
    # Two distinct entries — both match, order should be score-descending.
    s.insert_knowledge(KnowledgeRow(id=0, topic="alpha alpha alpha", summary="x", kind="finding"))
    s.insert_knowledge(KnowledgeRow(id=0, topic="alpha", summary="x", kind="finding"))
    hits = search.search("alpha")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_reindex_does_not_error(store, search):
    _, s = store
    s.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    search.reindex()  # All types
    search.reindex(EntityType.KNOWLEDGE)  # Single type
    # After reindex, search still works
    assert search.search("t")
