"""Piece A: correctness axis (report_outcome) + supersession (issue #21)."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import CORE_VERSION, _has_column, migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools


def _has_table(db, table):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


_V9_COLS = ("correct_count", "incorrect_count", "valid_until", "superseded_by", "status")


class TestV8ToV9Migration:
    def test_fresh_install_has_v9_columns_and_table(self, tmp_path):
        db = KnowledgeDB(tmp_path / "fresh.db")
        migrate_core(db)
        for col in _V9_COLS:
            assert _has_column(db, "rules", col), col
        assert _has_table(db, "rule_outcomes")
        row = db.execute(
            "SELECT version FROM _mcm_versions WHERE component='core'"
        ).fetchone()
        assert row["version"] == CORE_VERSION
        assert CORE_VERSION >= 9

    def test_v8_db_migrates_to_v9_preserving_rows(self, tmp_path):
        db = KnowledgeDB(tmp_path / "v8.db")
        db.executescript(
            """
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, keywords TEXT NOT NULL,
                file_path TEXT, description TEXT, category TEXT,
                hit_count INTEGER DEFAULT 0, last_hit_at TEXT,
                reinforcement_count INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0,
                content_hash TEXT, archived INTEGER DEFAULT 0, archived_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                content TEXT, created_by TEXT, updated_by TEXT
            );
            CREATE TABLE _mcm_versions (
                component TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT
            );
            INSERT INTO _mcm_versions (component, version) VALUES ('core', 8);
            INSERT INTO rules (title, keywords) VALUES ('t', 'k');
            """
        )
        db.commit()

        migrate_core(db)

        for col in _V9_COLS:
            assert _has_column(db, "rules", col), col
        assert _has_table(db, "rule_outcomes")
        row = db.execute(
            "SELECT status, correct_count, incorrect_count FROM rules WHERE title='t'"
        ).fetchone()
        assert row["correct_count"] == 0
        assert row["incorrect_count"] == 0
        assert row["status"] == "active"


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
def env(db, project_root):
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    search_all_fn = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
    register_session_tools(mcp, db, tracker, "test-project", [])
    register_rules_tools(mcp, db, tracker, "test-project", [project_root / "rules"], project_root)
    return mcp, db


def _mk_rule(mcp, db, title, author):
    """Create a rule and pin its author (created_by) deterministically."""
    mcp["add_rule"](title=title, keywords="k", content="body")
    rid = db.execute("SELECT id FROM rules WHERE title = ?", (title,)).fetchone()["id"]
    db.execute_write("UPDATE rules SET created_by = ? WHERE id = ?", (author, rid))
    db.commit()
    return rid


class TestReportOutcome:
    def test_independent_pass_increments_correct(self, env):
        mcp, db = env
        rid = _mk_rule(mcp, db, "R1", author="alice")
        res = mcp["report_outcome"](rule_ids=[rid], passed=True, actor="bob")
        assert "recorded" in res
        row = db.execute(
            "SELECT correct_count, incorrect_count FROM rules WHERE id=?", (rid,)
        ).fetchone()
        assert row["correct_count"] == 1
        assert row["incorrect_count"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM rule_outcomes WHERE rule_id=?", (rid,)
        ).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) c FROM rule_events WHERE rule_id=? AND event_type='outcome'",
            (rid,),
        ).fetchone()["c"] == 1

    def test_independent_fail_increments_incorrect(self, env):
        mcp, db = env
        rid = _mk_rule(mcp, db, "R2", author="alice")
        mcp["report_outcome"](rule_ids=[rid], passed=False, actor="bob")
        row = db.execute(
            "SELECT correct_count, incorrect_count FROM rules WHERE id=?", (rid,)
        ).fetchone()
        assert row["correct_count"] == 0
        assert row["incorrect_count"] == 1

    def test_self_report_is_logged_but_uncounted(self, env):
        """AUTHOR!=JUDGE: the author's own report is recorded but must NOT
        move the correctness counters."""
        mcp, db = env
        rid = _mk_rule(mcp, db, "R3", author="alice")
        res = mcp["report_outcome"](rule_ids=[rid], passed=True, actor="alice")
        assert "self-report" in res
        row = db.execute(
            "SELECT correct_count, incorrect_count FROM rules WHERE id=?", (rid,)
        ).fetchone()
        assert row["correct_count"] == 0
        assert row["incorrect_count"] == 0
        # ...but it IS in the ledger.
        assert db.execute(
            "SELECT COUNT(*) c FROM rule_outcomes WHERE rule_id=?", (rid,)
        ).fetchone()["c"] == 1

    def test_not_found_reported(self, env):
        mcp, db = env
        res = mcp["report_outcome"](rule_ids=[9999], passed=True, actor="bob")
        assert "not found" in res


class TestSupersedeRule:
    def test_supersede_marks_status_and_event(self, env):
        mcp, db = env
        old = _mk_rule(mcp, db, "Old", author="alice")
        new = _mk_rule(mcp, db, "New", author="alice")
        res = mcp["supersede_rule"](old_id=old, new_id=new, actor="bob")
        assert "Superseded" in res
        row = db.execute(
            "SELECT status, superseded_by, valid_until FROM rules WHERE id=?", (old,)
        ).fetchone()
        assert row["status"] == "superseded"
        assert row["superseded_by"] == new
        assert row["valid_until"] is not None
        assert db.execute(
            "SELECT COUNT(*) c FROM rule_events WHERE rule_id=? AND event_type='superseded'",
            (old,),
        ).fetchone()["c"] == 1

    def test_supersede_missing_ids(self, env):
        mcp, db = env
        rid = _mk_rule(mcp, db, "Solo", author="alice")
        assert "not found" in mcp["supersede_rule"](old_id=9999, new_id=rid)
        assert "not found" in mcp["supersede_rule"](old_id=rid, new_id=9999)


class TestSupersededFilteredFromSearch:
    def test_superseded_rule_hidden_from_default_search(self, env):
        mcp, db = env
        _mk_rule(mcp, db, "Obsolete widget frobnication", author="alice")
        new = _mk_rule(mcp, db, "Modern widget frobnication", author="alice")
        old = db.execute(
            "SELECT id FROM rules WHERE title='Obsolete widget frobnication'"
        ).fetchone()["id"]

        res = mcp["search"](query="frobnication")
        assert "Obsolete widget frobnication" in res

        mcp["supersede_rule"](old_id=old, new_id=new, actor="bob")

        res = mcp["search"](query="frobnication")
        assert "Obsolete widget frobnication" not in res
        assert "Modern widget frobnication" in res

        res_all = mcp["search"](query="frobnication", include_archived=True)
        assert "Obsolete widget frobnication" in res_all


class TestCorrectnessRanking:
    def test_correctness_weight_demotes_failures_and_promotes_passes(self):
        # Under the #25 additive-hybrid rerank, correctness is a signed
        # tanh(net) term (bounded), not a raw linear net — so assert the
        # direction (promote/demote), not an exact linear value.
        from mcm_engine.scoring import compose_rank

        base = compose_rank(relevance=0.5, hit_count=0, reinforcement_count=0,
                            pinned=False, age_days=None)
        better = compose_rank(relevance=0.5, hit_count=0, reinforcement_count=0,
                              pinned=False, age_days=None,
                              correct_count=4, incorrect_count=1)
        worse = compose_rank(relevance=0.5, hit_count=0, reinforcement_count=0,
                             pinned=False, age_days=None,
                             correct_count=0, incorrect_count=3)
        assert better > base   # net-positive outcomes promote
        assert worse < base    # a failing rule is demoted, not banned
