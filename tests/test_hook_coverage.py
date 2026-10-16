"""Hook-side coverage helper (issues #157/#158/#159).

Residency mirrors the server ledger (#156): a topic is resident only if it was
covered after the last compaction boundary. Stale topics (older than the TTL)
and non-resident topics do not count as covered under the default check.
"""
from __future__ import annotations

from mcm_engine.hooks import coverage


def test_record_and_is_covered():
    e = {}
    coverage.record_topic(e, "Healthcheck", now=100.0)
    assert e["covered_topics"] == {"healthcheck": 100.0}
    assert coverage.is_covered(e, "healthcheck", now=101.0) is True
    assert coverage.is_covered(e, "HEALTHCHECK", now=101.0) is True  # normalized
    assert coverage.is_covered(e, "missing", now=101.0) is False


def test_record_empty_topic_is_noop():
    e = {}
    coverage.record_topic(e, "", now=100.0)
    coverage.record_topic(e, "   ", now=100.0)
    assert e.get("covered_topics", {}) == {}


def test_stale_topic_not_covered():
    e = {}
    coverage.record_topic(e, "t", now=0.0)
    assert coverage.is_covered(e, "t", now=coverage.COVERAGE_TTL_SECONDS + 1) is False
    assert coverage.is_covered(e, "t", now=coverage.COVERAGE_TTL_SECONDS - 1) is True


def test_compaction_makes_prior_coverage_non_resident():
    e = {}
    coverage.record_topic(e, "t", now=100.0)
    coverage.note_compaction(e, now=200.0)
    # covered before the boundary: not resident...
    assert coverage.is_covered(e, "t", now=201.0) is False
    # ...but still covered if residency is not required.
    assert coverage.is_covered(e, "t", now=201.0, require_resident=False) is True
    # re-covered after the boundary: resident again.
    coverage.record_topic(e, "t", now=300.0)
    assert coverage.is_covered(e, "t", now=301.0) is True


def test_prune_drops_stale_topics():
    e = {}
    coverage.record_topic(e, "old", now=0.0)
    coverage.record_topic(e, "fresh", now=coverage.COVERAGE_TTL_SECONDS)
    coverage.prune_topics(e, now=coverage.COVERAGE_TTL_SECONDS + 1)
    assert "old" not in e["covered_topics"]
    assert "fresh" in e["covered_topics"]


def test_prune_noop_without_topics():
    e = {}
    coverage.prune_topics(e, now=1.0)  # must not raise
    assert e.get("covered_topics") in (None, {})
