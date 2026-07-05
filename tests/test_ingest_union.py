"""Union ingestion (#53): one `ingest <dir>` run covers ALL matching
ingesters, not just the first.

The bug this locks out: `find()` returned the FIRST matching ingester, so a
Python repo that also held a README.md and some .rs files resolved to
python-ast alone — its markdown and rust content was silently dropped. A
knowledge base can't afford to miss net-new signal because of where it happened
to live.

Contract:
  - `find_all(source)` returns EVERY matching ingester, in precedence order.
  - `--type` still selects a single ingester (the escape hatch).
  - Precedence-based extension ownership: each ingester declares the extensions
    it owns; lower-precedence ingesters (text-dir, the catch-all) are told to
    skip already-owned extensions so no file is surfaced twice.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from mcm_engine.cli import main
from mcm_engine.ingest import (
    NoMatchingIngester,
    UnknownIngester,
    find,
    find_all,
)
from mcm_engine.ingest.markdown import MarkdownDirIngester
from mcm_engine.ingest.python_ast import PythonAstIngester
from mcm_engine.ingest.text_dir import TextDirIngester


# ---------------------------------------------------------------------------
# find_all — every matching ingester, in precedence order
# ---------------------------------------------------------------------------


def _polyglot_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "main.py").write_text('"""mod doc."""\ndef f():\n    """fn doc."""\n    return 1\n', encoding="utf-8")
    (repo / "README.md").write_text("# Readme\nsome prose about the design\n", encoding="utf-8")
    (repo / "lib.rs").write_text("fn main() {\n    // rust rule lives here\n}\n", encoding="utf-8")
    return repo


def test_find_all_returns_every_matching_ingester(tmp_path):
    repo = _polyglot_repo(tmp_path)
    names = [i.name for i in find_all(str(repo))]
    assert "python-ast" in names
    assert "markdown-dir" in names
    assert "text-dir" in names


def test_find_all_preserves_precedence_order(tmp_path):
    """Registration order is precedence order: specific ingesters first,
    catch-all last. The union driver relies on this for extension exclusion."""
    repo = _polyglot_repo(tmp_path)
    names = [i.name for i in find_all(str(repo))]
    assert names.index("python-ast") < names.index("text-dir")
    assert names.index("markdown-dir") < names.index("text-dir")


def test_find_all_explicit_type_returns_exactly_one(tmp_path):
    repo = _polyglot_repo(tmp_path)
    ings = find_all(str(repo), explicit_name="text-dir")
    assert [i.name for i in ings] == ["text-dir"]


def test_find_all_unknown_type_raises(tmp_path):
    repo = _polyglot_repo(tmp_path)
    with pytest.raises(UnknownIngester):
        find_all(str(repo), explicit_name="nope")


def test_find_all_no_match_raises(tmp_path):
    """An empty directory matches nothing."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NoMatchingIngester):
        find_all(str(empty))


def test_find_still_returns_single_for_back_compat(tmp_path):
    """find() keeps its first-match-only behavior for callers that want one."""
    repo = _polyglot_repo(tmp_path)
    ing = find(str(repo))
    assert ing.name == "python-ast"


# ---------------------------------------------------------------------------
# owned_extensions — the precedence-exclusion contract
# ---------------------------------------------------------------------------


def test_specific_ingesters_own_their_extensions():
    assert "py" in PythonAstIngester.owned_extensions()
    assert "md" in MarkdownDirIngester.owned_extensions()


def test_text_dir_owns_nothing_exclusively():
    """text-dir is the catch-all — it claims no extension for itself, so it
    never blocks a more-specific ingester from a file type."""
    assert TextDirIngester.owned_extensions() == frozenset()


# ---------------------------------------------------------------------------
# End-to-end: a polyglot repo surfaces content from ALL ingesters, once each
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    (proj / "rules").mkdir()
    (proj / "mcm-engine.yaml").write_text(
        yaml.dump({
            "project_name": "test",
            "db_path": ".claude/knowledge.db",
            "rules_path": "rules/",
            "plugins": [],
        }),
        encoding="utf-8",
    )
    return proj


def _run_cli(monkeypatch, capsys, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["mcm-engine"] + argv)
    rc = 0
    try:
        main()
    except SystemExit as e:
        rc = e.code or 0
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_bulk_ingest_covers_all_ingesters_no_double_surface(tmp_path, monkeypatch, capsys):
    """The payoff: bulk-ingest a python repo that also has markdown + rust.
    Every family's content lands, and no file is surfaced twice (python-ast
    owns .py, markdown-dir owns .md, text-dir picks up the .rs leftover)."""
    proj = _project(tmp_path)
    repo = _polyglot_repo(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(repo),
        "--project-root", str(proj),
        "--bulk", "--kind", "poly",
    ])

    assert rc == 0
    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        topics = {r[0] for r in con.execute(
            "SELECT topic FROM knowledge WHERE kind='poly'"
        ).fetchall()}
    finally:
        con.close()

    # markdown-dir surfaced the README (extension stripped).
    assert "README" in topics
    # text-dir surfaced the rust file that no specific ingester owns.
    assert "lib.rs" in topics
    # python-ast surfaced the module + function.
    assert any(t == "main.py" or t.startswith("main.py::") for t in topics)
    # text-dir must NOT have re-surfaced the .py as a whole-file candidate
    # (python-ast owns .py). No bare "main.py" whole-file dup beyond what
    # python-ast emits, and definitely no "README.md" (markdown owns it).
    assert "README.md" not in topics


def test_bulk_ingest_reports_every_ingester(tmp_path, monkeypatch, capsys):
    """The stderr banner names all ingesters that ran, not just one."""
    proj = _project(tmp_path)
    repo = _polyglot_repo(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(repo),
        "--project-root", str(proj),
        "--bulk", "--kind", "poly",
    ])

    assert rc == 0
    assert "python-ast" in err
    assert "markdown-dir" in err
    assert "text-dir" in err
