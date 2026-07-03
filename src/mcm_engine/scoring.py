"""Composite search rank — scale-free additive-hybrid (issues #25 / #27).

#25 introduced the additive-hybrid weighted sum but normalized the lexical
relevance with a fixed-scale sigmoid whose midpoint was calibrated for SQLite
FTS5 bm25 (magnitudes ~1-20). The Postgres adapter scores with ts_rank_cd on a
much smaller scale (~0.1-1), so that sigmoid collapsed relevance to ~0 there and
counters dominated. #27 replaces the fixed sigmoid with BATCH MIN-MAX relative
normalization applied in the search layer:

    relevance = (raw_rank - batch_min) / (batch_max - batch_min)

which is scale-free — identical behavior for bm25, ts_rank_cd, or any future
adapter, with zero per-scale tuning. `compose_rank` therefore receives a
pre-normalized `relevance` in [0,1]; tools/search.py computes it across each
query's candidate batch.

    composite = W_RELEVANCE     * relevance                    # [0,1], batch min-max
              + W_HIT           * saturate(hit_count)          # x/(x+k), [0,1)
              + W_REINFORCEMENT * saturate(reinforcement_count)
              + W_CORRECTNESS   * tanh(net_outcomes / scale)   # signed, [-1,1]
              + W_RECENCY       * recency_bonus(age_days)      # linear, [0,1]
              + PINNED_WEIGHT   * (1 if pinned else 0)

W_RELEVANCE > the summed other non-pinned weights, so a top-of-batch match is
never out-voted by counter noise. Correctness (issue #21) is signed: a failing
rule is demoted (not banned), an untested / net-zero rule is neutral. Sign
convention unchanged: higher = better.
"""
from __future__ import annotations

import math
from typing import Optional

# --- weights (on the NORMALIZED [0,1]/[-1,1] terms) -------------------------
# Relevance must exceed the sum of the other non-pinned weights (0.1+0.3+0.5+0.3
# = 1.2) so a top-of-batch match is never swamped by maxed-out counters.
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


def minmax_normalize(value: float, lo: float, hi: float) -> float:
    """Scale a raw lexical rank to [0,1] relative to the candidate batch.

    Scale-free: works identically for bm25 (~1-20) or ts_rank_cd (~0.1-1) since
    it uses the batch's own min/max. A degenerate batch (hi <= lo: a single hit,
    or all-equal scores) returns 1.0 — uniform, so the other signals break the
    tie. Clamped to [0,1] for safety.
    """
    if hi <= lo:
        return 1.0
    v = (float(value) - lo) / (hi - lo)
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


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
    relevance: float,
    hit_count: Optional[int],
    reinforcement_count: Optional[int],
    pinned: bool,
    age_days: Optional[float],
    correct_count: Optional[int] = None,
    incorrect_count: Optional[int] = None,
) -> float:
    """Additive-hybrid composite for knowledge + rules entities.

    `relevance` is a pre-normalized [0,1] lexical score (batch min-max, computed
    by the search layer — see `minmax_normalize`). `correct_count`/
    `incorrect_count` (issue #21) default to None so non-rule callers are
    unchanged.
    """
    base = (
        W_RELEVANCE * float(relevance)
        + W_HIT * _saturate(hit_count, HIT_SATURATION)
        + W_REINFORCEMENT * _saturate(reinforcement_count, REINFORCEMENT_SATURATION)
        + W_CORRECTNESS * _correctness_term(correct_count, incorrect_count)
        + W_RECENCY * recency_bonus(age_days)
    )
    return base + (PINNED_WEIGHT if pinned else 0.0)


def compose_rank_pinned_only(
    *,
    relevance: float,
    pinned: bool,
) -> float:
    """Reduced composite for negative + errors entities, which only track
    `pinned` (no counters). `relevance` is the same batch-min-max [0,1] value."""
    return W_RELEVANCE * float(relevance) + (PINNED_WEIGHT if pinned else 0.0)
