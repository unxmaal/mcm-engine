"""Knowledge management tools — add_knowledge, add_negative, report_error,
reinforce_knowledge, pin_item, unpin_item.

Rewired in MCM2-02 (Phase 0): all SQL goes through SqliteStorage /
SqliteCounters instead of db.execute directly. The tool functions remain
the same shape externally; only their internals changed.
"""
from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from ..backends import (
    EntityType,
    EntityTypeLiteral,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RelationRow,
    parse_valid_until,
)
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
        source_classification: str = "",
        valid_until: str = "",
    ) -> str:
        """Store a learning (finding, decision, or insight). Exact topic match
        updates the existing entry; a fuzzy match warns but still inserts.

        references: optional pointers to the source of truth instead of restating
        it in prose — a list of {type, target, note?} where type is one of
        file/symbol/test/url (e.g. {"type": "file", "target": "src/x.py:42"}).
        Omit to leave unchanged on update; pass [] to clear.
        source_classification: optional data-classification label the source
        assigned (e.g. public/internal/confidential). Carried, not interpreted.
        valid_until: optional ISO date/datetime ("2026-12-31") after which this
        finding is treated as expired in search (soft [EXPIRED] tag + rank
        penalty). Omit for a durable fact that never expires.
        """
        tracker.record_call("add_knowledge", topic=topic)
        tracker.record_store()
        refs_provided = references is not None
        try:
            validated_refs = validate_refs(references) if refs_provided else None
        except ValueError as e:
            return _with_nudge(f"add_knowledge rejected: {e}", tracker, topic)
        try:
            valid_until_dt = parse_valid_until(valid_until)
        except ValueError:
            return _with_nudge(
                f"add_knowledge rejected: valid_until '{valid_until}' is not an "
                f"ISO date/datetime (e.g. 2026-12-31).", tracker, topic)
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
            if valid_until:
                update_fields["valid_until"] = valid_until_dt
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
            source_classification=source_classification or None,
            valid_until=valid_until_dt,
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
        source_classification: str = "",
    ) -> str:
        """Store what doesn't work — mistakes, anti-patterns, dead ends.

        source_classification: optional source-assigned data-classification
        label. Carried, not interpreted."""
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
            source_classification=source_classification or None,
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
        source_classification: str = "",
    ) -> str:
        """Log an error and search all knowledge scopes for matching fixes in
        one call. Call this the moment you hit an error, before attempting a fix.

        source_classification: optional source-assigned data-classification
        label. Carried, not interpreted."""
        tracker.record_call("report_error", topic=error_text[:50])
        tracker.record_store()

        storage.insert_error(ErrorRow(
            id=0,
            pattern=error_text,
            context=context or None,
            tags=tags or None,
            project=project or project_name,
            source_classification=source_classification or None,
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
    def supersede_knowledge(old_id: int, new_id: int, actor: str = "") -> str:
        """Soft-expire knowledge finding old_id in favor of new_id (issue #111):
        the knowledge-id-space analog of supersede_rule, so a wrong or outdated
        finding is retired first-class and auditable rather than corrected
        prose-only. old_id drops out of default search but stays inspectable
        (get_entry / include_archived); a `supersedes` relation new_id -> old_id
        is recorded so the correction shows up in get_related. Reversible with
        unsupersede_knowledge.

        Refuses a self-supersede (old==new) and superseding by an already-
        superseded finding (either would retire a finding with no live
        successor). Confirm both ids with get_entry first — knowledge and rule
        id spaces overlap, and this operates ONLY on knowledge.
        """
        tracker.record_call("supersede_knowledge")

        if old_id == new_id:
            return _with_nudge(
                f"Refused: a finding cannot supersede itself "
                f"(old_id == new_id == {old_id}).", tracker,
            )
        old = storage.find_by_id(EntityType.KNOWLEDGE, old_id)
        if old is None:
            return _with_nudge(
                f"NOT_FOUND: no knowledge with id={old_id}.", tracker,
            )
        new = storage.find_by_id(EntityType.KNOWLEDGE, new_id)
        if new is None:
            return _with_nudge(
                f"NOT_FOUND: no knowledge with id={new_id}.", tracker,
            )
        if getattr(new, "status", "active") == "superseded":
            return _with_nudge(
                f"Refused: knowledge #{new_id} is itself superseded, so "
                f"superseding by it would leave no live successor. Revive "
                f"#{new_id} with unsupersede_knowledge first.", tracker,
            )

        # Record the audit relation first (idempotent — None if it already
        # exists), then flip status. If the relation write fails, status is
        # left untouched (safe); a stray relation without the flip is
        # removable via unlink_knowledge.
        storage.insert_relation(RelationRow(
            id=0,
            source_type=EntityType.KNOWLEDGE, source_id=new_id,
            target_type=EntityType.KNOWLEDGE, target_id=old_id,
            relation="supersedes",
            note=(f"by {actor}" if actor else None),
        ))
        storage.supersede_knowledge(old_id, new_id)
        return _with_nudge(
            f"Superseded knowledge #{old_id} ('{old.topic}') "
            f"by #{new_id} ('{new.topic}').", tracker,
        )

    @mcp.tool()
    def unsupersede_knowledge(knowledge_id: int) -> str:
        """Revive a superseded knowledge finding (issue #111): status back to
        active, superseded_by cleared, and the `supersedes` audit relation
        removed. The inverse of supersede_knowledge and the recovery path for an
        accidental supersede. Only acts on a finding currently 'superseded'."""
        tracker.record_call("unsupersede_knowledge")

        row = storage.find_by_id(EntityType.KNOWLEDGE, knowledge_id)
        if row is None:
            return _with_nudge(
                f"NOT_FOUND: no knowledge with id={knowledge_id}.", tracker,
            )
        status = getattr(row, "status", "active")
        if status != "superseded":
            return _with_nudge(
                f"Knowledge #{knowledge_id} is not superseded (status={status}); "
                f"nothing to do.", tracker,
            )
        new_id = getattr(row, "superseded_by", None)
        storage.unsupersede_knowledge(knowledge_id)
        if new_id:
            storage.delete_relation(
                EntityType.KNOWLEDGE, new_id,
                EntityType.KNOWLEDGE, knowledge_id, "supersedes",
            )
        return _with_nudge(
            f"Unsuperseded knowledge #{knowledge_id} ('{row.topic}') "
            f"— back to active.", tracker,
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
