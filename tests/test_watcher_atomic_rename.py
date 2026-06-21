"""Cutover defect #8: macOS BSD `sed -i ''` does atomic-rename (write
to .!PID!file then rename over the original), and watchdog reports a
TRAILING FileDeletedEvent for the original .md path after the rename
completes. Our 500ms debounce coalesces all the events into "last op
wins" — which is `delete` — so the row gets soft-deleted even though
the file still exists.

The fix: in ``_cascade_delete`` verify the file is actually gone
before archiving. If it still exists, treat the spurious delete as
an upsert and re-cascade the current content.

This is real-world: any editor doing atomic save (most modern editors:
sed, vim's writebackup, IntelliJ, VSCode under certain configurations)
hits this pattern.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType
from mcm_engine.files.watcher import RulesWatcher
from mcm_engine.schema import migrate_core
from mcm_engine.db import KnowledgeDB


_DEBOUNCE_MS = 50
_SETTLE_S = 0.6


@pytest.fixture
def wired(tmp_path):
    db_path = tmp_path / "atomic.db"
    db = KnowledgeDB(db_path)
    migrate_core(db)
    storage = SqliteStorage(db=db)

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    w = RulesWatcher(storage, rules_dir, tmp_path, debounce_ms=_DEBOUNCE_MS)
    yield {"watcher": w, "storage": storage, "rules_dir": rules_dir}
    w.stop()


def test_sed_inplace_edit_does_not_archive_row(wired):
    """sed -i '' on a rule file MUST end with the row containing the
    new content, NOT archived. Defect #8 from the live cutover Phase C:
    the trailing FileDeletedEvent from atomic-rename was archiving rows
    whose files were still on disk."""
    rule_path = wired["rules_dir"] / "sed-target.md"
    rule_path.write_text(
        "# Sed target rule\n\n**Keywords:** sed,test\n\noriginal body line\n",
        encoding="utf-8",
    )
    wired["watcher"].sync_once()
    rid = wired["storage"].find_rule_by_file_path("rules/sed-target.md").id

    wired["watcher"].start()
    # macOS BSD sed -i syntax. On Linux this would be `sed -i 's/.../.../'`
    # without the empty-string positional argument; the behavior is the
    # same — atomic-rename via temp file.
    subprocess.run(
        ["sed", "-i", "", "s/original body line/updated body line/", str(rule_path)],
        check=True,
    )
    time.sleep(_SETTLE_S)

    row = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert row is not None
    assert row.archived is False, (
        "sed -i atomic-rename produced a FileDeletedEvent that the watcher "
        "treated as a real delete — row was archived even though the file "
        "still exists. Defect #8."
    )
    assert row.description and "updated body line" in row.description, (
        "watcher saw the rename + delete sequence but didn't pick up the "
        "new content"
    )


def test_spurious_delete_event_with_file_still_present_does_not_archive(wired):
    """Direct synthetic version of defect #8. Bypasses watchdog timing
    quirks: directly invokes the watcher's internal scheduler with a
    delete op for a path whose file still exists."""
    rule_path = wired["rules_dir"] / "phantom-delete.md"
    rule_path.write_text(
        "# Phantom delete\n\n**Keywords:** phantom\n\noriginal\n",
        encoding="utf-8",
    )
    wired["watcher"].sync_once()
    rid = wired["storage"].find_rule_by_file_path("rules/phantom-delete.md").id

    # Schedule a delete for a path whose file still exists. Simulates
    # the trailing FileDeletedEvent from an atomic-rename.
    wired["watcher"]._cascade_delete(rule_path)

    row = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert row.archived is False, (
        "_cascade_delete archived a row whose file still exists on disk"
    )


def test_real_delete_still_archives(wired):
    """Regression guard for the defect #8 fix — actually deleting the
    file must still archive the row."""
    rule_path = wired["rules_dir"] / "really-deleted.md"
    rule_path.write_text(
        "# Really deleted\n\n**Keywords:** real\n\nbody\n",
        encoding="utf-8",
    )
    wired["watcher"].sync_once()
    rid = wired["storage"].find_rule_by_file_path("rules/really-deleted.md").id

    rule_path.unlink()
    wired["watcher"]._cascade_delete(rule_path)

    row = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert row.archived is True, (
        "_cascade_delete should still archive when the file is genuinely gone"
    )
