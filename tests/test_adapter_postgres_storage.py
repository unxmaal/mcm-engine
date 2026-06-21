"""MCM2-08 + MCM2-09b: Postgres StorageBackend conformance.

This file is the parametrized counterpart to test_adapter_sqlite_storage.py —
it runs the exact same StorageConformance suite against a Postgres
adapter, which is what proves the adapter contract is real.

The tests are SKIPPED automatically when:
  - psycopg isn't installed (the `postgres` extra), OR
  - no Postgres reachable at MCM_TEST_POSTGRES_DSN (default points at the
    docker-compose service on 127.0.0.1:55432).

To run locally:
    docker compose -f tests/docker-compose.yml up -d postgres
    uv pip install -e '.[postgres]'
    uv run python -m pytest tests/test_adapter_postgres_storage.py
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.testing.conformance import StorageConformance

DEFAULT_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_DSN)


def _postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        import psycopg
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason=(
        f"Postgres not reachable at {TEST_DSN} "
        "(start with `docker compose -f tests/docker-compose.yml up -d postgres` "
        "and `uv pip install -e '.[postgres]'`)."
    ),
)


class TestPostgresStorage(StorageConformance):
    @pytest.fixture
    def storage(self):
        # Deferred imports so collection doesn't fail when the adapter
        # module is being built out and psycopg may be absent.
        from mcm_engine.adapters.postgres.storage import PostgresStorage

        store = PostgresStorage(dsn=TEST_DSN)
        store.ensure_schema()
        # Each test gets a clean slate. The conformance asserts counts.
        store.truncate_all()
        return store
