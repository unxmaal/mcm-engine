"""Ambient recall adapts to transport (issue #57/#58): HTTP server -> MCP call,
stdio/no-server -> local authoritative store. The hook never speaks SQL to a
remote KB."""
from __future__ import annotations

import json

import pytest

from mcm_engine.hooks import mcp_enforcement as enf


@pytest.fixture(autouse=True)
def _no_env_url(monkeypatch):
    monkeypatch.delenv("MCM_MCP_URL", raising=False)


def _write_mcp_json(d, entry):
    (d / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mcm-engine": entry}}), encoding="utf-8")


# --- transport detection ---------------------------------------------------


def test_http_transport_yields_url(tmp_path):
    _write_mcp_json(tmp_path, {"type": "http", "url": "http://192.168.8.88:8080/mcp"})
    assert enf._mcp_http_url(tmp_path) == "http://192.168.8.88:8080/mcp"


def test_stdio_transport_yields_no_url(tmp_path):
    _write_mcp_json(tmp_path, {"command": "mcm-engine", "args": ["run"]})
    assert enf._mcp_http_url(tmp_path) is None


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_MCP_URL", "http://env:9/mcp")
    assert enf._mcp_http_url(tmp_path) == "http://env:9/mcp"


# --- branch selection ------------------------------------------------------


def test_http_config_uses_mcp_never_local(tmp_path, monkeypatch):
    _write_mcp_json(tmp_path, {"type": "http", "url": "http://x/mcp"})
    seen = {}

    def fake_http(url, q, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return ("T", "f")

    monkeypatch.setattr(enf, "_mcp_http_recall", fake_http)
    monkeypatch.setattr(enf, "_local_recall",
                        lambda q, cwd: pytest.fail("must not read a local db in HTTP mode"))
    assert enf._default_ambient_search("q", tmp_path) == ("T", "f")
    assert seen["url"] == "http://x/mcp"


def test_stdio_config_uses_local_never_http(tmp_path, monkeypatch):
    _write_mcp_json(tmp_path, {"command": "mcm-engine", "args": ["run"]})
    monkeypatch.setattr(enf, "_local_recall", lambda q, cwd: ("LocalRule", None))
    monkeypatch.setattr(enf, "_mcp_http_recall",
                        lambda url, q: pytest.fail("must not call HTTP in stdio mode"))
    assert enf._default_ambient_search("q", tmp_path) == ("LocalRule", None)


# --- parsing the search tool's text ----------------------------------------


def test_parse_top_rule_with_file():
    text = ("[RULE #85] booterizer: enable BOOTP (booterizer)\n"
            "  SGI machines netboot via BOOTP.\n"
            "  File: rules/mcm-engine/booterizer.md")
    assert enf._parse_top_rule(text) == ("booterizer: enable BOOTP", "rules/mcm-engine/booterizer.md")


def test_parse_top_rule_without_file():
    assert enf._parse_top_rule("[RULE #1] Some title (cat)\n  body") == ("Some title", None)


def test_parse_top_rule_no_hit():
    assert enf._parse_top_rule("") is None
    assert enf._parse_top_rule("No results for 'x'") is None
