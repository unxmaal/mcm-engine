"""MCM2-03: public adapter contract surface.

Tests for the Protocol classes that third-party adapters implement:
StorageBackend, CounterStore, SearchBackend, SessionStore. Plus the
supporting enums and dataclasses that cross the boundary.

The tests assert *shape*, not behavior — actual behavior is exercised
by the conformance suite (mcm_engine.testing.conformance) once
adapters exist.
"""
from __future__ import annotations

import inspect

import pytest


def test_backends_module_imports_cheaply():
    """Importing mcm_engine.backends must not pull in adapter deps.

    NG-8: the engine core has zero adapter-specific dependencies. If
    importing backends triggers `import psycopg` (etc.), the import is
    bleeding adapter coupling into the core. Runs in a fresh subprocess
    so the check is order-independent — other tests in the same session
    may have already imported psycopg/redis for their own purposes.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", (
            "import sys, mcm_engine.backends; "
            "forbidden = {'psycopg', 'psycopg2', 'redis', "
            "'opensearchpy', 'opensearch', 'elasticsearch'}; "
            "present = sorted(forbidden & set(sys.modules)); "
            "print(','.join(present))"
        )],
        capture_output=True, text=True, check=True,
    )
    present = [m for m in proc.stdout.strip().split(",") if m]
    assert not present, f"backends import pulled in adapter deps: {present}"


def test_contract_version_is_positive_int():
    """The contract is at version 1 to start. Bumped on breaking changes
    per docs/contract-versioning.md."""
    from mcm_engine.backends import CONTRACT_VERSION

    assert isinstance(CONTRACT_VERSION, int)
    assert CONTRACT_VERSION >= 1


def test_entity_type_enum_has_four_members():
    """EntityType drives the dynamic-table-name patterns identified in
    the seam inventory. Four values: knowledge, negative, error, rule."""
    from mcm_engine.backends import EntityType

    names = {e.value for e in EntityType}
    assert names == {"knowledge", "negative", "error", "rule"}


def test_capability_enum_exists():
    """Capability enum is the escape hatch for non-breaking method
    additions. May be empty in v1, but must exist for adapters to declare
    against."""
    from mcm_engine.backends import Capability

    # Must be an enum class
    assert hasattr(Capability, "__members__")


# ---- Dataclass shapes ---------------------------------------------------


def test_search_hit_dataclass_shape():
    """SearchHit is the unit of search results across backends.

    Per OQ-7: SearchBackend returns SearchHit dataclasses; a separate
    formatter renders them. Different adapters produce identical-shape
    hits.
    """
    from dataclasses import fields

    from mcm_engine.backends import SearchHit

    field_names = {f.name for f in fields(SearchHit)}
    required = {"entity_type", "entity_id", "score", "is_pinned"}
    assert required.issubset(field_names), (
        f"SearchHit missing required fields: {required - field_names}"
    )


def test_row_dataclasses_exist():
    """The row shapes that cross the StorageBackend boundary."""
    from mcm_engine.backends import (
        ErrorRow,
        KnowledgeRow,
        NegativeRow,
        RelationRow,
        RuleRow,
        SessionRow,
        SnapshotRow,
    )

    # Every row dataclass exposes at least 'id' (the primary key shape).
    for cls in (KnowledgeRow, NegativeRow, ErrorRow, RuleRow,
                SessionRow, SnapshotRow, RelationRow):
        from dataclasses import fields

        assert "id" in {f.name for f in fields(cls)}, f"{cls.__name__} lacks an id field"


# ---- StorageBackend Protocol --------------------------------------------


@pytest.fixture
def StorageBackend():
    from mcm_engine.backends import StorageBackend
    return StorageBackend


def test_storage_backend_is_a_protocol(StorageBackend):
    """StorageBackend uses typing.Protocol so adapters duck-type against it."""
    # Protocols have a _is_protocol attribute (CPython detail).
    assert getattr(StorageBackend, "_is_protocol", False), (
        "StorageBackend must be a typing.Protocol class"
    )


@pytest.mark.parametrize("method_name", [
    # Knowledge CRUD
    "find_knowledge_by_topic_kind",
    "insert_knowledge",
    "update_knowledge",
    "find_similar_knowledge",
    # Negative
    "insert_negative",
    # Errors
    "insert_error",
    # Rules
    "find_rule_by_title",
    "find_rule_by_file_path",
    "insert_rule",
    "update_rule",
    "list_rules_with_file_paths",
    "soft_delete_rule",
    "restore_rule",
    # Relations
    "insert_relation",
    "list_outgoing_relations",
    "list_incoming_relations",
    # Sessions + snapshots
    "insert_session",
    "get_last_session",
    "next_snapshot_seq",
    "insert_snapshot",
    "get_last_snapshot",
    # Cross-entity (driven by dynamic-table sites in the inventory)
    "set_pinned",
    "count_by_type",
    "list_pinned",
    "entry_exists",
    # Schema management
    "ensure_schema",
])
def test_storage_backend_method_present(StorageBackend, method_name):
    """Every StorageBackend method named in the seam inventory's
    'Implications for the contract' section is part of the Protocol."""
    assert hasattr(StorageBackend, method_name), (
        f"StorageBackend.{method_name} is missing from the Protocol. "
        f"See docs/seam-inventory.md."
    )


# ---- CounterStore Protocol ----------------------------------------------


@pytest.fixture
def CounterStore():
    from mcm_engine.backends import CounterStore
    return CounterStore


def test_counter_store_is_a_protocol(CounterStore):
    assert getattr(CounterStore, "_is_protocol", False)


@pytest.mark.parametrize("method_name", [
    "increment",
    "get",
    "top_by",
    "flush",
    "last_flushed_snapshot",
])
def test_counter_store_method_present(CounterStore, method_name):
    assert hasattr(CounterStore, method_name)


# ---- SearchBackend Protocol ---------------------------------------------


@pytest.fixture
def SearchBackend():
    from mcm_engine.backends import SearchBackend
    return SearchBackend


def test_search_backend_is_a_protocol(SearchBackend):
    assert getattr(SearchBackend, "_is_protocol", False)


@pytest.mark.parametrize("method_name", ["search", "reindex", "search_plugin"])
def test_search_backend_method_present(SearchBackend, method_name):
    assert hasattr(SearchBackend, method_name)


# ---- SessionStore Protocol ----------------------------------------------


@pytest.fixture
def SessionStore():
    from mcm_engine.backends import SessionStore
    return SessionStore


def test_session_store_is_a_protocol(SessionStore):
    assert getattr(SessionStore, "_is_protocol", False)


@pytest.mark.parametrize("method_name", [
    "load_state",
    "save_state",
])
def test_session_store_method_present(SessionStore, method_name):
    assert hasattr(SessionStore, method_name)
