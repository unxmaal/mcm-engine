"""Unified entry model: projection between the four legacy row types and
``Entry`` (issue #179, 4.0 program #176).

One shape for every stored thing: ``(kind, id)`` identity, a headline
``title``, an optional one-line ``summary`` and long ``body``, common
lifecycle/telemetry columns, and ``facets`` for the kind-specific named fields.
The projection is lossless both ways; ``entry_hash`` lets a migration prove it
row by row. Table-driven on the kind string: this module is the adapter
between the legacy rows and the unified model and goes away at the 5.0
contract step, so it is not a kind policy.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from .backends import (
    Entry,
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RuleRow,
)

KINDS: tuple[str, ...] = ("knowledge", "negative", "error", "rule")


def _dt(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


# ---- legacy row -> Entry ------------------------------------------------------

def _knowledge_to_entry(r: KnowledgeRow) -> Entry:
    return Entry(
        kind="knowledge", id=r.id, subkind=r.kind, title=r.topic,
        summary=r.summary, body=r.detail, tags=r.tags, project=r.project,
        pinned=bool(r.pinned), status=r.status or "active",
        superseded_by=r.superseded_by, valid_until=r.valid_until,
        hit_count=r.hit_count or 0, last_hit_at=r.last_hit_at,
        reinforcement_count=r.reinforcement_count or 0,
        created_at=r.created_at, updated_at=r.updated_at,
        source_classification=r.source_classification,
        facets={k: v for k, v in {
            "rationale": r.rationale, "alternatives": r.alternatives,
            "references": r.references,
        }.items() if v},
    )


def _negative_to_entry(r: NegativeRow) -> Entry:
    return Entry(
        kind="negative", id=r.id, title=r.category, summary=r.what_failed,
        project=r.project, pinned=bool(r.pinned), created_at=r.created_at,
        source_classification=r.source_classification,
        facets={k: v for k, v in {
            "why_failed": r.why_failed, "correct_approach": r.correct_approach,
            "severity": r.severity if r.severity != "normal" else None,
        }.items() if v},
    )


def _error_to_entry(r: ErrorRow) -> Entry:
    return Entry(
        kind="error", id=r.id, title=r.pattern, tags=r.tags, project=r.project,
        pinned=bool(r.pinned), created_at=r.created_at,
        source_classification=r.source_classification,
        facets={k: v for k, v in {
            "context": r.context, "root_cause": r.root_cause, "fix": r.fix,
        }.items() if v},
    )


def _rule_to_entry(r: RuleRow) -> Entry:
    facets: dict[str, Any] = {k: v for k, v in {
        "file_path": r.file_path, "category": r.category,
        "content_hash": r.content_hash, "created_by": r.created_by,
        "updated_by": r.updated_by, "archived_at": _dt(r.archived_at),
    }.items() if v}
    if r.archived:
        facets["archived"] = True
    if r.correct_count:
        facets["correct_count"] = r.correct_count
    if r.incorrect_count:
        facets["incorrect_count"] = r.incorrect_count
    if r.importance:
        facets["importance"] = r.importance
    if r.scope != "conditional":
        facets["scope"] = r.scope
    return Entry(
        kind="rule", id=r.id, subkind=r.kind, title=r.title,
        summary=r.description, body=r.content, tags=r.keywords,
        pinned=bool(r.pinned), status=r.status or "active",
        superseded_by=r.superseded_by, valid_until=r.valid_until,
        hit_count=r.hit_count or 0, last_hit_at=r.last_hit_at,
        reinforcement_count=r.reinforcement_count or 0,
        created_at=r.created_at, updated_at=r.updated_at,
        source_classification=r.source_classification, facets=facets,
    )


_TO_ENTRY: dict[str, Callable[[Any], Entry]] = {
    "knowledge": _knowledge_to_entry,
    "negative": _negative_to_entry,
    "error": _error_to_entry,
    "rule": _rule_to_entry,
}


def to_entry(etype: EntityType, row: Any) -> Entry:
    return _TO_ENTRY[etype.value](row)


# ---- Entry -> legacy row ------------------------------------------------------

def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _entry_to_knowledge(e: Entry) -> KnowledgeRow:
    f = e.facets
    return KnowledgeRow(
        id=e.id, topic=e.title, summary=e.summary or "", kind=e.subkind or "finding",
        detail=e.body, tags=e.tags, project=e.project,
        rationale=f.get("rationale"), alternatives=f.get("alternatives"),
        references=f.get("references"), hit_count=e.hit_count,
        last_hit_at=e.last_hit_at, reinforcement_count=e.reinforcement_count,
        pinned=e.pinned, created_at=e.created_at, updated_at=e.updated_at,
        source_classification=e.source_classification, status=e.status,
        superseded_by=e.superseded_by, valid_until=e.valid_until,
    )


def _entry_to_negative(e: Entry) -> NegativeRow:
    f = e.facets
    return NegativeRow(
        id=e.id, category=e.title, what_failed=e.summary or "",
        why_failed=f.get("why_failed"), correct_approach=f.get("correct_approach"),
        severity=f.get("severity") or "normal", project=e.project, pinned=e.pinned,
        created_at=e.created_at, source_classification=e.source_classification,
    )


def _entry_to_error(e: Entry) -> ErrorRow:
    f = e.facets
    return ErrorRow(
        id=e.id, pattern=e.title, context=f.get("context"),
        root_cause=f.get("root_cause"), fix=f.get("fix"), tags=e.tags,
        project=e.project, pinned=e.pinned, created_at=e.created_at,
        source_classification=e.source_classification,
    )


def _entry_to_rule(e: Entry) -> RuleRow:
    f = e.facets
    return RuleRow(
        id=e.id, title=e.title, keywords=e.tags or "", file_path=f.get("file_path"),
        description=e.summary, category=f.get("category"), hit_count=e.hit_count,
        last_hit_at=e.last_hit_at, reinforcement_count=e.reinforcement_count,
        pinned=e.pinned, content_hash=f.get("content_hash"),
        archived=bool(f.get("archived", False)), archived_at=_parse_dt(f.get("archived_at")),
        created_at=e.created_at, updated_at=e.updated_at, content=e.body,
        created_by=f.get("created_by"), updated_by=f.get("updated_by"),
        correct_count=int(f.get("correct_count", 0)),
        incorrect_count=int(f.get("incorrect_count", 0)),
        valid_until=e.valid_until, superseded_by=e.superseded_by, status=e.status,
        importance=int(f.get("importance", 0)), scope=f.get("scope") or "conditional",
        kind=e.subkind or "fact", source_classification=e.source_classification,
    )


_FROM_ENTRY: dict[str, Callable[[Entry], Any]] = {
    "knowledge": _entry_to_knowledge,
    "negative": _entry_to_negative,
    "error": _entry_to_error,
    "rule": _entry_to_rule,
}


def from_entry(e: Entry) -> Any:
    return _FROM_ENTRY[e.kind](e)


# ---- content hash + backfill ---------------------------------------------------

def entry_hash(e: Entry) -> str:
    """Stable digest over every projected field (timestamps as ISO text, facets
    key-sorted). Equal hashes on both sides of a backfill prove the row moved
    losslessly."""
    d = asdict(e)
    for k in ("valid_until", "last_hit_at", "created_at", "updated_at"):
        d[k] = _dt(d[k])
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class EntriesBackfillError(RuntimeError):
    """The backfill did not reproduce the legacy tables exactly. The caller
    rolls back; the legacy tables are untouched either way."""


def backfill(storage, *, verify: bool = True) -> dict[str, dict[str, int]]:
    """Copy every legacy row into ``entries`` (ids preserved; existing
    ``(kind, id)`` rows are left alone so a rerun is a no-op) and, with
    ``verify``, prove per-kind counts and per-row ``entry_hash`` agree. Run
    inside ``storage.transaction()`` so a failed verify leaves nothing behind.
    Returns {kind: {legacy, entries, inserted}}."""
    report: dict[str, dict[str, int]] = {}
    for et in EntityType:
        kind = et.value
        inserted = 0
        for row in storage.iter_entries(et):
            if storage.insert_entry(to_entry(et, row), if_absent=True):
                inserted += 1
        legacy = storage.count_by_type(et)
        unified = storage.count_entries(kind)
        report[kind] = {"legacy": legacy, "entries": unified, "inserted": inserted}
        if not verify:
            continue
        if legacy != unified:
            raise EntriesBackfillError(
                f"{kind}: legacy rows {legacy} != entries rows {unified}")
        for row in storage.iter_entries(et):
            want = to_entry(et, row)
            got = storage.find_entry(kind, row.id)
            if got is None or entry_hash(got) != entry_hash(want):
                raise EntriesBackfillError(
                    f"{kind} #{row.id}: entries row does not reproduce the legacy row")
    for kind in KINDS:
        storage.seed_entry_ids(kind)
    return report


def needs_backfill(storage) -> bool:
    """True when ``entries`` is empty while any legacy table has rows."""
    if any(storage.count_entries(k) for k in KINDS):
        return False
    return any(storage.count_by_type(et) for et in EntityType)


# ---- dual-write mirror (issue #180) -------------------------------------------

_ENTRY_COLUMNS: tuple[str, ...] = (
    "subkind", "title", "summary", "body", "tags", "project", "pinned", "status",
    "superseded_by", "valid_until", "hit_count", "last_hit_at",
    "reinforcement_count", "created_at", "updated_at", "source_classification",
    "facets",
)


def entry_fields(e: Entry) -> dict[str, Any]:
    """Every column of ``e`` except its identity, as ``update_entry`` kwargs."""
    return {k: getattr(e, k) for k in _ENTRY_COLUMNS}


def mirror_entry(storage, etype: EntityType, entry_id: int) -> str:
    """Re-project one legacy row into ``entries`` (legacy is authoritative in
    4.x). Idempotent. Call inside ``storage.transaction()`` so the legacy write
    and its mirror commit together. Returns inserted|updated|deleted|same."""
    kind = etype.value
    row = storage.find_by_id(etype, entry_id)
    if row is None:
        storage.delete_entry(kind, entry_id)
        return "deleted"
    want = to_entry(etype, row)
    got = storage.find_entry(kind, entry_id)
    if got is None:
        storage.insert_entry(want)
        storage.seed_entry_ids(kind)
        return "inserted"
    if entry_hash(got) != entry_hash(want):
        storage.update_entry(kind, entry_id, **entry_fields(want))
        return "updated"
    return "same"


def returned_id(bound: dict, ret: Any) -> tuple:
    return (ret,)


def arg_ids(*names: str) -> Callable[[dict, Any], tuple]:
    return lambda bound, ret: tuple(bound[n] for n in names)


def mirrored(etype: Optional[EntityType], ids: Callable[[dict, Any], Iterable]):
    """Decorate a storage mutation so the rows it touched are re-projected into
    ``entries`` within the same transaction. ``ids`` picks the affected ids from
    the bound arguments and the return value; ``etype=None`` reads the kind from
    the ``entity_type`` argument."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            with self.transaction():
                ret = fn(self, *args, **kwargs)
                bound = sig.bind(self, *args, **kwargs).arguments
                et = etype if etype is not None else bound["entity_type"]
                for eid in ids(bound, ret):
                    if eid is not None:
                        mirror_entry(self, et, eid)
            return ret
        return wrapper
    return deco


