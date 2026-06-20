"""Unified search tool — FTS5 across all knowledge scopes.

Rewired in MCM2-02 (Phase 0): the composite rank formula moved out of SQL
ORDER BY clauses into mcm_engine.scoring. SQL access goes through
SqliteSearch / SqliteStorage / SqliteCounters. Counter bumps (the inline
UPDATE-after-SELECT pattern) move to CounterStore.increment.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from mcp.server.fastmcp import FastMCP

from ..adapters.sqlite.counters import SqliteCounters
from ..adapters.sqlite.search import SqliteSearch
from ..adapters.sqlite.storage import SqliteStorage
from ..backends import EntityType, SearchHit
from ..db import KnowledgeDB
from ..scoring import compose_rank, compose_rank_pinned_only
from ..tracker import SessionTracker

# Quality gate: minimum normalized score (higher = better) for FTS results.
# The v1 SQL used `rank <= -1.0` (FTS5 rank is negative-better). After our
# higher-better normalization this becomes `score >= 1.0`.
RANK_THRESHOLD = 1.0

# Staleness window: same 90-day threshold as v1.
STALE_DAYS = 90


def _staleness_tag(age_days: float | None, last_hit_age_days: float | None, pinned: bool) -> str:
    if pinned or age_days is None:
        return ""
    if age_days < STALE_DAYS:
        return ""
    if last_hit_age_days is not None and last_hit_age_days < STALE_DAYS:
        return ""
    return " [STALE]"


def _pinned_tag(pinned: bool) -> str:
    return " [PINNED]" if pinned else ""


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _age_days(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    delta = datetime.now() - ts
    return delta.total_seconds() / 86400.0


def _project_match(row_project: str | None, requested: str) -> bool:
    """Replicate the OR-with-NULL/empty semantics from the v1 search SQL."""
    return (
        row_project == requested
        or row_project is None
        or row_project == ""
    )


def _score_and_format_knowledge(
    hit: SearchHit,
    storage: SqliteStorage,
    counters: SqliteCounters,
    project: str,
) -> tuple[float, str] | None:
    row = storage.find_by_id(EntityType.KNOWLEDGE, hit.entity_id)
    if row is None:
        return None
    if project and not _project_match(row.project, project):
        return None
    snap = counters.last_flushed_snapshot(EntityType.KNOWLEDGE, hit.entity_id)
    composite = compose_rank(
        raw_rank=hit.score,
        hit_count=snap.get("hit_count"),
        reinforcement_count=snap.get("reinforcement_count"),
        pinned=bool(snap.get("pinned")),
        age_days=_age_days(row.created_at),
    )
    age_d = _age_days(row.created_at)
    last_hit_d = _age_days(row.last_hit_at)
    stale = _staleness_tag(age_d, last_hit_d, hit.is_pinned)
    pinned = _pinned_tag(hit.is_pinned)
    entry = f"[KNOWLEDGE/{(row.kind or 'finding').upper()}]{stale}{pinned} {row.topic}: {row.summary}"
    if row.detail:
        entry += f"\n  Detail: {row.detail}"
    if row.tags:
        entry += f"\n  Tags: {row.tags}"
    return composite, entry


def _score_and_format_rule(
    hit: SearchHit,
    storage: SqliteStorage,
    counters: SqliteCounters,
) -> tuple[float, str] | None:
    row = storage.find_by_id(EntityType.RULE, hit.entity_id)
    if row is None:
        return None
    snap = counters.last_flushed_snapshot(EntityType.RULE, hit.entity_id)
    composite = compose_rank(
        raw_rank=hit.score,
        hit_count=snap.get("hit_count"),
        reinforcement_count=snap.get("reinforcement_count"),
        pinned=bool(snap.get("pinned")),
        age_days=_age_days(row.created_at),
    )
    age_d = _age_days(row.created_at)
    last_hit_d = _age_days(row.last_hit_at)
    stale = _staleness_tag(age_d, last_hit_d, hit.is_pinned)
    pinned = _pinned_tag(hit.is_pinned)
    entry = f"[RULE]{stale}{pinned} {row.title}"
    if row.category:
        entry += f" ({row.category})"
    if row.description:
        entry += f"\n  {row.description}"
    if row.file_path:
        entry += f"\n  File: {row.file_path}"
    return composite, entry


def _score_and_format_negative(
    hit: SearchHit,
    storage: SqliteStorage,
    project: str,
) -> tuple[float, str] | None:
    row = storage.find_by_id(EntityType.NEGATIVE, hit.entity_id)
    if row is None:
        return None
    if project and not _project_match(row.project, project):
        return None
    composite = compose_rank_pinned_only(raw_rank=hit.score, pinned=hit.is_pinned)
    pinned = _pinned_tag(hit.is_pinned)
    entry = f"[NEGATIVE]{pinned} {row.category}: {row.what_failed}"
    if row.why_failed:
        entry += f"\n  Why: {row.why_failed}"
    if row.correct_approach:
        entry += f"\n  Fix: {row.correct_approach}"
    return composite, entry


def _score_and_format_error(
    hit: SearchHit,
    storage: SqliteStorage,
    project: str,
) -> tuple[float, str] | None:
    row = storage.find_by_id(EntityType.ERROR, hit.entity_id)
    if row is None:
        return None
    if project and not _project_match(row.project, project):
        return None
    composite = compose_rank_pinned_only(raw_rank=hit.score, pinned=hit.is_pinned)
    pinned = _pinned_tag(hit.is_pinned)
    entry = f"[ERROR]{pinned} {row.pattern}"
    if row.root_cause:
        entry += f"\n  Root cause: {row.root_cause}"
    if row.fix:
        entry += f"\n  Fix: {row.fix}"
    return composite, entry


def _scope_block(
    etype: EntityType,
    search_backend: SqliteSearch,
    storage: SqliteStorage,
    counters: SqliteCounters,
    *,
    query: str,
    limit: int,
    project: str,
    min_rank: float,
    bump_counters: bool,
) -> list[str]:
    """Search one entity scope and return formatted result strings."""
    # Pull more than `limit` so the threshold filter still has options to
    # sort across. SqliteSearch already sorts by raw rank desc; the
    # composite re-sort may reorder a bit.
    raw_hits = search_backend.search(query, entity_types={etype}, limit=limit * 3)

    scored: list[tuple[float, str, int]] = []  # (composite, formatted, entity_id)
    for hit in raw_hits:
        if min_rank > 0 and hit.score < min_rank:
            continue
        if etype is EntityType.KNOWLEDGE:
            result = _score_and_format_knowledge(hit, storage, counters, project)
        elif etype is EntityType.RULE:
            result = _score_and_format_rule(hit, storage, counters)
        elif etype is EntityType.NEGATIVE:
            result = _score_and_format_negative(hit, storage, project)
        elif etype is EntityType.ERROR:
            result = _score_and_format_error(hit, storage, project)
        else:
            continue
        if result is None:
            continue
        composite, formatted = result
        scored.append((composite, formatted, hit.entity_id))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    if bump_counters and etype in (EntityType.KNOWLEDGE, EntityType.RULE):
        for _, _, eid in top:
            counters.increment(etype, eid, "hit_count")
            counters.increment(etype, eid, "last_hit_at")

    return [formatted for _, formatted, _ in top]


def _search_all_scopes(
    search_backend: SqliteSearch,
    storage: SqliteStorage,
    counters: SqliteCounters,
    query: str,
    limit: int,
    plugin_search_scopes: list,
    *,
    min_rank: float,
    project: str,
) -> str:
    results: list[str] = []

    # Apply gate only when min_rank > 0 (the explicit-search path passes 0
    # to disable).
    gate = min_rank if min_rank > 0 else 0.0

    # Knowledge (with counter bump).
    results.extend(_scope_block(
        EntityType.KNOWLEDGE, search_backend, storage, counters,
        query=query, limit=limit, project=project, min_rank=gate, bump_counters=True,
    ))

    # Negative.
    results.extend(_scope_block(
        EntityType.NEGATIVE, search_backend, storage, counters,
        query=query, limit=limit, project=project, min_rank=gate, bump_counters=False,
    ))

    # Errors.
    results.extend(_scope_block(
        EntityType.ERROR, search_backend, storage, counters,
        query=query, limit=limit, project=project, min_rank=gate, bump_counters=False,
    ))

    # Rules (with counter bump). Rules have no project column.
    results.extend(_scope_block(
        EntityType.RULE, search_backend, storage, counters,
        query=query, limit=limit, project="", min_rank=gate, bump_counters=True,
    ))

    # Plugin search scopes route through the SearchBackend (MCM2-07).
    for scope in plugin_search_scopes:
        try:
            scope_results = search_backend.search_plugin(scope, query, limit)
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
):
    """Register the unified search tool.

    Args:
        project_name: Default project for scoping report_error auto-searches.
    """
    storage = SqliteStorage(db=db)
    counters = SqliteCounters(db=db)
    search_backend = SqliteSearch(db=db)

    def search_all(query: str, limit: int = 10) -> str:
        """Internal search function used by report_error. Applies quality
        gate and project scope."""
        return _search_all_scopes(
            search_backend, storage, counters, query, limit,
            plugin_search_scopes,
            min_rank=RANK_THRESHOLD, project=project_name,
        )

    @mcp.tool()
    def search(query: str, scope: str = "all", limit: int = 10, project: str = "") -> str:
        """Search across all knowledge, negative knowledge, errors, rules,
        and plugin data.

        Uses FTS5 full-text search with LIKE fallback. Results are ranked by
        a composite of text relevance, hit frequency, reinforcement, and
        recency. Weak matches (below the quality gate threshold) are
        filtered out unless explicitly requested via the `search` tool.
        Entries older than 90 days without recent hits are tagged [STALE].
        Pinned items are tagged [PINNED] and never go stale.
        """
        tracker.record_call("search", topic=query)
        # Explicit search: no quality gate.
        result = _search_all_scopes(
            search_backend, storage, counters, query, limit,
            plugin_search_scopes,
            min_rank=0.0, project=project,
        )
        if not result:
            return _with_nudge(f"No results for '{query}'.", tracker, query)
        return _with_nudge(result, tracker, query)

    return search_all
