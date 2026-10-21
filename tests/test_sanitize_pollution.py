"""Serialized-entry leakage cleanup (issue #142): the sanitizer re-splits a
polluted summary, the store-time guards reject it, and sanitize_knowledge cleans
existing rows in place.
"""
from __future__ import annotations

from mcm_engine.sanitize import (
    clean_error,
    clean_negative,
    clean_summary_detail,
    is_polluted,
    strip_tool_call_fragments,
)

# A summary as observed on #157: real text, then the serialized entry blob.
POLLUTED = (
    "Measured 2026-09-20 on localharness: two lanes had no source. "
    "permanently inert.</summary>\n"
    "<detail>Before: image reddit-sd-week. After: every lane reachable.</detail>\n"
    "<rationale>Asked to fix the music branch.</rationale>\n"
    "<tags>localharness,discovery</tags>\n"
    "<kind>finding</kind>\n"
    "<project>localharness</project>\n"
    "</invoke>"
)


def test_is_polluted_detects_blob_and_passes_clean_text():
    assert is_polluted(POLLUTED) is True
    assert is_polluted("A normal one-line summary with no markup.") is False
    assert is_polluted("") is False
    assert is_polluted(None) is False


def test_clean_summary_detail_resplits():
    summary, detail, changed = clean_summary_detail(POLLUTED, None)
    assert changed is True
    assert summary == (
        "Measured 2026-09-20 on localharness: two lanes had no source. "
        "permanently inert."
    )
    # <detail> body salvaged because the detail column was empty.
    assert detail == "Before: image reddit-sd-week. After: every lane reachable."
    # No structural tags survive in the cleaned summary.
    assert "</summary>" not in summary and "</invoke>" not in summary


def test_clean_summary_detail_leaves_clean_rows_untouched():
    s, d, changed = clean_summary_detail("clean summary", "clean detail")
    assert (s, d, changed) == ("clean summary", "clean detail", False)


def test_clean_summary_detail_keeps_existing_detail():
    _, detail, changed = clean_summary_detail(POLLUTED, "operator-written detail")
    assert changed is True
    assert detail == "operator-written detail"  # not overwritten by salvage


def test_clean_summary_detail_recovers_when_blob_starts_with_tag():
    blob = "<summary>real text here</summary><detail>body</detail></invoke>"
    summary, detail, changed = clean_summary_detail(blob, None)
    assert changed is True
    assert summary == "real text here"
    assert detail == "body"


def test_strip_tool_call_fragments():
    assert strip_tool_call_fragments("rule body</invoke>") == "rule body"
    assert strip_tool_call_fragments("clean rule body") == "clean rule body"
    assert strip_tool_call_fragments("") == ""


# --- NEGATIVE / ERROR pollution (issue #170) --------------------------------

# A what_failed as observed on NEGATIVE #10: the whole serialized entry crammed
# into the headline column, sibling fields empty.
POLLUTED_NEG = (
    "I measured the cache honestly and then wrote the opposite into the issue."
    "</what_failed>\n"
    '<parameter name="why_failed">Zero requests is evidence of caching, not '
    "staleness.</parameter>\n"
    '<parameter name="correct_approach">Re-read the write-up against the '
    "measurement, not memory of it.</parameter>\n"
    '<parameter name="project">localharness</parameter>\n'
    '<parameter name="tags">honesty,cache</parameter>\n'
    "</invoke>"
)

# An ERROR pattern as observed on #71: real text, serialized <error_text> close,
# then the sibling fields; last block lacks a closing </parameter>.
POLLUTED_ERR = (
    "main is RED: a gitignored eval dir exists on the desk and in no clone."
    "</error_text>\n"
    '<parameter name="context">A check whose input differs between desk and '
    "CI.</parameter>\n"
    '<parameter name="project">localharness'
)


def test_clean_negative_resplits_into_columns():
    wf, why, fix, changed = clean_negative(POLLUTED_NEG, None, None)
    assert changed is True
    assert wf == (
        "I measured the cache honestly and then wrote the opposite into the issue."
    )
    assert why == "Zero requests is evidence of caching, not staleness."
    assert fix == "Re-read the write-up against the measurement, not memory of it."
    for junk in ("</what_failed>", "<parameter", "</invoke>"):
        assert junk not in wf and junk not in (why or "") and junk not in (fix or "")


def test_clean_negative_leaves_clean_rows_untouched():
    out = clean_negative("tried X", "because Y", "do Z instead")
    assert out == ("tried X", "because Y", "do Z instead", False)


def test_clean_negative_keeps_existing_columns():
    # When the real columns are already populated, salvage must not overwrite.
    wf, why, fix, changed = clean_negative(
        POLLUTED_NEG, "operator why", "operator fix"
    )
    assert changed is True
    assert (why, fix) == ("operator why", "operator fix")


def test_clean_error_resplits_into_columns():
    pat, ctx, rc, fix, changed = clean_error(POLLUTED_ERR, None, None, None)
    assert changed is True
    assert pat == (
        "main is RED: a gitignored eval dir exists on the desk and in no clone."
    )
    assert ctx == "A check whose input differs between desk and CI."
    assert rc is None and fix is None  # not present in the blob
    assert "<parameter" not in pat and "</error_text>" not in pat


def test_clean_error_leaves_clean_rows_untouched():
    out = clean_error("boom", "while doing X", "because Y", "do Z")
    assert out == ("boom", "while doing X", "because Y", "do Z", False)
