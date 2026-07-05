"""Store-identity + authoritative-store binding (stray-db branch).

Invariant: once you DEFINE the database (config.authoritative_store), no
entrypoint may silently use a different one. Every storage backend self-reports
a StorageIdentity; verify_store() fails closed on a mismatch.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.authority import WrongStoreError, verify_store
from mcm_engine.backends import StorageIdentity
from mcm_engine.config import load_config
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core


def _sqlite(path) -> SqliteStorage:
    db = KnowledgeDB(path)
    migrate_core(db)
    return SqliteStorage(db=db)


# --- StorageIdentity -------------------------------------------------------


def test_sqlite_identity_is_kind_plus_absolute_path(tmp_path):
    p = tmp_path / "sub" / "k.db"
    ident = _sqlite(p).identity
    assert ident.kind == "sqlite"
    assert ident.location == str(p.resolve())
    assert str(ident) == f"sqlite:{p.resolve()}"


def test_sqlite_memory_identity_is_stable(tmp_path):
    ident = SqliteStorage(db_path=":memory:").identity
    assert str(ident) == "sqlite::memory:"


def test_identity_is_a_frozen_value():
    a = StorageIdentity("sqlite", "/x/y.db")
    b = StorageIdentity("sqlite", "/x/y.db")
    assert a == b
    with pytest.raises(Exception):
        a.kind = "postgres"  # frozen


# --- verify_store (the guard) ----------------------------------------------


def test_verify_store_passes_when_unpinned(tmp_path):
    # Empty expected == not pinned == no enforcement (back-compat).
    verify_store(_sqlite(tmp_path / "k.db").identity, "")


def test_verify_store_passes_on_exact_match(tmp_path):
    ident = _sqlite(tmp_path / "k.db").identity
    verify_store(ident, str(ident))  # must not raise


def test_verify_store_fails_closed_on_mismatch(tmp_path):
    ident = _sqlite(tmp_path / "k.db").identity
    with pytest.raises(WrongStoreError) as e:
        verify_store(ident, "sqlite:/some/other/authoritative.db")
    # Error must name both the expected and the actual store.
    assert "/some/other/authoritative.db" in str(e.value)
    assert ident.location in str(e.value)


# --- config field ----------------------------------------------------------


def test_authoritative_store_config_field(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text(
        yaml.dump({"project_name": "x",
                   "authoritative_store": "sqlite:/data/knowledge.db"}),
        encoding="utf-8",
    )
    cfg = load_config(project_root=tmp_path)
    assert cfg.authoritative_store == "sqlite:/data/knowledge.db"


def test_authoritative_store_defaults_empty(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text(
        yaml.dump({"project_name": "x"}), encoding="utf-8")
    assert load_config(project_root=tmp_path).authoritative_store == ""
