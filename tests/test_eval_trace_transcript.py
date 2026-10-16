"""Build a replay trace from a Claude Code transcript (issue #160).

A transcript is JSONL: assistant records carry tool_use blocks, user records
carry tool_result blocks (with is_error) paired by tool_use_id. The adapter
reuses the #159 classifier and is tolerant of malformed lines.
"""
from __future__ import annotations

import json

from mcm_engine.eval.trace_transcript import load_transcript


def _write(tmp_path, records):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def _asst(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _user(blocks):
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def test_parses_tool_use_into_trace(tmp_path):
    p = _write(tmp_path, [
        _asst([{"type": "tool_use", "id": "t1", "name": "mcp__mcm-engine__search",
                "input": {"query": "widget"}}]),
        _asst([{"type": "tool_use", "id": "t2", "name": "Edit",
                "input": {"file_path": "src/widget.py"}}]),
    ])
    trace = load_transcript(p)
    assert len(trace) == 2
    assert trace[0].looked_up is True and trace[0].topic == "widget"
    assert trace[1].consequential is True and trace[1].topic == "widget"
    assert trace[1].led_to_failure is False


def test_tool_result_error_marks_led_to_failure(tmp_path):
    p = _write(tmp_path, [
        _asst([{"type": "tool_use", "id": "t1", "name": "Bash",
                "input": {"command": "terraform apply"}}]),
        _user([{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                "content": "boom"}]),
    ])
    trace = load_transcript(p)
    assert len(trace) == 1
    assert trace[0].consequential is True and trace[0].topic == "terraform"
    assert trace[0].led_to_failure is True


def test_non_consequential_tool_is_not_flagged(tmp_path):
    p = _write(tmp_path, [
        _asst([{"type": "tool_use", "id": "t1", "name": "Bash",
                "input": {"command": "ls -la"}}]),
    ])
    trace = load_transcript(p)
    assert trace[0].consequential is False and trace[0].topic == ""


def test_order_preserved_and_seq_increments(tmp_path):
    p = _write(tmp_path, [
        _asst([{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x"}},
               {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "y.py"}}]),
    ])
    trace = load_transcript(p)
    assert [e.seq for e in trace] == [0, 1]


def test_tolerates_malformed_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        "not json{\n"
        + json.dumps(_asst([{"type": "tool_use", "id": "t1", "name": "Edit",
                             "input": {"file_path": "a.py"}}]))
        + "\n\n",
        encoding="utf-8",
    )
    trace = load_transcript(p)
    assert len(trace) == 1 and trace[0].topic == "a"


def test_missing_file_returns_empty(tmp_path):
    assert load_transcript(tmp_path / "nope.jsonl") == []
