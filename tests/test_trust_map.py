"""Issue #36 — graded actor->weight trust map (late-binding correctness)."""
from __future__ import annotations

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import SessionTracker
from mcm_engine.trust import actor_weight


# --- trust.py unit ----------------------------------------------------------

def test_actor_weight_from_env(monkeypatch):
    monkeypatch.setenv("MCM_TRUST_WEIGHTS", '{"alice": 5.0, "bob": 0.1}')
    monkeypatch.delenv("MCM_TRUST_DEFAULT", raising=False)
    assert actor_weight("alice") == 5.0
    assert actor_weight("bob") == 0.1
    assert actor_weight("carol") == 1.0  # unlisted -> default 1.0


def test_actor_weight_custom_default(monkeypatch):
    monkeypatch.setenv("MCM_TRUST_WEIGHTS", "{}")
    monkeypatch.setenv("MCM_TRUST_DEFAULT", "0.5")
    assert actor_weight("nobody") == 0.5


def test_actor_weight_malformed_env_is_safe(monkeypatch):
    monkeypatch.setenv("MCM_TRUST_WEIGHTS", "not json")
    monkeypatch.delenv("MCM_TRUST_DEFAULT", raising=False)
    assert actor_weight("anyone") == 1.0


def test_list_rule_outcomes(tmp_path):
    db = KnowledgeDB(str(tmp_path / "o.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    rid = s.insert_rule(RuleRow(id=0, title="R", keywords="k", content="c"))
    s.record_outcome(rid, "alice", True)
    s.record_outcome(rid, "bob", False)
    assert s.list_rule_outcomes(rid) == [("alice", True), ("bob", False)]


# --- integration: trust weighting changes ranking ---------------------------

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


def test_trust_weighting_changes_rule_ranking(tmp_path, monkeypatch):
    db = KnowledgeDB(str(tmp_path / "t.db"))
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tr = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200))
    register_rules_tools(mcp, db, tr, project_name="t", rules_paths=[rules_dir],
                         project_root=tmp_path, files_authoritative=False)
    register_search_tools(mcp, db, tr, [], "t")
    s = SqliteStorage(db=db)

    # Two rules matching the same query, identical but for the actor who
    # reported a PASS. Distinct created_by so the outcomes count (author!=judge).
    mcp["add_rule"](title="Widget Alpha", keywords="widget shared",
                    content="alpha body", actor="author_a")
    mcp["add_rule"](title="Widget Beta", keywords="widget shared",
                    content="beta body", actor="author_b")
    ra = s.find_rule_by_title("Widget Alpha")
    rb = s.find_rule_by_title("Widget Beta")
    mcp["report_outcome"](rule_ids=[ra.id], passed=True, actor="trusted")
    mcp["report_outcome"](rule_ids=[rb.id], passed=True, actor="flaky")

    monkeypatch.setenv("MCM_TRUST_WEIGHTS", '{"trusted": 5.0, "flaky": 0.1}')
    out = mcp["search"](query="widget shared")
    assert "Widget Alpha" in out and "Widget Beta" in out
    # trusted's pass weighs far more than flaky's, so Alpha ranks first.
    assert out.index("Widget Alpha") < out.index("Widget Beta")
