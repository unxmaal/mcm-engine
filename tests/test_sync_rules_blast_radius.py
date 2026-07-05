"""Blast-radius guard for sync_rules (issue #20).

sync_rules must NOT archive a large fraction of the corpus in one sweep — that's
the wrong-context accident that once archived 80 of 80 rules. It shares the
watcher's guard (mcm_engine.destructive.archive_would_storm).
"""
from __future__ import annotations

import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.destructive import archive_would_storm
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tracker import SessionTracker


# --- the shared predicate --------------------------------------------------


def test_storm_needs_both_floor_and_fraction():
    assert archive_would_storm(6, 10) is True          # 6>5 and 0.6>0.5
    assert archive_would_storm(5, 10) is False          # not > floor
    assert archive_would_storm(6, 12) is False          # 0.5 not > 0.5
    assert archive_would_storm(80, 80) is True          # the incident
    assert archive_would_storm(2, 80) is False          # ordinary drift


def test_small_corpora_churn_freely():
    # Below the floor, even a total wipe is allowed (a 3-rule project).
    assert archive_would_storm(3, 3) is False


# --- sync_rules end-to-end -------------------------------------------------


class _FakeMCP:
    def __init__(self):
        self._t = {}

    def tool(self):
        def deco(fn):
            self._t[fn.__name__] = fn
            return fn
        return deco

    def __getitem__(self, n):
        return self._t[n]


@pytest.fixture
def wired(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    db = KnowledgeDB(tmp_path / "rules.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = _FakeMCP()
    register_rules_tools(
        mcp, db, SessionTracker(NudgeConfig(store_reminder_turns=1000,
                                            checkpoint_turns=1000, mandatory_stop_turns=2000)),
        project_name="t", rules_paths=[rules_dir], project_root=tmp_path,
    )
    return {"add_rule": mcp["add_rule"], "sync_rules": mcp["sync_rules"],
            "storage": SqliteStorage(db=db), "rules_dir": rules_dir}


def _active(storage):
    from mcm_engine.backends import EntityType
    return [r for r in storage.iter_entries(EntityType.RULE)
            if not getattr(r, "archived", False)]


def _seed(wired, n):
    for i in range(n):
        wired["add_rule"](title=f"Rule {i}", keywords="k", content=f"body {i}")


def test_sync_refuses_mass_archive_without_force(wired):
    _seed(wired, 8)
    assert len(_active(wired["storage"])) == 8
    # Simulate wrong context: every backing file vanishes.
    for f in wired["rules_dir"].glob("**/*.md"):
        f.unlink()

    out = wired["sync_rules"]()

    assert "REFUSED" in out
    assert "0 orphans archived" in out
    assert len(_active(wired["storage"])) == 8, "guard must archive NOTHING"


def test_sync_force_allows_mass_archive(wired):
    _seed(wired, 8)
    for f in wired["rules_dir"].glob("**/*.md"):
        f.unlink()

    out = wired["sync_rules"](force=True)

    assert "8 orphans archived" in out
    assert len(_active(wired["storage"])) == 0


def test_sync_archives_ordinary_drift_without_force(wired):
    _seed(wired, 8)
    # Delete just one backing file — well under the blast radius.
    victim = sorted(wired["rules_dir"].glob("**/*.md"))[0]
    victim.unlink()

    out = wired["sync_rules"]()

    assert "1 orphans archived" in out
    assert len(_active(wired["storage"])) == 7
