"""knowledge.valid_until (Phase 3, v16): forward-dated validity honored in
search as a soft [EXPIRED] tag + rank penalty, for both knowledge and rules.
Distinct from [STALE] (recency) and from supersession (hidden).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import (
    EntityType,
    KnowledgeRow,
    RuleRow,
    SearchHit,
    parse_valid_until,
)
from mcm_engine.tools.search import (
    _score_and_format_knowledge,
    _score_and_format_rule,
)

PAST = (datetime.now() - timedelta(days=2)).isoformat()
FUTURE = (datetime.now() + timedelta(days=365)).isoformat()


@pytest.fixture
def env(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "v.db"))
    s.ensure_schema()
    return s, SqliteCounters(db=s._db)


def _k(storage, topic, valid_until=None):
    return storage.insert_knowledge(KnowledgeRow(
        id=0, topic=topic, kind="finding", summary=f"{topic} body",
        valid_until=parse_valid_until(valid_until) if valid_until else None))


def _score_k(storage, counters, kid, include_archived=False):
    hit = SearchHit(entity_type=EntityType.KNOWLEDGE, entity_id=kid, score=1.0)
    return _score_and_format_knowledge(hit, storage, counters, project="",
                                       relevance=1.0, include_archived=include_archived)


# ---- storage round-trip ----

def test_valid_until_round_trip(env):
    s, _c = env
    kid = _k(s, "expires", valid_until=FUTURE)
    row = s.find_by_id(EntityType.KNOWLEDGE, kid)
    assert row.valid_until is not None
    assert row.valid_until.date() == (datetime.now() + timedelta(days=365)).date()
    # unset -> None (durable)
    kid2 = _k(s, "durable")
    assert s.find_by_id(EntityType.KNOWLEDGE, kid2).valid_until is None


# ---- search honoring ----

def test_expired_knowledge_tagged_and_deprioritized(env):
    s, c = env
    live = _k(s, "live")
    exp = _k(s, "expired", valid_until=PAST)
    live_score, live_fmt = _score_k(s, c, live)
    exp_score, exp_fmt = _score_k(s, c, exp)
    assert "[EXPIRED]" in exp_fmt and "[EXPIRED]" not in live_fmt
    # still returned, but sunk below the live hit
    assert exp_score < live_score


def test_future_valid_until_is_not_expired(env):
    s, c = env
    kid = _k(s, "future", valid_until=FUTURE)
    score, fmt = _score_k(s, c, kid)
    assert "[EXPIRED]" not in fmt


def test_superseded_still_dropped_no_double_tag(env):
    s, c = env
    old = _k(s, "old", valid_until=PAST)
    new = _k(s, "new")
    s.supersede_knowledge(old, new)   # old -> superseded, though also past-expiry
    # superseded drop wins; not surfaced at all by default
    assert _score_k(s, c, old) is None
    # with include_archived it surfaces, and since it's expired it carries the tag
    res = _score_k(s, c, old, include_archived=True)
    assert res is not None and "[EXPIRED]" in res[1]


# ---- rules: first honoring of the previously-inert rules.valid_until ----

def test_expired_rule_tagged_and_deprioritized(env):
    s, c = env
    live = s.insert_rule(RuleRow(id=0, title="LiveRule", keywords="k"))
    exp = s.insert_rule(RuleRow(id=0, title="ExpiredRule", keywords="k"))
    # forward-date the expiry via the set_rule_metadata write path
    s.set_rule_metadata(exp, valid_until=datetime.now() - timedelta(days=2))

    def score(rid):
        hit = SearchHit(entity_type=EntityType.RULE, entity_id=rid, score=1.0)
        return _score_and_format_rule(hit, s, c, include_archived=False, relevance=1.0)

    live_score, live_fmt = score(live)
    exp_score, exp_fmt = score(exp)
    assert "[EXPIRED]" in exp_fmt and "[EXPIRED]" not in live_fmt
    assert exp_score < live_score


# ---- parse helper ----

def test_parse_valid_until():
    assert parse_valid_until("") is None
    assert parse_valid_until("2026-12-31").year == 2026
    with pytest.raises(ValueError):
        parse_valid_until("not-a-date")


# ---- Postgres parity (gated) ----

DSN = os.environ.get("MCM_TEST_POSTGRES_DSN",
                     "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_available() -> bool:
    try:
        import psycopg
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pg = pytest.mark.skipif(not _pg_available(), reason="no postgres at DSN")


@pg
def test_postgres_valid_until_round_trip(tmp_path):
    from mcm_engine.adapters.postgres.storage import PostgresStorage
    s = PostgresStorage(DSN)
    s.ensure_schema()
    kid = s.insert_knowledge(KnowledgeRow(
        id=0, topic="pg-valid-until", kind="finding", summary="body",
        valid_until=parse_valid_until(FUTURE)))
    row = s.find_by_id(EntityType.KNOWLEDGE, kid)
    assert row.valid_until is not None
    assert row.valid_until.year == (datetime.now() + timedelta(days=365)).year
