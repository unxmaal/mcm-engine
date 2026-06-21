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
    """Table descriptor for a plugin-owned table that should participate in
    the unified search tool.

    MCM2-07: this is purely passive metadata. The engine's SearchBackend
    consumes the descriptor and runs the search; the plugin layer carries
    no SQL of its own. To run a search against a scope, call
    ``ctx.search.search_plugin(scope, query, limit)``.

    Attributes:
        name: Unique scope name (e.g., "rules", "site_data")
        label: Display label for results (e.g., "RULE", "SITE")
        fts_table: FTS5 virtual table name, or None for LIKE-only
        base_table: The real table to join against
        fts_columns: Columns in the FTS5 table to search
        display_columns: Columns to show in results
        like_columns: Columns to scan in the LIKE fallback
        format_fn: Optional custom formatter. Receives a row mapping,
            returns str. If None, the backend uses a default
            "[LABEL] col1 | col2" formatter.
    """

    name: str
    label: str
    fts_table: str | None
    base_table: str
    fts_columns: list[str] = field(default_factory=list)
    display_columns: list[str] = field(default_factory=list)
    like_columns: list[str] = field(default_factory=list)
    format_fn: Any = None  # Callable[[Mapping[str, Any]], str] | None


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

        server is an MCMServer instance. Plugins use:
          - server.mcp           — register MCP tool decorators on this
          - server.tracker       — record_call / nudge integration
          - server.ctx           — engine-managed adapters (storage, counters,
                                   search). MCM2-07: prefer ctx over raw db
                                   for engine-owned entity types.
          - server.db            — raw KnowledgeDB handle for the plugin's
                                   own tables (engine tables go through ctx).
        """
        pass

    def get_search_scopes(self) -> list[SearchScope]:
        """Return SearchScope descriptors to extend the unified search tool.

        Post-MCM2-07 each scope is passive metadata; the engine's
        SearchBackend runs the actual search.
        """
        return []

    def get_nudge(self, tracker: SessionTracker) -> str | None:
        """Return a domain-specific nudge, or None."""
        return None

    def on_session_start(self, db: KnowledgeDB) -> dict[str, str]:
        """Return extra context for session_start. Keys become output lines."""
        return {}
