"""Redis CounterStore (MCM2-13).

Live counters live in Redis sorted sets — one ZSET per
(entity_type, counter_name). The score is the counter value; the member
is the entity_id (as a string).

Why ZSETs and not HASHes:
  - ZSETs give us O(log N) ``top_by`` (ZREVRANGE WITHSCORES) without
    scanning every key.
  - ZSETs let us atomically increment with ZINCRBY.
  - ``get`` for a single entity is a handful of ZSCOREs (one per known
    counter for the entity type) — cheap.

Flush model: Redis IS the live store. ``flush()`` is a no-op here; durable
write-back to a paired ``StorageBackend`` is a separate concern (the
write-back daemon, not part of the read path). See
``docs/counter-flush-policy.md``.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import redis

from ...backends import CONTRACT_VERSION, Capability, EntityType


# Per-entity-type counter columns — same shape as SqliteCounters so the
# rejection-of-unknown-counters behavior matches across adapters.
_COUNTERS_FOR_TYPE: dict[EntityType, set[str]] = {
    EntityType.KNOWLEDGE: {"hit_count", "reinforcement_count", "pinned", "last_hit_at"},
    EntityType.RULE:      {"hit_count", "reinforcement_count", "pinned", "last_hit_at"},
    EntityType.NEGATIVE:  {"pinned"},
    EntityType.ERROR:     {"pinned"},
}


class RedisCounters:
    """CounterStore on Redis using one ZSET per (entity_type, counter)."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(
        self,
        url: str = "redis://127.0.0.1:6379/0",
        *,
        namespace: str = "mcm:",
        client: Optional[redis.Redis] = None,
    ):
        self._namespace = namespace
        self._client = client or redis.Redis.from_url(url, decode_responses=True)

    # ---- key helpers ----

    def _zset_key(self, entity_type: EntityType, counter_name: str) -> str:
        return f"{self._namespace}counters:{entity_type.value}:{counter_name}"

    def _validate(self, entity_type: EntityType, counter_name: str) -> None:
        allowed = _COUNTERS_FOR_TYPE.get(entity_type, set())
        if counter_name not in allowed:
            raise ValueError(
                f"{entity_type.value} has no counter column '{counter_name}'"
            )

    # ---- CounterStore Protocol ----

    def increment(
        self,
        entity_type: EntityType,
        entity_id: int,
        counter_name: str,
        by: int = 1,
    ) -> None:
        self._validate(entity_type, counter_name)
        key = self._zset_key(entity_type, counter_name)
        if counter_name == "last_hit_at":
            # last_hit_at is a timestamp, not an accumulator — overwrite
            # to current epoch.
            self._client.zadd(key, {str(entity_id): time.time()})
        else:
            self._client.zincrby(key, by, str(entity_id))

    def get(
        self, entity_type: EntityType, entity_id: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for counter_name in _COUNTERS_FOR_TYPE.get(entity_type, set()):
            score = self._client.zscore(
                self._zset_key(entity_type, counter_name),
                str(entity_id),
            )
            if score is None:
                # Mirror SqliteCounters: absent counter is 0 for accumulators,
                # None for last_hit_at.
                result[counter_name] = None if counter_name == "last_hit_at" else 0
            else:
                if counter_name in ("hit_count", "reinforcement_count"):
                    result[counter_name] = int(score)
                elif counter_name == "pinned":
                    result[counter_name] = bool(int(score))
                else:
                    result[counter_name] = score  # last_hit_at: keep as float
        return result

    def top_by(
        self,
        entity_type: EntityType,
        counter_name: str,
        k: int,
    ) -> list[tuple[int, float]]:
        self._validate(entity_type, counter_name)
        key = self._zset_key(entity_type, counter_name)
        members = self._client.zrevrange(key, 0, k - 1, withscores=True)
        return [(int(member), float(score)) for member, score in members]

    def flush(self) -> None:
        """No-op — Redis IS the live store. Durable write-back happens
        in a separate periodic task, not on the read path."""
        return None

    def last_flushed_snapshot(
        self, entity_type: EntityType, entity_id: int,
    ) -> dict[str, Any]:
        """Same as ``get`` for the Redis adapter — Redis is the
        authoritative live store, so the "flushed" view equals the
        "live" view at all times within this counter store."""
        return self.get(entity_type, entity_id)

    # ---- test / ops convenience (not part of the Protocol) ----

    def reset_namespace(self) -> None:
        """Delete every key under this instance's namespace. Used by
        tests to start clean and by ops to wipe counter state."""
        pattern = f"{self._namespace}*"
        for key in self._client.scan_iter(match=pattern, count=500):
            self._client.delete(key)
