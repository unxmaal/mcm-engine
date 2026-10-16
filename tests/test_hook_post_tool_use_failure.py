"""PostToolUseFailure hook: failure-triggered recall + dedup (issue #158).

A tool failure matches the error against known errors/fixes and injects the
match. A dedup key (hash of the noise-normalized error signature) suppresses
re-lookup for the same failure within a session, so a retry loop fires one
search, not one per attempt. User interrupts are skipped. Fail-open (#61).
"""
from __future__ import annotations

import io
import json

import pytest

from mcm_engine.hooks.mcp_enforcement import _read_state, _state_path
from mcm_engine.hooks.post_tool_use_failure import (
    main,
    query_from_error,
    signature,
)


# --- signature / dedup ------------------------------------------------------


def test_signature_normalizes_run_to_run_noise():
    a = signature("Segfault at 0x7ffee3b2 in worker 4821", "Bash")
    b = signature("Segfault at 0x1a2b3c4d in worker 9", "Bash")
    assert a == b  # hex addresses and integers normalized out


def test_signature_distinguishes_different_errors():
    a = signature("ModuleNotFoundError: no module named psycopg", "Bash")
    b = signature("PermissionError: denied", "Bash")
    assert a != b


def test_signature_normalizes_absolute_paths_to_basename():
    a = signature("cannot open /home/alice/project/src/foo.py", "Edit")
    b = signature("cannot open /var/tmp/build/src/foo.py", "Edit")
    assert a == b


# --- query extraction -------------------------------------------------------


def test_query_prefers_quoted_identifier():
    assert query_from_error("ModuleNotFoundError: No module named 'psycopg'") == "psycopg"


def test_query_falls_back_to_camelcase_class():
    assert query_from_error("Raised PermissionError while opening") == "permissionerror"


def test_query_empty_when_nothing_distinctive():
    assert query_from_error("ugh no") == ""


# --- main() wiring ----------------------------------------------------------


def _run(monkeypatch, capsys, event, *, search=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    rc = main(search=search)
    return rc, capsys.readouterr().out


def test_failure_matches_and_injects(monkeypatch, capsys, tmp_path):
    def fake_search(q, cwd):
        assert q == "psycopg"
        return "[ERROR #12] install mcm-engine[postgres]"

    rc, out = _run(
        monkeypatch, capsys,
        {"tool_name": "Bash", "error": "ModuleNotFoundError: No module named 'psycopg'",
         "cwd": str(tmp_path), "session_id": "s1"},
        search=fake_search,
    )
    assert rc == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    assert "[ERROR #12]" in hso["additionalContext"]


def test_dedup_suppresses_second_identical_failure(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_search(q, cwd):
        calls.append(q)
        return "match"

    event = {"tool_name": "Bash", "error": "ValueError: bad input at line 42",
             "cwd": str(tmp_path), "session_id": "s1"}
    # First failure: looks up.
    _run(monkeypatch, capsys, event, search=fake_search)
    # Same failure class (only the line number differs): suppressed.
    event2 = dict(event, error="ValueError: bad input at line 99")
    rc, out = _run(monkeypatch, capsys, event2, search=fake_search)
    assert rc == 0
    assert out.strip() == ""  # nothing injected the second time
    assert calls == ["valueerror"]  # the search ran exactly once (CamelCase class)


def test_different_failure_not_deduped(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_search(q, cwd):
        calls.append(q)
        return "match"

    base = {"tool_name": "Bash", "cwd": str(tmp_path), "session_id": "s1"}
    _run(monkeypatch, capsys, dict(base, error="ValueError: bad input"), search=fake_search)
    _run(monkeypatch, capsys, dict(base, error="KeyError: missing config"), search=fake_search)
    assert len(calls) == 2  # two distinct failures -> two lookups


def test_interrupt_is_skipped(monkeypatch, capsys, tmp_path):
    calls = []
    rc, out = _run(
        monkeypatch, capsys,
        {"tool_name": "Bash", "error": "KeyboardInterrupt", "is_interrupt": True,
         "cwd": str(tmp_path)},
        search=lambda q, cwd: calls.append(q) or "x",
    )
    assert rc == 0 and out.strip() == "" and calls == []


def test_empty_error_is_noop(monkeypatch, capsys, tmp_path):
    rc, out = _run(
        monkeypatch, capsys,
        {"tool_name": "Bash", "cwd": str(tmp_path)},
        search=lambda q, cwd: "x",
    )
    assert rc == 0 and out.strip() == ""


def test_error_from_tool_response_dict(monkeypatch, capsys, tmp_path):
    rc, out = _run(
        monkeypatch, capsys,
        {"tool_name": "Bash", "tool_response": {"stderr": "FileNotFoundError: missing"},
         "cwd": str(tmp_path), "session_id": "s1"},
        search=lambda q, cwd: "[ERROR #5] match",
    )
    hso = json.loads(out)["hookSpecificOutput"]
    assert "[ERROR #5]" in hso["additionalContext"]


def test_empty_search_still_records_dedup(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_search(q, cwd):
        calls.append(q)
        return ""  # no match

    event = {"tool_name": "Bash", "error": "ValueError: nope",
             "cwd": str(tmp_path), "session_id": "s1"}
    _run(monkeypatch, capsys, event, search=fake_search)
    _run(monkeypatch, capsys, event, search=fake_search)
    assert len(calls) == 1  # deduped even though the first lookup found nothing


def test_bad_stdin_is_fail_open(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{"))
    assert main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_dedup_key_recorded_in_state(monkeypatch, capsys, tmp_path):
    _run(
        monkeypatch, capsys,
        {"tool_name": "Bash", "error": "ValueError: x", "cwd": str(tmp_path),
         "session_id": "s7"},
        search=lambda q, cwd: "m",
    )
    entry = _read_state(_state_path(tmp_path))["s7"]
    assert entry["failure_lookups"]  # at least one dedup key stored
