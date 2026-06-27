"""Wikilink -> rule relations must also fire on the watcher path.

stdio startup runs watcher.sync_once() (NOT the sync_rules MCP tool), so
the wikilink relation-building has to live in a shared helper both paths
call. Written test-first: mcm_engine.rules_links does not exist yet, and
sync_once() does not build relations yet (red).
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType
from mcm_engine.files.watcher import RulesWatcher
from mcm_engine.rules_links import build_wikilink_relations, extract_wikilinks


@pytest.fixture
def storage(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "w.db"))
    s.ensure_schema()
    return s


@pytest.fixture
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def watcher(storage, rules_dir, tmp_path):
    return RulesWatcher(storage, rules_dir, tmp_path, debounce_ms=50)


def _write_rule(path, title, body="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n**Keywords:** kw\n\n{body}\n", encoding="utf-8",
    )


def _outgoing(storage, rule):
    return storage.list_outgoing_relations(EntityType.RULE, rule.id)


class TestSharedHelperSurface:
    def test_extract_wikilinks_available(self):
        assert extract_wikilinks("see [[a]] and [[b-c]]") == {"a", "b-c"}


class TestSyncOnceBuildsRelations:
    def test_link_becomes_relation(self, watcher, storage, rules_dir):
        _write_rule(rules_dir / "alpha.md", "Alpha", "links to [[beta]]")
        _write_rule(rules_dir / "beta.md", "Beta")
        counts = watcher.sync_once()
        assert counts.get("links", 0) >= 1
        a = storage.find_rule_by_file_path("rules/alpha.md")
        b = storage.find_rule_by_file_path("rules/beta.md")
        assert any(r.target_id == b.id for r in _outgoing(storage, a))

    def test_unresolved_link_skipped(self, watcher, storage, rules_dir):
        _write_rule(rules_dir / "alpha.md", "Alpha", "points at [[ghost]]")
        counts = watcher.sync_once()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert _outgoing(storage, a) == []
        assert counts.get("links", 0) == 0

    def test_self_link_skipped(self, watcher, storage, rules_dir):
        _write_rule(rules_dir / "alpha.md", "Alpha", "see [[alpha]]")
        watcher.sync_once()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert _outgoing(storage, a) == []

    def test_idempotent_across_runs(self, watcher, storage, rules_dir):
        _write_rule(rules_dir / "alpha.md", "Alpha", "links to [[beta]]")
        _write_rule(rules_dir / "beta.md", "Beta")
        watcher.sync_once()
        watcher.sync_once()
        a = storage.find_rule_by_file_path("rules/alpha.md")
        assert len(_outgoing(storage, a)) == 1


class TestBuildWikilinkRelationsDirect:
    def test_returns_count(self, storage, rules_dir, tmp_path):
        # Seed rules straight through the watcher's upsert, then call the
        # helper directly — it must resolve from storage + files alone.
        w = RulesWatcher(storage, rules_dir, tmp_path, debounce_ms=50)
        _write_rule(rules_dir / "x.md", "X", "links to [[y]]")
        _write_rule(rules_dir / "y.md", "Y")
        # Upsert rows without the relation pass:
        for md in sorted(rules_dir.glob("*.md")):
            w._cascade_upsert(md)
        created = build_wikilink_relations(storage, tmp_path)
        assert created == 1
        # Second call is idempotent.
        assert build_wikilink_relations(storage, tmp_path) == 0
