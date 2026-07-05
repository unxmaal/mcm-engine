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

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

#: Bumped on any breaking change to the Protocol classes or row dataclasses.
#: Adapters declare the version they were built against; mismatch raises at
#: registration time. See docs/contract-versioning.md.
CONTRACT_VERSION: int = 1


class MissingDependencyError(ImportError):
    """Raised when an adapter is instantiated without its optional client
    library installed.

    Importing the adapter module + class is always free — registry
    resolution must work on a bare `pip install mcm-engine`. The cost
    only lands when the adapter is actually constructed, and the error
    points at the right extras name (e.g. ``mcm-engine[postgres]``).
    """


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
    """

    #: Adapter supports dense-vector (embedding-based) similarity search.
    #: MCM2-16 / MCM2-17. Embedding model selection is a separate axis
    #: (see Capability.EMBEDDING_PROVIDER). Adapters without this flag
    #: serve lexical search only — callers requesting vector search
    #: degrade to lexical with a warning rather than failing.
    VECTOR_SEARCH = "vector_search"

    #: Adapter generates / accepts embedding vectors directly. Without
    #: this, callers must precompute embeddings and pass them in via
    #: VECTOR_SEARCH calls. Reserved — no adapter implements yet.
    EMBEDDING_PROVIDER = "embedding_provider"

    #: Adapter persists in-session tracker state across process restarts.
    #: Embedded InMemorySession does NOT have this; a Redis-backed
    #: SessionStore would.
    DURABLE_SESSION = "durable_session"


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
    # Issue #10 provenance additions (v8 schema): full rule body plus
    # actor attribution. `content` is the full markdown body (the row's
    # `description` remains the first-500-char leading-text FTS signal).
    content: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    # Issue #21 (v9): correctness axis + supersession. correct/incorrect are
    # outcome-driven (distinct from popularity). status/valid_until/superseded_by
    # implement non-destructive supersession.
    correct_count: int = 0
    incorrect_count: int = 0
    valid_until: Optional[datetime] = None
    superseded_by: Optional[int] = None
    status: str = "active"
    # Issue #64 (v11): rule hierarchy axes — importance (ordinal blast-radius
    # rank), scope (universal/conditional), kind (directive/fact). Orthogonal to
    # the confidence/lifecycle columns above. See mcm_engine.hierarchy for the
    # vocab. Conservative defaults: a fresh rule is a low-importance fact.
    importance: int = 0
    scope: str = "conditional"
    kind: str = "fact"


@dataclass
class RuleEventRow:
    """One append-only audit row per state change on a rule (issue #10).

    `rule_id` is deliberately NOT a foreign key on either backend — events
    outlive the rule they describe, so a hard-deleted rule leaves its
    history intact. `actor` is `'nobody'` when a write is unattributed.
    """

    id: int
    rule_id: int
    event_type: str
    actor: str = "nobody"
    at: Optional[datetime] = None
    content_hash: Optional[str] = None
    source_repo: Optional[str] = None
    source_ref: Optional[str] = None
    source_commit: Optional[str] = None
    note: Optional[str] = None


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


@dataclass(frozen=True)
class StorageIdentity:
    """What store a backend is actually bound to. ``kind`` is the adapter family
    ("sqlite" / "postgres"); ``location`` is the resolved, credential-free
    address (absolute db path, or host/dbname). Every backend self-reports one
    so that "which database did this write land in" is a mechanical fact, and so
    a configured authoritative store can be verified rather than assumed."""

    kind: str
    location: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.location}"


@runtime_checkable
class StorageBackend(Protocol):
    """Durable persistence for knowledge, rules, sessions, and relations.

    Implementations: SQLite (embedded reference), Postgres (first-party
    extra), plus any third-party adapter that passes
    mcm_engine.testing.conformance.run_storage_conformance().
    """

    CONTRACT_VERSION: int
    capabilities: set[Capability]

    #: The store this backend is bound to (self-reported, credential-free).
    identity: StorageIdentity

    # ---- Schema management ----
    def ensure_schema(self) -> None:
        """Create or migrate the adapter's underlying schema to the
        current version. Idempotent. Called at composition-root startup."""
        ...

    # ---- Transactions ----
    def transaction(self) -> "AbstractContextManager[None]":
        """Group multiple writes into one atomic unit.

        Writes issued inside the block that would normally self-commit are
        deferred; the whole block commits once on clean exit and rolls back
        on any exception. Used by bulk paths (e.g. the import_rules tool) that
        need all-or-nothing across several rows and their audit events.

        Caveat — the embedded reference adapters use a single shared
        connection, so a block holds one connection-level transaction. Any
        write issued on the SAME connection by another thread while the block
        is open is folded into this block's commit/rollback. Call it for
        deploy-time / single-writer batches (its intended use), not while
        other tool writes are expected to race it on the same adapter.
        """
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
    def find_rule_by_content_hash(
        self, content_hash: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]: ...
    def find_rule_by_file_path(
        self, file_path: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]: ...
    def insert_rule(self, row: RuleRow) -> int: ...
    def update_rule(self, rule_id: int, **fields: Any) -> None: ...
    def list_rules_with_file_paths(
        self, *, caller: Optional[str] = None
    ) -> list[RuleRow]: ...
    def list_archived_rules(
        self, *, caller: Optional[str] = None
    ) -> list[RuleRow]:
        """All soft-deleted rules. Backs the restore_rule bulk-recovery tool
        (issue #16) — archived rows are invisible to search but recoverable."""
        ...
    def soft_delete_rule(self, rule_id: int) -> None: ...
    def restore_rule(self, rule_id: int) -> None: ...

    def record_outcome(
        self, rule_id: int, actor: str, passed: bool, *, count: bool = True
    ) -> None:
        """Record one outcome report for a rule (issue #21): append a
        rule_outcomes ledger row + a rule_events row, and bump correct/incorrect
        counters only when ``count`` (author!=judge self-reports pass False)."""
        ...

    def supersede_rule(self, old_id: int, new_id: int, actor: str) -> None:
        """Soft-supersede a rule (issue #21): mark superseded, never delete."""
        ...

    def record_token_event(self, kind: str, tokens: int) -> None:
        """Append a token-ledger row (issue #37): kind is 'saved'|'spent'."""
        ...

    def token_totals(self) -> dict:
        """Return {'saved': int, 'spent': int} from the token ledger (issue #37)."""
        ...

    def list_rule_outcomes(self, rule_id: int) -> list:
        """Return [(actor, passed_bool), ...] for a rule's outcome ledger
        (issue #36) — used for late-binding trust-weighted correctness."""
        ...

    # ---- Rule provenance / audit log (issue #10) ----
    #
    # Events are emitted by the *tool layer* (add_rule / sync_rules /
    # reinforce_rule / promote_to_rule), never implicitly by the write
    # methods above. Keeping emission in the tools means bulk paths that
    # call insert_rule directly — the migrate CLI, the files watcher — do
    # NOT invent history, which honours issue #10's "no backfilled events"
    # rule for free.
    def insert_rule_event(
        self,
        rule_id: int,
        event_type: str,
        actor: str,
        *,
        content_hash: Optional[str] = None,
        source_repo: Optional[str] = None,
        source_ref: Optional[str] = None,
        source_commit: Optional[str] = None,
        note: Optional[str] = None,
    ) -> int: ...

    def list_rule_events(
        self, rule_id: int, *, limit: Optional[int] = None,
        caller: Optional[str] = None,
    ) -> list[RuleEventRow]: ...

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

    def find_by_id(
        self, entity_type: EntityType, entity_id: int,
        *, caller: Optional[str] = None,
    ) -> Optional[Any]:
        """Generic entity lookup by (type, id). Returns the appropriate
        Row dataclass (KnowledgeRow / NegativeRow / ErrorRow / RuleRow),
        or None when absent. Used by tools that need an entry back for
        response formatting."""
        ...

    # ---- Bulk iteration (used by the migrate CLI) ----
    def iter_entries(
        self, entity_type: EntityType, *, caller: Optional[str] = None,
    ) -> Iterator[Any]:
        """Yield every row of an entity type in id-ascending order.

        Returns the same dataclass shape as ``find_by_id``. Used by the
        ``mcm-engine migrate`` CLI to walk a source store row-by-row.
        Adapters MAY stream (cursor-based) or batch.
        """
        ...

    def iter_sessions(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[SessionRow]: ...

    def iter_snapshots(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[SnapshotRow]: ...

    def iter_relations(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[RelationRow]: ...

    def bump_sequences(self) -> None:
        """After a bulk load that inserted explicit ids, advance the
        adapter's id generator past the maximum existing id. Postgres
        IDENTITY columns need this; SQLite ROWID does not (no-op there).
        """
        ...

    # ---- Engine-wide counters (session.py rewire) ----
    def count_relations(self, *, caller: Optional[str] = None) -> int: ...
    def count_snapshots(self, *, caller: Optional[str] = None) -> int: ...
    def count_recent_knowledge(
        self, since_days: float, *, caller: Optional[str] = None,
    ) -> int:
        """Count knowledge entries created within the last `since_days` days."""
        ...
    def count_stale_knowledge(
        self,
        threshold_days: float = 90.0,
        *,
        caller: Optional[str] = None,
    ) -> int:
        """Count un-pinned knowledge entries older than threshold_days
        with no hit within the same window."""
        ...


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

    def search_plugin(
        self,
        scope: Any,
        query: str,
        limit: int = 10,
        *,
        caller: Optional[str] = None,
    ) -> list[str]:
        """Search a plugin-defined table described by `scope` and return
        formatted result strings.

        `scope` is a structural descriptor (the plugin layer's SearchScope
        dataclass) carrying table/column metadata. The adapter decides how
        to translate that into its own index — SQLite uses the named FTS5
        virtual table + LIKE fallback; Postgres adapters can interpret the
        same descriptor against their own tsvector index.

        MCM2-07: this replaced SearchScope.search so plugin code holds no
        SQL of its own.
        """
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
