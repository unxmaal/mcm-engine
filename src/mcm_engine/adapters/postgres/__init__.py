"""Postgres reference adapters (MCM2-08+).

Importing this module requires ``psycopg`` (the `postgres` extra). The
engine core does NOT import this module — discovery happens through
``mcm_engine.registry.AdapterRegistry`` via the
``mcm_engine.adapters.storage = "postgres"`` entry point (or via the
``module:Class`` escape hatch in dev).
"""
from .counters import PostgresCounters
from .search import PostgresSearch
from .storage import PostgresStorage

__all__ = ["PostgresCounters", "PostgresSearch", "PostgresStorage"]
