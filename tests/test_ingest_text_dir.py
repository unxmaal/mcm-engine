"""Tests for the text-dir generic-fallback ingester.

Contract this suite locks down:
  - text-dir matches any directory containing text-like files
  - text-dir does NOT claim .md files (markdown-dir owns those)
  - text-dir is the catch-all (registered last), so language-specific
    ingesters get first crack
  - report() surfaces what was seen + suggests AST upgrades for langs
    where the count is high enough to matter
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcm_engine.backends import KnowledgeRow
from mcm_engine.ingest import find, registered
from mcm_engine.ingest.text_dir import TEXT_EXTENSIONS, TextDirIngester


# ---------------------------------------------------------------------------
# Registry: text-dir is registered AND comes after the specific ingesters
# ---------------------------------------------------------------------------


def test_text_dir_is_registered_on_package_import():
    names = [cls.name for cls in registered()]
    assert "text-dir" in names


def test_text_dir_comes_after_specific_ingesters_in_registry_order():
    """Order matters because find() returns the first matching ingester.
    text-dir must be the last fallback — otherwise it'd shadow more
    specific ingesters that should win."""
    names = [cls.name for cls in registered()]
    text_dir_idx = names.index("text-dir")
    md_idx = names.index("markdown-dir")
    py_ast_idx = names.index("python-ast")
    assert text_dir_idx > md_idx
    assert text_dir_idx > py_ast_idx


# ---------------------------------------------------------------------------
# matches() — when text-dir should claim a directory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["py", "rs", "ts", "go", "json", "yaml", "toml", "txt", "j2"])
def test_matches_dir_with_any_text_extension(tmp_path, ext):
    (tmp_path / f"a.{ext}").write_text("body", encoding="utf-8")
    assert TextDirIngester.matches(str(tmp_path)) is True


def test_matches_dockerfile_no_extension(tmp_path):
    """Dockerfile and Makefile are extension-less but well-known text files."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    assert TextDirIngester.matches(str(tmp_path)) is True


def test_does_not_match_dir_with_only_binaries(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "song.mp3").write_bytes(b"binary")
    assert TextDirIngester.matches(str(tmp_path)) is False


def test_does_not_match_dir_with_only_md(tmp_path):
    """Markdown is owned by markdown-dir — text-dir leaves it alone so
    the two ingesters don't double-claim the same files."""
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    assert TextDirIngester.matches(str(tmp_path)) is False


