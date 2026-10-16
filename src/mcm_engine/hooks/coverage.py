"""Hook-side retrieval coverage (issues #157/#158/#159).

The server owns an in-process coverage ledger (issue #156) keyed on the opaque
MCP ServerSession. The hooks are a separate process family keyed by the Claude
Code agent key (session_id, or session_id:agent_id) and CANNOT address that
in-process ledger: a hook that searches over HTTP opens its own ServerSession,
and there is no reliable session_id<->ServerSession mapping (RULE #116,
DECISION #39). So the hooks keep their own coverage view inside the shared
enforcement state file, under each agent entry.

Residency (the founding anti-amnesia concern, mirrored from #156): a topic is
RESIDENT only if it was covered AFTER the last compaction boundary for that
session. covered_topics maps topic -> epoch; last_compaction_at is the boundary
stamped by the SessionStart (source==compact) and PreCompact hooks. A topic
whose retrieved bodies predate the last compaction is covered-but-not-resident
and does not satisfy enforcement.
"""
from __future__ import annotations

import time

# A topic searched more than this long ago is stale regardless of residency:
# the session has moved on and the bodies are unlikely to still inform it.
COVERAGE_TTL_SECONDS = 24 * 3600


def _key(topic: str) -> str:
    return (topic or "").lower().strip()


def record_topic(entry: dict, topic: str, *, now: float | None = None) -> None:
    """Record that `topic` was retrieved for this agent. No-op on empty topic."""
    key = _key(topic)
    if not key:
        return
    now = time.time() if now is None else now
    entry.setdefault("covered_topics", {})[key] = now


def note_compaction(entry: dict, *, now: float | None = None) -> None:
    """Stamp a compaction boundary: all prior coverage becomes non-resident."""
    entry["last_compaction_at"] = time.time() if now is None else now


def is_covered(
    entry: dict,
    topic: str,
    *,
    now: float | None = None,
    ttl: float = COVERAGE_TTL_SECONDS,
    require_resident: bool = True,
) -> bool:
    """True if `topic` was retrieved for this agent, is not stale, and (by
    default) is still resident (covered after the last compaction boundary)."""
    now = time.time() if now is None else now
    seen = entry.get("covered_topics", {}).get(_key(topic))
    if seen is None:
        return False
    if now - seen > ttl:
        return False
    # Residency only bites once a compaction has actually happened; with no
    # boundary stamped yet, all recorded coverage is resident.
    boundary = entry.get("last_compaction_at", 0.0)
    if require_resident and boundary and seen <= boundary:
        return False
    return True


def prune_topics(
    entry: dict, *, now: float | None = None, ttl: float = COVERAGE_TTL_SECONDS
) -> None:
    """Drop stale topics so the state file does not grow without bound."""
    now = time.time() if now is None else now
    topics = entry.get("covered_topics")
    if not topics:
        return
    for key in [k for k, seen in topics.items() if now - seen > ttl]:
        del topics[key]
