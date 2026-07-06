"""Remote codebase ingestion (issue #72): local span-extraction client +
`sift_candidates` MCP tool.

The split: the client walks + extracts + gates locally (corpus-free), then ships
only rule-like spans to the server, which bands them against the live corpus.
This locks the three pieces — `rulesift.sift_spans` (the corpus-dependent tail),
the `sift_candidates` MCP tool (read-only survivor listing), and the CLI
`ingest --remote` client (spans over MCP, no local DB).
"""
from __future__ import annotations

import json
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
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://kb.local/mcp", {}))
    monkeypatch.setattr(
        cli, "_sift_remote_call",
        lambda url, spans, **kw: captured.update(url=url, spans=spans) or f"SIFTED {len(spans)}")

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
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: (None, {}))

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


# ---------------------------------------------------------------------------
# #74 — auth: endpoint resolution carries headers
# ---------------------------------------------------------------------------


def test_endpoint_reads_headers_from_mcp_json(tmp_path, monkeypatch):
    from mcm_engine.hooks.mcp_enforcement import _mcp_http_endpoint

    monkeypatch.delenv("MCM_MCP_URL", raising=False)
    monkeypatch.delenv("MCM_MCP_TOKEN", raising=False)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"kb": {
        "type": "http", "url": "http://kb/mcp",
        "headers": {"Authorization": "Bearer abc"}}}}), encoding="utf-8")
    url, headers = _mcp_http_endpoint(tmp_path)
    assert url == "http://kb/mcp"
    assert headers["Authorization"] == "Bearer abc"


def test_endpoint_token_env_fallback(tmp_path, monkeypatch):
    from mcm_engine.hooks.mcp_enforcement import _mcp_http_endpoint

    monkeypatch.setenv("MCM_MCP_URL", "http://kb/mcp")
    monkeypatch.setenv("MCM_MCP_TOKEN", "tok123")
    url, headers = _mcp_http_endpoint(tmp_path)
    assert url == "http://kb/mcp"
    assert headers["Authorization"] == "Bearer tok123"


def test_endpoint_none_without_url(tmp_path, monkeypatch):
    from mcm_engine.hooks.mcp_enforcement import _mcp_http_endpoint

    monkeypatch.delenv("MCM_MCP_URL", raising=False)
    url, headers = _mcp_http_endpoint(tmp_path)
    assert url is None


# ---------------------------------------------------------------------------
# #76 — server-side per-call span cap
# ---------------------------------------------------------------------------


def test_sift_candidates_refuses_over_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("MCM_SIFT_MAX_SPANS", raising=False)
    mcp, _ = _wire(tmp_path)
    over = [{"text": f"always rule {i}", "source_topic": f"f{i}"} for i in range(26)]
    out = mcp["sift_candidates"](over)
    assert "refused" in out and "25" in out


def test_sift_candidates_cap_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_SIFT_MAX_SPANS", "2")
    mcp, _ = _wire(tmp_path)
    out = mcp["sift_candidates"]([{"text": "always a", "source_topic": "x"}] * 3)
    assert "refused" in out and "cap is 2" in out


# ---------------------------------------------------------------------------
# #75 — client batching / retry / resume / fail-open
# ---------------------------------------------------------------------------


def _spans(n):
    return [{"text": f"always rule number {i}", "source_topic": f"f{i}.rs"} for i in range(n)]


def test_classify_remote_error():
    import asyncio

    import httpx
    assert cli._classify_remote_error(asyncio.TimeoutError()) == "timeout"
    assert cli._classify_remote_error(httpx.ReadError("x")) == "transient"
    assert cli._classify_remote_error(httpx.ConnectError("x")) == "transient"

    class Grp(Exception):
        def __init__(self, excs):
            self.exceptions = excs
    assert cli._classify_remote_error(Grp([httpx.ReadError("x")])) == "transient"

    req = httpx.Request("POST", "http://x")
    auth = httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    assert cli._classify_remote_error(auth) == "auth"
    five = httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
    assert cli._classify_remote_error(five) == "transient"


