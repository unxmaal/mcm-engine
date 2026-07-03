"""Issue #35 — spreading activation: one-hop recall over the relations graph.

After the direct search hits, rules LINKED to a hit (via the relations graph)
are surfaced too — marked [related], appended after the direct hits — so a rule
the query missed on tokens still comes up if it's linked to one that matched.
Read-only; value scales with how many links the corpus has.
"""
from __future__ import annotations

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RelationRow, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import SessionTracker


class FakeMCP:
    def __init__(self):
        self._t = {}

    def tool(self):
        def d(fn):
            self._t[fn.__name__] = fn
            return fn
        return d

    def __getitem__(self, n):
        return self._t[n]


def _wire(tmp_path):
    db = KnowledgeDB(str(tmp_path / "s.db"))
    migrate_core(db)
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200))
    register_search_tools(mcp, db, tracker, [])
    register_rules_tools(mcp, db, tracker, "test", [tmp_path / "rules"], tmp_path)
    return mcp, SqliteStorage(db=db)


def test_linked_rule_surfaces_as_related_when_query_misses_it(tmp_path):
    mcp, storage = _wire(tmp_path)
    a = storage.insert_rule(RuleRow(id=0, title="quaxolotl calibration procedure",
                                    keywords="quaxolotl calibration",
                                    content="how to calibrate the quaxolotl device"))
    b = storage.insert_rule(RuleRow(id=0, title="zubblefish maintenance schedule",
                                    keywords="zubblefish maintenance",
                                    content="when to service the zubblefish unit"))
    storage.insert_relation(RelationRow(
        id=0, source_type=EntityType.RULE, source_id=a,
        target_type=EntityType.RULE, target_id=b, relation="references"))

    # query hits only A on tokens; B shares nothing with the query
    out = mcp["search"](query="quaxolotl")
    assert "quaxolotl calibration procedure" in out       # direct hit
    assert "zubblefish maintenance schedule" in out       # surfaced via the link
    assert "[related]" in out
    # related is appended AFTER the direct hit, never above it
    assert out.index("quaxolotl calibration") < out.index("zubblefish maintenance")


def test_no_links_means_no_related(tmp_path):
    mcp, storage = _wire(tmp_path)
    storage.insert_rule(RuleRow(id=0, title="quaxolotl calibration procedure",
                                keywords="quaxolotl", content="calibrate the quaxolotl"))
    out = mcp["search"](query="quaxolotl")
    assert "[related]" not in out


def test_superseded_neighbor_is_not_surfaced(tmp_path):
    mcp, storage = _wire(tmp_path)
    a = storage.insert_rule(RuleRow(id=0, title="quaxolotl calibration procedure",
                                    keywords="quaxolotl", content="calibrate quaxolotl"))
    b = storage.insert_rule(RuleRow(id=0, title="zubblefish maintenance schedule",
                                    keywords="zubblefish", content="service zubblefish"))
    c = storage.insert_rule(RuleRow(id=0, title="zubblefish maintenance schedule v2",
                                    keywords="zubblefish", content="service zubblefish better"))
    storage.insert_relation(RelationRow(
        id=0, source_type=EntityType.RULE, source_id=a,
        target_type=EntityType.RULE, target_id=b, relation="references"))
    storage.supersede_rule(b, c, "tester")  # b is now superseded
    out = mcp["search"](query="quaxolotl")
    assert "zubblefish maintenance schedule" not in out  # superseded neighbor hidden
