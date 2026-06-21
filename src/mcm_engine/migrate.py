"""Adapter-agnostic storage migration (MCM2-11).

The migrator reads rows from a source StorageBackend and writes them to a
destination StorageBackend, preserving ids. After the bulk load, the
destination's ``bump_sequences()`` is called so future auto-generated ids
don't collide.

DSN format:
    sqlite:///path/to/knowledge.db
    postgres://user:pass@host:port/database
    postgresql://user:pass@host:port/database

The migrator is one-shot and refuses a non-empty destination by default
(use ``--force`` to override). On success, prints per-table counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .backends import EntityType, StorageBackend


_ENTITY_ORDER: tuple[EntityType, ...] = (
    # Order matters when foreign keys are enforced — independent tables
    # first. sessions before snapshots; everything before relations.
    EntityType.KNOWLEDGE,
    EntityType.NEGATIVE,
    EntityType.ERROR,
    EntityType.RULE,
)


@dataclass
class MigrationReport:
    """Per-table row counts of what was copied."""
    knowledge: int = 0
    negative: int = 0
    errors: int = 0
    rules: int = 0
    sessions: int = 0
    snapshots: int = 0
    relations: int = 0

    def total(self) -> int:
        return (self.knowledge + self.negative + self.errors + self.rules
                + self.sessions + self.snapshots + self.relations)


def open_storage(dsn: str) -> StorageBackend:
    """Resolve a DSN to a StorageBackend instance.

    SQLite: ``sqlite:///abs/path`` or ``sqlite:relative/path``.
    Postgres: ``postgresql://...`` (passed through to psycopg).
    """
    parsed = urlparse(dsn)
    scheme = parsed.scheme.lower()

    if scheme == "sqlite":
        # urlparse treats ``sqlite:///foo`` as netloc='' path='/foo' and
        # ``sqlite:foo`` as path='foo'. Both routes give us the path.
        path = parsed.path or parsed.netloc
        if dsn.startswith("sqlite:///"):
            # Absolute / leave as-is including leading /
            pass
        elif path.startswith("/"):
            # already absolute
            pass
        from .adapters.sqlite.storage import SqliteStorage
        return SqliteStorage(db_path=path)

    if scheme in ("postgres", "postgresql"):
        from .adapters.postgres.storage import PostgresStorage
        return PostgresStorage(dsn=dsn)

    raise ValueError(
        f"unknown DSN scheme '{scheme}' (expected sqlite:// or postgresql://)"
    )


def _is_destination_empty(dest: StorageBackend) -> bool:
    """True when every owned table has zero rows."""
    for et in EntityType:
        if dest.count_by_type(et) > 0:
            return False
    if dest.count_relations() > 0:
        return False
    if dest.count_snapshots() > 0:
        return False
    if dest.get_last_session() is not None:
        return False
    return True


def migrate(
    source: StorageBackend,
    dest: StorageBackend,
    *,
    force: bool = False,
) -> MigrationReport:
    """Copy every row from ``source`` to ``dest`` with ids preserved.

    Raises ValueError if dest is non-empty and force=False.
    """
    source.ensure_schema()
    dest.ensure_schema()

    if not _is_destination_empty(dest):
        if not force:
            raise ValueError(
                "destination is non-empty; pass force=True to overwrite. "
                "WARNING: this implementation does not TRUNCATE the dest. "
                "To start fresh, drop and recreate the destination tables "
                "before invoking migrate()."
            )

    report = MigrationReport()

    # Entity-typed rows (the four pin-able tables).
    for et in _ENTITY_ORDER:
        for row in source.iter_entries(et):
            if et is EntityType.KNOWLEDGE:
                dest.insert_knowledge(row)
                report.knowledge += 1
            elif et is EntityType.NEGATIVE:
                dest.insert_negative(row)
                report.negative += 1
            elif et is EntityType.ERROR:
                dest.insert_error(row)
                report.errors += 1
            elif et is EntityType.RULE:
                dest.insert_rule(row)
                report.rules += 1

    # Sessions before snapshots (FK).
    for row in source.iter_sessions():
        dest.insert_session(row)
        report.sessions += 1

    for row in source.iter_snapshots():
        dest.insert_snapshot(row)
        report.snapshots += 1

    # Relations last — referenced rows must exist by the time we get here.
    for row in source.iter_relations():
        result = dest.insert_relation(row)
        # On UNIQUE collision insert_relation returns None; with a fresh
        # dest that shouldn't happen, but ignore quietly if it does.
        if result is not None:
            report.relations += 1

    # Advance every IDENTITY past MAX(id) so future inserts don't collide.
    dest.bump_sequences()
    return report


def format_report(report: MigrationReport) -> str:
    lines = [
        f"  knowledge:  {report.knowledge}",
        f"  negative:   {report.negative}",
        f"  errors:     {report.errors}",
        f"  rules:      {report.rules}",
        f"  sessions:   {report.sessions}",
        f"  snapshots:  {report.snapshots}",
        f"  relations:  {report.relations}",
        f"  ----------",
        f"  total:      {report.total()}",
    ]
    return "\n".join(lines)
