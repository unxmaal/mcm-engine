"""Unified search tool — FTS5 across all knowledge scopes."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB, build_fts_queries, build_like_patterns, sanitize_fts
from ..tracker import SessionTracker

# Quality gate: FTS5 rank threshold.
# rank is negative (more negative = better match). Results weaker than this are dropped.
# -1.0 filters out single-word incidental matches while keeping real hits.
RANK_THRESHOLD = -1.0

# Staleness: entries older than this many days without a hit are annotated as stale.
STALE_DAYS = 90


def _staleness_tag(age_days: float, last_hit_age_days: float | None, pinned: int = 0) -> str:
    """Return a staleness tag if the entry is old and un-reinforced.

    Pinned items are never stale.
    """
    if pinned:
        return ""
    if age_days < STALE_DAYS:
        return ""
    # If it's been hit recently, it's still active
    if last_hit_age_days is not None and last_hit_age_days < STALE_DAYS:
        return ""
    return " [STALE]"


def _pinned_tag(pinned: int) -> str:
    """Return a [PINNED] tag if the entry is pinned."""
    return " [PINNED]" if pinned else ""


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _fts_match(db: KnowledgeDB, sql: str, fts_queries: list[str], extra_params: tuple, limit: int) -> list:
    """Try each FTS query in order, return results from the first that hits."""
    for fts_query in fts_queries:
        try:
            params = (fts_query,) + extra_params + (limit,)
            rows = db.execute(sql, params).fetchall()
            if rows:
                return rows
        except Exception:
            continue
    return []


def _like_search(db: KnowledgeDB, sql: str, like_patterns: list[str], columns: list[str],
                 extra_params: tuple, limit: int) -> list:
    """Per-term OR LIKE fallback search."""
    if not like_patterns:
        return []

    clauses = []
    params: list = []
    for pat in like_patterns:
        col_clauses = [f"{col} LIKE ?" for col in columns]
        clauses.append(f"({' OR '.join(col_clauses)})")
        params.extend([pat] * len(columns))

    where = " OR ".join(clauses)
    full_sql = sql.replace("{LIKE_WHERE}", where)
    return db.execute(full_sql, tuple(params) + extra_params + (limit,)).fetchall()


def _search_all_scopes(
    db: KnowledgeDB,
    query: str,
    limit: int,
    plugin_search_scopes: list,
    min_rank: float = RANK_THRESHOLD,
    project: str = "",
) -> str:
    """Search all scopes and return formatted results. Used by both search tool and report_error.

    Args:
        min_rank: Quality gate — FTS5 rank threshold. Results with rank > min_rank
                  (i.e., weaker matches) are dropped. Set to 0.0 to disable.
        project: If non-empty, filter to entries matching this project + global (NULL project).
    """
    results: list[str] = []
    fts_queries = build_fts_queries(query)
    like_patterns = build_like_patterns(query)

    # Build project filter clause
    project_filter = ""
    project_params: tuple = ()
    if project:
        project_filter = "AND (k.project = ? OR k.project IS NULL OR k.project = '')"
        project_params = (project,)

    # Knowledge FTS — composite ranking with quality gate
    rank_filter = "AND rank <= ?" if min_rank < 0 else ""
    rank_params: tuple = (min_rank,) if min_rank < 0 else ()

    rows = _fts_match(
        db,
        "SELECT k.id, k.topic, k.kind, k.summary, k.detail, k.tags, k.hit_count, "
        "  k.reinforcement_count, k.pinned, rank AS fts_rank, "
        "  COALESCE((julianday('now') - julianday(k.created_at)), 0) AS age_days, "
        "  CASE WHEN k.last_hit_at IS NOT NULL "
        "    THEN (julianday('now') - julianday(k.last_hit_at)) ELSE NULL END AS last_hit_age "
        "FROM knowledge_fts f JOIN knowledge k ON f.rowid = k.id "
        f"WHERE knowledge_fts MATCH ? {rank_filter} {project_filter} "
        "ORDER BY (rank - 0.1 * k.hit_count - 0.3 * k.reinforcement_count - 2.0 * k.pinned "
        "  - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(k.created_at), 999)) / 30.0) "
        "LIMIT ?",
        fts_queries,
        rank_params + project_params,
        limit,
    )

    if not rows:
        # LIKE fallback — per-term OR
        like_project_filter = ""
        like_project_params: tuple = ()
        if project:
            like_project_filter = "AND (project = ? OR project IS NULL OR project = '')"
            like_project_params = (project,)

        rows = _like_search(
            db,
            "SELECT id, topic, kind, summary, detail, tags, pinned, "
            "  COALESCE((julianday('now') - julianday(created_at)), 0) AS age_days, "
            "  CASE WHEN last_hit_at IS NOT NULL "
            "    THEN (julianday('now') - julianday(last_hit_at)) ELSE NULL END AS last_hit_age "
            f"FROM knowledge WHERE ({{LIKE_WHERE}}) {like_project_filter} "
            "ORDER BY (hit_count + MAX(0, 30 - COALESCE(julianday('now') - julianday(created_at), 999)) / 30.0) DESC "
            "LIMIT ?",
            like_patterns,
            ["topic", "summary", "detail", "tags"],
            like_project_params,
            limit,
        )
        for r in rows:
            stale = _staleness_tag(r["age_days"], r["last_hit_age"], r["pinned"])
            pinned = _pinned_tag(r["pinned"])
            entry = f"[KNOWLEDGE/{r['kind'].upper()}]{stale}{pinned} {r['topic']}: {r['summary']}"
            if r["detail"]:
                entry += f"\n  Detail: {r['detail']}"
            results.append(entry)
    else:
        for r in rows:
            db.execute_write(
                "UPDATE knowledge SET hit_count = hit_count + 1, "
                "last_hit_at = datetime('now') WHERE id = ?",
                (r["id"],),
            )
            stale = _staleness_tag(r["age_days"], r["last_hit_age"], r["pinned"])
            pinned = _pinned_tag(r["pinned"])
            entry = f"[KNOWLEDGE/{r['kind'].upper()}]{stale}{pinned} {r['topic']}: {r['summary']}"
            if r["detail"]:
                entry += f"\n  Detail: {r['detail']}"
            if r["tags"]:
                entry += f"\n  Tags: {r['tags']}"
            results.append(entry)
        db.commit()

    # Negative knowledge FTS
    neg_project_filter = ""
    neg_project_params: tuple = ()
    if project:
        neg_project_filter = "AND (n.project = ? OR n.project IS NULL OR n.project = '')"
        neg_project_params = (project,)

    rows = _fts_match(
        db,
        "SELECT n.id, n.category, n.what_failed, n.why_failed, n.correct_approach, "
        "  n.pinned, rank AS fts_rank "
        "FROM negative_fts f JOIN negative_knowledge n ON f.rowid = n.id "
        f"WHERE negative_fts MATCH ? {rank_filter} {neg_project_filter} "
        "ORDER BY (rank - 2.0 * n.pinned) LIMIT ?",
        fts_queries,
        rank_params + neg_project_params,
        limit,
    )

    if not rows:
        like_neg_project_filter = ""
        like_neg_project_params: tuple = ()
        if project:
            like_neg_project_filter = "AND (project = ? OR project IS NULL OR project = '')"
            like_neg_project_params = (project,)

        rows = _like_search(
            db,
            "SELECT category, what_failed, why_failed, correct_approach, pinned "
            f"FROM negative_knowledge WHERE ({{LIKE_WHERE}}) {like_neg_project_filter} "
            "LIMIT ?",
            like_patterns,
            ["category", "what_failed", "why_failed"],
            like_neg_project_params,
            limit,
        )

    for r in rows:
        pinned = _pinned_tag(r["pinned"])
        entry = f"[NEGATIVE]{pinned} {r['category']}: {r['what_failed']}"
        if r["why_failed"]:
            entry += f"\n  Why: {r['why_failed']}"
        if r["correct_approach"]:
            entry += f"\n  Fix: {r['correct_approach']}"
        results.append(entry)

    # Errors FTS
    err_project_filter = ""
    err_project_params: tuple = ()
    if project:
        err_project_filter = "AND (e.project = ? OR e.project IS NULL OR e.project = '')"
        err_project_params = (project,)

    rows = _fts_match(
        db,
        "SELECT e.id, e.pattern, e.context, e.root_cause, e.fix, e.pinned, "
        "  rank AS fts_rank "
        "FROM errors_fts f JOIN errors e ON f.rowid = e.id "
        f"WHERE errors_fts MATCH ? {rank_filter} {err_project_filter} "
        "ORDER BY (rank - 2.0 * e.pinned) LIMIT ?",
        fts_queries,
        rank_params + err_project_params,
        limit,
    )

    if not rows:
        like_err_project_filter = ""
        like_err_project_params: tuple = ()
        if project:
            like_err_project_filter = "AND (project = ? OR project IS NULL OR project = '')"
            like_err_project_params = (project,)

        rows = _like_search(
            db,
            "SELECT pattern, context, root_cause, fix, pinned FROM errors "
            f"WHERE ({{LIKE_WHERE}}) {like_err_project_filter} "
            "LIMIT ?",
            like_patterns,
            ["pattern", "context", "root_cause"],
            like_err_project_params,
            limit,
        )

    for r in rows:
        pinned = _pinned_tag(r["pinned"])
        entry = f"[ERROR]{pinned} {r['pattern']}"
        if r["root_cause"]:
            entry += f"\n  Root cause: {r['root_cause']}"
        if r["fix"]:
            entry += f"\n  Fix: {r['fix']}"
        results.append(entry)

    # Rules FTS — composite ranking with quality gate (rules have no project column)
    rows = _fts_match(
        db,
        "SELECT r.id, r.title, r.keywords, r.description, r.category, r.file_path, "
        "  r.hit_count, r.reinforcement_count, r.pinned, rank AS fts_rank, "
        "  COALESCE((julianday('now') - julianday(r.created_at)), 0) AS age_days, "
        "  CASE WHEN r.last_hit_at IS NOT NULL "
        "    THEN (julianday('now') - julianday(r.last_hit_at)) ELSE NULL END AS last_hit_age "
        "FROM rules_fts f JOIN rules r ON f.rowid = r.id "
        f"WHERE rules_fts MATCH ? {rank_filter} "
        "ORDER BY (rank - 0.1 * r.hit_count - 0.3 * r.reinforcement_count - 2.0 * r.pinned "
        "  - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(r.created_at), 999)) / 30.0) "
        "LIMIT ?",
        fts_queries,
        rank_params,
        limit,
    )

    if not rows:
        rows = _like_search(
            db,
            "SELECT id, title, keywords, description, category, file_path, pinned, "
            "  COALESCE((julianday('now') - julianday(created_at)), 0) AS age_days, "
            "  CASE WHEN last_hit_at IS NOT NULL "
            "    THEN (julianday('now') - julianday(last_hit_at)) ELSE NULL END AS last_hit_age "
            "FROM rules WHERE ({LIKE_WHERE}) "
            "ORDER BY (hit_count + MAX(0, 30 - COALESCE(julianday('now') - julianday(created_at), 999)) / 30.0) DESC "
            "LIMIT ?",
            like_patterns,
            ["title", "keywords", "description", "category"],
            (),
            limit,
        )
        for r in rows:
            stale = _staleness_tag(r["age_days"], r["last_hit_age"], r["pinned"])
            pinned = _pinned_tag(r["pinned"])
            entry = f"[RULE]{stale}{pinned} {r['title']}"
            if r["category"]:
                entry += f" ({r['category']})"
            if r["description"]:
                entry += f"\n  {r['description']}"
            if r["file_path"]:
                entry += f"\n  File: {r['file_path']}"
            results.append(entry)
    else:
        for r in rows:
            db.execute_write(
                "UPDATE rules SET hit_count = hit_count + 1, "
                "last_hit_at = datetime('now') WHERE id = ?",
                (r["id"],),
            )
            stale = _staleness_tag(r["age_days"], r["last_hit_age"], r["pinned"])
            pinned = _pinned_tag(r["pinned"])
            entry = f"[RULE]{stale}{pinned} {r['title']}"
            if r["category"]:
                entry += f" ({r['category']})"
            if r["description"]:
                entry += f"\n  {r['description']}"
            if r["file_path"]:
                entry += f"\n  File: {r['file_path']}"
            results.append(entry)
        db.commit()

    # Plugin search scopes
    for scope in plugin_search_scopes:
        try:
            scope_results = scope.search(db, query, sanitize_fts(query),
                                         f"%{query}%", limit)
            results.extend(scope_results)
        except Exception:
            pass

    return "\n\n".join(results) if results else ""


def register_search_tools(
    mcp: FastMCP,
    db: KnowledgeDB,
    tracker: SessionTracker,
    plugin_search_scopes: list,
    project_name: str = "",
) -> None:
    """Register the unified search tool.

    Args:
        project_name: Default project for scoping report_error auto-searches.
    """

    def search_all(query: str, limit: int = 10) -> str:
        """Internal search function used by report_error. Applies quality gate and project scope."""
        return _search_all_scopes(
            db, query, limit, plugin_search_scopes,
            min_rank=RANK_THRESHOLD, project=project_name,
        )

    @mcp.tool()
    def search(query: str, scope: str = "all", limit: int = 10, project: str = "") -> str:
        """Search across all knowledge, negative knowledge, errors, rules, and plugin data.

        Uses FTS5 full-text search with LIKE fallback. Results are ranked by
        a composite of text relevance, hit frequency, reinforcement, and recency.
        Weak matches (below the quality gate threshold) are filtered out.
        Entries older than 90 days without recent hits are tagged [STALE].
        Pinned items are tagged [PINNED] and never go stale.

        QUERY TIPS — use short keyword phrases, not natural language:
          Good: "convert SRPM"     Bad: "how does worker package conversion work"
          Good: "staging mtime"    Bad: "what causes the staging environment to be stale"
          Good: "dlmalloc link"    Bad: "why does linking fail with dlmalloc"

        Porter stemming matches inflected forms automatically (e.g., "convert"
        matches "conversion", "converting"). Multi-word queries try AND first,
        then OR, then prefix matching.

        Args:
            query: Search keywords (2-4 words ideal, avoid full sentences)
            scope: 'all', 'knowledge', 'negative', 'errors', or a plugin scope name
            limit: Max results per scope (default 10)
            project: Filter to this project (+ global items). Empty = no filter.
        """
        tracker.record_call("search", topic=query)

        # Explicit search: no quality gate (user asked for it)
        result = _search_all_scopes(
            db, query, limit, plugin_search_scopes, min_rank=0.0, project=project,
        )
        if not result:
            return _with_nudge(f"No results for '{query}'.", tracker, query)
        return _with_nudge(result, tracker, query)

    # Return the internal search function for report_error to use
    return search_all
