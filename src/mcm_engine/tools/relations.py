"""Relationship tools — link_knowledge, unlink_knowledge, get_related.

Rewired in MCM2-02 (Phase 0): all SQL routes through SqliteStorage.
"""
from __future__ import annotations

from typing import Literal, get_args

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, EntityTypeLiteral, RelationRow, StorageBackend
from ..tracker import SessionTracker
from ..wiring import Context, coerce_context

VALID_TYPES = {e.value for e in EntityType}

# The sealed relation vocabulary. Declared as a Literal so it surfaces as an
# enum in the MCP tool schema (issue #49 — callers couldn't discover the allowed
# values), and VALID_RELATIONS is derived from it so the runtime check and the
# schema can never drift apart.
#
# "depends_on" (v3.11.0) is the causal-provenance verb for first-class decisions:
# a decision --[depends_on]--> the knowledge/error/rule/decision it rests on. It
# is the directed edge trace_chain walks to reconstruct a decision's ancestry.
RelationType = Literal[
    "causes", "contradicts", "fixes", "related", "supersedes", "depends_on",
]
VALID_RELATIONS = set(get_args(RelationType))

# trace_chain bounds — a hop ceiling and a total-nodes cap so a dense or cyclic
# graph can't produce runaway output. The cap is surfaced (not silent) when hit.
_MAX_TRACE_DEPTH = 20
_MAX_TRACE_NODES = 200


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _entry_label(storage: StorageBackend, entry_type: str, entry_id: int) -> str:
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
    ctx_or_db,
    tracker: SessionTracker,
) -> None:
    """Register link_knowledge and get_related tools.

    Accepts a Context or a raw KnowledgeDB for backward compat.
    """
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage

    @mcp.tool()
    def link_knowledge(
        source_type: EntityTypeLiteral,
        source_id: int,
        target_type: EntityTypeLiteral,
        target_id: int,
        relation: RelationType,
        note: str = "",
    ) -> str:
        """Create a typed relationship between two knowledge entries. Look up the
        numeric ids in `search` output; every result is tagged with its id."""
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
    def unlink_knowledge(
        source_type: EntityTypeLiteral,
        source_id: int,
        target_type: EntityTypeLiteral,
        target_id: int,
        relation: RelationType,
    ) -> str:
        """Remove one typed relationship — the inverse of `link_knowledge`, so a
        wrong link (a mis-guessed target id) can be retracted through a tool
        instead of raw SQL (issue #111). Keyed on the full
        (source, target, relation) tuple; idempotent (a no-op if it does not
        exist). Look up ids with `search` / `get_entry` first.
        """
        tracker.record_call("unlink_knowledge")

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

        deleted = storage.delete_relation(
            EntityType(source_type), source_id,
            EntityType(target_type), target_id, relation,
        )
        arrow = (f"{source_type} #{source_id} --[{relation}]--> "
                 f"{target_type} #{target_id}")
        if deleted:
            return _with_nudge(f"Unlinked: {arrow}", tracker)
        return _with_nudge(
            f"No such relationship (no-op): {arrow}", tracker,
        )

    @mcp.tool()
    def get_related(
        entry_type: EntityTypeLiteral,
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

    @mcp.tool()
    def trace_chain(
        entry_type: EntityTypeLiteral,
        entry_id: int,
        relation: str = "",
        direction: str = "outgoing",
        max_depth: int = 5,
    ) -> str:
        """Walk the relations graph from one entry up to `max_depth` hops and
        render the reachable chain as an indented tree. Where `get_related` shows
        one hop, this follows edges transitively — e.g. a decision's full
        `depends_on` ancestry, or a `supersedes` lineage.

        relation: restrict the walk to a single verb (one of causes / contradicts
        / fixes / related / supersedes / depends_on); "" (default) walks any verb.
        direction: "outgoing" follows source->target (default); "incoming" follows
        target->source; "both" walks either way.
        max_depth: hop limit (clamped to 1..20). Cycles are guarded and the total
        node count is capped (surfaced, not silent, when hit).
        """
        tracker.record_call("trace_chain", topic=f"{entry_type}#{entry_id}")

        if entry_type not in VALID_TYPES:
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {', '.join(sorted(VALID_TYPES))}",
                tracker,
            )
        if relation and relation not in VALID_RELATIONS:
            return _with_nudge(
                f"Invalid relation '{relation}'. Use: "
                f"{', '.join(sorted(VALID_RELATIONS))} (or '' for any).", tracker,
            )
        if direction not in ("outgoing", "incoming", "both"):
            return _with_nudge(
                f"Invalid direction '{direction}'. Use: outgoing, incoming, both.",
                tracker,
            )
        etype = EntityType(entry_type)
        if not storage.entry_exists(etype, entry_id):
            return _with_nudge(f"{entry_type} #{entry_id} not found.", tracker)

        depth_cap = max(1, min(int(max_depth), _MAX_TRACE_DEPTH))
        want_out = direction in ("outgoing", "both")
        want_in = direction in ("incoming", "both")

        seen = {(etype, entry_id)}
        lines = [_entry_label(storage, entry_type, entry_id)]
        queue: list[tuple[EntityType, int, int]] = [(etype, entry_id, 0)]
        truncated = False
        while queue and not truncated:
            cur_type, cur_id, d = queue.pop(0)
            if d >= depth_cap:
                continue
            edges = []  # (dir, relation, neighbor_type, neighbor_id, note)
            if want_out:
                for r in storage.list_outgoing_relations(cur_type, cur_id):
                    if not relation or r.relation == relation:
                        edges.append(("out", r.relation, r.target_type,
                                      r.target_id, r.note))
            if want_in:
                for r in storage.list_incoming_relations(cur_type, cur_id):
                    if not relation or r.relation == relation:
                        edges.append(("in", r.relation, r.source_type,
                                      r.source_id, r.note))
            for kind, rel, ntype, nid, note in edges:
                key = (ntype, nid)
                if key in seen:
                    continue
                seen.add(key)
                indent = "  " * (d + 1)
                arrow = f"--[{rel}]-->" if kind == "out" else f"<--[{rel}]--"
                line = f"{indent}{arrow} {_entry_label(storage, ntype.value, nid)}"
                if note:
                    line += f"  ({note})"
                lines.append(line)
                if len(seen) - 1 >= _MAX_TRACE_NODES:
                    truncated = True
                    break
                queue.append((ntype, nid, d + 1))

        if len(lines) == 1:
            lines.append("  (no relationships in that direction)")
        if truncated:
            lines.append(f"  ... truncated at {_MAX_TRACE_NODES} nodes.")
        return _with_nudge("\n".join(lines), tracker)
