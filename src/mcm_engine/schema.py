"""Core schema SQL and migration engine for MCM Engine."""
from __future__ import annotations

from .db import KnowledgeDB, log

CORE_VERSION = 3

# Full schema for fresh installs (creates everything at latest version)
CORE_SCHEMA = """
-- Core knowledge table
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'finding',
    summary TEXT NOT NULL,
    detail TEXT,
    tags TEXT,
    project TEXT,
    rationale TEXT,
    alternatives TEXT,
    hit_count INTEGER DEFAULT 0,
    last_hit_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- What doesn't work
CREATE TABLE IF NOT EXISTS negative_knowledge (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    what_failed TEXT NOT NULL,
    why_failed TEXT,
    correct_approach TEXT,
    severity TEXT DEFAULT 'normal',
    project TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Error patterns and fixes
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    context TEXT,
    root_cause TEXT,
    fix TEXT,
    tags TEXT,
    project TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Session handoffs
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    current_task TEXT,
    findings_summary TEXT,
    next_steps TEXT,
    blockers TEXT,
    context_snapshot TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Schema version tracking (core + plugins)
CREATE TABLE IF NOT EXISTS _mcm_versions (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- FTS5 indexes
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    topic, kind, summary, detail, tags,
    content='knowledge',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS negative_fts USING fts5(
    category, what_failed, why_failed, correct_approach,
    content='negative_knowledge',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS errors_fts USING fts5(
    pattern, context, root_cause, fix, tags,
    content='errors',
    content_rowid='id'
);

-- Triggers: knowledge FTS sync
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, topic, kind, summary, detail, tags)
    VALUES (new.id, new.topic, new.kind, new.summary, new.detail, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, kind, summary, detail, tags)
    VALUES ('delete', old.id, old.topic, old.kind, old.summary, old.detail, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, kind, summary, detail, tags)
    VALUES ('delete', old.id, old.topic, old.kind, old.summary, old.detail, old.tags);
    INSERT INTO knowledge_fts(rowid, topic, kind, summary, detail, tags)
    VALUES (new.id, new.topic, new.kind, new.summary, new.detail, new.tags);
END;

-- Triggers: negative_knowledge FTS sync
CREATE TRIGGER IF NOT EXISTS negative_ai AFTER INSERT ON negative_knowledge BEGIN
    INSERT INTO negative_fts(rowid, category, what_failed, why_failed, correct_approach)
    VALUES (new.id, new.category, new.what_failed, new.why_failed, new.correct_approach);
END;

CREATE TRIGGER IF NOT EXISTS negative_ad AFTER DELETE ON negative_knowledge BEGIN
    INSERT INTO negative_fts(negative_fts, rowid, category, what_failed, why_failed, correct_approach)
    VALUES ('delete', old.id, old.category, old.what_failed, old.why_failed, old.correct_approach);
END;

CREATE TRIGGER IF NOT EXISTS negative_au AFTER UPDATE ON negative_knowledge BEGIN
    INSERT INTO negative_fts(negative_fts, rowid, category, what_failed, why_failed, correct_approach)
    VALUES ('delete', old.id, old.category, old.what_failed, old.why_failed, old.correct_approach);
    INSERT INTO negative_fts(rowid, category, what_failed, why_failed, correct_approach)
    VALUES (new.id, new.category, new.what_failed, new.why_failed, new.correct_approach);
END;

-- Triggers: errors FTS sync
CREATE TRIGGER IF NOT EXISTS errors_ai AFTER INSERT ON errors BEGIN
    INSERT INTO errors_fts(rowid, pattern, context, root_cause, fix, tags)
    VALUES (new.id, new.pattern, new.context, new.root_cause, new.fix, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS errors_ad AFTER DELETE ON errors BEGIN
    INSERT INTO errors_fts(errors_fts, rowid, pattern, context, root_cause, fix, tags)
    VALUES ('delete', old.id, old.pattern, old.context, old.root_cause, old.fix, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS errors_au AFTER UPDATE ON errors BEGIN
    INSERT INTO errors_fts(errors_fts, rowid, pattern, context, root_cause, fix, tags)
    VALUES ('delete', old.id, old.pattern, old.context, old.root_cause, old.fix, old.tags);
    INSERT INTO errors_fts(rowid, pattern, context, root_cause, fix, tags)
    VALUES (new.id, new.pattern, new.context, new.root_cause, new.fix, new.tags);
END;

-- External rules (file-backed knowledge)
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    keywords TEXT NOT NULL,
    file_path TEXT,
    description TEXT,
    category TEXT,
    hit_count INTEGER DEFAULT 0,
    last_hit_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS rules_fts USING fts5(
    title, keywords, description, category,
    content='rules',
    content_rowid='id'
);

-- Triggers: rules FTS sync
CREATE TRIGGER IF NOT EXISTS rules_ai AFTER INSERT ON rules BEGIN
    INSERT INTO rules_fts(rowid, title, keywords, description, category)
    VALUES (new.id, new.title, new.keywords, new.description, new.category);
END;

CREATE TRIGGER IF NOT EXISTS rules_ad AFTER DELETE ON rules BEGIN
    INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category)
    VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category);
END;

CREATE TRIGGER IF NOT EXISTS rules_au AFTER UPDATE ON rules BEGIN
    INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category)
    VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category);
    INSERT INTO rules_fts(rowid, title, keywords, description, category)
    VALUES (new.id, new.title, new.keywords, new.description, new.category);
END;
"""


