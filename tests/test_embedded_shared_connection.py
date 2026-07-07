"""Embedded-SQLite adapters must share ONE connection (issue #79).

`build_context` used to instantiate storage/counters/search independently, so
each opened its OWN SQLite connection to the same file. Separate connections to
one file serialize their writes via `busy_timeout` and SELF-CONTEND: a
post-`search` counter bump (a write on the counters connection) blocks up to 5s
waiting on the sibling storage/search connections, stacking into the ~20s search
stall the issue reported.

The gap that let it ship: every tool-level test wired via the single-`db`
`coerce_context` legacy path, which already shares one connection — so nothing
exercised the config-driven `build_context` composition root the daemon uses.
These tests close that gap by asserting the sharing invariant AND driving a real
`search` through a `build_context`-wired Context.
"""
from __future__ import annotations

import sqlite3

import pytest

from mcm_engine.backends import EntityType
from mcm_engine.config import MCMConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker
from mcm_engine.wiring import build_context, build_verified_context


def _embedded_config(db_path) -> MCMConfig:
    """A default (embedded) config with every SQLite axis pointed at one file."""
    config = MCMConfig(project_name="t", db_path=str(db_path))
    for opts in (config.backends.storage_options,
                 config.backends.counters_options,
                 config.backends.search_options):
        opts["db_path"] = str(db_path)
    return config


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


# --- the sharing invariant (the direct regression guard) -------------------


def test_embedded_adapters_share_one_connection(tmp_path):
    ctx = build_context(_embedded_config(tmp_path / "k.db"))
    assert ctx.storage._db is ctx.counters._db
    assert ctx.storage._db is ctx.search._db
    # one KnowledgeDB => one sqlite connection => one writer, no self-contention.
    assert ctx.storage._db.conn is ctx.counters._db.conn is ctx.search._db.conn


def test_verified_context_shares_one_connection(tmp_path):
    ctx = build_verified_context(_embedded_config(tmp_path / "k.db"))
    assert ctx.storage._db is ctx.counters._db is ctx.search._db


def test_shared_db_is_reused_when_passed(tmp_path):
    """The daemon hands its plugin connection to build_context; the embedded
    adapters must reuse that exact instance, not open a fourth connection."""
    db_path = tmp_path / "k.db"
    db = KnowledgeDB(str(db_path))
    ctx = build_context(_embedded_config(db_path), shared_db=db)
    assert ctx.storage._db is db
    assert ctx.counters._db is db
    assert ctx.search._db is db


def test_distinct_files_get_distinct_connections(tmp_path):
    """Sharing is keyed by resolved file path — different files must NOT be
    forced onto one connection."""
    config = MCMConfig(project_name="t", db_path=str(tmp_path / "a.db"))
    config.backends.storage_options["db_path"] = str(tmp_path / "a.db")
    config.backends.counters_options["db_path"] = str(tmp_path / "b.db")
    config.backends.search_options["db_path"] = str(tmp_path / "a.db")
    ctx = build_context(config)
    assert ctx.storage._db is ctx.search._db            # same file -> shared
    assert ctx.counters._db is not ctx.storage._db      # other file -> separate


def test_memory_dbs_are_not_shared(tmp_path):
    """Each ``:memory:`` is a distinct database — sharing must skip it, so the
    default-empty-options case keeps today's per-adapter isolation."""
    ctx = build_context(MCMConfig(project_name="t"))
    assert ctx.storage._db is not ctx.counters._db
    assert ctx.storage._db is not ctx.search._db


# --- the failure mode this prevents (canary) -------------------------------


def test_separate_connections_to_one_file_contend(tmp_path):
    """Documents WHY sharing matters: two connections to one SQLite file
    serialize writes, and the second stalls-then-fails under `busy_timeout`.
    That is exactly the wait a post-search counter bump used to hit against a
    sibling adapter's connection. If the sharing invariant above ever regresses,
    the search path would reintroduce this stall."""
    path = str(tmp_path / "k.db")
    a = KnowledgeDB(path)
    migrate_core(a)
    b = KnowledgeDB(path)
    b.conn.execute("PRAGMA busy_timeout=100")  # bound the wait so the test is fast
    a.conn.execute("BEGIN IMMEDIATE")          # a takes the write lock
    try:
        with pytest.raises(sqlite3.OperationalError, match="lock"):
            b.conn.execute("BEGIN IMMEDIATE")  # b waits 100ms, then "database is locked"
    finally:
        a.conn.rollback()


# --- end-to-end: a real search through the composition root ----------------


def test_search_over_build_context_bumps_hitcount(tmp_path):
    """The scenario from #79: a `search` wired through `build_context` must
    return results AND land its post-search hit-count bump — proving the bump
    write went through the shared connection instead of stalling/being swallowed
    on a sibling-connection lock."""
    db_path = tmp_path / "k.db"
    migrate_core(KnowledgeDB(str(db_path)))
    ctx = build_context(_embedded_config(db_path))
    assert ctx.storage._db is ctx.counters._db is ctx.search._db

    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000,
        mandatory_stop_turns=1000, rules_check_interval=0, periodic_tools={}))
    mcp = FakeMCP()
    search_all = register_search_tools(mcp, ctx, tracker, [])
    register_knowledge_tools(mcp, ctx, tracker, "t", search_all)

    mcp["add_knowledge"](topic="stork-artifact-promotion",
                         summary="dirty/clean promotion via codeartifact")
    out = mcp["search"](query="stork-artifact-promotion")
    assert "stork-artifact-promotion" in out

    row = ctx.storage.find_knowledge_by_topic_kind("stork-artifact-promotion", "finding")
    assert row is not None
    snap = ctx.counters.get(EntityType.KNOWLEDGE, row.id)
    assert snap.get("hit_count", 0) >= 1
