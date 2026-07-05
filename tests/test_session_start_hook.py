"""SessionStart hook: compels the agent to load context via the MCP; it must
NOT open a database (issue #58)."""
from __future__ import annotations

import io
import json

from mcm_engine.hooks import session_start as ss


def test_injects_mcp_directive(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "SessionStart", "source": "startup", "cwd": "/tmp",
    })))
    rc = ss.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    # It compels the MCP call, not resume content from a db.
    assert "session_start" in ctx
    assert "MCP" in ctx


def test_hook_never_opens_storage():
    """Regression for #58: the module must not reach into the wiring/storage
    layer at all — importing build_verified_context here is the bug."""
    import inspect
    src = inspect.getsource(ss)
    assert "build_verified_context" not in src
    assert "build_context" not in src
    assert "storage" not in src.lower()


def test_fail_open_on_bad_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert ss.main() == 0


def test_fail_open_when_stdin_read_raises(monkeypatch):
    class _Boom:
        def read(self):
            raise RuntimeError("stdin gone")
    monkeypatch.setattr("sys.stdin", _Boom())
    assert ss.main() == 0
