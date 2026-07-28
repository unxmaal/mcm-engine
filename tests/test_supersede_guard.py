"""supersede_rule guard + unsupersede recovery (issues #99, #100).

#99: reject self-supersede (old==new) and superseding by an already-superseded
rule, either of which hides both rules with no live successor.
#100: unsupersede_rule revives a superseded rule (restore_rule only un-archives).
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
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
    s = SqliteStorage(db=db)  # shares the connection with the tools' context
    mcp = _FakeMCP()
    register_rules_tools(mcp, db, SessionTracker(NudgeConfig()), "proj",
                         [tmp_path], tmp_path)
    a = s.insert_rule(RuleRow(id=0, title="Rule A", keywords="a"))
    b = s.insert_rule(RuleRow(id=0, title="Rule B", keywords="b"))
    return mcp.tools, s, a, b


def _status(s, rid):
    return s.find_by_id(EntityType.RULE, rid).status


def test_self_supersede_refused(env):
    tools, s, a, _b = env
    out = tools["supersede_rule"](a, a)
    assert "Refused" in out and "itself" in out
    assert _status(s, a) == "active"


def test_cyclic_supersede_refused_leaves_successor_live(env):
    tools, s, a, b = env
    # A superseded by B is a legitimate supersede.
    assert "Superseded" in tools["supersede_rule"](a, b)
    assert _status(s, a) == "superseded" and _status(s, b) == "active"
    # Closing the cycle (B superseded by the now-dead A) must be refused, so B
    # stays live — the real #87/#88 incident no longer hides both.
    out = tools["supersede_rule"](b, a)
    assert "Refused" in out
    assert _status(s, b) == "active"


def test_unsupersede_revives(env):
    tools, s, a, b = env
    tools["supersede_rule"](a, b)
    assert _status(s, a) == "superseded"
    out = tools["unsupersede_rule"](a)
    assert "Unsuperseded" in out
    row = s.find_by_id(EntityType.RULE, a)
    assert row.status == "active" and row.superseded_by is None


def test_unsupersede_noop_when_active(env):
    tools, _s, _a, b = env
    assert "not superseded" in tools["unsupersede_rule"](b)


def test_unsupersede_missing_rule(env):
    tools, *_ = env
    assert "not found" in tools["unsupersede_rule"](987654321)
