"""MCM2-13b: PostgresCounters conformance.

Skipped automatically when Postgres isn't reachable. Demonstrates that
the CounterStore contract is product-agnostic — same Protocol against a
different durable store. PostgresCounters is write-through to entry-row
columns, mirroring SqliteCounters but on the Postgres adapter.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.testing.conformance import CounterConformance

DEFAULT_PG_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_PG_DSN)


def _postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        import psycopg
        with psycopg.connect(TEST_PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason=f"Postgres not reachable at {TEST_PG_DSN}",
)


class TestPostgresCounters(CounterConformance):
    @pytest.fixture
    def _shared(self):
        from mcm_engine.adapters.postgres.counters import PostgresCounters
        from mcm_engine.adapters.postgres.storage import PostgresStorage

        storage = PostgresStorage(dsn=TEST_PG_DSN)
        storage.ensure_schema()
        storage.truncate_all()
        counters = PostgresCounters(dsn=TEST_PG_DSN)
        return storage, counters

    @pytest.fixture
    def storage(self, _shared):
        return _shared[0]

    @pytest.fixture
    def counters(self, _shared):
        return _shared[1]
