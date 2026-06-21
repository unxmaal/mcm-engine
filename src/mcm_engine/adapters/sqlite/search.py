"""Embedded SQLite SearchBackend.

FTS5 across the four searchable tables (knowledge, negative, errors,
rules) with a LIKE fallback per the existing engine behavior. Returns
SearchHit dataclasses; the Python composite scorer (engine-side) is the
caller's responsibility — this adapter only contributes the lexical
score per the inventory's high-priority finding.

Hit-counting (the inline UPDATE-after-SELECT pattern) does NOT happen
here. Counter writes are CounterStore's concern; this adapter is
read-only.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ...backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    SearchHit,
)
from ...db import KnowledgeDB, build_fts_queries, build_like_patterns, sanitize_fts

# Per-entity FTS table mapping. The adapter knows the SQLite-specific
# FTS5 virtual-table layout; the abstract score (higher = better) lives
# in SearchHit.score regardless of how this adapter computes it.
_FTS: dict[EntityType, dict[str, Any]] = {
    EntityType.KNOWLEDGE: {
        "fts_table":  "knowledge_fts",
        "base_table": "knowledge",
        "like_columns": ["topic", "summary", "detail", "tags"],
    },
    EntityType.NEGATIVE: {
        "fts_table":  "negative_fts",
        "base_table": "negative_knowledge",
        "like_columns": ["category", "what_failed", "why_failed"],
    },
    EntityType.ERROR: {
        "fts_table":  "errors_fts",
        "base_table": "errors",
        "like_columns": ["pattern", "context", "root_cause"],
    },
    EntityType.RULE: {
        "fts_table":  "rules_fts",
        "base_table": "rules",
        "like_columns": ["title", "keywords", "description", "category"],
    },
}


class SqliteSearch:
    """SearchBackend on the embedded SQLite reference."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, db_path: str | Any = ":memory:", db: Optional[KnowledgeDB] = None):
        self._db = db if db is not None else KnowledgeDB(db_path)

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
        fts_queries = build_fts_queries(query)
        like_patterns = build_like_patterns(query)

        hits: list[SearchHit] = []
        for etype in targets:
            cfg = _FTS[etype]
            hits.extend(
                self._search_one(
                    etype, cfg, fts_queries, like_patterns,
                    limit=limit, project=project,
                )
            )
        # Normalize: higher score = better. SQLite FTS5 rank is negative;
        # we flip the sign so SearchHit.score is a uniform "higher better."
        # Already done per-hit below.
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def reindex(self, entity_type: Optional[EntityType] = None) -> None:
        """Rebuild the FTS5 indexes from the content tables."""
        targets = {entity_type} if entity_type else set(EntityType)
        for et in targets:
            fts = _FTS[et]["fts_table"]
            self._db.execute_write(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        self._db.commit()

    def search_plugin(
        self,
        scope: Any,
        query: str,
        limit: int = 10,
        *,
        caller: Optional[str] = None,
    ) -> list[str]:
        """Search a plugin's table per the SearchScope descriptor.

        MCM2-07: this is the new home for what used to live as
        SearchScope.search. The plugin layer carries only metadata; this
        adapter owns the SQL.
        """
        fts_query = sanitize_fts(query)
        like_pattern = f"%{query}%"
        rows: list[sqlite3.Row] = []

        # Try FTS5 first when the scope declares one.
        if scope.fts_table and scope.fts_columns:
            try:
                cols = ", ".join(f"b.{c}" for c in scope.display_columns)
                rows = self._db.execute(
                    f"SELECT {cols} FROM {scope.fts_table} f "
                    f"JOIN {scope.base_table} b ON f.rowid = b.id "
                    f"WHERE {scope.fts_table} MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

        # LIKE fallback.
        if not rows and scope.like_columns:
            conditions = " OR ".join(f"{c} LIKE ?" for c in scope.like_columns)
            cols = ", ".join(scope.display_columns)
            params = tuple(like_pattern for _ in scope.like_columns) + (limit,)
            rows = self._db.execute(
                f"SELECT {cols} FROM {scope.base_table} WHERE {conditions} LIMIT ?",
                params,
            ).fetchall()

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
        cfg: dict[str, Any],
        fts_queries: list[str],
        like_patterns: list[str],
        *,
        limit: int,
        project: Optional[str],
    ) -> list[SearchHit]:
        fts_table = cfg["fts_table"]
        base_table = cfg["base_table"]

        # Build project filter (rules table has no project column).
        proj_clause = ""
        proj_params: tuple = ()
        if project is not None and base_table != "rules":
            proj_clause = "AND (b.project = ? OR b.project IS NULL OR b.project = '')"
            proj_params = (project,)

        rows: list[sqlite3.Row] = []
        for fts_q in fts_queries:
            try:
                rows = self._db.execute(
                    f"SELECT b.id AS id, b.pinned AS pinned, rank AS fts_rank "
                    f"FROM {fts_table} f JOIN {base_table} b ON f.rowid = b.id "
                    f"WHERE {fts_table} MATCH ? {proj_clause} "
                    f"ORDER BY rank LIMIT ?",
                    (fts_q, *proj_params, limit),
                ).fetchall()
                if rows:
                    break
            except sqlite3.OperationalError:
                continue

        if not rows and like_patterns:
            # LIKE fallback — per-term OR across the configured columns.
            like_cols = cfg["like_columns"]
            clauses: list[str] = []
            params: list[Any] = []
            for pat in like_patterns:
                col_clauses = [f"{c} LIKE ?" for c in like_cols]
                clauses.append("(" + " OR ".join(col_clauses) + ")")
                params.extend([pat] * len(like_cols))
            where = " OR ".join(clauses)

            proj_clause_like = ""
            proj_params_like: tuple = ()
            if project is not None and base_table != "rules":
                proj_clause_like = "AND (project = ? OR project IS NULL OR project = '')"
                proj_params_like = (project,)

            rows = self._db.execute(
                f"SELECT id, pinned, NULL AS fts_rank FROM {base_table} "
                f"WHERE ({where}) {proj_clause_like} LIMIT ?",
                tuple(params) + proj_params_like + (limit,),
            ).fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            raw_rank = r["fts_rank"]
            # FTS5 rank is negative-better → flip to higher-better; LIKE
            # has no rank, treat as a baseline 0.5.
            score = (-float(raw_rank)) if raw_rank is not None else 0.5
            hits.append(SearchHit(
                entity_type=etype,
                entity_id=r["id"],
                score=score,
                is_pinned=bool(r["pinned"]),
                is_stale=False,
                counters_snapshot={},
                row=None,
            ))
        return hits
