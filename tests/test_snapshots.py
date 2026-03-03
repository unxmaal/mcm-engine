"""Tests for snapshots — save_snapshot, get_resume_context, handoff auto-snapshot."""
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
def snap_env(db, project_root):
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


class TestSaveSnapshot:
    def test_creates_snapshot(self, snap_env):
        mcp, db, tracker = snap_env
        result = mcp["save_snapshot"](
            goal="Build nano",
            progress="Compiled, linking",
            open_questions="Which malloc?",
            blockers="None",
            next_steps="Run tests",
            active_files="nano.yaml",
            key_decisions="Use dlmalloc",
        )
        assert "Snapshot #1 saved" in result

        row = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert row["goal"] == "Build nano"
        assert row["progress"] == "Compiled, linking"
        assert row["open_questions"] == "Which malloc?"
        assert row["next_steps"] == "Run tests"
        assert row["active_files"] == "nano.yaml"
        assert row["key_decisions"] == "Use dlmalloc"
        assert row["sequence_num"] == 1

    def test_sequential_numbering(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["save_snapshot"](goal="Step 1")
        mcp["save_snapshot"](goal="Step 2")
        mcp["save_snapshot"](goal="Step 3")

        rows = db.execute("SELECT sequence_num FROM snapshots ORDER BY id").fetchall()
        assert [r["sequence_num"] for r in rows] == [1, 2, 3]

    def test_links_to_session(self, snap_env):
        mcp, db, tracker = snap_env
        # Create a session first
        mcp["session_handoff"](status="test session")

        session = db.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        # The handoff auto-creates a snapshot, so sequence_num will be 2 for the next one
        mcp["save_snapshot"](goal="After handoff")

        snapshot = db.execute(
            "SELECT * FROM snapshots WHERE goal = 'After handoff'"
        ).fetchone()
        assert snapshot["session_id"] == session["id"]

    def test_minimal_snapshot(self, snap_env):
        mcp, db, tracker = snap_env
        result = mcp["save_snapshot"](goal="Just a goal")
        assert "Snapshot #1 saved" in result


class TestHandoffAutoSnapshot:
    def test_handoff_creates_snapshot(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["session_handoff"](
            status="completed build",
            current_task="testing grep",
            next_steps="deploy",
            blockers="none",
        )

        snapshot = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert snapshot is not None
        assert snapshot["goal"] == "testing grep"
        assert snapshot["progress"] == "completed build"
        assert snapshot["next_steps"] == "deploy"
        assert snapshot["blockers"] == "none"

    def test_handoff_snapshot_linked_to_session(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["session_handoff"](status="test")

        session = db.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        snapshot = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        assert snapshot["session_id"] == session["id"]


class TestGetResumeContext:
    def test_returns_last_session(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["session_handoff"](
            status="built nano",
            current_task="testing",
            next_steps="deploy to IRIX",
        )

        result = mcp["get_resume_context"]()
        assert "Last Session" in result
        assert "built nano" in result
        assert "deploy to IRIX" in result

    def test_returns_last_snapshot(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["save_snapshot"](
            goal="Build grep",
            progress="50% done",
            key_decisions="Use compat getline",
        )

        result = mcp["get_resume_context"]()
        assert "Last Snapshot" in result
        assert "Build grep" in result
        assert "50% done" in result

    def test_returns_pinned_items(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["add_knowledge"](topic="critical fact", summary="always remember this")
        row = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()
        mcp["pin_item"](entry_type="knowledge", entry_id=row["id"])

        result = mcp["get_resume_context"]()
        assert "Pinned Knowledge" in result
        assert "critical fact" in result

    def test_returns_pinned_rules(self, snap_env):
        mcp, db, tracker = snap_env
        mcp["add_rule"](title="Critical Rule", keywords="critical")
        row = db.execute("SELECT id FROM rules LIMIT 1").fetchone()
        mcp["pin_item"](entry_type="rule", entry_id=row["id"])

        result = mcp["get_resume_context"]()
        assert "Pinned Rules" in result
        assert "Critical Rule" in result

    def test_empty_context(self, snap_env):
        mcp, db, tracker = snap_env
        result = mcp["get_resume_context"]()
        assert "Project: test-project" in result

    def test_returns_project_name(self, snap_env):
        mcp, db, tracker = snap_env
        result = mcp["get_resume_context"]()
        assert "test-project" in result
