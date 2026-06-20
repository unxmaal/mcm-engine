"""MCM2-02 + MCM2-09: embedded SQLite StorageBackend conformance.

This file is a thin specialization of the shared StorageConformance
suite — the SQLite adapter is the embedded reference, so it MUST pass
every conformance test unchanged. Postgres and any third-party adapter
subclass the same suite from a separate file.
"""
from __future__ import annotations

import pytest

from mcm_engine.testing.conformance import StorageConformance


class TestSqliteStorage(StorageConformance):
    @pytest.fixture
    def storage(self, tmp_path):
        from mcm_engine.adapters.sqlite.storage import SqliteStorage

        s = SqliteStorage(db_path=str(tmp_path / "store.db"))
        s.ensure_schema()
        return s
