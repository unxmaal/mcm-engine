"""Core schema SQL and migration engine for MCM Engine."""
from __future__ import annotations

from .db import KnowledgeDB, log

CORE_VERSION = 9

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
    reinforcement_count INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
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
    pinned INTEGER DEFAULT 0,
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
    pinned INTEGER DEFAULT 0,
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
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS negative_fts USING fts5(
    category, what_failed, why_failed, correct_approach,
    content='negative_knowledge',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS errors_fts USING fts5(
    pattern, context, root_cause, fix, tags,
    content='errors',
    content_rowid='id',
    tokenize='porter unicode61'
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
-- v7: content_hash + archived columns support the watcher cascade (MCM2-23).
-- v8: content (full body) + created_by/updated_by attribution (issue #10).
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
    -- v9: correctness axis + supersession (issue #21). None are FTS-indexed.
    correct_count INTEGER DEFAULT 0,
    incorrect_count INTEGER DEFAULT 0,
    valid_until TEXT,
    superseded_by INTEGER,
    status TEXT DEFAULT 'active'
);

-- The FTS column is named `content` to match the rules.content column
-- (external-content FTS5 reads each column from the like-named base
-- column). The bare `content` column and the `content='rules'` option
-- are disambiguated by the `=` — verified against sqlite fts5.
CREATE VIRTUAL TABLE IF NOT EXISTS rules_fts USING fts5(
    title, keywords, description, category, content,
    content='rules',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers: rules FTS sync
CREATE TRIGGER IF NOT EXISTS rules_ai AFTER INSERT ON rules BEGIN
    INSERT INTO rules_fts(rowid, title, keywords, description, category, content)
    VALUES (new.id, new.title, new.keywords, new.description, new.category, new.content);
END;

CREATE TRIGGER IF NOT EXISTS rules_ad AFTER DELETE ON rules BEGIN
    INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category, content)
    VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category, old.content);
END;

CREATE TRIGGER IF NOT EXISTS rules_au AFTER UPDATE ON rules BEGIN
    INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category, content)
    VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category, old.content);
    INSERT INTO rules_fts(rowid, title, keywords, description, category, content)
    VALUES (new.id, new.title, new.keywords, new.description, new.category, new.content);
END;

