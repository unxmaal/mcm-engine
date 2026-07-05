"""link_knowledge's relation vocabulary is discoverable (issue #49).

The allowed relations are a sealed Literal, VALID_RELATIONS is derived from it
(can't drift), and the tool docstring lists them so they surface in the schema.
"""
from __future__ import annotations

from typing import get_args

from mcp.server.fastmcp import FastMCP

from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.relations import (
    VALID_RELATIONS,
    RelationType,
    register_relations_tools,
)
from mcm_engine.tracker import NudgeConfig, SessionTracker


def test_valid_relations_is_derived_from_the_literal():
    assert VALID_RELATIONS == set(get_args(RelationType))
    assert VALID_RELATIONS == {"causes", "contradicts", "fixes", "related", "supersedes"}


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_link_knowledge_docstring_lists_every_relation(tmp_path):
    db = KnowledgeDB(tmp_path / "k.db")
    migrate_core(db)
    mcp = _FakeMCP()
    register_relations_tools(mcp, db, SessionTracker(NudgeConfig()))

    doc = mcp.tools["link_knowledge"].__doc__ or ""
    for rel in ("causes", "contradicts", "fixes", "related", "supersedes"):
        assert rel in doc, f"link_knowledge docstring omits '{rel}'"
