"""Regression for cutover defect #7: _age_days choked on tz-aware
datetimes from Postgres TIMESTAMPTZ.

The original bug: SqliteStorage returns tz-naive datetimes (parsed via
datetime.fromisoformat from SQLite's TEXT storage), while PostgresStorage
returns tz-aware ones. ``_age_days`` did ``datetime.now() - ts`` —
which fails with "can't subtract offset-naive and offset-aware
datetimes" when the search axis is Postgres-backed.

Surfaced on the live cutover (Phase B, search query="cutover"
scope="rules") immediately after defects #5/#6 were fixed and the
engine was finally routing through Postgres.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcm_engine.tools.search import _age_days


def test_age_days_tz_naive():
    """Baseline: SqliteStorage-style tz-naive input still works."""
    ts = datetime.now()  # naive — same shape SQLite returns
    days = _age_days(ts)
    assert days is not None
    assert 0 <= days < 0.01


def test_age_days_tz_aware_utc():
    """PostgresStorage-style tz-aware (UTC) input must not raise."""
    ts = datetime.now(timezone.utc)
    days = _age_days(ts)
    assert days is not None
    assert 0 <= days < 0.01


def test_age_days_tz_aware_non_utc():
    """A tz-aware non-UTC timestamp also must not raise — covers
    non-UTC RDS instances and Meilisearch-style local times."""
    from datetime import timedelta

    pacific = timezone(timedelta(hours=-8))
    ts = datetime.now(pacific)
    days = _age_days(ts)
    assert days is not None
    assert 0 <= days < 0.01


def test_age_days_none_returns_none():
    assert _age_days(None) is None