def test_remote_batches_spans(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: _spans(5))
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    calls = []
    monkeypatch.setattr(cli, "_sift_remote_call",
                        lambda url, spans, **kw: calls.append(len(spans)) or "ok")
    cli._ingest_remote([], tmp_path, 5, source_key="s", batch_size=2, batch_timeout=5)
    assert calls == [2, 2, 1]


def test_remote_retries_transient_then_succeeds(tmp_path, monkeypatch, capsys):
    import httpx
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: _spans(1))
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    seq = [httpx.ReadError("boom"), "OK-RESULT"]

    def stub(url, spans, **kw):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(cli, "_sift_remote_call", stub)
    cli._ingest_remote([], tmp_path, 1, source_key="s", batch_size=5, batch_timeout=5)
    assert "OK-RESULT" in capsys.readouterr().out
    assert not seq


def test_remote_permanent_failure_is_fail_open_exit1(tmp_path, monkeypatch, capsys):
    import httpx
    two = [{"text": "always a", "source_topic": "a"}, {"text": "always b", "source_topic": "b"}]
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: two)
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    monkeypatch.setattr(cli, "_sleep", lambda s: None)

    def stub(url, spans, **kw):
        if any(s["source_topic"] == "a" for s in spans):
            raise httpx.ReadError("boom")
        return "OK-B"
    monkeypatch.setattr(cli, "_sift_remote_call", stub)
    with pytest.raises(SystemExit) as ei:
        cli._ingest_remote([], tmp_path, 2, source_key="s", batch_size=1, batch_timeout=5)
    assert ei.value.code == 1
    cap = capsys.readouterr()
    assert "OK-B" in cap.out       # fail-open: the healthy batch still ran
    assert "a" in cap.err          # failed topic surfaced


def test_remote_auth_error_exits_1(tmp_path, monkeypatch, capsys):
    import httpx
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: _spans(1))
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    req = httpx.Request("POST", "http://x")

    def stub(url, spans, **kw):
        raise httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    monkeypatch.setattr(cli, "_sift_remote_call", stub)
    with pytest.raises(SystemExit) as ei:
        cli._ingest_remote([], tmp_path, 1, source_key="s", batch_size=5, batch_timeout=5)
    assert ei.value.code == 1
    assert "auth" in capsys.readouterr().err.lower()


def test_remote_timeout_splits_batch(tmp_path, monkeypatch, capsys):
    import asyncio
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: _spans(2))
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    state = {"first": True}

    def stub(url, spans, **kw):
        if len(spans) == 2 and state["first"]:
            state["first"] = False
            raise asyncio.TimeoutError()
        return f"ok-{len(spans)}"
    monkeypatch.setattr(cli, "_sift_remote_call", stub)
    cli._ingest_remote([], tmp_path, 2, source_key="s", batch_size=2, batch_timeout=5)
    err = capsys.readouterr().err
    assert "split" in err


def test_remote_resume_skips_done(tmp_path, monkeypatch, capsys):
    import httpx
    two = [{"text": "always a", "source_topic": "a"}, {"text": "always b", "source_topic": "b"}]
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g: two)
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: ("http://x", {}))
    monkeypatch.setattr(cli, "_sleep", lambda s: None)

    # run 1: span 'b' fails permanently, 'a' succeeds
    def stub1(url, spans, **kw):
        if any(s["source_topic"] == "b" for s in spans):
            raise httpx.ReadError("boom")
        return "A-OK"
    monkeypatch.setattr(cli, "_sift_remote_call", stub1)
    with pytest.raises(SystemExit):
        cli._ingest_remote([], tmp_path, 2, source_key="src", batch_size=1, batch_timeout=5)
    state_file = tmp_path / ".mcm-engine" / "ingest-state.json"
    assert state_file.exists()

    # run 2: 'b' now succeeds; 'a' must be skipped (resumed), state cleared
    seen = []

    def stub2(url, spans, **kw):
        seen.extend(s["source_topic"] for s in spans)
        return "OK2"
    monkeypatch.setattr(cli, "_sift_remote_call", stub2)
    cli._ingest_remote([], tmp_path, 2, source_key="src", batch_size=1, batch_timeout=5)
    assert "a" not in seen and "b" in seen
    assert not state_file.exists()
