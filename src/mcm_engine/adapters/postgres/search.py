"""Postgres SearchBackend (MCM2-15a).

Uses the ``tsvector`` generated columns + GIN indexes from
PostgresStorage's DDL. Scoring via ``ts_rank_cd``. Returns SearchHit
dataclasses with score normalized higher-better (ts_rank_cd is already
non-negative-higher-better, so no flip needed at this boundary; see
``rules/mcm2/search-adapter-contract-normalize-composite-rank-sign-at-the-boundary.md``).

For LIKE fallback: when ``plainto_tsquery`` returns an empty tsquery
(e.g. the query is all stopwords or special characters), fall back to
an ILIKE scan across the natural-language columns.
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
    SearchHit,
)


# Per-entity table + LIKE-fallback columns (mirrors SqliteSearch._FTS).
_TABLES: dict[EntityType, dict[str, Any]] = {
    EntityType.KNOWLEDGE: {
        "table": "knowledge",
        "like_columns": ["topic", "summary", "detail", "tags"],
    },
    EntityType.NEGATIVE: {
        "table": "negative_knowledge",
        "like_columns": ["category", "what_failed", "why_failed"],
    },
    EntityType.ERROR: {
        "table": "errors",
        "like_columns": ["pattern", "context", "root_cause"],
    },
    EntityType.RULE: {
        "table": "rules",
        "like_columns": ["title", "keywords", "description", "category"],
    },
}


class PostgresSearch:
    """SearchBackend on Postgres tsvector + ts_rank_cd."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, dsn: str, *, conn: Optional["psycopg.Connection"] = None):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:
            raise MissingDependencyError(
                "PostgresSearch requires psycopg. "
                "Install with: pip install 'mcm-engine[postgres]'"
            ) from e
        self._dsn = dsn
        if conn is None:
            conn = psycopg.connect(dsn, row_factory=dict_row)
        else:
            conn.row_factory = dict_row
        self._conn = conn

    def search(
        self,
        query: str,
        *,
        entity_types: Optional[set[EntityType]] = None,
        limit: int = 10,
        project: Optional[str] = None,
        caller: Optional[str] = None,
    ) -> list[SearchHit]:
        targets = entity_types if entity_types is not None else set(EntityType)
        hits: list[SearchHit] = []
        for etype in targets:
            hits.extend(self._search_one(etype, query, limit=limit, project=project))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def reindex(self, entity_type: Optional[EntityType] = None) -> None:
        """Refresh the GIN index. tsvector generated columns auto-populate
        on INSERT/UPDATE, so the routine ``reindex`` here is a REINDEX of
        the GIN index — rarely needed but cheap to expose."""
        targets = {entity_type} if entity_type else set(EntityType)
        with self._conn.cursor() as cur:
            for et in targets:
                table = _TABLES[et]["table"]
                # Use the named index from PostgresStorage's DDL.
                idx_table = "negative" if table == "negative_knowledge" else table.rstrip("s") if table == "errors" else table
                # Just rebuild the named GIN. Names in postgres/storage.py:
                # idx_knowledge_tsv, idx_negative_tsv, idx_errors_tsv, idx_rules_tsv
                tsv_index_name = {
                    "knowledge": "idx_knowledge_tsv",
                    "negative_knowledge": "idx_negative_tsv",
                    "errors": "idx_errors_tsv",
                    "rules": "idx_rules_tsv",
                }[table]
                cur.execute(f"REINDEX INDEX {tsv_index_name}")
        self._conn.commit()

    def search_plugin(
        self,
        scope: Any,
        query: str,
        limit: int = 10,
        *,
        caller: Optional[str] = None,
    ) -> list[str]:
        """Postgres-side plugin search.

        SearchScope.fts_table/fts_columns are FTS5-specific and ignored
        here. We use ILIKE across ``like_columns`` against
        ``base_table`` — plugin authors who want full tsvector ranking
        on Postgres should add tsvector columns in their own
        ``get_schema_sql`` and ship a backend-specific search hook in a
        future contract iteration.
        """
        if not scope.like_columns:
            return []
        conditions = " OR ".join(f"{c} ILIKE %s" for c in scope.like_columns)
        cols = ", ".join(scope.display_columns)
        like_pattern = f"%{query}%"
        params = tuple(like_pattern for _ in scope.like_columns) + (limit,)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM {scope.base_table} "
                f"WHERE {conditions} LIMIT %s",
                params,
            )
            rows = cur.fetchall()

        results: list[str] = []
        for r in rows:
            if scope.format_fn:
                results.append(scope.format_fn(r))
            else:
                vals = [str(r[c]) for c in scope.display_columns if r[c]]
                results.append(f"[{scope.label}] {' | '.join(vals)}")
        return results

    # ---- internals ----

    def _search_one(
        self,
        etype: EntityType,
        query: str,
        *,
        limit: int,
        project: Optional[str],
    ) -> list[SearchHit]:
        table = _TABLES[etype]["table"]

        proj_clause = ""
        proj_params: tuple = ()
        if project is not None and table != "rules":
            proj_clause = "AND (project = %s OR project IS NULL OR project = '')"
            proj_params = (project,)

        # Try tsvector path first.
        rows: list[dict[str, Any]] = []
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT id, pinned, "
                f"  ts_rank_cd(tsv, plainto_tsquery('english', %s)) AS rank "
                f"FROM {table} "
                f"WHERE tsv @@ plainto_tsquery('english', %s) {proj_clause} "
                f"ORDER BY rank DESC LIMIT %s",
                (query, query, *proj_params, limit),
            )
            rows = cur.fetchall()

        # LIKE fallback: empty query (all stopwords / hostile chars) won't
        # match anything via tsvector; pivot to ILIKE.
        if not rows:
            like_cols = _TABLES[etype]["like_columns"]
            conditions = " OR ".join(f"{c} ILIKE %s" for c in like_cols)
            like_pattern = f"%{query}%"
            like_params = tuple(like_pattern for _ in like_cols)
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, pinned, 0.5::float8 AS rank FROM {table} "
                    f"WHERE ({conditions}) {proj_clause} LIMIT %s",
                    (*like_params, *proj_params, limit),
                )
                rows = cur.fetchall()

        return [
            SearchHit(
                entity_type=etype,
                entity_id=r["id"],
                # ts_rank_cd is already higher-better and non-negative.
                # LIKE fallback uses the documented 0.5 baseline (matches
                # SqliteSearch's normalization).
                score=float(r["rank"]) if r["rank"] is not None else 0.5,
                is_pinned=bool(r["pinned"]),
                is_stale=False,
                counters_snapshot={},
                row=None,
            )
            for r in rows
        ]
