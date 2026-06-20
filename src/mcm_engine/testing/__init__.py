"""Reusable test helpers for mcm-engine adapter implementations.

The conformance suites in ``mcm_engine.testing.conformance`` are exposed
as importable mixin classes so third-party adapter packages can verify
their implementation against the same suite the embedded SQLite
reference passes.
"""
from __future__ import annotations

from .conformance import (
    CounterConformance,
    SearchConformance,
    SessionConformance,
    StorageConformance,
)

__all__ = [
    "StorageConformance",
    "CounterConformance",
    "SearchConformance",
    "SessionConformance",
]
