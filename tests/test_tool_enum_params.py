"""Closed-set tool params surface as schema enums, not bare `str` (c5 Phase 3).

Context-engineering cleanup: params with a known, runtime-validated vocabulary
are annotated with a Literal so the allowed values reach the generated MCP tool
schema instead of living only in docstring prose. This guards against anyone
reverting a promoted param back to `str`.
"""
from __future__ import annotations

from pathlib import Path
from typing import get_args, get_type_hints

from mcm_engine.backends import EntityTypeLiteral
from mcm_engine.db import KnowledgeDB
from mcm_engine.hierarchy import KINDS, SCOPES
from mcm_engine.schema import migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _register_all(tmp_path) -> dict:
    db = KnowledgeDB(tmp_path / "k.db")
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", lambda *a, **k: [])
    register_rules_tools(mcp, db, tracker, "proj", [tmp_path], tmp_path)
    return mcp.tools


def _enum(fn, param) -> set:
    return set(get_args(get_type_hints(fn)[param]))


def _literal_values(fn, param) -> tuple[set, bool]:
    """Return (string vocabulary, optional?) for a param whose annotation is a
    Literal or `Literal | None`."""
    vals: set = set()
    optional = False
    for a in get_args(get_type_hints(fn)[param]):
        if a is type(None):
            optional = True
            continue
        nested = get_args(a)  # Literal nested inside Optional
        vals.update(nested if nested else {a})
    return vals, optional


ENTITY_KINDS = set(get_args(EntityTypeLiteral))


def test_entity_type_params_are_enums(tmp_path):
    tools = _register_all(tmp_path)
    assert _enum(tools["pin_item"], "entry_type") == ENTITY_KINDS
    assert _enum(tools["unpin_item"], "entry_type") == ENTITY_KINDS
    # promote_to_rule can't promote a rule to a rule — subset without "rule"
    assert _enum(tools["promote_to_rule"], "source_type") == {"knowledge", "negative", "error"}


def test_search_scope_is_an_enum(tmp_path):
    tools = _register_all(tmp_path)
    assert _enum(tools["search"], "scope") == {"all", "knowledge", "negative", "errors", "rules"}


def test_rule_metadata_scope_and_kind_are_enums(tmp_path):
    tools = _register_all(tmp_path)
    # Optional Literals: the vocabulary, and None allowed (None means "skip").
    scope_vals, scope_opt = _literal_values(tools["set_rule_metadata"], "scope")
    kind_vals, kind_opt = _literal_values(tools["set_rule_metadata"], "kind")
    assert scope_vals == set(SCOPES) and scope_opt
    assert kind_vals == set(KINDS) and kind_opt


def test_import_rules_on_duplicate_is_an_enum(tmp_path):
    tools = _register_all(tmp_path)
    assert _enum(tools["import_rules"], "on_duplicate") == {"update", "skip", "error"}
