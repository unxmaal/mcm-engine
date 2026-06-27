"""SessionStart hook: emits resume context as additionalContext JSON.

Test-first. `mcm_engine.hooks.session_start` does not exist yet (red).
"""
from __future__ import annotations

import io
import json

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import KnowledgeRow, SessionRow
from mcm_engine.hooks import session_start as ss


@pytest.fixture
def seeded_storage():
    s = SqliteStorage(db_path=":memory:")
    s.ensure_schema()
    return s


def _add_handoff(storage, status, current_task="", next_steps="", blockers=""):
    storage.insert_session(SessionRow(
        id=0,
        status=status,
        current_task=current_task or None,
        findings_summary="",
        next_steps=next_steps or None,
        blockers=blockers or None,
        context_snapshot="{}",
    ))


class TestBuildSessionContext:
    def test_includes_handoff_fields(self, seeded_storage):
        _add_handoff(
            seeded_storage,
            status="mid-refactor of tracker",
            current_task="per-tool counters",
            next_steps="wire the SessionStart hook",
        )
        text = ss.build_session_context(seeded_storage, "unxmaal")
        assert "mid-refactor of tracker" in text
        assert "per-tool counters" in text
        assert "wire the SessionStart hook" in text
        assert "unxmaal" in text

    def test_includes_recent_knowledge_count(self, seeded_storage):
        seeded_storage.insert_knowledge(KnowledgeRow(id=0, topic="t", summary="s"))
        _add_handoff(seeded_storage, status="x")
        text = ss.build_session_context(seeded_storage, "p")
        assert "knowledge" in text.lower()

    def test_empty_when_no_data(self, seeded_storage):
        assert ss.build_session_context(seeded_storage, "p") == ""


class TestMain:
    def test_emits_additionalcontext_json(self, seeded_storage, monkeypatch, capsys):
        _add_handoff(seeded_storage, status="resume me please")
        monkeypatch.setattr(
            ss, "_load_storage", lambda cwd: (seeded_storage, "unxmaal"),
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "hook_event_name": "SessionStart", "source": "startup", "cwd": "/tmp",
        })))
        rc = ss.main()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "resume me please" in out["hookSpecificOutput"]["additionalContext"]

    def test_no_stdout_when_no_context(self, seeded_storage, monkeypatch, capsys):
        monkeypatch.setattr(
            ss, "_load_storage", lambda cwd: (seeded_storage, "p"),
        )
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        rc = ss.main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == ""

    def test_fail_open_when_storage_errors(self, monkeypatch, capsys):
        def boom(cwd):
            raise RuntimeError("db unreachable")
        monkeypatch.setattr(ss, "_load_storage", boom)
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert ss.main() == 0  # never block session start

    def test_malformed_stdin_does_not_crash(self, seeded_storage, monkeypatch):
        monkeypatch.setattr(
            ss, "_load_storage", lambda cwd: (seeded_storage, "p"),
        )
        monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
        assert ss.main() == 0
