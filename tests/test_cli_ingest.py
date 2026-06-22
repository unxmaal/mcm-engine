"""CLI-level tests for `mcm-engine ingest`.

The unit tests in test_ingest_markdown.py cover the framework + ingester
internals. This file locks down the CLI surface: default = curated emit
(no writes), `--bulk` = auto-insert, pagination via --batch/--offset.

The behavioral split between curated and bulk is the *whole point* of
the refactor — if these tests regress, the engine silently goes back to
"every ingest dumps to the KB," which is exactly what we set out to
prevent.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

from mcm_engine.cli import main


def _vault(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a fake markdown vault under tmp_path/vault and return its path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for rel, content in files.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return vault


def _project(tmp_path: Path) -> Path:
    """Build a minimal mcm-engine project rooted at tmp_path/proj."""
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
    """Invoke mcm_engine.cli.main with argv; capture (exit, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["mcm-engine"] + argv)
    rc = 0
    try:
        main()
    except SystemExit as e:
        rc = e.code or 0
    out = capsys.readouterr()
    return rc, out.out, out.err


# ---------------------------------------------------------------------------
# Default mode = curated, no writes
# ---------------------------------------------------------------------------


def test_default_mode_emits_candidates_but_does_not_write(tmp_path, monkeypatch, capsys):
    """The whole point of the refactor: default ingest must NOT touch the DB."""
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {
        "a.md": "# A\nbody a\n",
        "Friends/heather.md": "# Heather\nfamily\n",
    })

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
    ])

    assert rc == 0
    # Candidates landed on stdout, delimited.
    assert "=== candidate 1/" in out
    assert "topic: a" in out
    assert "topic: Friends/heather" in out
    # The DB was not created / written to.
    db = proj / ".claude" / "knowledge.db"
    if db.exists():
        # If the file got touched (e.g. schema init), at least confirm no
        # knowledge rows. We do this by importing the storage and checking.
        import sqlite3
        con = sqlite3.connect(str(db))
        try:
            try:
                count = con.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            except sqlite3.OperationalError:
                count = 0  # no schema yet — even better, nothing was written
            assert count == 0, "default ingest mode wrote rows; it must not"
        finally:
            con.close()


def test_default_mode_emits_meta_to_stderr_not_stdout(tmp_path, monkeypatch, capsys):
    """Banner lines belong on stderr so stdout is clean candidate payload —
    pipeable to `tee`/`less` without garbage at the top."""
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {"a.md": "body\n"})

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
    ])

    assert rc == 0
    # Meta on stderr.
    assert "# ingester:" in err
    assert "# mode:     curated" in err
    # Stdout = candidate blocks ONLY (no '# ingester' contamination).
    assert "# ingester:" not in out


def test_default_mode_respects_batch_size(tmp_path, monkeypatch, capsys):
    """--batch limits how many candidates are emitted per invocation."""
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {
        f"note{i:02d}.md": f"body {i}\n" for i in range(10)
    })

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--batch", "3",
    ])

    assert rc == 0
    # Exactly 3 candidate blocks emitted, even though 10 are present.
    assert out.count("=== candidate ") == 3
    # The meta block tells the user how to keep going.
    assert "--offset 3" in err


def test_default_mode_offset_picks_up_where_previous_batch_left_off(
    tmp_path, monkeypatch, capsys,
):
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {
        f"note{i:02d}.md": f"body {i}\n" for i in range(10)
    })

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--batch", "3",
        "--offset", "3",
    ])

    assert rc == 0
    assert out.count("=== candidate ") == 3
    # We're past the first 3 — confirm the indices reflect that.
    assert "=== candidate 4/" in out
    assert "=== candidate 6/" in out


def test_default_mode_last_batch_drops_next_hint(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {
        f"note{i:02d}.md": f"body {i}\n" for i in range(5)
    })

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--batch", "10",   # bigger than corpus
    ])

    assert rc == 0
    # No more pages, so no "next:" hint.
    assert "next:" not in err


# ---------------------------------------------------------------------------
# --bulk mode — the opt-in escape hatch
# ---------------------------------------------------------------------------


def test_bulk_mode_writes_every_candidate(tmp_path, monkeypatch, capsys):
    """--bulk says 'I know this is my authoritative corpus' — every
    candidate goes in. This is the path the Obsidian dump used."""
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {
        "a.md": "# A\nbody a\n",
        "b.md": "# B\nbody b\n",
    })

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--bulk",
        "--kind", "test-bulk",
    ])

    assert rc == 0
    assert "bulk done" in out
    # Rows landed.
    import sqlite3
    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        topics = {row[0] for row in con.execute(
            "SELECT topic FROM knowledge WHERE kind='test-bulk'"
        ).fetchall()}
    finally:
        con.close()
    assert topics == {"a", "b"}


def test_bulk_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {"a.md": "body\n"})

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--bulk", "--dry-run",
        "--kind", "test-bulk-dry",
    ])

    assert rc == 0
    assert "[DRY RUN]" in out
    assert "would insert: a" in out
    # No rows landed.
    import sqlite3
    db = proj / ".claude" / "knowledge.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        try:
            try:
                count = con.execute(
                    "SELECT COUNT(*) FROM knowledge WHERE kind='test-bulk-dry'"
                ).fetchone()[0]
            except Exception:
                count = 0
        finally:
            con.close()
        assert count == 0


def test_bulk_is_idempotent_on_topic_plus_kind(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    vault = _vault(tmp_path, {"a.md": "v1\n"})

    # First run.
    _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--bulk", "--kind", "test-idem",
    ])
    # Update file content.
    (vault / "a.md").write_text("v2\n", encoding="utf-8")
    # Second run.
    rc, out, _ = _run_cli(monkeypatch, capsys, [
        "ingest", str(vault),
        "--project-root", str(proj),
        "--bulk", "--kind", "test-idem",
    ])

    assert rc == 0
    # Second run updates, doesn't double-insert.
    import sqlite3
    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT topic, detail FROM knowledge WHERE kind='test-idem'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0][0] == "a"
    assert "v2" in rows[0][1]


# ---------------------------------------------------------------------------
# Listing types
# ---------------------------------------------------------------------------


def test_list_types_includes_markdown_dir(monkeypatch, capsys):
    rc, out, _ = _run_cli(monkeypatch, capsys, ["ingest", "--list-types"])
    assert rc == 0
    assert "markdown-dir" in out


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_source_path_errors_helpfully(monkeypatch, capsys):
    """Calling `mcm-engine ingest` with neither a source nor --list-types
    should fail with a useful message, not crash."""
    rc, _, err = _run_cli(monkeypatch, capsys, ["ingest"])
    assert rc == 2
    assert "source" in err.lower()


def test_unknown_type_lists_alternatives(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    rc, _, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(tmp_path),
        "--project-root", str(proj),
        "--type", "does-not-exist",
    ])
    assert rc == 2
    assert "does-not-exist" in err
    assert "markdown-dir" in err  # helpfully suggests what IS available
