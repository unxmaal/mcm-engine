"""Embedded SQLite CounterStore.

In the embedded reference, counters live on the entry row itself —
matching today's mcm-engine behavior. `increment` does UPDATE col = col + 1
on the entity table; `get` reads the snapshot back. Adapters that want
to live off-row (Redis, dedicated Postgres table) implement their own
flush semantics.
"""
from __future__ import annotations

from typing import Any, Optional

from ...backends import CONTRACT_VERSION, Capability, EntityType
from ...db import KnowledgeDB


_TABLE: dict[EntityType, str] = {
    EntityType.KNOWLEDGE: "knowledge",
    EntityType.NEGATIVE:  "negative_knowledge",
    EntityType.ERROR:     "errors",
    EntityType.RULE:      "rules",
}

# Which counter columns each table actually exposes. negative + errors
# only have `pinned`; knowledge + rules have the full set.
_COUNTER_COLUMNS: dict[str, set[str]] = {
    "knowledge":          {"hit_count", "reinforcement_count", "pinned", "last_hit_at"},
    "rules":              {"hit_count", "reinforcement_count", "pinned", "last_hit_at"},
    "negative_knowledge": {"pinned"},
    "errors":             {"pinned"},
}


class SqliteCounters:
    """CounterStore on the embedded SQLite reference.

    Counters are columns on the entry row; writes are synchronous
    (write-through). last_flushed_snapshot is identical to live data
    because there is no flush window in this adapter.
    """

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, db_path: str | Any = ":memory:", db: Optional[KnowledgeDB] = None):
        self._db = db if db is not None else KnowledgeDB(db_path)

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
        if col == "last_hit_at":
            self._db.execute_write(
                f"UPDATE {table} SET last_hit_at = datetime('now') WHERE id = ?",
                (entity_id,),
            )
        else:
            self._db.execute_write(
                f"UPDATE {table} SET {col} = {col} + ? WHERE id = ?",
                (by, entity_id),
            )
        self._db.commit()

    def get(self, entity_type: EntityType, entity_id: int) -> dict[str, Any]:
        table = _TABLE[entity_type]
        cols = sorted(_COUNTER_COLUMNS[table])
        sql = f"SELECT {', '.join(cols)} FROM {table} WHERE id = ?"
        row = self._db.execute(sql, (entity_id,)).fetchone()
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
        rows = self._db.execute(
            f"SELECT id, {col} AS value FROM {table} "
            f"ORDER BY {col} DESC LIMIT ?",
            (k,),
        ).fetchall()
        return [(r["id"], float(r["value"] or 0)) for r in rows]

    def flush(self) -> None:
        # Write-through; no buffered state.
        return None

    def last_flushed_snapshot(
        self, entity_type: EntityType, entity_id: int
    ) -> dict[str, Any]:
        # Identical to live for the embedded reference.
        return self.get(entity_type, entity_id)
