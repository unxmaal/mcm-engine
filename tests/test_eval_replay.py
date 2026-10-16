"""Replay evaluation of retrieval-trigger policies (issue #160).

Scores candidate policies by asymmetric FN/FP cost against a proxy label. Replay
ranks TRIGGER decisions only; it cannot judge post-retrieval behavior (that is
the live paired eval). These tests pin the label logic, each policy's firing,
and the weighted scorer.
"""
from __future__ import annotations

from mcm_engine.eval.replay import (
    Combined,
    FixedInterval,
    LIVE_PAIRED_EVAL_PLAN,
    RiskBased,
    TraceEvent,
    default_policies,
    evaluate,
    score_policy,
    should_retrieve,
)


def _ev(seq, **kw):
    return TraceEvent(seq=seq, tool=kw.pop("tool", "Edit"), **kw)


# --- proxy label ------------------------------------------------------------


def test_label_consequential_with_kb_hit_is_positive():
    assert should_retrieve(_ev(0, consequential=True, topic="a", kb_has_hit=True)) is True


def test_label_failure_is_positive_regardless_of_oracle():
    assert should_retrieve(_ev(0, consequential=True, topic="a", led_to_failure=True)) is True


def test_label_consequential_no_kb_hit_is_negative():
    assert should_retrieve(_ev(0, consequential=True, topic="a", kb_has_hit=False)) is False


def test_label_unknown_oracle_no_failure_is_unlabeled():
    assert should_retrieve(_ev(0, consequential=True, topic="a")) is None


def test_label_non_consequential_is_negative():
    assert should_retrieve(_ev(0, consequential=False)) is False


def test_label_lookup_itself_is_negative():
    assert should_retrieve(_ev(0, looked_up=True, topic="a")) is False


# --- policies ---------------------------------------------------------------


def test_fixed_interval_fires_every_n():
    p = FixedInterval(3)
    p.reset()
    fires = [p.step(_ev(i, consequential=True, topic=str(i))) for i in range(7)]
    # first n calls build up, then it fires and resets.
    assert fires == [False, False, False, True, False, False, True]


def test_fixed_interval_reset_on_lookup():
    p = FixedInterval(3)
    p.reset()
    p.step(_ev(0)); p.step(_ev(1))
    p.step(_ev(2, looked_up=True, tool="mcp__x__search", topic="q"))
    # counter reset: next two do not fire
    assert p.step(_ev(3)) is False
    assert p.step(_ev(4)) is False


def test_risk_based_fires_on_uncovered_consequential_only():
    p = RiskBased()
    p.reset()
    assert p.step(_ev(0, consequential=True, topic="widget")) is True   # uncovered
    assert p.step(_ev(1, consequential=True, topic="widget")) is False  # now covered
    assert p.step(_ev(2, consequential=False, topic="x")) is False      # not consequential


def test_risk_based_lookup_covers_topic():
    p = RiskBased()
    p.reset()
    p.step(_ev(0, looked_up=True, topic="widget", tool="mcp__x__search"))
    assert p.step(_ev(1, consequential=True, topic="widget")) is False  # covered by lookup


def test_combined_fires_if_either():
    p = Combined(3)
    p.reset()
    # a consequential uncovered op fires via the risk arm immediately
    assert p.step(_ev(0, consequential=True, topic="a")) is True


# --- scorer -----------------------------------------------------------------


def test_score_counts_tp_fn_fp_with_weights():
    trace = [
        _ev(0, consequential=True, topic="a", kb_has_hit=True),   # should: a lookup
        _ev(1, consequential=True, topic="b", led_to_failure=True),  # should: before a failure
        _ev(2, consequential=False, topic="c"),                    # should not
    ]
    # A policy that always fires: TP on 0 and 1, FP on 2.
    class AlwaysFire:
        name = "always"
        def reset(self): pass
        def step(self, ev): return True

    sc = score_policy(trace, AlwaysFire(), fn_weight=5.0, fp_weight=1.0)
    assert sc.tp == 2 and sc.fn == 0 and sc.fp == 1
    assert sc.fp_cost == 1.0


def test_fn_cost_weights_failure_heavier():
    trace = [
        _ev(0, consequential=True, topic="a", kb_has_hit=True),       # plain positive
        _ev(1, consequential=True, topic="b", led_to_failure=True),   # failure positive
    ]
    class NeverFire:
        name = "never"
        def reset(self): pass
        def step(self, ev): return False

    sc = score_policy(trace, NeverFire(), fn_weight=5.0, failure_multiplier=2.0)
    assert sc.fn == 2
    # 5 (plain) + 5*2 (failure) = 15
    assert sc.fn_cost == 15.0
    assert sc.recall == 0.0


def test_unlabeled_events_excluded_from_scoring():
    trace = [_ev(0, consequential=True, topic="a")]  # unknown oracle, no failure
    class AlwaysFire:
        name = "x"
        def reset(self): pass
        def step(self, ev): return True
    sc = score_policy(trace, AlwaysFire())
    assert sc.tp == 0 and sc.fn == 0 and sc.fp == 0  # nothing scored


def test_evaluate_runs_all_default_policies():
    trace = [
        _ev(0, consequential=True, topic="a", kb_has_hit=True),
        _ev(1, consequential=True, topic="a", led_to_failure=True),
    ]
    scores = evaluate(trace, default_policies())
    assert {s.name for s in scores} == {"fixed-interval", "risk-based", "combined"}
    for s in scores:
        d = s.as_dict()
        assert "weighted_cost" in d and "recall" in d


def test_risk_based_beats_fixed_on_a_consequential_heavy_trace():
    # Every op is a fresh consequential topic with a KB hit: risk-based fires on
    # each (high recall), the fixed interval only every 8th (misses most).
    trace = [_ev(i, consequential=True, topic=f"t{i}", kb_has_hit=True) for i in range(16)]
    scores = {s.name: s for s in evaluate(trace, default_policies())}
    assert scores["risk-based"].weighted_cost < scores["fixed-interval"].weighted_cost


def test_live_eval_plan_present():
    assert "live paired" in LIVE_PAIRED_EVAL_PLAN.lower()
