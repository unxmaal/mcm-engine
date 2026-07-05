"""Cutover defect #5: MCMServer hardcoded the embedded SQLite Context
regardless of `backends:` config. Tool calls always went to SQLite even
when YAML said `backends.storage=postgres` (or anything else).

Captured at the live cutover by comparing hit_count changes between
the Postgres and SQLite mirrors after a read_rule — only SQLite moved.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.search import SqliteSearch
from mcm_engine.adapters.sqlite.session import InMemorySession
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    KnowledgeRow,
    RuleRow,
)
from mcm_engine.config import BackendsConfig, MCMConfig
from mcm_engine.registry import AdapterRegistry
from mcm_engine.server import MCMServer


# A fake storage that wraps SqliteStorage but flips a flag whenever it
# observes a write. Used to prove MCMServer wiring goes through the
# registry, not the hardcoded SqliteStorage(db=self.db) constructor.
class _RecordingStorage(SqliteStorage):
    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    saw_insert_knowledge: bool = False

    def __init__(self, db_path: str | Any = ":memory:", db=None):
        super().__init__(db_path=db_path, db=db)
        # Per-instance flag so multiple tests don't share state.
        self._saw_insert = False

    def insert_knowledge(self, row: KnowledgeRow) -> int:
        self._saw_insert = True
        type(self).saw_insert_knowledge = True
        return super().insert_knowledge(row)


def test_mcmserver_uses_build_context_with_loaded_config(monkeypatch, tmp_path):
    """MCMServer MUST build its context (via the verified wrapper) with the
    loaded config."""
    from mcm_engine import server as server_mod
    from mcm_engine import wiring as wiring_mod

    called_with: dict[str, Any] = {}
    real = wiring_mod.build_verified_context

    def spy(config, *, registry: Optional[AdapterRegistry] = None):
        called_with["config"] = config
        return real(config, registry=registry)

    monkeypatch.setattr(server_mod, "build_verified_context", spy)

    config = MCMConfig(
        project_name="t",
        db_path=str(tmp_path / "t.db"),
    )
    MCMServer(config, project_root=tmp_path)

    assert called_with.get("config") is config, (
        "MCMServer.__init__ did NOT build its context from config — the "
        "backends config in YAML is being silently ignored. Defect #5."
    )


def test_mcmserver_ctx_is_built_via_registry_not_hardcoded(tmp_path):
    """When backends config selects a non-default storage class through
    a manually-registered adapter, MCMServer's ctx.storage must be an
    instance of THAT class — not a hardcoded SqliteStorage."""
    registry = AdapterRegistry()
    registry.register(registry.GROUP_STORAGE, "recording", _RecordingStorage)

    config = MCMConfig(
        project_name="t",
        db_path=str(tmp_path / "t.db"),
        backends=BackendsConfig(
            storage="recording",
            storage_options={"db_path": str(tmp_path / "t.db")},
        ),
    )
    server = MCMServer(config, project_root=tmp_path, registry=registry)
    assert isinstance(server.ctx.storage, _RecordingStorage), (
        f"server.ctx.storage is {type(server.ctx.storage).__name__}, "
        f"not _RecordingStorage. MCMServer is bypassing the registry."
    )


def test_add_knowledge_tool_writes_through_ctx_adapter(tmp_path):
    """End-to-end: when the registry resolves storage to _RecordingStorage,
    a call to the `add_knowledge` MCP tool MUST land on that instance.

    This is the production-truth version of the cutover finding —
    the hit_count for read_rule moved in SQLite, not Postgres, because
    the tool's register_* function built its own SqliteStorage and
    ignored ctx entirely.
    """
    _RecordingStorage.saw_insert_knowledge = False
    registry = AdapterRegistry()
    registry.register(registry.GROUP_STORAGE, "recording", _RecordingStorage)

    config = MCMConfig(
        project_name="t",
        db_path=str(tmp_path / "t.db"),
        backends=BackendsConfig(
            storage="recording",
            storage_options={"db_path": str(tmp_path / "t.db")},
        ),
    )
    server = MCMServer(config, project_root=tmp_path, registry=registry)

    # Drive the registered MCP tool. FastMCP stores tool functions
    # internally; reach for the underlying callable.
    add_knowledge_tool = server.mcp._tool_manager._tools["add_knowledge"]
    add_knowledge_tool.fn(
        topic="cutover defect 5 sentinel",
        summary="proves ctx.storage is the recording adapter",
        kind="finding",
    )

    assert _RecordingStorage.saw_insert_knowledge, (
        "add_knowledge tool did not route through ctx.storage. The tool's "
        "register_* function is still building its own SqliteStorage from "
        "raw db, ignoring the config-wired Context."
    )
