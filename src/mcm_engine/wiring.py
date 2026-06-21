"""Composition root — the only module that resolves config to adapters.

Everything downstream depends on the `Context` returned by `build_context`,
which holds the four adapter instances. Tools never import adapter modules
directly.

This module MUST NOT import any concrete adapter class at module load time.
Lazy imports inside ``coerce_context`` are allowed because that function is
the legacy-API shim and only imported adapters when its db-shaped argument
demands them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from .backends import (
    CounterStore,
    SearchBackend,
    SessionStore,
    StorageBackend,
)
from .config import BackendsConfig, MCMConfig
from .registry import AdapterRegistry


@dataclass
class Context:
    """The wired engine — one instance per adapter concern.

    Tools receive a Context and never see the individual adapter classes.
    Swapping adapters is a config change, not a code change.
    """

    storage: StorageBackend
    counters: CounterStore
    search: SearchBackend
    session: SessionStore


def build_context(
    config: MCMConfig,
    *,
    registry: Optional[AdapterRegistry] = None,
) -> Context:
    """Resolve each adapter from `config.backends` and instantiate.

    Args:
        config: Loaded MCMConfig. `config.backends` selects each adapter.
        registry: Adapter registry. Defaults to a fresh `AdapterRegistry()`
            which uses entry-point discovery only. Tests may pass a
            pre-populated registry with manually-registered fakes.

    Raises:
        AdapterNotFoundError: an adapter name doesn't resolve.
        ContractVersionError: an adapter declares a mismatching version.
    """
    registry = registry or AdapterRegistry()
    backends = config.backends

    storage_cls = registry.resolve(registry.GROUP_STORAGE, backends.storage)
    counters_cls = registry.resolve(registry.GROUP_COUNTERS, backends.counters)
    search_cls = registry.resolve(registry.GROUP_SEARCH, backends.search)
    session_cls = registry.resolve(registry.GROUP_SESSION, backends.session)

    # Cross-adapter dependency injection: some search adapters (notably
    # the v1 OpenSearch one) need a storage handle for their sync model.
    # Adapters opt in explicitly via a `needs_storage = True` class
    # attribute — no introspection of __init__ signatures, so renaming
    # the constructor's `storage` kwarg can't silently break the wiring.
    storage = storage_cls(**backends.storage_options)
    search_options = dict(backends.search_options)
    if getattr(search_cls, "needs_storage", False) and "storage" not in search_options:
        search_options["storage"] = storage

    return Context(
        storage=storage,
        counters=counters_cls(**backends.counters_options),
        search=search_cls(**search_options),
        session=session_cls(**backends.session_options),
    )


def coerce_context(value: Any) -> Context:
    """Accept either a Context or a raw ``KnowledgeDB`` and return a
    Context. The db→Context path is a backward-compat shim for tests
    and any plugin code that still passes the raw connection; new code
    should pass a Context directly.

    For the db-shaped path, builds an embedded SQLite Context sharing
    the given connection so writes are visible across all four adapters.
    """
    if isinstance(value, Context):
        return value
    # Lazy import — keeps backends/__init__.py free of adapter imports.
    from .adapters.sqlite.counters import SqliteCounters
    from .adapters.sqlite.search import SqliteSearch
    from .adapters.sqlite.session import InMemorySession
    from .adapters.sqlite.storage import SqliteStorage
    from .db import KnowledgeDB

    if isinstance(value, KnowledgeDB):
        return Context(
            storage=SqliteStorage(db=value),
            counters=SqliteCounters(db=value),
            search=SqliteSearch(db=value),
            session=InMemorySession(),
        )
    raise TypeError(
        f"coerce_context expected a Context or KnowledgeDB, got {type(value).__name__}"
    )
