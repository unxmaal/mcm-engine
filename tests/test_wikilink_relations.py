"""Wikilink [[slug]] -> rule->rule relation creation in sync_rules.

Written test-first. `extract_wikilinks` and the relation-building pass in
sync_rules do not exist yet when these are first run (red).
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType
from mcm_engine.config import NudgeConfig
from mcm_engine.tools.rules import extract_wikilinks, register_rules_tools
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


@pytest.fixture
def sync_env(db, project_root):
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    rules_path = project_root / "rules"
    register_rules_tools(mcp, db, tracker, "test-project", [rules_path], project_root)
    storage = SqliteStorage(db=db)  # shares the same connection as the tools
    return mcp, storage, rules_path


def _write(rules_path, slug, body=""):
    (rules_path / f"{slug}.md").write_text(
        f"# {slug.replace('-', ' ').title()}\n\n**Keywords:** k\n\n{body}\n",
        encoding="utf-8",
    )


def _outgoing(storage, rule):
    return storage.list_outgoing_relations(EntityType.RULE, rule.id)


class TestExtractWikilinks:
    def test_finds_links(self):
        assert extract_wikilinks("see [[alpha]] and [[beta-gamma]]") == {"alpha", "beta-gamma"}

    def test_empty_when_none(self):
        assert extract_wikilinks("no links here at all") == set()

    def test_dedups(self):
        assert extract_wikilinks("[[a]] then [[a]] again") == {"a"}

    def test_ignores_single_brackets(self):
        assert extract_wikilinks("[not a link] and (parens)") == set()

    def test_strips_inner_whitespace(self):
        assert extract_wikilinks("[[ spaced ]]") == {"spaced"}


class TestSyncCreatesRelations:
    def test_link_becomes_relation(self, sync_env):
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "this links to [[beta]]")
        _write(rules_path, "beta")
        mcp["sync_rules"]()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        b = storage.find_rule_by_file_path("rules/beta.md")
        out = _outgoing(storage, a)
        assert any(
            r.target_id == b.id and r.target_type == EntityType.RULE for r in out
        )

    def test_unresolved_link_is_silently_skipped(self, sync_env):
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "points at [[ghost-that-does-not-exist]]")
        mcp["sync_rules"]()  # must not raise
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert _outgoing(storage, a) == []

    def test_self_link_skipped(self, sync_env):
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "refers to itself [[alpha]]")
        mcp["sync_rules"]()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert _outgoing(storage, a) == []

    def test_idempotent_across_runs(self, sync_env):
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "links to [[beta]]")
        _write(rules_path, "beta")
        mcp["sync_rules"]()
        mcp["sync_rules"]()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert len(_outgoing(storage, a)) == 1

    def test_links_resolve_regardless_of_file_order(self, sync_env):
        # 'alpha' links to 'zeta', which sorts after it — the relation pass
        # must see the full slug map, not just files processed so far.
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "links to [[zeta]]")
        _write(rules_path, "zeta")
        mcp["sync_rules"]()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        z = storage.find_rule_by_file_path("rules/zeta.md")
        assert any(r.target_id == z.id for r in _outgoing(storage, a))

    def test_count_reported_in_message(self, sync_env):
        mcp, storage, rules_path = sync_env
        _write(rules_path, "alpha", "links to [[beta]]")
        _write(rules_path, "beta")
        result = mcp["sync_rules"]()
        assert "link" in result.lower()
