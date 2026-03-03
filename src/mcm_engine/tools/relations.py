"""Relationship tools — link_knowledge, get_related."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB
from ..tracker import SessionTracker

VALID_TYPES = {"knowledge", "error", "rule", "negative"}
VALID_RELATIONS = {"fixes", "causes", "supersedes", "contradicts", "related"}


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _entry_label(db: KnowledgeDB, entry_type: str, entry_id: int) -> str:
    """Get a human-readable label for an entry."""
    if entry_type == "knowledge":
        row = db.execute("SELECT topic, summary FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        return f"[KNOWLEDGE] {row['topic']}: {row['summary'][:60]}" if row else f"[KNOWLEDGE #{entry_id}]"
    elif entry_type == "error":
        row = db.execute("SELECT pattern FROM errors WHERE id = ?", (entry_id,)).fetchone()
        return f"[ERROR] {row['pattern'][:80]}" if row else f"[ERROR #{entry_id}]"
    elif entry_type == "rule":
        row = db.execute("SELECT title FROM rules WHERE id = ?", (entry_id,)).fetchone()
        return f"[RULE] {row['title']}" if row else f"[RULE #{entry_id}]"
    elif entry_type == "negative":
        row = db.execute(
            "SELECT category, what_failed FROM negative_knowledge WHERE id = ?", (entry_id,)
        ).fetchone()
        return f"[NEGATIVE] {row['category']}: {row['what_failed'][:60]}" if row else f"[NEGATIVE #{entry_id}]"
    return f"[{entry_type.upper()} #{entry_id}]"


def _entry_exists(db: KnowledgeDB, entry_type: str, entry_id: int) -> bool:
    """Check if an entry exists."""
    table_map = {
        "knowledge": "knowledge",
        "error": "errors",
        "rule": "rules",
        "negative": "negative_knowledge",
    }
    table = table_map.get(entry_type)
    if not table:
        return False
    row = db.execute(f"SELECT id FROM {table} WHERE id = ?", (entry_id,)).fetchone()
    return row is not None


def register_relations_tools(
    mcp: FastMCP,
    db: KnowledgeDB,
    tracker: SessionTracker,
) -> None:
    """Register link_knowledge and get_related tools."""

    @mcp.tool()
    def link_knowledge(
        source_type: str,
        source_id: int,
        target_type: str,
        target_id: int,
        relation: str,
        note: str = "",
    ) -> str:
        """Create a typed relationship between two knowledge entries.

        Args:
            source_type: 'knowledge', 'error', 'rule', or 'negative'
            source_id: ID of the source entry
            target_type: 'knowledge', 'error', 'rule', or 'negative'
            target_id: ID of the target entry
            relation: 'fixes', 'causes', 'supersedes', 'contradicts', or 'related'
            note: Optional note explaining the relationship
        """
        tracker.record_call("link_knowledge", topic=f"{source_type}->{target_type}")

        if source_type not in VALID_TYPES:
            return _with_nudge(
                f"Invalid source_type '{source_type}'. Use: {', '.join(sorted(VALID_TYPES))}",
                tracker,
            )
        if target_type not in VALID_TYPES:
            return _with_nudge(
                f"Invalid target_type '{target_type}'. Use: {', '.join(sorted(VALID_TYPES))}",
                tracker,
            )
        if relation not in VALID_RELATIONS:
            return _with_nudge(
                f"Invalid relation '{relation}'. Use: {', '.join(sorted(VALID_RELATIONS))}",
                tracker,
            )

        if not _entry_exists(db, source_type, source_id):
            return _with_nudge(f"Source {source_type} #{source_id} not found.", tracker)
        if not _entry_exists(db, target_type, target_id):
            return _with_nudge(f"Target {target_type} #{target_id} not found.", tracker)

        try:
            db.execute_write(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_type, source_id, target_type, target_id, relation, note),
            )
            db.commit()
        except Exception:
            # UNIQUE constraint — link already exists
            return _with_nudge(
                f"Relationship already exists: {source_type} #{source_id} --[{relation}]--> {target_type} #{target_id}",
                tracker,
            )

        src_label = _entry_label(db, source_type, source_id)
        tgt_label = _entry_label(db, target_type, target_id)
        return _with_nudge(
            f"Linked: {src_label}\n  --[{relation}]--> {tgt_label}",
            tracker,
        )

    @mcp.tool()
    def get_related(
        entry_type: str,
        entry_id: int,
    ) -> str:
        """Get all relationships for a knowledge entry (both directions).

        Args:
            entry_type: 'knowledge', 'error', 'rule', or 'negative'
            entry_id: ID of the entry
        """
        tracker.record_call("get_related", topic=f"{entry_type}#{entry_id}")

        if entry_type not in VALID_TYPES:
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {', '.join(sorted(VALID_TYPES))}",
                tracker,
            )
        if not _entry_exists(db, entry_type, entry_id):
            return _with_nudge(f"{entry_type} #{entry_id} not found.", tracker)

        entry_label = _entry_label(db, entry_type, entry_id)
        parts = [entry_label]

        # Outgoing relations (this entry is the source)
        outgoing = db.execute(
            "SELECT target_type, target_id, relation, note FROM relations "
            "WHERE source_type = ? AND source_id = ?",
            (entry_type, entry_id),
        ).fetchall()

        # Incoming relations (this entry is the target)
        incoming = db.execute(
            "SELECT source_type, source_id, relation, note FROM relations "
            "WHERE target_type = ? AND target_id = ?",
            (entry_type, entry_id),
        ).fetchall()

        if not outgoing and not incoming:
            parts.append("\nNo relationships found.")
            return _with_nudge("\n".join(parts), tracker)

        if outgoing:
            parts.append("\nOutgoing:")
            for r in outgoing:
                label = _entry_label(db, r["target_type"], r["target_id"])
                line = f"  --[{r['relation']}]--> {label}"
                if r["note"]:
                    line += f"  ({r['note']})"
                parts.append(line)

        if incoming:
            parts.append("\nIncoming:")
            for r in incoming:
                label = _entry_label(db, r["source_type"], r["source_id"])
                line = f"  <--[{r['relation']}]-- {label}"
                if r["note"]:
                    line += f"  ({r['note']})"
                parts.append(line)

        return _with_nudge("\n".join(parts), tracker)
