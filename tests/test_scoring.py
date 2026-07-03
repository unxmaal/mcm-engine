"""Composite ranking scorer — additive-hybrid reformulation (issue #25).

Every signal is normalized to a bounded range before a weighted sum, so no
unbounded counter can swamp the lexical relevance term. (The pre-#25 formula
added *raw* counts to raw_rank, letting hit_count=100 dominate a strong text
match — the bug this reformulation fixes.)

- relevance: query-length-adaptive sigmoid over the sign-flipped (higher=better)
  bm25 rank -> [0,1], and W_RELEVANCE > sum of the other weights so a strong
  lexical match cannot be out-voted by counter noise.
- hit / reinforcement: saturating (x/(x+k)) -> [0,1), diminishing returns.
- correctness: signed tanh(net/scale) -> [-1,1]; 0 for no data or net-zero, so
  a failing rule is *demoted* (not banned) and an untested rule is neutral.
- recency: linear over 30 days, unchanged.
- pinned: a flat additive boost on top.

Sort convention unchanged: higher = better.
"""
from __future__ import annotations

import pytest

from mcm_engine.scoring import (
    PINNED_WEIGHT,
    W_RELEVANCE,
    compose_rank,
    compose_rank_pinned_only,
    normalize_relevance,
    recency_bonus,
)


# --- relevance normalization ------------------------------------------------

@pytest.mark.parametrize("raw", [-50.0, -1.0, 0.0, 1.0, 8.0, 50.0, 1000.0])
def test_relevance_is_bounded_0_1(raw):
    assert 0.0 <= normalize_relevance(raw) <= 1.0


def test_relevance_monotonic_increasing_in_raw_rank():
    xs = [-10.0, 0.0, 5.0, 8.0, 12.0, 30.0]
    vs = [normalize_relevance(x) for x in xs]
    assert all(a < b for a, b in zip(vs, vs[1:]))


def test_relevance_query_length_adaptive():
    """Query term count shifts the sigmoid, so the same raw rank normalizes
    differently for a 2-term vs a 20-term query (Mem0-style)."""
    r = 6.0
    assert normalize_relevance(r, query_terms=2) != normalize_relevance(r, query_terms=20)


# --- the load-bearing invariant: relevance is not swamped by counters -------

def test_strong_match_beats_weak_but_maximally_popular():
    strong = compose_rank(
        raw_rank=20.0, hit_count=0, reinforcement_count=0,
        pinned=False, age_days=999.0, query_terms=4,
    )
    weak_but_popular = compose_rank(
        raw_rank=-5.0, hit_count=1000, reinforcement_count=1000,
        pinned=False, age_days=0.0, correct_count=1000, incorrect_count=0,
        query_terms=4,
    )
    assert strong > weak_but_popular


# --- monotonicity + saturation of counter terms -----------------------------

def test_more_hits_never_lowers_score():
    base = dict(raw_rank=5.0, reinforcement_count=0, pinned=False, age_days=10.0)
    assert compose_rank(hit_count=100, **base) >= compose_rank(hit_count=1, **base)


def test_reinforcement_saturates_but_increases_and_is_bounded():
    base = dict(raw_rank=5.0, hit_count=0, pinned=False, age_days=10.0)
    a = compose_rank(reinforcement_count=1, **base)
    b = compose_rank(reinforcement_count=5, **base)
    c = compose_rank(reinforcement_count=1000, **base)
    zero = compose_rank(reinforcement_count=0, **base)
    assert a < b < c
    assert (c - zero) < 0.31  # bounded by ~W_REINFORCEMENT (0.3)


# --- correctness is signed: promote / demote / neutral ----------------------

def test_positive_outcomes_promote_and_negative_demote():
    base = dict(raw_rank=5.0, hit_count=0, reinforcement_count=0,
                pinned=False, age_days=10.0)
    neutral = compose_rank(**base)
    good = compose_rank(correct_count=5, incorrect_count=0, **base)
    bad = compose_rank(correct_count=0, incorrect_count=5, **base)
    assert bad < neutral < good


def test_no_outcome_data_and_net_zero_are_both_neutral():
    base = dict(raw_rank=5.0, hit_count=0, reinforcement_count=0,
                pinned=False, age_days=10.0)
    assert compose_rank(**base) == pytest.approx(
        compose_rank(correct_count=3, incorrect_count=3, **base)
    )


# --- pinned is a flat additive boost ---------------------------------------

def test_pinned_adds_pinned_weight_holding_signals_equal():
    kw = dict(raw_rank=5.0, hit_count=3, reinforcement_count=1, age_days=10.0,
              correct_count=2, incorrect_count=0, query_terms=4)
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
    v = compose_rank(raw_rank=5.0, hit_count=None, reinforcement_count=None,
                     pinned=False, age_days=None)
    assert isinstance(v, float)


def test_pinned_only_uses_relevance_plus_pin():
    unpinned = compose_rank_pinned_only(raw_rank=5.0, pinned=False)
    pinned = compose_rank_pinned_only(raw_rank=5.0, pinned=True)
    assert pinned == pytest.approx(unpinned + PINNED_WEIGHT)
    assert unpinned == pytest.approx(W_RELEVANCE * normalize_relevance(5.0))
