"""Composition root — the only module that resolves config to adapters.

Everything downstream depends on the `Context` returned by `build_context`,
which holds the four adapter instances. Tools never import adapter modules
directly.

This module MUST NOT import any concrete adapter class. It only imports
the registry and the Protocol classes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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

    return Context(
        storage=storage_cls(**backends.storage_options),
        counters=counters_cls(**backends.counters_options),
        search=search_cls(**backends.search_options),
        session=session_cls(**backends.session_options),
    )
