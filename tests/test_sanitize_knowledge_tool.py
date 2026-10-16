"""sanitize_knowledge maintenance tool + add_knowledge store-time guard (#142)."""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, KnowledgeRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.corpus import register_corpus_tools
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker

POLLUTED_SUMMARY = (
    "Real one-line summary.</summary>\n<detail>the body</detail>\n"
    "<kind>finding</kind>\n<project>localharness</project>\n</invoke>"
)


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, tracker)
    register_knowledge_tools(mcp, db, tracker, "mcm", lambda q, limit=5: "")
    return mcp.tools, s


def test_sanitize_knowledge_dry_run_reports_without_changing(env):
    tools, s = env
    rid = s.insert_knowledge(KnowledgeRow(id=0, topic="t", summary=POLLUTED_SUMMARY))
    s.insert_knowledge(KnowledgeRow(id=0, topic="clean", summary="nothing wrong here"))

    out = tools["sanitize_knowledge"](dry_run=True)
    assert "scanned 2" in out and "1 polluted" in out
    assert f"#{rid}" in out
    # Unchanged on disk.
    assert "</summary>" in s.find_by_id(EntityType.KNOWLEDGE, rid).summary


def test_sanitize_knowledge_apply_cleans_in_place(env):
    tools, s = env
    rid = s.insert_knowledge(KnowledgeRow(id=0, topic="t", summary=POLLUTED_SUMMARY))

    out = tools["sanitize_knowledge"](dry_run=False)
    assert "updated 1" in out

    row = s.find_by_id(EntityType.KNOWLEDGE, rid)
    assert row.summary == "Real one-line summary."
    assert "</summary>" not in row.summary and "</invoke>" not in row.summary
    assert row.detail == "the body"


def test_add_knowledge_guard_cleans_on_store(env):
    tools, s = env
    tools["add_knowledge"](topic="guarded", summary=POLLUTED_SUMMARY)
    row = s.find_knowledge_by_topic_kind("guarded", "finding")
    assert row is not None
    assert row.summary == "Real one-line summary."
    assert "</invoke>" not in row.summary
    assert row.detail == "the body"
