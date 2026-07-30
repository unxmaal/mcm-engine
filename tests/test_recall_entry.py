"""recall_entry — recall-by-id for all four entity types (issue #103).

Generalizes kb_recall. knowledge/negative/error hard-delete; a rule moves to a
terminal status='recalled' (never deleted, so file-sync can't revive it). All
paths write a recall_log row tagged with entity_type. Postgres-gated (recall_log
is postgres-only), skips cleanly without a DB.
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from mcm_engine.backends import (  # noqa: E402
    EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow,
)
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


def _recall_log_type(claim_id):
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT entity_type FROM recall_log WHERE claim_id = %s "
                    "ORDER BY id DESC LIMIT 1", (claim_id,))
        r = cur.fetchone()
    return r[0] if r else None


def test_recall_knowledge_hard_deletes():
    tools, s = _pg_tools()
    kid = s.insert_knowledge(KnowledgeRow(id=0, topic="k-recall", summary="s"))
    out = tools["recall_entry"]("knowledge", kid, reason="pii", principal="gov")
    assert "Hard-deleted knowledge" in out
    assert s.find_by_id(EntityType.KNOWLEDGE, kid) is None
    assert _recall_log_type(kid) == "knowledge"


def test_recall_negative_hard_deletes():
    tools, s = _pg_tools()
    nid = s.insert_negative(NegativeRow(id=0, category="c", what_failed="wf"))
    out = tools["recall_entry"]("negative", nid)
    assert "Hard-deleted negative" in out
    assert s.find_by_id(EntityType.NEGATIVE, nid) is None
    assert _recall_log_type(nid) == "negative"


def test_recall_error_hard_deletes():
    tools, s = _pg_tools()
    eid = s.insert_error(ErrorRow(id=0, pattern="boom"))
    out = tools["recall_entry"]("error", eid)
    assert "Hard-deleted error" in out
    assert s.find_by_id(EntityType.ERROR, eid) is None
    assert _recall_log_type(eid) == "error"


def test_recall_rule_is_terminal_status_not_delete():
    tools, s = _pg_tools()
    rid = s.insert_rule(RuleRow(id=0, title="bad rule", keywords="x"))
    out = tools["recall_entry"]("rule", rid, reason="leaked secret")
    assert "Recalled (terminal status) rule" in out
    row = s.find_by_id(EntityType.RULE, rid)
    # The row survives (audit) but is terminally recalled.
    assert row is not None and row.status == "recalled"
    assert _recall_log_type(rid) == "rule"
    # A recalled rule is excluded from list_rules (session_start invariants).
    assert all(r.id != rid for r in s.list_rules(include_archived=True))
    # And a rule_events 'recalled' row was written.
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM rule_events WHERE rule_id = %s AND "
                    "event_type = 'recalled'", (rid,))
        assert cur.fetchone() is not None


def test_recall_not_found_is_explicit_no_audit():
    tools, _ = _pg_tools()
    assert "NOT_FOUND" in tools["recall_entry"]("knowledge", 987654321)
    assert _recall_log_type(987654321) is None
