"""Phase 4 (#64): the rule hierarchy drives behavior.

Three seams where importance/scope stop being columns you look at and start
changing what the engine does:

  1. Ranking — importance and universal scope lift a rule in compose_rank,
     but relevance still dominates (a far better text match wins).
  2. session_start injects the top (invariant) tier so the highest-binding
     rules are in front of the agent every session, not waiting to be recalled.
  3. find_conflicting_rules uses importance as the tiebreak — the higher-tier
     rule is named the keeper, the lower yields.
"""
from __future__ import annotations

from mcm_engine import hierarchy
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.scoring import compose_rank
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.session import register_session_tools
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

    def __contains__(self, n):
        return n in self._t


def _base(**kw):
    d = dict(relevance=0.5, hit_count=0, reinforcement_count=0, pinned=False, age_days=None)
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# 1. ranking
# ---------------------------------------------------------------------------


def test_importance_lifts_rank():
    assert compose_rank(**_base(importance=2)) > compose_rank(**_base(importance=0))


def test_universal_scope_lifts_rank():
    assert compose_rank(**_base(scope="universal")) > compose_rank(**_base(scope="conditional"))


def test_defaults_match_lowest_tier():
    """Omitting the new args (every non-rule caller) equals importance 0 /
    non-universal — so knowledge ranking is unchanged."""
    assert compose_rank(**_base()) == compose_rank(**_base(importance=0, scope="conditional"))


def test_relevance_still_dominates_importance():
    """A far better text match at importance 0 must still beat a weak match at
    importance 2 — the hierarchy nudges, it doesn't override relevance."""
    strong_ref = compose_rank(**_base(relevance=1.0, importance=0, scope="conditional"))
    weak_inv = compose_rank(**_base(relevance=0.0, importance=2, scope="universal"))
    assert strong_ref > weak_inv


# ---------------------------------------------------------------------------
# 2. session_start invariant injection
# ---------------------------------------------------------------------------


def _wire_session(tmp_path):
    db = KnowledgeDB(tmp_path / "s.db")
    migrate_core(db)
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000, mandatory_stop_turns=100000))
    register_session_tools(mcp, db, tracker, "t", [])
    return mcp, SqliteStorage(db=db)


def test_session_start_injects_invariant_tier(tmp_path):
    mcp, storage = _wire_session(tmp_path)
    rid = storage.insert_rule(RuleRow(id=0, title="Use uv for all Python", keywords="uv"))
    storage.set_rule_metadata(rid, importance=2, actor="t")
    storage.insert_rule(RuleRow(id=0, title="minecraft server port fact", keywords="mc"))
    out = mcp["session_start"]()
    assert "Invariant" in out
    assert "Use uv for all Python" in out
    # a plain importance-0 fact is NOT injected
    assert "minecraft server port fact" not in out


def test_session_start_omits_section_when_no_invariants(tmp_path):
    mcp, storage = _wire_session(tmp_path)
    storage.insert_rule(RuleRow(id=0, title="just a fact", keywords="k"))
    out = mcp["session_start"]()
    assert "Invariant" not in out


# ---------------------------------------------------------------------------
# 3. conflict tiebreak by importance
# ---------------------------------------------------------------------------


def _wire_rules(tmp_path):
    db = KnowledgeDB(tmp_path / "r.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000, mandatory_stop_turns=100000))
    register_rules_tools(mcp, db, tracker, "t", [rules_dir], tmp_path, files_authoritative=False)
    return mcp, SqliteStorage(db=db)


def test_conflict_tiebreak_names_higher_importance_keeper(tmp_path):
    mcp, storage = _wire_rules(tmp_path)
    a = storage.insert_rule(RuleRow(
        id=0, title="cache invalidation strategy policy", keywords="cache invalidation",
        content="always invalidate immediately on every write"))
    b = storage.insert_rule(RuleRow(
        id=0, title="cache invalidation strategy policy", keywords="cache invalidation",
        content="never invalidate eagerly rely on ttl expiry only"))
    storage.set_rule_metadata(a, importance=2, actor="t")  # a is the invariant

    out = mcp["find_conflicting_rules"]()

    assert f"#{a}" in out and f"#{b}" in out
    assert "importance" in out.lower()
    # a (importance 2) is the keeper; b yields and is the supersede target
    assert "OVERRIDES" in out
    assert f"supersede #{b}" in out


def test_conflict_equal_importance_leaves_decision_to_human(tmp_path):
    mcp, storage = _wire_rules(tmp_path)
    a = storage.insert_rule(RuleRow(
        id=0, title="cache invalidation strategy policy", keywords="cache invalidation",
        content="always invalidate immediately on every write"))
    b = storage.insert_rule(RuleRow(
        id=0, title="cache invalidation strategy policy", keywords="cache invalidation",
        content="never invalidate eagerly rely on ttl expiry only"))
    # both default importance 0
    out = mcp["find_conflicting_rules"]()
    assert f"#{a}" in out and f"#{b}" in out
    assert "OVERRIDES" not in out
