"""record_decision (Phase 1): first-class decisions on the knowledge table +
depends_on causal edges. No new schema — kind='decision' rides insert_knowledge,
based_on refs become depends_on relations, supersedes_decision reuses the
supersede_knowledge path.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.decisions import register_decisions_tools
from mcm_engine.tools.relations import VALID_RELATIONS, register_relations_tools
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
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    storage = SqliteStorage(db=db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_decisions_tools(mcp, db, tracker, project_name="mcm")
    register_relations_tools(mcp, db, tracker)
    return mcp.tools, storage


def _seed_knowledge(storage, topic="evidence", summary="a prior finding"):
    return storage.insert_knowledge(KnowledgeRow(
        id=0, topic=topic, kind="finding", summary=summary))


def test_record_decision_stores_kind_decision(env):
    tools, storage = env
    out = tools["record_decision"](
        topic="pick relation vocab", scenario="need a causal verb",
        reasoning="depends_on is directional", outcome="add depends_on",
        confidence=0.8,
    )
    assert "Recorded decision #" in out
    row = storage.find_knowledge_by_topic_kind("pick relation vocab", "decision")
    assert row is not None
    assert row.kind == "decision"
    assert row.summary == "add depends_on"
    assert "**Scenario:**" in row.detail and "Confidence: 0.80" in row.detail


def test_based_on_creates_depends_on_edges(env):
    tools, storage = env
    ev = _seed_knowledge(storage, topic="evidence-1")
    err = storage.insert_error(ErrorRow(id=0, pattern="boom at line 5"))
    out = tools["record_decision"](
        topic="d", scenario="s", reasoning="r", outcome="o",
        based_on=[f"knowledge#{ev}", f"error#{err}"],
    )
    assert "depends_on: knowledge#" in out
    dec_id = storage.find_knowledge_by_topic_kind("d", "decision").id
    outgoing = storage.list_outgoing_relations(EntityType.KNOWLEDGE, dec_id)
    verbs = {(r.relation, r.target_type, r.target_id) for r in outgoing}
    assert ("depends_on", EntityType.KNOWLEDGE, ev) in verbs
    assert ("depends_on", EntityType.ERROR, err) in verbs


def test_bad_refs_are_skipped_not_fatal(env):
    tools, storage = env
    out = tools["record_decision"](
        topic="d", scenario="s", reasoning="r", outcome="o",
        based_on=["knowledge#9999", "garbage", "knowledge#"],
    )
    assert "Recorded decision #" in out       # still stored
    assert "skipped refs:" in out
    assert "not found" in out and "malformed" in out
    dec_id = storage.find_knowledge_by_topic_kind("d", "decision").id
    assert storage.list_outgoing_relations(EntityType.KNOWLEDGE, dec_id) == []


def test_supersedes_prior_decision(env):
    tools, storage = env
    tools["record_decision"](topic="old", scenario="s", reasoning="r",
                             outcome="use X")
    old_id = storage.find_knowledge_by_topic_kind("old", "decision").id
    out = tools["record_decision"](
        topic="new", scenario="s", reasoning="changed mind", outcome="use Y",
        supersedes_decision=old_id,
    )
    assert f"supersedes decision #{old_id}" in out
    old = storage.find_by_id(EntityType.KNOWLEDGE, old_id)
    assert old.status == "superseded"
    new_id = storage.find_knowledge_by_topic_kind("new", "decision").id
    assert old.superseded_by == new_id
    # supersedes audit relation recorded new --[supersedes]--> old
    outs = storage.list_outgoing_relations(EntityType.KNOWLEDGE, new_id)
    assert any(r.relation == "supersedes" and r.target_id == old_id for r in outs)


def test_supersede_missing_target_refused(env):
    tools, _storage = env
    out = tools["record_decision"](topic="d", scenario="s", reasoning="r",
                                   outcome="o", supersedes_decision=4242)
    assert "NOT_FOUND" in out


def test_requires_topic_and_outcome(env):
    tools, _storage = env
    assert "needs at least" in tools["record_decision"](
        topic="", scenario="s", reasoning="r", outcome="")


def test_depends_on_is_in_relation_vocabulary(env):
    tools, storage = env
    a = _seed_knowledge(storage, topic="a")
    b = _seed_knowledge(storage, topic="b")
    assert "depends_on" in VALID_RELATIONS
    out = tools["link_knowledge"]("knowledge", a, "knowledge", b, "depends_on")
    assert "depends_on" in out
