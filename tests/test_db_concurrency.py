"""Thread-safety of the shared SQLite connection (issue #83 hardening).

Post-#79 a SINGLE `KnowledgeDB` connection is shared by every embedded adapter,
and it is also driven by real background threads (the watcher cascade's Timer
threads). The connection carries a plain-int `_tx_depth` and a swappable
`self.conn`; without a lock, a concurrent write's `commit()` no-ops inside
another thread's open transaction (silent lost write, or discarded on that
block's rollback), and `_reconnect` swaps the connection out mid-statement.

These tests exercise the concurrent paths directly. They pass with the
connection lock and FAIL if it is removed — so they are a standing guard against
this defect class, not just a one-off fix check. Timings carry wide margins to
stay deterministic on a loaded CI box.
"""
from __future__ import annotations

import threading
import time

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core


def _wire(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    storage = SqliteStorage(db=db)
    counters = SqliteCounters(db=db)
    rid = storage.insert_rule(RuleRow(id=0, title="t", keywords="k"))
    return db, storage, counters, rid


def _hits(counters, rid) -> int:
    return counters.get(EntityType.RULE, rid).get("hit_count", 0)


def test_transaction_serializes_a_concurrent_writer(tmp_path):
    """While a transaction is open, another thread's write must WAIT for the
    block to finish rather than interleave into it."""
    _db, storage, counters, rid = _wire(tmp_path)
    entered = threading.Event()
    b_finished = threading.Event()

    def hold_tx():
        with storage.transaction():
            entered.set()
            time.sleep(0.3)          # keep the transaction open

    def writer():
        entered.wait(2)
        counters.increment(EntityType.RULE, rid, "hit_count")
        b_finished.set()

    ta = threading.Thread(target=hold_tx)
    tb = threading.Thread(target=writer)
    ta.start(); tb.start()
    entered.wait(2)
    # During the open transaction the writer is blocked on the connection lock.
    assert not b_finished.wait(0.15), "writer ran inside another thread's open transaction"
    ta.join(2); tb.join(2)
    assert b_finished.is_set()
    assert _hits(counters, rid) == 1


def test_rolled_back_transaction_does_not_lose_a_concurrent_write(tmp_path):
    """The corruption case: a sibling write must not be folded into — and lost
    with — a transaction that later rolls back."""
    _db, storage, counters, rid = _wire(tmp_path)
    entered = threading.Event()

    def hold_then_rollback():
        try:
            with storage.transaction():
                entered.set()
                time.sleep(0.2)      # give the writer its chance to attempt the write
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

    def writer():
        entered.wait(2)
        counters.increment(EntityType.RULE, rid, "hit_count")

    ta = threading.Thread(target=hold_then_rollback)
    tb = threading.Thread(target=writer)
    ta.start(); tb.start()
    ta.join(2); tb.join(2)

    # Serialized after the rollback, the increment persists. (Without the lock it
    # would commit-skip inside the open tx and be discarded by the rollback -> 0.)
    assert _hits(counters, rid) == 1


def test_concurrent_increments_are_not_lost(tmp_path):
    """N threads hammering the same counter: every increment must survive."""
    _db, storage, counters, rid = _wire(tmp_path)
    threads_n, per_thread = 8, 40
    start = threading.Event()
    errors: list[Exception] = []

    def worker():
        start.wait(2)
        try:
            for _ in range(per_thread):
                counters.increment(EntityType.RULE, rid, "hit_count")
        except Exception as e:  # a lock/closed-db error would count as a failure
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in ts:
        t.start()
    start.set()
    for t in ts:
        t.join(5)

    assert not errors, f"increment raised under contention: {errors[:3]}"
    assert _hits(counters, rid) == threads_n * per_thread


def test_background_writer_during_tool_transaction(tmp_path):
    """The live H4 case: a background thread (the watcher writes rules the same
    way) mutates storage while a tool transaction is open on another thread. Both
    must complete and both writes must persist."""
    _db, storage, counters, rid = _wire(tmp_path)
    entered = threading.Event()
    other_id: dict = {}

    def tool_tx():
        with storage.transaction():
            entered.set()
            counters.increment(EntityType.RULE, rid, "hit_count")
            time.sleep(0.2)

    def watcher_write():
        entered.wait(2)
        other_id["id"] = storage.insert_rule(RuleRow(id=0, title="bg", keywords="bg"))

    ta = threading.Thread(target=tool_tx)
    tb = threading.Thread(target=watcher_write)
    ta.start(); tb.start()
    ta.join(2); tb.join(2)

    assert _hits(counters, rid) == 1                                   # tool tx committed
    assert storage.find_by_id(EntityType.RULE, other_id["id"]) is not None  # bg write committed
