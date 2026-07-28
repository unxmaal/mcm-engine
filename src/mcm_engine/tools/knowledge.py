"""Knowledge management tools — add_knowledge, add_negative, report_error,
reinforce_knowledge, pin_item, unpin_item.

Rewired in MCM2-02 (Phase 0): all SQL goes through SqliteStorage /
SqliteCounters instead of db.execute directly. The tool functions remain
the same shape externally; only their internals changed.
"""
from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, EntityTypeLiteral, ErrorRow, KnowledgeRow, NegativeRow
from ..refs import dump_refs, validate_refs
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
        references: list[dict] | None = None,
    ) -> str:
        """Store a learning (finding, decision, or insight). Exact topic match
        updates the existing entry; a fuzzy match warns but still inserts.

        references: optional pointers to the source of truth instead of restating
        it in prose — a list of {type, target, note?} where type is one of
        file/symbol/test/url (e.g. {"type": "file", "target": "src/x.py:42"}).
        Omit to leave unchanged on update; pass [] to clear.
        """
        tracker.record_call("add_knowledge", topic=topic)
        tracker.record_store()
        refs_provided = references is not None
        try:
            validated_refs = validate_refs(references) if refs_provided else None
        except ValueError as e:
            return _with_nudge(f"add_knowledge rejected: {e}", tracker, topic)
        try:  # #37: storing knowledge cost tokens.
            storage.record_token_event(
                "spent", max(1, (len(summary) + len(detail or "")) // 4))
        except Exception:
            pass

        # Exact topic match — update instead of insert.
        existing = storage.find_knowledge_by_topic_kind(topic, kind)
        if existing is not None:
            update_fields = dict(
                summary=summary,
                detail=detail,
                tags=tags,
                rationale=rationale,
                alternatives=alternatives,
            )
            if refs_provided:
                update_fields["refs_json"] = dump_refs(validated_refs)
            storage.update_knowledge(existing.id, **update_fields)
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
            references=validated_refs,
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
        """Log an error and search all knowledge scopes for matching fixes in
        one call. Call this the moment you hit an error, before attempting a fix."""
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
    def pin_item(entry_type: EntityTypeLiteral, entry_id: int) -> str:
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
    def unpin_item(entry_type: EntityTypeLiteral, entry_id: int) -> str:
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
        """Hard-delete a stored claim by id and append a row to recall_log
        (postgres backend only). Returns NOT_FOUND if the claim doesn't exist,
        never a silent no-op; the recall_log row persists after deletion."""
        tracker.record_call("kb_recall")

        # recall_log (hard-delete + audit) is a postgres-only table; the SQLite
        # adapter has no equivalent. Detect by the adapter's self-reported
        # identity, NOT by poking storage._conn — under the connection pool
        # (issue #83) _conn only resolves inside a borrowed method or a
        # transaction() block and otherwise raises, which broke this tool
        # (issue #98).
        if getattr(storage.identity, "kind", None) != "postgres":
            return _with_nudge(
                "kb_recall requires the postgres storage backend.", tracker,
            )

        try:
            # Borrow ONE pooled connection for the whole SELECT/INSERT/DELETE.
            # transaction() binds it (so storage._conn resolves inside the
            # block), commits on clean exit, and rolls back on any exception.
            with storage.transaction():
                conn = storage._conn
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, topic FROM knowledge WHERE id = %s",
                        (claim_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
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
        except Exception as e:
            return _with_nudge(
                f"kb_recall failed: {type(e).__name__}: {e}", tracker,
            )

        return _with_nudge(
            f"Recalled claim #{claim_id} ('{topic}'). "
            f"recall_log row written for principal={principal!r}.",
            tracker,
        )
