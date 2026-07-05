"""Blast-radius guard for bulk-destructive rule operations (issue #20).

A single sweep that would archive a large fraction of the corpus is almost
always a wrong-context accident — an empty/misrooted rules dir, a wrong
`project_root`, a failed volume mount, a database-authoritative pod running the
files sweep — not real drift. The watcher cascade already had this guard inline;
`sync_rules` did not, and that's how one call archived 80 of 80 rules.

`archive_would_storm` is the ONE shared predicate both call sites consult, so
the guard can't be present in one and missing in the other.
"""
from __future__ import annotations

DEFAULT_ARCHIVE_FLOOR = 5
DEFAULT_ARCHIVE_FRACTION = 0.5


def archive_would_storm(
    orphan_count: int,
    managed_count: int,
    *,
    floor: int = DEFAULT_ARCHIVE_FLOOR,
    fraction: float = DEFAULT_ARCHIVE_FRACTION,
) -> bool:
    """True if archiving ``orphan_count`` of ``managed_count`` managed rules
    looks like a wipe rather than ordinary drift: strictly more than ``floor``
    rows AND strictly more than ``fraction`` of the managed set. Below the floor,
    small corpora churn freely (a 3-rule project can lose all 3 without alarm)."""
    if orphan_count <= floor:
        return False
    return orphan_count / max(1, managed_count) > fraction
