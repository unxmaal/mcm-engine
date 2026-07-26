"""link_knowledge's relation vocabulary is discoverable (issue #49).

The allowed relations are a sealed Literal and VALID_RELATIONS is derived from
it (can't drift). The `relation` param is annotated with that Literal, so the
vocabulary surfaces as an enum in the generated tool schema rather than as prose
in the docstring.
"""
from __future__ import annotations

from typing import get_args, get_type_hints

from mcp.server.fastmcp import FastMCP

from mcm_engine.backends import EntityTypeLiteral
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


def test_link_knowledge_params_surface_the_sealed_enums(tmp_path):
    """The vocabulary lives in the param annotations (schema enum), not prose."""
    db = KnowledgeDB(tmp_path / "k.db")
    migrate_core(db)
    mcp = _FakeMCP()
    register_relations_tools(mcp, db, SessionTracker(NudgeConfig()))

    hints = get_type_hints(mcp.tools["link_knowledge"])
    # relation is the sealed relation vocabulary
    assert set(get_args(hints["relation"])) == VALID_RELATIONS
    # source_type / target_type are the entity-kind enum, not a bare str
    entity_kinds = set(get_args(EntityTypeLiteral))
    assert set(get_args(hints["source_type"])) == entity_kinds
    assert set(get_args(hints["target_type"])) == entity_kinds
