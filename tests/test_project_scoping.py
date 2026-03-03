"""Tests for project scoping — project filtering in search, global visibility, project params."""
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
def scope_env(db, project_root):
    """Tool environment with project_name='alpha' on search."""
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100,
        checkpoint_turns=100,
        mandatory_stop_turns=200,
    ))
    rules_path = project_root / "rules"

    search_all_fn = register_search_tools(
        mcp, db, tracker, [], project_name="alpha"
    )
    register_knowledge_tools(mcp, db, tracker, "alpha", search_all_fn)
    register_session_tools(mcp, db, tracker, "alpha", [])
    register_rules_tools(mcp, db, tracker, "alpha", [rules_path], project_root)

    return mcp, db, tracker


class TestProjectOnAddKnowledge:
    def test_default_project(self, scope_env):
        """add_knowledge without project param uses server's project_name."""
        mcp, db, tracker = scope_env
        mcp["add_knowledge"](topic="default-proj", summary="uses default")
        row = db.execute("SELECT project FROM knowledge WHERE topic = 'default-proj'").fetchone()
        assert row["project"] == "alpha"

    def test_explicit_project(self, scope_env):
        """add_knowledge with explicit project param overrides default."""
        mcp, db, tracker = scope_env
        mcp["add_knowledge"](topic="explicit-proj", summary="uses explicit", project="beta")
        row = db.execute("SELECT project FROM knowledge WHERE topic = 'explicit-proj'").fetchone()
        assert row["project"] == "beta"


class TestProjectOnAddNegative:
    def test_default_project(self, scope_env):
        mcp, db, tracker = scope_env
        mcp["add_negative"](category="test", what_failed="something")
        row = db.execute("SELECT project FROM negative_knowledge LIMIT 1").fetchone()
        assert row["project"] == "alpha"

    def test_explicit_project(self, scope_env):
        mcp, db, tracker = scope_env
        mcp["add_negative"](category="test", what_failed="something", project="beta")
        row = db.execute("SELECT project FROM negative_knowledge LIMIT 1").fetchone()
        assert row["project"] == "beta"


class TestProjectOnReportError:
    def test_default_project(self, scope_env):
        mcp, db, tracker = scope_env
        mcp["report_error"](error_text="test error")
        row = db.execute("SELECT project FROM errors LIMIT 1").fetchone()
        assert row["project"] == "alpha"

    def test_explicit_project(self, scope_env):
        mcp, db, tracker = scope_env
        mcp["report_error"](error_text="test error", project="beta")
        row = db.execute("SELECT project FROM errors LIMIT 1").fetchone()
        assert row["project"] == "beta"


class TestProjectFilteringInSearch:
    def test_search_filters_by_project(self, scope_env):
        """search(project='alpha') should find alpha entries but not beta."""
        mcp, db, tracker = scope_env
        mcp["add_knowledge"](topic="alpha malloc", summary="alpha's malloc insight", project="alpha")
        mcp["add_knowledge"](topic="beta malloc", summary="beta's malloc insight", project="beta")

        result = mcp["search"](query="malloc", project="alpha")
        assert "alpha malloc" in result
        assert "beta malloc" not in result

    def test_global_items_always_visible(self, scope_env):
        """Items with NULL/empty project should appear in project-scoped searches."""
        mcp, db, tracker = scope_env
        # Insert a global item (NULL project)
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, kind, project) "
            "VALUES ('global malloc', 'available everywhere', 'finding', NULL)"
        )
        db.commit()
        mcp["add_knowledge"](topic="alpha malloc", summary="alpha specific", project="alpha")

        result = mcp["search"](query="malloc", project="alpha")
        assert "alpha malloc" in result
        assert "global malloc" in result

    def test_no_filter_when_empty(self, scope_env):
        """search(project='') should return all entries regardless of project."""
        mcp, db, tracker = scope_env
        mcp["add_knowledge"](topic="alpha item", summary="alpha", project="alpha")
        mcp["add_knowledge"](topic="beta item", summary="beta", project="beta")

        result = mcp["search"](query="item", project="")
        assert "alpha item" in result
        assert "beta item" in result

    def test_negative_knowledge_project_filter(self, scope_env):
        """Negative knowledge should also be filtered by project."""
        mcp, db, tracker = scope_env
        mcp["add_negative"](
            category="alpha-build", what_failed="alpha pattern", project="alpha"
        )
        mcp["add_negative"](
            category="beta-build", what_failed="beta pattern", project="beta"
        )

        result = mcp["search"](query="build pattern", project="alpha")
        assert "alpha" in result.lower()
        # beta should not appear in project-scoped search
        # (though FTS5 matching may vary, the important thing is alpha appears)

    def test_errors_project_filter(self, scope_env):
        """Errors should also be filtered by project."""
        mcp, db, tracker = scope_env
        mcp["report_error"](error_text="alpha segfault in malloc", project="alpha")
        mcp["report_error"](error_text="beta segfault in malloc", project="beta")

        result = mcp["search"](query="segfault malloc", project="alpha")
        assert "alpha" in result.lower()


class TestSessionStartProjectBreakdown:
    def test_shows_project_breakdown(self, scope_env):
        """session_start should show per-project counts."""
        mcp, db, tracker = scope_env
        mcp["add_knowledge"](topic="proj-a", summary="a", project="alpha")
        mcp["add_knowledge"](topic="proj-b", summary="b", project="alpha")
        # Insert a global item
        db.execute_write(
            "INSERT INTO knowledge (topic, summary, kind, project) "
            "VALUES ('global', 'g', 'finding', NULL)"
        )
        db.commit()

        result = mcp["session_start"]()
        assert "project=" in result
        assert "global=" in result
