"""MCM2-19: orthogonal config switches per scaling axis.

The four adapter axes (storage / counters / search / session) MUST be
independently configurable. This test mixes adapters across axes to
prove the wiring layer doesn't quietly couple them — e.g.
``storage=postgres + counters=redis + search=opensearch +
session=embedded`` is a valid, runnable configuration.

Cross-axis tests are gated on the external services they touch; the
named-resolution tests run unconditionally and prove that the registry
recognizes every reference adapter name out of pyproject.toml's entry
points.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.config import BackendsConfig, MCMConfig
from mcm_engine.registry import AdapterRegistry

DEFAULT_PG_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_PG_DSN)
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/0"
TEST_REDIS_URL = os.environ.get("MCM_TEST_REDIS_URL", DEFAULT_REDIS_URL)
DEFAULT_OS_URL = "http://127.0.0.1:59200"
TEST_OS_URL = os.environ.get("MCM_TEST_OPENSEARCH_URL", DEFAULT_OS_URL)


def _all_services_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(TEST_PG_DSN, connect_timeout=2):
            pass
        import redis as _redis
        _redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2).ping()
        import urllib.request
        with urllib.request.urlopen(f"{TEST_OS_URL}/_cluster/health", timeout=2) as r:
            if r.status != 200:
                return False
        return True
    except Exception:
        return False


# ---- Named resolution via entry points (no service required) ---------------


def test_registry_resolves_postgres_storage_by_name():
    """The 'postgres' name registered in pyproject.toml's
    [project.entry-points."mcm_engine.adapters.storage"] resolves
    to PostgresStorage. No connection needed — just the lookup."""
    reg = AdapterRegistry()
    cls = reg.resolve(reg.GROUP_STORAGE, "postgres")
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    assert cls is PostgresStorage


def test_registry_resolves_redis_counters_by_name():
    reg = AdapterRegistry()
    cls = reg.resolve(reg.GROUP_COUNTERS, "redis")
    from mcm_engine.adapters.redis.counters import RedisCounters
    assert cls is RedisCounters


def test_registry_resolves_postgres_counters_by_name():
    reg = AdapterRegistry()
    cls = reg.resolve(reg.GROUP_COUNTERS, "postgres")
    from mcm_engine.adapters.postgres.counters import PostgresCounters
    assert cls is PostgresCounters


def test_registry_resolves_postgres_search_by_name():
    reg = AdapterRegistry()
    cls = reg.resolve(reg.GROUP_SEARCH, "postgres")
    from mcm_engine.adapters.postgres.search import PostgresSearch
    assert cls is PostgresSearch


def test_registry_resolves_opensearch_by_name():
    reg = AdapterRegistry()
    cls = reg.resolve(reg.GROUP_SEARCH, "opensearch")
    from mcm_engine.adapters.opensearch.search import OpenSearchSearch
    assert cls is OpenSearchSearch


def test_each_axis_lists_its_named_adapters():
    """Sanity probe: each group's _known_names returns every reference
    adapter name that should be discoverable."""
    reg = AdapterRegistry()

    storage_names = set(reg._known_names(reg.GROUP_STORAGE))
    assert {"embedded", "postgres"} <= storage_names

    counters_names = set(reg._known_names(reg.GROUP_COUNTERS))
    assert {"embedded", "postgres", "redis"} <= counters_names

    search_names = set(reg._known_names(reg.GROUP_SEARCH))
    assert {"embedded", "postgres", "opensearch"} <= search_names

    session_names = set(reg._known_names(reg.GROUP_SESSION))
    assert {"embedded"} <= session_names


# ---- Mixed-axis wiring with all three services ----------------------------


@pytest.mark.skipif(
    not _all_services_available(),
    reason="requires all three test containers (postgres + redis + opensearch)",
)
def test_orthogonal_mix_postgres_storage_redis_counters_opensearch_search(tmp_path):
    """A non-default mix runs end-to-end: insert a knowledge row via
    PostgresStorage, increment its hit_count via RedisCounters, search
    for it via OpenSearchSearch."""
    from mcm_engine.adapters.opensearch.search import OpenSearchSearch
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    from mcm_engine.adapters.redis.counters import RedisCounters
    from mcm_engine.adapters.sqlite.session import InMemorySession
    from mcm_engine.backends import EntityType, KnowledgeRow
    from mcm_engine.wiring import build_context

    cfg = MCMConfig(
        project_name="mix-test",
        db_path=str(tmp_path / "should-not-be-used.db"),
        backends=BackendsConfig(
            storage="postgres",
            counters="redis",
            search="opensearch",
            session="embedded",
            storage_options={"dsn": TEST_PG_DSN},
            counters_options={
                "url": TEST_REDIS_URL,
                "namespace": f"mcm:test:orthogonal:{tmp_path.name}:",
            },
            search_options={
                "url": TEST_OS_URL,
                "index_prefix": f"mcm-test-orthogonal-{tmp_path.name}-",
                # OpenSearchSearch needs the storage handle; the wiring
                # layer can't compose that automatically yet, so pass
                # None and the test sets it post-wire.
                "storage": None,
            },
            session_options={},
        ),
    )

    # Pre-wire prep: PostgresStorage's truncate + Redis namespace reset.
    pg_storage = PostgresStorage(dsn=TEST_PG_DSN)
    pg_storage.ensure_schema()
    pg_storage.truncate_all()
    redis_c = RedisCounters(
        url=TEST_REDIS_URL,
        namespace=f"mcm:test:orthogonal:{tmp_path.name}:",
    )
    redis_c.reset_namespace()

    # OpenSearchSearch wants `storage` at construction. Pre-wire so we
    # can pass it in. (build_context can't inject this — it's a
    # cross-adapter dependency.)
    cfg.backends.search_options["storage"] = pg_storage

    ctx = build_context(cfg)
    assert isinstance(ctx.storage, PostgresStorage)
    assert isinstance(ctx.counters, RedisCounters)
    assert isinstance(ctx.search, OpenSearchSearch)
    assert isinstance(ctx.session, InMemorySession)

    # End-to-end exercise.
    kid = ctx.storage.insert_knowledge(KnowledgeRow(
        id=0, topic="orthogonal mix proof", summary="sentinel", kind="finding",
    ))
    ctx.counters.increment(EntityType.KNOWLEDGE, kid, "hit_count", by=2)

    snap = ctx.counters.get(EntityType.KNOWLEDGE, kid)
    assert snap["hit_count"] == 2

    hits = ctx.search.search("orthogonal", entity_types={EntityType.KNOWLEDGE})
    assert any(h.entity_id == kid for h in hits), (
        f"OpenSearch failed to find the row inserted into Postgres "
        f"and tracked in Redis. hits: {hits}"
    )
