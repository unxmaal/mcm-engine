"""CLI-level: store identity is surfaced, and a pinned authoritative_store
fails closed on mismatch (stray-db branch)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from mcm_engine.cli import main


def _project(tmp_path: Path, authoritative_store: str = "") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    (proj / "rules").mkdir()
    data = {"project_name": "test", "db_path": ".claude/knowledge.db",
            "rules_path": "rules/", "plugins": []}
    if authoritative_store:
        data["authoritative_store"] = authoritative_store
    (proj / "mcm-engine.yaml").write_text(yaml.dump(data), encoding="utf-8")
    return proj


def _codedir(tmp_path: Path) -> Path:
    d = tmp_path / "code"
    d.mkdir()
    (d / "a.go").write_text("// you must set the flag or it fails\nfunc a() {}\n", encoding="utf-8")
    return d


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["mcm-engine"] + argv)
    rc = 0
    try:
        main()
    except SystemExit as e:
        rc = e.code or 0
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_ingest_surfaces_the_store_it_uses(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    code = _codedir(tmp_path)
    rc, out, err = _run(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--type", "text-dir", "--project-root", str(proj),
    ])
    assert rc == 0
    # The resolved store is announced, and it's the project's db.
    expected_db = (proj / ".claude" / "knowledge.db").resolve()
    assert f"# store:    sqlite:{expected_db}" in err


def test_pinned_authoritative_store_mismatch_fails_closed(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path, authoritative_store="sqlite:/definitely/not/this/one.db")
    code = _codedir(tmp_path)
    rc, out, err = _run(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--type", "text-dir", "--project-root", str(proj),
    ])
    assert rc == 2
    assert "/definitely/not/this/one.db" in err  # names the pin
    assert "knowledge.db" in err                 # names the actual store


def test_pinned_authoritative_store_match_proceeds(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    correct = f"sqlite:{(proj / '.claude' / 'knowledge.db').resolve()}"
    # rewrite config with the correct pin
    _project  # noqa
    data = yaml.safe_load((proj / "mcm-engine.yaml").read_text())
    data["authoritative_store"] = correct
    (proj / "mcm-engine.yaml").write_text(yaml.dump(data), encoding="utf-8")
    code = _codedir(tmp_path)
    rc, out, err = _run(monkeypatch, capsys, [
        "ingest", str(code), "--rules", "--type", "text-dir", "--project-root", str(proj),
    ])
    assert rc == 0
    assert correct in err
