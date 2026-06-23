"""Tests for the python-ast ingester.

Contract this suite locks down:
  - python-ast matches Python project markers OR any .py file
  - emits one candidate per top-level def / async def / class
  - emits a module-level candidate if (and only if) the module has a docstring
  - candidate detail contains signature + docstring + body (up to a cap)
  - private (single-underscore-prefix) symbols are skipped
  - syntax-broken files are skipped silently (counted in report)
  - report() summarizes parse counts
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcm_engine.backends import KnowledgeRow
from mcm_engine.ingest import find, registered
from mcm_engine.ingest.python_ast import PythonAstIngester


# ---------------------------------------------------------------------------
# Registry placement
# ---------------------------------------------------------------------------


def test_python_ast_is_registered():
    names = [cls.name for cls in registered()]
    assert "python-ast" in names


def test_python_ast_comes_first_so_it_wins_over_text_dir():
    names = [cls.name for cls in registered()]
    py_idx = names.index("python-ast")
    txt_idx = names.index("text-dir")
    assert py_idx < txt_idx


# ---------------------------------------------------------------------------
# matches() — what counts as a Python project
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
])
def test_matches_dir_with_project_marker(tmp_path, marker):
    (tmp_path / marker).write_text("x", encoding="utf-8")
    assert PythonAstIngester.matches(str(tmp_path)) is True


def test_matches_dir_with_just_py_files(tmp_path):
    (tmp_path / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    assert PythonAstIngester.matches(str(tmp_path)) is True


def test_does_not_match_dir_without_python(tmp_path):
    (tmp_path / "a.rs").write_text("fn x() {}", encoding="utf-8")
    (tmp_path / "README.md").write_text("# project", encoding="utf-8")
    assert PythonAstIngester.matches(str(tmp_path)) is False


def test_does_not_match_when_only_py_files_are_inside_skip_dirs(tmp_path):
    """`.venv/.../site-packages/*.py` shouldn't trick the matcher into
    claiming a non-Python project as Python."""
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "lib.py").write_text("ok", encoding="utf-8")
    (tmp_path / "a.rs").write_text("fn x() {}", encoding="utf-8")
    assert PythonAstIngester.matches(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# stream() — what gets emitted
# ---------------------------------------------------------------------------


def _ingest_all(tmp_path: Path, opts: dict[str, Any] | None = None) -> list[KnowledgeRow]:
    return list(PythonAstIngester().stream(str(tmp_path), opts or {}))


def test_top_level_function_emits_one_candidate(tmp_path):
    (tmp_path / "mod.py").write_text(
        '"""module docstring"""\n\n'
        'def hello(name):\n'
        '    """Say hi to name."""\n'
        '    return f"hi {name}"\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py::hello" in topics


def test_module_with_docstring_emits_module_candidate(tmp_path):
    (tmp_path / "mod.py").write_text(
        '"""I am a module docstring."""\n\ndef f(): pass\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py" in topics


def test_module_without_docstring_does_not_emit_module_candidate(tmp_path):
    """Module-level candidate fires only on docstring presence —
    otherwise we'd be emitting noise for every Python file with no doc."""
    (tmp_path / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py" not in topics
    assert "mod.py::f" in topics


def test_top_level_class_emits_one_candidate(tmp_path):
    (tmp_path / "mod.py").write_text(
        'class Widget:\n'
        '    """A widget."""\n'
        '    def __init__(self):\n'
        '        self.x = 1\n'
        '    def doit(self):\n'
        '        pass\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py::Widget" in topics


def test_private_function_skipped(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def public_fn(): pass\n'
        'def _private_fn(): pass\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py::public_fn" in topics
    assert "mod.py::_private_fn" not in topics


def test_dunder_function_NOT_skipped(tmp_path):
    """Dunder methods are public API by convention even though they
    start with underscore. The is-private check distinguishes _x (skip)
    from __x__ (keep)."""
    (tmp_path / "mod.py").write_text(
        'def __version__(): return "1.0"\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "mod.py::__version__" in topics


def test_class_summary_uses_docstring_when_present(tmp_path):
    (tmp_path / "mod.py").write_text(
        'class Widget:\n'
        '    """A widget that does widget things."""\n'
        '    pass\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    widget = next(r for r in rows if r.topic == "mod.py::Widget")
    assert "widget things" in widget.summary.lower()


def test_function_detail_contains_signature_and_docstring(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def add(a: int, b: int) -> int:\n'
        '    """Sum two integers."""\n'
        '    return a + b\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    fn = next(r for r in rows if r.topic == "mod.py::add")
    assert "def add(a: int, b: int)" in fn.detail
    assert "Sum two integers" in fn.detail


def test_long_function_body_truncated_with_note(tmp_path):
    """Functions above the body-line cap surface as signature + docstring
    + a note pointing to the source file. Avoids dumping a 500-line
    function into every candidate."""
    body = "\n".join(f"    x = {i}" for i in range(80))
    (tmp_path / "mod.py").write_text(
        f'def big():\n    """Big function."""\n{body}\n',
        encoding="utf-8",
    )
    rows = _ingest_all(tmp_path)
    fn = next(r for r in rows if r.topic == "mod.py::big")
    assert "body omitted" in fn.detail
    assert "80 lines" in fn.detail or "81 lines" in fn.detail or "82 lines" in fn.detail


def test_syntax_error_file_skipped(tmp_path):
    """A file with a parse error shouldn't crash the ingest — we just
    skip it (and count it in the report)."""
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def g(): pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "ok.py::g" in topics
    assert not any(t.startswith("broken.py") for t in topics)


def test_skip_dirs_excluded(tmp_path):
    (tmp_path / "src.py").write_text("def f(): pass\n", encoding="utf-8")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "junk.py").write_text("def junk(): pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    topics = {r.topic for r in rows}
    assert "src.py::f" in topics
    assert not any(t.startswith(".venv/") for t in topics)


def test_kind_defaults_to_knowledge(tmp_path):
    (tmp_path / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    assert rows[0].kind == "knowledge"


def test_kind_can_be_overridden(tmp_path):
    (tmp_path / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path, {"kind": "code"})
    assert rows[0].kind == "code"


def test_tags_include_language_and_ast_kind(tmp_path):
    (tmp_path / "mod.py").write_text("def f(): pass\nclass C: pass\n", encoding="utf-8")
    rows = _ingest_all(tmp_path)
    fn = next(r for r in rows if r.topic == "mod.py::f")
    cls = next(r for r in rows if r.topic == "mod.py::C")
    assert "language:python" in fn.tags
    assert "ast:function" in fn.tags
    assert "ast:class" in cls.tags


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


def test_report_summarizes_parse_count(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def g(): pass\nclass C: pass\n", encoding="utf-8")
    ing = PythonAstIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert "parsed 2" in report
    assert "candidate" in report


def test_report_mentions_syntax_errors(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def g(): pass\n", encoding="utf-8")
    ing = PythonAstIngester()
    list(ing.stream(str(tmp_path), {}))
    report = ing.report()
    assert "syntax error" in report.lower()
