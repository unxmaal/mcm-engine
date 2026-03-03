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
    ) -> str:
        """Store a learning — finding, decision, or insight.

        Args:
            topic: What this knowledge is about
            summary: One-line summary
            kind: One of 'finding', 'decision', 'insight'
            detail: Extended explanation
            tags: Comma-separated tags for search
            rationale: For decisions — why this choice
            alternatives: For decisions — what was rejected
        """
        tracker.record_call("add_knowledge", topic=topic)
        tracker.record_store()
        db.execute_write(
            "INSERT INTO knowledge (topic, kind, summary, detail, tags, project, rationale, alternatives) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, kind, summary, detail, tags, project_name, rationale, alternatives),
        )
        db.commit()
        return _with_nudge(f"Stored {kind}: {topic} — {summary}", tracker, topic)

    @mcp.tool()
    def add_negative(
        category: str,
        what_failed: str,
        why_failed: str = "",
        correct_approach: str = "",
        severity: str = "normal",
    ) -> str:
        """Store what doesn't work — mistakes, anti-patterns, dead ends.

        Args:
            category: Area or topic
            what_failed: What was tried
            why_failed: Why it didn't work
            correct_approach: What to do instead
            severity: 'normal' or 'critical'
        """
        tracker.record_call("add_negative", topic=category)
        tracker.record_store()
        db.execute_write(
            "INSERT INTO negative_knowledge "
            "(category, what_failed, why_failed, correct_approach, severity, project) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (category, what_failed, why_failed, correct_approach, severity, project_name),
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
    ) -> str:
        """Report an error AND automatically search for matching fixes.

        THE KILLER FEATURE: one tool call logs the error AND searches all
        knowledge scopes for matching fixes. Always returns both the logged
        confirmation and any matching rules/fixes.

        Args:
            error_text: The error message or text
            context: Additional context (build phase, file, etc.)
            tags: Comma-separated tags
        """
        tracker.record_call("report_error", topic=error_text[:50])
        tracker.record_store()

        # Insert error
        db.execute_write(
            "INSERT INTO errors (pattern, context, tags, project) VALUES (?, ?, ?, ?)",
            (error_text, context, tags, project_name),
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
