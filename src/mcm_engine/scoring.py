"""Composite search rank — additive-hybrid reformulation (issue #25).

The pre-#25 formula added *raw* counter values to the lexical rank:

    composite = raw_rank + 0.1*hit + 0.3*reinforcement + 0.5*(correct-incorrect)
              + 2.0*pinned + recency_bonus

which let an unbounded counter (hit_count=100 -> +10) swamp the relevance
signal. This reformulation normalizes every signal to a bounded range and
takes a weighted sum, with relevance weighted above the *summed* other weights
so a strong lexical match cannot be out-voted by counter noise:

    composite = W_RELEVANCE     * relevance(raw_rank, query_terms)   # sigmoid, [0,1]
              + W_HIT           * saturate(hit_count)                # x/(x+k), [0,1)
              + W_REINFORCEMENT * saturate(reinforcement_count)
              + W_CORRECTNESS   * tanh(net_outcomes / scale)         # signed, [-1,1]
              + W_RECENCY       * recency_bonus(age_days)            # linear, [0,1]
              + PINNED_WEIGHT   * (1 if pinned else 0)

Design notes:
- relevance uses a query-length-adaptive logistic sigmoid over the sign-flipped
  (higher=better) bm25 rank — the Mem0 `normalize_bm25` shape. `query_terms`
  defaults to None (fixed params) so callers that don't have the query still work.
- correctness (issue #21) is SIGNED via tanh(net): a failing rule is demoted
  (negative contribution) not banned, and an untested rule / net-zero rule is
  neutral (0). "Decay/exploration, never a hard ban."
- Sign convention unchanged: higher = better (SqliteSearch flips FTS5's
  negative-better bm25 before this scorer sees it).

TUNING CAVEAT: the bm25 sigmoid midpoints/steepness assume a bm25 magnitude
scale; they're isolated constants below and should be validated against real
query bm25 distributions (or swapped for batch min-max) before heavy reliance.
"""
from __future__ import annotations

import math
from typing import Optional

# --- weights (on the NORMALIZED [0,1]/[-1,1] terms) -------------------------
# Relevance must exceed the sum of the other non-pinned weights (0.1+0.3+0.5+0.3
# = 1.2) so a top lexical match is never swamped by maxed-out counters.
W_RELEVANCE = 2.0
W_HIT = 0.1
W_REINFORCEMENT = 0.3
W_CORRECTNESS = 0.5
W_RECENCY = 0.3
PINNED_WEIGHT = 2.0

# --- normalization tunables -------------------------------------------------
HIT_SATURATION = 10.0          # hit_count == HIT_SATURATION -> 0.5 contribution
REINFORCEMENT_SATURATION = 3.0  # reinforcement saturates faster (stronger per unit)
CORRECTNESS_SCALE = 2.0        # net outcomes -> tanh(net/scale)
RECENCY_WINDOW_DAYS = 30.0
RECENCY_MAX_BONUS = 1.0

# bm25 sigmoid params, query-length-adaptive (Mem0-style). Fewer query terms ->
# lower midpoint (a short query's top bm25 is smaller).
_BM25_DEFAULT = (8.0, 0.6)     # (midpoint, steepness) when query length unknown


def _bm25_params(query_terms: Optional[int]) -> tuple[float, float]:
    if not query_terms:
        return _BM25_DEFAULT
    if query_terms <= 3:
        return (5.0, 0.7)
    if query_terms <= 15:
        return (8.0, 0.6)
    return (12.0, 0.5)


def _sigmoid(x: float, midpoint: float, steepness: float) -> float:
    z = -steepness * (x - midpoint)
    if z >= 60:      # avoid overflow; sigmoid -> 0
        return 0.0
    if z <= -60:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def normalize_relevance(raw_rank: float, query_terms: Optional[int] = None) -> float:
    """Sign-flipped bm25 rank -> [0,1] via a query-length-adaptive sigmoid.
    Monotonically increasing in raw_rank."""
    midpoint, steepness = _bm25_params(query_terms)
    return _sigmoid(float(raw_rank), midpoint, steepness)


def _saturate(count: Optional[int], k: float) -> float:
    """Unbounded non-negative counter -> [0,1) with diminishing returns."""
    x = max(0.0, float(count or 0))
    return x / (x + k)


def _correctness_term(correct_count: Optional[int], incorrect_count: Optional[int]) -> float:
    """Signed correctness in [-1,1]: 0 for no data or net-zero, positive when
    outcomes net-pass, negative when they net-fail."""
    net = float(correct_count or 0) - float(incorrect_count or 0)
    if net == 0.0:
        return 0.0
    return math.tanh(net / CORRECTNESS_SCALE)


def recency_bonus(age_days: Optional[float]) -> float:
    """Linear decay over RECENCY_WINDOW_DAYS, clamped at 0. Unchanged from v1."""
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
    query_terms: Optional[int] = None,
) -> float:
    """Additive-hybrid composite for knowledge + rules entities.

    `correct_count`/`incorrect_count` (issue #21) and `query_terms` (issue #25)
    default to None so existing callers are unchanged; rules pass correctness to
    fold outcome-driven trust into ranking, and callers with the query pass its
    term count for the length-adaptive relevance sigmoid.
    """
    base = (
        W_RELEVANCE * normalize_relevance(raw_rank, query_terms)
        + W_HIT * _saturate(hit_count, HIT_SATURATION)
        + W_REINFORCEMENT * _saturate(reinforcement_count, REINFORCEMENT_SATURATION)
        + W_CORRECTNESS * _correctness_term(correct_count, incorrect_count)
        + W_RECENCY * recency_bonus(age_days)
    )
    return base + (PINNED_WEIGHT if pinned else 0.0)


def compose_rank_pinned_only(
    *,
    raw_rank: float,
    pinned: bool,
    query_terms: Optional[int] = None,
) -> float:
    """Reduced composite for negative + errors entities, which only track
    `pinned` (no counters). Uses the same relevance normalization so lexical
    scale is consistent with `compose_rank`."""
    return W_RELEVANCE * normalize_relevance(raw_rank, query_terms) + (
        PINNED_WEIGHT if pinned else 0.0
    )
