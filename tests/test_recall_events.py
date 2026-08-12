"""recall_events — paged read of the recall audit trail (issue #110).

Read-only consumable counterpart to recall_entry. Postgres-gated (recall_log is
postgres-only), skips cleanly without a DB.
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from mcm_engine.backends import KnowledgeRow  # noqa: E402
from mcm_engine.tools.corpus import register_corpus_tools  # noqa: E402
from mcm_engine.tracker import NudgeConfig, SessionTracker  # noqa: E402
from mcm_engine.wiring import Context  # noqa: E402

DSN = os.environ.get("MCM_TEST_POSTGRES_DSN",
                     "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="no postgres at DSN")


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _pg_tools():
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    s = PostgresStorage(DSN)
    s.ensure_schema()
    ctx = Context(storage=s, counters=None, search=None, session=None)
    mcp = _FakeMCP()
    register_corpus_tools(mcp, ctx, SessionTracker(NudgeConfig()))
    return mcp.tools, s


def _max_recall_id():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM recall_log")
        return cur.fetchone()[0]


def test_recorded_recall_shows_up():
    tools, s = _pg_tools()
    before = _max_recall_id()
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="ev", summary="s"))
    tools["recall_entry"]("knowledge", kid, reason="pii", principal="gov")
    out = tools["recall_events"](after_id=before)
    assert f"[recall] knowledge #{kid}" in out
    assert "principal=gov" in out and "reason=pii" in out
    assert "recalled=" in out
    assert "next: recall_events(after_id=" in out


def test_keyset_cursor_excludes_prior_events():
    tools, s = _pg_tools()
    k1 = s.insert_knowledge(KnowledgeRow(id=0, topic="a", summary="s"))
    k2 = s.insert_knowledge(KnowledgeRow(id=0, topic="b", summary="s"))
    start = _max_recall_id()
    tools["recall_entry"]("knowledge", k1)
    mid = _max_recall_id()
    tools["recall_entry"]("knowledge", k2)
    page = tools["recall_events"](after_id=mid)
    # Only the second recall (id > mid) is on this page.
    assert f"knowledge #{k2}" in page and f"knowledge #{k1}" not in page
    _ = start


def test_empty_page_at_end():
    tools, _s = _pg_tools()
    out = tools["recall_events"](after_id=10_000_000_000)
    assert "No recall events" in out and "End of recall log" in out


def test_since_filter_is_applied():
    tools, s = _pg_tools()
    before = _max_recall_id()
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="since", summary="s"))
    tools["recall_entry"]("knowledge", kid)
    # A far-future `since` excludes everything.
    out = tools["recall_events"](after_id=before, since="2999-01-01")
    assert "No recall events" in out and "at/after 2999-01-01" in out


def test_read_only_accounting():
    tools, _s = _pg_tools()
    from mcm_engine.tracker import SessionTracker
    assert "recall_events" in SessionTracker.READ_ONLY_TOOLS
