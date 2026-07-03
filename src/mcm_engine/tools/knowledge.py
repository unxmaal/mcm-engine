"""Knowledge management tools — add_knowledge, add_negative, report_error,
reinforce_knowledge, pin_item, unpin_item.

Rewired in MCM2-02 (Phase 0): all SQL goes through SqliteStorage /
SqliteCounters instead of db.execute directly. The tool functions remain
the same shape externally; only their internals changed.
"""
from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow
from ..tracker import SessionTracker
from ..wiring import Context, coerce_context


def _extract_keywords(error_text: str) -> list[str]:
    """Extract significant search keywords from error text."""
    noise = {
        "error", "warning", "undefined", "reference", "to", "in", "the", "a",
        "an", "for", "of", "from", "with", "not", "no", "is", "was", "at",
        "by", "on", "or", "and", "that", "this", "it", "be", "as", "are",
        "but", "if", "line", "file", "symbol", "function", "type",
    }
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", error_text)
    keywords: list[str] = []
    seen: set[str] = set()
    for w in words:
        wl = w.lower()
        if wl not in noise and wl not in seen and len(wl) > 2:
            keywords.append(wl)
            seen.add(wl)
            if len(keywords) >= 8:
                break
    return keywords


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def register_knowledge_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str,
    search_all_fn,
) -> None:
    """Register add_knowledge, add_negative, report_error,
    reinforce_knowledge, pin_item, unpin_item.

    Uses ``ctx.storage`` and ``ctx.counters`` so every adapter axis
    selected in ``backends:`` config is honored at runtime. Accepts a
    raw KnowledgeDB too for backward compat with older callers.
    """
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage
    counters = ctx.counters

    @mcp.tool()
    def add_knowledge(
        topic: str,
        summary: str,
        kind: str = "finding",
        detail: str = "",
        tags: str = "",
        rationale: str = "",
        alternatives: str = "",
        project: str = "",
    ) -> str:
        """Store a learning — finding, decision, or insight.

        Automatically detects duplicates: exact topic match updates the existing
        entry; fuzzy match warns but still inserts.
        """
        tracker.record_call("add_knowledge", topic=topic)
        tracker.record_store()
        try:  # #37: storing knowledge cost tokens.
            storage.record_token_event(
                "spent", max(1, (len(summary) + len(detail or "")) // 4))
        except Exception:
            pass

        # Exact topic match — update instead of insert.
        existing = storage.find_knowledge_by_topic_kind(topic, kind)
        if existing is not None:
            storage.update_knowledge(
                existing.id,
                summary=summary,
                detail=detail,
                tags=tags,
                rationale=rationale,
                alternatives=alternatives,
            )
            return _with_nudge(
                f"Updated existing {kind}: {topic} (was: {existing.summary[:80]})",
                tracker, topic,
            )

        # Fuzzy match — warn but still insert.
        warning = ""
        similar = storage.find_similar_knowledge(topic)
        if similar is not None:
            warning = (
                f"\n  Note: similar entry exists — "
                f"[{similar.topic}]: {(similar.summary or '')[:80]}"
            )

        storage.insert_knowledge(KnowledgeRow(
            id=0,  # adapter assigns
            topic=topic,
            kind=kind,
            summary=summary,
            detail=detail or None,
            tags=tags or None,
            project=project or project_name,
            rationale=rationale or None,
            alternatives=alternatives or None,
        ))
        msg = f"Stored {kind}: {topic} — {summary}"
        if warning:
            msg += warning
        return _with_nudge(msg, tracker, topic)

    @mcp.tool()
    def add_negative(
        category: str,
        what_failed: str,
        why_failed: str = "",
        correct_approach: str = "",
        severity: str = "normal",
        project: str = "",
    ) -> str:
        """Store what doesn't work — mistakes, anti-patterns, dead ends."""
        tracker.record_call("add_negative", topic=category)
        tracker.record_store()
        storage.insert_negative(NegativeRow(
            id=0,
            category=category,
            what_failed=what_failed,
            why_failed=why_failed or None,
            correct_approach=correct_approach or None,
            severity=severity,
            project=project or project_name,
        ))
        return _with_nudge(
            f"Stored negative knowledge: {category} — {what_failed}",
            tracker, category,
        )

    @mcp.tool()
    def report_error(
        error_text: str,
        context: str = "",
        tags: str = "",
        project: str = "",
    ) -> str:
        """Report an error AND automatically search for matching fixes.

        THE KILLER FEATURE: one tool call logs the error AND searches all
        knowledge scopes for matching fixes.
        """
        tracker.record_call("report_error", topic=error_text[:50])
        tracker.record_store()

        storage.insert_error(ErrorRow(
            id=0,
            pattern=error_text,
            context=context or None,
            tags=tags or None,
            project=project or project_name,
        ))

        parts = [f"Error logged: {error_text[:100]}"]

        keywords = _extract_keywords(error_text)
        if keywords:
            query = " ".join(keywords[:5])
            search_results = search_all_fn(query, limit=5)
            if search_results:
                parts.append("\n--- Matching knowledge ---")
                parts.append(search_results)
            else:
                parts.append("No matching knowledge found.")
        else:
            parts.append("Could not extract search keywords from error text.")

        return _with_nudge("\n".join(parts), tracker, error_text[:50])

    @mcp.tool()
    def reinforce_knowledge(entry_id: int) -> str:
        """Deliberately reinforce a knowledge entry — signals "still correct"."""
        tracker.record_call("reinforce_knowledge")
        row = storage.find_by_id(EntityType.KNOWLEDGE, entry_id)
        if row is None:
            return _with_nudge(f"Knowledge entry {entry_id} not found.", tracker)

        counters.increment(EntityType.KNOWLEDGE, entry_id, "reinforcement_count")
        counters.increment(EntityType.KNOWLEDGE, entry_id, "last_hit_at")

        snap = counters.get(EntityType.KNOWLEDGE, entry_id)
        count = snap.get("reinforcement_count", 0)
        return _with_nudge(
            f"Reinforced: {row.topic} (reinforcement_count={count})", tracker,
        )

    @mcp.tool()
    def pin_item(entry_type: str, entry_id: int) -> str:
        """Pin an item so it's always loaded and never goes stale."""
        tracker.record_call("pin_item")
        try:
            etype = EntityType(entry_type)
        except ValueError:
            valid = ", ".join(e.value for e in EntityType)
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {valid}", tracker,
            )
        if not storage.entry_exists(etype, entry_id):
            return _with_nudge(f"{entry_type} entry {entry_id} not found.", tracker)
        storage.set_pinned(etype, entry_id, True)
        return _with_nudge(f"Pinned {entry_type} #{entry_id}.", tracker)

    @mcp.tool()
    def unpin_item(entry_type: str, entry_id: int) -> str:
        """Unpin an item, restoring normal staleness behavior."""
        tracker.record_call("unpin_item")
        try:
            etype = EntityType(entry_type)
        except ValueError:
            valid = ", ".join(e.value for e in EntityType)
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {valid}", tracker,
            )
        if not storage.entry_exists(etype, entry_id):
            return _with_nudge(f"{entry_type} entry {entry_id} not found.", tracker)
        storage.set_pinned(etype, entry_id, False)
        return _with_nudge(f"Unpinned {entry_type} #{entry_id}.", tracker)

    @mcp.tool()
    def kb_recall(
        claim_id: int,
        reason: str = "",
        principal: str = "governance",
    ) -> str:
        """Hard-delete a stored claim and append to recall_log.

        LODESTONE additive. The plan-of-record (lodestone-lite-plan.md
        phase 4) frames recall as a single-store DELETE: the chart's
        deployment has one Postgres, so a row delete plus an
        append-only recall_log row is the whole story. No drill, no
        manifest registry; those are deferred to plan.md.

        Returns "NOT_FOUND" if the claim doesn't exist — never a
        silent no-op. The recall_log row persists even after the
        claim is gone.
        """
        tracker.record_call("kb_recall")

        # Only the postgres adapter has the recall_log table; soft-
        # archive (the SQLite adapter's primitive) is the fallback.
        conn = getattr(storage, "_conn", None)
        if conn is None or not hasattr(conn, "cursor"):
            return _with_nudge(
                "kb_recall requires the postgres storage backend.", tracker,
            )

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, topic FROM knowledge WHERE id = %s",
                    (claim_id,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return _with_nudge(
                        f"NOT_FOUND: no claim with id={claim_id}.", tracker,
                    )
                topic = row["topic"] if hasattr(row, "keys") else row[1]

                cur.execute(
                    "INSERT INTO recall_log (claim_id, principal, reason) "
                    "VALUES (%s, %s, %s)",
                    (claim_id, principal or "governance", reason or None),
                )
                cur.execute("DELETE FROM knowledge WHERE id = %s", (claim_id,))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return _with_nudge(
                f"kb_recall failed: {type(e).__name__}: {e}", tracker,
            )

        return _with_nudge(
            f"Recalled claim #{claim_id} ('{topic}'). "
            f"recall_log row written for principal={principal!r}.",
            tracker,
        )
