"""Risk-based coverage gate on PreToolUse (issue #159).

The fixed-8 interval is kept as a net, but the real trigger is a CONSEQUENTIAL
op (file mutation, or deploy/destructive command) whose topic has no resident
coverage this session: the hook does the lookup itself and injects it, then
allows. Routine bash and already-covered topics pass silently. Deterministic
classifier only (DECISION #39). Fail-open (RULE #61).
"""
from __future__ import annotations

import io
import json

import pytest

from mcm_engine.hooks import coverage, mcp_enforcement
from mcm_engine.hooks.mcp_enforcement import (
    _consequential_op,
    _coverage_gate,
    _file_topic,
    _read_state,
    _state_path,
    stamp_compaction_boundary,
)


# --- classifier -------------------------------------------------------------


def test_file_topic_is_basename_stem():
    assert _file_topic("src/mcm_engine/tools/search.py") == "search"
    assert _file_topic("/a/b/Makefile") == "makefile"
    assert _file_topic("") == ""


@pytest.mark.parametrize("tool,ti,expected_conseq,expected_topic", [
    ("Edit", {"file_path": "src/foo/bar.py"}, True, "bar"),
    ("Write", {"file_path": "x/config.yaml"}, True, "config"),
    ("Bash", {"command": "terraform apply -auto-approve"}, True, "terraform"),
    ("Bash", {"command": "rm -rf build/"}, True, "rm"),
    ("Bash", {"command": "kubectl delete pod web"}, True, "kubectl"),
    ("Bash", {"command": "ls -la && cat README.md"}, False, ""),
    ("Bash", {"command": "git status"}, False, ""),
    ("Bash", {"command": "uv run pytest -q"}, False, ""),
    ("Read", {"file_path": "foo.py"}, False, ""),
])
def test_consequential_op(tool, ti, expected_conseq, expected_topic):
    conseq, topic = _consequential_op(tool, {"tool_input": ti})
    assert conseq is expected_conseq
    assert topic == expected_topic


# --- _coverage_gate ---------------------------------------------------------


def test_gate_injects_on_uncovered_consequential_op(tmp_path):
    s = {}
    event = {"tool_name": "Edit", "tool_input": {"file_path": "src/widget.py"},
             "cwd": str(tmp_path)}
    msg = _coverage_gate("Edit", event, s, tmp_path, now=100.0,
                         search=lambda q, cwd: f"[RULE #9] {q} notes")
    assert msg is not None and "widget" in msg
    # coverage recorded so a repeat does not re-fire
    assert coverage.is_covered(s, "widget", now=101.0) is True


def test_gate_silent_when_already_covered(tmp_path):
    s = {}
    coverage.record_topic(s, "widget", now=50.0)
    calls = []
    msg = _coverage_gate(
        "Edit", {"tool_input": {"file_path": "widget.py"}}, s, tmp_path,
        now=100.0, search=lambda q, cwd: calls.append(q) or "x",
    )
    assert msg is None
    assert calls == []  # no lookup for a covered topic


def test_gate_none_on_non_consequential(tmp_path):
    msg = _coverage_gate(
        "Bash", {"tool_input": {"command": "ls"}}, {}, tmp_path,
        now=100.0, search=lambda q, cwd: "x",
    )
    assert msg is None


def test_gate_records_coverage_even_when_search_empty(tmp_path):
    s = {}
    msg = _coverage_gate(
        "Edit", {"tool_input": {"file_path": "widget.py"}}, s, tmp_path,
        now=100.0, search=lambda q, cwd: "",
    )
    assert msg is None  # nothing to inject
    assert coverage.is_covered(s, "widget", now=101.0) is True  # but covered


def test_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_COVERAGE_GATE", "0")
    msg = _coverage_gate(
        "Edit", {"tool_input": {"file_path": "widget.py"}}, {}, tmp_path,
        now=100.0, search=lambda q, cwd: "x",
    )
    assert msg is None


# --- main() integration -----------------------------------------------------


def _run(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    mcp_enforcement.main()
    return capsys.readouterr().out


def test_main_injects_gate_on_first_edit(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mcp_enforcement, "_gate_http_search",
                        lambda q, cwd: f"[RULE #9] about {q}")
    out = _run(monkeypatch, capsys,
               {"tool_name": "Edit", "tool_input": {"file_path": "src/widget.py"},
                "cwd": str(tmp_path), "session_id": "s1"})
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "widget" in hso["additionalContext"]


def test_main_second_edit_same_topic_is_silent(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(mcp_enforcement, "_gate_http_search",
                        lambda q, cwd: calls.append(q) or f"hit {q}")
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/widget.py"},
          "cwd": str(tmp_path), "session_id": "s1"}
    _run(monkeypatch, capsys, ev)
    out2 = _run(monkeypatch, capsys, ev)
    assert out2.strip() == ""  # covered now: no gate, no warn (only 2 edits)
    assert calls == ["widget"]  # looked up exactly once


def test_main_agent_search_covers_topic_for_gate(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(mcp_enforcement, "_gate_http_search",
                        lambda q, cwd: calls.append(q) or "hit")
    # the agent itself searches "widget"
    _run(monkeypatch, capsys,
         {"tool_name": "mcp__mcm-engine__search", "tool_input": {"query": "widget"},
          "cwd": str(tmp_path), "session_id": "s1"})
    # then edits widget.py — already covered by the observed search
    out = _run(monkeypatch, capsys,
               {"tool_name": "Edit", "tool_input": {"file_path": "widget.py"},
                "cwd": str(tmp_path), "session_id": "s1"})
    assert out.strip() == ""
    assert calls == []  # the gate never had to look up


def test_interval_only_warn_counted_on_non_consequential(monkeypatch, capsys, tmp_path):
    # drive WARN_THRESHOLD routine bash calls -> the fixed-8 net warns, and
    # since bash-ls is non-consequential it counts as an interval-only catch.
    for _ in range(mcp_enforcement.WARN_THRESHOLD):
        _run(monkeypatch, capsys,
             {"tool_name": "Bash", "tool_input": {"command": "ls"},
              "cwd": str(tmp_path), "session_id": "s9"})
    s = _read_state(_state_path(tmp_path))["s9"]
    assert s.get("interval_only_warns", 0) >= 1


# --- residency across a compaction boundary --------------------------------


def test_stamp_compaction_makes_coverage_non_resident(monkeypatch, capsys, tmp_path):
    hits = []
    monkeypatch.setattr(mcp_enforcement, "_gate_http_search",
                        lambda q, cwd: hits.append(q) or f"hit {q}")
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "widget.py"},
          "cwd": str(tmp_path), "session_id": "s1"}
    # first edit: gate fires, topic now covered + resident
    _run(monkeypatch, capsys, ev)
    assert hits == ["widget"]
    # a compaction boundary drops residency
    stamp_compaction_boundary({"cwd": str(tmp_path), "session_id": "s1"})
    # next edit of the same file: non-resident -> gate fires again
    _run(monkeypatch, capsys, ev)
    assert hits == ["widget", "widget"]
