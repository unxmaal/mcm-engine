"""Reusable adapter conformance suites.

Third-party adapter packages import the relevant mixin class and provide
the fixture that constructs their implementation. The shared test bodies
then run against the adapter unchanged, giving every backend the same
guarantees the embedded SQLite reference passes.

Usage from an adapter test file::

    import pytest
    from mcm_engine.testing.conformance import StorageConformance

    class TestPostgresStorage(StorageConformance):
        @pytest.fixture
        def storage(self, postgres_conn):
            store = PostgresStorage(conn=postgres_conn)
            store.ensure_schema()
            return store

The mixins do not own a fixture — the subclass must provide one named
``storage`` / ``counters`` / ``search`` / ``session_store`` as appropriate.
Other fixtures the mixin relies on (e.g. ``store_db_path`` for
CounterConformance) are described in each mixin's docstring.

The shapes asserted here are deliberately backend-agnostic: counts,
existence, dataclass field round-trips, normalized score ordering. Bit-
identical orderings or vendor-specific behaviors must not appear in this
suite — they belong in the adapter's own tests.
"""
from __future__ import annotations

import pytest

from ..backends import (
    CONTRACT_VERSION,
    CounterStore,
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RelationRow,
    RuleRow,
    SearchBackend,
    SearchHit,
    SessionRow,
    SessionStore,
    SnapshotRow,
    StorageBackend,
)


# ---------------------------------------------------------------------------
# StorageBackend
# ---------------------------------------------------------------------------


