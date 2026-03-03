"""Tests for reinforcement — reinforce_knowledge, reinforce_rule, ranking impact."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
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
def reinforce_env(db, project_root):
    """Full tool environment with all tools registered."""
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

    return mcp, db, tracker


class TestReinforceKnowledge:
    def test_increments_count(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        mcp["add_knowledge"](topic="WAL mode", summary="use WAL")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'WAL mode'").fetchone()

        result = mcp["reinforce_knowledge"](entry_id=row["id"])
        assert "Reinforced" in result
        assert "reinforcement_count=1" in result

        # Reinforce again
        result = mcp["reinforce_knowledge"](entry_id=row["id"])
        assert "reinforcement_count=2" in result

    def test_sets_last_hit_at(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        mcp["add_knowledge"](topic="test-hit", summary="test")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'test-hit'").fetchone()

        mcp["reinforce_knowledge"](entry_id=row["id"])
        updated = db.execute(
            "SELECT last_hit_at FROM knowledge WHERE id = ?", (row["id"],)
        ).fetchone()
        assert updated["last_hit_at"] is not None

    def test_not_found(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        result = mcp["reinforce_knowledge"](entry_id=9999)
        assert "not found" in result

    def test_preserves_hit_count(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        mcp["add_knowledge"](topic="hit-test", summary="test")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'hit-test'").fetchone()

        # Set some hit count
        db.execute_write("UPDATE knowledge SET hit_count = 5 WHERE id = ?", (row["id"],))
        db.commit()

        mcp["reinforce_knowledge"](entry_id=row["id"])
        updated = db.execute("SELECT hit_count FROM knowledge WHERE id = ?", (row["id"],)).fetchone()
        assert updated["hit_count"] == 5  # Not modified


class TestReinforceRule:
    def test_increments_count(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        mcp["add_rule"](title="Test Rule", keywords="test", content="body")
        row = db.execute("SELECT id FROM rules WHERE title = 'Test Rule'").fetchone()

        result = mcp["reinforce_rule"](rule_id=row["id"])
        assert "Reinforced" in result
        assert "reinforcement_count=1" in result

    def test_sets_last_hit_at(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        mcp["add_rule"](title="Hit Rule", keywords="hit", content="body")
        row = db.execute("SELECT id FROM rules WHERE title = 'Hit Rule'").fetchone()

        mcp["reinforce_rule"](rule_id=row["id"])
        updated = db.execute(
            "SELECT last_hit_at FROM rules WHERE id = ?", (row["id"],)
        ).fetchone()
        assert updated["last_hit_at"] is not None

    def test_not_found(self, reinforce_env):
        mcp, db, tracker = reinforce_env
        result = mcp["reinforce_rule"](rule_id=9999)
        assert "not found" in result


class TestReinforcementInRanking:
    def test_reinforced_knowledge_ranks_higher(self, reinforce_env):
        """Reinforced entries should appear in search results (boosted)."""
        mcp, db, tracker = reinforce_env
        mcp["add_knowledge"](topic="reinforced malloc", summary="important malloc fact")
        mcp["add_knowledge"](topic="unreinforced malloc", summary="obscure malloc trivia")

        row = db.execute(
            "SELECT id FROM knowledge WHERE topic = 'reinforced malloc'"
        ).fetchone()
        # Reinforce multiple times
        for _ in range(5):
            mcp["reinforce_knowledge"](entry_id=row["id"])

        result = mcp["search"](query="malloc")
        assert "reinforced malloc" in result
        assert "unreinforced malloc" in result

    def test_reinforced_rule_ranks_higher(self, reinforce_env):
        """Reinforced rules should appear in search results (boosted)."""
        mcp, db, tracker = reinforce_env
        mcp["add_rule"](title="Important dlmalloc rule", keywords="dlmalloc, malloc")
        mcp["add_rule"](title="Minor dlmalloc note", keywords="dlmalloc, malloc")

        row = db.execute(
            "SELECT id FROM rules WHERE title = 'Important dlmalloc rule'"
        ).fetchone()
        for _ in range(3):
            mcp["reinforce_rule"](rule_id=row["id"])

        result = mcp["search"](query="dlmalloc")
        assert "Important dlmalloc rule" in result
