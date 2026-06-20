"""Public adapter contract for mcm-engine v2.

Third-party adapters implement the Protocol classes defined here:

    StorageBackend  — durable knowledge/rules/sessions persistence
    CounterStore    — hit/reinforcement/pinned counters (may be off-row)
    SearchBackend   — ranked lexical search across the stored entities
    SessionStore    — tracker/nudge persistence (optional)

Plus EmbeddingBackend in a future phase for vector search (deferred).

This module MUST NOT import any adapter-specific library (NG-8). The
only allowed imports are: typing, dataclasses, enum, datetime, pathlib.
The CI guard in tests/test_protocols.py asserts this.

Contract versioning lives in docs/contract-versioning.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional, Protocol, runtime_checkable

#: Bumped on any breaking change to the Protocol classes or row dataclasses.
#: Adapters declare the version they were built against; mismatch raises at
#: registration time. See docs/contract-versioning.md.
CONTRACT_VERSION: int = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EntityType(StrEnum):
    """The four pin-able / cross-table entity kinds.

    Drives every "dynamic table name" call site identified in the seam
    inventory (knowledge.py pin/unpin, relations.py existence checks,
    session.py count loops).
    """

    KNOWLEDGE = "knowledge"
    NEGATIVE = "negative"
    ERROR = "error"
    RULE = "rule"


class Capability(StrEnum):
    """Optional capabilities adapters may opt into.

    The escape hatch from docs/contract-versioning.md: a new method can be
    added without bumping CONTRACT_VERSION if it lives behind a capability
    flag. The engine probes `Capability.X in adapter.capabilities` before
    calling. Adapters without the capability fall back to the
    always-required methods.

    Empty for v1; populated as the contract evolves.
    """

    # Placeholder so the enum class exists and adapters have a target
    # to import. Real capabilities arrive with their methods.
    _RESERVED = "_reserved"


# ---------------------------------------------------------------------------
# Row dataclasses — the boundary shape between engine and adapter.
# ---------------------------------------------------------------------------
#
# These mirror the SQLite v6 schema (plus the v7 watcher additions on rules).
# Adapters convert their native row representation into these dataclasses on
# read and accept them on write.
#
# Counter columns (hit_count, reinforcement_count, pinned, last_hit_at) live
# on KnowledgeRow / RuleRow as a *flushed snapshot* of the CounterStore's
# state — read-only from the StorageBackend perspective. Live counter
# updates flow through CounterStore. See docs/seam-inventory.md.


@dataclass
class KnowledgeRow:
    id: int
    topic: str
    summary: str
    kind: str = "finding"
    detail: Optional[str] = None
    tags: Optional[str] = None
    project: Optional[str] = None
    rationale: Optional[str] = None
    alternatives: Optional[str] = None
    hit_count: int = 0
    last_hit_at: Optional[datetime] = None
    reinforcement_count: int = 0
    pinned: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class NegativeRow:
    id: int
    category: str
    what_failed: str
    why_failed: Optional[str] = None
    correct_approach: Optional[str] = None
    severity: str = "normal"
    project: Optional[str] = None
    pinned: bool = False
    created_at: Optional[datetime] = None


@dataclass
class ErrorRow:
    id: int
    pattern: str
    context: Optional[str] = None
    root_cause: Optional[str] = None
    fix: Optional[str] = None
    tags: Optional[str] = None
    project: Optional[str] = None
    pinned: bool = False
    created_at: Optional[datetime] = None


@dataclass
class RuleRow:
    id: int
    title: str
    keywords: str
    file_path: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    hit_count: int = 0
    last_hit_at: Optional[datetime] = None
    reinforcement_count: int = 0
    pinned: bool = False
    # MCM2-23 watcher cascade additions (v7 schema):
    content_hash: Optional[str] = None
    archived: bool = False
    archived_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SessionRow:
    id: int
    status: str
    current_task: Optional[str] = None
    findings_summary: Optional[str] = None
    next_steps: Optional[str] = None
    blockers: Optional[str] = None
    context_snapshot: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class SnapshotRow:
    id: int
    sequence_num: int
    session_id: Optional[int] = None
    goal: Optional[str] = None
    progress: Optional[str] = None
    open_questions: Optional[str] = None
    blockers: Optional[str] = None
    next_steps: Optional[str] = None
    active_files: Optional[str] = None
    key_decisions: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class RelationRow:
    id: int
    source_type: EntityType
    source_id: int
    target_type: EntityType
    target_id: int
    relation: str
    note: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class SearchHit:
    """A single search result returned by SearchBackend.

    `score` semantics: higher = better, across all adapters. SQLite's FTS5
    rank is negative-better; adapters convert at their boundary so the
    scorer doesn't need to know which adapter produced the hit.

    `counters_snapshot` is the row's flushed counter state at search time
    — distinct from CounterStore's live counts, which may have drifted by
    the documented staleness window (OQ-3: minutes).
    """

    entity_type: EntityType
    entity_id: int
    score: float
    is_pinned: bool = False
    is_stale: bool = False
    counters_snapshot: dict[str, Any] = field(default_factory=dict)
    #: The row itself, where the adapter has it cheaply. Optional —
    #: SearchBackend MAY return just the id+score pair and let the caller
    #: fetch via StorageBackend, or include the row to save a round trip.
    row: Optional[Any] = None


# ---------------------------------------------------------------------------
# Protocol: StorageBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Durable persistence for knowledge, rules, sessions, and relations.

    Implementations: SQLite (embedded reference), Postgres (first-party
    extra), plus any third-party adapter that passes
    mcm_engine.testing.conformance.run_storage_conformance().
    """

    CONTRACT_VERSION: int
    capabilities: set[Capability]

    # ---- Schema management ----
    def ensure_schema(self) -> None:
        """Create or migrate the adapter's underlying schema to the
        current version. Idempotent. Called at composition-root startup."""
        ...

    # NOTE: every read method accepts `caller: Optional[str] = None` as a
    # MCM2-05 no-op pass-through. Today's embedded reference ignores it;
    # future multi-tenant adapters will filter on caller identity.

    # ---- Knowledge ----
    def find_knowledge_by_topic_kind(
        self, topic: str, kind: str, *, caller: Optional[str] = None
    ) -> Optional[KnowledgeRow]: ...
    def find_similar_knowledge(
        self, topic: str, *, caller: Optional[str] = None
    ) -> Optional[KnowledgeRow]: ...
    def insert_knowledge(self, row: KnowledgeRow) -> int: ...
    def update_knowledge(self, knowledge_id: int, **fields: Any) -> None: ...

    # ---- Negative ----
    def insert_negative(self, row: NegativeRow) -> int: ...

    # ---- Errors ----
    def insert_error(self, row: ErrorRow) -> int: ...

    # ---- Rules ----
    def find_rule_by_title(
        self, title: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]: ...
    def find_rule_by_file_path(
        self, file_path: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]: ...
    def insert_rule(self, row: RuleRow) -> int: ...
    def update_rule(self, rule_id: int, **fields: Any) -> None: ...
    def list_rules_with_file_paths(
        self, *, caller: Optional[str] = None
    ) -> list[RuleRow]: ...
    def soft_delete_rule(self, rule_id: int) -> None: ...
    def restore_rule(self, rule_id: int) -> None: ...

    # ---- Relations ----
    def insert_relation(self, row: RelationRow) -> Optional[int]:
        """Returns the new id, or None if the unique constraint was
        violated (relation already exists)."""
        ...

    def list_outgoing_relations(
        self, source_type: EntityType, source_id: int,
        *, caller: Optional[str] = None,
    ) -> list[RelationRow]: ...

    def list_incoming_relations(
        self, target_type: EntityType, target_id: int,
        *, caller: Optional[str] = None,
    ) -> list[RelationRow]: ...

    # ---- Sessions + snapshots ----
    def insert_session(self, row: SessionRow) -> int: ...
    def get_last_session(
        self, *, caller: Optional[str] = None
    ) -> Optional[SessionRow]: ...
    def next_snapshot_seq(self, session_id: Optional[int]) -> int: ...
    def insert_snapshot(self, row: SnapshotRow) -> int: ...
    def get_last_snapshot(
        self, *, caller: Optional[str] = None
    ) -> Optional[SnapshotRow]: ...

    # ---- Cross-entity (driven by dynamic-table sites in the inventory) ----
    def set_pinned(self, entity_type: EntityType, entity_id: int, value: bool) -> None: ...

    def count_by_type(
        self,
        entity_type: EntityType,
        *,
        project: Optional[str] = None,
        pinned: Optional[bool] = None,
        caller: Optional[str] = None,
    ) -> int: ...

    def list_pinned(
        self, entity_type: EntityType, *, caller: Optional[str] = None
    ) -> list[Any]: ...

    def entry_exists(
        self, entity_type: EntityType, entity_id: int,
        *, caller: Optional[str] = None,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Protocol: CounterStore
# ---------------------------------------------------------------------------


@runtime_checkable
class CounterStore(Protocol):
    """Hit / reinforcement / pinned counters.

    May be in-process (embedded reference: dict + write-through to
    StorageBackend), Redis (sorted-set backed), or a separate Postgres
    counter table. Splitting these off the entry row is what relieves
    write pressure on the durable store and lets ranked reads be served
    cheaply.

    Per OQ-3: adapters MAY batch writes with a staleness window not
    exceeding a few minutes. The embedded reference writes through
    synchronously.
    """

    CONTRACT_VERSION: int
    capabilities: set[Capability]

    def increment(
        self, entity_type: EntityType, entity_id: int, counter_name: str, by: int = 1
    ) -> None: ...

    def get(self, entity_type: EntityType, entity_id: int) -> dict[str, Any]: ...

    def top_by(
        self,
        entity_type: EntityType,
        counter_name: str,
        k: int,
    ) -> list[tuple[int, float]]:
        """Return up to k (entity_id, counter_value) pairs, descending."""
        ...

    def flush(self) -> None:
        """Force any batched writes to land. No-op for write-through
        adapters."""
        ...

    def last_flushed_snapshot(
        self, entity_type: EntityType, entity_id: int
    ) -> dict[str, Any]:
        """Return the counter values as of the last flush — used by
        SearchBackend.search() to compose the rank without a round-trip
        to live counts."""
        ...


# ---------------------------------------------------------------------------
# Protocol: SearchBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchBackend(Protocol):
    """Ranked lexical (and eventually vector) search across stored entities.

    Returns SearchHit dataclasses. The Python composite scorer combines
    these with live CounterStore values to produce the final ordering, so
    SearchBackend does NOT promise consistent ordering across adapters —
    it promises a normalized score (higher = better).

    Capability flag (future): Capability.VECTOR_SEARCH.
    """

    CONTRACT_VERSION: int
    capabilities: set[Capability]

    def search(
        self,
        query: str,
        *,
        entity_types: Optional[set[EntityType]] = None,
        limit: int = 10,
        project: Optional[str] = None,
        caller: Optional[str] = None,
    ) -> list[SearchHit]: ...

    def reindex(self, entity_type: Optional[EntityType] = None) -> None:
        """Rebuild the search index from the current StorageBackend
        state. Required when the durable store has changed outside the
        normal write path (e.g., a bulk migration)."""
        ...


# ---------------------------------------------------------------------------
# Protocol: SessionStore
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionStore(Protocol):
    """Persistence for in-session tracker state.

    Per OQ-5: in-memory is the default; SessionStore exists as an
    extension point. Embedded reference is in-process (today's behavior:
    state lives in SessionTracker and is lost on restart). Third-party
    adapters MAY persist state to Redis, SQLite, etc.
    """

    CONTRACT_VERSION: int
    capabilities: set[Capability]

    def load_state(self, key: str) -> Optional[dict[str, Any]]:
        """Return the previously-saved state under `key`, or None."""
        ...

    def save_state(self, key: str, state: dict[str, Any]) -> None:
        """Persist `state` under `key`. Overwrites any prior value."""
        ...
