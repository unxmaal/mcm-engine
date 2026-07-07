"""Per-connection serialization for the Postgres adapters (issue #83 hardening).

Each Postgres adapter holds ONE psycopg connection for the process. Post-#79 the
same class of hazard the SQLite side has applies here: the connection carries a
`_tx_depth` and a deferred-commit protocol, and a psycopg connection driven by
two threads at once raises "another operation in progress" or folds a concurrent
write into an open transaction. Today the tool path is serialized (FastMCP runs
sync tools inline on the event loop) so this is defense-in-depth, but it must be
correct the moment any tool is offloaded to a thread or made async.

``serialize_methods`` wraps every public, NON-generator method of an adapter to
hold a per-instance re-entrant lock, so all access to the connection is
serialized. It mirrors the ``KnowledgeDB`` lock on the SQLite side.

Generator methods are skipped on purpose:
  * ``transaction()`` takes the lock across its WHOLE block itself (a naive
    wrapper would release the lock before the block body runs).
  * the streaming ``iter_*()`` reads use server-side cursors and are batch /
    single-threaded; holding the lock across a caller-driven stream would pin the
    connection for the stream's lifetime. They are left unlocked by design.

This is a correctness lock, not a scaling mechanism. The horizontal-scale design
(a psycopg connection pool per pod + offloading CPU-heavy tools off the event
loop) is described in docs/scaling.md; the lock is what the pool replaces.
"""
from __future__ import annotations

import functools
import inspect


def _wrap(method):
    @functools.wraps(method)
    def inner(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return inner


def serialize_methods(cls):
    """Class decorator: wrap public, non-generator methods to hold ``self._lock``.

    The adapter's ``__init__`` must create ``self._lock`` (a ``threading.RLock``)
    before any wrapped method is called. Re-entrant, so a wrapped method calling
    another wrapped method on the same instance — or a lock-holding
    ``transaction()`` body — does not deadlock."""
    for name, attr in list(vars(cls).items()):
        if name.startswith("_"):
            continue
        if not inspect.isfunction(attr) or inspect.isgeneratorfunction(attr):
            continue
        setattr(cls, name, _wrap(attr))
    return cls
