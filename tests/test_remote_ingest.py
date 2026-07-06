"""Remote codebase ingestion (issue #72): local span-extraction client +
`sift_candidates` MCP tool.

The split: the client walks + extracts + gates locally (corpus-free), then ships
only rule-like spans to the server, which bands them against the live corpus.
This locks the three pieces — `rulesift.sift_spans` (the corpus-dependent tail),
the `sift_candidates` MCP tool (read-only survivor listing), and the CLI
`ingest --remote` client (spans over MCP, no local DB).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from mcm_engine import cli
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.ingest import find_all, rulesift
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tracker import SessionTracker


# ---------------------------------------------------------------------------
# rulesift.sift_spans — the corpus-dependent tail of the funnel
# ---------------------------------------------------------------------------


def test_sift_spans_keeps_rulelike_novel():
    out = rulesift.sift_spans(
        [("Always use uv for all Python; never call pip directly.", "a.py")],
        existing_rules=[])
    assert len(out) == 1
    assert out[0].band is rulesift.Band.NOVEL
    assert out[0].source_topic == "a.py"


def test_sift_spans_drops_non_rulelike():
    out = rulesift.sift_spans([("x = compute(y) + 1", "a.py")], existing_rules=[])
    assert out == []


def test_sift_spans_drops_known():
    body = "Always use uv for all Python; never call pip or poetry directly here."
    out = rulesift.sift_spans([(body, "a.py")], existing_rules=[(1, body)])
    assert out == []  # near-identical to the corpus -> KNOWN -> dropped


def test_sift_spans_collapses_intra_run_dupes():
    s = "Never commit secrets; always load tokens from environment variables."
    out = rulesift.sift_spans([(s, "a.py"), (s, "b.py")], existing_rules=[])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# sift_candidates MCP tool
# ---------------------------------------------------------------------------


class FakeMCP:
    def __init__(self):
        self._t = {}

    def tool(self):
        def d(fn):
            self._t[fn.__name__] = fn
            return fn
        return d

    def __getitem__(self, n):
        return self._t[n]

    def __contains__(self, n):
        return n in self._t


def _wire(tmp_path):
    db = KnowledgeDB(tmp_path / "r.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000, mandatory_stop_turns=100000))
    register_rules_tools(mcp, db, tracker, "t", [rules_dir], tmp_path,
                         files_authoritative=False)
    return mcp, SqliteStorage(db=db)


def test_sift_candidates_tool_is_registered(tmp_path):
    mcp, _ = _wire(tmp_path)
    assert "sift_candidates" in mcp


def test_sift_candidates_returns_novel_survivor(tmp_path):
    mcp, _ = _wire(tmp_path)
    out = mcp["sift_candidates"]([
        {"text": "Never log secrets to stdout in request handlers.", "source_topic": "log.py"},
    ])
    assert "Never log secrets" in out
    assert "net-new candidate" in out
    assert "from log.py" in out


def test_sift_candidates_is_read_only(tmp_path):
    mcp, storage = _wire(tmp_path)
    before = len(list(storage.iter_entries(EntityType.RULE)))
    mcp["sift_candidates"]([{"text": "Always use uv; never call pip.", "source_topic": "a.py"}])
    assert len(list(storage.iter_entries(EntityType.RULE))) == before


def test_sift_candidates_zero_when_nothing_rulelike(tmp_path):
    mcp, _ = _wire(tmp_path)
    out = mcp["sift_candidates"]([{"text": "config = load(x)", "source_topic": "a.py"}])
    assert "0 net-new" in out


# ---------------------------------------------------------------------------
# client: _collect_remote_spans
# ---------------------------------------------------------------------------


def test_collect_remote_spans_extracts_rulelike(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text(
        "// Always use uv for python; never call pip directly.\nfn main() {}\n",
        encoding="utf-8")
    groups = [(ing, list(ing.stream(str(repo), {}))) for ing in find_all(str(repo))]
    spans = cli._collect_remote_spans(groups)
    assert any("Always use uv" in s["text"] for s in spans)
    assert all(set(s) == {"text", "source_topic"} for s in spans)


# ---------------------------------------------------------------------------
# CLI: ingest --remote
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    (proj / "rules").mkdir()
    (proj / "mcm-engine.yaml").write_text(
        yaml.dump({"project_name": "test", "db_path": ".claude/knowledge.db",
                   "rules_path": "rules/", "plugins": []}),
        encoding="utf-8")
    return proj


def _run_cli(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["mcm-engine"] + argv)
    rc = 0
    try:
        cli.main()
    except SystemExit as e:
        rc = e.code or 0
    out = capsys.readouterr()
    return rc, out.out, out.err


def _rs_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text(
        "// Always use uv for python; never call pip directly.\nfn main() {}\n",
        encoding="utf-8")
    return repo


def test_ingest_remote_ships_spans_over_mcp(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    repo = _rs_repo(tmp_path)
    captured = {}
    monkeypatch.setattr(cli, "_remote_mcp_url", lambda _pr: "http://kb.local/mcp")
    monkeypatch.setattr(
        cli, "_sift_remote_call",
        lambda url, spans: captured.update(url=url, spans=spans) or f"SIFTED {len(spans)}")

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(repo), "--remote", "--project-root", str(proj)])

    assert rc == 0
    assert "SIFTED" in out
    assert captured["url"] == "http://kb.local/mcp"
    assert any("Always use uv" in s["text"] for s in captured["spans"])
    # the remote path must not create/populate a local DB
    db = proj / ".claude" / "knowledge.db"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(str(db))
        try:
            try:
                n = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            assert n == 0
        finally:
            con.close()


def test_ingest_remote_errors_without_endpoint(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    repo = _rs_repo(tmp_path)
    monkeypatch.setattr(cli, "_remote_mcp_url", lambda _pr: None)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(repo), "--remote", "--project-root", str(proj)])

    assert rc == 2
    assert "MCP HTTP endpoint" in err


def test_ingest_remote_incompatible_with_bulk(tmp_path, monkeypatch, capsys):
    proj = _project(tmp_path)
    repo = _rs_repo(tmp_path)

    rc, out, err = _run_cli(monkeypatch, capsys, [
        "ingest", str(repo), "--remote", "--bulk", "--project-root", str(proj)])

    assert rc == 2
    assert "incompatible" in err
