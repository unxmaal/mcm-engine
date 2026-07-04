"""CLI tests for the Slice 2 harness-delegation loop (fix_ingestion).

Two-phase, provider-agnostic:
  1. `ingest <dir> --rules --adjudicate` emits an adjudication REQUEST (the
     calling harness's model decides — no model in the engine).
  2. `apply-rules <verdicts.json>` commits the returned verdicts to the KB via
     commit_verdicts (backend-agnostic).
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


def test_adjudicate_flag_emits_a_decision_request(tmp_path, monkeypatch, capsys):
    code = _codedir(tmp_path, {
        "boot.go": "// WARNING: you must hold both buttons for 3s to power on\nfunc x() {}\n",
    })
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--adjudicate", "--project-root", str(proj),
    ])

    assert rc == 0
    assert "adjudication request" in out.lower()
    assert "hold both buttons for 3s" in out
    # The return schema + action vocabulary must be present for the agent.
    for tok in ("action", "add", "refine", "reinforce", "reject"):
        assert tok in out
    # Still curated — no writes.
    db = proj / ".claude" / "knowledge.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        try:
            try:
                n = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            assert n == 0
        finally:
            con.close()


def test_apply_rules_commits_verdicts(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(
        '[{"action":"add","title":"No secrets in logs",'
        '"keywords":"secrets,logging","content":"Never log secrets to stdout."}]',
        encoding="utf-8",
    )

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "apply-rules", str(verdicts), "--project-root", str(proj),
    ])

    assert rc == 0
    assert "created" in out.lower() and "1" in out
    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        titles = [r[0] for r in con.execute("SELECT title FROM rules").fetchall()]
        assert "No secrets in logs" in titles
    finally:
        con.close()


def test_apply_rules_reports_errors_without_aborting(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(
        '[{"action":"add","title":"","keywords":"k","content":"no title"},'
        '{"action":"add","title":"Good","keywords":"k","content":"a body"}]',
        encoding="utf-8",
    )

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "apply-rules", str(verdicts), "--project-root", str(proj),
    ])

    assert rc == 0
    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        titles = [r[0] for r in con.execute("SELECT title FROM rules").fetchall()]
        assert titles == ["Good"]  # the good one landed, the bad one did not
    finally:
        con.close()
