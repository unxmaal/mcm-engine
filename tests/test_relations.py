"""Tests for typed relationship edges."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.relations import register_relations_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools


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
def rel_env(db, project_root):
    """Full tool environment with relations tools."""
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100,
        checkpoint_turns=100,
        mandatory_stop_turns=200,
    ))
    rules_path = project_root / "rules"

    search_all_fn = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
    register_session_tools(mcp, db, tracker, "test-project", [])
    register_rules_tools(mcp, db, tracker, "test-project", [rules_path], project_root)
    register_relations_tools(mcp, db, tracker)

    return mcp, db, tracker


class TestLinkKnowledge:
    def test_basic_link(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="malloc crash", summary="IRIX bsearch crashes")
        mcp["add_knowledge"](topic="bsearch fix", summary="preload libmogrix_compat.so")

        k1 = db.execute("SELECT id FROM knowledge WHERE topic = 'malloc crash'").fetchone()
        k2 = db.execute("SELECT id FROM knowledge WHERE topic = 'bsearch fix'").fetchone()

        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=k2["id"],
            target_type="knowledge", target_id=k1["id"],
            relation="fixes",
            note="preload fixes bsearch crash",
        )
        assert "Linked" in result
        assert "fixes" in result

        row = db.execute("SELECT * FROM relations LIMIT 1").fetchone()
        assert row["relation"] == "fixes"
        assert row["note"] == "preload fixes bsearch crash"

    def test_cross_type_link(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="rld behavior", summary="IRIX rld ignores DT_RUNPATH")
        mcp["report_error"](error_text="library not found: libfoo.so")

        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()
        e = db.execute("SELECT id FROM errors LIMIT 1").fetchone()

        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=k["id"],
            target_type="error", target_id=e["id"],
            relation="causes",
        )
        assert "Linked" in result
        assert "causes" in result

    def test_duplicate_link_rejected(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="a", summary="a")
        mcp["add_knowledge"](topic="b", summary="b")

        k1 = db.execute("SELECT id FROM knowledge WHERE topic = 'a'").fetchone()
        k2 = db.execute("SELECT id FROM knowledge WHERE topic = 'b'").fetchone()

        mcp["link_knowledge"](
            source_type="knowledge", source_id=k1["id"],
            target_type="knowledge", target_id=k2["id"],
            relation="related",
        )
        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=k1["id"],
            target_type="knowledge", target_id=k2["id"],
            relation="related",
        )
        assert "already exists" in result

    def test_invalid_relation(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="a", summary="a")
        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()

        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=k["id"],
            target_type="knowledge", target_id=k["id"],
            relation="banana",
        )
        assert "Invalid relation" in result

    def test_invalid_type(self, rel_env):
        mcp, db, tracker = rel_env
        result = mcp["link_knowledge"](
            source_type="widget", source_id=1,
            target_type="knowledge", target_id=1,
            relation="fixes",
        )
        assert "Invalid source_type" in result

    def test_missing_source(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="exists", summary="exists")
        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()

        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=9999,
            target_type="knowledge", target_id=k["id"],
            relation="fixes",
        )
        assert "not found" in result

    def test_missing_target(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="exists", summary="exists")
        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()

        result = mcp["link_knowledge"](
            source_type="knowledge", source_id=k["id"],
            target_type="knowledge", target_id=9999,
            relation="fixes",
        )
        assert "not found" in result


class TestGetRelated:
    def test_shows_outgoing(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="fix", summary="the fix")
        mcp["add_knowledge"](topic="problem", summary="the problem")

        k1 = db.execute("SELECT id FROM knowledge WHERE topic = 'fix'").fetchone()
        k2 = db.execute("SELECT id FROM knowledge WHERE topic = 'problem'").fetchone()

        mcp["link_knowledge"](
            source_type="knowledge", source_id=k1["id"],
            target_type="knowledge", target_id=k2["id"],
            relation="fixes",
        )

        result = mcp["get_related"](entry_type="knowledge", entry_id=k1["id"])
        assert "Outgoing" in result
        assert "fixes" in result
        assert "problem" in result

    def test_shows_incoming(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="fix", summary="the fix")
        mcp["add_knowledge"](topic="problem", summary="the problem")

        k1 = db.execute("SELECT id FROM knowledge WHERE topic = 'fix'").fetchone()
        k2 = db.execute("SELECT id FROM knowledge WHERE topic = 'problem'").fetchone()

        mcp["link_knowledge"](
            source_type="knowledge", source_id=k1["id"],
            target_type="knowledge", target_id=k2["id"],
            relation="fixes",
        )

        result = mcp["get_related"](entry_type="knowledge", entry_id=k2["id"])
        assert "Incoming" in result
        assert "fixes" in result
        assert "fix" in result

    def test_no_relations(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="lonely", summary="no friends")
        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()

        result = mcp["get_related"](entry_type="knowledge", entry_id=k["id"])
        assert "No relationships found" in result

    def test_invalid_type(self, rel_env):
        mcp, db, tracker = rel_env
        result = mcp["get_related"](entry_type="widget", entry_id=1)
        assert "Invalid entry_type" in result

    def test_missing_entry(self, rel_env):
        mcp, db, tracker = rel_env
        result = mcp["get_related"](entry_type="knowledge", entry_id=9999)
        assert "not found" in result

    def test_link_to_rule(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="rld issue", summary="rld problem")
        mcp["add_rule"](title="rld fix", keywords="rld", content="fix it")

        k = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()
        r = db.execute("SELECT id FROM rules LIMIT 1").fetchone()

        mcp["link_knowledge"](
            source_type="rule", source_id=r["id"],
            target_type="knowledge", target_id=k["id"],
            relation="supersedes",
        )

        result = mcp["get_related"](entry_type="knowledge", entry_id=k["id"])
        assert "supersedes" in result
        assert "RULE" in result


class TestStalenessAndQualityGates:
    def test_stale_tag_on_old_entry(self, rel_env):
        """Entries >90 days old without recent hits should be tagged [STALE]."""
        mcp, db, tracker = rel_env
        # Insert with old created_at and no last_hit_at
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at) "
            "VALUES ('ancient topic', 'very old finding', datetime('now', '-100 days'))"
        )
        db.commit()

        result = mcp["search"](query="ancient topic")
        assert "STALE" in result

    def test_no_stale_tag_on_recent_entry(self, rel_env):
        mcp, db, tracker = rel_env
        mcp["add_knowledge"](topic="fresh topic", summary="brand new finding")

        result = mcp["search"](query="fresh topic")
        assert "STALE" not in result

    def test_no_stale_tag_when_recently_hit(self, rel_env):
        """Old entry that was recently hit should NOT be stale."""
        mcp, db, tracker = rel_env
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at, last_hit_at) "
            "VALUES ('old but active', 'still useful', datetime('now', '-200 days'), datetime('now', '-5 days'))"
        )
        db.commit()

        result = mcp["search"](query="old but active")
        assert "STALE" not in result

    def test_session_start_shows_stale_count(self, rel_env):
        mcp, db, tracker = rel_env
        # Insert stale entries
        for i in range(3):
            db.execute_write(
                "INSERT INTO knowledge (topic, summary, created_at) "
                f"VALUES ('stale_{i}', 'old', datetime('now', '-{100 + i} days'))"
            )
        db.commit()

        result = mcp["session_start"]()
        assert "Stale knowledge" in result
        assert "3" in result

    def test_quality_gate_filters_weak_matches(self, rel_env):
        """Inserting unrelated content shouldn't match a specific query."""
        mcp, db, tracker = rel_env
        # Add something very specific
        mcp["add_knowledge"](topic="IRIX bsearch null", summary="bsearch crashes on nmemb=0")
        # Add something unrelated
        mcp["add_knowledge"](topic="python virtualenv", summary="use venv for isolation")

        result = mcp["search"](query="bsearch null crash")
        # Should find bsearch, should NOT find virtualenv
        assert "bsearch" in result.lower()
        # virtualenv shouldn't match "bsearch null crash" with quality gate
        assert "virtualenv" not in result.lower()
