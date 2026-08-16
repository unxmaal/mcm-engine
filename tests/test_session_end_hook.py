"""SessionEnd hook (Phase 2): transcript token parse, argument assembly, write
routing, and fail-open behavior.

The actual storage upsert is covered by test_session_metrics_storage; here we
verify the hook wires transcript + accumulated state into the write call and
never disrupts teardown.
"""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import mcm_engine.hooks.session_end as se
from mcm_engine.hooks.mcp_enforcement import _state_path, _write_state


def _run(event: dict) -> int:
    stdin = io.StringIO(json.dumps(event))
    with patch.object(sys, "stdin", stdin):
        return se.main()


def _write_transcript(tmp_path, records):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


# ---- transcript parsing ----

def test_parse_transcript_sums_usage(tmp_path):
    p = _write_transcript(tmp_path, [
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 200, "output_tokens": 70}}},
        {"type": "user", "message": {"content": "hi"}},         # no usage
        {"usage": {"input_tokens": 1, "output_tokens": 1}},     # top-level usage
    ])
    t = se._parse_transcript_tokens(str(p))
    assert t == {"in": 301, "out": 121, "cache_read": 10, "cache_write": 5}


def test_parse_transcript_is_fail_open_on_garbage(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("not json\n{bad\n", encoding="utf-8")
    assert se._parse_transcript_tokens(str(p)) == {
        "in": 0, "out": 0, "cache_read": 0, "cache_write": 0}


def test_parse_missing_file_returns_zeros(tmp_path):
    assert se._parse_transcript_tokens(str(tmp_path / "nope.jsonl"))["out"] == 0


# ---- argument assembly ----

def test_build_arguments_merges_state_and_tokens():
    entry = {
        "first_seen_at": "2026-08-16T00:00:00+00:00",
        "fixation_events": 2,
        "metrics": {"loc_added": 40, "loc_removed": 12, "loc_churned": 8,
                    "comment_lines_added": 5, "code_lines_added": 35,
                    "edit_cycles_max": 4},
    }
    tokens = {"in": 5, "out": 9, "cache_read": 1, "cache_write": 2}
    args = se._build_arguments("sess-1", entry, tokens)
    assert args["cc_session_id"] == "sess-1"
    assert args["out_tokens"] == 9 and args["in_tokens"] == 5
    assert args["loc_churned"] == 8 and args["fixation_events"] == 2
    assert args["edit_cycles_max"] == 4


def test_has_signal():
    assert not se._has_signal({"cc_session_id": "x", "first_seen_at": "t"})
    assert se._has_signal({"cc_session_id": "x", "out_tokens": 1})


# ---- write routing + fail-open (end to end main) ----

def _seed_state(tmp_path, session_id="s1"):
    sp = _state_path(tmp_path)
    _write_state(sp, {session_id: {
        "first_seen_at": "2026-08-16T00:00:00+00:00",
        "fixation_events": 1,
        "metrics": {"loc_added": 10, "code_lines_added": 10, "edit_cycles_max": 2},
    }})


def test_main_routes_to_remote_when_endpoint_configured(tmp_path):
    _seed_state(tmp_path)
    tx = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": 3, "output_tokens": 7}}}])
    captured = {}

    def fake_call(url, name, arguments, headers=None, timeout=None):
        captured.update({"url": url, "name": name, "args": arguments})
        return ""

    with patch.object(se, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(se, "mcp_http_call_tool", side_effect=fake_call):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path),
                   "transcript_path": str(tx)})
    assert rc == 0
    assert captured["name"] == "record_session_metrics"
    assert captured["args"]["out_tokens"] == 7
    assert captured["args"]["loc_added"] == 10
    assert captured["args"]["fixation_events"] == 1


def test_main_routes_to_local_when_no_endpoint(tmp_path):
    _seed_state(tmp_path)
    tx = _write_transcript(tmp_path, [
        {"message": {"usage": {"output_tokens": 4}}}])
    captured = {}
    with patch.object(se, "_mcp_http_endpoint", return_value=(None, {})), \
         patch.object(se, "_write_local", side_effect=lambda cwd, args: captured.update(args)):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path),
                   "transcript_path": str(tx)})
    assert rc == 0
    assert captured["out_tokens"] == 4


def test_main_fail_open_when_write_raises(tmp_path):
    _seed_state(tmp_path)
    tx = _write_transcript(tmp_path, [
        {"message": {"usage": {"output_tokens": 4}}}])

    def boom(*a, **k):
        raise RuntimeError("backend down")

    with patch.object(se, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(se, "mcp_http_call_tool", side_effect=boom):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path),
                   "transcript_path": str(tx)})
    assert rc == 0  # teardown never disrupted


def test_main_skips_empty_session(tmp_path):
    # No state, no transcript tokens -> nothing worth recording, no write.
    called = {"n": 0}
    with patch.object(se, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(se, "mcp_http_call_tool",
                      side_effect=lambda *a, **k: called.__setitem__("n", called["n"] + 1)):
        rc = _run({"session_id": "ghost", "cwd": str(tmp_path)})
    assert rc == 0 and called["n"] == 0


def test_main_no_session_id_is_noop(tmp_path):
    assert _run({"cwd": str(tmp_path)}) == 0
