"""MCM2-11: storage migration end-to-end.

The migrator is exercised in two configurations:
  - SQLite source -> SQLite destination (in-process, always runs)
  - SQLite source -> Postgres destination (skipped without Docker)

Both verify the same shape: ids preserved, every row landed, sequences
bumped so future inserts don't collide.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.backends import (
    EntityType,
    ErrorRow,
    KnowledgeRow,
    NegativeRow,
    RelationRow,
    RuleRow,
    SessionRow,
    SnapshotRow,
)
from mcm_engine.migrate import migrate, open_storage


DEFAULT_PG_DSN = "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
TEST_PG_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN", DEFAULT_PG_DSN)


def _postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        import psycopg
        with psycopg.connect(TEST_PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _seed(storage):
    """Insert a representative row in every table; returns the ids."""
    k1 = storage.insert_knowledge(KnowledgeRow(
        id=0, topic="postgres tsvector", summary="how-to", kind="finding",
        project="alpha", tags="postgres,fts",
    ))
    k2 = storage.insert_knowledge(KnowledgeRow(
        id=0, topic="sqlite fts5", summary="how-to", kind="decision",
    ))
    n1 = storage.insert_negative(NegativeRow(
        id=0, category="schema", what_failed="missing GIN index",
    ))
    e1 = storage.insert_error(ErrorRow(
        id=0, pattern="OperationalError", root_cause="schema drift",
    ))
    r1 = storage.insert_rule(RuleRow(
        id=0, title="prefer GIN over GiST for tsvector",
        keywords="postgres,gin,tsvector",
        file_path="rules/postgres/gin.md",
    ))

    s1 = storage.insert_session(SessionRow(
        id=0, status="working", current_task="migrate",
    ))
    storage.insert_snapshot(SnapshotRow(
        id=0, sequence_num=1, session_id=s1, goal="seed",
    ))

    storage.insert_relation(RelationRow(
        id=0,
        source_type=EntityType.KNOWLEDGE, source_id=k1,
        target_type=EntityType.RULE,      target_id=r1,
        relation="supersedes",
    ))

    return {"k1": k1, "k2": k2, "n1": n1, "e1": e1, "r1": r1, "s1": s1}


def _make_sqlite(tmp_path, name):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    p = tmp_path / name
    s = SqliteStorage(db_path=str(p))
    s.ensure_schema()
    return s


# ---------------------------------------------------------------------------
# SQLite -> SQLite
# ---------------------------------------------------------------------------


def test_migrate_sqlite_to_sqlite_preserves_counts(tmp_path):
    src = _make_sqlite(tmp_path, "src.db")
    dst = _make_sqlite(tmp_path, "dst.db")
    _seed(src)

    report = migrate(src, dst)

    assert report.knowledge == 2
    assert report.negative == 1
    assert report.errors == 1
    assert report.rules == 1
    assert report.sessions == 1
    assert report.snapshots == 1
    assert report.relations == 1
    assert report.total() == 8


def test_migrate_sqlite_to_sqlite_preserves_ids(tmp_path):
    src = _make_sqlite(tmp_path, "src.db")
    dst = _make_sqlite(tmp_path, "dst.db")
    ids = _seed(src)

    migrate(src, dst)

    # The exact ids the source assigned must be reachable on the dest.
    assert dst.entry_exists(EntityType.KNOWLEDGE, ids["k1"])
    assert dst.entry_exists(EntityType.KNOWLEDGE, ids["k2"])
    assert dst.entry_exists(EntityType.NEGATIVE, ids["n1"])
    assert dst.entry_exists(EntityType.ERROR, ids["e1"])
    assert dst.entry_exists(EntityType.RULE, ids["r1"])

    k1 = dst.find_by_id(EntityType.KNOWLEDGE, ids["k1"])
    assert k1.topic == "postgres tsvector"
    assert k1.tags == "postgres,fts"
    assert k1.project == "alpha"


def test_migrate_refuses_nonempty_destination(tmp_path):
    src = _make_sqlite(tmp_path, "src.db")
    dst = _make_sqlite(tmp_path, "dst.db")
    _seed(src)
    # Seed dst too so it's non-empty.
    dst.insert_knowledge(KnowledgeRow(id=0, topic="existing", summary="x", kind="finding"))

    with pytest.raises(ValueError, match="non-empty"):
        migrate(src, dst)


def test_migrate_force_allows_nonempty_destination(tmp_path):
    src = _make_sqlite(tmp_path, "src.db")
    dst = _make_sqlite(tmp_path, "dst.db")
    _seed(src)
    # Pre-seed dst with an id well above any the source will assign so
    # the merge doesn't collide. Force-mode appends; it does not dedupe.
    dst.insert_knowledge(KnowledgeRow(
        id=999, topic="existing", summary="x", kind="finding",
    ))

    report = migrate(src, dst, force=True)
    assert report.knowledge == 2  # only the source rows are counted in the report
    # Total count on dst is now src.knowledge + the one pre-existing row.
    assert dst.count_by_type(EntityType.KNOWLEDGE) == 3


def test_migrate_after_bump_sequences_new_inserts_dont_collide(tmp_path):
    """After migrate(), inserting with id=0 must use an id past every
    migrated id — not collide with one."""
    src = _make_sqlite(tmp_path, "src.db")
    dst = _make_sqlite(tmp_path, "dst.db")
    ids = _seed(src)
    migrate(src, dst)

    new_id = dst.insert_knowledge(KnowledgeRow(
        id=0, topic="post-migration", summary="ok", kind="finding",
    ))
    assert new_id > ids["k2"], (
        f"new id {new_id} collided with migrated id {ids['k2']}"
    )


def test_open_storage_sqlite_dsn(tmp_path):
    p = tmp_path / "x.db"
    s = open_storage(f"sqlite:///{p}")
    s.ensure_schema()
    new_id = s.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s", kind="finding"))
    assert new_id == 1


# ---------------------------------------------------------------------------
# SQLite -> Postgres
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _postgres_available(),
    reason=f"Postgres not reachable at {TEST_PG_DSN}",
)
def test_migrate_sqlite_to_postgres(tmp_path):
    from mcm_engine.adapters.postgres.storage import PostgresStorage

    src = _make_sqlite(tmp_path, "src.db")
    ids = _seed(src)

    dst = PostgresStorage(dsn=TEST_PG_DSN)
    dst.ensure_schema()
    dst.truncate_all()

    report = migrate(src, dst)
    assert report.total() == 8

    # Ids preserved across the boundary.
    assert dst.entry_exists(EntityType.KNOWLEDGE, ids["k1"])
    k1 = dst.find_by_id(EntityType.KNOWLEDGE, ids["k1"])
    assert k1.topic == "postgres tsvector"
    assert k1.project == "alpha"

    # Sequence was bumped — next auto-id won't collide with k2.
    new_id = dst.insert_knowledge(KnowledgeRow(
        id=0, topic="post-migration", summary="ok", kind="finding",
    ))
    assert new_id > ids["k2"]
