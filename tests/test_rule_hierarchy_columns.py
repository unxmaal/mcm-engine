"""Rule hierarchy columns — importance / scope / kind (issue #64, Phase 1).

Rules were a flat table distinguishable only by a nullable `category` string.
This adds three orthogonal axes so a universal directive (uv-always) is
structurally distinct from a situational fact (a hardware pinout):

  - importance: ordinal blast-radius rank (higher binds harder), default 0
  - scope:      universal | conditional, default conditional
  - kind:       directive | fact, default fact

Defaults are the most conservative (a fresh rule is a low-importance
situational fact until deliberately promoted). This suite locks the vocab,
the fresh-install schema, the v10->v11 migration on an existing DB, and that
hydration round-trips the columns.
"""
from __future__ import annotations

from mcm_engine import hierarchy
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import CORE_VERSION, _has_column, migrate_core


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_scope_and_kind_vocab():
    assert set(hierarchy.SCOPES) == {"universal", "conditional"}
    assert set(hierarchy.KINDS) == {"directive", "fact"}


def test_conservative_defaults():
    assert hierarchy.DEFAULT_IMPORTANCE == 0
    assert hierarchy.DEFAULT_SCOPE == "conditional"
    assert hierarchy.DEFAULT_KIND == "fact"


def test_importance_is_a_three_tier_ordinal():
    assert hierarchy.IMPORTANCE_MIN == 0
    assert hierarchy.IMPORTANCE_REFERENCE < hierarchy.IMPORTANCE_DEFAULT < hierarchy.IMPORTANCE_INVARIANT
    assert hierarchy.IMPORTANCE_INVARIANT == hierarchy.IMPORTANCE_MAX


def test_validators():
    assert hierarchy.valid_scope("universal")
    assert not hierarchy.valid_scope("galactic")
    assert hierarchy.valid_kind("directive")
    assert not hierarchy.valid_kind("vibe")
    assert hierarchy.valid_importance(0)
    assert hierarchy.valid_importance(2)
    assert not hierarchy.valid_importance(-1)
    assert not hierarchy.valid_importance(99)
    assert not hierarchy.valid_importance("2")


def test_normalize_importance_clamps():
    assert hierarchy.normalize_importance(-5) == hierarchy.IMPORTANCE_MIN
    assert hierarchy.normalize_importance(99) == hierarchy.IMPORTANCE_MAX
    assert hierarchy.normalize_importance(1) == 1


# ---------------------------------------------------------------------------
# RuleRow dataclass
# ---------------------------------------------------------------------------


def test_rulerow_has_hierarchy_fields_with_conservative_defaults():
    r = RuleRow(id=0, title="t", keywords="k")
    assert r.importance == 0
    assert r.scope == "conditional"
    assert r.kind == "fact"


# ---------------------------------------------------------------------------
# Fresh install schema
# ---------------------------------------------------------------------------


def test_core_version_is_at_least_11():
    assert CORE_VERSION >= 11


def test_fresh_install_has_hierarchy_columns(tmp_path):
    db = KnowledgeDB(tmp_path / "fresh.db")
    migrate_core(db)
    assert _has_column(db, "rules", "importance")
    assert _has_column(db, "rules", "scope")
    assert _has_column(db, "rules", "kind")


# ---------------------------------------------------------------------------
# v10 -> v11 migration on an existing database
# ---------------------------------------------------------------------------


def _seed_v10_rules_db(tmp_path):
    """A rules table shaped like v10 (no importance/scope/kind), stamped v10."""
    db = KnowledgeDB(tmp_path / "v10.db")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS _mcm_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            keywords TEXT NOT NULL,
            file_path TEXT,
            description TEXT,
            category TEXT,
            hit_count INTEGER DEFAULT 0,
            last_hit_at TEXT,
            reinforcement_count INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            content_hash TEXT,
            archived INTEGER DEFAULT 0,
            archived_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            content TEXT,
            created_by TEXT,
            updated_by TEXT,
            correct_count INTEGER DEFAULT 0,
            incorrect_count INTEGER DEFAULT 0,
            valid_until TEXT,
            superseded_by INTEGER,
            status TEXT DEFAULT 'active'
        );
    """)
    db.execute_write(
        "INSERT INTO _mcm_versions (component, version) VALUES ('core', 10)"
    )
    db.execute_write(
        "INSERT INTO rules (title, keywords, content) "
        "VALUES ('old rule', 'k', 'body that predates the hierarchy')"
    )
    db.commit()
    return db


def test_v10_to_v11_adds_columns_and_preserves_data(tmp_path):
    db = _seed_v10_rules_db(tmp_path)
    assert not _has_column(db, "rules", "importance")

    migrate_core(db)

    assert _has_column(db, "rules", "importance")
    assert _has_column(db, "rules", "scope")
    assert _has_column(db, "rules", "kind")

    row = db.execute("SELECT version FROM _mcm_versions WHERE component='core'").fetchone()
    assert row["version"] == CORE_VERSION

    # Existing row survives and gets the conservative defaults.
    r = db.execute("SELECT * FROM rules WHERE title='old rule'").fetchone()
    assert r["content"] == "body that predates the hierarchy"
    assert r["importance"] == 0
    assert r["scope"] == "conditional"
    assert r["kind"] == "fact"


# ---------------------------------------------------------------------------
# Hydration round-trip
# ---------------------------------------------------------------------------


def test_inserted_rule_hydrates_with_defaults(tmp_path):
    db = KnowledgeDB(tmp_path / "h.db")
    migrate_core(db)
    storage = SqliteStorage(db=db)
    rid = storage.insert_rule(RuleRow(id=0, title="R", keywords="k", content="body"))
    got = storage.find_by_id(EntityType.RULE, rid)
    assert got.importance == 0
    assert got.scope == "conditional"
    assert got.kind == "fact"


def test_hydration_reflects_tuned_values(tmp_path):
    """Prove the columns are real: a direct UPDATE is read back through
    hydration (this is what the Phase-2 set_rule_metadata write will drive)."""
    db = KnowledgeDB(tmp_path / "h2.db")
    migrate_core(db)
    storage = SqliteStorage(db=db)
    rid = storage.insert_rule(RuleRow(id=0, title="uv", keywords="k", content="use uv"))
    db.execute_write(
        "UPDATE rules SET importance=2, scope='universal', kind='directive' WHERE id=?",
        (rid,),
    )
    db.commit()
    got = storage.find_by_id(EntityType.RULE, rid)
    assert got.importance == 2
    assert got.scope == "universal"
    assert got.kind == "directive"