class StorageConformance:
    """Conformance tests for any StorageBackend implementation.

    Subclasses MUST provide a ``storage`` fixture returning a freshly
    schema'd StorageBackend instance.
    """

    # ---- Class-level contract -------------------------------------------

    def test_class_declares_contract_version(self, storage):
        assert storage.CONTRACT_VERSION == CONTRACT_VERSION

    def test_class_declares_capabilities_set(self, storage):
        assert hasattr(storage, "capabilities")
        assert isinstance(storage.capabilities, set)

    def test_protocol_runtime_check(self, storage):
        assert isinstance(storage, StorageBackend)

    # ---- Knowledge CRUD --------------------------------------------------

    def test_insert_then_find_knowledge_by_topic_kind(self, storage):
        row = KnowledgeRow(
            id=0,
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

    def test_find_knowledge_by_topic_kind_returns_none_when_absent(self, storage):
        assert storage.find_knowledge_by_topic_kind("nope", "finding") is None

    def test_find_similar_knowledge_returns_top_match(self, storage):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="postgres tsvector setup", summary="howto", kind="finding"
        ))
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="completely unrelated thing", summary="howto", kind="finding"
        ))
        result = storage.find_similar_knowledge("postgres tsvector")
        assert result is not None
        assert "tsvector" in result.topic

    def test_update_knowledge(self, storage):
        new_id = storage.insert_knowledge(KnowledgeRow(
            id=0, topic="t", summary="old", kind="finding"
        ))
        storage.update_knowledge(new_id, summary="new", detail="added")
        fetched = storage.find_knowledge_by_topic_kind("t", "finding")
        assert fetched.summary == "new"
        assert fetched.detail == "added"

    # ---- Negative + Errors ----------------------------------------------

    def test_insert_negative(self, storage):
        new_id = storage.insert_negative(NegativeRow(
            id=0, category="bug", what_failed="thing exploded"
        ))
        assert new_id > 0
        assert storage.entry_exists(EntityType.NEGATIVE, new_id)
        assert storage.count_by_type(EntityType.NEGATIVE) == 1

    def test_insert_error(self, storage):
        new_id = storage.insert_error(ErrorRow(
            id=0, pattern="ZeroDivisionError"
        ))
        assert new_id > 0
        assert storage.entry_exists(EntityType.ERROR, new_id)
        assert storage.count_by_type(EntityType.ERROR) == 1

    # ---- Rules ----------------------------------------------------------

    def test_insert_then_find_rule_by_title_and_file_path(self, storage):
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

    def test_update_rule(self, storage):
        new_id = storage.insert_rule(RuleRow(
            id=0, title="t", keywords="kw",
        ))
        storage.update_rule(new_id, description="d2", category="c2")
        fetched = storage.find_rule_by_title("t")
        assert fetched.description == "d2"
        assert fetched.category == "c2"

    def test_soft_delete_and_restore_rule(self, storage):
        new_id = storage.insert_rule(RuleRow(id=0, title="t", keywords="kw"))
        storage.soft_delete_rule(new_id)
        fetched = storage.find_rule_by_title("t")
        assert fetched is not None
        assert fetched.archived is True
        assert fetched.archived_at is not None

        storage.restore_rule(new_id)
        fetched = storage.find_rule_by_title("t")
        assert fetched.archived is False
        assert fetched.archived_at is None

    def test_list_archived_rules(self, storage):
        live = storage.insert_rule(RuleRow(id=0, title="live", keywords="kw"))
        gone = storage.insert_rule(RuleRow(id=0, title="gone", keywords="kw"))
        storage.soft_delete_rule(gone)
        archived_ids = {r.id for r in storage.list_archived_rules()}
        assert gone in archived_ids
        assert live not in archived_ids

    def test_list_rules_with_file_paths_skips_pathless(self, storage):
        storage.insert_rule(RuleRow(id=0, title="A", keywords="kw", file_path="a.md"))
        storage.insert_rule(RuleRow(id=0, title="B", keywords="kw", file_path=None))
        storage.insert_rule(RuleRow(id=0, title="C", keywords="kw", file_path="c.md"))
        rules = storage.list_rules_with_file_paths()
        titles = sorted(r.title for r in rules)
        assert titles == ["A", "C"]

    # ---- transaction() atomicity (issue #14) ----

    def test_transaction_commits_on_clean_exit(self, storage):
        with storage.transaction():
            rid = storage.insert_rule(RuleRow(id=0, title="tx-ok", keywords="kw"))
            storage.insert_rule_event(rid, "created", "someone")
        row = storage.find_rule_by_title("tx-ok")
        assert row is not None
        assert [e.event_type for e in storage.list_rule_events(row.id)] == ["created"]

    def test_transaction_rolls_back_on_exception(self, storage):
        # A rule + its event written, then an error inside the block: the whole
        # unit must roll back, leaving neither the row nor the event behind.
        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with storage.transaction():
                rid = storage.insert_rule(
                    RuleRow(id=0, title="tx-rollback", keywords="kw"))
                storage.insert_rule_event(rid, "created", "someone")
                raise _Boom()

        assert storage.find_rule_by_title("tx-rollback") is None

    def test_insert_rule_persists_content_and_attribution(self, storage):
        """v8: full body + created_by/updated_by round-trip (issue #10)."""
        new_id = storage.insert_rule(RuleRow(
            id=0, title="prov", keywords="kw",
            content="the full markdown body of the rule",
            created_by="alice", updated_by="alice",
        ))
        fetched = storage.find_rule_by_title("prov")
        assert fetched.id == new_id
        assert fetched.content == "the full markdown body of the rule"
        assert fetched.created_by == "alice"
        assert fetched.updated_by == "alice"

    # ---- Rule events (audit log) ---------------------------------------

    def test_insert_and_list_rule_events(self, storage):
        rid = storage.insert_rule(RuleRow(id=0, title="ev", keywords="kw"))
        eid = storage.insert_rule_event(
            rid, "created", "alice",
            content_hash="abc", source_repo="r", source_ref="main",
            source_commit="deadbeef", note="n",
        )
        assert eid
        events = storage.list_rule_events(rid)
        assert len(events) == 1
        e = events[0]
        assert e.rule_id == rid
        assert e.event_type == "created"
        assert e.actor == "alice"
        assert e.content_hash == "abc"
        assert e.source_repo == "r"
        assert e.source_ref == "main"
        assert e.source_commit == "deadbeef"
        assert e.note == "n"
        assert e.at is not None

    def test_rule_event_actor_defaults_to_nobody(self, storage):
        rid = storage.insert_rule(RuleRow(id=0, title="ev2", keywords="kw"))
        storage.insert_rule_event(rid, "created", "")
        events = storage.list_rule_events(rid)
        assert events[0].actor == "nobody"

    def test_list_rule_events_respects_limit(self, storage):
        rid = storage.insert_rule(RuleRow(id=0, title="ev3", keywords="kw"))
        for _ in range(3):
            storage.insert_rule_event(rid, "reinforced", "bob")
        assert len(storage.list_rule_events(rid, limit=2)) == 2
        assert len(storage.list_rule_events(rid)) == 3

    # ---- Relations ------------------------------------------------------

    def test_insert_relation_and_list_outgoing_incoming(self, storage):
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

    def test_insert_relation_duplicate_returns_none(self, storage):
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

    # ---- Sessions + snapshots -------------------------------------------

    def test_insert_session_and_get_last(self, storage):
        new_id = storage.insert_session(SessionRow(
            id=0, status="working", current_task="testing"
        ))
        last = storage.get_last_session()
        assert last is not None
        assert last.id == new_id
        assert last.status == "working"

    def test_snapshot_seq_increments_per_session(self, storage):
        s1 = storage.insert_session(SessionRow(id=0, status="s1"))
        s2 = storage.insert_session(SessionRow(id=0, status="s2"))

        assert storage.next_snapshot_seq(s1) == 1
        storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=s1, goal="g"))
        assert storage.next_snapshot_seq(s1) == 2

        # Independent counter per session
        assert storage.next_snapshot_seq(s2) == 1

    def test_snapshot_seq_for_null_session(self, storage):
        assert storage.next_snapshot_seq(None) == 1
        storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=None, goal="g"))
        assert storage.next_snapshot_seq(None) == 2

    def test_get_last_snapshot(self, storage):
        s = storage.insert_session(SessionRow(id=0, status="s"))
        storage.insert_snapshot(SnapshotRow(id=0, sequence_num=1, session_id=s, goal="first"))
        storage.insert_snapshot(SnapshotRow(id=0, sequence_num=2, session_id=s, goal="second"))
        last = storage.get_last_snapshot()
        assert last.goal == "second"

    # ---- Cross-entity (enum-driven) -------------------------------------

    def test_set_pinned_then_list_pinned(self, storage):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
        storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding"))
        storage.set_pinned(EntityType.KNOWLEDGE, k, True)

        pinned = storage.list_pinned(EntityType.KNOWLEDGE)
        assert len(pinned) == 1
        assert pinned[0].id == k

    def test_set_pinned_false_restores(self, storage):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
        storage.set_pinned(EntityType.KNOWLEDGE, k, True)
        storage.set_pinned(EntityType.KNOWLEDGE, k, False)
        assert len(storage.list_pinned(EntityType.KNOWLEDGE)) == 0

    @pytest.mark.parametrize("etype", list(EntityType))
    def test_count_by_type_zero_initially(self, storage, etype):
        assert storage.count_by_type(etype) == 0

    def test_count_by_type_with_project_filter(self, storage):
        storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding", project="alpha"))
        storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding", project="beta"))
        storage.insert_knowledge(KnowledgeRow(id=0, topic="C", summary="c", kind="finding", project=None))

        assert storage.count_by_type(EntityType.KNOWLEDGE) == 3
        assert storage.count_by_type(EntityType.KNOWLEDGE, project="alpha") == 1
        assert storage.count_by_type(EntityType.KNOWLEDGE, project="beta") == 1
        assert storage.count_by_type(EntityType.KNOWLEDGE, pinned=False) == 3

    def test_entry_exists(self, storage):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
        assert storage.entry_exists(EntityType.KNOWLEDGE, k) is True
        assert storage.entry_exists(EntityType.KNOWLEDGE, 99999) is False


