"""Tests for external rules file tools."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import (
    _generate_rule_content,
    _parse_rule_file,
    _slugify,
    register_rules_tools,
)
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
def rules_env(db, project_root):
    """Full tool environment with rules tools registered."""
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

    return mcp, db, tracker, rules_path, project_root


# --- Slugify tests ---

class TestSlugify:
    def test_basic(self):
        assert _slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert _slugify("IRIX rld ignores DT_RUNPATH!") == "irix-rld-ignores-dt-runpath"

    def test_empty(self):
        assert _slugify("") == "untitled"
        assert _slugify("   ") == "untitled"

    def test_unicode(self):
        result = _slugify("Über cool stuff")
        assert "ber" in result or result == "ber-cool-stuff"

    def test_multiple_separators(self):
        assert _slugify("foo--bar__baz  qux") == "foo-bar-baz-qux"


# --- Parse rule file tests ---

class TestParseRuleFile:
    def test_standard_format(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(
            "# IRIX rld ignores DT_RUNPATH\n\n"
            "**Keywords:** rld, runpath, DT_RUNPATH\n"
            "**Category:** linker\n\n"
            "IRIX rld does not honor DT_RUNPATH in ELF binaries.\n\n"
            "## Fix\n\nUse -Wl,--disable-new-dtags\n"
        )
        parsed = _parse_rule_file(f)
        assert parsed["title"] == "IRIX rld ignores DT_RUNPATH"
        assert "rld" in parsed["keywords"]
        assert parsed["category"] == "linker"
        assert "IRIX rld does not honor" in parsed["description"]

    def test_minimal(self, tmp_path):
        f = tmp_path / "minimal.md"
        f.write_text("# Just a title\n\nSome body text.\n")
        parsed = _parse_rule_file(f)
        assert parsed["title"] == "Just a title"
        assert parsed["description"] == "Some body text."
        assert "keywords" not in parsed
        assert "category" not in parsed

    def test_missing_fields(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("No heading here.\n")
        parsed = _parse_rule_file(f)
        assert "title" not in parsed

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.md"
        parsed = _parse_rule_file(f)
        assert parsed == {}


# --- add_rule tests ---

class TestAddRule:
    def test_creates_file_and_db_entry(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        result = mcp["add_rule"](
            title="Test Rule",
            keywords="test, rule",
            content="This is a test rule.",
            category="testing",
        )
        assert "Rule added" in result
        assert "File:" in result

        # Check DB
        row = db.execute("SELECT * FROM rules WHERE title = 'Test Rule'").fetchone()
        assert row is not None
        assert row["keywords"] == "test, rule"
        assert row["category"] == "testing"

        # Check file exists
        expected_file = rules_path / "testing" / "test-rule.md"
        assert expected_file.exists()
        content = expected_file.read_text()
        assert "# Test Rule" in content
        assert "**Keywords:** test, rule" in content

    def test_indexes_existing_file(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        # Create a file manually
        f = rules_path / "existing.md"
        f.write_text(
            "# Existing Rule\n\n"
            "**Keywords:** existing, manual\n"
            "**Category:** manual\n\n"
            "This was created manually.\n"
        )
        result = mcp["add_rule"](
            title="Existing Rule",
            keywords="existing, manual",
            file_path="rules/existing.md",
            category="manual",
        )
        assert "Rule added" in result

        row = db.execute("SELECT * FROM rules WHERE title = 'Existing Rule'").fetchone()
        assert row is not None
        assert row["file_path"] == "rules/existing.md"

    def test_warns_missing_file(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        result = mcp["add_rule"](
            title="Ghost Rule",
            keywords="ghost",
            file_path="rules/nonexistent.md",
        )
        assert "Warning" in result
        assert "does not exist" in result

    def test_dedup_updates_existing(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_rule"](title="Dup Rule", keywords="first", content="v1")
        result = mcp["add_rule"](title="Dup Rule", keywords="second", content="v2")
        assert "Updated existing rule" in result

        rows = db.execute("SELECT * FROM rules WHERE title = 'Dup Rule'").fetchall()
        assert len(rows) == 1
        assert rows[0]["keywords"] == "second"

    def test_no_overwrite_existing_file(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        # Create first rule
        mcp["add_rule"](title="Same Name", keywords="a", content="first")
        # Create second with different title but same slug won't happen,
        # but creating with same slug via same title (different invocation) would.
        # Since dedup catches by title, create one with different title but same slug base
        cat_dir = rules_path / ""
        (cat_dir / "same-name.md").exists()  # First one is here

        # Add another with different title that slugifies differently
        mcp["add_rule"](title="Other Name", keywords="b", content="second")
        assert (rules_path / "other-name.md").exists()


# --- read_rule tests ---

class TestReadRule:
    def test_reads_file(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        f = rules_path / "readable.md"
        f.write_text("# Readable Rule\n\nContent here.\n")
        # Index it first
        mcp["add_rule"](
            title="Readable Rule",
            keywords="read",
            file_path="rules/readable.md",
        )

        result = mcp["read_rule"](file_path="rules/readable.md")
        assert "# Readable Rule" in result
        assert "Content here." in result

    def test_increments_hit_count(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        f = rules_path / "counter.md"
        f.write_text("# Counter Rule\n\nBody.\n")
        mcp["add_rule"](
            title="Counter Rule",
            keywords="counter",
            file_path="rules/counter.md",
        )

        mcp["read_rule"](file_path="rules/counter.md")
        mcp["read_rule"](file_path="rules/counter.md")

        row = db.execute(
            "SELECT hit_count FROM rules WHERE file_path = 'rules/counter.md'"
        ).fetchone()
        assert row["hit_count"] >= 2

    def test_handles_missing(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        result = mcp["read_rule"](file_path="rules/nope.md")
        assert "not found" in result


# --- promote_to_rule tests ---

class TestPromoteToRule:
    def test_from_knowledge(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_knowledge"](
            topic="WAL mode",
            summary="Use WAL for concurrency",
            detail="WAL allows concurrent reads while writing",
            tags="sqlite,wal",
        )
        row = db.execute("SELECT id FROM knowledge LIMIT 1").fetchone()

        result = mcp["promote_to_rule"](
            source_type="knowledge",
            source_id=row["id"],
            title="SQLite WAL Mode",
            category="database",
        )
        assert "Rule added" in result

        # Verify file was created
        rule_file = rules_path / "database" / "sqlite-wal-mode.md"
        assert rule_file.exists()

    def test_from_negative(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_negative"](
            category="build",
            what_failed="inline C in YAML",
            why_failed="unmaintainable",
            correct_approach="use patches/ dir",
        )
        row = db.execute("SELECT id FROM negative_knowledge LIMIT 1").fetchone()

        result = mcp["promote_to_rule"](
            source_type="negative",
            source_id=row["id"],
            title="No Inline C in YAML",
            category="build",
        )
        assert "Rule added" in result

    def test_from_error(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["report_error"](
            error_text="undefined reference to getline",
            context="linking nano",
        )
        row = db.execute("SELECT id FROM errors LIMIT 1").fetchone()

        # Add root_cause and fix to the error
        db.execute_write(
            "UPDATE errors SET root_cause = 'IRIX lacks getline', fix = 'use compat getline' WHERE id = ?",
            (row["id"],),
        )
        db.commit()

        result = mcp["promote_to_rule"](
            source_type="error",
            source_id=row["id"],
            title="IRIX Missing getline",
            category="compat",
        )
        assert "Rule added" in result

    def test_invalid_source_type(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        result = mcp["promote_to_rule"](
            source_type="invalid",
            source_id=1,
            title="Nope",
        )
        assert "Invalid source_type" in result

    def test_missing_source_id(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        result = mcp["promote_to_rule"](
            source_type="knowledge",
            source_id=9999,
            title="Nope",
        )
        assert "not found" in result


# --- sync_rules tests ---

class TestSyncRules:
    def test_indexes_new_files(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        # Create a rule file manually
        f = rules_path / "manual.md"
        f.write_text(
            "# Manual Rule\n\n"
            "**Keywords:** manual, test\n"
            "**Category:** testing\n\n"
            "Created by hand.\n"
        )

        result = mcp["sync_rules"]()
        assert "1 new" in result

        row = db.execute("SELECT * FROM rules WHERE title = 'Manual Rule'").fetchone()
        assert row is not None
        assert row["keywords"] == "manual, test"
        assert row["category"] == "testing"

    def test_updates_existing(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        # Create and index a file
        f = rules_path / "updatable.md"
        f.write_text("# Original Title\n\n**Keywords:** old\n\nOld content.\n")
        mcp["sync_rules"]()

        # Update the file
        f.write_text("# Updated Title\n\n**Keywords:** new, updated\n\nNew content.\n")
        result = mcp["sync_rules"]()
        assert "1 updated" in result

        row = db.execute(
            "SELECT * FROM rules WHERE file_path = 'rules/updatable.md'"
        ).fetchone()
        assert row["title"] == "Updated Title"
        assert row["keywords"] == "new, updated"

    def test_removes_orphans(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        # Create and index a file
        f = rules_path / "doomed.md"
        f.write_text("# Doomed Rule\n\n**Keywords:** doomed\n\nGoing away.\n")
        mcp["sync_rules"]()

        # Delete the file
        f.unlink()
        result = mcp["sync_rules"]()
        assert "1 orphans removed" in result

        row = db.execute(
            "SELECT * FROM rules WHERE title = 'Doomed Rule'"
        ).fetchone()
        assert row is None

    def test_missing_rules_dir(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        import shutil
        shutil.rmtree(rules_path)
        result = mcp["sync_rules"]()
        assert "No rules directories found" in result

    def test_subdirectory_indexing(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        subdir = rules_path / "linker"
        subdir.mkdir()
        f = subdir / "rld-runpath.md"
        f.write_text(
            "# IRIX rld ignores DT_RUNPATH\n\n"
            "**Keywords:** rld, runpath\n"
            "**Category:** linker\n\n"
            "Description here.\n"
        )
        mcp["sync_rules"]()

        row = db.execute(
            "SELECT * FROM rules WHERE title = 'IRIX rld ignores DT_RUNPATH'"
        ).fetchone()
        assert row is not None
        assert row["file_path"] == "rules/linker/rld-runpath.md"


# --- Integration tests ---

class TestRulesIntegration:
    def test_search_finds_rules(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_rule"](
            title="IRIX dlmalloc in executables only",
            keywords="dlmalloc, malloc, heap",
            content="Never link dlmalloc into shared libraries.",
            category="linker",
        )

        result = mcp["search"](query="dlmalloc")
        assert "RULE" in result
        assert "dlmalloc" in result.lower()

    def test_report_error_auto_searches_rules(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_rule"](
            title="IRIX lacks getline",
            keywords="getline, undefined, missing",
            content="Use compat getline implementation.",
            category="compat",
        )

        result = mcp["report_error"](error_text="undefined reference to getline")
        assert "Error logged" in result
        # Should find the rule about getline
        assert "getline" in result.lower()

    def test_session_start_shows_rules_count(self, rules_env):
        mcp, db, tracker, rules_path, project_root = rules_env
        mcp["add_rule"](title="Rule A", keywords="a", content="body a")
        mcp["add_rule"](title="Rule B", keywords="b", content="body b")

        result = mcp["session_start"]()
        assert "Rules indexed: 2" in result


# --- Multi-path tests ---

class TestMultiPathRules:
    @pytest.fixture
    def multi_env(self, db, project_root):
        """Environment with two rules paths: project-local + shared external."""
        mcp = FakeMCP()
        tracker = SessionTracker(NudgeConfig(
            store_reminder_turns=100,
            checkpoint_turns=100,
            mandatory_stop_turns=200,
        ))
        local_rules = project_root / "rules"
        # Create an external shared rules directory (outside project root)
        shared_rules = project_root.parent / "shared-rules"
        shared_rules.mkdir(exist_ok=True)

        search_all_fn = register_search_tools(mcp, db, tracker, [])
        register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
        register_session_tools(mcp, db, tracker, "test-project", [])
        register_rules_tools(
            mcp, db, tracker, "test-project",
            [local_rules, shared_rules], project_root,
        )

        return mcp, db, tracker, local_rules, shared_rules, project_root

    def test_new_files_created_in_primary_path(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        result = mcp["add_rule"](
            title="Local Rule",
            keywords="local",
            content="Created in primary path.",
            category="testing",
        )
        assert "Rule added" in result
        assert (local_rules / "testing" / "local-rule.md").exists()
        assert not list(shared_rules.rglob("local-rule.md"))

    def test_sync_indexes_both_paths(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        # Create a rule in local path
        f1 = local_rules / "local.md"
        f1.write_text("# Local Rule\n\n**Keywords:** local\n\nLocal content.\n")
        # Create a rule in shared path
        f2 = shared_rules / "shared.md"
        f2.write_text("# Shared Rule\n\n**Keywords:** shared, bigcorp\n\nShared content.\n")

        result = mcp["sync_rules"]()
        assert "2 new" in result

        local_row = db.execute("SELECT * FROM rules WHERE title = 'Local Rule'").fetchone()
        shared_row = db.execute("SELECT * FROM rules WHERE title = 'Shared Rule'").fetchone()
        assert local_row is not None
        assert shared_row is not None
        # Local path is relative; shared path is absolute (outside project root)
        assert local_row["file_path"] == "rules/local.md"
        assert shared_row["file_path"] == str(f2)

    def test_search_finds_shared_rules(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        f = shared_rules / "bigcorp-auth.md"
        f.write_text(
            "# BigCorp SSO Integration\n\n"
            "**Keywords:** sso, auth, oauth, bigcorp\n"
            "**Category:** auth\n\n"
            "Always use the internal SSO gateway at sso.bigcorp.internal.\n"
        )
        mcp["sync_rules"]()

        result = mcp["search"](query="bigcorp sso auth")
        assert "RULE" in result
        assert "SSO" in result

    def test_read_rule_works_with_absolute_path(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        f = shared_rules / "readable-shared.md"
        f.write_text("# Readable Shared\n\n**Keywords:** shared\n\nShared content.\n")
        mcp["sync_rules"]()

        result = mcp["read_rule"](file_path=str(f))
        assert "# Readable Shared" in result
        assert "Shared content." in result

    def test_orphan_removal_for_external_path(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        f = shared_rules / "doomed-shared.md"
        f.write_text("# Doomed Shared\n\n**Keywords:** doomed\n\nGoing away.\n")
        mcp["sync_rules"]()

        # Verify it was indexed
        row = db.execute("SELECT * FROM rules WHERE title = 'Doomed Shared'").fetchone()
        assert row is not None

        # Delete the file and re-sync
        f.unlink()
        result = mcp["sync_rules"]()
        assert "1 orphans removed" in result

        row = db.execute("SELECT * FROM rules WHERE title = 'Doomed Shared'").fetchone()
        assert row is None

    def test_missing_shared_path_still_syncs_local(self, multi_env):
        mcp, db, tracker, local_rules, shared_rules, project_root = multi_env
        # Create a local rule
        f = local_rules / "survives.md"
        f.write_text("# Survivor\n\n**Keywords:** local\n\nStill here.\n")

        # Remove shared path
        import shutil
        shutil.rmtree(shared_rules)

        result = mcp["sync_rules"]()
        assert "1 new" in result
        row = db.execute("SELECT * FROM rules WHERE title = 'Survivor'").fetchone()
        assert row is not None
