"""MCM2-13: Redis CounterStore conformance.

Skipped automatically when:
  - ``redis`` isn't installed (the `redis` extra), OR
  - no Redis reachable at MCM_TEST_REDIS_URL (default points at the
    docker-compose service on 127.0.0.1:56379).

To run locally:
    docker compose -f tests/docker-compose.yml up -d redis
    uv pip install -e '.[redis]'
    uv run python -m pytest tests/test_adapter_redis_counters.py
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.testing.conformance import CounterConformance

DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/0"
TEST_REDIS_URL = os.environ.get("MCM_TEST_REDIS_URL", DEFAULT_REDIS_URL)


def _redis_available() -> bool:
    try:
        import redis  # noqa: F401
    except ImportError:
        return False
    try:
        import redis as _redis
        client = _redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason=(
        f"Redis not reachable at {TEST_REDIS_URL} "
        "(start with `docker compose -f tests/docker-compose.yml up -d redis` "
        "and `uv pip install -e '.[redis]'`)."
    ),
)


class TestRedisCounters(CounterConformance):
    @pytest.fixture
    def _shared(self, tmp_path):
        from mcm_engine.adapters.redis.counters import RedisCounters
        from mcm_engine.adapters.sqlite.storage import SqliteStorage

        storage = SqliteStorage(db_path=str(tmp_path / "redis-counters.db"))
        storage.ensure_schema()
        # Namespace per-test so tests don't trip over each other when
        # parallelized. Cleared on entry.
        ns = f"mcm:test:{tmp_path.name}:"
        counters = RedisCounters(url=TEST_REDIS_URL, namespace=ns)
        counters.reset_namespace()
        return storage, counters

    @pytest.fixture
    def storage(self, _shared):
        return _shared[0]

    @pytest.fixture
    def counters(self, _shared):
        return _shared[1]
