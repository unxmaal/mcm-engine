"""trace_chain (Phase 2): bounded N-hop directed walk over the relations graph.

Generalizes the 1-hop get_related / _spread_related_rules into a depth-limited,
single-verb, directional, cycle-guarded traversal. Read-only.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow, RelationRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.relations import register_relations_tools
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
    mcp = _FakeMCP()
    register_relations_tools(mcp, db, SessionTracker(NudgeConfig()))
    return mcp.tools, storage


def _k(storage, topic):
    return storage.insert_knowledge(KnowledgeRow(id=0, topic=topic, kind="finding",
                                                 summary=topic))


def _link(storage, s, t, rel="depends_on", st=EntityType.KNOWLEDGE,
          tt=EntityType.KNOWLEDGE):
    storage.insert_relation(RelationRow(id=0, source_type=st, source_id=s,
                                        target_type=tt, target_id=t, relation=rel))


def test_walks_multiple_hops(env):
    tools, s = env
    a, b, c = _k(s, "A"), _k(s, "B"), _k(s, "C")
    _link(s, a, b)
    _link(s, b, c)
    out = tools["trace_chain"]("knowledge", a, relation="depends_on")
    assert "A" in out and "B" in out and "C" in out
    assert "--[depends_on]-->" in out


def test_max_depth_stops_the_walk(env):
    tools, s = env
    a, b, c = _k(s, "A"), _k(s, "B"), _k(s, "C")
    _link(s, a, b)
    _link(s, b, c)
    out = tools["trace_chain"]("knowledge", a, relation="depends_on", max_depth=1)
    assert "B" in out
    # C is two hops away -> excluded at depth 1
    assert "C:" not in out


def test_relation_filter_excludes_other_verbs(env):
    tools, s = env
    a, b, d = _k(s, "A"), _k(s, "B"), _k(s, "D")
    _link(s, a, b, rel="depends_on")
    _link(s, a, d, rel="related")
    out = tools["trace_chain"]("knowledge", a, relation="depends_on")
    assert "B" in out
    assert "D:" not in out
    # no filter walks both
    both = tools["trace_chain"]("knowledge", a)
    assert "B" in both and "D" in both


def test_direction_incoming(env):
    tools, s = env
    a, b, c = _k(s, "A"), _k(s, "B"), _k(s, "C")
    _link(s, a, b)
    _link(s, b, c)
    # from C, following edges backwards reaches B then A
    out = tools["trace_chain"]("knowledge", c, relation="depends_on",
                               direction="incoming")
    assert "B" in out and "A" in out
    assert "<--[depends_on]--" in out
    # outgoing from C reaches nothing
    fwd = tools["trace_chain"]("knowledge", c, direction="outgoing")
    assert "no relationships" in fwd


def test_cycle_terminates(env):
    tools, s = env
    a, b = _k(s, "A"), _k(s, "B")
    _link(s, a, b)
    _link(s, b, a)  # cycle
    out = tools["trace_chain"]("knowledge", a, max_depth=10)
    # terminates, each node rendered once past the root
    assert out.count("[KNOWLEDGE] B") == 1


def test_cross_type_chain(env):
    tools, s = env
    dec = _k(s, "decision-topic")
    err = s.insert_error(ErrorRow(id=0, pattern="boom at line 5"))
    _link(s, dec, err, tt=EntityType.ERROR)
    out = tools["trace_chain"]("knowledge", dec, relation="depends_on")
    assert "[ERROR]" in out and "boom at line 5" in out


def test_guards(env):
    tools, s = env
    a = _k(s, "A")
    assert "not found" in tools["trace_chain"]("knowledge", 9999)
    assert "Invalid direction" in tools["trace_chain"]("knowledge", a,
                                                       direction="sideways")
    assert "Invalid relation" in tools["trace_chain"]("knowledge", a,
                                                      relation="bogus")


def test_trace_chain_is_read_only():
    assert "trace_chain" in SessionTracker.READ_ONLY_TOOLS
