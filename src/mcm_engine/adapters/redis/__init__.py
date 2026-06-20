"""Redis-backed adapter implementations (Phase 2).

Importing this module requires ``redis`` (the `redis` extra). The engine
core does NOT import this module — discovery is via the registry.
"""
from .counters import RedisCounters

__all__ = ["RedisCounters"]