def _has_column(db: KnowledgeDB, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cols = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in cols)


# Incremental migrations: (from_version) -> callable
# Each function upgrades from version N to N+1.
def _migrate_v1_to_v2(db: KnowledgeDB) -> None:
    """v1 -> v2: Add rules table, FTS5, and triggers.

    CREATE IF NOT EXISTS handles this idempotently — the tables/triggers
    were already applied by CORE_SCHEMA executescript.
    """
    log("Migration v1->v2: rules table (applied via CORE_SCHEMA)")


def _migrate_v2_to_v3(db: KnowledgeDB) -> None:
    """v2 -> v3: Add last_hit_at columns to knowledge and rules."""
    if not _has_column(db, "knowledge", "last_hit_at"):
        db.execute_write("ALTER TABLE knowledge ADD COLUMN last_hit_at TEXT")
        log("Migration v2->v3: added knowledge.last_hit_at")

    if not _has_column(db, "rules", "last_hit_at"):
        db.execute_write("ALTER TABLE rules ADD COLUMN last_hit_at TEXT")
        log("Migration v2->v3: added rules.last_hit_at")

    db.commit()


_MIGRATIONS = [
    # (from_version, to_version, function)
    (1, 2, _migrate_v1_to_v2),
    (2, 3, _migrate_v2_to_v3),
]


def migrate_core(db: KnowledgeDB) -> None:
    """Apply core schema and run any pending migrations.

    Fresh databases get the full CORE_SCHEMA (latest version).
    Existing databases run incremental migrations from their current version.
    """
    # Apply full schema — CREATE IF NOT EXISTS makes this safe for existing DBs
    db.executescript(CORE_SCHEMA)

    # Check current version
    existing = db.execute(
        "SELECT version FROM _mcm_versions WHERE component = 'core'"
    ).fetchone()

    if existing is None:
        # Fresh install — already at latest
        db.execute_write(
            "INSERT INTO _mcm_versions (component, version) VALUES ('core', ?)",
            (CORE_VERSION,),
        )
        db.commit()
        log(f"Core schema initialized at version {CORE_VERSION}")
    elif existing["version"] < CORE_VERSION:
        current = existing["version"]
        # Run each pending migration in order
        for from_v, to_v, migrate_fn in _MIGRATIONS:
            if from_v >= current and to_v <= CORE_VERSION:
                log(f"Running migration v{from_v}->v{to_v}")
                migrate_fn(db)
                current = to_v

        db.execute_write(
            "UPDATE _mcm_versions SET version = ?, updated_at = datetime('now') WHERE component = 'core'",
            (CORE_VERSION,),
        )
        db.commit()
        log(f"Core schema migrated to version {CORE_VERSION}")
    else:
        log(f"Core schema at version {CORE_VERSION}")


def migrate_plugin(db: KnowledgeDB, plugin_name: str, schema_sql: str, version: int) -> None:
    """Apply a plugin's schema. Tracks version in _mcm_versions."""
    db.executescript(schema_sql)

    existing = db.execute(
        "SELECT version FROM _mcm_versions WHERE component = ?",
        (f"plugin:{plugin_name}",),
    ).fetchone()
    if existing is None:
        db.execute_write(
            "INSERT INTO _mcm_versions (component, version) VALUES (?, ?)",
            (f"plugin:{plugin_name}", version),
        )
        db.commit()
    elif existing["version"] < version:
        db.execute_write(
            "UPDATE _mcm_versions SET version = ?, updated_at = datetime('now') WHERE component = ?",
            (version, f"plugin:{plugin_name}"),
        )
        db.commit()
    log(f"Plugin '{plugin_name}' schema at version {version}")
