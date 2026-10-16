"""Serialized-entry leakage cleanup (issue #142): the sanitizer re-splits a
polluted summary, the store-time guards reject it, and sanitize_knowledge cleans
existing rows in place.
"""
from __future__ import annotations

from mcm_engine.sanitize import (
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
