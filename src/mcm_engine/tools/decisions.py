"""Decision tools — record_decision.

First-class decisions (v3.11.0) ride the knowledge table (``kind='decision'``)
plus the relations graph rather than a new entity/table. A decision records
``scenario -> reasoning -> outcome`` and links --[depends_on]--> the
knowledge/error/rule/decision it rests on, so ``trace_chain`` can reconstruct its
causal ancestry. Discovery of past decisions is just ``search(scope="knowledge")``
(they carry ``kind='decision'``) — no separate find_similar tool.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, KnowledgeRow, RelationRow
from ..tracker import SessionTracker
from ..wiring import coerce_context

# A decision may depend on any real entity — same vocabulary EntityType exposes.
_VALID_REF_TYPES = {e.value for e in EntityType}


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _parse_ref(ref: str):
    """Parse a ``"type#id"`` reference (e.g. ``"knowledge#41"``) into
    ``(EntityType, int)``. Returns None on any malformed input so the caller can
    report and skip it rather than abort the whole decision."""
    if not isinstance(ref, str) or "#" not in ref:
        return None
    type_part, _, id_part = ref.partition("#")
    type_part = type_part.strip().lower()
    id_part = id_part.strip()
    if type_part not in _VALID_REF_TYPES or not id_part.isdigit():
        return None
    return EntityType(type_part), int(id_part)


def register_decisions_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str,
) -> None:
    """Register record_decision.

    Rides ``ctx.storage`` (knowledge + relations axes). Accepts a raw
    KnowledgeDB too for backward compat with older callers.
    """
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage

    @mcp.tool()
    def record_decision(
        topic: str,
        scenario: str,
        reasoning: str,
        outcome: str,
        confidence: float = 0.0,
        based_on: list[str] | None = None,
        supersedes_decision: int = 0,
        tags: str = "",
        project: str = "",
    ) -> str:
        """Record a first-class decision: what you decided (`outcome`), the
        situation that prompted it (`scenario`), and why (`reasoning`). Stored as
        a knowledge entry with kind='decision'.

        based_on: optional list of "type#id" refs to the evidence this decision
        rests on, e.g. ["knowledge#41", "error#7", "rule#12"]. Each becomes a
        decision --[depends_on]--> ref edge that `trace_chain` can walk. Look the
        ids up in `search` output (every result is tagged with its id). Malformed
        or missing refs are skipped and reported, not fatal.
        supersedes_decision: id of a prior decision this one replaces; the old
        decision is soft-expired (status='superseded') and a `supersedes` audit
        relation is recorded — identical to supersede_knowledge.
        confidence: 0.0–1.0, carried in the stored detail; not interpreted.

        Discover past decisions with `search(scope="knowledge")` (they carry
        kind='decision'); reconstruct a decision's ancestry with `trace_chain`.
        """
        tracker.record_call("record_decision", topic=topic)
        tracker.record_store()

        if not topic.strip() or not outcome.strip():
            return _with_nudge(
                "record_decision needs at least a topic and an outcome.", tracker,
            )

        # Validate the supersede target up front so we never create a decision
        # that points at a missing / already-superseded predecessor.
        old = None
        if supersedes_decision:
            old = storage.find_by_id(EntityType.KNOWLEDGE, supersedes_decision)
            if old is None:
                return _with_nudge(
                    f"NOT_FOUND: no knowledge/decision with id={supersedes_decision} "
                    f"to supersede.", tracker,
                )
            if getattr(old, "status", "active") == "superseded":
                return _with_nudge(
                    f"Refused: decision #{supersedes_decision} is already "
                    f"superseded; revive it with unsupersede_knowledge first.",
                    tracker,
                )

        detail = "\n\n".join(p for p in (
            f"**Scenario:** {scenario}" if scenario.strip() else "",
            f"**Reasoning:** {reasoning}" if reasoning.strip() else "",
            f"Confidence: {confidence:.2f}",
        ) if p)

        new_id = storage.insert_knowledge(KnowledgeRow(
            id=0,  # adapter assigns
            topic=topic,
            kind="decision",
            summary=outcome,
            detail=detail,
            tags=tags or None,
            project=project or project_name,
            rationale=reasoning or None,
        ))

        # depends_on edges to the evidence.
        links_made: list[str] = []
        skipped: list[str] = []
        for ref in (based_on or []):
            parsed = _parse_ref(ref)
            if parsed is None:
                skipped.append(f"{ref} (malformed; use 'type#id')")
                continue
            etype, tid = parsed
            if not storage.entry_exists(etype, tid):
                skipped.append(f"{ref} (not found)")
                continue
            rel_id = storage.insert_relation(RelationRow(
                id=0,
                source_type=EntityType.KNOWLEDGE, source_id=new_id,
                target_type=etype, target_id=tid,
                relation="depends_on",
            ))
            if rel_id is None:
                skipped.append(f"{ref} (link already exists)")
            else:
                links_made.append(f"{etype.value}#{tid}")

        # Supersede a prior decision — audit relation first, then flip status
        # (mirrors the supersede_knowledge tool: new --[supersedes]--> old).
        if old is not None:
            storage.insert_relation(RelationRow(
                id=0,
                source_type=EntityType.KNOWLEDGE, source_id=new_id,
                target_type=EntityType.KNOWLEDGE, target_id=supersedes_decision,
                relation="supersedes",
            ))
            storage.supersede_knowledge(supersedes_decision, new_id)

        parts = [f"Recorded decision #{new_id}: {topic} — {outcome}"]
        if links_made:
            parts.append(f"depends_on: {', '.join(links_made)}")
        if skipped:
            parts.append(f"skipped refs: {'; '.join(skipped)}")
        if old is not None:
            parts.append(
                f"supersedes decision #{supersedes_decision} ('{old.topic}')")
        return _with_nudge("\n".join(parts), tracker, topic)
