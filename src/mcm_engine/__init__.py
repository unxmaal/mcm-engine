"""MCM Engine — Memory Context Management for AI coding sessions.

Public API:
    MCMServer   — Main server class
    MCMConfig   — Configuration dataclass
    MCMPlugin   — Base class for plugins
    SearchScope — Dataclass for plugin search integration
    KnowledgeDB — SQLite wrapper (for plugin use)
    load_config — Load config from YAML + env vars
"""
from .config import MCMConfig, NudgeConfig, load_config
from .db import KnowledgeDB
from .plugin import MCMPlugin, SearchScope
from .server import MCMServer

__all__ = [
    "MCMServer",
    "MCMConfig",
    "NudgeConfig",
    "MCMPlugin",
    "SearchScope",
    "KnowledgeDB",
    "load_config",
]

__version__ = "0.1.0"
