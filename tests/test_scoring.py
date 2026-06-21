"""MCM2-14: composite ranking scorer extracted from SQL.

The seam inventory flagged the composite rank expression in tools/search.py
as the load-bearing surface of the SearchBackend contract. Extracting it
into a Python function makes the formula testable in isolation and lets
adapters return raw bm25-or-equivalent ranks while the engine handles
the counters+recency+pinned composition.

The scorer takes:
- raw lexical rank (already sign-normalized: higher = better)
- counter snapshot from CounterStore.last_flushed_snapshot
- age in days
- pinned flag

And returns the composite score that the SearchBackend caller uses to
sort results.

Formula matches the v1 mcm-engine baseline:
    composite = raw_rank
              + 0.1 * hit_count
              + 0.3 * reinforcement_count
              + 2.0 * (1 if pinned else 0)
              + recency_bonus(age_days)
where recency_bonus(d) = max(0, (30 - d) / 30).
"""
from __future__ import annotations

import math

import pytest


def test_module_exposes_compose_rank():
    from mcm_engine.scoring import compose_rank

    assert callable(compose_rank)


def test_baseline_no_counters_returns_raw_rank():
    """With no counters and no pinned and old age, composite ≈ raw rank.

    Specifically: hit_count=0, reinforcement_count=0, pinned=False, age=999
    -> recency_bonus = 0, so composite == raw_rank.
    """
    from mcm_engine.scoring import compose_rank

    score = compose_rank(
        raw_rank=1.5,
        hit_count=0,
        reinforcement_count=0,
        pinned=False,
        age_days=999.0,
    )
    assert score == pytest.approx(1.5)


def test_hit_count_adds_weight():
    from mcm_engine.scoring import compose_rank

    base = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                        pinned=False, age_days=999.0)
    boosted = compose_rank(raw_rank=0, hit_count=10, reinforcement_count=0,
                           pinned=False, age_days=999.0)
    assert boosted == pytest.approx(base + 1.0)  # 10 * 0.1


def test_reinforcement_outweighs_hit_count():
    """Reinforcement weight (0.3) is 3x hit weight (0.1)."""
    from mcm_engine.scoring import compose_rank

    same_signal = compose_rank(raw_rank=0, hit_count=3, reinforcement_count=0,
                               pinned=False, age_days=999.0)
    via_reinforcement = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=1,
                                     pinned=False, age_days=999.0)
    # 3 * 0.1 == 1 * 0.3 — equal signal strength.
    assert same_signal == pytest.approx(via_reinforcement)


def test_pinned_adds_2_point_0():
    from mcm_engine.scoring import compose_rank

    unpinned = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                            pinned=False, age_days=999.0)
    pinned = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                          pinned=True, age_days=999.0)
    assert pinned == pytest.approx(unpinned + 2.0)


def test_recency_bonus_linear_within_30_days():
    """recency_bonus(0) = 1.0, recency_bonus(15) = 0.5, recency_bonus(30) = 0."""
    from mcm_engine.scoring import compose_rank

    fresh = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                         pinned=False, age_days=0.0)
    half = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                        pinned=False, age_days=15.0)
    edge = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                        pinned=False, age_days=30.0)
    assert fresh == pytest.approx(1.0)
    assert half == pytest.approx(0.5)
    assert edge == pytest.approx(0.0)


def test_recency_bonus_clamps_at_zero_after_30():
    from mcm_engine.scoring import compose_rank

    aged = compose_rank(raw_rank=0, hit_count=0, reinforcement_count=0,
                        pinned=False, age_days=120.0)
    assert aged == pytest.approx(0.0)


def test_reduced_shape_for_no_counter_entities():
    """negative + errors have only `pinned`, no hit_count or reinforcement.
    A `compose_rank_pinned_only` reflects the reduced rank expression in
    the SQL (search.py:182, 228)."""
    from mcm_engine.scoring import compose_rank_pinned_only

    raw = 1.0
    assert compose_rank_pinned_only(raw_rank=raw, pinned=False) == pytest.approx(1.0)
    assert compose_rank_pinned_only(raw_rank=raw, pinned=True) == pytest.approx(3.0)


def test_accepts_none_for_optional_counters():
    """A scorer call where counter values are unknown (e.g., LIKE
    fallback without rank-tracked counters) treats them as zero rather
    than erroring."""
    from mcm_engine.scoring import compose_rank

    score = compose_rank(
        raw_rank=0.5,
        hit_count=None,
        reinforcement_count=None,
        pinned=False,
        age_days=None,
    )
    # raw + 0 + 0 + 0 + 0 = 0.5
    assert score == pytest.approx(0.5)
