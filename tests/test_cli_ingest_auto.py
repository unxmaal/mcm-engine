"""CLI tests for `ingest --auto` (Slice 3, fix_ingestion).

The fully-automatic path: sift -> model adjudication -> confidence-routed commit
(auto-commit above the bar, review-queue below). The model is stubbed via
build_adjudicator so no network is touched.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml

import mcm_engine.ingest.adjudicate as adj_mod
from mcm_engine.cli import main
from mcm_engine.ingest.adjudicate import Action, Verdict


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
        (d / rel).write_text(content, encoding="utf-8")
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


class _StubAdjudicator:
    """Returns one high-confidence add and one low-confidence add, ignoring
    input — deterministic routing regardless of what was sifted."""

    def adjudicate(self, candidates, existing):
        return [
            Verdict(Action.ADD, title="Auto high", keywords="k",
                    content="A confidently-good rule.", confidence=0.95),
            Verdict(Action.ADD, title="Auto low", keywords="k",
                    content="A shaky rule.", confidence=0.2),
        ]


def test_auto_commits_high_confidence_and_queues_low(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(adj_mod, "build_adjudicator", lambda config: _StubAdjudicator())
    code = _codedir(tmp_path, {
        "a.go": "// you must set upload_speed to 460800 or the flash fails\nfunc a() {}\n",
    })
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--auto", "--project-root", str(proj),
    ])

    assert rc == 0
    assert "committed created=1" in out
    assert "queued for review=1" in out

    db = proj / ".claude" / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        titles = [r[0] for r in con.execute("SELECT title FROM rules").fetchall()]
    finally:
        con.close()
    assert "Auto high" in titles          # high confidence auto-committed
    assert "Auto low" not in titles       # low confidence did NOT commit

    queue = proj / ".claude" / "rule-review-queue.jsonl"
    assert queue.exists() and "Auto low" in queue.read_text(encoding="utf-8")


def test_auto_errors_without_configured_adjudicator(tmp_path, monkeypatch, capsys):
    # No monkeypatch: default config has no adjudicator provider.
    code = _codedir(tmp_path, {"a.go": "// must do the thing carefully\nfunc a() {}\n"})
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--auto", "--project-root", str(proj),
    ])

    assert rc == 2
    assert "adjudicator" in err.lower()


def test_auto_incompatible_with_bulk(tmp_path, monkeypatch, capsys):
    code = _codedir(tmp_path, {"a.go": "// must do the thing\nfunc a() {}\n"})
    proj = _project(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(code), "--auto", "--bulk", "--project-root", str(proj),
    ])

    assert rc == 2
    assert "auto" in err.lower() and "bulk" in err.lower()
