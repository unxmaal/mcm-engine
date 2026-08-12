"""find_duplicate_entries / find_conflicting_entries — dedup + conflict for the
non-rule entity types (issue #113).

Reuses the generic MinHash/LSH engine (dedup.py) over knowledge/negative/error,
mirroring find_duplicate_rules / find_conflicting_rules. SQLite-only.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import ErrorRow, KnowledgeRow, NegativeRow
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.corpus import register_corpus_tools
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
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    tracker = SessionTracker(NudgeConfig())
    mcp = _FakeMCP()
    register_corpus_tools(mcp, db, tracker)
    return mcp.tools, s, tracker


def test_duplicate_knowledge_clusters(env):
    tools, s, _t = env
    text = ("The Postgres connection pool must be borrowed inside a "
            "transaction block before self._conn resolves to a live handle.")
    a = s.insert_knowledge(KnowledgeRow(id=0, topic="pool borrow", summary=text))
    b = s.insert_knowledge(KnowledgeRow(id=0, topic="pool borrow", summary=text))
    # An unrelated entry must not join the cluster.
    s.insert_knowledge(KnowledgeRow(
        id=0, topic="unrelated", summary="Cats sleep sixteen hours a day."))
    out = tools["find_duplicate_entries"]("knowledge", 0.9)
    assert "near-duplicate knowledge cluster" in out
    assert f"#{a}" in out and f"#{b}" in out


def test_no_duplicates_message(env):
    tools, s, _t = env
    s.insert_knowledge(KnowledgeRow(
        id=0, topic="alpha", summary="A wholly distinct first observation."))
    s.insert_knowledge(KnowledgeRow(
        id=0, topic="beta", summary="An entirely different second remark."))
    out = tools["find_duplicate_entries"]("knowledge", 0.9)
    assert "No near-duplicate knowledge entries found." in out


def test_conflicting_knowledge_pairs(env):
    tools, s, _t = env
    a = s.insert_knowledge(KnowledgeRow(
        id=0, topic="database connection pooling strategy",
        summary="Always keep a fixed pool of twenty warm connections open."))
    b = s.insert_knowledge(KnowledgeRow(
        id=0, topic="database connection pooling strategy",
        summary="Never pool; open a fresh connection for each request instead."))
    out = tools["find_conflicting_entries"]("knowledge", 0.5, 0.4)
    assert "conflict candidate" in out
    assert f"#{a}" in out and f"#{b}" in out


def test_works_for_negative_and_error(env):
    tools, s, _t = env
    dup = ("Reached for a global mutable singleton to share the connection, "
           "which broke under concurrent async tasks with a locked database.")
    n1 = s.insert_negative(NegativeRow(id=0, category="concurrency", what_failed=dup))
    n2 = s.insert_negative(NegativeRow(id=0, category="concurrency", what_failed=dup))
    out = tools["find_duplicate_entries"]("negative", 0.9)
    assert f"#{n1}" in out and f"#{n2}" in out

    boom = ("Traceback: sqlite3.OperationalError database is locked during the "
            "WAL checkpoint while a concurrent writer held the reserved lock.")
    e1 = s.insert_error(ErrorRow(id=0, pattern=boom))
    e2 = s.insert_error(ErrorRow(id=0, pattern=boom))
    out_e = tools["find_duplicate_entries"]("error", 0.9)
    assert f"#{e1}" in out_e and f"#{e2}" in out_e


def test_read_only_accounting(env):
    tools, _s, tracker = env
    tools["find_duplicate_entries"]("knowledge")
    tools["find_conflicting_entries"]("knowledge")
    assert "find_duplicate_entries" in tracker.READ_ONLY_TOOLS
    assert "find_conflicting_entries" in tracker.READ_ONLY_TOOLS
    assert tracker.calls_since.get("find_duplicate_entries", 0) == 0
