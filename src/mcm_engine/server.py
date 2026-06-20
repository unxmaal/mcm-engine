"""MCMServer — composes FastMCP + DB + tracker + plugins."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .adapters.sqlite.counters import SqliteCounters
from .adapters.sqlite.search import SqliteSearch
from .adapters.sqlite.session import InMemorySession
from .adapters.sqlite.storage import SqliteStorage
from .config import MCMConfig
from .db import KnowledgeDB, log, set_log_path
from .files.watcher import RulesWatcher
from .plugin import MCMPlugin, SearchScope
from .schema import migrate_core, migrate_plugin
from .tracker import SessionTracker
from .tools.knowledge import register_knowledge_tools
from .tools.relations import register_relations_tools
from .tools.rules import register_rules_tools
from .tools.search import register_search_tools
from .tools.session import register_session_tools
from .wiring import Context


def _load_plugin(spec: str) -> MCMPlugin:
    """Load a plugin from an entry point name or module:Class path."""
    if ":" in spec:
        # Direct module:Class import
        module_path, class_name = spec.rsplit(":", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls()
    else:
        # Entry point lookup
        if sys.version_info >= (3, 12):
            from importlib.metadata import entry_points
            eps = entry_points(group="mcm_engine.plugins", name=spec)
        else:
            from importlib.metadata import entry_points
            all_eps = entry_points()
            if hasattr(all_eps, "select"):
                eps = all_eps.select(group="mcm_engine.plugins", name=spec)
            else:
                eps = [ep for ep in all_eps.get("mcm_engine.plugins", []) if ep.name == spec]

        for ep in eps:
            cls = ep.load()
            return cls()

        raise ValueError(f"Plugin '{spec}' not found in entry points or as module:Class")


class MCMServer:
    """Main server class that composes all components.

    Usage:
        config = load_config()
        server = MCMServer(config, project_root=Path("."))
        server.run()
    """

    def __init__(self, config: MCMConfig, project_root: Path | None = None):
        if project_root is None:
            project_root = Path.cwd()
        self.config = config
        self.project_root = project_root

        # Set up logging
        set_log_path(config.log_path)
        log(f"MCM Engine starting for project '{config.project_name}'")

        # Database
        db_path = config.resolve_db_path(project_root)
        self.db = KnowledgeDB(db_path)
        migrate_core(self.db)

        # Engine-managed adapters (the Context). Today the embedded
        # SQLite set is used for the legacy stdio path; the orthogonal
        # config switches in MCM2-19 are exercised by build_context()
        # callers (the test suite and future serve startup). Plugins
        # can read ``self.ctx`` per MCM2-07's docstring.
        self.ctx: Context = Context(
            storage=SqliteStorage(db=self.db),
            counters=SqliteCounters(db=self.db),
            search=SqliteSearch(db=self.db),
            session=InMemorySession(),
        )

        # Tracker
        self.tracker = SessionTracker(config.nudges)

        # FastMCP server
        instructions = config.server_instructions or (
            f"Knowledge management server for {config.project_name}. "
            "Use search to find knowledge, report_error to log errors and auto-search for fixes, "
            "add_knowledge/add_negative to store findings, and session_start/session_handoff "
            "for session management."
        )
        self.mcp = FastMCP(config.server_name, instructions=instructions)

        # Plugins
        self._plugins: list[MCMPlugin] = []
        self._search_scopes: list[SearchScope] = []
        self._plugin_session_fns: list = []

        # Load plugins
        for spec in config.plugins:
            try:
                plugin = _load_plugin(spec)
                self._plugins.append(plugin)
                log(f"Loaded plugin: {plugin.name}")
            except Exception as e:
                log(f"Failed to load plugin '{spec}': {e}")

        # Apply plugin schemas
        for plugin in self._plugins:
            schema_sql = plugin.get_schema_sql()
            if schema_sql:
                migrate_plugin(self.db, plugin.name, schema_sql, plugin.version)

            # Collect search scopes
            self._search_scopes.extend(plugin.get_search_scopes())

            # Collect session start functions
            if hasattr(plugin, "on_session_start"):
                self._plugin_session_fns.append(plugin.on_session_start)

            # Register plugin nudges
            nudge_fn = plugin.get_nudge
            if nudge_fn:
                self.tracker.register_plugin_nudge(nudge_fn)

        # Register core tools
        # Search first (returns the internal search_all function)
        search_all_fn = register_search_tools(
            self.mcp, self.db, self.tracker, self._search_scopes,
            project_name=config.project_name,
        )
        self._search_all_fn = search_all_fn

        # Knowledge tools (needs search_all for report_error)
        register_knowledge_tools(
            self.mcp, self.db, self.tracker, config.project_name, search_all_fn
        )

        # Session tools
        register_session_tools(
            self.mcp, self.db, self.tracker, config.project_name, self._plugin_session_fns
        )

        # Rules tools
        rules_paths = config.resolve_rules_paths(project_root)
        register_rules_tools(
            self.mcp, self.db, self.tracker, config.project_name, rules_paths, project_root
        )

        # Relations tools
        register_relations_tools(self.mcp, self.db, self.tracker)

        # Register plugin tools
        for plugin in self._plugins:
            try:
                plugin.register_tools(self)
            except Exception as e:
                log(f"Failed to register tools for plugin '{plugin.name}': {e}")

        # Watcher cascade (MCM2-23). Constructed but NOT started here —
        # stdio mode calls sync_once at startup and never starts the
        # background thread; serve mode (start_watcher()) starts the
        # observer for live file→DB cascades.
        rules_paths = config.resolve_rules_paths(project_root)
        primary_rules = rules_paths[0] if rules_paths else project_root / "rules"
        self.watcher: RulesWatcher = RulesWatcher(
            self.ctx.storage, primary_rules, project_root,
        )

        log("MCM Engine ready")

    def start_watcher(self) -> None:
        """Daemon-mode startup: run a one-shot sync and then begin the
        background observer thread. See docs/watcher-cascade.md."""
        counts = self.watcher.sync_once()
        log(f"sync_rules at startup: {counts}")
        self.watcher.start()

    def stop_watcher(self) -> None:
        """Halt the observer (graceful shutdown)."""
        self.watcher.stop()

    def with_nudge(self, result: str, topic: str | None = None) -> str:
        """Append a behavioral nudge to a result string."""
        nudge = self.tracker.get_nudge(topic)
        if nudge:
            return f"{result}\n\n---\n{nudge}"
        return result

    def run(self):
        """Start the MCP server (stdio transport).

        Per docs/watcher-cascade.md: stdio mode runs sync_once at
        startup so the DB reflects the disk state at the moment the
        session begins, but does NOT start the background observer —
        process lifetime is too short for live file watching to pay
        for itself.
        """
        try:
            counts = self.watcher.sync_once()
            log(f"sync_rules at stdio startup: {counts}")
        except Exception as e:
            log(f"sync_rules at stdio startup failed (non-fatal): {e}")
        self.mcp.run()