_INSERT_LEGACY: dict[str, str] = {
    "knowledge": "insert_knowledge",
    "negative": "insert_negative",
    "error": "insert_error",
    "rule": "insert_rule",
}


def insert_legacy(storage, etype: EntityType, row: Any) -> int:
    """Insert a legacy row of any kind (ids preserved when set). The adapter's
    mirror populates ``entries``."""
    return getattr(storage, _INSERT_LEGACY[etype.value])(row)


def divergence(storage) -> list[str]:
    """Every way ``entries`` disagrees with the legacy tables; [] when the two
    sides are identical row for row."""
    out: list[str] = []
    for et in EntityType:
        kind = et.value
        legacy = storage.count_by_type(et)
        unified = storage.count_entries(kind)
        if legacy != unified:
            out.append(f"{kind}: legacy rows {legacy} != entries rows {unified}")
        for row in storage.iter_entries(et):
            got = storage.find_entry(kind, row.id)
            if got is None:
                out.append(f"{kind} #{row.id}: missing from entries")
            elif entry_hash(got) != entry_hash(to_entry(et, row)):
                out.append(f"{kind} #{row.id}: entries row differs")
    return out


def reconcile(storage) -> dict[str, int]:
    """Make ``entries`` agree with the legacy tables, row by row, in one
    transaction. Runs at boot after the backfill so a missed mirror never
    outlives a restart. Returns {checked, inserted, updated, deleted}."""
    report = {"checked": 0, "inserted": 0, "updated": 0, "deleted": 0}
    with storage.transaction():
        for et in EntityType:
            kind = et.value
            unified_ids = [e.id for e in storage.iter_entries_unified(kind)]
            legacy_ids: set[int] = set()
            for row in storage.iter_entries(et):
                legacy_ids.add(row.id)
                report["checked"] += 1
                outcome = mirror_entry(storage, et, row.id)
                if outcome != "same":
                    report[outcome] += 1
            for eid in unified_ids:
                if eid not in legacy_ids:
                    storage.delete_entry(kind, eid)
                    report["deleted"] += 1
    return report
