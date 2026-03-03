"""Session management tools — session_start, session_handoff, session_summary, save_snapshot, get_resume_context."""
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
    """Register session_start, session_handoff, session_summary, save_snapshot, get_resume_context tools."""

    @mcp.tool()
    def session_start() -> str:
        """Initialize a session. Returns recent knowledge count, last handoff, pinned items, and plugin context.

        Call this at the start of every session for orientation.
        """
        tracker.record_call("session_start")
        parts: list[str] = []

        # Recent knowledge count
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM knowledge WHERE created_at > datetime('now', '-7 days')"
        ).fetchone()
        parts.append(f"Recent knowledge (7d): {row['cnt']}")

        # Total counts with project breakdown
        for table, label in [
            ("knowledge", "Total knowledge"),
            ("negative_knowledge", "Negative knowledge"),
            ("errors", "Errors logged"),
        ]:
            total = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()["cnt"]
            if project_name:
                proj_count = db.execute(
                    f"SELECT COUNT(*) as cnt FROM {table} WHERE project = ?",
                    (project_name,),
                ).fetchone()["cnt"]
                global_count = total - proj_count
                parts.append(f"{label}: {total} (project={proj_count}, global={global_count})")
            else:
                parts.append(f"{label}: {total}")

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

        # Snapshots count
        try:
            row = db.execute("SELECT COUNT(*) as cnt FROM snapshots").fetchone()
            parts.append(f"Snapshots: {row['cnt']}")
        except Exception:
            pass

        # Stale knowledge (>90 days old, no recent hit, NOT pinned)
        try:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM knowledge "
                "WHERE julianday('now') - julianday(created_at) > 90 "
                "AND (last_hit_at IS NULL OR julianday('now') - julianday(last_hit_at) > 90) "
                "AND pinned = 0"
            ).fetchone()
            if row["cnt"] > 0:
                parts.append(f"Stale knowledge (>90d unreinforced): {row['cnt']}")
        except Exception:
            pass

        # Pinned items section
        try:
            pinned_parts: list[str] = []
            for table, label in [
                ("knowledge", "knowledge"),
                ("negative_knowledge", "negative"),
                ("errors", "error"),
                ("rules", "rule"),
            ]:
                rows = db.execute(
                    f"SELECT id FROM {table} WHERE pinned = 1"
                ).fetchall()
                if rows:
                    pinned_parts.append(f"  {label}: {len(rows)}")
            if pinned_parts:
                parts.append("Pinned items:\n" + "\n".join(pinned_parts))
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

        Resets ALL behavioral counters. Automatically creates a final snapshot
        with the handoff data. Call this before ending a session or when nudged
        to checkpoint.

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

        # Auto-create a final snapshot with the handoff data
        session_row = db.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        session_id = session_row["id"] if session_row else None

        # Get next sequence number for this session (handle NULL session_id)
        if session_id is not None:
            seq_row = db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 as next_seq "
                "FROM snapshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            seq_row = db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 as next_seq "
                "FROM snapshots WHERE session_id IS NULL",
            ).fetchone()
        next_seq = seq_row["next_seq"]

        try:
            db.execute_write(
                "INSERT INTO snapshots "
                "(session_id, sequence_num, goal, progress, next_steps, blockers) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, next_seq, current_task, status, next_steps, blockers),
            )
            db.commit()
        except Exception:
            pass  # snapshots table might not exist in older DBs

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
        """Save a numbered mid-session checkpoint.

        Use this to capture state at key milestones without ending the session.
        Snapshots are numbered sequentially within the current session.

        Args:
            goal: Current goal or task
            progress: What's been done so far
            open_questions: Unresolved questions
            blockers: Current blockers
            next_steps: Immediate next steps
            active_files: Files currently being worked on
            key_decisions: Important decisions made
        """
        tracker.record_call("save_snapshot")

        # Find the current session (most recent)
        session_row = db.execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        session_id = session_row["id"] if session_row else None

        # Get next sequence number (handle NULL session_id)
        if session_id is not None:
            seq_row = db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 as next_seq "
                "FROM snapshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            seq_row = db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 as next_seq "
                "FROM snapshots WHERE session_id IS NULL",
            ).fetchone()
        next_seq = seq_row["next_seq"]

        db.execute_write(
            "INSERT INTO snapshots "
            "(session_id, sequence_num, goal, progress, open_questions, blockers, "
            "next_steps, active_files, key_decisions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, next_seq, goal, progress, open_questions, blockers,
             next_steps, active_files, key_decisions),
        )
        db.commit()
        return _with_nudge(
            f"Snapshot #{next_seq} saved (session={session_id}).", tracker
        )

    @mcp.tool()
    def get_resume_context() -> str:
        """Get structured context for resuming work.

        Returns the last session handoff, the last snapshot, all pinned items,
        critical rules, and a project summary. Designed for session start or
        after context compaction.
        """
        tracker.record_call("get_resume_context")
        parts: list[str] = []

        # Last session
        session = db.execute(
            "SELECT status, current_task, next_steps, blockers, created_at "
            "FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if session:
            parts.append("--- Last Session ---")
            parts.append(f"Status: {session['status']}")
            if session["current_task"]:
                parts.append(f"Task: {session['current_task']}")
            if session["next_steps"]:
                parts.append(f"Next: {session['next_steps']}")
            if session["blockers"]:
                parts.append(f"Blockers: {session['blockers']}")
            parts.append(f"Time: {session['created_at']}")

        # Last snapshot
        try:
            snapshot = db.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if snapshot:
                parts.append(f"\n--- Last Snapshot (#{snapshot['sequence_num']}) ---")
                if snapshot["goal"]:
                    parts.append(f"Goal: {snapshot['goal']}")
                if snapshot["progress"]:
                    parts.append(f"Progress: {snapshot['progress']}")
                if snapshot["open_questions"]:
                    parts.append(f"Open questions: {snapshot['open_questions']}")
                if snapshot["blockers"]:
                    parts.append(f"Blockers: {snapshot['blockers']}")
                if snapshot["next_steps"]:
                    parts.append(f"Next: {snapshot['next_steps']}")
                if snapshot["active_files"]:
                    parts.append(f"Active files: {snapshot['active_files']}")
                if snapshot["key_decisions"]:
                    parts.append(f"Key decisions: {snapshot['key_decisions']}")
        except Exception:
            pass

        # Pinned knowledge
        try:
            pinned_k = db.execute(
                "SELECT topic, kind, summary FROM knowledge WHERE pinned = 1"
            ).fetchall()
            if pinned_k:
                parts.append("\n--- Pinned Knowledge ---")
                for r in pinned_k:
                    parts.append(f"[{r['kind'].upper()}] {r['topic']}: {r['summary']}")
        except Exception:
            pass

        # Pinned negative knowledge
        try:
            pinned_n = db.execute(
                "SELECT category, what_failed FROM negative_knowledge WHERE pinned = 1"
            ).fetchall()
            if pinned_n:
                parts.append("\n--- Pinned Negative Knowledge ---")
                for r in pinned_n:
                    parts.append(f"{r['category']}: {r['what_failed']}")
        except Exception:
            pass

        # Pinned errors
        try:
            pinned_e = db.execute(
                "SELECT pattern, fix FROM errors WHERE pinned = 1"
            ).fetchall()
            if pinned_e:
                parts.append("\n--- Pinned Errors ---")
                for r in pinned_e:
                    entry = r["pattern"]
                    if r["fix"]:
                        entry += f" -> {r['fix']}"
                    parts.append(entry)
        except Exception:
            pass

        # Pinned rules
        try:
            pinned_r = db.execute(
                "SELECT title, file_path FROM rules WHERE pinned = 1"
            ).fetchall()
            if pinned_r:
                parts.append("\n--- Pinned Rules ---")
                for r in pinned_r:
                    entry = r["title"]
                    if r["file_path"]:
                        entry += f" ({r['file_path']})"
                    parts.append(entry)
        except Exception:
            pass

        # Project summary
        parts.append(f"\nProject: {project_name}")

        if not parts:
            return _with_nudge("No resume context available.", tracker)
        return _with_nudge("\n".join(parts), tracker)
