"""Postgres connection pool: real concurrency + isolation (issue #83).

These run against a live Postgres (the tests/docker-compose.yml container, or
MCM_TEST_POSTGRES_DSN); they skip cleanly when none is reachable. They prove the
pool REPLACED the serialization lock with genuine parallelism, and that the
cross-client corruption the lock guarded against is gone by construction: each
operation runs on its own connection, so one client's rolled-back transaction
can't swallow another's committed write.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from mcm_engine.backends import EntityType, RuleRow  # noqa: E402

DEFAULT_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_DSN)


def _pg_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason=f"Postgres not reachable at {TEST_DSN}")


@pytest.fixture
def wired():
    """One shared pool, storage + counters wired on it (the build_context
    shape). Seeds a rule and yields (storage, counters, pool, rule_id)."""
    from mcm_engine.adapters.postgres._pool import make_pool
    from mcm_engine.adapters.postgres.counters import PostgresCounters
    from mcm_engine.adapters.postgres.storage import PostgresStorage

    pool = make_pool(TEST_DSN, min_size=2, max_size=8)
    storage = PostgresStorage(TEST_DSN, pool=pool)
    counters = PostgresCounters(TEST_DSN, pool=pool)
    storage.ensure_schema()
    storage.truncate_all()
    rid = storage.insert_rule(RuleRow(id=0, title="seed", keywords="seed"))
    try:
        yield storage, counters, pool, rid
    finally:
        pool.close()


def _hits(counters, rid) -> int:
    return counters.get(EntityType.RULE, rid).get("hit_count", 0)


def test_concurrent_increments_lose_nothing(wired):
    _storage, counters, _pool, rid = wired
    n_threads, per = 8, 25
    start = threading.Event()
    errors: list[Exception] = []

    def worker():
        start.wait(2)
        try:
            for _ in range(per):
                counters.increment(EntityType.RULE, rid, "hit_count")
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ts:
        t.start()
    start.set()
    for t in ts:
        t.join(10)

    assert not errors, f"increment raised under contention: {errors[:3]}"
    assert _hits(counters, rid) == n_threads * per


def test_rolled_back_transaction_does_not_lose_a_concurrent_write(wired):
    """The #83 corruption, gone by construction: thread A's transaction rolls
    back; thread B's concurrent increment (its own pooled connection) survives,
    and A's insert does not."""
    storage, counters, _pool, rid = wired
    entered = threading.Event()

    def rollback_tx():
        try:
            with storage.transaction():
                storage.insert_rule(RuleRow(id=0, title="A-only", keywords="x"))
                entered.set()
                time.sleep(0.3)
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

    def writer():
        entered.wait(2)
        counters.increment(EntityType.RULE, rid, "hit_count")

    ta = threading.Thread(target=rollback_tx)
    tb = threading.Thread(target=writer)
    ta.start(); tb.start()
    ta.join(5); tb.join(5)

    assert _hits(counters, rid) == 1                                  # B survived
    assert storage.find_rule_by_title("A-only") is None              # A rolled back


def test_uncommitted_transaction_is_isolated(wired):
    """A transaction's writes are invisible to other connections until it
    commits — the pool gives real transaction isolation, not a shared buffer."""
    storage, _counters, _pool, _rid = wired
    seen_mid = threading.Event()
    visible_mid = {}
    release = threading.Event()

    def holder():
        with storage.transaction():
            storage.insert_rule(RuleRow(id=0, title="pending", keywords="p"))
            seen_mid.set()
            release.wait(2)   # hold the transaction open

    t = threading.Thread(target=holder)
    t.start()
    seen_mid.wait(2)
    # A separate operation (its own pooled connection) must NOT see it yet.
    visible_mid["before_commit"] = storage.find_rule_by_title("pending") is not None
    release.set()
    t.join(5)
    visible_after = storage.find_rule_by_title("pending") is not None

    assert visible_mid["before_commit"] is False   # isolated while open
    assert visible_after is True                    # visible once committed


def test_pool_runs_operations_in_parallel(wired):
    """The pool replaced the serialization lock: two operations overlap in time
    instead of running one-at-a-time."""
    _storage, _counters, pool, _rid = wired

    def sleeper():
        with pool.connection() as conn:
            conn.execute("SELECT pg_sleep(0.3)")

    t0 = time.monotonic()
    ts = [threading.Thread(target=sleeper) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    elapsed = time.monotonic() - t0
    # Serialized this would be ~0.9s; in parallel it's ~0.3s. Generous ceiling.
    assert elapsed < 0.7, f"operations did not run in parallel (took {elapsed:.2f}s)"
