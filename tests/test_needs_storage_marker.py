"""Cross-adapter dependency injection used to introspect search_cls's
__init__ signature for a parameter literally named ``storage`` (in
``wiring._accepts_kwarg``). That meant renaming the param would
silently break the injection with no error — the OpenSearch adapter
would just construct without a storage handle and its reindex would
explode at first use.

The fix: replace introspection with an explicit ``needs_storage = True``
class attribute on adapters that require it. Wiring checks the attribute,
not the signature — so a rename or refactor of the adapter constructor
can't silently turn the injection off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    SearchBackend,
    SearchHit,
)
from mcm_engine.config import BackendsConfig, MCMConfig, NudgeConfig
from mcm_engine.registry import AdapterRegistry
from mcm_engine.wiring import build_context


# ---------------------------------------------------------------------------
# Fake search adapters used to exercise the marker contract in isolation.
# ---------------------------------------------------------------------------


class _SearchNeedsStorage:
    """Opts into storage injection via the explicit marker."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()
    needs_storage: bool = True

    def __init__(self, *, storage=None):
        self.storage_received = storage

    def search(self, query, *, entity_types=None, scope=None,
               include_archived=False, limit=20, caller=None):
        return []

    def reindex(self) -> None:
        return None


class _SearchDoesNotNeedStorage:
    """No marker — wiring must NOT inject storage."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self):
        self.constructed_without_storage_kwarg = True

    def search(self, query, *, entity_types=None, scope=None,
               include_archived=False, limit=20, caller=None):
        return []

    def reindex(self) -> None:
        return None


class _SearchExplicitlyDeclines:
    """Explicit needs_storage=False — wiring must NOT inject."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()
    needs_storage: bool = False

    def __init__(self):
        self.constructed_without_storage_kwarg = True

    def search(self, query, *, entity_types=None, scope=None,
               include_archived=False, limit=20, caller=None):
        return []

    def reindex(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures: minimal registry/config that wire fake search adapters.
# ---------------------------------------------------------------------------


def _registry_with_search(search_cls: type) -> AdapterRegistry:
    """Build a registry where 'embedded' storage/counters/session stay
    real and only the search slot is replaced with the given fake."""
    reg = AdapterRegistry()
    reg.register(reg.GROUP_SEARCH, "fake", search_cls)
    return reg


def _config_using_fake_search(tmp_path) -> MCMConfig:
    db_path = tmp_path / "wiring-marker.db"
    return MCMConfig(
        project_name="test",
        db_path=db_path,
        rules_path=tmp_path / "rules",
        plugins=[],
        nudges=NudgeConfig(),
        backends=BackendsConfig(
            storage="embedded",
            counters="embedded",
            search="fake",
            session="embedded",
            storage_options={"db_path": str(db_path)},
            counters_options={"db_path": str(db_path)},
            session_options={},
            search_options={},
        ),
    )


# ---------------------------------------------------------------------------
# Real adapters — sanity that they declare the marker correctly.
# ---------------------------------------------------------------------------


def test_opensearch_adapter_declares_needs_storage_true():
    """The reference v1 OpenSearch adapter MUST keep this marker — its
    reindex model can't function without a storage handle."""
    from mcm_engine.adapters.opensearch.search import OpenSearchSearch
    assert getattr(OpenSearchSearch, "needs_storage", False) is True


def test_sqlite_search_does_not_request_storage_injection():
    """SqliteSearch reads from the shared SQLite db directly — it does
    NOT need a separate storage handle, so the marker must be absent
    or False."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    assert getattr(SqliteSearch, "needs_storage", False) is False


def test_postgres_search_does_not_request_storage_injection():
    """PostgresSearch queries the same Postgres rows via tsvector — no
    separate storage handle needed."""
    from mcm_engine.adapters.postgres.search import PostgresSearch
    assert getattr(PostgresSearch, "needs_storage", False) is False


# ---------------------------------------------------------------------------
# Functional: wiring must follow the marker.
# ---------------------------------------------------------------------------


def test_wiring_injects_storage_when_marker_true(tmp_path):
    reg = _registry_with_search(_SearchNeedsStorage)
    cfg = _config_using_fake_search(tmp_path)
    ctx = build_context(cfg, registry=reg)
    assert ctx.search.storage_received is ctx.storage


def test_wiring_does_not_inject_when_marker_absent(tmp_path):
    reg = _registry_with_search(_SearchDoesNotNeedStorage)
    cfg = _config_using_fake_search(tmp_path)
    ctx = build_context(cfg, registry=reg)
    # If wiring had tried to pass storage=, __init__ would have rejected
    # it (no such param). Reaching this point + the flag confirms it didn't.
    assert ctx.search.constructed_without_storage_kwarg is True


def test_wiring_does_not_inject_when_marker_explicit_false(tmp_path):
    reg = _registry_with_search(_SearchExplicitlyDeclines)
    cfg = _config_using_fake_search(tmp_path)
    ctx = build_context(cfg, registry=reg)
    assert ctx.search.constructed_without_storage_kwarg is True


def test_explicit_storage_in_search_options_is_not_overwritten(tmp_path):
    """If the user pre-populates search_options['storage'], wiring must
    respect that and not replace it with the live storage instance.
    Preserves the existing override-via-options escape hatch."""
    reg = _registry_with_search(_SearchNeedsStorage)
    cfg = _config_using_fake_search(tmp_path)
    sentinel = object()
    cfg.backends.search_options["storage"] = sentinel
    ctx = build_context(cfg, registry=reg)
    assert ctx.search.storage_received is sentinel
