"""MCM2-04b: adapter registry.

The registry is the one piece of code outside the composition root that
knows how to turn a config-string name into an adapter class.

Two discovery paths:
1. `importlib.metadata.entry_points()` — first-party adapters declare
   entry points; third-party adapters install themselves.
2. `"module:Class"` escape hatch — for in-repo dev work or unregistered
   adapters.

The registry also enforces CONTRACT_VERSION at resolve time — adapters
with a mismatched version raise a clear error before any traffic flows.
"""
from __future__ import annotations

import sys
import textwrap
import types

import pytest

from mcm_engine.backends import (
    CONTRACT_VERSION,
    Capability,
    CounterStore,
    SearchBackend,
    SessionStore,
    StorageBackend,
)


@pytest.fixture
def registry():
    from mcm_engine.registry import AdapterRegistry
    return AdapterRegistry()


# ---- Discovery groups ----------------------------------------------------


def test_group_constants_exist():
    from mcm_engine.registry import AdapterRegistry

    assert AdapterRegistry.GROUP_STORAGE == "mcm_engine.adapters.storage"
    assert AdapterRegistry.GROUP_COUNTERS == "mcm_engine.adapters.counters"
    assert AdapterRegistry.GROUP_SEARCH == "mcm_engine.adapters.search"
    assert AdapterRegistry.GROUP_SESSION == "mcm_engine.adapters.session"


# ---- module:Class escape hatch ------------------------------------------


def _make_module(name: str, body: str) -> None:
    """Create a synthetic importable module for test purposes."""
    mod = types.ModuleType(name)
    exec(textwrap.dedent(body), mod.__dict__)
    sys.modules[name] = mod


def test_resolve_via_module_class_syntax(registry):
    """`module:Class` lookup imports the module and grabs the class."""
    _make_module("_test_adapter_a", f"""
        CONTRACT_VERSION = {CONTRACT_VERSION}

        class GoodStorage:
            CONTRACT_VERSION = {CONTRACT_VERSION}
            capabilities = set()
    """)
    cls = registry.resolve(registry.GROUP_STORAGE, "_test_adapter_a:GoodStorage")
    assert cls.__name__ == "GoodStorage"


def test_module_class_missing_module_raises_clear_error(registry):
    """A typo in the module half of `module:Class` produces an error
    that names the missing module."""
    with pytest.raises(ImportError) as exc:
        registry.resolve(registry.GROUP_STORAGE, "_no_such_module_xyz:SomeClass")
    assert "_no_such_module_xyz" in str(exc.value)


def test_module_class_missing_class_raises_clear_error(registry):
    """If the module loads but the class is absent, the error names
    the missing class."""
    _make_module("_test_adapter_b", "VALUE = 1\n")
    with pytest.raises(AttributeError) as exc:
        registry.resolve(registry.GROUP_STORAGE, "_test_adapter_b:Missing")
    assert "Missing" in str(exc.value)


# ---- CONTRACT_VERSION check ---------------------------------------------


def test_contract_version_mismatch_raises(registry):
    """An adapter declaring a different CONTRACT_VERSION is rejected at
    resolve time with an error naming both versions."""
    _make_module("_test_adapter_c", f"""
        class WrongVersion:
            CONTRACT_VERSION = {CONTRACT_VERSION + 99}
            capabilities = set()
    """)
    from mcm_engine.registry import ContractVersionError
    with pytest.raises(ContractVersionError) as exc:
        registry.resolve(registry.GROUP_STORAGE, "_test_adapter_c:WrongVersion")
    msg = str(exc.value)
    assert str(CONTRACT_VERSION) in msg
    assert str(CONTRACT_VERSION + 99) in msg
    assert "WrongVersion" in msg


def test_missing_contract_version_raises(registry):
    """Adapters MUST declare CONTRACT_VERSION explicitly."""
    _make_module("_test_adapter_d", """
        class NoVersion:
            capabilities = set()
    """)
    from mcm_engine.registry import ContractVersionError
    with pytest.raises(ContractVersionError):
        registry.resolve("mcm_engine.adapters.storage", "_test_adapter_d:NoVersion")


# ---- Manual registration (for in-repo dev) ------------------------------


def test_register_and_resolve_by_name(registry):
    """An in-repo adapter can be registered manually and resolved by name."""
    _make_module("_test_adapter_e", f"""
        class LocalStorage:
            CONTRACT_VERSION = {CONTRACT_VERSION}
            capabilities = set()
    """)
    import _test_adapter_e
    registry.register(registry.GROUP_STORAGE, "local", _test_adapter_e.LocalStorage)
    cls = registry.resolve(registry.GROUP_STORAGE, "local")
    assert cls.__name__ == "LocalStorage"


# ---- Unknown name --------------------------------------------------------


def test_unknown_bare_name_raises_clear_error(registry):
    """A bare name that doesn't match a registered or entry-point adapter
    raises with the list of known names."""
    from mcm_engine.registry import AdapterNotFoundError
    with pytest.raises(AdapterNotFoundError) as exc:
        registry.resolve(registry.GROUP_STORAGE, "definitely_not_an_adapter")
    msg = str(exc.value)
    assert "definitely_not_an_adapter" in msg


# ---- Group isolation -----------------------------------------------------


def test_groups_are_isolated(registry):
    """An adapter registered in one group is not visible from another."""
    _make_module("_test_adapter_f", f"""
        class S:
            CONTRACT_VERSION = {CONTRACT_VERSION}
            capabilities = set()
    """)
    import _test_adapter_f
    registry.register(registry.GROUP_STORAGE, "iso", _test_adapter_f.S)
    # Same name in a different group does not resolve.
    from mcm_engine.registry import AdapterNotFoundError
    with pytest.raises(AdapterNotFoundError):
        registry.resolve(registry.GROUP_COUNTERS, "iso")
