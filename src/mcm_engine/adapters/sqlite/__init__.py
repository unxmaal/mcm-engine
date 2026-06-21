"""Embedded SQLite adapter — the reference implementation.

All four Protocols (StorageBackend, CounterStore, SearchBackend,
SessionStore) ship here. Each can be loaded via the registry under the
name "embedded". They share the same SQLite file when given the same
db_path.
"""
from .counters import SqliteCounters
from .search import SqliteSearch
from .session import InMemorySession
from .storage import SqliteStorage

__all__ = ["SqliteStorage", "SqliteCounters", "SqliteSearch", "InMemorySession"]
