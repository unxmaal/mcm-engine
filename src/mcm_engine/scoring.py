"""Composite search rank — extracted from SQL per MCM2-14.

The v1 mcm-engine carried this formula inside two SQL ORDER BY clauses
(tools/search.py:115-117 and :271-273), tying together FTS5's rank with
counter columns and recency. Splitting it out lets:

  * the formula be unit-tested in isolation;
  * SearchBackend adapters return raw lexical ranks without knowing
    about counters;
  * CounterStore live off-row without breaking ranking.

Formula (v1 baseline — match what the SQL did):

    composite = raw_rank
              + 0.1  * hit_count
              + 0.3  * reinforcement_count
              + 2.0  * (1 if pinned else 0)
              + recency_bonus(age_days)

    recency_bonus(d) = max(0, (30 - d) / 30)   # 1.0 fresh, 0.0 at 30d+

Sign convention: every score is "higher = better", regardless of which
adapter produced raw_rank. SQLite's FTS5 rank is negative-better; the
SqliteSearch adapter flips the sign before constructing SearchHit, so
this scorer receives a higher-better float from every adapter.
"""
from __future__ import annotations

from typing import Optional

# Weights from the v1 SQL composite. If we ever want to tune these, do
# it in one place — here — not in a dozen ORDER BY clauses.
HIT_WEIGHT = 0.1
REINFORCEMENT_WEIGHT = 0.3
# Issue #21: correctness (outcome-driven) is weighted above popularity. The
# term is net (correct - incorrect), so a rule that keeps failing is DEMOTED
# (a negative contribution) rather than hard-banned — ranking adjustment, not
# exclusion, per the "decay/exploration, never a hard ban" caveat.
CORRECTNESS_WEIGHT = 0.5
PINNED_WEIGHT = 2.0
RECENCY_WINDOW_DAYS = 30.0
RECENCY_MAX_BONUS = 1.0


def recency_bonus(age_days: Optional[float]) -> float:
    """Linear decay over RECENCY_WINDOW_DAYS, clamped at 0."""
    if age_days is None:
        return 0.0
    if age_days >= RECENCY_WINDOW_DAYS:
        return 0.0
    if age_days <= 0:
        return RECENCY_MAX_BONUS
    return (RECENCY_WINDOW_DAYS - age_days) / RECENCY_WINDOW_DAYS


def compose_rank(
    *,
    raw_rank: float,
    hit_count: Optional[int],
    reinforcement_count: Optional[int],
    pinned: bool,
    age_days: Optional[float],
    correct_count: Optional[int] = None,
    incorrect_count: Optional[int] = None,
) -> float:
    """The full composite score for knowledge + rules entities.

    `correct_count`/`incorrect_count` (issue #21) default to None so every
    existing caller is unchanged; rules pass them to fold outcome-driven
    correctness into ranking as a net (correct - incorrect) term.
    """
    hits = float(hit_count or 0)
    reinforcement = float(reinforcement_count or 0)
    correctness = float(correct_count or 0) - float(incorrect_count or 0)
    pinned_term = PINNED_WEIGHT if pinned else 0.0
    return (
        float(raw_rank)
        + HIT_WEIGHT * hits
        + REINFORCEMENT_WEIGHT * reinforcement
        + CORRECTNESS_WEIGHT * correctness
        + pinned_term
        + recency_bonus(age_days)
    )


def compose_rank_pinned_only(
    *,
    raw_rank: float,
    pinned: bool,
) -> float:
    """Reduced composite for negative + errors entities, which only track
    `pinned` (no hit_count, no reinforcement, no last_hit_at)."""
    return float(raw_rank) + (PINNED_WEIGHT if pinned else 0.0)
