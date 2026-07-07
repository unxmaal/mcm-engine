"""Connection pooling for the Postgres adapters (issue #83 scaling successor).

The earlier fix serialized every adapter method on one shared connection with a
lock — correct, but a throughput ceiling: one operation at a time per pod. This
replaces that with a per-pod ``psycopg_pool.ConnectionPool``. Each adapter method
borrows a connection for its duration and returns it; Postgres runs concurrent
operations in parallel (MVCC), and there is no shared ``_tx_depth`` to corrupt.

How it stays low-churn: method bodies keep using ``self._conn`` and
``self._conn.cursor()``. ``self._conn`` is now a **property** that returns the
connection bound to the current call-chain (a ``contextvars.ContextVar``); the
``@pooled_adapter`` class decorator wraps each public, non-generator method to
borrow a connection from the pool and bind it for the method's duration —
committing on clean exit, rolling back on error (that is ``pool.connection()``'s
contract, which wraps psycopg's ``with conn:``). A method called INSIDE an open
``transaction()`` reuses that block's connection instead of borrowing, so the
whole block is one unit of work.

``transaction()`` is a generator (skipped by the decorator): it borrows one
connection, binds it across the block, and the block commits/rolls back as a
unit. Nested ``transaction()`` reuses the outer connection.

See docs/scaling.md for where this sits (Layer B) and what still rides on top
(offloading CPU-heavy tools off the event loop).
"""
from __future__ import annotations

import contextvars
import functools
import inspect
from contextlib import contextmanager
from typing import Any, Optional

# The connection bound to the current call-chain (a borrowed pool connection or
# an open transaction's connection). None outside any adapter method.
_active_conn: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "mcm_pg_active_conn", default=None
)


class _SingleConnPool:
    """A pool-shaped wrapper around ONE caller-owned connection, for tests /
    conformance that construct an adapter with ``conn=``. Mirrors
    ``ConnectionPool.connection()``: psycopg's ``with conn:`` commits on clean
    exit / rolls back on error; the connection is reused, never closed here."""

    def __init__(self, conn: Any):
        self._conn = conn

    @contextmanager
    def connection(self, timeout: float | None = None):
        with self._conn:
            yield self._conn

    def close(self) -> None:  # caller owns the raw connection
        pass


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 10):
    """Open a ConnectionPool whose connections use ``dict_row`` (the row factory
    every adapter expects). ``max_size`` bounds connections per pod; front
    Postgres with RDS Proxy / PgBouncer when many pods multiply that (see
    docs/scaling.md)."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        open=True,
        kwargs={"row_factory": dict_row},
    )


def resolve_pool(dsn: str, conn: Any = None, pool: Any = None):
    """Pick the connection source for an adapter, in precedence order: an
    injected shared ``pool`` (build_context wires one per pod), else a
    caller-owned ``conn`` wrapped as a single-connection pool, else a fresh pool
    opened from ``dsn``."""
    if pool is not None:
        return pool
    if conn is not None:
        return _SingleConnPool(conn)
    return make_pool(dsn)


@contextmanager
def _borrow(self):
    """Yield the active call-chain connection, or borrow one from the pool for
    the duration of the block (committing on clean exit / rolling back on
    error)."""
    existing = _active_conn.get()
    if existing is not None:
        yield existing
    else:
        with self._pool.connection() as conn:
            token = _active_conn.set(conn)
            try:
                yield conn
            finally:
                _active_conn.reset(token)


def _active_conn_property(self):
    conn = _active_conn.get()
    if conn is None:
        raise RuntimeError(
            "no active Postgres connection on this call-chain — a wrapped "
            "adapter method borrows one; direct access outside a method must go "
            "through the pool (see transport borrow sites)."
        )
    return conn


def _wrap(method):
    @functools.wraps(method)
    def inner(self, *args, **kwargs):
        if _active_conn.get() is not None:
            # Already inside a borrowed scope (a transaction or an outer wrapped
            # method): reuse it so the whole thing is one unit of work.
            return method(self, *args, **kwargs)
        with self._pool.connection() as conn:
            token = _active_conn.set(conn)
            try:
                return method(self, *args, **kwargs)
            finally:
                _active_conn.reset(token)

    return inner


# Public methods that must NOT borrow a connection (they manage the pool itself,
# or otherwise don't touch a connection).
_NO_BORROW = frozenset({"close"})


def pooled_adapter(cls):
    """Class decorator: give the adapter a ``_conn`` property (the current
    call-chain connection) and wrap every public, non-generator method to run
    inside a borrowed connection. Generators (``transaction()``, the streaming
    ``iter_*()``) and pool-lifecycle methods (``close``) are skipped —
    ``transaction()`` scopes the connection itself, the ``iter_*`` streams borrow
    their own, and ``close`` must not check out a connection to shut the pool."""
    cls._conn = property(_active_conn_property)
    if not any("close" in vars(k) for k in cls.__mro__):
        cls.close = _close
    for name, attr in list(vars(cls).items()):
        if name.startswith("_") or name in _NO_BORROW:
            continue
        if not inspect.isfunction(attr) or inspect.isgeneratorfunction(attr):
            continue
        setattr(cls, name, _wrap(attr))
    return cls


def _close(self) -> None:
    """Close the underlying pool (no-op for the single-connection wrapper, which
    doesn't own the connection). Safe to call more than once."""
    close = getattr(self._pool, "close", None)
    if callable(close):
        close()
