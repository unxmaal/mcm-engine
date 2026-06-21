"""Regression tests for the cutover-test-plan defects #1 and #3:

  #1 — `search` tool accepts `scope=` but ignores it (always queries
       all entity types). Pass = narrows to the requested scope.

  #3 — `search` returns archived rules instead of filtering them out
       by default. Pass = archived rules don't appear unless
       `include_archived=True` is passed.

Both surfaced during the mcm2 cutover smoke test (Phase A.5).
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, KnowledgeRow, NegativeRow, ErrorRow, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
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
def wired(tmp_path):
    """A db + search tool + helpers, seeded with one row per entity type
    that shares a single distinctive search term."""
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    db_path = tmp_path / "scope.db"
    db = KnowledgeDB(db_path)
    migrate_core(db)
    storage = SqliteStorage(db=db)

    # Each entity type gets a row containing the term "needle-token".
    storage.insert_knowledge(KnowledgeRow(
        id=0, topic="needle-token in knowledge", summary="x", kind="finding",
    ))
    storage.insert_negative(NegativeRow(
        id=0, category="needle-token in negative", what_failed="x",
    ))
    storage.insert_error(ErrorRow(
        id=0, pattern="needle-token in error",
    ))
    rule_id = storage.insert_rule(RuleRow(
        id=0, title="needle-token in rule", keywords="needle",
    ))

    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    register_search_tools(mcp, db, tracker, plugin_search_scopes=[])

    return {
        "search": mcp["search"],
        "storage": storage,
        "rule_id": rule_id,
    }


# ---------------------------------------------------------------------------
# Defect #1 — scope=… filter
# ---------------------------------------------------------------------------


def test_scope_all_returns_every_entity_type(wired):
    """Default scope still returns hits from every type (no regression)."""
    result = wired["search"](query="needle-token")
    assert "[KNOWLEDGE/" in result
    assert "[NEGATIVE]" in result
    assert "[ERROR]" in result
    assert "[RULE]" in result


def test_scope_knowledge_returns_only_knowledge(wired):
    result = wired["search"](query="needle-token", scope="knowledge")
    assert "[KNOWLEDGE/" in result
    assert "[NEGATIVE]" not in result, "scope=knowledge leaked NEGATIVE result"
    assert "[ERROR]" not in result, "scope=knowledge leaked ERROR result"
    assert "[RULE]" not in result, "scope=knowledge leaked RULE result"


def test_scope_negative_returns_only_negative(wired):
    result = wired["search"](query="needle-token", scope="negative")
    assert "[NEGATIVE]" in result
    assert "[KNOWLEDGE/" not in result
    assert "[ERROR]" not in result
    assert "[RULE]" not in result


def test_scope_errors_returns_only_errors(wired):
    result = wired["search"](query="needle-token", scope="errors")
    assert "[ERROR]" in result
    assert "[KNOWLEDGE/" not in result
    assert "[NEGATIVE]" not in result
    assert "[RULE]" not in result


def test_scope_rules_returns_only_rules(wired):
    result = wired["search"](query="needle-token", scope="rules")
    assert "[RULE]" in result
    assert "[KNOWLEDGE/" not in result
    assert "[NEGATIVE]" not in result
    assert "[ERROR]" not in result


def test_scope_unknown_value_falls_back_to_all(wired):
    """An unknown scope string shouldn't silently drop everything; it
    should behave like the default (all). The cutover test surfaces
    bad scopes — better to return too much than nothing."""
    result = wired["search"](query="needle-token", scope="nonsense")
    assert "[KNOWLEDGE/" in result
    assert "[RULE]" in result


# ---------------------------------------------------------------------------
# Defect #3 — archived rules leak into search
# ---------------------------------------------------------------------------


def test_archived_rule_not_in_default_search(wired):
    """Soft-deleting a rule must hide it from the default search."""
    wired["storage"].soft_delete_rule(wired["rule_id"])
    result = wired["search"](query="needle-token")
    assert "[RULE]" not in result, (
        "archived rule appeared in search — defect #3 from cutover A.5"
    )


def test_archived_rule_invisible_even_when_scope_is_rules(wired):
    """Narrowing to scope=rules + the rule being archived = no result."""
    wired["storage"].soft_delete_rule(wired["rule_id"])
    result = wired["search"](query="needle-token", scope="rules")
    assert "[RULE]" not in result


def test_unarchived_rule_returns_to_search(wired):
    """Restore should bring the rule back into search results."""
    wired["storage"].soft_delete_rule(wired["rule_id"])
    wired["storage"].restore_rule(wired["rule_id"])
    result = wired["search"](query="needle-token")
    assert "[RULE]" in result


def test_archived_knowledge_not_filtered(wired):
    """Knowledge/negative/error rows don't have an `archived` column —
    the filter is rule-specific. Searching for those should be
    unaffected by the rule archival path."""
    wired["storage"].soft_delete_rule(wired["rule_id"])
    result = wired["search"](query="needle-token", scope="knowledge")
    assert "[KNOWLEDGE/" in result
