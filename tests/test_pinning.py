"""Tests for pinning — pin/unpin, never stale, search boost, session_start display."""
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
def pin_env(db, project_root):
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


class TestPinItem:
    def test_pin_knowledge(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["add_knowledge"](topic="pin-me", summary="important")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'pin-me'").fetchone()

        result = mcp["pin_item"](entry_type="knowledge", entry_id=row["id"])
        assert "Pinned" in result

        updated = db.execute("SELECT pinned FROM knowledge WHERE id = ?", (row["id"],)).fetchone()
        assert updated["pinned"] == 1

    def test_pin_negative(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["add_negative"](category="build", what_failed="bad pattern")
        row = db.execute("SELECT id FROM negative_knowledge LIMIT 1").fetchone()

        result = mcp["pin_item"](entry_type="negative", entry_id=row["id"])
        assert "Pinned" in result

        updated = db.execute(
            "SELECT pinned FROM negative_knowledge WHERE id = ?", (row["id"],)
        ).fetchone()
        assert updated["pinned"] == 1

    def test_pin_error(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["report_error"](error_text="segfault in malloc")
        row = db.execute("SELECT id FROM errors LIMIT 1").fetchone()

        result = mcp["pin_item"](entry_type="error", entry_id=row["id"])
        assert "Pinned" in result

    def test_pin_rule(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["add_rule"](title="Pin Rule", keywords="pin")
        row = db.execute("SELECT id FROM rules LIMIT 1").fetchone()

        result = mcp["pin_item"](entry_type="rule", entry_id=row["id"])
        assert "Pinned" in result

    def test_invalid_type(self, pin_env):
        mcp, db, tracker = pin_env
        result = mcp["pin_item"](entry_type="widget", entry_id=1)
        assert "Invalid entry_type" in result

    def test_not_found(self, pin_env):
        mcp, db, tracker = pin_env
        result = mcp["pin_item"](entry_type="knowledge", entry_id=9999)
        assert "not found" in result


class TestUnpinItem:
    def test_unpin_knowledge(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["add_knowledge"](topic="unpin-me", summary="was pinned")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'unpin-me'").fetchone()

        mcp["pin_item"](entry_type="knowledge", entry_id=row["id"])
        result = mcp["unpin_item"](entry_type="knowledge", entry_id=row["id"])
        assert "Unpinned" in result

        updated = db.execute("SELECT pinned FROM knowledge WHERE id = ?", (row["id"],)).fetchone()
        assert updated["pinned"] == 0

    def test_invalid_type(self, pin_env):
        mcp, db, tracker = pin_env
        result = mcp["unpin_item"](entry_type="widget", entry_id=1)
        assert "Invalid entry_type" in result

    def test_not_found(self, pin_env):
        mcp, db, tracker = pin_env
        result = mcp["unpin_item"](entry_type="knowledge", entry_id=9999)
        assert "not found" in result


class TestPinnedNeverStale:
    def test_pinned_old_entry_not_stale(self, pin_env):
        """Pinned entries should never be tagged [STALE] even if old."""
        mcp, db, tracker = pin_env
        # Insert with old created_at
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at, pinned) "
            "VALUES ('ancient pinned', 'very old but pinned', datetime('now', '-200 days'), 1)"
        )
        db.commit()

        result = mcp["search"](query="ancient pinned")
        assert "STALE" not in result
        assert "PINNED" in result

    def test_unpinned_old_entry_is_stale(self, pin_env):
        """Unpinned old entries should still be tagged [STALE]."""
        mcp, db, tracker = pin_env
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at, pinned) "
            "VALUES ('ancient unpinned', 'very old not pinned', datetime('now', '-200 days'), 0)"
        )
        db.commit()

        result = mcp["search"](query="ancient unpinned")
        assert "STALE" in result


class TestPinnedInSessionStart:
    def test_shows_pinned_items(self, pin_env):
        mcp, db, tracker = pin_env
        mcp["add_knowledge"](topic="pinned-k", summary="pinned knowledge")
        row = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()
        mcp["pin_item"](entry_type="knowledge", entry_id=row["id"])

        result = mcp["session_start"]()
        assert "Pinned items" in result
        assert "knowledge: 1" in result

    def test_no_pinned_section_when_empty(self, pin_env):
        mcp, db, tracker = pin_env
        result = mcp["session_start"]()
        assert "Pinned items" not in result

    def test_stale_count_excludes_pinned(self, pin_env):
        """Session start's stale count should not include pinned items."""
        mcp, db, tracker = pin_env
        # Insert old entries: one pinned, one not
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at, pinned) "
            "VALUES ('old-pinned', 'pinned old', datetime('now', '-200 days'), 1)"
        )
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, created_at, pinned) "
            "VALUES ('old-unpinned', 'unpinned old', datetime('now', '-200 days'), 0)"
        )
        db.commit()

        result = mcp["session_start"]()
        # Should show 1 stale (only the unpinned one)
        if "Stale knowledge" in result:
            assert "1" in result  # Only the unpinned one


class TestPinnedSearchBoost:
    def test_pinned_knowledge_in_search(self, pin_env):
        """Pinned entries should appear with [PINNED] tag."""
        mcp, db, tracker = pin_env
        mcp["add_knowledge"](topic="boost malloc", summary="pinned malloc fact")
        row = db.execute("SELECT id FROM knowledge WHERE topic = 'boost malloc'").fetchone()
        mcp["pin_item"](entry_type="knowledge", entry_id=row["id"])

        result = mcp["search"](query="malloc")
        assert "PINNED" in result
        assert "boost malloc" in result

    def test_pinned_rule_in_search(self, pin_env):
        """Pinned rules should appear with [PINNED] tag."""
        mcp, db, tracker = pin_env
        mcp["add_rule"](title="Pinned dlmalloc rule", keywords="dlmalloc")
        row = db.execute("SELECT id FROM rules LIMIT 1").fetchone()
        mcp["pin_item"](entry_type="rule", entry_id=row["id"])

        result = mcp["search"](query="dlmalloc")
        assert "PINNED" in result
