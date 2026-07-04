"""Tests for the mechanical rule-sift funnel (Slice 1, fix_ingestion).

This locks down the deterministic, model-free stages that turn a raw
ingester candidate stream into a small set of net-new, rule-shaped
candidates:

  A. extract_spans  — pull rule-shaped spans (comment/docstring blocks)
                      from raw code; treat curated prose as one span.
  B. is_rule_like   — normative-language gate; drops plain code/boilerplate.
  C. band_for /     — novelty banding vs the existing rule corpus:
     classify_novelty  KNOWN (drop) / REFINE (escalate) / NOVEL (new).
  D. sift           — the whole funnel + intra-run near-dup collapse.

No LLM anywhere here — that's the point. The model only ever sees what
survives this funnel.
"""
from __future__ import annotations

from mcm_engine.backends import KnowledgeRow
from mcm_engine.ingest import rulesift
from mcm_engine.ingest.rulesift import Band, RuleCandidate


# ---------------------------------------------------------------------------
# A. extract_spans
# ---------------------------------------------------------------------------


def test_prose_mode_returns_whole_text_as_one_span():
    text = "Always resolve db_path against project_root before wiring adapters."
    assert rulesift.extract_spans(text, raw_code=False) == [text]


def test_empty_text_yields_no_spans():
    assert rulesift.extract_spans("", raw_code=False) == []
    assert rulesift.extract_spans("   \n\n", raw_code=True) == []


def test_raw_code_extracts_hash_comment_block():
    src = 'x = 1\n# NOTE: upload_speed must be 460800; 921600 fails\ny = 2\n'
    spans = rulesift.extract_spans(src, raw_code=True)
    assert any("upload_speed must be 460800" in s for s in spans)


def test_raw_code_merges_consecutive_comment_lines_into_one_span():
    src = (
        "# You must hold both buttons for 3s to power on.\n"
        "# There is no power switch on the TC001.\n"
        "def boot(): pass\n"
    )
    spans = rulesift.extract_spans(src, raw_code=True)
    merged = [s for s in spans if "power on" in s and "power switch" in s]
    assert len(merged) == 1, f"consecutive comments should merge: {spans}"


def test_raw_code_extracts_slash_comments():
    src = "int x;\n// never call free() twice on this handle\nreturn x;\n"
    spans = rulesift.extract_spans(src, raw_code=True)
    assert any("never call free() twice" in s for s in spans)


def test_raw_code_extracts_block_comment():
    src = "a;\n/* WARNING: this mutex must be held before touching the queue */\nb;\n"
    spans = rulesift.extract_spans(src, raw_code=True)
    assert any("mutex must be held" in s for s in spans)


def test_raw_code_extracts_triple_quoted_docstring():
    src = 'def f():\n    """You must call init() before f() or it deadlocks."""\n    pass\n'
    spans = rulesift.extract_spans(src, raw_code=True)
    assert any("must call init() before f()" in s for s in spans)


def test_raw_code_without_comments_yields_no_spans():
    src = "def add(a, b):\n    return a + b\n"
    assert rulesift.extract_spans(src, raw_code=True) == []


def test_url_in_code_is_not_mistaken_for_a_comment():
    """`//` only starts a comment at line start (after whitespace), so a URL
    mid-assignment must not be captured as a comment span."""
    src = 'endpoint = "https://example.com/api"\n'
    assert rulesift.extract_spans(src, raw_code=True) == []


# ---------------------------------------------------------------------------
# B. is_rule_like
# ---------------------------------------------------------------------------


def test_normative_comment_is_rule_like():
    assert rulesift.is_rule_like("upload_speed must be 460800; 921600 fails after the baud switch")
    assert rulesift.is_rule_like("never commit secrets to git")
    assert rulesift.is_rule_like("WARNING: hold both buttons to power on the device")


