"""MCM2-23: files-win watcher cascade.

Covers the seven scenarios documented in docs/watcher-cascade.md.
Backend-down buffer/drain is deferred to a future task — we test only
the happy paths plus deletion and rename here.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.files.watcher import RulesWatcher, compute_content_hash


# Short debounce in tests so we don't have to sleep 500ms.
_DEBOUNCE_MS = 50
# Sleep budget after writes — must exceed debounce + cascade work + the
# observable fsevents queueing latency on macOS, which under concurrent
# write-then-read can drift a few hundred ms.
_SETTLE_S = 0.6


@pytest.fixture
def storage(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "watcher.db"))
    s.ensure_schema()
    return s


@pytest.fixture
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def watcher(storage, rules_dir, tmp_path):
    w = RulesWatcher(
        storage, rules_dir, tmp_path, debounce_ms=_DEBOUNCE_MS,
    )
    yield w
    w.stop()


def _write_rule(path: Path, title: str, body: str = "body line") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n**Keywords:** kw1, kw2\n\n{body}\n", encoding="utf-8")


# ---- sync_once (startup behavior) -----------------------------------------


def test_sync_once_inserts_files_into_storage(watcher, rules_dir, storage):
    _write_rule(rules_dir / "a.md", "Alpha rule")
    _write_rule(rules_dir / "nested" / "b.md", "Bravo rule")

    counts = watcher.sync_once()
    assert counts["upserted"] == 2

    titles = sorted(r.title for r in storage.list_rules_with_file_paths())
    assert titles == ["Alpha rule", "Bravo rule"]


def test_sync_once_archives_orphans(watcher, rules_dir, storage):
    # Pre-existing row pointing at a file that no longer exists.
    rid = storage.insert_rule(RuleRow(
        id=0, title="Stale", keywords="kw", file_path="rules/missing.md",
    ))
    _write_rule(rules_dir / "live.md", "Live rule")

    counts = watcher.sync_once()
    assert counts["upserted"] == 1
    assert counts["archived"] == 1

    stale = storage.find_by_id(EntityType.RULE, rid)
    assert stale.archived is True


def test_sync_once_unchanged_files_dont_double_cascade(watcher, rules_dir):
    _write_rule(rules_dir / "a.md", "Alpha")
    watcher.sync_once()
    first_count = watcher.cascade_count

    counts = watcher.sync_once()
    assert counts["unchanged"] == 1
    # Second sync sees identical content_hash → no write.
    assert watcher.cascade_count == first_count


# ---- live cascade ---------------------------------------------------------


def test_external_edit_cascades_within_debounce(watcher, rules_dir, storage):
    """The headline scenario: human edits a file, watcher cascades to DB."""
    rule_path = rules_dir / "x.md"
    _write_rule(rule_path, "Original")
    watcher.sync_once()

    watcher.start()
    _write_rule(rule_path, "Updated title", body="new body content")
    time.sleep(_SETTLE_S)

    row = storage.find_rule_by_file_path("rules/x.md")
    assert row is not None
    assert row.title == "Updated title"


def test_engine_write_does_not_double_cascade(watcher, rules_dir, storage):
    """The content-hash check prevents re-writes when the engine wrote
    the same content the watcher is about to see."""
    rule_path = rules_dir / "y.md"
    _write_rule(rule_path, "Y rule")
    watcher.sync_once()  # one cascade
    baseline = watcher.cascade_count

    watcher.start()
    # Pretend the engine wrote the same content; watcher fires for the
    # file system event but content_hash matches, no extra write.
    rule_path.write_text(rule_path.read_text(encoding="utf-8"), encoding="utf-8")
    time.sleep(_SETTLE_S)

    assert watcher.cascade_count == baseline, (
        f"watcher re-cascaded an unchanged file "
        f"(baseline={baseline}, after={watcher.cascade_count})"
    )


def test_deletion_soft_deletes_row(watcher, rules_dir, storage):
    rule_path = rules_dir / "z.md"
    _write_rule(rule_path, "Z rule")
    watcher.sync_once()
    rid = storage.find_rule_by_file_path("rules/z.md").id

    watcher.start()
    rule_path.unlink()
    time.sleep(_SETTLE_S)

    row = storage.find_by_id(EntityType.RULE, rid)
    assert row is not None  # not hard-deleted
    assert row.archived is True


def test_recreate_after_delete_restores_row(watcher, rules_dir, storage):
    rule_path = rules_dir / "r.md"
    _write_rule(rule_path, "R rule")
    watcher.sync_once()
    rid = storage.find_rule_by_file_path("rules/r.md").id

    watcher.start()
    rule_path.unlink()
    time.sleep(_SETTLE_S)
    assert storage.find_by_id(EntityType.RULE, rid).archived is True

    _write_rule(rule_path, "R rule recreated", body="post-restore body")
    time.sleep(_SETTLE_S)

    row = storage.find_by_id(EntityType.RULE, rid)
    assert row.archived is False
    assert row.title == "R rule recreated"


def test_rename_archives_old_creates_new(watcher, rules_dir, storage):
    old_path = rules_dir / "old" / "foo.md"
    _write_rule(old_path, "Foo rule")
    watcher.sync_once()
    old_rid = storage.find_rule_by_file_path("rules/old/foo.md").id

    watcher.start()
    new_path = rules_dir / "new" / "foo.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    time.sleep(_SETTLE_S)

    old_row = storage.find_by_id(EntityType.RULE, old_rid)
    assert old_row.archived is True
    new_row = storage.find_rule_by_file_path("rules/new/foo.md")
    assert new_row is not None
    assert new_row.title == "Foo rule"


# ---- mode discipline ------------------------------------------------------


def test_stdio_mode_does_not_start_watcher_thread(watcher):
    """Constructing a RulesWatcher must NOT spawn the observer thread.
    Only ``.start()`` does. Stdio mode never calls start()."""
    # Newly constructed: no observer running.
    assert watcher._observer is None


def test_compute_content_hash_is_deterministic():
    h1 = compute_content_hash("hello world")
    h2 = compute_content_hash("hello world")
    assert h1 == h2
    assert h1 != compute_content_hash("hello")
