"""Postgres CounterStore (MCM2-13b).

Same shape as SqliteCounters: write-through to entry-row columns on the
six entity tables. Demonstrates that the CounterStore contract is
product-agnostic by reusing the same single-row-update strategy against
a different durable store.

Counter shape per entity type matches SqliteCounters exactly. The only
real difference is timestamps (Postgres ``TIMESTAMPTZ now()`` vs SQLite
``datetime('now')``) and booleans (real ``BOOLEAN`` vs ``INTEGER 0/1``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import psycopg

from ...backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    MissingDependencyError,
)


_TABLE: dict[EntityType, str] = {
    EntityType.KNOWLEDGE: "knowledge",
    EntityType.NEGATIVE:  "negative_knowledge",
    EntityType.ERROR:     "errors",
    EntityType.RULE:      "rules",
}

_COUNTER_COLUMNS: dict[str, set[str]] = {
    "knowledge":          {"hit_count", "reinforcement_count", "pinned", "last_hit_at"},
    "rules":              {"hit_count", "reinforcement_count", "pinned", "last_hit_at",
                           "correct_count", "incorrect_count"},
    "negative_knowledge": {"pinned"},
    "errors":             {"pinned"},
}


class PostgresCounters:
    """CounterStore on Postgres — write-through to entry-row columns."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, dsn: str, *, conn: Optional["psycopg.Connection"] = None):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:
            raise MissingDependencyError(
                "PostgresCounters requires psycopg. "
                "Install with: pip install 'mcm-engine[postgres]'"
            ) from e
        self._dsn = dsn
        if conn is None:
            conn = psycopg.connect(dsn, row_factory=dict_row)
        else:
            conn.row_factory = dict_row
        self._conn = conn

    def _resolve_column(self, entity_type: EntityType, counter_name: str) -> str:
        table = _TABLE[entity_type]
        cols = _COUNTER_COLUMNS[table]
        if counter_name not in cols:
            raise ValueError(
                f"{table} has no counter column {counter_name!r}. "
                f"Valid for this entity_type: {sorted(cols)}"
            )
        return counter_name

    def increment(
        self,
        entity_type: EntityType,
        entity_id: int,
        counter_name: str,
        by: int = 1,
    ) -> None:
        col = self._resolve_column(entity_type, counter_name)
        table = _TABLE[entity_type]
        with self._conn.cursor() as cur:
            if col == "last_hit_at":
                cur.execute(
                    f"UPDATE {table} SET last_hit_at = now() WHERE id = %s",
                    (entity_id,),
                )
            elif col == "pinned":
                # pinned is BOOLEAN — increment-by-1 is "set true".
                cur.execute(
                    f"UPDATE {table} SET pinned = TRUE WHERE id = %s",
                    (entity_id,),
                )
            else:
                cur.execute(
                    f"UPDATE {table} SET {col} = {col} + %s WHERE id = %s",
                    (by, entity_id),
                )
        self._conn.commit()

    def get(self, entity_type: EntityType, entity_id: int) -> dict[str, Any]:
        table = _TABLE[entity_type]
        cols = sorted(_COUNTER_COLUMNS[table])
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE id = %s",
                (entity_id,),
            )
            row = cur.fetchone()
        if row is None:
            return {}
        out: dict[str, Any] = {}
        for c in cols:
            v = row[c]
            if c == "pinned":
                out[c] = bool(v)
            else:
                out[c] = v
        return out

    def top_by(
        self,
        entity_type: EntityType,
        counter_name: str,
        k: int,
    ) -> list[tuple[int, float]]:
        col = self._resolve_column(entity_type, counter_name)
        table = _TABLE[entity_type]
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT id, {col} AS value FROM {table} "
                f"ORDER BY {col} DESC NULLS LAST LIMIT %s",
                (k,),
            )
            rows = cur.fetchall()
        return [(r["id"], float(r["value"] or 0)) for r in rows]

    def flush(self) -> None:
        # Write-through, no buffered state.
        return None

    def last_flushed_snapshot(
        self, entity_type: EntityType, entity_id: int,
    ) -> dict[str, Any]:
        return self.get(entity_type, entity_id)
