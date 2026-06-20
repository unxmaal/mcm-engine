"""Relationship tools — link_knowledge, get_related.

Rewired in MCM2-02 (Phase 0): all SQL routes through SqliteStorage.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..adapters.sqlite.storage import SqliteStorage
from ..backends import EntityType
from ..db import KnowledgeDB
from ..tracker import SessionTracker
from ..backends import RelationRow

VALID_TYPES = {e.value for e in EntityType}
VALID_RELATIONS = {"fixes", "causes", "supersedes", "contradicts", "related"}


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _entry_label(storage: SqliteStorage, entry_type: str, entry_id: int) -> str:
    """Get a human-readable label for an entry."""
    etype = EntityType(entry_type)
    row = storage.find_by_id(etype, entry_id)
    if row is None:
        return f"[{entry_type.upper()} #{entry_id}]"
    if etype is EntityType.KNOWLEDGE:
        return f"[KNOWLEDGE] {row.topic}: {(row.summary or '')[:60]}"
    if etype is EntityType.ERROR:
        return f"[ERROR] {(row.pattern or '')[:80]}"
    if etype is EntityType.RULE:
        return f"[RULE] {row.title}"
    if etype is EntityType.NEGATIVE:
        return f"[NEGATIVE] {row.category}: {(row.what_failed or '')[:60]}"
    return f"[{entry_type.upper()} #{entry_id}]"


def register_relations_tools(
    mcp: FastMCP,
    db: KnowledgeDB,
    tracker: SessionTracker,
) -> None:
    """Register link_knowledge and get_related tools."""
    storage = SqliteStorage(db=db)

    @mcp.tool()
    def link_knowledge(
        source_type: str,
        source_id: int,
        target_type: str,
        target_id: int,
        relation: str,
        note: str = "",
    ) -> str:
        """Create a typed relationship between two knowledge entries."""
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

        src_etype = EntityType(source_type)
        tgt_etype = EntityType(target_type)

        if not storage.entry_exists(src_etype, source_id):
            return _with_nudge(f"Source {source_type} #{source_id} not found.", tracker)
        if not storage.entry_exists(tgt_etype, target_id):
            return _with_nudge(f"Target {target_type} #{target_id} not found.", tracker)

        new_id = storage.insert_relation(RelationRow(
            id=0,
            source_type=src_etype, source_id=source_id,
            target_type=tgt_etype, target_id=target_id,
            relation=relation,
            note=note or None,
        ))
        if new_id is None:
            return _with_nudge(
                f"Relationship already exists: {source_type} #{source_id} "
                f"--[{relation}]--> {target_type} #{target_id}",
                tracker,
            )

        src_label = _entry_label(storage, source_type, source_id)
        tgt_label = _entry_label(storage, target_type, target_id)
        return _with_nudge(
            f"Linked: {src_label}\n  --[{relation}]--> {tgt_label}", tracker,
        )

    @mcp.tool()
    def get_related(
        entry_type: str,
        entry_id: int,
    ) -> str:
        """Get all relationships for a knowledge entry (both directions)."""
        tracker.record_call("get_related", topic=f"{entry_type}#{entry_id}")

        if entry_type not in VALID_TYPES:
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {', '.join(sorted(VALID_TYPES))}",
                tracker,
            )
        etype = EntityType(entry_type)
        if not storage.entry_exists(etype, entry_id):
            return _with_nudge(f"{entry_type} #{entry_id} not found.", tracker)

        entry_label = _entry_label(storage, entry_type, entry_id)
        parts = [entry_label]

        outgoing = storage.list_outgoing_relations(etype, entry_id)
        incoming = storage.list_incoming_relations(etype, entry_id)

        if not outgoing and not incoming:
            parts.append("\nNo relationships found.")
            return _with_nudge("\n".join(parts), tracker)

        if outgoing:
            parts.append("\nOutgoing:")
            for r in outgoing:
                label = _entry_label(storage, r.target_type.value, r.target_id)
                line = f"  --[{r.relation}]--> {label}"
                if r.note:
                    line += f"  ({r.note})"
                parts.append(line)

        if incoming:
            parts.append("\nIncoming:")
            for r in incoming:
                label = _entry_label(storage, r.source_type.value, r.source_id)
                line = f"  <--[{r.relation}]-- {label}"
                if r.note:
                    line += f"  ({r.note})"
                parts.append(line)

        return _with_nudge("\n".join(parts), tracker)
