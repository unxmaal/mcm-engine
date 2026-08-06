"""Postgres search query_mode: strict (default) vs natural (#107).

strict AND-matches every lexeme (today's behavior). natural retries OR-ranked
ONLY when strict returns nothing, so a natural-language phrase returns its best
rows instead of nothing — while a query whose terms all hit stays precise.
Postgres-gated; skips cleanly without a DB.
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from mcm_engine.adapters.postgres.search import (  # noqa: E402
    PostgresSearch, _or_tsquery_text,
)
from mcm_engine.adapters.postgres.storage import PostgresStorage  # noqa: E402
from mcm_engine.backends import EntityType, KnowledgeRow  # noqa: E402

DSN = os.environ.get("MCM_TEST_POSTGRES_DSN",
                     "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test")


def _pg_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="no postgres at DSN")


def _seed():
    s = PostgresStorage(DSN)
    s.ensure_schema()
    with s.transaction():
        with s._conn.cursor() as cur:
            cur.execute("DELETE FROM knowledge")
    # One row that matches 4 of the 5 phrase terms (no 'saml').
    kid = s.insert_knowledge(KnowledgeRow(
        id=0, topic="token portal",
        summary="the token portal authenticates to entra id via oauth"))
    return kid


# ---- helper (pure) --------------------------------------------------------

def test_or_tsquery_text_builds_or_string():
    assert _or_tsquery_text("token portal entra") == "token | portal | entra"


def test_or_tsquery_text_strips_operators_and_junk():
    assert _or_tsquery_text("c++  &&  entra-id!") == "c | entra | id"


def test_or_tsquery_text_empty_on_no_words():
    assert _or_tsquery_text("  --- &&& ") == ""


# ---- strict mode (default) ------------------------------------------------

def test_strict_phrase_with_missing_term_returns_nothing():
    kid = _seed()
    strict = PostgresSearch(DSN)  # default query_mode='strict'
    hits = strict.search("token portal authenticate entra saml",
                         entity_types={EntityType.KNOWLEDGE})
    assert all(h.entity_id != kid for h in hits)  # 'saml' absent -> AND yields nothing


def test_strict_all_terms_present_matches():
    kid = _seed()
    strict = PostgresSearch(DSN)
    hits = strict.search("token portal entra",
                         entity_types={EntityType.KNOWLEDGE})
    assert any(h.entity_id == kid for h in hits)


# ---- natural mode ---------------------------------------------------------

def test_natural_phrase_with_missing_term_returns_best_row():
    kid = _seed()
    natural = PostgresSearch(DSN, query_mode="natural")
    hits = natural.search("how does the token portal authenticate to entra saml",
                          entity_types={EntityType.KNOWLEDGE})
    assert any(h.entity_id == kid for h in hits), "natural mode should OR-recall the row"


def test_natural_precise_query_still_matches():
    kid = _seed()
    natural = PostgresSearch(DSN, query_mode="natural")
    hits = natural.search("token portal entra",
                          entity_types={EntityType.KNOWLEDGE})
    assert any(h.entity_id == kid for h in hits)


def test_unknown_mode_coerces_to_strict():
    kid = _seed()
    coerced = PostgresSearch(DSN, query_mode="banana")
    assert coerced._query_mode == "strict"
    # Behaves strict: the missing-term phrase yields nothing.
    hits = coerced.search("token portal authenticate entra saml",
                          entity_types={EntityType.KNOWLEDGE})
    assert all(h.entity_id != kid for h in hits)