def test_does_not_match_empty_dir(tmp_path):
    assert TextDirIngester.matches(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Dispatcher precedence: text-dir is the fallback after more-specific ones
# ---------------------------------------------------------------------------


def test_python_project_picks_python_ast_not_text_dir(tmp_path):
    """A directory with a pyproject.toml should resolve to python-ast,
    not text-dir, even though text-dir would also match."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def f(): pass\n", encoding="utf-8")
    ing = find(str(tmp_path))
    assert ing.name == "python-ast"


def test_markdown_vault_picks_markdown_dir_not_text_dir(tmp_path):
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    ing = find(str(tmp_path))
    assert ing.name == "markdown-dir"


def test_pure_rust_project_falls_through_to_text_dir(tmp_path):
    """No Python, no markdown — text-dir is the only thing that claims it."""
    (tmp_path / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "lib.rs").write_text("// lib\n", encoding="utf-8")
    ing = find(str(tmp_path))
    assert ing.name == "text-dir"


# ---------------------------------------------------------------------------
# stream() — candidate shape
# ---------------------------------------------------------------------------


def _ingest_all(tmp_path: Path, opts: dict[str, Any] | None = None) -> list[KnowledgeRow]:
    return list(TextDirIngester().stream(str(tmp_path), opts or {}))


def test_topic_includes_extension(tmp_path):
    """text-dir keeps the extension in the topic so a.py and a.md are
    distinguishable knowledge entries."""
    (tmp_path / "a.py").write_text("# python", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].topic == "a.py"


def test_one_candidate_per_text_file(tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.rs").write_text("b", encoding="utf-8")
    (tmp_path / "c.go").write_text("c", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert {r.topic for r in rows} == {"a.py", "b.rs", "c.go"}


def test_md_files_not_picked_up(tmp_path):
    """text-dir's contract is to leave markdown for markdown-dir."""
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "a.py" in topics
    assert "b.md" not in topics


def test_binary_files_skipped(tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "song.mp3").write_bytes(b"\xff\xfb\x90")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    rows = _ingest_all(tmp_path)
    assert {r.topic for r in rows} == {"a.py"}


def test_lockfiles_skipped(tmp_path):
    (tmp_path / "src.py").write_text("src", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lots of lock content", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"big":"file"}', encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert {r.topic for r in rows} == {"src.py"}


def test_default_skip_dirs_skipped(tmp_path):
    """node_modules, .venv, .git, etc. don't yield candidates."""
    (tmp_path / "src.py").write_text("ok", encoding="utf-8")
    junk = tmp_path / "node_modules" / "lib"
    junk.mkdir(parents=True)
    (junk / "junk.js").write_text("dropped", encoding="utf-8")
    venv_junk = tmp_path / ".venv" / "lib" / "pkg"
    venv_junk.mkdir(parents=True)
    (venv_junk / "junk.py").write_text("also dropped", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert {r.topic for r in rows} == {"src.py"}


def test_terraform_project_surfaces_only_user_authored_tf(tmp_path):
    """Regression for the dodd.cloud demo: text-dir on a terraform tree
    must surface main.tf but NOT the .terraform/ provider downloads,
    NOT the .terraform.lock.hcl, and NOT terraform.tfstate (which often
    contains secrets)."""
    (tmp_path / "main.tf").write_text(
        'resource "aws_route53_zone" "main" {\n  name = "example.com"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / ".terraform.lock.hcl").write_text("# provider lock", encoding="utf-8")
    (tmp_path / "terraform.tfstate").write_text('{"secrets":"here"}', encoding="utf-8")
    provider_junk = tmp_path / ".terraform" / "providers" / "registry.terraform.io" / "hashicorp" / "aws"
    provider_junk.mkdir(parents=True)
    (provider_junk / "LICENSE.txt").write_text("Apache 2.0...", encoding="utf-8")
    (provider_junk / "terraform-provider-aws_v5_x5").write_bytes(b"binary blob")

    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert topics == {"main.tf"}, (
        f"text-dir on a terraform tree should surface only user-authored "
        f"main.tf; got {topics}"
    )


def test_tags_include_extension(tmp_path):
    (tmp_path / "a.rs").write_text("fn x() {}", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    tags = set(rows[0].tags.split(","))
    assert "ext:rs" in tags
    assert "text-dir" in tags


def test_tags_include_folder_hierarchy(tmp_path):
    sub = tmp_path / "src" / "cli"
    sub.mkdir(parents=True)
    (sub / "main.py").write_text("ok", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    tags = set(rows[0].tags.split(","))
    assert {"src", "cli"}.issubset(tags)


def test_jinja_templates_are_surfaced(tmp_path):
    """.j2 templates carry config/rules in ansible-style repos — text-dir must
    surface them. Regression: booterizer had 20 .j2 files skipped."""
    (tmp_path / "dhcpd.conf.j2").write_text(
        "# option domain-name must match the netboot server\n", encoding="utf-8"
    )
    rows = _ingest_all(tmp_path)
    assert "dhcpd.conf.j2" in {r.topic for r in rows}


def test_detail_holds_full_file_contents(tmp_path):
    body = "fn main() {\n  println!(\"hi\");\n}\n"
    (tmp_path / "main.rs").write_text(body, encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].detail == body


def test_summary_prefers_first_non_comment_line(tmp_path):
    (tmp_path / "a.py").write_text(
        "# this is a comment\n# more comments\nimport os\n", encoding="utf-8"
    )
    rows = _ingest_all(tmp_path)
    assert rows[0].summary == "import os"


def test_summary_falls_back_to_comment_when_no_other_content(tmp_path):
    (tmp_path / "a.py").write_text("# just a comment\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].summary == "# just a comment"


def test_kind_defaults_to_knowledge(tmp_path):
    (tmp_path / "a.py").write_text("ok", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].kind == "knowledge"


def test_kind_can_be_overridden(tmp_path):
    (tmp_path / "a.py").write_text("ok", encoding="utf-8")
    rows = _ingest_all(tmp_path, {"kind": "source-code"})
    assert rows[0].kind == "source-code"


# ---------------------------------------------------------------------------
# report() — the metacognitive flag mechanism
# ---------------------------------------------------------------------------


def test_report_is_empty_before_streaming():
    ing = TextDirIngester()
    assert ing.report() == ""


def test_report_summarizes_extensions_after_stream(tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / "c.rs").write_text("c", encoding="utf-8")
    ing = TextDirIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert ".py: 2" in report
    assert ".rs: 1" in report
    assert "text-dir surfaced" in report


def test_report_lists_skipped_extensions(tmp_path):
    """Binaries/unknowns that text-dir refused get logged so the user
    knows what was excluded."""
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "song.mp3").write_bytes(b"binary")
    (tmp_path / "img.png").write_bytes(b"binary")
    ing = TextDirIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert "skipped" in report
    assert ".mp3" in report or ".png" in report


def test_report_suggests_python_ast_when_enough_py_files_seen(tmp_path):
    """The metacognitive moment: text-dir notes that python-ast IS
    available and recommends switching for higher fidelity."""
    for i in range(5):
        (tmp_path / f"mod{i}.py").write_text("ok", encoding="utf-8")
    ing = TextDirIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert "python-ast" in report


def test_report_suggests_building_ast_ingester_for_unsupported_language(tmp_path):
    """For languages without a registered AST ingester, the report
    surfaces it as a build-this-someday note. This IS the flag mechanism
    the user asked for."""
    for i in range(5):
        (tmp_path / f"mod{i}.rs").write_text("fn x() {}", encoding="utf-8")
    ing = TextDirIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert "rust" in report.lower()
    assert "ast" in report.lower()


def test_report_does_not_suggest_for_low_counts(tmp_path):
    """A single .rs file shouldn't trigger 'build a rust ingester' noise."""
    (tmp_path / "a.rs").write_text("fn x() {}", encoding="utf-8")
    (tmp_path / "b.py").write_text("ok", encoding="utf-8")  # also low count
    ing = TextDirIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    # The summary line still exists, but no "consider:" suggestions for
    # 1-2 files of any single language.
    assert "consider:" not in report
