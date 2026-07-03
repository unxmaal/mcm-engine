"""Issue #10 — rule provenance + full content.

Covers the tool-layer behavior (actor resolution, event emission) and the
v7 -> v8 incremental migration. Storage-level round-trips for content /
attribution / rule_events live in the cross-adapter StorageConformance
suite so both SQLite and Postgres exercise them.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, KnowledgeRow, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import CORE_VERSION, _has_column, migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.rules import register_rules_tools


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


@pytest.fixture
def wired(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    db = KnowledgeDB(tmp_path / "rules.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    register_rules_tools(
        mcp, db, tracker, project_name="t",
        rules_paths=[rules_dir], project_root=tmp_path,
    )
    return {
        "mcp": mcp,
        "storage": SqliteStorage(db=db),
        "tmp_path": tmp_path,
        "rules_dir": rules_dir,
    }


def _events(storage, rule_id):
    return storage.list_rule_events(rule_id)


def _types(storage, rule_id):
    return [e.event_type for e in _events(storage, rule_id)]


# ---- add_rule content + attribution -------------------------------------


def test_add_rule_persists_full_content(wired):
    body = "line one\n\nline two with lots more than the description would hold"
    wired["mcp"]["add_rule"](title="Full body", keywords="kw", content=body)
    row = wired["storage"].find_rule_by_title("Full body")
    assert row.content == body


def test_add_rule_without_actor_is_nobody(wired):
    wired["mcp"]["add_rule"](title="Anon", keywords="kw", content="body")
    row = wired["storage"].find_rule_by_title("Anon")
    assert row.created_by == "nobody"
    events = _events(wired["storage"], row.id)
    assert events[0].event_type == "created"
    assert events[0].actor == "nobody"


def test_add_rule_with_explicit_actor(wired):
    wired["mcp"]["add_rule"](title="Owned", keywords="kw", content="body", actor="alice")
    row = wired["storage"].find_rule_by_title("Owned")
    assert row.created_by == "alice"
    assert row.updated_by == "alice"
    events = _events(wired["storage"], row.id)
    assert events[0].actor == "alice"


def test_mcm_actor_env_used_when_no_explicit(wired, monkeypatch):
    monkeypatch.setenv("MCM_ACTOR", "envbot")
    wired["mcp"]["add_rule"](title="Env", keywords="kw", content="body")
    row = wired["storage"].find_rule_by_title("Env")
    assert row.created_by == "envbot"


def test_explicit_actor_beats_env(wired, monkeypatch):
    monkeypatch.setenv("MCM_ACTOR", "envbot")
    wired["mcp"]["add_rule"](title="Both", keywords="kw", content="body", actor="alice")
    row = wired["storage"].find_rule_by_title("Both")
    assert row.created_by == "alice"


def test_reupsert_same_content_emits_no_event(wired):
    add = wired["mcp"]["add_rule"]
    add(title="Idem", keywords="kw", content="same body", actor="alice")
    row = wired["storage"].find_rule_by_title("Idem")
    add(title="Idem", keywords="kw", content="same body", actor="bob")
    # Only the original `created` event; the no-op re-add adds nothing.
    assert _types(wired["storage"], row.id) == ["created"]


def test_reupsert_changed_content_emits_updated(wired):
    add = wired["mcp"]["add_rule"]
    add(title="Chg", keywords="kw", content="original", actor="alice")
    row = wired["storage"].find_rule_by_title("Chg")
    add(title="Chg", keywords="kw", content="different", actor="bob")
    types = _types(wired["storage"], row.id)
    assert "updated" in types
    updated = [e for e in _events(wired["storage"], row.id) if e.event_type == "updated"][0]
    assert updated.actor == "bob"
    assert wired["storage"].find_rule_by_title("Chg").content == "different"


# ---- sync_rules ----------------------------------------------------------


def _write_rule(rules_dir, name, title, body="body text here"):
    (rules_dir / name).write_text(
        f"# {title}\n\n**Keywords:** kw\n\n{body}\n", encoding="utf-8"
    )


def test_sync_created_events_propagate_source(wired):
    _write_rule(wired["rules_dir"], "a.md", "Rule A")
    wired["mcp"]["sync_rules"](
        actor="loader", source_repo="repo", source_ref="main",
        source_commit="c0ffee",
    )
    row = wired["storage"].find_rule_by_title("Rule A")
    ev = _events(wired["storage"], row.id)[0]
    assert ev.event_type == "created"
    assert ev.actor == "loader"
    assert (ev.source_repo, ev.source_ref, ev.source_commit) == ("repo", "main", "c0ffee")


def test_sync_modified_file_emits_updated(wired):
    _write_rule(wired["rules_dir"], "b.md", "Rule B", body="first")
    wired["mcp"]["sync_rules"](actor="loader")
    row = wired["storage"].find_rule_by_title("Rule B")
    _write_rule(wired["rules_dir"], "b.md", "Rule B", body="second edit")
    wired["mcp"]["sync_rules"](actor="loader")
    assert "updated" in _types(wired["storage"], row.id)


def test_sync_deleted_file_emits_archived(wired):
    _write_rule(wired["rules_dir"], "c.md", "Rule C")
    wired["mcp"]["sync_rules"](actor="loader")
    row = wired["storage"].find_rule_by_title("Rule C")
    (wired["rules_dir"] / "c.md").unlink()
    wired["mcp"]["sync_rules"](actor="loader")
    assert "archived" in _types(wired["storage"], row.id)


def test_sync_reappeared_file_emits_restored(wired):
    _write_rule(wired["rules_dir"], "d.md", "Rule D")
    wired["mcp"]["sync_rules"](actor="loader")
    row = wired["storage"].find_rule_by_title("Rule D")
    (wired["rules_dir"] / "d.md").unlink()
    wired["mcp"]["sync_rules"](actor="loader")
    _write_rule(wired["rules_dir"], "d.md", "Rule D")
    wired["mcp"]["sync_rules"](actor="loader")
    assert "restored" in _types(wired["storage"], row.id)


# ---- reinforce + promote -------------------------------------------------


def test_reinforce_emits_event_and_increments_once(wired):
    rid = wired["storage"].insert_rule(RuleRow(id=0, title="R", keywords="kw"))
    wired["mcp"]["reinforce_rule"](rid, actor="alice")
    events = [e for e in _events(wired["storage"], rid) if e.event_type == "reinforced"]
    assert len(events) == 1
    assert events[0].actor == "alice"
    assert wired["storage"].find_by_id(EntityType.RULE, rid).reinforcement_count == 1


def test_promote_emits_promoted_with_note(wired):
    kid = wired["storage"].insert_knowledge(KnowledgeRow(
        id=0, topic="topic", summary="a useful finding", kind="finding",
    ))
    wired["mcp"]["promote_to_rule"](
        source_type="knowledge", source_id=kid, title="Promoted", actor="alice",
    )
    row = wired["storage"].find_rule_by_title("Promoted")
    promoted = [e for e in _events(wired["storage"], row.id) if e.event_type == "promoted"]
    assert len(promoted) == 1
    assert promoted[0].note == f"knowledge:{kid}"
    assert promoted[0].actor == "alice"


# ---- read_rule DB fallback (issue #10 pod case) -------------------------


def test_read_rule_prefers_file_on_disk(wired):
    wired["mcp"]["add_rule"](title="OnDisk", keywords="kw", content="disk body")
    row = wired["storage"].find_rule_by_title("OnDisk")
    out = wired["mcp"]["read_rule"](row.file_path)
    # The on-disk file carries the generated markdown (title header + body).
    assert "# OnDisk" in out
    assert "disk body" in out


def test_read_rule_falls_back_to_db_content_when_file_absent(wired):
    # A row whose backing file does not exist on disk — the pod deployment
    # case where rules were loaded via add_rule but there's no filesystem.
    wired["storage"].insert_rule(RuleRow(
        id=0, title="Ghost", keywords="kw",
        file_path="rules/ghost-never-written.md",
        content="the body only lives in the database",
    ))
    out = wired["mcp"]["read_rule"]("rules/ghost-never-written.md")
    # #34: read_rule now delimits the stored body as untrusted data, so the
    # body is present but no longer the literal prefix.
    assert "the body only lives in the database" in out


def test_read_rule_db_fallback_increments_hit_count(wired):
    rid = wired["storage"].insert_rule(RuleRow(
        id=0, title="GhostHits", keywords="kw",
        file_path="rules/ghost-hits.md", content="body",
    ))
    wired["mcp"]["read_rule"]("rules/ghost-hits.md")
    assert wired["storage"].find_by_id(EntityType.RULE, rid).hit_count == 1


def test_read_rule_not_found_when_no_file_and_no_content(wired):
    wired["storage"].insert_rule(RuleRow(
        id=0, title="Empty", keywords="kw",
        file_path="rules/empty.md", content=None,
    ))
    out = wired["mcp"]["read_rule"]("rules/empty.md")
    assert "not found" in out.lower()


# ---- events outlive their rule (no FK cascade) --------------------------


def test_rule_events_survive_hard_delete(wired):
    storage = wired["storage"]
    rid = storage.insert_rule(RuleRow(id=0, title="Doomed", keywords="kw"))
    storage.insert_rule_event(rid, "created", "alice")
    # Hard-delete the rule row directly. rule_events.rule_id is deliberately
    # not a foreign key, so the delete must not cascade or error.
    storage._db.execute_write("DELETE FROM rules WHERE id = ?", (rid,))
    storage._db.commit()
    assert storage.find_by_id(EntityType.RULE, rid) is None
    events = storage.list_rule_events(rid)
    assert len(events) == 1
    assert events[0].event_type == "created"


# ---- migration v7 -> v8 --------------------------------------------------

_V7_RULES_DDL = """
CREATE TABLE rules (
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
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE rules_fts USING fts5(
    title, keywords, description, category,
    content='rules', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER rules_ai AFTER INSERT ON rules BEGIN
    INSERT INTO rules_fts(rowid, title, keywords, description, category)
    VALUES (new.id, new.title, new.keywords, new.description, new.category);
END;
CREATE TABLE _mcm_versions (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def test_migrate_v7_to_v8(tmp_path):
    db = KnowledgeDB(tmp_path / "v7.db")
    db.executescript(_V7_RULES_DDL)
    db.execute_write("INSERT INTO _mcm_versions (component, version) VALUES ('core', 7)")
    db.execute_write(
        "INSERT INTO rules (title, keywords, description) "
        "VALUES ('Legacy Rule', 'legacykw', 'legacy description body')"
    )
    db.commit()

    migrate_core(db)

    # New columns exist, unattributed (no backfill).
    assert _has_column(db, "rules", "content")
    assert _has_column(db, "rules", "created_by")
    assert _has_column(db, "rules", "updated_by")
    row = db.execute("SELECT * FROM rules WHERE title = 'Legacy Rule'").fetchone()
    assert row["content"] is None
    assert row["created_by"] is None

    # rule_events table exists and is empty — no invented history.
    assert db.execute("SELECT COUNT(*) AS c FROM rule_events").fetchone()["c"] == 0

    # FTS still returns pre-existing rules after the rebuild.
    hit = db.execute(
        "SELECT r.title FROM rules_fts f JOIN rules r ON f.rowid = r.id "
        "WHERE rules_fts MATCH 'legacy' ORDER BY rank LIMIT 1"
    ).fetchone()
    assert hit is not None and hit["title"] == "Legacy Rule"

    # And new content is now indexable through the rebuilt triggers.
    db.execute_write(
        "INSERT INTO rules (title, keywords, content) "
        "VALUES ('Fresh', 'freshkw', 'zebra content term')"
    )
    db.commit()
    hit2 = db.execute(
        "SELECT r.title FROM rules_fts f JOIN rules r ON f.rowid = r.id "
        "WHERE rules_fts MATCH 'zebra' LIMIT 1"
    ).fetchone()
    assert hit2 is not None and hit2["title"] == "Fresh"

    assert db.execute(
        "SELECT version FROM _mcm_versions WHERE component = 'core'"
    ).fetchone()["version"] == CORE_VERSION
