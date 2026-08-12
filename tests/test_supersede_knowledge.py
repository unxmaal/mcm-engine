"""supersede_knowledge / unsupersede_knowledge — soft-expire a knowledge
finding in the knowledge id space (issue #111).

Mirrors supersede_rule: old drops out of default search but stays inspectable
via get_entry / include_archived, a `supersedes` audit relation is recorded, and
the whole thing is reversible. SQLite-only.
"""
from __future__ import annotations

import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.corpus import register_corpus_tools
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.relations import register_relations_tools
from mcm_engine.tools.search import register_search_tools


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


@pytest.fixture
def env(db):
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100, mandatory_stop_turns=200,
    ))
    search_all_fn = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "test-project", search_all_fn)
    register_relations_tools(mcp, db, tracker)
    register_corpus_tools(mcp, db, tracker)
    return mcp, db, tracker


def _add(mcp, db, topic, summary):
    mcp["add_knowledge"](topic=topic, summary=summary)
    return db.execute(
        "SELECT id FROM knowledge WHERE topic = ?", (topic,)).fetchone()["id"]


def test_supersede_hides_from_search_but_keeps_inspectable(env):
    mcp, db, _t = env
    old = _add(mcp, db, "griffin pool sizing", "keep twenty warm connections")
    new = _add(mcp, db, "griffin pool sizing v2", "pool size is now dynamic")

    out = mcp["supersede_knowledge"](old_id=old, new_id=new)
    assert "Superseded knowledge" in out and f"#{old}" in out and f"#{new}" in out

    # Status flipped.
    row = db.execute("SELECT status, superseded_by FROM knowledge WHERE id = ?",
                     (old,)).fetchone()
    assert row["status"] == "superseded" and row["superseded_by"] == new

    # Hidden from default search...
    hidden = mcp["search"](query="griffin pool sizing", scope="knowledge")
    assert f"#{old}]" not in hidden
    # ...but reachable via get_entry (audit) and include_archived.
    assert f"#{old} [knowledge]" in mcp["get_entry"]("knowledge", old)
    shown = mcp["search"](query="griffin pool", scope="knowledge",
                          include_archived=True)
    assert f"#{old}]" in shown

    # A `supersedes` audit relation new -> old was recorded.
    rel = db.execute(
        "SELECT relation FROM relations WHERE source_id = ? AND target_id = ?",
        (new, old)).fetchone()
    assert rel is not None and rel["relation"] == "supersedes"


def test_unsupersede_reverts_and_removes_relation(env):
    mcp, db, _t = env
    old = _add(mcp, db, "narwhal cache policy", "cache for one hour")
    new = _add(mcp, db, "narwhal cache policy v2", "cache for one day")
    mcp["supersede_knowledge"](old_id=old, new_id=new)

    out = mcp["unsupersede_knowledge"](knowledge_id=old)
    assert "Unsuperseded knowledge" in out and f"#{old}" in out

    row = db.execute("SELECT status, superseded_by FROM knowledge WHERE id = ?",
                     (old,)).fetchone()
    assert row["status"] == "active" and row["superseded_by"] is None
    # Audit relation removed.
    assert db.execute(
        "SELECT COUNT(*) AS c FROM relations WHERE source_id = ? AND target_id = ?",
        (new, old)).fetchone()["c"] == 0
    # Back in default search.
    assert f"#{old}]" in mcp["search"](query="narwhal cache", scope="knowledge")


def test_refuses_self_supersede(env):
    mcp, db, _t = env
    k = _add(mcp, db, "sole finding", "text")
    out = mcp["supersede_knowledge"](old_id=k, new_id=k)
    assert "cannot supersede itself" in out
    assert db.execute("SELECT status FROM knowledge WHERE id = ?",
                      (k,)).fetchone()["status"] == "active"


def test_refuses_supersede_by_already_superseded(env):
    mcp, db, _t = env
    a = _add(mcp, db, "finding a", "a")
    b = _add(mcp, db, "finding b", "b")
    c = _add(mcp, db, "finding c", "c")
    mcp["supersede_knowledge"](old_id=b, new_id=c)  # b superseded by c
    # Now try to supersede a by the already-superseded b.
    out = mcp["supersede_knowledge"](old_id=a, new_id=b)
    assert "itself superseded" in out
    assert db.execute("SELECT status FROM knowledge WHERE id = ?",
                      (a,)).fetchone()["status"] == "active"


def test_not_found(env):
    mcp, db, _t = env
    k = _add(mcp, db, "real", "text")
    assert "NOT_FOUND" in mcp["supersede_knowledge"](old_id=k, new_id=999999)
    assert "NOT_FOUND" in mcp["supersede_knowledge"](old_id=999999, new_id=k)
    assert "NOT_FOUND" in mcp["unsupersede_knowledge"](knowledge_id=888888)


def test_unsupersede_noop_when_active(env):
    mcp, db, _t = env
    k = _add(mcp, db, "active finding", "text")
    out = mcp["unsupersede_knowledge"](knowledge_id=k)
    assert "not superseded" in out


def test_superseded_excluded_from_dedup(env):
    mcp, db, _t = env
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    from mcm_engine.backends import KnowledgeRow
    # Insert two identical rows directly — add_knowledge would suppress the
    # duplicate, and we specifically need two live near-duplicates here.
    s = SqliteStorage(db=db)
    text = ("The reserved lock is taken during the WAL checkpoint while a "
            "concurrent writer holds the shared cache in the busy handler loop.")
    a = s.insert_knowledge(KnowledgeRow(id=0, topic="lock contention", summary=text))
    b = s.insert_knowledge(KnowledgeRow(id=0, topic="lock contention", summary=text))
    # Both live -> they cluster.
    assert f"#{a}" in mcp["find_duplicate_entries"]("knowledge", 0.9)
    # Supersede one -> it is hidden from the hygiene sweep, so no cluster.
    mcp["supersede_knowledge"](old_id=a, new_id=b)
    out = mcp["find_duplicate_entries"]("knowledge", 0.9)
    assert "No near-duplicate knowledge entries found." in out
