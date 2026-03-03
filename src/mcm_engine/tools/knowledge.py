"""Knowledge management tools — add_knowledge, add_negative, report_error."""
from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB, sanitize_fts
from ..tracker import SessionTracker


def _extract_keywords(error_text: str) -> list[str]:
    """Extract significant search keywords from error text."""
    noise = {
        "error", "warning", "undefined", "reference", "to", "in", "the", "a",
        "an", "for", "of", "from", "with", "not", "no", "is", "was", "at",
        "by", "on", "or", "and", "that", "this", "it", "be", "as", "are",
        "but", "if", "line", "file", "symbol", "function", "type",
    }
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", error_text)
    keywords = []
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
    db: KnowledgeDB,
    tracker: SessionTracker,
    project_name: str,
    search_all_fn,
) -> None:
    """Register add_knowledge, add_negative, report_error tools on the MCP server."""

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

        Args:
            topic: What this knowledge is about
            summary: One-line summary
            kind: One of 'finding', 'decision', 'insight'
            detail: Extended explanation
            tags: Comma-separated tags for search
            rationale: For decisions — why this choice
            alternatives: For decisions — what was rejected
            project: Project name (defaults to server's project_name)
        """
        tracker.record_call("add_knowledge", topic=topic)
        tracker.record_store()

        # Exact topic match — update instead of insert
        existing = db.execute(
            "SELECT id, summary FROM knowledge WHERE topic = ? AND kind = ?",
            (topic, kind),
        ).fetchone()
        if existing:
            db.execute_write(
                "UPDATE knowledge SET summary = ?, detail = ?, tags = ?, "
                "rationale = ?, alternatives = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (summary, detail, tags, rationale, alternatives, existing["id"]),
            )
            db.commit()
            return _with_nudge(
                f"Updated existing {kind}: {topic} (was: {existing['summary'][:80]})",
                tracker, topic,
            )

        # Fuzzy match — warn but still insert
        warning = ""
        try:
            fts_query = sanitize_fts(topic)
            similar = db.execute(
                "SELECT k.id, k.topic, k.summary "
                "FROM knowledge_fts f JOIN knowledge k ON f.rowid = k.id "
                "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT 1",
                (fts_query,),
            ).fetchone()
            if similar:
                warning = f"\n  Note: similar entry exists — [{similar['topic']}]: {similar['summary'][:80]}"
        except Exception:
            pass

        db.execute_write(
            "INSERT INTO knowledge (topic, kind, summary, detail, tags, project, rationale, alternatives) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, kind, summary, detail, tags, project or project_name, rationale, alternatives),
        )
        db.commit()
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
        """Store what doesn't work — mistakes, anti-patterns, dead ends.

        Args:
            category: Area or topic
            what_failed: What was tried
            why_failed: Why it didn't work
            correct_approach: What to do instead
            severity: 'normal' or 'critical'
            project: Project name (defaults to server's project_name)
        """
        tracker.record_call("add_negative", topic=category)
        tracker.record_store()
        db.execute_write(
            "INSERT INTO negative_knowledge "
            "(category, what_failed, why_failed, correct_approach, severity, project) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (category, what_failed, why_failed, correct_approach, severity, project or project_name),
        )
        db.commit()
        return _with_nudge(
            f"Stored negative knowledge: {category} — {what_failed}", tracker, category
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
        knowledge scopes for matching fixes. Always returns both the logged
        confirmation and any matching rules/fixes.

        Args:
            error_text: The error message or text
            context: Additional context (build phase, file, etc.)
            tags: Comma-separated tags
            project: Project name (defaults to server's project_name)
        """
        tracker.record_call("report_error", topic=error_text[:50])
        tracker.record_store()

        # Insert error
        db.execute_write(
            "INSERT INTO errors (pattern, context, tags, project) VALUES (?, ?, ?, ?)",
            (error_text, context, tags, project or project_name),
        )
        db.commit()

        parts = [f"Error logged: {error_text[:100]}"]

        # Auto-search: extract keywords and search all scopes
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
        """Deliberately reinforce a knowledge entry — signals "still correct".

        Stronger than a passive search hit (3x weight in ranking).

        Args:
            entry_id: ID of the knowledge entry to reinforce
        """
        tracker.record_call("reinforce_knowledge")
        row = db.execute("SELECT id, topic FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return _with_nudge(f"Knowledge entry {entry_id} not found.", tracker)

        db.execute_write(
            "UPDATE knowledge SET reinforcement_count = reinforcement_count + 1, "
            "last_hit_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (entry_id,),
        )
        db.commit()
        count = db.execute(
            "SELECT reinforcement_count FROM knowledge WHERE id = ?", (entry_id,)
        ).fetchone()["reinforcement_count"]
        return _with_nudge(
            f"Reinforced: {row['topic']} (reinforcement_count={count})", tracker
        )

    _PINNABLE_TABLES = {
        "knowledge": "knowledge",
        "negative": "negative_knowledge",
        "error": "errors",
        "rule": "rules",
    }

    @mcp.tool()
    def pin_item(entry_type: str, entry_id: int) -> str:
        """Pin an item so it's always loaded and never goes stale.

        Pinned items get a fixed 2.0 boost in search ranking (~20 passive hits).

        Args:
            entry_type: 'knowledge', 'negative', 'error', or 'rule'
            entry_id: ID of the entry to pin
        """
        tracker.record_call("pin_item")
        table = _PINNABLE_TABLES.get(entry_type)
        if not table:
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {', '.join(_PINNABLE_TABLES.keys())}",
                tracker,
            )
        row = db.execute(f"SELECT id FROM {table} WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return _with_nudge(f"{entry_type} entry {entry_id} not found.", tracker)

        db.execute_write(f"UPDATE {table} SET pinned = 1 WHERE id = ?", (entry_id,))
        db.commit()
        return _with_nudge(f"Pinned {entry_type} #{entry_id}.", tracker)

    @mcp.tool()
    def unpin_item(entry_type: str, entry_id: int) -> str:
        """Unpin an item, restoring normal staleness behavior.

        Args:
            entry_type: 'knowledge', 'negative', 'error', or 'rule'
            entry_id: ID of the entry to unpin
        """
        tracker.record_call("unpin_item")
        table = _PINNABLE_TABLES.get(entry_type)
        if not table:
            return _with_nudge(
                f"Invalid entry_type '{entry_type}'. Use: {', '.join(_PINNABLE_TABLES.keys())}",
                tracker,
            )
        row = db.execute(f"SELECT id FROM {table} WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return _with_nudge(f"{entry_type} entry {entry_id} not found.", tracker)

        db.execute_write(f"UPDATE {table} SET pinned = 0 WHERE id = ?", (entry_id,))
        db.commit()
        return _with_nudge(f"Unpinned {entry_type} #{entry_id}.", tracker)
