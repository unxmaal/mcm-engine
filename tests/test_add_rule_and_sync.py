"""Regression tests for cutover-test-plan defects #2 and #4:

  #2 — `add_rule` doesn't compute/store `content_hash`. The watcher
       cascade's no-op dedup (docs/watcher-cascade.md) depends on the
       row's content_hash matching the file's hash so an engine-
       initiated write doesn't re-cascade. Without it, every add_rule
       triggers a redundant watcher round-trip.

  #4 — `sync_rules` re-archives already-archived rows (resets
       archived_at, inflates the "N archived" count). Also: when a
       file reappears at an archived path, sync_rules should restore
       (unarchive) the row.

Both surfaced during the mcm2 cutover smoke test (Phase A.5).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.files.watcher import compute_content_hash
from mcm_engine.schema import migrate_core
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

    db_path = tmp_path / "rules.db"
    db = KnowledgeDB(db_path)
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
        "add_rule": mcp["add_rule"],
        "sync_rules": mcp["sync_rules"],
        "storage": SqliteStorage(db=db),
        "tmp_path": tmp_path,
        "rules_dir": rules_dir,
    }


# ---------------------------------------------------------------------------
# Defect #2 — add_rule must populate content_hash
# ---------------------------------------------------------------------------


def test_add_rule_new_file_populates_content_hash(wired):
    """When add_rule creates a new file, its content_hash must match
    the hash of what was written. Required by the watcher cascade's
    no-op dedup."""
    wired["add_rule"](title="Hash test A", keywords="hash,test")

    row = wired["storage"].find_rule_by_title("Hash test A")
    assert row is not None
    assert row.file_path is not None
    file_text = (wired["tmp_path"] / row.file_path).read_text(encoding="utf-8")
    expected_hash = compute_content_hash(file_text)
    assert row.content_hash == expected_hash, (
        "content_hash mismatch — watcher will treat the engine write as "
        "an external edit and re-cascade pointlessly"
    )


def test_add_rule_existing_file_indexes_with_content_hash(wired):
    """add_rule(file_path=...) pointing at a pre-existing file should
    still hash the file content into the row."""
    rule_path = wired["rules_dir"] / "preexisting.md"
    rule_path.write_text(
        "# Hash test B\n\n**Keywords:** hash,test\n\nbody\n",
        encoding="utf-8",
    )
    wired["add_rule"](
        title="Hash test B", keywords="hash,test",
        file_path="rules/preexisting.md",
    )

    row = wired["storage"].find_rule_by_title("Hash test B")
    assert row is not None
    expected = compute_content_hash(rule_path.read_text(encoding="utf-8"))
    assert row.content_hash == expected


def test_add_rule_content_hash_changes_when_content_changes(wired):
    """Two rules with different content must have different hashes —
    sanity check that the hash isn't being computed off something
    invariant like the title."""
    wired["add_rule"](title="Hash A", keywords="kw", content="alpha body")
    wired["add_rule"](title="Hash B", keywords="kw", content="beta body")

    a = wired["storage"].find_rule_by_title("Hash A")
    b = wired["storage"].find_rule_by_title("Hash B")
    assert a.content_hash and b.content_hash
    assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# Defect #4 — sync_rules should skip already-archived rows + restore reappeared
# ---------------------------------------------------------------------------


def test_sync_rules_skips_already_archived(wired):
    """An already-archived row should not be re-archived by sync_rules."""
    rid = wired["storage"].insert_rule(RuleRow(
        id=0, title="prior orphan", keywords="kw",
        file_path="rules/never-existed.md",
    ))
    wired["storage"].soft_delete_rule(rid)

    archived_at_before = wired["storage"].find_by_id(EntityType.RULE, rid).archived_at
    assert archived_at_before is not None

    # No new file; sync_rules iterates the same row.
    msg = wired["sync_rules"]()
    assert "0 orphans archived" in msg, (
        f"sync_rules re-archived an already-archived row. Message: {msg}"
    )

    archived_at_after = wired["storage"].find_by_id(EntityType.RULE, rid).archived_at
    assert archived_at_after == archived_at_before, (
        "sync_rules reset archived_at on an already-archived row"
    )


def test_sync_rules_restores_reappeared_file(wired):
    """If a file is deleted (archived), then re-created at the same path,
    the next sync_rules must un-archive the row."""
    # Create + sync to register the row.
    rule_path = wired["rules_dir"] / "comeback.md"
    rule_path.write_text(
        "# Comeback rule\n\n**Keywords:** comeback\n\nfirst body\n",
        encoding="utf-8",
    )
    wired["sync_rules"]()
    rid = wired["storage"].find_rule_by_file_path("rules/comeback.md").id

    # Delete + sync to archive.
    rule_path.unlink()
    wired["sync_rules"]()
    assert wired["storage"].find_by_id(EntityType.RULE, rid).archived is True

    # Re-create + sync to restore.
    rule_path.write_text(
        "# Comeback rule\n\n**Keywords:** comeback\n\nrestored body\n",
        encoding="utf-8",
    )
    wired["sync_rules"]()
    row = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert row.archived is False, (
        "sync_rules did not restore a row whose file came back"
    )


def test_sync_rules_idempotent_archive_count(wired):
    """Running sync_rules twice in a row with no on-disk changes must
    report 0 orphans archived on the second run."""
    rid = wired["storage"].insert_rule(RuleRow(
        id=0, title="orphan", keywords="kw",
        file_path="rules/gone.md",
    ))
    msg1 = wired["sync_rules"]()
    assert "1 orphans archived" in msg1
    msg2 = wired["sync_rules"]()
    assert "0 orphans archived" in msg2, (
        f"Second sync_rules re-counted archived rows. Message: {msg2}"
    )
