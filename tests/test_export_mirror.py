"""Piece C: one-way DB -> git review mirror (issue #22).

The mirror renders ACTIVE rules to a git repo of markdown for review. It is
read-only w.r.t. the store and structurally one-way (writes only into the
external git dir), and it excludes superseded/archived rules so it never
presents dead rules as authoritative.
"""
from __future__ import annotations

import subprocess

from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.mirror import export_mirror
from mcm_engine.schema import migrate_core
from mcm_engine.adapters.sqlite.storage import SqliteStorage


def _storage(tmp_path):
    db = KnowledgeDB(str(tmp_path / "mirror.db"))
    migrate_core(db)
    return SqliteStorage(db=db)


def _seed(storage):
    """Two widgets (one soon superseded) + one unrelated active rule."""
    old = storage.insert_rule(RuleRow(id=0, title="Old widget frobnication",
                                      keywords="widget", category="widgets",
                                      content="old body"))
    new = storage.insert_rule(RuleRow(id=0, title="New widget frobnication",
                                      keywords="widget", category="widgets",
                                      content="new body"))
    storage.insert_rule(RuleRow(id=0, title="Unrelated rule", keywords="misc",
                                category="misc", content="c body"))
    storage.supersede_rule(old, new, "tester")
    return old, new


def test_mirror_writes_active_excludes_superseded_and_commits(tmp_path):
    storage = _storage(tmp_path)
    _seed(storage)
    out = tmp_path / "mirror-repo"

    result = export_mirror(storage, out)

    assert result["committed"] is True
    assert result["written"] == 2  # new + unrelated, NOT the superseded old one
    assert (out / "rules" / "widgets" / "new-widget-frobnication.md").exists()
    assert (out / "rules" / "misc" / "unrelated-rule.md").exists()
    assert not (out / "rules" / "widgets" / "old-widget-frobnication.md").exists()
    # the rendered file carries the authoritative markdown shape
    body = (out / "rules" / "misc" / "unrelated-rule.md").read_text()
    assert body.startswith("# Unrelated rule")
    assert "**Keywords:** misc" in body

    # a real git commit exists
    assert (out / ".git").exists()
    log = subprocess.run(["git", "-C", str(out), "log", "--oneline"],
                         capture_output=True, text=True)
    assert log.stdout.strip()


def test_mirror_uses_explicit_git_identity(tmp_path):
    """Commits with -c user.email/name so it works on a fresh box with no git
    identity configured (containers, CI)."""
    storage = _storage(tmp_path)
    _seed(storage)
    out = tmp_path / "mirror-repo"
    export_mirror(storage, out)
    author = subprocess.run(["git", "-C", str(out), "log", "-1", "--format=%ae"],
                            capture_output=True, text=True).stdout.strip()
    assert author == "mcm-engine-mirror@localhost"


def test_mirror_is_read_only_on_source(tmp_path):
    """Enumerate-only: the mirror must not mutate the store (never trips the
    sync_rules orphan-archive path)."""
    storage = _storage(tmp_path)
    _seed(storage)
    before = list(storage.iter_entries(EntityType.RULE))
    export_mirror(storage, tmp_path / "mirror-repo")
    after = list(storage.iter_entries(EntityType.RULE))
    # all three rows survive unchanged (incl. the superseded one, still in DB)
    assert len(after) == 3 == len(before)
    assert {r.title for r in after} == {r.title for r in before}


def test_mirror_second_run_reports_no_changes(tmp_path):
    storage = _storage(tmp_path)
    _seed(storage)
    out = tmp_path / "mirror-repo"
    export_mirror(storage, out)
    second = export_mirror(storage, out)
    assert second["committed"] is False
    assert second["written"] == 2


def test_mirror_reflects_a_later_supersession_as_a_removal(tmp_path):
    storage = _storage(tmp_path)
    old, new = _seed(storage)
    out = tmp_path / "mirror-repo"
    export_mirror(storage, out)
    assert (out / "rules" / "misc" / "unrelated-rule.md").exists()

    # supersede the previously-active "unrelated" rule; next mirror drops its file
    extra = storage.insert_rule(RuleRow(id=0, title="Unrelated v2", keywords="misc",
                                        category="misc", content="v2"))
    # find the unrelated rule id
    unrelated = next(r for r in storage.iter_entries(EntityType.RULE)
                     if r.title == "Unrelated rule")
    storage.supersede_rule(unrelated.id, extra, "tester")

    result = export_mirror(storage, out)
    assert result["committed"] is True
    assert not (out / "rules" / "misc" / "unrelated-rule.md").exists()
    assert (out / "rules" / "misc" / "unrelated-v2.md").exists()


def test_cli_export_mirror_dispatch(tmp_path, monkeypatch):
    """`mcm-engine export-mirror --from <dsn> --out <dir>` dispatches and runs."""
    import sys

    from mcm_engine.cli import main as cli_main

    dbp = tmp_path / "cli.db"
    db = KnowledgeDB(str(dbp))
    migrate_core(db)
    storage = SqliteStorage(db=db)
    storage.insert_rule(RuleRow(id=0, title="CLI rule", keywords="k",
                                category="c", content="b"))

    out = tmp_path / "cli-mirror"
    monkeypatch.setattr(sys, "argv", [
        "mcm-engine", "export-mirror", "--from", f"sqlite:///{dbp}", "--out", str(out),
    ])
    cli_main()
    assert (out / "rules" / "c" / "cli-rule.md").exists()
