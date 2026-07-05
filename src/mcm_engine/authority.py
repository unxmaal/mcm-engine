"""Authoritative-store binding — fail closed when the store in hand isn't the
one that was DEFINED.

The rule (stray-db): once a project declares its authoritative database
(``config.authoritative_store``), no entrypoint may silently operate against a
different one — not a cwd-relative default, not a stale local copy, not an
accidentally-fabricated db. Enforcement is one function, called at every
composition root that opens a store.

Binding is opt-in by design: an empty ``authoritative_store`` means "unpinned"
and preserves historical behavior. Pin it in ``mcm-engine.yaml`` to the value of
``StorageIdentity`` you intend (e.g. ``sqlite:/abs/path/knowledge.db`` or
``postgres:host/dbname``) and any run that resolves elsewhere fails loudly
instead of quietly writing to the wrong brain.
"""
from __future__ import annotations

from .backends import StorageIdentity


class WrongStoreError(Exception):
    """Raised when the resolved store does not match the declared authoritative
    store. Fail-closed: the operation must stop, not proceed against the wrong
    database."""


def verify_store(actual: StorageIdentity, expected: str) -> None:
    """Assert the store actually opened matches the declared authoritative one.

    ``expected`` is ``config.authoritative_store`` (a ``str(StorageIdentity)``).
    Empty ``expected`` == unpinned == no enforcement. A non-empty mismatch raises
    ``WrongStoreError`` naming both stores so the operator sees exactly which
    database was about to be used and which was declared.
    """
    if not expected:
        return
    if str(actual) != expected:
        raise WrongStoreError(
            f"refusing to use a non-authoritative database: resolved "
            f"{actual}, but this project is pinned to {expected}. "
            f"Fix your config/backends, or update authoritative_store if you "
            f"genuinely mean to point at a different database."
        )
