"""MCM2-04: composition root.

`build_context(config)` reads config, resolves each adapter via the
registry, instantiates them, and returns a Context dataclass that holds
the four adapter instances. The composition root is the ONLY module that
touches concrete adapter classes — everything downstream consumes the
Context.
"""
from __future__ import annotations

import textwrap

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    CounterStore,
    EntityType,
    SearchBackend,
    SessionStore,
    StorageBackend,
)
from mcm_engine.config import MCMConfig, load_config
from mcm_engine.registry import AdapterRegistry


# ---- Fake adapters for wiring tests -------------------------------------


class FakeStorage:
    CONTRACT_VERSION = CONTRACT_VERSION
    capabilities: set = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeCounters:
    CONTRACT_VERSION = CONTRACT_VERSION
    capabilities: set = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSearch:
    CONTRACT_VERSION = CONTRACT_VERSION
    capabilities: set = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSession:
    CONTRACT_VERSION = CONTRACT_VERSION
    capabilities: set = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def registry_with_fakes():
    r = AdapterRegistry()
    r.register(r.GROUP_STORAGE, "fake", FakeStorage)
    r.register(r.GROUP_COUNTERS, "fake", FakeCounters)
    r.register(r.GROUP_SEARCH, "fake", FakeSearch)
    r.register(r.GROUP_SESSION, "fake", FakeSession)
    return r


# ---- Context shape -------------------------------------------------------


def test_context_dataclass_has_four_adapter_slots():
    from dataclasses import fields

    from mcm_engine.wiring import Context

    names = {f.name for f in fields(Context)}
    assert names == {"storage", "counters", "search", "session"}


# ---- build_context behavior ---------------------------------------------


def test_build_context_returns_instances_of_resolved_classes(registry_with_fakes):
    from mcm_engine.wiring import build_context

    config = MCMConfig(project_name="test")
    config.backends.storage = "fake"
    config.backends.counters = "fake"
    config.backends.search = "fake"
    config.backends.session = "fake"

    ctx = build_context(config, registry=registry_with_fakes)
    assert isinstance(ctx.storage, FakeStorage)
    assert isinstance(ctx.counters, FakeCounters)
    assert isinstance(ctx.search, FakeSearch)
    assert isinstance(ctx.session, FakeSession)


def test_build_context_default_adapter_is_embedded():
    """A bare MCMConfig with no backends declared selects 'embedded' for
    every concern. The composition root will fail if 'embedded' is not
    registered — which it isn't yet (MCM2-02 ships it), so this test
    proves the *intent*, not yet a usable boot path."""
    config = MCMConfig(project_name="test")
    assert config.backends.storage == "embedded"
    assert config.backends.counters == "embedded"
    assert config.backends.search == "embedded"
    assert config.backends.session == "embedded"


def test_build_context_passes_adapter_kwargs(registry_with_fakes):
    """Adapter-specific config (e.g., a Postgres DSN) is passed to the
    adapter's __init__ as kwargs."""
    from mcm_engine.wiring import build_context

    config = MCMConfig(project_name="test")
    config.backends.storage = "fake"
    config.backends.storage_options = {"dsn": "postgres://localhost/x", "pool": 5}
    config.backends.counters = "fake"
    config.backends.search = "fake"
    config.backends.session = "fake"

    ctx = build_context(config, registry=registry_with_fakes)
    assert ctx.storage.kwargs == {"dsn": "postgres://localhost/x", "pool": 5}


def test_build_context_propagates_unknown_adapter_error():
    """When the configured adapter name is unknown, the error from the
    registry propagates with enough context for the user to fix the
    config."""
    from mcm_engine.registry import AdapterNotFoundError
    from mcm_engine.wiring import build_context

    config = MCMConfig(project_name="test")
    config.backends.storage = "no_such_adapter"

    with pytest.raises(AdapterNotFoundError) as exc:
        build_context(config, registry=AdapterRegistry())
    assert "no_such_adapter" in str(exc.value)


# ---- BackendsConfig YAML parsing ----------------------------------------


def test_backends_config_parsed_from_yaml(tmp_path):
    """A `backends:` block in YAML is parsed into MCMConfig.backends.

    Verifies the MCM2-06 strict-keys hygiene doesn't reject the new
    nested block.
    """
    cfg_path = tmp_path / "mcm-engine.yaml"
    cfg_path.write_text(textwrap.dedent("""
        project_name: test
        backends:
          storage: postgres
          counters: redis
          search: opensearch
          session: embedded
          storage_options:
            dsn: postgres://example
    """))
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.backends.storage == "postgres"
    assert cfg.backends.counters == "redis"
    assert cfg.backends.search == "opensearch"
    assert cfg.backends.session == "embedded"
    assert cfg.backends.storage_options == {"dsn": "postgres://example"}


def test_unknown_backend_key_raises(tmp_path):
    """Unknown keys inside the backends block fail closed, same as
    nudges and top-level (MCM2-06)."""
    cfg_path = tmp_path / "mcm-engine.yaml"
    cfg_path.write_text(textwrap.dedent("""
        project_name: test
        backends:
          storige: postgres   # typo
    """))
    with pytest.raises(ValueError, match=r"unknown.*backends.*storige"):
        load_config(config_path=cfg_path, project_root=tmp_path)
