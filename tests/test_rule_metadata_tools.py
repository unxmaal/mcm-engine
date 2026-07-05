"""Phase 2 MCP verbs for the rule hierarchy (issue #64).

The tuning UI reads/writes through the shared storage library directly, but the
agent path gets MCP verbs that mirror it: `list_rules` (full-column read) and
`set_rule_metadata` (the audited hierarchy write). These lock the tool surface:
list surfaces the axes + signals, set updates + validates + degrades to a
returned error string (never an exception) on bad input.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tracker import SessionTracker


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]

    def __contains__(self, name):
        return name in self._tools


@pytest.fixture
def wired(tmp_path):
    db = KnowledgeDB(tmp_path / "r.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    register_rules_tools(
        mcp, db, tracker, project_name="t",
        rules_paths=[rules_dir], project_root=tmp_path,
    )
    return {"mcp": mcp, "storage": SqliteStorage(db=db)}


def test_both_tools_are_registered(wired):
    assert "list_rules" in wired["mcp"]
    assert "set_rule_metadata" in wired["mcp"]


def test_list_rules_tool_surfaces_title_and_axes(wired):
    wired["mcp"]["add_rule"](title="Rule One", keywords="k", content="body")
    out = wired["mcp"]["list_rules"]()
    assert "Rule One" in out
    assert "importance" in out.lower()


def test_set_rule_metadata_tool_updates(wired):
    wired["mcp"]["add_rule"](title="uv rule", keywords="k", content="use uv")
    rid = wired["storage"].find_rule_by_title("uv rule").id
    wired["mcp"]["set_rule_metadata"](
        rule_id=rid, importance=2, scope="universal", kind="directive")
    r = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert (r.importance, r.scope, r.kind) == (2, "universal", "directive")


def test_set_rule_metadata_tool_rejects_invalid_without_raising(wired):
    wired["mcp"]["add_rule"](title="x", keywords="k", content="b")
    rid = wired["storage"].find_rule_by_title("x").id
    out = wired["mcp"]["set_rule_metadata"](rule_id=rid, scope="galactic")
    assert isinstance(out, str)
    assert "galactic" in out or "invalid" in out.lower() or "scope" in out.lower()
    # unchanged
    assert wired["storage"].find_by_id(EntityType.RULE, rid).scope == "conditional"


def test_set_rule_metadata_tool_unknown_rule(wired):
    out = wired["mcp"]["set_rule_metadata"](rule_id=999999, importance=1)
    assert isinstance(out, str)
    assert "not found" in out.lower() or "999999" in out
