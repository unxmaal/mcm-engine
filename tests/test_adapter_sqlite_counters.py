"""Embedded SQLite CounterStore — pinned to the shared CounterConformance."""
from __future__ import annotations

import pytest

from mcm_engine.testing.conformance import CounterConformance


class TestSqliteCounters(CounterConformance):
    @pytest.fixture
    def _shared(self, tmp_path):
        from mcm_engine.adapters.sqlite.counters import SqliteCounters
        from mcm_engine.adapters.sqlite.storage import SqliteStorage

        db_path = str(tmp_path / "counters.db")
        storage = SqliteStorage(db_path=db_path)
        storage.ensure_schema()
        counters = SqliteCounters(db_path=db_path)
        return storage, counters

    @pytest.fixture
    def storage(self, _shared):
        return _shared[0]

    @pytest.fixture
    def counters(self, _shared):
        return _shared[1]
