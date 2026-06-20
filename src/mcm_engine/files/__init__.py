"""Files-win watcher cascade (MCM2-23).

See docs/watcher-cascade.md for the design rationale.
"""
from .watcher import RulesWatcher, compute_content_hash

__all__ = ["RulesWatcher", "compute_content_hash"]
