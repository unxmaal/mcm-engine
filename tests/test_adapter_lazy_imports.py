"""The remote adapter modules must be importable on a bare install
(no `[postgres]` / `[redis]` / `[opensearch]` extras). Only INSTANTIATING
an adapter should require its client lib, and the error must point at
the right extra.

Why this matters: `registry.resolve()` imports the adapter class — if
that transitively imports psycopg/redis/opensearchpy, then any user who
installs `mcm-engine` without extras can't even enumerate which backends
exist. The contract is: name lookup is dep-free, construction needs the
lib.
"""
from __future__ import annotations

import builtins
import importlib
import sys
from typing import Iterable

import pytest

from mcm_engine.backends import MissingDependencyError


# ---------------------------------------------------------------------------
# Helper: block specific client libs from being importable for the duration
# of a test, then force-reimport the adapter modules so their `import` lines
# resolve against the blocked import.
# ---------------------------------------------------------------------------


def _block_imports(monkeypatch: pytest.MonkeyPatch, prefixes: Iterable[str]) -> None:
    """Patch __import__ to raise ImportError for any name whose top-level
    module matches one of `prefixes`. Also evict already-cached versions
    of those modules + dependent adapter modules from sys.modules so the
    next import re-resolves through the patched __import__."""
    blocked = tuple(prefixes)

    # Evict the blocked client libs themselves.
    for name in list(sys.modules):
        if name in blocked or any(name.startswith(p + ".") for p in blocked):
            monkeypatch.delitem(sys.modules, name, raising=False)

    # Evict any already-cached adapter modules so they re-import.
    for name in list(sys.modules):
        if name.startswith("mcm_engine.adapters."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    original_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".", 1)[0]
        if top in blocked:
            raise ImportError(f"No module named '{name}' (blocked by test)")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)


# ---------------------------------------------------------------------------
# Importability — module + class must load even with the client lib absent.
# ---------------------------------------------------------------------------


def test_postgres_storage_module_imports_without_psycopg(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.storage")
    assert hasattr(mod, "PostgresStorage")


def test_postgres_counters_module_imports_without_psycopg(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.counters")
    assert hasattr(mod, "PostgresCounters")


def test_postgres_search_module_imports_without_psycopg(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.search")
    assert hasattr(mod, "PostgresSearch")


def test_redis_counters_module_imports_without_redis(monkeypatch):
    _block_imports(monkeypatch, ["redis"])
    mod = importlib.import_module("mcm_engine.adapters.redis.counters")
    assert hasattr(mod, "RedisCounters")


def test_opensearch_search_module_imports_without_opensearchpy(monkeypatch):
    _block_imports(monkeypatch, ["opensearchpy"])
    mod = importlib.import_module("mcm_engine.adapters.opensearch.search")
    assert hasattr(mod, "OpenSearchSearch")


# ---------------------------------------------------------------------------
# Registry resolution — also must not pull in the client lib.
# ---------------------------------------------------------------------------


def test_registry_resolves_remote_adapters_without_client_libs(monkeypatch):
    """Bare-install scenario: a user doing `pip install mcm-engine` (no
    extras) must still be able to enumerate/resolve adapter classes."""
    _block_imports(monkeypatch, ["psycopg", "redis", "opensearchpy"])
    # Force registry re-init so entry-point discovery runs through patched
    # __import__.
    monkeypatch.delitem(sys.modules, "mcm_engine.registry", raising=False)
    from mcm_engine.registry import AdapterRegistry

    reg = AdapterRegistry()
    # These all do importlib.import_module under the hood — should not
    # crash because the adapter modules no longer eager-import their libs.
    assert reg.resolve(reg.GROUP_STORAGE, "postgres").__name__ == "PostgresStorage"
    assert reg.resolve(reg.GROUP_COUNTERS, "postgres").__name__ == "PostgresCounters"
    assert reg.resolve(reg.GROUP_SEARCH, "postgres").__name__ == "PostgresSearch"
    assert reg.resolve(reg.GROUP_COUNTERS, "redis").__name__ == "RedisCounters"
    assert reg.resolve(reg.GROUP_SEARCH, "opensearch").__name__ == "OpenSearchSearch"


# ---------------------------------------------------------------------------
# Construction must raise MissingDependencyError naming the right extra.
# ---------------------------------------------------------------------------


def test_postgres_storage_construction_without_psycopg_raises(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.storage")
    with pytest.raises(MissingDependencyError) as exc:
        mod.PostgresStorage("postgresql://nowhere")
    assert "mcm-engine[postgres]" in str(exc.value)


def test_postgres_counters_construction_without_psycopg_raises(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.counters")
    with pytest.raises(MissingDependencyError) as exc:
        mod.PostgresCounters("postgresql://nowhere")
    assert "mcm-engine[postgres]" in str(exc.value)


def test_postgres_search_construction_without_psycopg_raises(monkeypatch):
    _block_imports(monkeypatch, ["psycopg"])
    mod = importlib.import_module("mcm_engine.adapters.postgres.search")
    with pytest.raises(MissingDependencyError) as exc:
        mod.PostgresSearch("postgresql://nowhere")
    assert "mcm-engine[postgres]" in str(exc.value)


def test_redis_counters_construction_without_redis_raises(monkeypatch):
    _block_imports(monkeypatch, ["redis"])
    mod = importlib.import_module("mcm_engine.adapters.redis.counters")
    with pytest.raises(MissingDependencyError) as exc:
        mod.RedisCounters("redis://nowhere:6379/0")
    assert "mcm-engine[redis]" in str(exc.value)


def test_opensearch_search_construction_without_opensearchpy_raises(monkeypatch):
    _block_imports(monkeypatch, ["opensearchpy"])
    mod = importlib.import_module("mcm_engine.adapters.opensearch.search")
    with pytest.raises(MissingDependencyError) as exc:
        # OpenSearch needs a storage kwarg too, but the lib-check fires
        # before any other arg validation.
        mod.OpenSearchSearch(url="http://nowhere:9200", storage=None)
    assert "mcm-engine[opensearch]" in str(exc.value)