-- Append-only audit log of rule state changes (issue #10). rule_id is
-- intentionally NOT a foreign key: events outlive the rule they describe.
CREATE TABLE IF NOT EXISTS rule_events (
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'nobody',
    at TEXT NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT,
    source_repo TEXT,
    source_ref TEXT,
    source_commit TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_events_rule_at ON rule_events (rule_id, at DESC);

-- v9: append-only per-outcome ledger (issue #21). Stores (actor, passed)
-- only — trust weight is applied at rank time (late-binding), never persisted.
CREATE TABLE IF NOT EXISTS rule_outcomes (
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT 'nobody',
    passed INTEGER NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rule_outcomes_rule ON rule_outcomes (rule_id);

-- Typed relationships between knowledge entries
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,  -- 'knowledge', 'error', 'rule', 'negative'
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,     -- 'fixes', 'causes', 'supersedes', 'contradicts', 'related'
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id, target_type, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_relations_source
    ON relations(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target
    ON relations(target_type, target_id);

-- Numbered mid-session checkpoints
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    sequence_num INTEGER NOT NULL,
    goal TEXT,
    progress TEXT,
    open_questions TEXT,
    blockers TEXT,
    next_steps TEXT,
    active_files TEXT,
    key_decisions TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id, sequence_num);
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


def _migrate_v3_to_v4(db: KnowledgeDB) -> None:
    """v3 -> v4: Add relations table for typed edges between knowledge entries.

    CREATE IF NOT EXISTS handles this idempotently — already applied by CORE_SCHEMA.
    """
    log("Migration v3->v4: relations table (applied via CORE_SCHEMA)")


def _migrate_v4_to_v5(db: KnowledgeDB) -> None:
    """v4 -> v5: Add reinforcement, pinning, snapshots, project scoping."""
    # knowledge: reinforcement_count, pinned
    if not _has_column(db, "knowledge", "reinforcement_count"):
        db.execute_write("ALTER TABLE knowledge ADD COLUMN reinforcement_count INTEGER DEFAULT 0")
        log("Migration v4->v5: added knowledge.reinforcement_count")
    if not _has_column(db, "knowledge", "pinned"):
        db.execute_write("ALTER TABLE knowledge ADD COLUMN pinned INTEGER DEFAULT 0")
        log("Migration v4->v5: added knowledge.pinned")

    # negative_knowledge: pinned
    if not _has_column(db, "negative_knowledge", "pinned"):
        db.execute_write("ALTER TABLE negative_knowledge ADD COLUMN pinned INTEGER DEFAULT 0")
        log("Migration v4->v5: added negative_knowledge.pinned")

    # errors: pinned
    if not _has_column(db, "errors", "pinned"):
        db.execute_write("ALTER TABLE errors ADD COLUMN pinned INTEGER DEFAULT 0")
        log("Migration v4->v5: added errors.pinned")

    # rules: reinforcement_count, pinned
    if not _has_column(db, "rules", "reinforcement_count"):
        db.execute_write("ALTER TABLE rules ADD COLUMN reinforcement_count INTEGER DEFAULT 0")
        log("Migration v4->v5: added rules.reinforcement_count")
    if not _has_column(db, "rules", "pinned"):
        db.execute_write("ALTER TABLE rules ADD COLUMN pinned INTEGER DEFAULT 0")
        log("Migration v4->v5: added rules.pinned")

    # snapshots table — CREATE IF NOT EXISTS handles idempotency via CORE_SCHEMA
    log("Migration v4->v5: snapshots table (applied via CORE_SCHEMA)")

    db.commit()


def _migrate_v5_to_v6(db: KnowledgeDB) -> None:
    """v5 -> v6: Add porter stemmer to all FTS5 indexes.

    FTS5 virtual tables cannot be ALTERed to change tokenizers.
    Drop and recreate each FTS table with 'porter unicode61', then rebuild
    the index from the content tables. Data tables are untouched.
    """
    fts_tables = [
        ("knowledge_fts", "knowledge",
         "topic, kind, summary, detail, tags", "knowledge"),
        ("negative_fts", "negative_knowledge",
         "category, what_failed, why_failed, correct_approach", "negative"),
        ("errors_fts", "errors",
         "pattern, context, root_cause, fix, tags", "errors"),
        ("rules_fts", "rules",
         "title, keywords, description, category", "rules"),
    ]

    for fts_table, content_table, columns, trigger_prefix in fts_tables:
        # Drop old triggers
        for suffix in ["ai", "ad", "au"]:
            db.execute_write(f"DROP TRIGGER IF EXISTS {trigger_prefix}_{suffix}")

        # Drop and recreate FTS table with porter stemmer
        db.execute_write(f"DROP TABLE IF EXISTS {fts_table}")
        db.execute_write(
            f"CREATE VIRTUAL TABLE {fts_table} USING fts5("
            f"  {columns},"
            f"  content='{content_table}',"
            f"  content_rowid='id',"
            f"  tokenize='porter unicode61'"
            f")"
        )

        # Rebuild FTS index from content table
        db.execute_write(f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')")
        log(f"Migration v5->v6: rebuilt {fts_table} with porter stemmer")

    # Recreate triggers (same SQL as CORE_SCHEMA, now against porter-tokenized tables)

    # knowledge triggers
    db.execute_write("""CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
        INSERT INTO knowledge_fts(rowid, topic, kind, summary, detail, tags)
        VALUES (new.id, new.topic, new.kind, new.summary, new.detail, new.tags);
    END""")
    db.execute_write("""CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
        INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, kind, summary, detail, tags)
        VALUES ('delete', old.id, old.topic, old.kind, old.summary, old.detail, old.tags);
    END""")
    db.execute_write("""CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
        INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, kind, summary, detail, tags)
        VALUES ('delete', old.id, old.topic, old.kind, old.summary, old.detail, old.tags);
        INSERT INTO knowledge_fts(rowid, topic, kind, summary, detail, tags)
        VALUES (new.id, new.topic, new.kind, new.summary, new.detail, new.tags);
    END""")

    # negative_knowledge triggers
    db.execute_write("""CREATE TRIGGER negative_ai AFTER INSERT ON negative_knowledge BEGIN
        INSERT INTO negative_fts(rowid, category, what_failed, why_failed, correct_approach)
        VALUES (new.id, new.category, new.what_failed, new.why_failed, new.correct_approach);
    END""")
    db.execute_write("""CREATE TRIGGER negative_ad AFTER DELETE ON negative_knowledge BEGIN
        INSERT INTO negative_fts(negative_fts, rowid, category, what_failed, why_failed, correct_approach)
        VALUES ('delete', old.id, old.category, old.what_failed, old.why_failed, old.correct_approach);
    END""")
    db.execute_write("""CREATE TRIGGER negative_au AFTER UPDATE ON negative_knowledge BEGIN
        INSERT INTO negative_fts(negative_fts, rowid, category, what_failed, why_failed, correct_approach)
        VALUES ('delete', old.id, old.category, old.what_failed, old.why_failed, old.correct_approach);
        INSERT INTO negative_fts(rowid, category, what_failed, why_failed, correct_approach)
        VALUES (new.id, new.category, new.what_failed, new.why_failed, new.correct_approach);
    END""")

    # errors triggers
    db.execute_write("""CREATE TRIGGER errors_ai AFTER INSERT ON errors BEGIN
        INSERT INTO errors_fts(rowid, pattern, context, root_cause, fix, tags)
        VALUES (new.id, new.pattern, new.context, new.root_cause, new.fix, new.tags);
    END""")
    db.execute_write("""CREATE TRIGGER errors_ad AFTER DELETE ON errors BEGIN
        INSERT INTO errors_fts(errors_fts, rowid, pattern, context, root_cause, fix, tags)
        VALUES ('delete', old.id, old.pattern, old.context, old.root_cause, old.fix, old.tags);
    END""")
    db.execute_write("""CREATE TRIGGER errors_au AFTER UPDATE ON errors BEGIN
        INSERT INTO errors_fts(errors_fts, rowid, pattern, context, root_cause, fix, tags)
        VALUES ('delete', old.id, old.pattern, old.context, old.root_cause, old.fix, old.tags);
        INSERT INTO errors_fts(rowid, pattern, context, root_cause, fix, tags)
        VALUES (new.id, new.pattern, new.context, new.root_cause, new.fix, new.tags);
    END""")

    # rules triggers
    db.execute_write("""CREATE TRIGGER rules_ai AFTER INSERT ON rules BEGIN
        INSERT INTO rules_fts(rowid, title, keywords, description, category)
        VALUES (new.id, new.title, new.keywords, new.description, new.category);
    END""")
    db.execute_write("""CREATE TRIGGER rules_ad AFTER DELETE ON rules BEGIN
        INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category)
        VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category);
    END""")
    db.execute_write("""CREATE TRIGGER rules_au AFTER UPDATE ON rules BEGIN
        INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category)
        VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category);
        INSERT INTO rules_fts(rowid, title, keywords, description, category)
        VALUES (new.id, new.title, new.keywords, new.description, new.category);
    END""")

    db.commit()


def _migrate_v6_to_v7(db: KnowledgeDB) -> None:
    """v6 -> v7: Add watcher-cascade columns to rules.

    The watcher (MCM2-23) writes `content_hash` per file so engine-initiated
    writes can short-circuit the cascade as no-ops; soft-deletes via
    `archived` + `archived_at` replace the v6 hard DELETE on orphan removal.
    """
    if not _has_column(db, "rules", "content_hash"):
        db.execute_write("ALTER TABLE rules ADD COLUMN content_hash TEXT")
        log("Migration v6->v7: added rules.content_hash")
    if not _has_column(db, "rules", "archived"):
        db.execute_write("ALTER TABLE rules ADD COLUMN archived INTEGER DEFAULT 0")
        log("Migration v6->v7: added rules.archived")
    if not _has_column(db, "rules", "archived_at"):
        db.execute_write("ALTER TABLE rules ADD COLUMN archived_at TEXT")
        log("Migration v6->v7: added rules.archived_at")
    db.commit()


def _migrate_v7_to_v8(db: KnowledgeDB) -> None:
    """v7 -> v8: rule provenance + full content (issue #10).

    Adds rules.content / created_by / updated_by, rebuilds rules_fts with
    the content column (and its triggers), and creates the append-only
    rule_events audit table. Existing rules get NULL content/attribution —
    no backfill; pre-v8 history is honestly unattributed. No rule_events
    rows are invented for existing rules.
    """
    if not _has_column(db, "rules", "content"):
        db.execute_write("ALTER TABLE rules ADD COLUMN content TEXT")
        log("Migration v7->v8: added rules.content")
    if not _has_column(db, "rules", "created_by"):
        db.execute_write("ALTER TABLE rules ADD COLUMN created_by TEXT")
        log("Migration v7->v8: added rules.created_by")
    if not _has_column(db, "rules", "updated_by"):
        db.execute_write("ALTER TABLE rules ADD COLUMN updated_by TEXT")
        log("Migration v7->v8: added rules.updated_by")

    # Rebuild rules_fts to include the content column. FTS5 vtables can't
    # be ALTERed to add a column — drop triggers + vtable, recreate with
    # content, rebuild from the (now content-bearing) rules table. The
    # triggers MUST be recreated with the new column list or live
    # inserts/updates would silently stop indexing content.
    for suffix in ("ai", "ad", "au"):
        db.execute_write(f"DROP TRIGGER IF EXISTS rules_{suffix}")
    db.execute_write("DROP TABLE IF EXISTS rules_fts")
    db.execute_write(
        "CREATE VIRTUAL TABLE rules_fts USING fts5("
        "  title, keywords, description, category, content,"
        "  content='rules',"
        "  content_rowid='id',"
        "  tokenize='porter unicode61'"
        ")"
    )
    db.execute_write("INSERT INTO rules_fts(rules_fts) VALUES('rebuild')")
    db.execute_write("""CREATE TRIGGER rules_ai AFTER INSERT ON rules BEGIN
        INSERT INTO rules_fts(rowid, title, keywords, description, category, content)
        VALUES (new.id, new.title, new.keywords, new.description, new.category, new.content);
    END""")
    db.execute_write("""CREATE TRIGGER rules_ad AFTER DELETE ON rules BEGIN
        INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category, content)
        VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category, old.content);
    END""")
    db.execute_write("""CREATE TRIGGER rules_au AFTER UPDATE ON rules BEGIN
        INSERT INTO rules_fts(rules_fts, rowid, title, keywords, description, category, content)
        VALUES ('delete', old.id, old.title, old.keywords, old.description, old.category, old.content);
        INSERT INTO rules_fts(rowid, title, keywords, description, category, content)
        VALUES (new.id, new.title, new.keywords, new.description, new.category, new.content);
    END""")
    log("Migration v7->v8: rebuilt rules_fts with content column")

    # Append-only audit log. CREATE IF NOT EXISTS is idempotent — already
    # applied by CORE_SCHEMA on fresh installs.
    db.execute_write("""CREATE TABLE IF NOT EXISTS rule_events (
        id INTEGER PRIMARY KEY,
        rule_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'nobody',
        at TEXT NOT NULL DEFAULT (datetime('now')),
        content_hash TEXT,
        source_repo TEXT,
        source_ref TEXT,
        source_commit TEXT,
        note TEXT
    )""")
    db.execute_write(
        "CREATE INDEX IF NOT EXISTS idx_rule_events_rule_at "
        "ON rule_events (rule_id, at DESC)"
    )
    log("Migration v7->v8: created rule_events table")
    db.commit()


def _migrate_v8_to_v9(db: KnowledgeDB) -> None:
    """v8 -> v9: correctness axis + supersession (issue #21).

    Adds rules.correct_count / incorrect_count (outcome-driven correctness,
    separate from popularity) plus valid_until / superseded_by / status for
    non-destructive supersession, and the append-only rule_outcomes ledger.
    None of the new columns are FTS-indexed, so rules_fts is left untouched.
    Existing rows default to status='active' with zero counts.
    """
    if not _has_column(db, "rules", "correct_count"):
        db.execute_write("ALTER TABLE rules ADD COLUMN correct_count INTEGER DEFAULT 0")
        log("Migration v8->v9: added rules.correct_count")
    if not _has_column(db, "rules", "incorrect_count"):
        db.execute_write("ALTER TABLE rules ADD COLUMN incorrect_count INTEGER DEFAULT 0")
        log("Migration v8->v9: added rules.incorrect_count")
    if not _has_column(db, "rules", "valid_until"):
        db.execute_write("ALTER TABLE rules ADD COLUMN valid_until TEXT")
        log("Migration v8->v9: added rules.valid_until")
    if not _has_column(db, "rules", "superseded_by"):
        db.execute_write("ALTER TABLE rules ADD COLUMN superseded_by INTEGER")
        log("Migration v8->v9: added rules.superseded_by")
    if not _has_column(db, "rules", "status"):
        db.execute_write("ALTER TABLE rules ADD COLUMN status TEXT DEFAULT 'active'")
        log("Migration v8->v9: added rules.status")

    db.execute_write(
        """CREATE TABLE IF NOT EXISTS rule_outcomes (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL,
            actor TEXT NOT NULL DEFAULT 'nobody',
            passed INTEGER NOT NULL,
            at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    db.execute_write(
        "CREATE INDEX IF NOT EXISTS idx_rule_outcomes_rule ON rule_outcomes (rule_id)"
    )
    log("Migration v8->v9: created rule_outcomes table")
    db.commit()


_MIGRATIONS = [
    # (from_version, to_version, function)
    (1, 2, _migrate_v1_to_v2),
    (2, 3, _migrate_v2_to_v3),
    (3, 4, _migrate_v3_to_v4),
    (4, 5, _migrate_v4_to_v5),
    (5, 6, _migrate_v5_to_v6),
    (6, 7, _migrate_v6_to_v7),
    (7, 8, _migrate_v7_to_v8),
    (8, 9, _migrate_v8_to_v9),
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
