"""supersede_knowledge on the Postgres adapter (issue #111).

Verifies the v14 knowledge lifecycle columns, the supersede/unsupersede storage
methods, and the audit relation on live Postgres. Postgres-gated; skips cleanly
without a DB.
"""
from __future__ import annotations

import os

import pytest

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
    register_knowledge_tools(mcp, ctx, SessionTracker(NudgeConfig()),
                             "test-project", lambda *a, **k: "")
    return mcp.tools, s


def test_pg_has_lifecycle_columns():
    _tools, _s = _pg_tools()
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'knowledge' AND column_name IN "
                    "('status', 'superseded_by')")
        cols = {r[0] for r in cur.fetchall()}
    assert cols == {"status", "superseded_by"}


def test_pg_supersede_and_unsupersede_roundtrip():
    tools, s = _pg_tools()
    old = s.insert_knowledge(KnowledgeRow(id=0, topic="pg-old", summary="s1"))
    new = s.insert_knowledge(KnowledgeRow(id=0, topic="pg-new", summary="s2"))

    out = tools["supersede_knowledge"](old_id=old, new_id=new)
    assert "Superseded knowledge" in out
    row = s.find_by_id(EntityType.KNOWLEDGE, old)
    assert row.status == "superseded" and row.superseded_by == new
    # Audit relation recorded.
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT relation FROM relations WHERE source_id = %s AND "
                    "target_id = %s AND source_type = 'knowledge'", (new, old))
        r = cur.fetchone()
    assert r is not None and r[0] == "supersedes"

    tools["unsupersede_knowledge"](knowledge_id=old)
    row2 = s.find_by_id(EntityType.KNOWLEDGE, old)
    assert row2.status == "active" and row2.superseded_by is None


def test_pg_refuses_self_supersede():
    tools, s = _pg_tools()
    k = s.insert_knowledge(KnowledgeRow(id=0, topic="pg-self", summary="s"))
    assert "cannot supersede itself" in tools["supersede_knowledge"](
        old_id=k, new_id=k)
