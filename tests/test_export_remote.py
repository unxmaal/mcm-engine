"""issue #78: MCP-native rule export.

- `export_rules` MCP tool: pages ACTIVE rules with full bodies as JSON, caps the
  limit, flags has_more.
- `mirror.render_rules_to_tree` / `git_commit_tree`: the factored renderer both
  the direct and the remote paths share.
- `cli._export_remote_fetch`: the client paging loop (stubbed transport).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcm_engine import cli
from mcm_engine.db import KnowledgeDB
from mcm_engine.mirror import git_commit_tree, render_rules_to_tree
from mcm_engine.schema import migrate_core
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def rules(tmp_path):
    db = KnowledgeDB(str(tmp_path / "r.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    register_rules_tools(mcp, db, tracker, "t", [tmp_path / "rules"], tmp_path,
                         files_authoritative=False)
    return mcp.tools


def _ids(tools):
    return [r["id"] for r in json.loads(tools["export_rules"](0, 100))["rules"]]


def test_export_rules_returns_active_with_content(rules):
    rules["add_rule"](title="Alpha rule", keywords="a,b", content="# Alpha\nbody A")
    page = json.loads(rules["export_rules"](0, 100))
    assert page["total"] == 1 and page["returned"] == 1 and page["has_more"] is False
    r = page["rules"][0]
    assert r["title"] == "Alpha rule" and "body A" in r["content"]


def test_export_rules_pages_and_flags_has_more(rules):
    for i in range(3):
        rules["add_rule"](title=f"Rule {i}", keywords="k", content=f"body {i}")
    p1 = json.loads(rules["export_rules"](0, 2))
    assert p1["returned"] == 2 and p1["has_more"] is True
    p2 = json.loads(rules["export_rules"](2, 2))
    assert p2["returned"] == 1 and p2["has_more"] is False
    # the two pages cover disjoint ids, no overlap, no gap
    assert {r["id"] for r in p1["rules"]} | {r["id"] for r in p2["rules"]} == set(_ids(rules))


def test_export_rules_clamps_limit_to_cap(rules, monkeypatch):
    for i in range(3):
        rules["add_rule"](title=f"R{i}", keywords="k", content="x")
    monkeypatch.setenv("MCM_EXPORT_PAGE_MAX", "1")
    page = json.loads(rules["export_rules"](0, 100))  # ask for 100, cap is 1
    assert page["limit"] == 1 and page["returned"] == 1 and page["has_more"] is True


def test_export_rules_excludes_superseded(rules):
    rules["add_rule"](title="Old", keywords="k", content="old")
    rules["add_rule"](title="New", keywords="k", content="new")
    old_id, new_id = _ids(rules)
    rules["supersede_rule"](old_id, new_id)
    surviving = _ids(rules)
    assert old_id not in surviving and new_id in surviving


def test_export_rules_output_is_pure_json_no_nudge(rules):
    # The client json.loads() this; a nudge appended to the string would break it.
    rules["add_rule"](title="A", keywords="k", content="x")
    out = rules["export_rules"](0, 100)
    json.loads(out)  # must not raise
    assert not out.rstrip().endswith((">", "]")) or out.rstrip()[-1] == "}"


# ---- renderer (shared by direct + remote) -----------------------------------

def test_render_rules_to_tree_and_commit(tmp_path):
    rows = [
        SimpleNamespace(title="Cache policy", keywords="cache", category="design",
                        content="# Cache\nLWW per name"),
        SimpleNamespace(title="No sed", keywords="sed", category="tooling",
                        content="# No sed"),
    ]
    out = tmp_path / "mirror"
    written = render_rules_to_tree(rows, out)
    assert written == 2
    assert (out / "rules" / "design" / "cache-policy.md").exists()
    assert (out / "rules" / "tooling" / "no-sed.md").exists()
    assert git_commit_tree(out, written) is True          # first commit
    assert git_commit_tree(out, written) is False         # idempotent, no change


# ---- client paging loop -----------------------------------------------------

def test_export_remote_fetch_pages_until_done(monkeypatch):
    pages = [
        {"rules": [{"title": "A", "keywords": "k", "category": "c", "content": "a"},
                   {"title": "B", "keywords": "k", "category": "c", "content": "b"}],
         "offset": 0, "limit": 2, "returned": 2, "total": 3, "has_more": True},
        {"rules": [{"title": "C", "keywords": "k", "category": "c", "content": "c"}],
         "offset": 2, "limit": 2, "returned": 1, "total": 3, "has_more": False},
    ]
    calls = []

    def fake_call(url, headers, offset, limit, *, timeout=None):
        calls.append(offset)
        return json.dumps(pages.pop(0))

    monkeypatch.setattr(cli, "_export_remote_call", fake_call)
    rows = cli._export_remote_fetch("http://x/mcp", {}, page=2)
    assert [r.title for r in rows] == ["A", "B", "C"]
    assert calls == [0, 2]  # paged with offset advancing by returned


def test_export_remote_fetch_auth_error_aborts(monkeypatch):
    def fake_call(url, headers, offset, limit, *, timeout=None):
        raise cli._RemoteAuthError("401")

    monkeypatch.setattr(cli, "_export_remote_call", fake_call)
    with pytest.raises(cli._RemoteAuthError):
        cli._export_remote_fetch("http://x/mcp", {}, page=2)
