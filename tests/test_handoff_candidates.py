"""Session-end candidate surfacing.

Covers the two best-effort storage queries (`list_unlinked_knowledge`,
`list_promotable_knowledge`) and their wiring into `session_handoff`'s
"Before you go" section. These power the link_knowledge / promote_to_rule
suggestions that the aggregate nudges never surface.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, KnowledgeRow, RelationRow
from mcm_engine.config import NudgeConfig
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import SessionTracker


@pytest.fixture
def storage(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "store.db"))
    s.ensure_schema()
    return s


def _add(storage, topic, summary="s"):
    return storage.insert_knowledge(KnowledgeRow(id=0, topic=topic, summary=summary))


def _link(storage, src, tgt):
    storage.insert_relation(RelationRow(
        id=0,
        source_type=EntityType.KNOWLEDGE, source_id=src,
        target_type=EntityType.KNOWLEDGE, target_id=tgt,
        relation="related",
    ))


def _bump_hits(storage, kid, n):
    storage._db.execute_write(
        "UPDATE knowledge SET hit_count = ? WHERE id = ?", (n, kid),
    )
    storage._db.commit()


class TestListUnlinkedKnowledge:
    def test_returns_only_unlinked(self, storage):
        a = _add(storage, "alpha")
        b = _add(storage, "beta")
        _link(storage, a, b)  # a and b now both participate in a relation
        c = _add(storage, "gamma")
        ids = {r.id for r in storage.list_unlinked_knowledge(limit=5)}
        assert c in ids
        assert a not in ids and b not in ids

    def test_target_side_also_counts_as_linked(self, storage):
        a = _add(storage, "alpha")
        b = _add(storage, "beta")
        _link(storage, a, b)
        # b is only ever a target, but must still be considered linked.
        ids = {r.id for r in storage.list_unlinked_knowledge()}
        assert b not in ids

    def test_empty_when_all_linked(self, storage):
        a = _add(storage, "alpha")
        b = _add(storage, "beta")
        _link(storage, a, b)
        assert storage.list_unlinked_knowledge() == []

    def test_respects_limit(self, storage):
        for i in range(7):
            _add(storage, f"k{i}")
        assert len(storage.list_unlinked_knowledge(limit=3)) == 3

    def test_most_recent_first(self, storage):
        _add(storage, "old")
        newest = _add(storage, "new")
        rows = storage.list_unlinked_knowledge()
        assert rows[0].id == newest


class TestListPromotableKnowledge:
    def test_returns_high_hit(self, storage):
        low = _add(storage, "low")
        high = _add(storage, "high")
        _bump_hits(storage, high, 10)
        ids = {r.id for r in storage.list_promotable_knowledge(min_hits=5)}
        assert high in ids
        assert low not in ids

    def test_order_by_hits_desc(self, storage):
        a = _add(storage, "a")
        b = _add(storage, "b")
        _bump_hits(storage, a, 6)
        _bump_hits(storage, b, 9)
        rows = storage.list_promotable_knowledge(min_hits=5)
        assert rows[0].id == b

    def test_empty_when_none_qualify(self, storage):
        _add(storage, "a")
        assert storage.list_promotable_knowledge(min_hits=5) == []

    def test_respects_limit(self, storage):
        for i in range(5):
            kid = _add(storage, f"k{i}")
            _bump_hits(storage, kid, 6)
        assert len(storage.list_promotable_knowledge(min_hits=5, limit=2)) == 2


class _FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


def _handoff_env(db):
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_session_tools(mcp, db, tracker, "test-project", [])
    return mcp


class TestSessionHandoffSurfacing:
    def test_surfaces_unlinked_knowledge(self, db):
        db.execute_write(
            "INSERT INTO knowledge (topic, summary) VALUES (?, ?)",
            ("needs-a-link", "x"),
        )
        db.commit()
        mcp = _handoff_env(db)
        result = mcp["session_handoff"](status="done")
        assert "Before you go" in result
        assert "needs-a-link" in result
        assert "link_knowledge" in result

    def test_no_section_when_nothing_to_suggest(self, db):
        mcp = _handoff_env(db)
        result = mcp["session_handoff"](status="done")
        assert "Before you go" not in result
        assert "Counters reset" in result

    def test_handoff_still_records_with_suggestions(self, db):
        db.execute_write(
            "INSERT INTO knowledge (topic, summary) VALUES (?, ?)",
            ("orphan", "x"),
        )
        db.commit()
        mcp = _handoff_env(db)
        result = mcp["session_handoff"](status="done")
        # Surfacing is additive — the handoff itself still succeeds.
        assert "Session handoff recorded" in result