def test_trivial_comment_is_not_rule_like():
    assert not rulesift.is_rule_like("increment counter")
    assert not rulesift.is_rule_like("loop over items")


def test_too_short_is_not_rule_like():
    assert not rulesift.is_rule_like("todo")
    assert not rulesift.is_rule_like("fixme")


def test_api_boilerplate_docstring_is_not_rule_like():
    doc = "Args: x the input value. Returns: the doubled value. Raises: nothing."
    assert not rulesift.is_rule_like(doc)


# ---------------------------------------------------------------------------
# C. band_for / classify_novelty
# ---------------------------------------------------------------------------


def test_band_for_thresholds():
    assert rulesift.band_for(0.95) is Band.KNOWN
    assert rulesift.band_for(0.90) is Band.KNOWN      # boundary inclusive
    assert rulesift.band_for(0.70) is Band.REFINE
    assert rulesift.band_for(0.50) is Band.REFINE     # boundary inclusive
    assert rulesift.band_for(0.20) is Band.NOVEL
    assert rulesift.band_for(0.0) is Band.NOVEL


def test_classify_novelty_empty_corpus_is_novel():
    band, matched = rulesift.classify_novelty("never commit secrets to git", [])
    assert band is Band.NOVEL
    assert matched is None


def test_classify_novelty_identical_is_known_and_names_the_rule():
    text = "always resolve db_path against project_root before wiring the storage adapters"
    band, matched = rulesift.classify_novelty(text, [(42, text)])
    assert band is Band.KNOWN
    assert matched == 42


def test_classify_novelty_unrelated_is_novel():
    existing = [(1, "the mahjong timer counts down on a 32x8 led matrix")]
    band, matched = rulesift.classify_novelty(
        "postgres advisory locks must be released in the same session that took them",
        existing,
    )
    assert band is Band.NOVEL
    assert matched is None


# ---------------------------------------------------------------------------
# D. sift — the whole funnel
# ---------------------------------------------------------------------------


def _row(topic: str, detail: str) -> KnowledgeRow:
    return KnowledgeRow(id=0, topic=topic, summary="", kind="knowledge", detail=detail)


def test_sift_surfaces_net_new_rule_shaped_span():
    rows = [_row("hw/boot.py", "def boot(): pass\n# You must hold both buttons for 3s to power on\n")]
    survivors = rulesift.sift(rows, [], raw_code=True)
    assert len(survivors) == 1
    assert survivors[0].band is Band.NOVEL
    assert survivors[0].source_topic == "hw/boot.py"
    assert "hold both buttons" in survivors[0].text


def test_sift_drops_non_rule_like_spans():
    rows = [_row("math.py", "def add(a, b):\n    return a + b\n# increment counter\n")]
    assert rulesift.sift(rows, [], raw_code=True) == []


def test_sift_drops_already_known_rules():
    known = "you must hold both buttons for 3s to power on the tc001 device"
    rows = [_row("hw/boot.py", f"# {known}\n")]
    survivors = rulesift.sift(rows, [(7, known)], raw_code=True)
    assert survivors == [], "a span matching an existing rule must be dropped"


def test_sift_collapses_intra_run_duplicates():
    """The same gotcha copy-pasted across two files should yield one survivor,
    not two — dedup runs over the survivor set before anything downstream."""
    span = "# you must set upload_speed to 460800 because 921600 fails after the baud switch\n"
    rows = [_row("a.py", span), _row("b.py", span)]
    survivors = rulesift.sift(rows, [], raw_code=True)
    assert len(survivors) == 1


def test_sift_prose_rows_treated_as_single_span():
    """Curated ingesters (markdown-dir, python-ast) already hand us prose;
    sift must not try to comment-scan them — the whole detail is one span."""
    rows = [_row("guide.md", "Never store the household code in the public seed.json file.")]
    survivors = rulesift.sift(rows, [], raw_code=False)
    assert len(survivors) == 1
    assert survivors[0].band is Band.NOVEL
