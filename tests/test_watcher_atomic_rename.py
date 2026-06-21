"""Cutover defect #8: an atomic-rename file save (write to ``.tmp`` then
``rename(tmp, target)``) emits a TRAILING ``FileDeletedEvent`` for the
original path even though the file still exists on disk after the
rename completes. Our 500ms debounce coalesces the event sequence into
"last op wins" — which is ``delete`` — so the row was being soft-deleted
even though the file was still there.

This is the save pattern used by vim's ``writebackup``, IntelliJ,
VSCode under certain configurations, BSD/GNU ``sed -i``, and most
other modern editors. We simulate it portably in Python — write to a
NamedTemporaryFile in the same directory, then ``os.replace`` over the
target — which makes the exact same kernel syscalls (and emits the
exact same fsnotify events) as the editors above.

The fix: in ``_cascade_delete`` verify the file is actually gone
before archiving. If it still exists, treat the spurious delete as
an upsert and re-cascade the current content.
"""
from __future__ import annotations

import os
import tempfile
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


def _atomic_rename_replace(path: Path, new_content: str) -> None:
    """The portable equivalent of what every atomic-save editor does
    under the hood: write transformed content to a temp file in the
    SAME directory, then ``os.replace`` it over the target. ``os.replace``
    is atomic on POSIX (single ``rename(2)`` syscall) and produces the
    same fsnotify event sequence as ``sed -i``, vim writebackup, etc."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tf:
        tf.write(new_content)
        tmp_path = tf.name
    os.replace(tmp_path, path)


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


def test_atomic_rename_edit_does_not_archive_row(wired):
    """An atomic-rename file save MUST end with the row containing the
    new content, NOT archived. Defect #8 from the live cutover Phase C:
    the trailing FileDeletedEvent from atomic-rename was archiving rows
    whose files were still on disk."""
    rule_path = wired["rules_dir"] / "edited-target.md"
    rule_path.write_text(
        "# Atomic-rename target\n\n**Keywords:** atomic,test\n\noriginal body line\n",
        encoding="utf-8",
    )
    wired["watcher"].sync_once()
    rid = wired["storage"].find_rule_by_file_path("rules/edited-target.md").id

    wired["watcher"].start()
    _atomic_rename_replace(
        rule_path,
        "# Atomic-rename target\n\n**Keywords:** atomic,test\n\nupdated body line\n",
    )
    time.sleep(_SETTLE_S)

    row = wired["storage"].find_by_id(EntityType.RULE, rid)
    assert row is not None
    assert row.archived is False, (
        "atomic-rename produced a FileDeletedEvent that the watcher "
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