# ---------------------------------------------------------------------------
# CounterStore
# ---------------------------------------------------------------------------


class CounterConformance:
    """Conformance for any CounterStore implementation.

    Subclasses MUST provide:
      - ``storage``: a StorageBackend instance (used to seed entities the
        counters then operate on). Convenience: when an adapter shares one
        connection between storage and counters, point both fixtures at
        the same backing connection.
      - ``counters``: a CounterStore instance backed by the same store.
    """

    def test_protocol_runtime_check(self, counters):
        assert isinstance(counters, CounterStore)
        assert counters.CONTRACT_VERSION == CONTRACT_VERSION

    def test_increment_then_get_returns_value(self, storage, counters):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
        counters.increment(EntityType.KNOWLEDGE, k, "hit_count", by=3)
        snapshot = counters.get(EntityType.KNOWLEDGE, k)
        assert snapshot["hit_count"] == 3

    def test_increment_last_hit_at_sets_timestamp(self, storage, counters):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
        counters.increment(EntityType.KNOWLEDGE, k, "last_hit_at")
        snapshot = counters.get(EntityType.KNOWLEDGE, k)
        assert snapshot["last_hit_at"] is not None

    def test_unknown_counter_raises(self, storage, counters):
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
        with pytest.raises(ValueError, match="no counter column"):
            counters.increment(EntityType.KNOWLEDGE, k, "made_up_counter")

    def test_top_by_returns_descending(self, storage, counters):
        a = storage.insert_knowledge(KnowledgeRow(id=0, topic="A", summary="a", kind="finding"))
        b = storage.insert_knowledge(KnowledgeRow(id=0, topic="B", summary="b", kind="finding"))
        counters.increment(EntityType.KNOWLEDGE, a, "hit_count", by=2)
        counters.increment(EntityType.KNOWLEDGE, b, "hit_count", by=5)
        top = counters.top_by(EntityType.KNOWLEDGE, "hit_count", 10)
        assert top[0] == (b, 5.0)
        assert top[1] == (a, 2.0)

    def test_negative_only_pinned(self, counters):
        """negative_knowledge tracks pinned but not hit_count."""
        with pytest.raises(ValueError, match="hit_count"):
            counters.increment(EntityType.NEGATIVE, 1, "hit_count")

    def test_flush_returns_none(self, counters):
        assert counters.flush() is None

    def test_last_flushed_snapshot_equals_live(self, storage, counters):
        """Write-through adapters have no staleness window; remote adapters
        with batching may relax this — override the test in that case."""
        k = storage.insert_knowledge(KnowledgeRow(id=0, topic="x", summary="x", kind="finding"))
        counters.increment(EntityType.KNOWLEDGE, k, "hit_count")
        assert (
            counters.last_flushed_snapshot(EntityType.KNOWLEDGE, k)
            == counters.get(EntityType.KNOWLEDGE, k)
        )


