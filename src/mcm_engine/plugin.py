"""MCMPlugin base class and SearchScope for domain-specific extensions."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import KnowledgeDB
    from .tracker import SessionTracker


@dataclass
class SearchScope:
    """Defines how to FTS5-search a plugin table and format results.

    Attributes:
        name: Unique scope name (e.g., "rules", "site_data")
        label: Display label for results (e.g., "RULE", "SITE")
        fts_table: FTS5 virtual table name, or None for LIKE-only
        base_table: The real table to join against
        fts_columns: Columns in the FTS5 table to search
        display_columns: Columns to show in results
        format_fn: Optional custom formatter. Receives a sqlite3.Row, returns str.
                   If None, a default formatter is used.
    """

    name: str
    label: str
    fts_table: str | None
    base_table: str
    fts_columns: list[str] = field(default_factory=list)
    display_columns: list[str] = field(default_factory=list)
    like_columns: list[str] = field(default_factory=list)
    format_fn: Any = None  # Callable[[sqlite3.Row], str] | None

    def search(self, db, query: str, fts_query: str, like_pattern: str, limit: int) -> list[str]:
        """Search this scope and return formatted result strings."""
        results: list[str] = []
        rows = []

        # Try FTS5 first
        if self.fts_table and self.fts_columns:
            try:
                cols = ", ".join(f"b.{c}" for c in self.display_columns)
                rows = db.execute(
                    f"SELECT {cols} FROM {self.fts_table} f "
                    f"JOIN {self.base_table} b ON f.rowid = b.id "
                    f"WHERE {self.fts_table} MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit),
                ).fetchall()
            except Exception:
                rows = []

        # LIKE fallback
        if not rows and self.like_columns:
            conditions = " OR ".join(f"{c} LIKE ?" for c in self.like_columns)
            cols = ", ".join(self.display_columns)
            params = tuple(like_pattern for _ in self.like_columns) + (limit,)
            rows = db.execute(
                f"SELECT {cols} FROM {self.base_table} WHERE {conditions} LIMIT ?",
                params,
            ).fetchall()

        # Format results
        for r in rows:
            if self.format_fn:
                results.append(self.format_fn(r))
            else:
                # Default: [LABEL] col1: col2
                vals = [str(r[c]) for c in self.display_columns if r[c]]
                results.append(f"[{self.label}] {' | '.join(vals)}")

        return results


class MCMPlugin(ABC):
    """Base class for MCM Engine plugins.

    Plugins extend the knowledge server with domain-specific:
    - Database tables (schema)
    - MCP tools
    - Search scopes
    - Behavioral nudges
    - Session start context
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name."""
        ...

    @property
    def version(self) -> int:
        """Schema version. Increment when schema changes."""
        return 1

    def get_schema_sql(self) -> str:
        """Return SQL to create plugin tables. Use CREATE TABLE IF NOT EXISTS."""
        return ""

    def get_migration_sql(self, current_version: int) -> list[str]:
        """Return SQL statements to migrate from current_version to self.version."""
        return []

    def register_tools(self, server) -> None:
        """Register additional MCP tools on the server.

        server is an MCMServer instance — use server.mcp, server.db, server.tracker.
        """
        pass

    def get_search_scopes(self) -> list[SearchScope]:
        """Return SearchScope definitions to extend the unified search tool."""
        return []

    def get_nudge(self, tracker: SessionTracker) -> str | None:
        """Return a domain-specific nudge, or None."""
        return None

    def on_session_start(self, db: KnowledgeDB) -> dict[str, str]:
        """Return extra context for session_start. Keys become output lines."""
        return {}
