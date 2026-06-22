"""Tests for the bulk-ingest framework + the markdown-dir ingester.

The contract this suite locks down:
- The dispatcher picks the right ingester (by auto-detection or
  explicit --type).
- The markdown ingester reads frontmatter / body / folder structure
  into KnowledgeRow fields the way callers expect.
- The dispatcher's behavior on missing ingesters is loud (no silent
  no-ops).

We don't exercise the CLI here — that goes via `tests/test_cli_*.py`
patterns. This file is the unit-level guarantee for the framework.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from mcm_engine.backends import KnowledgeRow
from mcm_engine.ingest import (
    Ingester,
    IngestError,
    NoMatchingIngester,
    UnknownIngester,
    find,
    register,
    registered,
)
from mcm_engine.ingest.markdown import MarkdownDirIngester


# ---------------------------------------------------------------------------
# Registry + dispatcher
# ---------------------------------------------------------------------------


def test_markdown_ingester_is_registered_on_package_import():
    """Side-effect of `import mcm_engine.ingest` — the built-in ingesters
    must self-register. If they don't, `mcm-engine ingest` returns 'no
    ingester' for every input."""
    names = [cls.name for cls in registered()]
    assert "markdown-dir" in names


def test_find_by_explicit_type_returns_named_ingester():
    ing = find("anything", explicit_name="markdown-dir")
    assert isinstance(ing, MarkdownDirIngester)


def test_find_by_explicit_type_unknown_raises():
    with pytest.raises(UnknownIngester) as exc:
        find("anything", explicit_name="not-a-real-ingester")
    assert "not-a-real-ingester" in str(exc.value)
    # Helpful error: lists what IS available.
    assert "markdown-dir" in str(exc.value)


def test_find_by_auto_detect_picks_markdown_dir(tmp_path):
    """A directory with at least one .md should match markdown-dir."""
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    ing = find(str(tmp_path))
    assert isinstance(ing, MarkdownDirIngester)


