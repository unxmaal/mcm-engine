"""PreCompact hook (#145): argument assembly, write routing (remote MCP tool vs
local store), and fail-open behavior.

The snapshot persistence itself is covered by the save_snapshot tool tests; here
we verify the hook turns a PreCompact event into a save_snapshot call and never
blocks or delays a compaction.
"""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import mcm_engine.hooks.pre_compact as pc


def _run(event: dict) -> int:
    with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
        return pc.main()


# ---- argument assembly ----

def test_manual_trigger_keeps_custom_instructions():
    args = pc._build_arguments("manual", "focus on the auth refactor")
    assert "manual" in args["progress"]
    assert args["open_questions"] == "focus on the auth refactor"


def test_auto_trigger_has_no_open_questions():
    args = pc._build_arguments("auto", "")
    assert "auto" in args["progress"]
    assert "open_questions" not in args


def test_missing_trigger_defaults_to_auto():
    args = pc._build_arguments("", "")
    assert "auto" in args["progress"]


# ---- write routing + fail-open (end to end main) ----

def test_main_routes_to_remote_when_endpoint_configured(tmp_path):
    captured = {}

    def fake_call(url, name, arguments, headers=None, timeout=None):
        captured.update({"url": url, "name": name, "args": arguments})
        return ""

    with patch.object(pc, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(pc, "mcp_http_call_tool", side_effect=fake_call):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path),
                   "trigger": "manual", "custom_instructions": "keep the ledger work"})
    assert rc == 0
    assert captured["name"] == "save_snapshot"
    assert "manual" in captured["args"]["progress"]
    assert captured["args"]["open_questions"] == "keep the ledger work"


def test_main_routes_to_local_when_no_endpoint(tmp_path):
    captured = {}
    with patch.object(pc, "_mcp_http_endpoint", return_value=(None, {})), \
         patch.object(pc, "_write_local", side_effect=lambda cwd, args: captured.update(args)):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path), "trigger": "auto"})
    assert rc == 0
    assert "auto" in captured["progress"]


def test_main_fail_open_when_write_raises(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    with patch.object(pc, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(pc, "mcp_http_call_tool", side_effect=boom):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path), "trigger": "manual"})
    assert rc == 0  # a compaction is never blocked by this hook


def test_main_fail_open_on_bad_stdin():
    with patch.object(sys, "stdin", io.StringIO("not json")):
        assert pc.main() == 0


def test_hook_does_not_read_a_shadow_store_on_the_remote_path(tmp_path):
    """Regression for issue #58 intent: when a server is configured, the write
    goes over HTTP to the authoritative server — the hook must not fall back to
    opening a local store."""
    with patch.object(pc, "_mcp_http_endpoint", return_value=("http://x/mcp", {})), \
         patch.object(pc, "mcp_http_call_tool", side_effect=lambda *a, **k: ""), \
         patch.object(pc, "_write_local", side_effect=AssertionError("must not touch local store")):
        rc = _run({"session_id": "s1", "cwd": str(tmp_path), "trigger": "manual"})
    assert rc == 0
