"""MCM2-17: capability flags + honest degradation.

Adapters declare what they support via the ``capabilities`` set.
Callers probe ``Capability.X in adapter.capabilities`` before calling
capability-gated methods. The Capability enum is the escape hatch
described in docs/contract-versioning.md for adding methods without
bumping CONTRACT_VERSION.

These tests assert the *shape*: that each adapter's capabilities set
matches the document of what it can do. Adapters lying about their
capabilities is a real bug — these tests catch it for the reference
adapters.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.backends import Capability


DEFAULT_PG_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_PG_DSN)
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/0"
TEST_REDIS_URL = os.environ.get("MCM_TEST_REDIS_URL", DEFAULT_REDIS_URL)
DEFAULT_OS_URL = "http://127.0.0.1:59200"
TEST_OS_URL = os.environ.get("MCM_TEST_OPENSEARCH_URL", DEFAULT_OS_URL)


def _pg_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(TEST_PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis as _redis
        client = _redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


def _os_available() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"{TEST_OS_URL}/_cluster/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ---- Capability enum surface -----------------------------------------------


def test_capability_enum_has_named_flags():
    """The Capability enum is no longer the empty placeholder it was in
    Phase 0 — it carries real flags adapters can declare against."""
    assert "VECTOR_SEARCH" in Capability.__members__
    assert "EMBEDDING_PROVIDER" in Capability.__members__
    assert "DURABLE_SESSION" in Capability.__members__


def test_capability_values_are_lowercase_strings():
    """StrEnum values are the canonical config-string form."""
    for cap in Capability:
        assert cap.value == cap.name.lower()


# ---- v1 adapters declare empty capability sets -----------------------------


def test_sqlite_storage_declares_no_capabilities(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    s = SqliteStorage(db_path=str(tmp_path / "x.db"))
    assert s.capabilities == set()


def test_sqlite_search_declares_no_capabilities(tmp_path):
    """SqliteSearch is lexical-only — no VECTOR_SEARCH."""
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    s = SqliteSearch(db_path=str(tmp_path / "x.db"))
    assert Capability.VECTOR_SEARCH not in s.capabilities


def test_inmemory_session_lacks_durable_session_capability():
    """The embedded SessionStore is in-process only; restarting loses
    state. It MUST NOT claim DURABLE_SESSION."""
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    assert Capability.DURABLE_SESSION not in s.capabilities


@pytest.mark.skipif(not _pg_available(), reason="postgres not available")
def test_postgres_search_lacks_vector_search():
    """PostgresSearch is lexical (tsvector). pgvector would be a
    separate adapter or capability opt-in. The reference impl declares
    no VECTOR_SEARCH."""
    from mcm_engine.adapters.postgres.search import PostgresSearch
    s = PostgresSearch(dsn=TEST_PG_DSN)
    assert Capability.VECTOR_SEARCH not in s.capabilities


@pytest.mark.skipif(not _redis_available(), reason="redis not available")
def test_redis_counters_declares_no_capabilities():
    from mcm_engine.adapters.redis.counters import RedisCounters
    c = RedisCounters(url=TEST_REDIS_URL, namespace="mcm:test:caps:")
    assert c.capabilities == set()


@pytest.mark.skipif(not _os_available(), reason="opensearch not available")
def test_opensearch_lacks_vector_search_in_v1(tmp_path):
    """OpenSearch *can* do vector search (knn) but our v1 adapter
    doesn't expose it. Declaring it would be lying — the search()
    method doesn't accept vectors."""
    from mcm_engine.adapters.opensearch.search import OpenSearchSearch
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    storage = SqliteStorage(db_path=str(tmp_path / "x.db"))
    storage.ensure_schema()
    s = OpenSearchSearch(
        url=TEST_OS_URL, index_prefix="mcm-test-caps-", storage=storage,
    )
    assert Capability.VECTOR_SEARCH not in s.capabilities
