"""Characterization replay harness (issue #182).

The harness itself is verified: a checked-in mini transcript replays against
a seeded store with a pinned all-agree result, and a store whose unified side
was tampered with is reported as disagreeing (so a 1.0 can only come from
real agreement).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mcm_engine.backends import ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from mcm_engine.cli import main as cli_main
from mcm_engine.replay_queries import (
    extract_queries,
    format_report,
    jaccard,
    open_context,
    prefix_agreement,
    replay,
)

FIXTURE = Path(__file__).parent / "fixtures" / "replay_mini.jsonl"


def _seed(storage):
    storage.insert_knowledge(KnowledgeRow(id=0, topic="mahjong timer", summary="turn clock",
                                          detail="body", tags="mj"))
    storage.insert_knowledge(KnowledgeRow(id=0, topic="mahjong scoring", summary="old way"))
    storage.insert_negative(NegativeRow(id=0, category="measurement", what_failed="counted sets",
                                        why_failed="zebra", correct_approach="rank"))
    storage.insert_error(ErrorRow(id=0, pattern="database is locked", context="wal",
                                  root_cause="giraffe contention", fix="retry"))
    r = storage.insert_rule(RuleRow(id=0, title="Nudge deadlock fix", keywords="nudge",
                                    content="# body", description="d", file_path="rules/x.md"))
    r2 = storage.insert_rule(RuleRow(id=0, title="Archived nudge", keywords="nudge", content="b"))
    storage.soft_delete_rule(r2)
    return r


def test_extract_queries_keeps_only_replayable_tool_uses():
    qs = extract_queries([FIXTURE])
    assert [q.tool for q in qs] == ["search", "search", "report_error", "get_entry",
                                    "get_entry", "read_rule", "search"]
    assert qs[1].args == {"query": "nudge", "scope": "rules", "detail": "full",
                          "include_archived": True}
    assert all(q.source == "replay_mini.jsonl" for q in qs)
    assert extract_queries(["/nonexistent/transcript.jsonl"]) == []


def test_metrics():
    assert jaccard([], []) == 1.0
    assert jaccard([["RULE", 1]], [["RULE", 1], ["RULE", 2]]) == 0.5
    assert prefix_agreement([["A", 1], ["A", 2]], [["A", 1], ["A", 3]]) == 0.5
    assert prefix_agreement([], [["A", 1]]) == 0.0


def test_seeded_store_agrees_completely(tmp_path):
    ctx = open_context(str(tmp_path / "k.db"))
    _seed(ctx.storage)
    report = replay(ctx, extract_queries([FIXTURE]), store="k.db")
    assert report.counts == {"search": 3, "report_error": 1, "get_entry": 2, "read_rule": 1}
    for tool in ("search", "get_entry", "read_rule"):
        assert report.agreement[tool]["agree"] == 1.0, tool
    assert report.agreement["search"]["exact_order"] == 1.0
    assert report.agreement["search"]["text_identical"] == 1.0
    # report_error is gated on an absolute raw FTS score; on a six-row store the
    # two indexes sit on different scales, so the harness must report the flip
    # rather than hide it, and show the paths agree with the gate off.
    re = report.agreement["report_error"]
    assert re["agree"] == 1.0 or (re["gate_flips"] == 1 and re["mean_ungated_jaccard"] == 1.0)
    assert [d["tool"] for d in report.disagreements] in ([], ["report_error"])
    # the replay is read-only: no hit counts moved
    assert ctx.storage.find_by_id(
        __import__("mcm_engine.backends", fromlist=["EntityType"]).EntityType.KNOWLEDGE, 1
    ).hit_count == 0
    assert "agree=1.000" in format_report(report)


def test_tampered_unified_side_is_reported(tmp_path):
    ctx = open_context(str(tmp_path / "k.db"))
    r = _seed(ctx.storage)
    # the unified side drifts: a rule loses its facets, a negative changes its text
    ctx.storage.update_entry("rule", r, title="renamed on the unified side")
    ctx.storage.delete_entry("knowledge", 1)
    ctx.storage.update_entry("negative", 1, summary="tampered")
    report = replay(ctx, extract_queries([FIXTURE]), store="k.db")
    tools = {d["tool"] for d in report.disagreements}
    assert {"get_entry", "read_rule", "search"} <= tools
    assert report.agreement["search"]["agree"] < 1.0
    assert report.agreement["get_entry"]["agree"] == 0.5   # #999 is missing on both sides


def test_cli_writes_a_json_report(tmp_path, capsys, monkeypatch):
    ctx = open_context(str(tmp_path / "k.db"))
    _seed(ctx.storage)
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "replay-queries", str(FIXTURE),
                                      "--store", str(tmp_path / "k.db"), "--out", str(out)])
    cli_main()
    printed = capsys.readouterr().out
    assert "search        n=3" in printed
    data = json.loads(out.read_text())
    assert data["counts"]["search"] == 3 and data["agreement"]["search"]["agree"] == 1.0


def test_verify_entries_cli_reports_counts_and_exit_code(tmp_path, capsys, monkeypatch):
    import pytest as _pytest
    ctx = open_context(str(tmp_path / "k.db"))
    _seed(ctx.storage)
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "corpus-verify-entries", "--store",
                                      str(tmp_path / "k.db")])
    cli_main()
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["rule"] == {"legacy": 2, "entries": 2}
    assert data["divergence"] == []
    ctx.storage.delete_entry("rule", 1)
    with _pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["rule"] == {"legacy": 2, "entries": 1}
    assert "rule #1: missing from entries" in data["divergence"]
