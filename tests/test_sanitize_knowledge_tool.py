"""sanitize_knowledge maintenance tool + add_knowledge store-time guard (#142)."""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow
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


# --- NEGATIVE / ERROR sweep + guards (issue #170) ---------------------------

POLLUTED_WF = (
    "The real what-failed sentence.</what_failed>\n"
    '<parameter name="why_failed">the real why</parameter>\n'
    '<parameter name="correct_approach">the real fix</parameter>\n'
    '<parameter name="project">localharness</parameter>\n</invoke>'
)
POLLUTED_PAT = (
    "The real error pattern.</error_text>\n"
    '<parameter name="context">the real context</parameter>\n</invoke>'
)


def test_sanitize_corpus_sweeps_negative_and_error(env):
    tools, s = env
    nid = s.insert_negative(NegativeRow(id=0, category="c", what_failed=POLLUTED_WF))
    eid = s.insert_error(ErrorRow(id=0, pattern=POLLUTED_PAT))
    s.insert_negative(NegativeRow(id=0, category="ok", what_failed="clean"))

    dry = tools["sanitize_corpus"](dry_run=True)
    assert "2 polluted across 3 table(s)" in dry
    assert f"#{nid}" in dry and f"#{eid}" in dry

    out = tools["sanitize_corpus"](dry_run=False)
    assert "applied" in out

    neg = s.find_by_id(EntityType.NEGATIVE, nid)
    assert neg.what_failed == "The real what-failed sentence."
    assert neg.why_failed == "the real why"
    assert neg.correct_approach == "the real fix"

    err = s.find_by_id(EntityType.ERROR, eid)
    assert err.pattern == "The real error pattern."
    assert err.context == "the real context"


def test_add_negative_guard_cleans_on_store(env):
    tools, s = env
    tools["add_negative"](category="c", what_failed=POLLUTED_WF)
    rows = [r for r in s.iter_entries(EntityType.NEGATIVE) if r.category == "c"]
    assert len(rows) == 1
    row = rows[0]
    assert row.what_failed == "The real what-failed sentence."
    assert row.why_failed == "the real why"
    assert "</invoke>" not in (row.what_failed or "")


def test_report_error_guard_cleans_on_store(tmp_path):
    db = KnowledgeDB(str(tmp_path / "e.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, tracker)
    # report_error calls search_all_fn(query, limit=, ids_out=); tolerate it.
    register_knowledge_tools(
        mcp, db, tracker, "mcm", lambda q, limit=5, ids_out=None: ""
    )
    mcp.tools["report_error"](error_text=POLLUTED_PAT)
    rows = list(s.iter_entries(EntityType.ERROR))
    assert len(rows) == 1
    assert rows[0].pattern == "The real error pattern."
    assert rows[0].context == "the real context"
    assert "</invoke>" not in (rows[0].pattern or "")
