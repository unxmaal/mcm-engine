"""kb_recall hard-delete + audit path (issue #98).

Under the connection pool (#83) the tool-layer reach to storage._conn raised
'no active Postgres connection on this call-chain'. kb_recall now detects the
backend by identity and borrows a connection via storage.transaction(). These
are postgres-gated (recall_log is postgres-only) and skip cleanly without a DB.
"""
from __future__ import annotations

import os

import pytest

# Skip the whole module (not error) when the postgres extra isn't installed —
# the embedded CI job has no psycopg. recall_log is postgres-only anyway.
psycopg = pytest.importorskip("psycopg")

from mcm_engine.backends import EntityType, KnowledgeRow  # noqa: E402
from mcm_engine.tools.knowledge import register_knowledge_tools  # noqa: E402
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
    register_knowledge_tools(mcp, ctx, SessionTracker(NudgeConfig()), "proj",
                             lambda *a, **k: [])
    return mcp.tools, s


def test_kb_recall_deletes_and_writes_audit_row():
    tools, s = _pg_tools()
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="recall-me", summary="s"))
    out = tools["kb_recall"](kid, reason="oops", principal="gov")
    assert "Recalled claim" in out
    # row is hard-deleted
    assert s.find_by_id(EntityType.KNOWLEDGE, kid) is None
    # and a recall_log audit row exists
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT principal, reason FROM recall_log WHERE claim_id = %s", (kid,))
        rec = cur.fetchone()
    assert rec is not None and rec[0] == "gov"


def test_kb_recall_not_found_is_explicit():
    tools, _ = _pg_tools()
    assert "NOT_FOUND" in tools["kb_recall"](987654321)


def test_kb_recall_not_found_leaves_no_audit_row():
    tools, _ = _pg_tools()
    tools["kb_recall"](987654322)
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM recall_log WHERE claim_id = %s", (987654322,))
        assert cur.fetchone() is None
