"""Hook state, ingest resume, unreadable files, call-time settings and schema skew (#226)."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from mcm_engine import cli, schema
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import RuleRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.eval.trace_transcript import load_transcript
from mcm_engine.files import watcher as watcher_mod
from mcm_engine.hooks import lessons, mcp_enforcement as hook, user_prompt_submit as ups
from mcm_engine.schema import CORE_VERSION, migrate_core
from mcm_engine.tools import rules as rules_mod
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker

REPO = Path(__file__).resolve().parent.parent


# ---- hook state file ----------------------------------------------------------

def _invoke(tool_name, *, session_id, cwd):
    event = {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {},
             "session_id": session_id, "cwd": str(cwd)}
    with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), patch.object(sys, "stderr", io.StringIO()):
        return hook.main()


def _state(cwd):
    return json.loads(hook._state_path(cwd).read_text(encoding="utf-8"))


def test_a_hook_that_runs_between_anothers_read_and_write_keeps_its_entry(tmp_path, monkeypatch):
    real, ran = hook._decide, []

    def decide(tool_name, s):
        if not ran:
            ran.append(1)
            _invoke("Bash", session_id="inner", cwd=tmp_path)
        return real(tool_name, s)

    monkeypatch.setattr(hook, "_decide", decide)
    _invoke("Bash", session_id="outer", cwd=tmp_path)
    keys = set(_state(tmp_path))
    assert any("inner" in k for k in keys), keys
    assert any("outer" in k for k in keys), keys


def test_a_leftover_at_the_old_fixed_temp_path_does_not_stop_the_write(tmp_path):
    sp = hook._state_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.with_suffix(sp.suffix + ".tmp").mkdir()
    _invoke("Bash", session_id="s1", cwd=tmp_path)
    assert any("s1" in k for k in _state(tmp_path))
    assert not [p.name for p in sp.parent.iterdir() if p.is_file() and p.name.endswith(".tmp")]


def test_compaction_stamp_keeps_other_agents_entries(tmp_path, monkeypatch):
    _invoke("Bash", session_id="other", cwd=tmp_path)
    real = hook.coverage.note_compaction

    def note(entry):
        _invoke("Bash", session_id="late", cwd=tmp_path)
        return real(entry)

    monkeypatch.setattr(hook.coverage, "note_compaction", note)
    hook.stamp_compaction_boundary({"session_id": "me", "cwd": str(tmp_path)})
    keys = set(_state(tmp_path))
    assert any("late" in k for k in keys) and any("other" in k for k in keys), keys


# ---- ingest resume file -------------------------------------------------------

_SPANS = [{"text": "always a", "source_topic": "a"}, {"text": "always b", "source_topic": "b"}]


def _partial_run(tmp_path, monkeypatch, url="http://x", strict=True):
    monkeypatch.setattr(cli, "_collect_remote_spans", lambda g, **kw: _SPANS)
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: (url, {}))
    monkeypatch.setattr(cli, "_sleep", lambda s: None)

    def stub(u, spans, **kw):
        if any(s["source_topic"] == "b" for s in spans):
            raise httpx.ReadError("boom")
        return "A-OK"

    monkeypatch.setattr(cli, "_sift_remote_call", stub)
    with pytest.raises(SystemExit):
        cli._ingest_remote([], tmp_path, 2, source_key="src", batch_size=1, batch_timeout=5, strict=strict)
    return tmp_path / ".mcm-engine" / "ingest-state.json"


def _second_run(tmp_path, monkeypatch, url="http://x", strict=True):
    seen = []
    monkeypatch.setattr(cli, "_remote_endpoint", lambda _pr: (url, {}))
    monkeypatch.setattr(cli, "_sift_remote_call",
                        lambda u, spans, **kw: seen.extend(s["source_topic"] for s in spans) or "OK")
    cli._ingest_remote([], tmp_path, 2, source_key="src", batch_size=1, batch_timeout=5, strict=strict)
    return seen


def test_ingest_resume_does_not_carry_over_to_another_server(tmp_path, monkeypatch, capsys):
    _partial_run(tmp_path, monkeypatch, url="http://x")
    assert sorted(_second_run(tmp_path, monkeypatch, url="http://y")) == ["a", "b"]


def test_ingest_resume_does_not_carry_over_to_the_other_gate(tmp_path, monkeypatch, capsys):
    _partial_run(tmp_path, monkeypatch, strict=True)
    assert sorted(_second_run(tmp_path, monkeypatch, strict=False)) == ["a", "b"]


@pytest.mark.gauntlet("a-child-that-outlives-its-owner", site="src/mcm_engine/cli.py:path")
def test_ingest_resume_file_expires(tmp_path, monkeypatch, capsys):
    state_file = _partial_run(tmp_path, monkeypatch)
    data = json.loads(state_file.read_text())
    data["saved_at"] = time.time() - 8 * 86400
    state_file.write_text(json.dumps(data))
    assert sorted(_second_run(tmp_path, monkeypatch)) == ["a", "b"]


def test_ingest_resume_still_skips_on_the_same_server_and_gate(tmp_path, monkeypatch, capsys):
    _partial_run(tmp_path, monkeypatch)
    assert _second_run(tmp_path, monkeypatch) == ["b"]


# ---- unreadable files ---------------------------------------------------------

class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_an_unreadable_rule_file_raises(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"# Title\n\xff\xfe\n")
    with pytest.raises(watcher_mod.RuleFileError):
        watcher_mod.parse_rule_file(bad)
    with pytest.raises(watcher_mod.RuleFileError):
        watcher_mod.parse_rule_file(tmp_path / "nope.md")


def test_there_is_one_rule_file_parser():
    assert rules_mod._parse_rule_file is watcher_mod.parse_rule_file


def test_sync_rules_names_the_files_it_could_not_read(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "good.md").write_text("# Good rule\n\n**Keywords:** k\n\nbody\n", encoding="utf-8")
    (rules_dir / "bad.md").write_bytes(b"# Bad rule\n\xff\xfe\n")
    mcp = _FakeMCP()
    register_rules_tools(mcp, db, SessionTracker(NudgeConfig()), "proj", [rules_dir], tmp_path)
    out = mcp.tools["sync_rules"]()
    assert "1 unreadable" in out and "bad.md" in out
    assert SqliteStorage(db=db).find_rule_by_title("Good rule") is not None


def test_watcher_logs_an_unreadable_file_and_leaves_its_row(tmp_path, monkeypatch):
    storage = SqliteStorage(db_path=str(tmp_path / "store.db"))
    storage.ensure_schema()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rid = storage.insert_rule(RuleRow(id=0, title="Kept", keywords="k", file_path="rules/kept.md"))
    (rules_dir / "kept.md").write_bytes(b"# Kept\n\xff\xfe\n")
    logged = []
    monkeypatch.setattr(watcher_mod, "log", logged.append)
    w = watcher_mod.RulesWatcher(storage=storage, rules_path=rules_dir, project_root=tmp_path)
    w.sync_once()
    assert any("kept.md" in m and "unreadable" in m for m in logged), logged
    from mcm_engine.backends import EntityType
    assert storage.find_by_id(EntityType.RULE, rid).archived is False


def test_a_missing_transcript_raises(tmp_path):
    with pytest.raises(OSError):
        load_transcript(tmp_path / "nope.jsonl")


def test_replay_eval_names_the_transcript_it_could_not_read(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    with pytest.raises(SystemExit) as e:
        cli.cmd_replay_eval(argparse.Namespace(transcript=[str(missing)]))
    assert e.value.code == 2
    assert "nope.jsonl" in capsys.readouterr().err


# ---- settings read when used, not at import -----------------------------------

def test_inject_cap_is_read_when_used(monkeypatch):
    result = "\n\n".join("hit %d %s" % (i, "x" * 80) for i in range(40))
    monkeypatch.setenv("MCM_INJECT_MAX_BYTES", "300")
    assert len(ups._truncate_injection(result, "q").encode()) <= 300
    monkeypatch.setenv("MCM_INJECT_MAX_BYTES", "0")
    assert ups._truncate_injection(result, "q") == result


def test_lessons_home_parts_follow_the_home_of_the_moment(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/zedhome")))
    at = lessons.ActionTerms()
    lessons._add_path(at, "/Users/zedhome")
    assert at.terms == []


def _script(name):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["audit_rules", "import_rules"])
def test_script_reads_its_endpoint_when_run(name, monkeypatch):
    mod = _script(name)
    monkeypatch.setenv("MCM_MCP_URL", "http://late.example/mcp")
    assert mod.default_url() == "http://late.example/mcp"


def test_import_script_reads_its_rules_dir_when_run(monkeypatch):
    mod = _script("import_rules")
    monkeypatch.setenv("MCM_RULES_DIR", "/late/rules")
    assert mod.default_rules_dir() == "/late/rules"


# ---- schema skew --------------------------------------------------------------

@pytest.mark.gauntlet("a-deploy-runs-two-releases-at-once", site="src/mcm_engine/schema.py:migrate_core:one-sided")
def test_an_older_build_on_a_newer_schema_says_so(tmp_path, monkeypatch):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    db.execute_write("UPDATE _mcm_versions SET version = ? WHERE component = 'core'", (CORE_VERSION + 1,))
    db.commit()
    logged = []
    monkeypatch.setattr(schema, "log", logged.append)
    migrate_core(db)
    assert any(f"v{CORE_VERSION + 1}" in m and "newer" in m for m in logged), logged
    row = db.execute("SELECT version FROM _mcm_versions WHERE component = 'core'").fetchone()
    assert row["version"] == CORE_VERSION + 1