def test_find_no_match_raises_with_help(tmp_path):
    """A directory containing zero .md files shouldn't match."""
    (tmp_path / "ignored.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(NoMatchingIngester) as exc:
        find(str(tmp_path))
    assert "markdown-dir" in str(exc.value)  # error names alternatives


def test_register_is_idempotent():
    """Re-registering the markdown ingester (which already self-registered
    on package import) shouldn't add a duplicate entry."""
    before = len(registered())
    register(MarkdownDirIngester)
    assert len(registered()) == before


# ---------------------------------------------------------------------------
# MarkdownDirIngester.matches
# ---------------------------------------------------------------------------


def test_matches_true_for_dir_with_md(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    assert MarkdownDirIngester.matches(str(tmp_path)) is True


def test_matches_false_for_dir_without_md(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert MarkdownDirIngester.matches(str(tmp_path)) is False


def test_matches_false_for_nonexistent_path(tmp_path):
    assert MarkdownDirIngester.matches(str(tmp_path / "nope")) is False


def test_matches_false_for_file_not_dir(tmp_path):
    f = tmp_path / "single.md"
    f.write_text("x", encoding="utf-8")
    assert MarkdownDirIngester.matches(str(f)) is False


# ---------------------------------------------------------------------------
# MarkdownDirIngester.stream — shape of yielded rows
# ---------------------------------------------------------------------------


def _ingest_all(tmp_path: Path, opts: dict[str, Any] | None = None) -> list[KnowledgeRow]:
    opts = opts or {}
    return list(MarkdownDirIngester().stream(str(tmp_path), opts))


def test_stream_yields_one_row_per_md(tmp_path):
    (tmp_path / "a.md").write_text("# A\nbody a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\nbody b", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert {r.topic for r in rows} == {"a", "b"}


def test_topic_is_relative_path_without_extension(tmp_path):
    sub = tmp_path / "Friends"
    sub.mkdir()
    (sub / "Heather Little.md").write_text("# Heather", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert any(r.topic == "Friends/Heather Little" for r in rows)


def test_kind_defaults_to_knowledge(tmp_path):
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].kind == "knowledge"


def test_kind_can_be_overridden_via_opts(tmp_path):
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    rows = _ingest_all(tmp_path, {"kind": "obsidian"})
    assert rows[0].kind == "obsidian"


def test_project_carried_from_opts(tmp_path):
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    rows = _ingest_all(tmp_path, {"project": "personal-obsidian"})
    assert rows[0].project == "personal-obsidian"


def test_summary_from_frontmatter_description(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ndescription: handpicked summary\n---\n# A\nbody\n",
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    assert rows[0].summary == "handpicked summary"


def test_summary_falls_back_to_first_non_heading_line(tmp_path):
    (tmp_path / "a.md").write_text("# Heading\n\nfirst real line\nsecond\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].summary == "first real line"


def test_summary_truncated_to_300_chars(tmp_path):
    long = "x" * 500
    (tmp_path / "a.md").write_text(long, encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert len(rows[0].summary) == 300


def test_tags_include_folder_hierarchy(tmp_path):
    sub = tmp_path / "Friends" / "Childhood"
    sub.mkdir(parents=True)
    (sub / "alice.md").write_text("hi", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    tags = set(rows[0].tags.split(","))
    assert {"friends", "childhood"}.issubset(tags)


def test_tags_include_ingester_name(tmp_path):
    """The ingester name surfaces as a tag so search can scope to a
    source ('obsidian'-tagged knowledge etc.)."""
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert "markdown-dir" in rows[0].tags.split(",")


def test_tags_include_frontmatter_tags_list(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntags: [foo, bar, baz]\n---\nbody\n", encoding="utf-8"
    )
    rows = _ingest_all(tmp_path)
    tags = set(rows[0].tags.split(","))
    assert {"foo", "bar", "baz"}.issubset(tags)


def test_tags_include_frontmatter_tags_csv(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntags: foo,bar,baz\n---\nbody\n", encoding="utf-8"
    )
    rows = _ingest_all(tmp_path)
    tags = set(rows[0].tags.split(","))
    assert {"foo", "bar", "baz"}.issubset(tags)


def test_detail_holds_full_file_contents(tmp_path):
    body = "---\ndescription: x\n---\n# A\nbody\nmore body\n"
    (tmp_path / "a.md").write_text(body, encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].detail == body


def test_skip_dirs_excludes_subtrees(tmp_path):
    """The .obsidian dir is conventionally excluded; the ingester must
    honor the skip list."""
    (tmp_path / "good.md").write_text("kept", encoding="utf-8")
    junk = tmp_path / ".obsidian"
    junk.mkdir()
    (junk / "noise.md").write_text("dropped", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "good" in topics
    assert not any(t.startswith(".obsidian") for t in topics)


def test_skip_dirs_honored_when_explicit_via_opts(tmp_path):
    (tmp_path / "good.md").write_text("kept", encoding="utf-8")
    junk = tmp_path / "drafts"
    junk.mkdir()
    (junk / "wip.md").write_text("dropped", encoding="utf-8")
    rows = _ingest_all(tmp_path, {"skip": {"drafts"}})
    topics = {r.topic for r in rows}
    assert "good" in topics
    assert not any(t.startswith("drafts") for t in topics)


def test_malformed_frontmatter_treats_whole_file_as_body(tmp_path):
    """If YAML parsing fails, we don't crash — we just treat the whole
    file as body so the ingest keeps going."""
    (tmp_path / "broken.md").write_text(
        "---\nthis is: not [valid yaml\nat all: ---\nbody continues\n",
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    assert len(rows) == 1
    # Tags should not contain the malformed YAML's "this is" key.
    # (We just verify the row exists and didn't error.)


def test_yields_sorted_for_deterministic_order(tmp_path):
    (tmp_path / "c.md").write_text("c", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert [r.topic for r in rows] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Custom-ingester smoke test — proves the registry is third-party-friendly
# ---------------------------------------------------------------------------


def test_third_party_ingester_can_register_and_be_found():
    """If someone in another package registers an Ingester, the
    dispatcher should pick it up by --type without any other ceremony."""

    class FakeJsonl:
        name = "fake-jsonl-for-test"

        @classmethod
        def matches(cls, source):
            return source.endswith(".jsonl-test")

        def stream(self, source, opts):
            yield KnowledgeRow(
                id=0, topic="fake", kind="knowledge",
                summary="from fake ingester", detail=None,
                tags=None, project=None,
            )

    try:
        register(FakeJsonl)
        # Auto-detect.
        ing = find("something.jsonl-test")
        assert isinstance(ing, FakeJsonl)
        # Explicit.
        ing2 = find("anything", explicit_name="fake-jsonl-for-test")
        assert isinstance(ing2, FakeJsonl)
        # Streams.
        rows = list(ing.stream("x", {}))
        assert len(rows) == 1 and rows[0].topic == "fake"
    finally:
        # Don't leak the test ingester into other tests.
        from mcm_engine.ingest import _REGISTERED
        if FakeJsonl in _REGISTERED:
            _REGISTERED.remove(FakeJsonl)
