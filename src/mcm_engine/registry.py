"""Adapter registry — resolves config-string names to adapter classes.

Two discovery paths:

1. `importlib.metadata.entry_points()` — first-party adapters declare entry
   points in their `pyproject.toml`:

       [project.entry-points."mcm_engine.adapters.storage"]
       postgres = "mcm_engine.adapters.postgres:PostgresStorage"

2. `"module:Class"` escape hatch — for in-repo dev work, third-party
   adapters not yet packaged, or testing.

The registry enforces `CONTRACT_VERSION` at resolve time: an adapter
declaring a different version raises `ContractVersionError` before any
traffic flows. See docs/contract-versioning.md.
"""
from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any

from .backends import CONTRACT_VERSION


class AdapterRegistryError(Exception):
    """Base class for registry errors."""


class AdapterNotFoundError(AdapterRegistryError):
    """The requested adapter name does not resolve to any class."""


class ContractVersionError(AdapterRegistryError):
    """An adapter declares an incompatible CONTRACT_VERSION."""


class AdapterRegistry:
    """Resolve config-string names to adapter classes.

    A single instance is created by the composition root; everything
    downstream depends on the resolved classes, not the registry.
    """

    GROUP_STORAGE = "mcm_engine.adapters.storage"
    GROUP_COUNTERS = "mcm_engine.adapters.counters"
    GROUP_SEARCH = "mcm_engine.adapters.search"
    GROUP_SESSION = "mcm_engine.adapters.session"

    _ALL_GROUPS = (GROUP_STORAGE, GROUP_COUNTERS, GROUP_SEARCH, GROUP_SESSION)

    def __init__(self, *, autoload_embedded: bool = True) -> None:
        # Manual registrations per group: {group: {name: cls}}
        self._manual: dict[str, dict[str, type]] = {g: {} for g in self._ALL_GROUPS}
        if autoload_embedded:
            self._register_embedded()

    def _register_embedded(self) -> None:
        """Bootstrap the embedded SQLite adapters under the name 'embedded'.

        Available without entry-point discovery so a fresh registry works
        for the in-process default deployment. Third-party adapters are
        still discovered via entry points (see resolve)."""
        # Lazy import to avoid a circular import at module load time.
        from .adapters.sqlite import (
            InMemorySession,
            SqliteCounters,
            SqliteSearch,
            SqliteStorage,
        )
        self.register(self.GROUP_STORAGE,  "embedded", SqliteStorage)
        self.register(self.GROUP_COUNTERS, "embedded", SqliteCounters)
        self.register(self.GROUP_SEARCH,   "embedded", SqliteSearch)
        self.register(self.GROUP_SESSION,  "embedded", InMemorySession)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, group: str, name: str, cls: type) -> None:
        """Register an adapter class under `name` in `group`.

        Used for in-repo dev work and tests. Production deployments
        typically rely on entry-point discovery instead.
        """
        if group not in self._ALL_GROUPS:
            raise AdapterRegistryError(
                f"unknown adapter group: {group!r}. "
                f"Valid groups: {', '.join(self._ALL_GROUPS)}"
            )
        self._check_contract_version(cls, label=name)
        self._manual[group][name] = cls

    def resolve(self, group: str, name: str) -> type:
        """Resolve `name` within `group` to an adapter class.

        Lookup order:
          1. If `name` contains ':', treat as `module:Class` and import.
          2. Manual registry for the group.
          3. Entry points declared for the group.

        Raises:
            AdapterNotFoundError: name resolves to nothing.
            ContractVersionError: class exists but declares a mismatching version.
            ImportError / AttributeError: module:Class lookup failed.
        """
        if group not in self._ALL_GROUPS:
            raise AdapterRegistryError(
                f"unknown adapter group: {group!r}. "
                f"Valid groups: {', '.join(self._ALL_GROUPS)}"
            )

        if ":" in name:
            cls = self._import_module_class(name)
            self._check_contract_version(cls, label=name)
            return cls

        # Manual registry
        manual = self._manual[group].get(name)
        if manual is not None:
            # Manual entries were version-checked at register() time, but
            # re-check defensively in case the class has been mutated.
            self._check_contract_version(manual, label=name)
            return manual

        # Entry points
        for ep in importlib.metadata.entry_points(group=group):
            if ep.name == name:
                cls = ep.load()
                self._check_contract_version(cls, label=name)
                return cls

        known = self._known_names(group)
        raise AdapterNotFoundError(
            f"no adapter named {name!r} in group {group!r}. "
            f"Known names: {', '.join(known) if known else '(none registered)'}. "
            f"Use 'module:Class' syntax to load an unregistered adapter."
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _known_names(self, group: str) -> list[str]:
        names: set[str] = set(self._manual[group])
        for ep in importlib.metadata.entry_points(group=group):
            names.add(ep.name)
        return sorted(names)

    @staticmethod
    def _import_module_class(spec: str) -> type:
        module_name, _, class_name = spec.partition(":")
        if not module_name or not class_name:
            raise ValueError(
                f"invalid module:Class spec {spec!r} — expected 'pkg.module:ClassName'"
            )
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise ImportError(
                f"could not import module {module_name!r} for adapter spec {spec!r}: {e}"
            ) from e
        try:
            return getattr(module, class_name)
        except AttributeError as e:
            raise AttributeError(
                f"module {module_name!r} has no class {class_name!r} (adapter spec {spec!r})"
            ) from e

    @staticmethod
    def _check_contract_version(cls: Any, *, label: str) -> None:
        declared = getattr(cls, "CONTRACT_VERSION", None)
        if declared is None:
            raise ContractVersionError(
                f"adapter {label!r} (class {cls.__name__!r}) does not declare "
                f"CONTRACT_VERSION. The engine requires CONTRACT_VERSION = {CONTRACT_VERSION}."
            )
        if declared != CONTRACT_VERSION:
            raise ContractVersionError(
                f"adapter {label!r} (class {cls.__name__!r}) declares "
                f"CONTRACT_VERSION = {declared}, but engine requires {CONTRACT_VERSION}. "
                f"Either upgrade the adapter, or pin an engine version that matches."
            )
