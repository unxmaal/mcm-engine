"""MCM Engine — Memory Context Management for AI coding sessions.

Public API:
    MCMServer   — Main server class
    MCMConfig   — Configuration dataclass
    MCMPlugin   — Base class for plugins
    SearchScope — Dataclass for plugin search integration
    KnowledgeDB — SQLite wrapper (for plugin use)
    load_config — Load config from YAML + env vars
"""
from importlib.metadata import PackageNotFoundError, version as _dist_version

from .config import MCMConfig, NudgeConfig, load_config
from .db import KnowledgeDB
from .plugin import MCMPlugin, SearchScope
from .server import MCMServer
from .tracker import MandatoryStopError

__all__ = [
    "MCMServer",
    "MCMConfig",
    "NudgeConfig",
    "MCMPlugin",
    "SearchScope",
    "KnowledgeDB",
    "MandatoryStopError",
    "load_config",
]

try:
    __version__ = _dist_version("mcm-engine")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
