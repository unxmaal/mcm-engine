"""Unified search tool — FTS5 across all knowledge scopes."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB, sanitize_fts
from ..tracker import SessionTracker


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _search_all_scopes(
    db: KnowledgeDB,
    query: str,
    limit: int,
    plugin_search_scopes: list,
) -> str:
    """Search all scopes and return formatted results. Used by both search tool and report_error."""
    results: list[str] = []
    fts_query = sanitize_fts(query)
    like_pattern = f"%{query}%"

    # Knowledge FTS — composite ranking: FTS5 relevance + hit_count boost + recency boost
    # rank is negative (more negative = better FTS match)
    # Composite: rank (FTS relevance) - 0.1*hit_count - recency_days_bonus
    #   where recency_days_bonus = max(0, 30 - days_since_creation) / 30
    # This promotes frequently-hit and recent entries.
    try:
        rows = db.execute(
            "SELECT k.id, k.topic, k.kind, k.summary, k.detail, k.tags, k.hit_count, "
            "  rank AS fts_rank, "
            "  COALESCE((julianday('now') - julianday(k.created_at)), 999) AS age_days "
            "FROM knowledge_fts f JOIN knowledge k ON f.rowid = k.id "
            "WHERE knowledge_fts MATCH ? "
            "ORDER BY (rank - 0.1 * k.hit_count - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(k.created_at), 999)) / 30.0) "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        for r in rows:
            db.execute_write(
                "UPDATE knowledge SET hit_count = hit_count + 1, "
                "last_hit_at = datetime('now') WHERE id = ?",
                (r["id"],),
            )
            entry = f"[KNOWLEDGE/{r['kind'].upper()}] {r['topic']}: {r['summary']}"
            if r["detail"]:
                entry += f"\n  Detail: {r['detail']}"
            if r["tags"]:
                entry += f"\n  Tags: {r['tags']}"
            results.append(entry)
        if rows:
            db.commit()
    except Exception:
        # FTS5 failed, try LIKE fallback — rank by hit_count + recency
        rows = db.execute(
            "SELECT id, topic, kind, summary, detail, tags FROM knowledge "
            "WHERE topic LIKE ? OR summary LIKE ? OR detail LIKE ? OR tags LIKE ? "
            "ORDER BY (hit_count + MAX(0, 30 - COALESCE(julianday('now') - julianday(created_at), 999)) / 30.0) DESC "
            "LIMIT ?",
            (like_pattern, like_pattern, like_pattern, like_pattern, limit),
        ).fetchall()
        for r in rows:
            entry = f"[KNOWLEDGE/{r['kind'].upper()}] {r['topic']}: {r['summary']}"
            if r["detail"]:
                entry += f"\n  Detail: {r['detail']}"
            results.append(entry)

    # Negative knowledge FTS
    try:
        rows = db.execute(
            "SELECT n.category, n.what_failed, n.why_failed, n.correct_approach "
            "FROM negative_fts f JOIN negative_knowledge n ON f.rowid = n.id "
            "WHERE negative_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        for r in rows:
            entry = f"[NEGATIVE] {r['category']}: {r['what_failed']}"
            if r["why_failed"]:
                entry += f"\n  Why: {r['why_failed']}"
            if r["correct_approach"]:
                entry += f"\n  Fix: {r['correct_approach']}"
            results.append(entry)
    except Exception:
        rows = db.execute(
            "SELECT category, what_failed, why_failed, correct_approach FROM negative_knowledge "
            "WHERE category LIKE ? OR what_failed LIKE ? OR why_failed LIKE ? LIMIT ?",
            (like_pattern, like_pattern, like_pattern, limit),
        ).fetchall()
        for r in rows:
            entry = f"[NEGATIVE] {r['category']}: {r['what_failed']}"
            if r["why_failed"]:
                entry += f"\n  Why: {r['why_failed']}"
            results.append(entry)

    # Errors FTS
    try:
        rows = db.execute(
            "SELECT e.pattern, e.context, e.root_cause, e.fix "
            "FROM errors_fts f JOIN errors e ON f.rowid = e.id "
            "WHERE errors_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        for r in rows:
            entry = f"[ERROR] {r['pattern']}"
            if r["root_cause"]:
                entry += f"\n  Root cause: {r['root_cause']}"
            if r["fix"]:
                entry += f"\n  Fix: {r['fix']}"
            results.append(entry)
    except Exception:
        rows = db.execute(
            "SELECT pattern, context, root_cause, fix FROM errors "
            "WHERE pattern LIKE ? OR context LIKE ? OR root_cause LIKE ? LIMIT ?",
            (like_pattern, like_pattern, like_pattern, limit),
        ).fetchall()
        for r in rows:
            entry = f"[ERROR] {r['pattern']}"
            if r["root_cause"]:
                entry += f"\n  Root cause: {r['root_cause']}"
            results.append(entry)

    # Rules FTS — composite ranking same as knowledge
    try:
        rows = db.execute(
            "SELECT r.id, r.title, r.keywords, r.description, r.category, r.file_path, r.hit_count "
            "FROM rules_fts f JOIN rules r ON f.rowid = r.id "
            "WHERE rules_fts MATCH ? "
            "ORDER BY (rank - 0.1 * r.hit_count - MAX(0, 30.0 - COALESCE(julianday('now') - julianday(r.created_at), 999)) / 30.0) "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        for r in rows:
            db.execute_write(
                "UPDATE rules SET hit_count = hit_count + 1, "
                "last_hit_at = datetime('now') WHERE id = ?",
                (r["id"],),
            )
            entry = f"[RULE] {r['title']}"
            if r["category"]:
                entry += f" ({r['category']})"
            if r["description"]:
                entry += f"\n  {r['description']}"
            if r["file_path"]:
                entry += f"\n  File: {r['file_path']}"
            results.append(entry)
        if rows:
            db.commit()
    except Exception:
        rows = db.execute(
            "SELECT id, title, keywords, description, category, file_path FROM rules "
            "WHERE title LIKE ? OR keywords LIKE ? OR description LIKE ? OR category LIKE ? "
            "ORDER BY (hit_count + MAX(0, 30 - COALESCE(julianday('now') - julianday(created_at), 999)) / 30.0) DESC "
            "LIMIT ?",
            (like_pattern, like_pattern, like_pattern, like_pattern, limit),
        ).fetchall()
        for r in rows:
            entry = f"[RULE] {r['title']}"
            if r["category"]:
                entry += f" ({r['category']})"
            if r["description"]:
                entry += f"\n  {r['description']}"
            if r["file_path"]:
                entry += f"\n  File: {r['file_path']}"
            results.append(entry)

    # Plugin search scopes
    for scope in plugin_search_scopes:
        try:
            scope_results = scope.search(db, query, fts_query, like_pattern, limit)
            results.extend(scope_results)
        except Exception:
            pass

    return "\n\n".join(results) if results else ""


def register_search_tools(
    mcp: FastMCP,
    db: KnowledgeDB,
    tracker: SessionTracker,
    plugin_search_scopes: list,
) -> None:
    """Register the unified search tool."""

    def search_all(query: str, limit: int = 10) -> str:
        """Internal search function used by report_error."""
        return _search_all_scopes(db, query, limit, plugin_search_scopes)

    @mcp.tool()
    def search(query: str, scope: str = "all", limit: int = 10) -> str:
        """Search across all knowledge, negative knowledge, errors, and plugin data.

        Uses FTS5 full-text search with LIKE fallback.

        Args:
            query: Search terms
            scope: 'all', 'knowledge', 'negative', 'errors', or a plugin scope name
            limit: Max results per scope (default 10)
        """
        tracker.record_call("search", topic=query)

        result = _search_all_scopes(db, query, limit, plugin_search_scopes)
        if not result:
            return _with_nudge(f"No results for '{query}'.", tracker, query)
        return _with_nudge(result, tracker, query)

    # Return the internal search function for report_error to use
    return search_all
