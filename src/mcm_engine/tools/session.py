"""Session management tools — session_start, session_handoff, session_summary."""
from __future__ import annotations

import json
import time

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB
from ..tracker import SessionTracker


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def register_session_tools(
    mcp: FastMCP,
    db: KnowledgeDB,
    tracker: SessionTracker,
    project_name: str,
    plugin_session_fns: list,
) -> None:
    """Register session_start, session_handoff, session_summary tools."""

    @mcp.tool()
    def session_start() -> str:
        """Initialize a session. Returns recent knowledge count, last handoff, and plugin context.

        Call this at the start of every session for orientation.
        """
        tracker.record_call("session_start")
        parts: list[str] = []

        # Recent knowledge count
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM knowledge WHERE created_at > datetime('now', '-7 days')"
        ).fetchone()
        parts.append(f"Recent knowledge (7d): {row['cnt']}")

        # Total counts
        for table, label in [
            ("knowledge", "Total knowledge"),
            ("negative_knowledge", "Negative knowledge"),
            ("errors", "Errors logged"),
        ]:
            row = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            parts.append(f"{label}: {row['cnt']}")

        # Rules count
        try:
            row = db.execute("SELECT COUNT(*) as cnt FROM rules").fetchone()
            parts.append(f"Rules indexed: {row['cnt']}")
        except Exception:
            pass  # rules table may not exist in older DBs

        # Relationships count
        try:
            row = db.execute("SELECT COUNT(*) as cnt FROM relations").fetchone()
            parts.append(f"Relationships: {row['cnt']}")
        except Exception:
            pass

        # Stale knowledge (>90 days old, no recent hit)
        try:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM knowledge "
                "WHERE julianday('now') - julianday(created_at) > 90 "
                "AND (last_hit_at IS NULL OR julianday('now') - julianday(last_hit_at) > 90)"
            ).fetchone()
            if row["cnt"] > 0:
                parts.append(f"Stale knowledge (>90d unreinforced): {row['cnt']}")
        except Exception:
            pass

        # Last handoff
        handoff = db.execute(
            "SELECT status, current_task, next_steps, blockers, created_at "
            "FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if handoff:
            parts.append(f"\n--- Last handoff ({handoff['created_at']}) ---")
            parts.append(f"Status: {handoff['status']}")
            if handoff["current_task"]:
                parts.append(f"Task: {handoff['current_task']}")
            if handoff["next_steps"]:
                parts.append(f"Next: {handoff['next_steps']}")
            if handoff["blockers"]:
                parts.append(f"Blockers: {handoff['blockers']}")
        else:
            parts.append("\nNo previous sessions found.")

        # Plugin session context
        for fn in plugin_session_fns:
            try:
                extra = fn(db)
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
        """Snapshot session state for the next session to pick up.

        Resets ALL behavioral counters. Call this before ending a session
        or when nudged to checkpoint.

        Args:
            status: One-line session status summary
            current_task: What's currently in progress
            next_steps: What the next session should do
            blockers: Any blockers or decisions needed
        """
        tracker.record_call("session_handoff")
        tracker.reset_all()

        # Count knowledge stored this session
        knowledge_count = db.execute(
            "SELECT COUNT(*) as cnt FROM knowledge WHERE created_at > datetime('now', '-1 day')"
        ).fetchone()["cnt"]

        context = json.dumps({
            "turn_count": tracker.turn_count,
            "session_duration_s": tracker.elapsed_seconds(),
            "knowledge_stored": knowledge_count,
        })

        db.execute_write(
            "INSERT INTO sessions "
            "(status, current_task, findings_summary, next_steps, blockers, context_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (status, current_task, "", next_steps, blockers, context),
        )
        db.commit()
        return "Session handoff recorded. Counters reset."

    @mcp.tool()
    def session_summary() -> str:
        """Current session statistics — interaction count, time, knowledge stored."""
        tracker.record_call("session_summary")

        elapsed = tracker.elapsed_seconds()
        minutes = elapsed // 60

        # Knowledge stored this session (approximate — last hour)
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM knowledge WHERE created_at > datetime('now', '-1 hour')"
        ).fetchone()

        parts = [
            f"Tool calls: {tracker.turn_count}",
            f"Session duration: {minutes}m {elapsed % 60}s",
            f"Calls since last store: {tracker.turn_count - tracker.last_store_turn}",
            f"Knowledge stored (recent): {row['cnt']}",
            f"Topics queried: {len(tracker.topic_freq)}",
        ]

        # Top topics
        if tracker.topic_freq:
            sorted_topics = sorted(
                tracker.topic_freq.items(), key=lambda x: x[1], reverse=True
            )[:5]
            topic_lines = [f"  {t}: {c}x" for t, c in sorted_topics]
            parts.append("Top topics:\n" + "\n".join(topic_lines))

        return _with_nudge("\n".join(parts), tracker)
