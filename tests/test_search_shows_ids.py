"""Search output must surface entry ids (issue #47).

link_knowledge needs (type, id) pairs, but nothing surfaced ids — so you
couldn't know what to link. Search results now carry the id inside the type
tag, e.g. `[KNOWLEDGE/FINDING #42] ...` / `[RULE #84] ...`.
"""
from __future__ import annotations

import re

import pytest

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import NudgeConfig, SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools


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


@pytest.fixture
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000,
        mandatory_stop_turns=1000, rules_check_interval=0,
        periodic_tools={},
    ))
    rules_path = tmp_path / "rules"
    rules_path.mkdir()
    search_all_fn = register_search_tools(mcp := FakeMCP(), db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
    register_rules_tools(mcp, db, tracker, "test-project", [rules_path], tmp_path)
    return mcp, db


def _id_of(db, table, where):
    return db.execute(f"SELECT id FROM {table} WHERE {where}").fetchone()["id"]


def test_knowledge_result_shows_its_id(env):
    mcp, db = env
    mcp["add_knowledge"](topic="net-new-idea", summary="a finding worth linking")
    kid = _id_of(db, "knowledge", "topic = 'net-new-idea'")

    out = mcp["search"](query="net-new-idea")

    assert f"#{kid}" in out, f"knowledge id {kid} not surfaced in search output:\n{out}"


def test_rule_result_shows_its_id(env):
    mcp, db = env
    mcp["add_rule"](title="Always pin the ratio", keywords="ratio, pin",
                    content="Pin the ratio from the reference amount.")
    rid = _id_of(db, "rules", "title = 'Always pin the ratio'")

    out = mcp["search"](query="ratio")

    assert f"#{rid}" in out, f"rule id {rid} not surfaced in search output:\n{out}"
    # id travels with the type tag so (type, id) for link_knowledge is unambiguous.
    assert re.search(rf"\[RULE #{rid}\]", out), out
