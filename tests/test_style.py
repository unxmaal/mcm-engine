"""issue #63 hard layer: the mechanical tic counter. Red-proofed both
directions — it must fire on the tics it claims to catch and must NOT fire on
clean prose (a counter that flags clean text is worse than none)."""
from __future__ import annotations

from mcm_engine.style import VOICE_EXEMPLAR, count_style_tics


def test_counts_emoji():
    assert count_style_tics("Shipped it 🚀 and 🎉")["emoji"] == 2
    assert count_style_tics("done ✅")["emoji"] == 1


def test_counts_em_dashes():
    assert count_style_tics("one — two — three")["em_dash"] == 2
    assert count_style_tics("inline -- dash")["em_dash"] == 1


def test_clean_prose_scores_zero():
    clean = (
        "The registry walks each ingester in registration order and takes the "
        "first that matches. To add one, implement matches and stream. "
        "A hyphen-joined word and a range 1-10 are not tics."
    )
    tics = count_style_tics(clean)
    assert tics == {"emoji": 0, "em_dash": 0}


def test_empty_and_none_safe():
    assert count_style_tics("") == {"emoji": 0, "em_dash": 0}
    assert count_style_tics(None) == {"emoji": 0, "em_dash": 0}


def test_hyphenated_words_are_not_em_dashes():
    # A single hyphen and triple-dash separators must not read as the tic.
    assert count_style_tics("well-formed, fail-open, read-only")["em_dash"] == 0
    assert count_style_tics("---")["em_dash"] == 0


def test_exemplar_is_a_positive_target_not_a_denylist():
    # The exemplar must itself be clean (it is surfaced into context) and must
    # read as a positive target, not a list of prohibitions.
    assert count_style_tics(VOICE_EXEMPLAR) == {"emoji": 0, "em_dash": 0}
    assert "AFTER:" in VOICE_EXEMPLAR and "BEFORE:" in VOICE_EXEMPLAR
