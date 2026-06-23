"""Python AST ingester — per-function / per-class semantic candidates.

Each top-level ``def`` / ``async def`` / ``class`` in a Python source
file becomes its own candidate. Each module with a docstring also
becomes a module-level candidate. The agent then evaluates each candidate
separately: is this function teaching a pattern worth recording? is this
class's docstring a real lesson or boilerplate?

Higher fidelity than text-dir at the cost of being Python-only. Other
language families would each need their own AST-aware ingester.

Topic format: ``<relative-path>::<symbol>`` for functions and classes,
``<relative-path>`` for module-level docstrings.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterator

from ..backends import KnowledgeRow
from . import IngestError, register


# Anything that signals "this directory is a Python project root."
_PYTHON_PROJECT_MARKERS = frozenset({
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
})

_SKIP_DIRS = frozenset({
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "build", "dist",
    ".git", ".svn", ".hg",
    "node_modules",
})

# Include the function body in the candidate detail up to this many
# lines. Beyond this, summarize ("function is N lines") and skip the body
# — the agent can Read the file directly if it needs more.
_MAX_BODY_LINES = 50


@register
class PythonAstIngester:
    """Emits one candidate per top-level function / class / module-doc
    in a Python codebase, parsed via ``ast.parse``."""

    name = "python-ast"

    def __init__(self) -> None:
        self._files_parsed = 0
        self._files_with_syntax_errors = 0
        self._candidates_emitted = 0

    @classmethod
    def matches(cls, source: str) -> bool:
        p = Path(source)
        if not p.is_dir():
            return False
        # Explicit project marker?
        for marker in _PYTHON_PROJECT_MARKERS:
            if (p / marker).exists():
                return True
        # Or any .py file under the tree (excluding skip-dirs)?
        for f in p.rglob("*.py"):
            rel_parts = f.relative_to(p).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            return True
        return False

    def stream(
        self, source: str, opts: dict[str, Any]
    ) -> Iterator[KnowledgeRow]:
        root = Path(source).expanduser().resolve()
        kind = opts.get("kind") or "knowledge"
        project = opts.get("project") or None
        skip = set(opts.get("skip") or _SKIP_DIRS)

        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue

            try:
                source_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise IngestError(f"read failed: {rel}", e) from e

            try:
                tree = ast.parse(source_text, filename=str(path))
            except SyntaxError:
                self._files_with_syntax_errors += 1
                continue

            self._files_parsed += 1
            lines = source_text.split("\n")

            mod_candidate = _module_candidate(rel, tree, kind, project, self.name)
            if mod_candidate is not None:
                self._candidates_emitted += 1
                yield mod_candidate

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_private(node.name):
                        continue
                    self._candidates_emitted += 1
                    yield _function_candidate(rel, lines, node, kind, project, self.name)
                elif isinstance(node, ast.ClassDef):
                    if _is_private(node.name):
                        continue
                    self._candidates_emitted += 1
                    yield _class_candidate(rel, lines, node, kind, project, self.name)

    def report(self) -> str:
        lines: list[str] = []
        if self._files_parsed:
            lines.append(
                f"# python-ast: parsed {self._files_parsed} file(s), "
                f"emitted {self._candidates_emitted} candidate(s)"
            )
        if self._files_with_syntax_errors:
            lines.append(
                f"# python-ast: {self._files_with_syntax_errors} file(s) had "
                f"syntax errors and were skipped (see source for details)"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Candidate builders
# ---------------------------------------------------------------------------


def _module_candidate(
    rel: Path, tree: ast.Module, kind: str, project: str | None, ingester_name: str,
) -> KnowledgeRow | None:
    docstring = ast.get_docstring(tree)
    if not docstring:
        return None
    return KnowledgeRow(
        id=0,
        topic=str(rel),
        kind=kind,
        summary=_summary_from_docstring(docstring),
        detail=f"# Module: {rel}\n\n{docstring}",
        tags=_tags_for(rel, "module", ingester_name),
        project=project,
    )


def _function_candidate(
    rel: Path,
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    kind: str,
    project: str | None,
    ingester_name: str,
) -> KnowledgeRow:
    sig = _function_signature(lines, node)
    docstring = ast.get_docstring(node) or ""
    summary = _summary_from_docstring(docstring) or sig

    body_start = node.lineno - 1
    body_end = node.end_lineno or node.lineno
    body_len = body_end - body_start

    detail_parts: list[str] = [f"# Function: {rel}::{node.name}\n"]
    if sig:
        detail_parts.append(f"```python\n{sig}\n```\n")
    if docstring:
        detail_parts.append(f"## Docstring\n\n{docstring}\n")
    if body_len <= _MAX_BODY_LINES:
        body_text = "\n".join(lines[body_start:body_end])
        detail_parts.append(f"## Source\n\n```python\n{body_text}\n```")
    else:
        detail_parts.append(
            f"## Source\n\n_({body_len} lines — body omitted from candidate; "
            f"Read {rel} directly if the body is relevant to the evaluation.)_"
        )

    return KnowledgeRow(
        id=0,
        topic=f"{rel}::{node.name}",
        kind=kind,
        summary=summary[:300],
        detail="\n".join(detail_parts),
        tags=_tags_for(rel, "function", ingester_name),
        project=project,
    )


def _class_candidate(
    rel: Path,
    lines: list[str],
    node: ast.ClassDef,
    kind: str,
    project: str | None,
    ingester_name: str,
) -> KnowledgeRow:
    docstring = ast.get_docstring(node) or ""
    bases = ", ".join(_unparse(b) for b in node.bases if _unparse(b))
    sig = f"class {node.name}({bases}):" if bases else f"class {node.name}:"

    methods: list[str] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_private(item.name):
                methods.append(item.name)

    summary = _summary_from_docstring(docstring) or sig
    detail_parts: list[str] = [
        f"# Class: {rel}::{node.name}\n",
        f"```python\n{sig}\n```\n",
    ]
    if docstring:
        detail_parts.append(f"## Docstring\n\n{docstring}\n")
    if methods:
        detail_parts.append("## Public methods\n\n" + "\n".join(
            f"- `{m}`" for m in methods
        ))

    return KnowledgeRow(
        id=0,
        topic=f"{rel}::{node.name}",
        kind=kind,
        summary=summary[:300],
        detail="\n".join(detail_parts),
        tags=_tags_for(rel, "class", ingester_name),
        project=project,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_private(name: str) -> bool:
    """Single-leading-underscore conventional-private; dunder is NOT private."""
    return name.startswith("_") and not name.startswith("__")


def _function_signature(lines: list[str], node: ast.AST) -> str:
    """Return everything from the ``def``/``async def`` line through the
    first line ending with a colon — i.e. the signature spanning multi-
    line argument lists. Strips trailing whitespace."""
    if not hasattr(node, "lineno"):
        return ""
    sig_lines: list[str] = []
    for i in range(node.lineno - 1, len(lines)):
        sig_lines.append(lines[i])
        if lines[i].rstrip().endswith(":"):
            break
    return "\n".join(sig_lines).strip()


def _summary_from_docstring(docstring: str) -> str:
    if not docstring:
        return ""
    for line in docstring.split("\n"):
        s = line.strip()
        if s:
            return s
    return ""


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def _tags_for(rel_path: Path, ast_kind: str, ingester_name: str) -> str:
    tags = {ingester_name, f"ast:{ast_kind}", "language:python"}
    for part in rel_path.parts[:-1]:
        tags.add(part.lower())
    return ",".join(sorted(tags))
