"""Session management tools — session_start, session_handoff, session_summary,
save_snapshot, get_resume_context.

Rewired in MCM2-02 (Phase 0): all SQL routes through SqliteStorage. Plugin
session callbacks still receive the raw db until MCM2-07 lands.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, SessionRow, SnapshotRow
from ..db import KnowledgeDB
from ..tracker import SessionTracker
from ..wiring import Context, coerce_context


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _handoff_suggestions(storage, tracker: SessionTracker) -> str:
    """Build the end-of-session "before you go" section: concrete candidates
    for the relationship-building tools that aggregate nudges never surface.

    Non-blocking by design — these are suggestions, not a gate. Every query
    is best-effort: a storage backend that doesn't implement the candidate
    methods simply yields no suggestions (honest-capabilities contract).
    """
    lines: list[str] = []

    try:
        unlinked = storage.list_unlinked_knowledge(limit=5)
    except Exception:
        unlinked = []
    if unlinked:
        lines.append(
            "Unlinked knowledge — connect related items with `link_knowledge`:"
        )
        for r in unlinked:
            lines.append(f"  #{r.id} [{r.kind}] {r.topic}")

    try:
        promotable = storage.list_promotable_knowledge(min_hits=5, limit=3)
    except Exception:
        promotable = []
    if promotable:
        lines.append(
            "Frequently-hit knowledge — consider `promote_to_rule`:"
        )
        for r in promotable:
            lines.append(f"  #{r.id} [{r.hit_count} hits] {r.topic}")

    if not lines:
        return ""
    lines.append(
        "Act on any that apply, or skip with 'nothing applicable' — these are "
        "suggestions, not blockers."
    )
    return "\n".join(lines)


_ENTITY_BY_NAME = {
    "knowledge": (EntityType.KNOWLEDGE, "Total knowledge"),
    "negative_knowledge": (EntityType.NEGATIVE, "Negative knowledge"),
    "errors": (EntityType.ERROR, "Errors logged"),
}

_PINNED_LABELS = [
    (EntityType.KNOWLEDGE, "knowledge"),
    (EntityType.NEGATIVE, "negative"),
    (EntityType.ERROR, "error"),
    (EntityType.RULE, "rule"),
]


def register_session_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str,
    plugin_session_fns: list,
    *,
    plugin_db: KnowledgeDB | None = None,
) -> None:
    """Register session_start, session_handoff, session_summary,
    save_snapshot, get_resume_context.

    Accepts a Context or a raw KnowledgeDB for backward compat. When a
    raw KnowledgeDB is passed (legacy callers, tests), it doubles as the
    ``plugin_db`` for the session-start callback path unless an explicit
    one was provided.
    """
    # When the legacy db is passed positionally and no plugin_db was
    # supplied, fall back to using the same db for plugin callbacks.
    if plugin_db is None and isinstance(ctx_or_db, KnowledgeDB):
        plugin_db = ctx_or_db
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage

    @mcp.tool()
    def session_start() -> str:
        """Initialize a session. Returns recent knowledge count, last handoff,
        pinned items, and plugin context."""
        tracker.record_call("session_start")
        parts: list[str] = []

        # Recent knowledge count (last 7 days).
        recent_7d = storage.count_recent_knowledge(since_days=7)
        parts.append(f"Recent knowledge (7d): {recent_7d}")

        # Per-table totals with optional project breakdown.
        for _label_key, (etype, label) in _ENTITY_BY_NAME.items():
            total = storage.count_by_type(etype)
            if project_name:
                proj_count = storage.count_by_type(etype, project=project_name)
                global_count = total - proj_count
                parts.append(
                    f"{label}: {total} (project={proj_count}, global={global_count})"
                )
            else:
                parts.append(f"{label}: {total}")

        # Rules — try is intentional: old DBs without the rules table fall through.
        try:
            parts.append(f"Rules indexed: {storage.count_by_type(EntityType.RULE)}")
        except Exception:
            pass

        # Relations + snapshots.
        try:
            parts.append(f"Relationships: {storage.count_relations()}")
        except Exception:
            pass
        try:
            parts.append(f"Snapshots: {storage.count_snapshots()}")
        except Exception:
            pass

        # Stale knowledge (>90 days, no recent hit, not pinned).
        try:
            stale = storage.count_stale_knowledge()
            if stale > 0:
                parts.append(f"Stale knowledge (>90d unreinforced): {stale}")
        except Exception:
            pass

        # Pinned items section.
        try:
            pinned_parts: list[str] = []
            for etype, label in _PINNED_LABELS:
                rows = storage.list_pinned(etype)
                if rows:
                    pinned_parts.append(f"  {label}: {len(rows)}")
            if pinned_parts:
                parts.append("Pinned items:\n" + "\n".join(pinned_parts))
        except Exception:
            pass

        # Last handoff (most recent session row).
        handoff = storage.get_last_session()
        if handoff is not None:
            parts.append(f"\n--- Last handoff ({handoff.created_at}) ---")
            parts.append(f"Status: {handoff.status}")
            if handoff.current_task:
                parts.append(f"Task: {handoff.current_task}")
            if handoff.next_steps:
                parts.append(f"Next: {handoff.next_steps}")
            if handoff.blockers:
                parts.append(f"Blockers: {handoff.blockers}")
        else:
            parts.append("\nNo previous sessions found.")

        # Plugin session context — plugins still get raw SQL on their
        # own tables, so we pass the SQLite db handle (only available
        # under the embedded SQLite layout). Plugin layer is documented
        # as embedded-only; with a non-embedded storage, plugin_db is
        # None and we skip these callbacks.
        if plugin_db is not None:
            for fn in plugin_session_fns:
                try:
                    extra = fn(plugin_db)
                    if extra:
                        for key, value in extra.items():
                            parts.append(f"{key}: {value}")
                except Exception:
                    pass

        parts.append(f"\nProject: {project_name}")
        return "\n".join(parts)

    @mcp.tool()
    def session_handoff(
        status: str,
        current_task: str = "",
        next_steps: str = "",
        blockers: str = "",
    ) -> str:
        """Snapshot session state for the next session to pick up."""
        tracker.record_call("session_handoff")
        # Compute suggestions BEFORE reset_all clears session tracking.
        suggestions = _handoff_suggestions(storage, tracker)
        tracker.reset_all()

        knowledge_count = storage.count_recent_knowledge(since_days=1)
        context = json.dumps({
            "turn_count": tracker.turn_count,
            "session_duration_s": tracker.elapsed_seconds(),
            "knowledge_stored": knowledge_count,
        })

        session_id = storage.insert_session(SessionRow(
            id=0,
            status=status,
            current_task=current_task or None,
            findings_summary="",
            next_steps=next_steps or None,
            blockers=blockers or None,
            context_snapshot=context,
        ))

        # Auto-snapshot with the handoff data.
        try:
            seq = storage.next_snapshot_seq(session_id)
            storage.insert_snapshot(SnapshotRow(
                id=0,
                session_id=session_id,
                sequence_num=seq,
                goal=current_task or None,
                progress=status,
                next_steps=next_steps or None,
                blockers=blockers or None,
            ))
        except Exception:
            # Older DBs may not have the snapshots table — non-fatal.
            pass

        msg = "Session handoff recorded. Counters reset."
        if suggestions:
            msg += "\n\n--- Before you go ---\n" + suggestions
        return msg

    @mcp.tool()
    def session_summary() -> str:
        """Current session statistics — interaction count, time,
        knowledge stored."""
        tracker.record_call("session_summary")

        elapsed = tracker.elapsed_seconds()
        minutes = elapsed // 60

        recent = storage.count_recent_knowledge(since_days=1.0 / 24.0)  # last hour

        parts = [
            f"Tool calls: {tracker.turn_count}",
            f"Session duration: {minutes}m {elapsed % 60}s",
            f"Calls since last store: {tracker.turn_count - tracker.last_store_turn}",
            f"Knowledge stored (recent): {recent}",
            f"Topics queried: {len(tracker.topic_freq)}",
        ]

        if tracker.topic_freq:
            sorted_topics = sorted(
                tracker.topic_freq.items(), key=lambda x: x[1], reverse=True,
            )[:5]
            topic_lines = [f"  {t}: {c}x" for t, c in sorted_topics]
            parts.append("Top topics:\n" + "\n".join(topic_lines))

        return _with_nudge("\n".join(parts), tracker)

    @mcp.tool()
    def save_snapshot(
        goal: str = "",
        progress: str = "",
        open_questions: str = "",
        blockers: str = "",
        next_steps: str = "",
        active_files: str = "",
        key_decisions: str = "",
    ) -> str:
        """Save a numbered mid-session checkpoint."""
        tracker.record_call("save_snapshot")
        tracker.last_checkpoint_turn = tracker.turn_count

        last_session = storage.get_last_session()
        session_id = last_session.id if last_session else None
        seq = storage.next_snapshot_seq(session_id)

        storage.insert_snapshot(SnapshotRow(
            id=0,
            session_id=session_id,
            sequence_num=seq,
            goal=goal or None,
            progress=progress or None,
            open_questions=open_questions or None,
            blockers=blockers or None,
            next_steps=next_steps or None,
            active_files=active_files or None,
            key_decisions=key_decisions or None,
        ))
        return _with_nudge(
            f"Snapshot #{seq} saved (session={session_id}).", tracker,
        )

    @mcp.tool()
    def get_resume_context() -> str:
        """Get structured context for resuming work."""
        tracker.record_call("get_resume_context")
        parts: list[str] = []

        # Last session.
        session = storage.get_last_session()
        if session is not None:
            parts.append("--- Last Session ---")
            parts.append(f"Status: {session.status}")
            if session.current_task:
                parts.append(f"Task: {session.current_task}")
            if session.next_steps:
                parts.append(f"Next: {session.next_steps}")
            if session.blockers:
                parts.append(f"Blockers: {session.blockers}")
            parts.append(f"Time: {session.created_at}")

        # Last snapshot.
        try:
            snap = storage.get_last_snapshot()
            if snap is not None:
                parts.append(f"\n--- Last Snapshot (#{snap.sequence_num}) ---")
                if snap.goal:
                    parts.append(f"Goal: {snap.goal}")
                if snap.progress:
                    parts.append(f"Progress: {snap.progress}")
                if snap.open_questions:
                    parts.append(f"Open questions: {snap.open_questions}")
                if snap.blockers:
                    parts.append(f"Blockers: {snap.blockers}")
                if snap.next_steps:
                    parts.append(f"Next: {snap.next_steps}")
                if snap.active_files:
                    parts.append(f"Active files: {snap.active_files}")
                if snap.key_decisions:
                    parts.append(f"Key decisions: {snap.key_decisions}")
        except Exception:
            pass

        # Pinned items per entity type.
        try:
            pinned_k = storage.list_pinned(EntityType.KNOWLEDGE)
            if pinned_k:
                parts.append("\n--- Pinned Knowledge ---")
                for r in pinned_k:
                    parts.append(f"[{r.kind.upper()}] {r.topic}: {r.summary}")
        except Exception:
            pass

        try:
            pinned_n = storage.list_pinned(EntityType.NEGATIVE)
            if pinned_n:
                parts.append("\n--- Pinned Negative Knowledge ---")
                for r in pinned_n:
                    parts.append(f"{r.category}: {r.what_failed}")
        except Exception:
            pass

        try:
            pinned_e = storage.list_pinned(EntityType.ERROR)
            if pinned_e:
                parts.append("\n--- Pinned Errors ---")
                for r in pinned_e:
                    entry = r.pattern
                    if r.fix:
                        entry += f" -> {r.fix}"
                    parts.append(entry)
        except Exception:
            pass

        try:
            pinned_r = storage.list_pinned(EntityType.RULE)
            if pinned_r:
                parts.append("\n--- Pinned Rules ---")
                for r in pinned_r:
                    entry = r.title
                    if r.file_path:
                        entry += f" ({r.file_path})"
                    parts.append(entry)
        except Exception:
            pass

        parts.append(f"\nProject: {project_name}")

        if not parts:
            return _with_nudge("No resume context available.", tracker)
        return _with_nudge("\n".join(parts), tracker)
