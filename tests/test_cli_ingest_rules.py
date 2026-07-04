"""CLI tests for `mcm-engine ingest --rules` (Slice 1, fix_ingestion).

`--rules` is the curated mode with the mechanical rule-sift funnel wired in:
instead of emitting one candidate per file (the whole codebase into the
agent's context), it emits ONLY net-new, rule-shaped spans, each tagged with
its novelty band. Still curated — no DB writes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml

from mcm_engine.cli import main


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


def _codedir(tmp_path: Path, files: dict[str, str]) -> Path:
    """A non-python, non-markdown code tree so ingest resolves to text-dir
    (the raw-code path the funnel's comment extraction targets)."""
    d = tmp_path / "code"
    d.mkdir()
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


def _run_cli(monkeypatch, capsys, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["mcm-engine"] + argv)
    rc = 0
    try:
        main()
    except SystemExit as e:
        rc = e.code or 0
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_rules_mode_emits_only_rule_shaped_candidates(tmp_path, monkeypatch, capsys):
    code = _codedir(tmp_path, {
        "boot.go": (
            "package main\n"
            "// WARNING: you must hold both buttons for 3s to power on the TC001\n"
            "func boot() {}\n"
        ),
        "math.go": (
            "package main\n"
            "// increment the counter\n"
            "func add(a, b int) int { return a + b }\n"
        ),
    })
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--project-root", str(proj),
    ])

    assert rc == 0
    # The rule-shaped span survives, tagged novel (empty rule corpus).
    assert "rule-candidate 1/1" in out
    assert "band: novel" in out
    assert "hold both buttons for 3s" in out
    # The trivial comment and the code body do NOT surface.
    assert "increment the counter" not in out
    assert "return a + b" not in out


def test_rules_mode_is_curated_no_writes(tmp_path, monkeypatch, capsys):
    code = _codedir(tmp_path, {
        "boot.go": "// never call free() twice on the same handle\nfunc x() {}\n",
    })
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--project-root", str(proj),
    ])

    assert rc == 0
    assert "# mode:     rules" in err
    db = proj / ".claude" / "knowledge.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        try:
            try:
                count = con.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            except sqlite3.OperationalError:
                count = 0
            assert count == 0, "rules mode is curated; it must not write rows"
        finally:
            con.close()


def test_rules_mode_reports_funnel_narrowing(tmp_path, monkeypatch, capsys):
    """The meta banner should show how much the funnel narrowed: N raw files
    down to M rule candidates. Silent truncation would hide the drop."""
    code = _codedir(tmp_path, {
        "a.go": "// you must set upload_speed to 460800 or the flash fails\nfunc a() {}\n",
        "b.go": "func b() { return }\n",
        "c.go": "func c() { return }\n",
    })
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--project-root", str(proj),
    ])

    assert rc == 0
    assert "candidates" in err  # raw file count reported
    assert "rule-candidate 1/1" in out


def test_rules_and_bulk_are_incompatible(tmp_path, monkeypatch, capsys):
    code = _codedir(tmp_path, {"a.go": "// must do the thing\nfunc a() {}\n"})
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--bulk", "--project-root", str(proj),
    ])

    assert rc == 2
    assert "rules" in err.lower() and "bulk" in err.lower()
