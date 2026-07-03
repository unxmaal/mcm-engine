"""Composite ranking scorer — scale-free additive-hybrid (issues #25 / #27).

#25 introduced the additive-hybrid weighted sum but normalized lexical
relevance with a fixed-scale sigmoid tuned for SQLite bm25; on the Postgres
adapter (ts_rank_cd, a much smaller scale) that sigmoid collapsed relevance to
~0. #27 makes relevance scale-free by BATCH MIN-MAX normalization in the search
layer, so `compose_rank` now receives a pre-normalized `relevance` in [0,1].

Invariants under test:
- minmax_normalize maps a batch to [0,1]; a degenerate batch -> 1.0.
- relevance dominates: W_RELEVANCE > summed other weights, so a top-of-batch
  match is never out-voted by maxed counters.
- counters saturate; correctness is signed (promote/demote/neutral); pinned is a
  flat additive boost; recency is linear.
"""
from __future__ import annotations

import pytest

from mcm_engine.scoring import (
    PINNED_WEIGHT,
    W_RELEVANCE,
    compose_rank,
    compose_rank_pinned_only,
    minmax_normalize,
    recency_bonus,
)


# --- batch min-max relevance normalization ---------------------------------

@pytest.mark.parametrize("value,lo,hi,expected", [
    (5.0, 0.0, 10.0, 0.5),
    (0.0, 0.0, 10.0, 0.0),
    (10.0, 0.0, 10.0, 1.0),
    (0.7, 0.2, 1.2, 0.5),     # works on a ts_rank_cd-like small scale
    (12.0, 2.0, 22.0, 0.5),   # ... and a bm25-like large scale — same result
])
def test_minmax_normalize_scale_free(value, lo, hi, expected):
    assert minmax_normalize(value, lo, hi) == pytest.approx(expected)


def test_minmax_degenerate_batch_is_uniform():
    # one hit, or all-equal scores -> hi <= lo -> uniform 1.0 (other signals
    # break the tie).
    assert minmax_normalize(5.0, 5.0, 5.0) == pytest.approx(1.0)
    assert minmax_normalize(3.0, 9.0, 2.0) == pytest.approx(1.0)  # hi<lo guard


def test_minmax_clamps_out_of_range():
    assert minmax_normalize(-1.0, 0.0, 10.0) == 0.0
    assert minmax_normalize(99.0, 0.0, 10.0) == 1.0


# --- the load-bearing invariant: relevance is not swamped by counters -------

def test_top_of_batch_match_beats_weak_but_maximally_popular():
    strong = compose_rank(
        relevance=1.0, hit_count=0, reinforcement_count=0,
        pinned=False, age_days=999.0,
    )
    weak_but_popular = compose_rank(
        relevance=0.0, hit_count=1000, reinforcement_count=1000,
        pinned=False, age_days=0.0, correct_count=1000, incorrect_count=0,
    )
    assert strong > weak_but_popular


def test_higher_relevance_ranks_higher_all_else_equal():
    kw = dict(hit_count=2, reinforcement_count=1, pinned=False, age_days=10.0)
    assert compose_rank(relevance=0.9, **kw) > compose_rank(relevance=0.2, **kw)


# --- monotonicity + saturation of counter terms -----------------------------

def test_more_hits_never_lowers_score():
    base = dict(relevance=0.5, reinforcement_count=0, pinned=False, age_days=10.0)
    assert compose_rank(hit_count=100, **base) >= compose_rank(hit_count=1, **base)


def test_reinforcement_saturates_but_increases_and_is_bounded():
    base = dict(relevance=0.5, hit_count=0, pinned=False, age_days=10.0)
    a = compose_rank(reinforcement_count=1, **base)
    b = compose_rank(reinforcement_count=5, **base)
    c = compose_rank(reinforcement_count=1000, **base)
    zero = compose_rank(reinforcement_count=0, **base)
    assert a < b < c
    assert (c - zero) < 0.31  # bounded by ~W_REINFORCEMENT (0.3)


# --- correctness is signed: promote / demote / neutral ----------------------

def test_positive_outcomes_promote_and_negative_demote():
    base = dict(relevance=0.5, hit_count=0, reinforcement_count=0,
                pinned=False, age_days=10.0)
    neutral = compose_rank(**base)
    good = compose_rank(correct_count=5, incorrect_count=0, **base)
    bad = compose_rank(correct_count=0, incorrect_count=5, **base)
    assert bad < neutral < good


def test_no_outcome_data_and_net_zero_are_both_neutral():
    base = dict(relevance=0.5, hit_count=0, reinforcement_count=0,
                pinned=False, age_days=10.0)
    assert compose_rank(**base) == pytest.approx(
        compose_rank(correct_count=3, incorrect_count=3, **base)
    )


# --- pinned is a flat additive boost ---------------------------------------

def test_pinned_adds_pinned_weight_holding_signals_equal():
    kw = dict(relevance=0.6, hit_count=3, reinforcement_count=1, age_days=10.0,
              correct_count=2, incorrect_count=0)
    assert compose_rank(pinned=True, **kw) == pytest.approx(
        compose_rank(pinned=False, **kw) + PINNED_WEIGHT
    )


# --- recency helper unchanged ----------------------------------------------

def test_recency_bonus_linear_and_clamped():
    assert recency_bonus(0.0) == pytest.approx(1.0)
    assert recency_bonus(15.0) == pytest.approx(0.5)
    assert recency_bonus(30.0) == pytest.approx(0.0)
    assert recency_bonus(120.0) == pytest.approx(0.0)
    assert recency_bonus(None) == 0.0


# --- robustness -------------------------------------------------------------

def test_accepts_none_counters():
    v = compose_rank(relevance=0.5, hit_count=None, reinforcement_count=None,
                     pinned=False, age_days=None)
    assert isinstance(v, float)


def test_pinned_only_uses_relevance_plus_pin():
    unpinned = compose_rank_pinned_only(relevance=0.5, pinned=False)
    pinned = compose_rank_pinned_only(relevance=0.5, pinned=True)
    assert pinned == pytest.approx(unpinned + PINNED_WEIGHT)
    assert unpinned == pytest.approx(W_RELEVANCE * 0.5)
