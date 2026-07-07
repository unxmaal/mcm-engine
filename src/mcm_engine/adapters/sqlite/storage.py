"""Embedded SQLite StorageBackend.

The reference implementation. SQL is centralized here — tool functions
will call into this via the wired Context (MCM2-02 follow-up step that
refactors tools/*.py to use ctx.storage instead of db.execute directly).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from ...backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RelationRow,
    RuleEventRow,
    RuleRow,
    SessionRow,
    SnapshotRow,
    StorageIdentity,
)
from ...db import KnowledgeDB
from ...hierarchy import validated_metadata_updates
from ...schema import migrate_core


# ---- table mappings -----------------------------------------------------

_ENTITY_TABLE: dict[EntityType, str] = {
    EntityType.KNOWLEDGE: "knowledge",
    EntityType.NEGATIVE:  "negative_knowledge",
    EntityType.ERROR:     "errors",
    EntityType.RULE:      "rules",
}


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _knowledge_from_row(r: sqlite3.Row) -> KnowledgeRow:
    return KnowledgeRow(
        id=r["id"],
        topic=r["topic"],
        summary=r["summary"],
        kind=r["kind"],
        detail=r["detail"],
        tags=r["tags"],
        project=r["project"],
        rationale=r["rationale"],
        alternatives=r["alternatives"],
        hit_count=r["hit_count"] or 0,
        last_hit_at=_parse_dt(r["last_hit_at"]),
        reinforcement_count=r["reinforcement_count"] or 0,
        pinned=bool(r["pinned"]),
        created_at=_parse_dt(r["created_at"]),
        updated_at=_parse_dt(r["updated_at"]),
    )


def _negative_from_row(r: sqlite3.Row) -> NegativeRow:
    return NegativeRow(
        id=r["id"],
        category=r["category"],
        what_failed=r["what_failed"],
        why_failed=r["why_failed"],
        correct_approach=r["correct_approach"],
        severity=r["severity"],
        project=r["project"],
        pinned=bool(r["pinned"]),
        created_at=_parse_dt(r["created_at"]),
    )


def _error_from_row(r: sqlite3.Row) -> ErrorRow:
    return ErrorRow(
        id=r["id"],
        pattern=r["pattern"],
        context=r["context"],
        root_cause=r["root_cause"],
        fix=r["fix"],
        tags=r["tags"],
        project=r["project"],
        pinned=bool(r["pinned"]),
        created_at=_parse_dt(r["created_at"]),
    )


def _col(r: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Tolerant column access — returns default if the column is absent from
    the row (e.g. a query written before a migration added the column)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def _rule_from_row(r: sqlite3.Row) -> RuleRow:
    return RuleRow(
        id=r["id"],
        title=r["title"],
        keywords=r["keywords"],
        file_path=r["file_path"],
        description=r["description"],
        category=r["category"],
        hit_count=r["hit_count"] or 0,
        last_hit_at=_parse_dt(r["last_hit_at"]),
        reinforcement_count=r["reinforcement_count"] or 0,
        pinned=bool(r["pinned"]),
        content_hash=r["content_hash"],
        archived=bool(r["archived"]),
        archived_at=_parse_dt(r["archived_at"]),
        created_at=_parse_dt(r["created_at"]),
        updated_at=_parse_dt(r["updated_at"]),
        content=r["content"],
        created_by=r["created_by"],
        updated_by=r["updated_by"],
        correct_count=_col(r, "correct_count", 0) or 0,
        incorrect_count=_col(r, "incorrect_count", 0) or 0,
        valid_until=_parse_dt(_col(r, "valid_until")),
        superseded_by=_col(r, "superseded_by"),
        status=_col(r, "status", "active") or "active",
        importance=_col(r, "importance", 0) or 0,
        scope=_col(r, "scope", "conditional") or "conditional",
        kind=_col(r, "kind", "fact") or "fact",
    )


def _rule_event_from_row(r: sqlite3.Row) -> RuleEventRow:
    return RuleEventRow(
        id=r["id"],
        rule_id=r["rule_id"],
        event_type=r["event_type"],
        actor=r["actor"],
        at=_parse_dt(r["at"]),
        content_hash=r["content_hash"],
        source_repo=r["source_repo"],
        source_ref=r["source_ref"],
        source_commit=r["source_commit"],
        note=r["note"],
    )


def _session_from_row(r: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id=r["id"],
        status=r["status"],
        current_task=r["current_task"],
        findings_summary=r["findings_summary"],
        next_steps=r["next_steps"],
        blockers=r["blockers"],
        context_snapshot=r["context_snapshot"],
        created_at=_parse_dt(r["created_at"]),
    )


def _snapshot_from_row(r: sqlite3.Row) -> SnapshotRow:
    return SnapshotRow(
        id=r["id"],
        sequence_num=r["sequence_num"],
        session_id=r["session_id"],
        goal=r["goal"],
        progress=r["progress"],
        open_questions=r["open_questions"],
        blockers=r["blockers"],
        next_steps=r["next_steps"],
        active_files=r["active_files"],
        key_decisions=r["key_decisions"],
        created_at=_parse_dt(r["created_at"]),
    )


def _relation_from_row(r: sqlite3.Row) -> RelationRow:
    return RelationRow(
        id=r["id"],
        source_type=EntityType(r["source_type"]),
        source_id=r["source_id"],
        target_type=EntityType(r["target_type"]),
        target_id=r["target_id"],
        relation=r["relation"],
        note=r["note"],
        created_at=_parse_dt(r["created_at"]),
    )


# ---- SqliteStorage ------------------------------------------------------


class SqliteStorage:
    """StorageBackend implementation against SQLite + FTS5."""

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(self, db_path: str | Any = ":memory:", db: Optional[KnowledgeDB] = None):
        # Either share a KnowledgeDB instance with sibling adapters, or
        # open our own from db_path.
        self._db = db if db is not None else KnowledgeDB(db_path)

    @property
    def identity(self) -> StorageIdentity:
        p = str(self._db.db_path)
        location = p if p == ":memory:" else str(Path(p).resolve())
        return StorageIdentity("sqlite", location)

    # ---- Schema management ----

    def ensure_schema(self) -> None:
        migrate_core(self._db)

    # ---- Transactions ----

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one atomic unit. The per-method commits that
        normally fire after each write are deferred; the whole block commits
        once on clean exit and rolls back on any exception. Delegates to
        ``KnowledgeDB.transaction`` so the connection lock is held across the
        whole block (issue #83 — no sibling thread can interleave a write)."""
        with self._db.transaction():
            yield

    # ---- Knowledge ----

    def find_knowledge_by_topic_kind(
        self, topic: str, kind: str, *, caller: Optional[str] = None
    ) -> Optional[KnowledgeRow]:
        # `caller` accepted as no-op pass-through (MCM2-05).
        row = self._db.execute(
            "SELECT * FROM knowledge WHERE topic = ? AND kind = ?",
            (topic, kind),
        ).fetchone()
        return _knowledge_from_row(row) if row else None

    def find_similar_knowledge(
        self, topic: str, *, caller: Optional[str] = None
    ) -> Optional[KnowledgeRow]:
        from ...db import sanitize_fts

        try:
            fts_query = sanitize_fts(topic)
            r = self._db.execute(
                "SELECT k.* FROM knowledge_fts f JOIN knowledge k ON f.rowid = k.id "
                "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT 1",
                (fts_query,),
            ).fetchone()
            return _knowledge_from_row(r) if r else None
        except sqlite3.OperationalError:
            return None

    def list_unlinked_knowledge(
        self, limit: int = 5, *, caller: Optional[str] = None
    ) -> list[KnowledgeRow]:
        """Most-recent knowledge rows that participate in no relation (neither
        source nor target). These are `link_knowledge` candidates surfaced at
        session end. `caller` accepted as no-op pass-through (MCM2-05)."""
        rows = self._db.execute(
            "SELECT k.* FROM knowledge k WHERE NOT EXISTS ("
            "  SELECT 1 FROM relations r WHERE "
            "  (r.source_type = 'knowledge' AND r.source_id = k.id) OR "
            "  (r.target_type = 'knowledge' AND r.target_id = k.id)) "
            "ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_knowledge_from_row(r) for r in rows]

    def list_promotable_knowledge(
        self, min_hits: int = 5, limit: int = 5, *, caller: Optional[str] = None
    ) -> list[KnowledgeRow]:
        """Knowledge rows hit at least `min_hits` times, most-hit first. These
        are `promote_to_rule` candidates: repeatedly-useful findings that have
        earned a place as a rule. `caller` accepted as no-op pass-through."""
        rows = self._db.execute(
            "SELECT * FROM knowledge WHERE hit_count >= ? "
            "ORDER BY hit_count DESC, id DESC LIMIT ?",
            (min_hits, limit),
        ).fetchall()
        return [_knowledge_from_row(r) for r in rows]

    def insert_knowledge(self, row: KnowledgeRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO knowledge "
                "(id, topic, kind, summary, detail, tags, project, rationale, alternatives) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.topic, row.kind, row.summary, row.detail, row.tags,
                 row.project, row.rationale, row.alternatives),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO knowledge "
                "(topic, kind, summary, detail, tags, project, rationale, alternatives) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row.topic, row.kind, row.summary, row.detail, row.tags,
                 row.project, row.rationale, row.alternatives),
            )
        self._db.commit()
        return cur.lastrowid

    def update_knowledge(self, knowledge_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"topic", "kind", "summary", "detail", "tags", "project",
                   "rationale", "alternatives"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown knowledge fields: {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = tuple(fields.values()) + (knowledge_id,)
        self._db.execute_write(
            f"UPDATE knowledge SET {cols}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        self._db.commit()

    # ---- Negative ----

    def insert_negative(self, row: NegativeRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO negative_knowledge "
                "(id, category, what_failed, why_failed, correct_approach, severity, project) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.category, row.what_failed, row.why_failed,
                 row.correct_approach, row.severity, row.project),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO negative_knowledge "
                "(category, what_failed, why_failed, correct_approach, severity, project) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.category, row.what_failed, row.why_failed,
                 row.correct_approach, row.severity, row.project),
            )
        self._db.commit()
        return cur.lastrowid

    # ---- Errors ----

    def insert_error(self, row: ErrorRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO errors (id, pattern, context, root_cause, fix, tags, project) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.pattern, row.context, row.root_cause, row.fix, row.tags, row.project),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO errors (pattern, context, root_cause, fix, tags, project) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.pattern, row.context, row.root_cause, row.fix, row.tags, row.project),
            )
        self._db.commit()
        return cur.lastrowid

    # ---- Rules ----

    def find_rule_by_title(
        self, title: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]:
        r = self._db.execute(
            "SELECT * FROM rules WHERE title = ?", (title,)
        ).fetchone()
        return _rule_from_row(r) if r else None

    def find_rule_by_file_path(
        self, file_path: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]:
        r = self._db.execute(
            "SELECT * FROM rules WHERE file_path = ?", (file_path,)
        ).fetchone()
        return _rule_from_row(r) if r else None

    def find_rule_by_content_hash(
        self, content_hash: str, *, caller: Optional[str] = None
    ) -> Optional[RuleRow]:
        """First ACTIVE rule (not archived, not superseded) with this exact
        content hash — the net-new guard for ingest (issue #54): same content,
        even under a different title, is a duplicate, not a new rule."""
        if not content_hash:
            return None
        r = self._db.execute(
            "SELECT * FROM rules WHERE content_hash = ? "
            "AND NOT COALESCE(archived, 0) "
            "AND COALESCE(status, 'active') = 'active' LIMIT 1",
            (content_hash,),
        ).fetchone()
        return _rule_from_row(r) if r else None

    def insert_rule(self, row: RuleRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO rules "
                "(id, title, keywords, file_path, description, category, content_hash, "
                " content, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.title, row.keywords, row.file_path, row.description,
                 row.category, row.content_hash, row.content, row.created_by,
                 row.updated_by),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO rules "
                "(title, keywords, file_path, description, category, content_hash, "
                " content, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.title, row.keywords, row.file_path, row.description,
                 row.category, row.content_hash, row.content, row.created_by,
                 row.updated_by),
            )
        self._db.commit()
        return cur.lastrowid

    def update_rule(self, rule_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"title", "keywords", "file_path", "description", "category",
                   "content_hash", "content", "created_by", "updated_by"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown rule fields: {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = tuple(fields.values()) + (rule_id,)
        self._db.execute_write(
            f"UPDATE rules SET {cols}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        self._db.commit()

    def list_rules_with_file_paths(
        self, *, caller: Optional[str] = None
    ) -> list[RuleRow]:
        rows = self._db.execute(
            "SELECT * FROM rules WHERE file_path IS NOT NULL AND file_path != ''"
        ).fetchall()
        return [_rule_from_row(r) for r in rows]

    def list_rules(
        self, *, include_archived: bool = False, min_importance: int = 0,
        limit: Optional[int] = None, caller: Optional[str] = None,
    ) -> list[RuleRow]:
        """Full-column rule read for the hierarchy tuning surface (issue #64).
        Ordered importance-first so the highest-binding rules — and any tier
        inflation — sort to the top. RuleRow already carries the derived
        signals (hit_count/reinforcement_count/correct/incorrect)."""
        clauses = ["COALESCE(importance, 0) >= ?"]
        params: list[Any] = [min_importance]
        if not include_archived:
            clauses.append("NOT COALESCE(archived, 0)")
        sql = (
            f"SELECT * FROM rules WHERE {' AND '.join(clauses)} "
            f"ORDER BY COALESCE(importance, 0) DESC, id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._db.execute(sql, tuple(params)).fetchall()
        return [_rule_from_row(r) for r in rows]

    def set_rule_metadata(
        self, rule_id: int, *,
        importance: Optional[int] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        category: Optional[str] = None,
        actor: str = "nobody",
    ) -> Optional[RuleRow]:
        """Set the hierarchy axes (issue #64). Validates against the vocab
        (raising ValueError before any write), updates only the provided
        fields, stamps updated_by, and emits an audited 'metadata' rule_events
        row. Atomic. Returns the updated row, the unchanged row if nothing was
        provided, or None if the rule is absent."""
        updates = validated_metadata_updates(importance, scope, kind, category)
        if self.find_by_id(EntityType.RULE, rule_id) is None:
            return None
        if not updates:
            return self.find_by_id(EntityType.RULE, rule_id)
        cols = ", ".join(f"{k} = ?" for k in updates)
        note = ", ".join(f"{k}={v}" for k, v in updates.items())
        values = tuple(updates.values()) + (actor or "nobody", rule_id)
        with self.transaction():
            self._db.execute_write(
                f"UPDATE rules SET {cols}, updated_by = ?, "
                f"updated_at = datetime('now') WHERE id = ?",
                values,
            )
            self._db.execute_write(
                "INSERT INTO rule_events (rule_id, event_type, actor, note) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, "metadata", actor or "nobody", note),
            )
        return self.find_by_id(EntityType.RULE, rule_id)

    def list_archived_rules(
        self, *, caller: Optional[str] = None
    ) -> list[RuleRow]:
        rows = self._db.execute(
            "SELECT * FROM rules WHERE archived = 1"
        ).fetchall()
        return [_rule_from_row(r) for r in rows]

    def soft_delete_rule(self, rule_id: int) -> None:
        self._db.execute_write(
            "UPDATE rules SET archived = 1, archived_at = datetime('now'), "
            "updated_at = datetime('now') WHERE id = ?",
            (rule_id,),
        )
        self._db.commit()

    def restore_rule(self, rule_id: int) -> None:
        self._db.execute_write(
            "UPDATE rules SET archived = 0, archived_at = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (rule_id,),
        )
        self._db.commit()

    def record_outcome(
        self, rule_id: int, actor: str, passed: bool, *, count: bool = True
    ) -> None:
        """Record one outcome report (issue #21). Always appends a rule_outcomes
        ledger row + a rule_events row; bumps correct/incorrect only when
        ``count`` — the author!=judge guard passes count=False for self-reports
        so they are logged but do not move the correctness signal. Atomic."""
        p = 1 if passed else 0
        with self.transaction():
            self._db.execute_write(
                "INSERT INTO rule_outcomes (rule_id, actor, passed) VALUES (?, ?, ?)",
                (rule_id, actor or "nobody", p),
            )
            if count:
                col = "correct_count" if passed else "incorrect_count"
                self._db.execute_write(
                    f"UPDATE rules SET {col} = {col} + 1 WHERE id = ?", (rule_id,)
                )
            note = ("passed" if passed else "failed") + (
                "" if count else " (self-report, uncounted)"
            )
            self._db.execute_write(
                "INSERT INTO rule_events (rule_id, event_type, actor, note) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, "outcome", actor or "nobody", note),
            )

    def record_token_event(self, kind: str, tokens: int) -> None:
        """Issue #37 — append one token-ledger row (best-effort telemetry)."""
        with self.transaction():
            self._db.execute_write(
                "INSERT INTO token_ledger (kind, tokens) VALUES (?, ?)",
                (kind, int(tokens)),
            )

    def token_totals(self) -> dict:
        """Issue #37 — {'saved': int, 'spent': int} from the token ledger."""
        totals = {"saved": 0, "spent": 0}
        rows = self._db.execute(
            "SELECT kind, COALESCE(SUM(tokens), 0) AS total "
            "FROM token_ledger GROUP BY kind"
        ).fetchall()
        for r in rows:
            totals[r["kind"]] = int(r["total"])
        return totals

    def list_rule_outcomes(self, rule_id: int) -> list:
        """Issue #36 — (actor, passed) rows for a rule, oldest first."""
        rows = self._db.execute(
            "SELECT actor, passed FROM rule_outcomes WHERE rule_id = ? ORDER BY id",
            (rule_id,),
        ).fetchall()
        return [(r["actor"], bool(r["passed"])) for r in rows]

    def supersede_rule(self, old_id: int, new_id: int, actor: str) -> None:
        """Soft-supersede a rule (issue #21): mark superseded rather than
        deleting, so it drops out of default retrieval but stays inspectable."""
        with self.transaction():
            self._db.execute_write(
                "UPDATE rules SET valid_until = datetime('now'), superseded_by = ?, "
                "status = 'superseded', updated_at = datetime('now') WHERE id = ?",
                (new_id, old_id),
            )
            self._db.execute_write(
                "INSERT INTO rule_events (rule_id, event_type, actor, note) "
                "VALUES (?, ?, ?, ?)",
                (old_id, "superseded", actor or "nobody", f"superseded_by:{new_id}"),
            )

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
    ) -> int:
        cur = self._db.execute_write(
            "INSERT INTO rule_events "
            "(rule_id, event_type, actor, content_hash, source_repo, source_ref, "
            " source_commit, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rule_id, event_type, actor or "nobody", content_hash, source_repo,
             source_ref, source_commit, note),
        )
        self._db.commit()
        return cur.lastrowid

    def list_rule_events(
        self, rule_id: int, *, limit: Optional[int] = None,
        caller: Optional[str] = None,
    ) -> list[RuleEventRow]:
        sql = (
            "SELECT * FROM rule_events WHERE rule_id = ? "
            "ORDER BY at DESC, id DESC"
        )
        params: tuple[Any, ...] = (rule_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (rule_id, limit)
        rows = self._db.execute(sql, params).fetchall()
        return [_rule_event_from_row(r) for r in rows]

    # ---- Relations ----

    def insert_relation(self, row: RelationRow) -> Optional[int]:
        try:
            if row.id:
                cur = self._db.execute_write(
                    "INSERT INTO relations "
                    "(id, source_type, source_id, target_type, target_id, relation, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row.id, row.source_type.value, row.source_id,
                     row.target_type.value, row.target_id, row.relation, row.note),
                )
            else:
                cur = self._db.execute_write(
                    "INSERT INTO relations "
                    "(source_type, source_id, target_type, target_id, relation, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row.source_type.value, row.source_id, row.target_type.value,
                     row.target_id, row.relation, row.note),
                )
            self._db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # UNIQUE constraint violation — relation already exists.
            return None

    def list_outgoing_relations(
        self, source_type: EntityType, source_id: int,
        *, caller: Optional[str] = None,
    ) -> list[RelationRow]:
        rows = self._db.execute(
            "SELECT * FROM relations WHERE source_type = ? AND source_id = ?",
            (source_type.value, source_id),
        ).fetchall()
        return [_relation_from_row(r) for r in rows]

    def list_incoming_relations(
        self, target_type: EntityType, target_id: int,
        *, caller: Optional[str] = None,
    ) -> list[RelationRow]:
        rows = self._db.execute(
            "SELECT * FROM relations WHERE target_type = ? AND target_id = ?",
            (target_type.value, target_id),
        ).fetchall()
        return [_relation_from_row(r) for r in rows]

    # ---- Sessions + snapshots ----

    def insert_session(self, row: SessionRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO sessions "
                "(id, status, current_task, findings_summary, next_steps, blockers, context_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.status, row.current_task, row.findings_summary,
                 row.next_steps, row.blockers, row.context_snapshot),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO sessions "
                "(status, current_task, findings_summary, next_steps, blockers, context_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.status, row.current_task, row.findings_summary,
                 row.next_steps, row.blockers, row.context_snapshot),
            )
        self._db.commit()
        return cur.lastrowid

    def get_last_session(
        self, *, caller: Optional[str] = None
    ) -> Optional[SessionRow]:
        r = self._db.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _session_from_row(r) if r else None

    def next_snapshot_seq(self, session_id: Optional[int]) -> int:
        if session_id is None:
            r = self._db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 AS next_seq "
                "FROM snapshots WHERE session_id IS NULL"
            ).fetchone()
        else:
            r = self._db.execute(
                "SELECT COALESCE(MAX(sequence_num), 0) + 1 AS next_seq "
                "FROM snapshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return r["next_seq"]

    def insert_snapshot(self, row: SnapshotRow) -> int:
        if row.id:
            cur = self._db.execute_write(
                "INSERT INTO snapshots "
                "(id, session_id, sequence_num, goal, progress, open_questions, blockers, "
                " next_steps, active_files, key_decisions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.session_id, row.sequence_num, row.goal, row.progress,
                 row.open_questions, row.blockers, row.next_steps,
                 row.active_files, row.key_decisions),
            )
        else:
            cur = self._db.execute_write(
                "INSERT INTO snapshots "
                "(session_id, sequence_num, goal, progress, open_questions, blockers, "
                " next_steps, active_files, key_decisions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.session_id, row.sequence_num, row.goal, row.progress,
                 row.open_questions, row.blockers, row.next_steps,
                 row.active_files, row.key_decisions),
            )
        self._db.commit()
        return cur.lastrowid

    def get_last_snapshot(
        self, *, caller: Optional[str] = None
    ) -> Optional[SnapshotRow]:
        r = self._db.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _snapshot_from_row(r) if r else None

    # ---- Cross-entity (enum-driven) ----

    def set_pinned(self, entity_type: EntityType, entity_id: int, value: bool) -> None:
        table = _ENTITY_TABLE[entity_type]
        self._db.execute_write(
            f"UPDATE {table} SET pinned = ? WHERE id = ?",
            (1 if value else 0, entity_id),
        )
        self._db.commit()

    def count_by_type(
        self,
        entity_type: EntityType,
        *,
        project: Optional[str] = None,
        pinned: Optional[bool] = None,
        caller: Optional[str] = None,
    ) -> int:
        table = _ENTITY_TABLE[entity_type]
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            # `project` column doesn't exist on rules — caller error if requested.
            if table == "rules":
                raise ValueError("rules has no project column; do not pass project=")
            # Strict equality: this is the count_by_type contract. Search uses
            # the legacy OR-with-NULL semantics in its own filter.
            clauses.append("project = ?")
            params.append(project)
        if pinned is not None:
            clauses.append("pinned = ?")
            params.append(1 if pinned else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        r = self._db.execute(
            f"SELECT COUNT(*) AS cnt FROM {table}{where}",
            tuple(params),
        ).fetchone()
        return r["cnt"]

    def list_pinned(
        self, entity_type: EntityType, *, caller: Optional[str] = None
    ) -> list[Any]:
        table = _ENTITY_TABLE[entity_type]
        rows = self._db.execute(
            f"SELECT * FROM {table} WHERE pinned = 1 ORDER BY id"
        ).fetchall()
        # Hydrate to the appropriate dataclass.
        if entity_type is EntityType.KNOWLEDGE:
            return [_knowledge_from_row(r) for r in rows]
        if entity_type is EntityType.NEGATIVE:
            return [_negative_from_row(r) for r in rows]
        if entity_type is EntityType.ERROR:
            return [_error_from_row(r) for r in rows]
        if entity_type is EntityType.RULE:
            return [_rule_from_row(r) for r in rows]
        raise ValueError(f"unknown EntityType {entity_type}")

    def entry_exists(
        self, entity_type: EntityType, entity_id: int,
        *, caller: Optional[str] = None,
    ) -> bool:
        table = _ENTITY_TABLE[entity_type]
        r = self._db.execute(
            f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1",
            (entity_id,),
        ).fetchone()
        return r is not None

    def find_by_id(
        self, entity_type: EntityType, entity_id: int,
        *, caller: Optional[str] = None,
    ) -> Optional[Any]:
        table = _ENTITY_TABLE[entity_type]
        row = self._db.execute(
            f"SELECT * FROM {table} WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        if entity_type is EntityType.KNOWLEDGE:
            return _knowledge_from_row(row)
        if entity_type is EntityType.NEGATIVE:
            return _negative_from_row(row)
        if entity_type is EntityType.ERROR:
            return _error_from_row(row)
        if entity_type is EntityType.RULE:
            return _rule_from_row(row)
        raise ValueError(f"unknown EntityType {entity_type}")

    # ---- Bulk iteration (migrate CLI) ----

    def iter_entries(
        self, entity_type: EntityType, *, caller: Optional[str] = None,
    ) -> Iterator[Any]:
        table = _ENTITY_TABLE[entity_type]
        hydrate = {
            EntityType.KNOWLEDGE: _knowledge_from_row,
            EntityType.NEGATIVE:  _negative_from_row,
            EntityType.ERROR:     _error_from_row,
            EntityType.RULE:      _rule_from_row,
        }[entity_type]
        rows = self._db.execute(
            f"SELECT * FROM {table} ORDER BY id"
        ).fetchall()
        for r in rows:
            yield hydrate(r)

    def iter_sessions(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[SessionRow]:
        rows = self._db.execute(
            "SELECT * FROM sessions ORDER BY id"
        ).fetchall()
        for r in rows:
            yield _session_from_row(r)

    def iter_snapshots(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[SnapshotRow]:
        rows = self._db.execute(
            "SELECT * FROM snapshots ORDER BY id"
        ).fetchall()
        for r in rows:
            yield _snapshot_from_row(r)

    def iter_relations(
        self, *, caller: Optional[str] = None,
    ) -> Iterator[RelationRow]:
        rows = self._db.execute(
            "SELECT * FROM relations ORDER BY id"
        ).fetchall()
        for r in rows:
            yield _relation_from_row(r)

    def bump_sequences(self) -> None:
        """No-op on SQLite — ROWID auto-advances past any explicit id."""
        return None

    # ---- Engine-wide counters ----

    def count_relations(self, *, caller: Optional[str] = None) -> int:
        r = self._db.execute("SELECT COUNT(*) AS cnt FROM relations").fetchone()
        return r["cnt"]

    def count_snapshots(self, *, caller: Optional[str] = None) -> int:
        r = self._db.execute("SELECT COUNT(*) AS cnt FROM snapshots").fetchone()
        return r["cnt"]

    def count_recent_knowledge(
        self, since_days: float, *, caller: Optional[str] = None,
    ) -> int:
        # julianday-based to match SQLite's recency model exactly.
        r = self._db.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge "
            "WHERE julianday('now') - julianday(created_at) < ?",
            (float(since_days),),
        ).fetchone()
        return r["cnt"]

    def count_stale_knowledge(
        self,
        threshold_days: float = 90.0,
        *,
        caller: Optional[str] = None,
    ) -> int:
        r = self._db.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge "
            "WHERE julianday('now') - julianday(created_at) > ? "
            "AND (last_hit_at IS NULL "
            "     OR julianday('now') - julianday(last_hit_at) > ?) "
            "AND pinned = 0",
            (float(threshold_days), float(threshold_days)),
        ).fetchone()
        return r["cnt"]
