"""Thread-safety of the Postgres adapters' shared connection (issue #83).

A single psycopg connection driven by two threads interleaves cursors and folds
concurrent writes into an open ``transaction()``. Each adapter serializes all
public methods on a per-instance re-entrant lock, and ``transaction()`` holds it
across the whole block. These tests use a FAKE connection so they run without a
live Postgres — they verify the LOCKING (the SQL fidelity is covered by the
conformance suite against a real DB). They fail if the lock is removed.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("psycopg")

from mcm_engine.adapters.postgres._concurrency import serialize_methods  # noqa: E402
from mcm_engine.adapters.postgres.counters import PostgresCounters  # noqa: E402
from mcm_engine.adapters.postgres.storage import PostgresStorage  # noqa: E402
from mcm_engine.backends import EntityType  # noqa: E402


class _Detector:
    """Records the max number of threads simultaneously 'inside' a guarded
    region — 1 means access was serialized."""

    def __init__(self):
        self.active = 0
        self.max_seen = 0
        self._l = threading.Lock()

    def enter(self):
        with self._l:
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)

    def exit(self):
        with self._l:
            self.active -= 1


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.detector.enter()
        time.sleep(0.01)      # widen the interleave window
        self._conn.detector.exit()

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, n=None):
        return []

    def __iter__(self):
        return iter(())


class _FakeConn:
    def __init__(self):
        self.row_factory = None
        self.detector = _Detector()
        self.commits = 0

    def cursor(self, name=None):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


# --- the decorator itself --------------------------------------------------


def test_serialize_methods_serializes_public_methods():
    det = _Detector()

    @serialize_methods
    class Thing:
        def __init__(self):
            self._lock = threading.RLock()

        def work(self):
            det.enter()
            time.sleep(0.01)
            det.exit()

    t = Thing()
    threads = [threading.Thread(target=t.work) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert det.max_seen == 1


def test_serialize_methods_skips_generators():
    import inspect

    @serialize_methods
    class Thing:
        def __init__(self):
            self._lock = threading.RLock()

        def stream(self):
            yield 1

    # a wrapped generator would no longer BE a generator function
    assert inspect.isgeneratorfunction(Thing.stream)


# --- adapter-level serialization -------------------------------------------


def test_counters_increment_is_serialized():
    counters = PostgresCounters("postgresql://x/y", conn=_FakeConn())
    threads = [
        threading.Thread(
            target=counters.increment, args=(EntityType.RULE, 1, "hit_count"))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert counters._conn.detector.max_seen == 1


def test_storage_transaction_blocks_other_methods():
    storage = PostgresStorage("postgresql://x/y", conn=_FakeConn())
    entered = threading.Event()
    other_done = threading.Event()

    def hold_tx():
        with storage.transaction():
            entered.set()
            time.sleep(0.3)

    def other():
        entered.wait(2)
        storage.find_by_id(EntityType.RULE, 1)   # wrapped -> must block on _lock
        other_done.set()

    ta = threading.Thread(target=hold_tx)
    tb = threading.Thread(target=other)
    ta.start(); tb.start()
    entered.wait(2)
    assert not other_done.wait(0.15), "a method ran during an open transaction"
    ta.join(2); tb.join(2)
    assert other_done.is_set()
