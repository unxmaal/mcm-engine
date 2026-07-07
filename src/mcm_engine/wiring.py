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


def _embedded_db_path(name: str, options: dict) -> Optional[str]:
    """The resolved absolute file an embedded-SQLite axis points at, or None
    when the axis isn't embedded / has no file / is in-memory. In-memory DBs are
    per-connection and must never be shared (each ``:memory:`` is a distinct
    database), so they're excluded."""
    if name != "embedded":
        return None
    p = options.get("db_path")
    if not p or str(p) == ":memory:":
        return None
    from pathlib import Path

    return str(Path(str(p)).resolve())


def build_context(
    config: MCMConfig,
    *,
    registry: Optional[AdapterRegistry] = None,
    shared_db: Any = None,
) -> Context:
    """Resolve each adapter from `config.backends` and instantiate.

    Args:
        config: Loaded MCMConfig. `config.backends` selects each adapter.
        registry: Adapter registry. Defaults to a fresh `AdapterRegistry()`
            which uses entry-point discovery only. Tests may pass a
            pre-populated registry with manually-registered fakes.
        shared_db: Optional pre-opened ``KnowledgeDB`` (the daemon passes the
            connection it also hands to plugins). Embedded-SQLite adapters that
            resolve to the same file reuse it instead of opening their own.

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

    # Shared-connection guard (issue #79): embedded-SQLite adapters that point
    # at the SAME file must share ONE KnowledgeDB connection. Separate
    # connections to a single file serialize their writes via busy_timeout and
    # SELF-CONTEND — a post-search counter bump (a write on the counters
    # connection) blocks up to 5s waiting on the sibling storage/search
    # connections, so a single `search` stacks those waits into ~20s. One
    # connection = one writer = no cross-adapter lock waits. Keyed by resolved
    # path so distinct files still get distinct connections.
    _dbs_by_path: dict[str, Any] = {}
    if shared_db is not None:
        from pathlib import Path

        _dbs_by_path[str(Path(str(shared_db.db_path)).resolve())] = shared_db

    def _shared_for(name: str, options: dict) -> Any:
        path = _embedded_db_path(name, options)
        if path is None:
            return None
        if path not in _dbs_by_path:
            from .db import KnowledgeDB

            _dbs_by_path[path] = KnowledgeDB(path)
        return _dbs_by_path[path]

    def _instantiate(cls, name: str, options: dict, extra: Optional[dict] = None):
        opts = dict(options)
        if extra:
            opts.update(extra)
        shared = _shared_for(name, options)
        if shared is not None:
            # `db=` wins over `db_path` in the embedded ctors; drop the now-
            # redundant path so the two can't disagree.
            opts.pop("db_path", None)
            return cls(db=shared, **opts)
        return cls(**opts)

    # Cross-adapter dependency injection: some search adapters (notably
    # the v1 OpenSearch one) need a storage handle for their sync model.
    # Adapters opt in explicitly via a `needs_storage = True` class
    # attribute — no introspection of __init__ signatures, so renaming
    # the constructor's `storage` kwarg can't silently break the wiring.
    storage = _instantiate(storage_cls, backends.storage, backends.storage_options)
    search_extra: dict = {}
    if getattr(search_cls, "needs_storage", False) and "storage" not in backends.search_options:
        search_extra["storage"] = storage

    return Context(
        storage=storage,
        counters=_instantiate(counters_cls, backends.counters, backends.counters_options),
        search=_instantiate(search_cls, backends.search, backends.search_options, search_extra),
        session=session_cls(**backends.session_options),
    )


def build_verified_context(
    config: MCMConfig,
    *,
    registry: Optional[AdapterRegistry] = None,
    shared_db: Any = None,
) -> Context:
    """``build_context`` + the authoritative-store guard, in one call.

    This is the choke point every composition root should use so the check can't
    be forgotten: if ``config.authoritative_store`` is pinned, the resolved
    storage's ``StorageIdentity`` must match it (fail closed via
    ``authority.verify_store``). Unpinned config is unchanged behavior.

    ``shared_db`` is forwarded to ``build_context`` so the daemon's plugin
    connection and the embedded adapters share one SQLite connection (issue #79).
    """
    from .authority import WrongStoreError, verify_store

    ctx = build_context(config, registry=registry, shared_db=shared_db)
    expected = getattr(config, "authoritative_store", "") or ""
    if expected:
        identity = getattr(ctx.storage, "identity", None)
        if identity is None:
            raise WrongStoreError(
                f"authoritative_store is pinned to {expected}, but the "
                f"{type(ctx.storage).__name__} backend reports no StorageIdentity "
                f"to verify against."
            )
        verify_store(identity, expected)
    return ctx


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