# ---------------------------------------------------------------------------
# SearchBackend
# ---------------------------------------------------------------------------


class SearchConformance:
    """Conformance for any SearchBackend implementation.

    Subclasses MUST provide:
      - ``storage``: a StorageBackend used to seed entities for the search
        to find.
      - ``search``: a SearchBackend pointing at the same backing store.
    """

    def test_protocol_runtime_check(self, search):
        assert isinstance(search, SearchBackend)
        assert search.CONTRACT_VERSION == CONTRACT_VERSION

    def test_search_finds_inserted_knowledge(self, storage, search):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="postgres tsvector setup", summary="how-to", kind="finding",
        ))
        hits = search.search("tsvector")
        assert len(hits) >= 1
        assert hits[0].entity_type == EntityType.KNOWLEDGE
        assert hits[0].score > 0  # higher = better

    def test_search_returns_search_hits(self, storage, search):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="A", summary="hello world", kind="finding",
        ))
        hits = search.search("hello")
        assert all(isinstance(h, SearchHit) for h in hits)

    def test_search_respects_entity_types(self, storage, search):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="A", summary="alpha unique-token", kind="finding",
        ))
        storage.insert_negative(NegativeRow(
            id=0, category="C", what_failed="alpha unique-token",
        ))
        hits_k = search.search("unique-token", entity_types={EntityType.KNOWLEDGE})
        hits_n = search.search("unique-token", entity_types={EntityType.NEGATIVE})
        assert all(h.entity_type == EntityType.KNOWLEDGE for h in hits_k)
        assert all(h.entity_type == EntityType.NEGATIVE for h in hits_n)

    def test_search_empty_result_for_unknown_query(self, search):
        assert search.search("zzqqxxnotanything") == []

    def test_search_pinned_flag_propagates(self, storage, search):
        k = storage.insert_knowledge(KnowledgeRow(
            id=0, topic="A", summary="pinned-tag", kind="finding",
        ))
        storage.set_pinned(EntityType.KNOWLEDGE, k, True)
        hits = search.search("pinned-tag")
        assert hits
        assert hits[0].is_pinned is True

    def test_search_higher_score_is_better(self, storage, search):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="alpha alpha alpha", summary="x", kind="finding",
        ))
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="alpha", summary="x", kind="finding",
        ))
        hits = search.search("alpha")
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_reindex_does_not_error(self, storage, search):
        storage.insert_knowledge(KnowledgeRow(
            id=0, topic="t", summary="s", kind="finding",
        ))
        search.reindex()
        search.reindex(EntityType.KNOWLEDGE)
        assert search.search("t")


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


class SessionConformance:
    """Conformance for any SessionStore implementation.

    Subclasses MUST provide a ``session_store`` fixture returning a fresh
    SessionStore instance.
    """

    def test_protocol_runtime_check(self, session_store):
        assert isinstance(session_store, SessionStore)
        assert session_store.CONTRACT_VERSION == CONTRACT_VERSION

    def test_missing_key_returns_none(self, session_store):
        assert session_store.load_state("absent") is None

    def test_save_then_load_roundtrip(self, session_store):
        session_store.save_state("tracker", {"turn_count": 7, "last_topic": "abc"})
        assert session_store.load_state("tracker") == {
            "turn_count": 7, "last_topic": "abc",
        }

    def test_save_overwrites(self, session_store):
        session_store.save_state("k", {"a": 1})
        session_store.save_state("k", {"a": 2})
        assert session_store.load_state("k") == {"a": 2}

    def test_load_returns_copy_not_internal_ref(self, session_store):
        session_store.save_state("k", {"x": [1, 2, 3]})
        loaded = session_store.load_state("k")
        loaded["x"] = "tampered"
        again = session_store.load_state("k")
        assert again == {"x": [1, 2, 3]}
