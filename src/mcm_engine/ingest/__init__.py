"""Bulk-ingest framework for mcm-engine.

Goal: a polymorphic ``mcm-engine ingest <source>`` that reads data from
some external place (markdown directory today; jsonl/csv/db/other in the
future) and inserts ``KnowledgeRow`` records via the configured
``StorageBackend`` — same code path as ``add_knowledge``, just without
the per-row MCP roundtrip.

Why a framework, not a script: an ad-hoc import script ends up
re-implementing config resolution, dedup, and progress reporting badly.
Centralizing the dispatcher + ingester registry keeps each new source
type small (just implement ``stream``) and inherits the engine's
correct path-resolution + backend-agnostic write logic.

Wiring an ingester:
    from mcm_engine.ingest import register, Ingester
    class MyIngester:
        name = "my-format"
        @classmethod
        def matches(cls, source): return source.endswith(".myfmt")
        def stream(self, source, opts): yield from ...
    register(MyIngester)

The CLI ``ingest`` subcommand (``mcm_engine.cli.cmd_ingest``) drives the
registry, hands rows through to ``ctx.storage``, and reports counters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

from ..backends import KnowledgeRow


@runtime_checkable
class Ingester(Protocol):
    """Source-specific reader that yields ``KnowledgeRow`` objects.

    Implementations register themselves at module import time via
    :func:`register`. Each ingester is selected by either
    auto-detection (``matches(source)``) or explicit ``--type`` flag.
    """

    #: short identifier for the ingester (e.g. "markdown-dir", "jsonl").
    #: Used by ``--type``/``--list-types`` and surfaces in error messages.
    name: str

    @classmethod
    def matches(cls, source: str) -> bool:
        """Return True iff this ingester can read ``source`` as-is. The
        dispatcher calls every registered ingester's ``matches`` in
        registration order and picks the first that says yes."""
        ...

    def stream(
        self, source: str, opts: dict[str, Any]
    ) -> Iterator[KnowledgeRow]:
        """Yield ``KnowledgeRow`` objects from ``source``. Per-row errors
        should be raised as ``IngestError`` with row context; the
        dispatcher will count + continue. Catastrophic source errors
        (missing file, malformed root) may raise other exceptions."""
        ...


class IngestError(Exception):
    """Per-row failure during ingest. Carries a human-readable context
    string so the dispatcher can report which row failed."""

    def __init__(self, context: str, original: Exception | None = None):
        super().__init__(f"{context}: {original}" if original else context)
        self.context = context
        self.original = original


@dataclass
class IngestReport:
    """Returned by the dispatcher; reflects what happened during a run."""

    inserted: int = 0
    updated: int = 0
    errors: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Registry — in-module for v1. Entry-point discovery can be added later
# without changing the public ``register``/``find`` surface.
# ---------------------------------------------------------------------------


_REGISTERED: list[type[Ingester]] = []


def register(cls: type[Ingester]) -> type[Ingester]:
    """Register an ingester class. Returns the class so it works as a
    decorator. Idempotent — re-registering the same class is a no-op."""
    if cls in _REGISTERED:
        return cls
    _REGISTERED.append(cls)
    return cls


def registered() -> list[type[Ingester]]:
    """Snapshot of registered ingester classes, in registration order."""
    return list(_REGISTERED)


class UnknownIngester(Exception):
    """Raised when ``--type`` names an ingester that isn't registered."""


class NoMatchingIngester(Exception):
    """Raised when no registered ingester recognizes ``source``."""


def find(source: str, *, explicit_name: Optional[str] = None) -> Ingester:
    """Pick an ingester for ``source``.

    - If ``explicit_name`` is given, return the named ingester or raise
      ``UnknownIngester``.
    - Otherwise call ``matches`` on each registered ingester in order
      and return the first that says yes.
    - Raise ``NoMatchingIngester`` if nothing matches.
    """
    if explicit_name:
        for cls in _REGISTERED:
            if cls.name == explicit_name:
                return cls()
        names = ", ".join(c.name for c in _REGISTERED) or "(none registered)"
        raise UnknownIngester(
            f"no ingester named '{explicit_name}'. Available: {names}"
        )

    for cls in _REGISTERED:
        if cls.matches(source):
            return cls()
    names = ", ".join(c.name for c in _REGISTERED) or "(none registered)"
    raise NoMatchingIngester(
        f"no registered ingester matches source '{source}'. "
        f"Try --type with one of: {names}"
    )


# Eager-import the built-in ingesters so they self-register on package
# load. Keep this list short and well-curated; third-party ingesters
# should register themselves at their own import time.
from . import markdown as _markdown  # noqa: F401, E402
