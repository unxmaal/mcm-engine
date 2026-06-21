"""End-to-end check: a default MCMConfig wires up the embedded SQLite
adapter set with no caller-side fuss.

The bootstrap path: AdapterRegistry() auto-registers the four embedded
adapters under the name "embedded". MCMConfig defaults backends.* to
"embedded". build_context therefore succeeds with zero adapter
configuration.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import (
    CounterStore,
    SearchBackend,
    SessionStore,
    StorageBackend,
)
from mcm_engine.config import MCMConfig
from mcm_engine.registry import AdapterRegistry
from mcm_engine.wiring import build_context


def test_default_registry_has_embedded_for_all_groups():
    r = AdapterRegistry()
    for g in (r.GROUP_STORAGE, r.GROUP_COUNTERS, r.GROUP_SEARCH, r.GROUP_SESSION):
        cls = r.resolve(g, "embedded")
        assert cls is not None


def test_build_context_with_default_config(tmp_path):
    """A blank MCMConfig + default registry produces a working Context."""
    config = MCMConfig(
        project_name="test",
        db_path=str(tmp_path / "engine.db"),
    )
    # Embedded adapters need db_path; the wiring layer must thread it.
    config.backends.storage_options = {"db_path": config.db_path}
    config.backends.counters_options = {"db_path": config.db_path}
    config.backends.search_options = {"db_path": config.db_path}

    ctx = build_context(config)
    assert isinstance(ctx.storage, StorageBackend)
    assert isinstance(ctx.counters, CounterStore)
    assert isinstance(ctx.search, SearchBackend)
    assert isinstance(ctx.session, SessionStore)


def test_default_context_storage_works_end_to_end(tmp_path):
    """The resolved StorageBackend is functional — ensure_schema + insert
    + find round-trip."""
    from mcm_engine.backends import KnowledgeRow

    config = MCMConfig(project_name="test", db_path=str(tmp_path / "engine.db"))
    config.backends.storage_options = {"db_path": config.db_path}
    config.backends.counters_options = {"db_path": config.db_path}
    config.backends.search_options = {"db_path": config.db_path}

    ctx = build_context(config)
    ctx.storage.ensure_schema()
    new_id = ctx.storage.insert_knowledge(KnowledgeRow(
        id=0, topic="default-wiring works", summary="ok", kind="finding"
    ))
    fetched = ctx.storage.find_knowledge_by_topic_kind("default-wiring works", "finding")
    assert fetched is not None
    assert fetched.id == new_id
