"""Embedded SessionStore — in-process dict.

Per OQ-5: tracker state is in-memory only. The embedded reference is a
plain dict that lives for the life of the process; restarts lose state.
Third-party adapters MAY persist (Redis, SQLite session table).

This adapter accepts the same `db_path` / `db` kwargs as its siblings so
the wiring layer can instantiate all four uniformly, but it ignores
them — state never touches disk in the embedded reference.
"""
from __future__ import annotations

from typing import Any, Optional

from ...backends import CONTRACT_VERSION, Capability


class InMemorySession:
    """SessionStore — in-process key/value, lost on restart."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, db_path: Optional[str] = None, db: Any = None):
        # db_path / db accepted for wiring uniformity; intentionally unused.
        self._state: dict[str, dict[str, Any]] = {}

    def load_state(self, key: str) -> Optional[dict[str, Any]]:
        v = self._state.get(key)
        # Return a shallow copy so callers can't mutate our internal state.
        return dict(v) if v is not None else None

    def save_state(self, key: str, state: dict[str, Any]) -> None:
        self._state[key] = dict(state)
