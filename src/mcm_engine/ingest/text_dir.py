"""Generic text-directory ingester — catch-all for codebases.

Surfaces any text-like file under a directory as one candidate. Lower
fidelity than a language-aware AST ingester (no per-function granularity)
but works for every language and any text format.

Order in the registry is after the more specific ingesters (python-ast,
markdown-dir). Auto-detection only falls through to text-dir when nothing
more specific claims the source.

The ``report()`` after a stream emits an extension breakdown + suggests
building dedicated AST ingesters for languages with a non-trivial count.
That's the metacognitive hook: the engine notices what it doesn't have
yet and tells you.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from ..backends import KnowledgeRow
from . import IngestError, register


# Text-like extensions we surface as candidates. Lowercase, no leading dot.
# Deliberately excludes .md (markdown-dir owns it) so each ingester has
# clear, non-overlapping ownership of its formats.
TEXT_EXTENSIONS = frozenset({
    # source code
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs",
    "rs", "go", "java", "kt", "swift",
    "c", "h", "cpp", "hpp", "cc", "cxx",
    "rb", "pl", "pm", "php",
    "sh", "bash", "zsh", "fish",
    "lua", "r", "scala", "clj", "ex", "exs",
    # templates (jinja/go/handlebars) — hold config + rules in IaC repos
    "j2", "jinja", "jinja2", "tmpl", "tpl",
    # markup / config (not .md — that belongs to markdown-dir)
    "txt", "rst", "adoc", "tex",
    "json", "yaml", "yml", "toml", "ini", "conf", "cfg", "service",
    "xml", "html", "htm", "css", "scss", "sass",
    # tabular / data
    "csv", "tsv",
    # SQL + queries
    "sql",
    # build / project files
    "dockerfile", "makefile",
    # infrastructure-as-code
    "tf", "hcl", "tfvars",
})

# Languages worth recommending an AST upgrade for. Map ext → (lang_name,
# whether we already have an AST ingester).
_AST_UPGRADE_HINTS: dict[str, tuple[str, bool]] = {
    "py":   ("python",     True),   # python-ast exists
    "rs":   ("rust",       False),
    "ts":   ("typescript", False),
    "tsx":  ("typescript", False),
    "js":   ("javascript", False),
    "jsx":  ("javascript", False),
    "go":   ("go",         False),
    "java": ("java",       False),
    "kt":   ("kotlin",     False),
}

# Directories to skip wholesale — large + low signal, mostly generated.
_DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg",
    ".venv", "venv", "env", "node_modules", "__pycache__",
    "target", "dist", "build", ".next", ".cache",
    ".obsidian", ".trash", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode",
    # terraform provider downloads — large, vendored, contains the
    # provider's own LICENSE.txt which would otherwise surface as junk.
    ".terraform",
})

# Files to skip by exact name (lockfiles, generated artifacts, secrets-prone).
_SKIP_FILENAMES = frozenset({
    "package-lock.json", "Cargo.lock", "uv.lock", "poetry.lock",
    "yarn.lock", "pnpm-lock.yaml", "Gemfile.lock", "composer.lock",
    # terraform-ecosystem: generated state (often contains secrets),
    # variable values (often contains secrets), provider lockfile.
    ".terraform.lock.hcl",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "terraform.tfvars",
})


@register
class TextDirIngester:
    """Catch-all directory ingester. Emits one candidate per text-like file."""

    name = "text-dir"

    def __init__(self) -> None:
        self._extensions_seen: Counter[str] = Counter()
        self._extensions_skipped: Counter[str] = Counter()

    @classmethod
    def matches(cls, source: str) -> bool:
        p = Path(source)
        if not p.is_dir():
            return False
        for f in p.rglob("*"):
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            rel_parts = f.relative_to(p).parts
            if any(part in _DEFAULT_SKIP_DIRS for part in rel_parts):
                continue
            if f.name in _SKIP_FILENAMES:
                continue
            if cls._is_text_file(f):
                return True
        return False

    @staticmethod
    def _is_text_file(path: Path) -> bool:
        ext = path.suffix.lstrip(".").lower()
        if ext in TEXT_EXTENSIONS:
            return True
        # No extension? Match by exact lowercase name for a few well-knowns.
        if not ext:
            return path.name.lower() in {"dockerfile", "makefile"}
        return False

    def stream(
        self, source: str, opts: dict[str, Any]
    ) -> Iterator[KnowledgeRow]:
        root = Path(source).expanduser().resolve()
        kind = opts.get("kind") or "knowledge"
        project = opts.get("project") or None
        skip = set(opts.get("skip") or _DEFAULT_SKIP_DIRS)

        for path in sorted(root.rglob("*")):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue

            rel = path.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            if path.name in _SKIP_FILENAMES:
                continue

            ext = path.suffix.lstrip(".").lower()
            if not self._is_text_file(path):
                if ext:
                    self._extensions_skipped[ext] += 1
                continue

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise IngestError(f"read failed: {rel}", e) from e

            self._extensions_seen[ext or path.name.lower()] += 1

            # Topic = relative path WITH extension. Distinguishes foo.py
            # from foo.md and makes per-file upserts unambiguous.
            topic = str(rel)
            summary = (_first_content_line(text) or f"({ext} file, {len(text)} chars)")[:300]
            tags = _tags_for(rel, ext or "noext", self.name)

            yield KnowledgeRow(
                id=0,
                topic=topic,
                kind=kind,
                summary=summary,
                detail=text,
                tags=tags,
                project=project,
            )

    def report(self) -> str:
        """Post-stream summary: extensions surfaced, extensions skipped,
        suggestions for languages where an AST ingester would help."""
        lines: list[str] = []
        if self._extensions_seen:
            counts = ", ".join(
                f".{e}: {n}" for e, n in self._extensions_seen.most_common()
            )
            lines.append(f"# text-dir surfaced: {counts}")
        if self._extensions_skipped:
            counts = ", ".join(
                f".{e}: {n}" for e, n in self._extensions_skipped.most_common(8)
            )
            lines.append(f"# text-dir skipped (non-text extensions): {counts}")

        suggestions = self._upgrade_suggestions()
        if suggestions:
            lines.append("# consider:")
            lines.extend(suggestions)
        return "\n".join(lines)

    def _upgrade_suggestions(self) -> list[str]:
        """For languages where text-dir saw enough files to be worth a
        proper AST ingester, surface the suggestion."""
        out: list[str] = []
        for ext, count in self._extensions_seen.most_common():
            hint = _AST_UPGRADE_HINTS.get(ext)
            if hint is None or count < 3:
                continue
            lang_name, already_has_ast = hint
            if already_has_ast:
                out.append(
                    f"  - {count} .{ext} files surfaced as text. "
                    f"`{lang_name}-ast` is registered — use "
                    f"`--type python-ast` for per-function candidates."
                )
            else:
                out.append(
                    f"  - {count} .{ext} files surfaced as text. "
                    f"No `{lang_name}-ast` ingester exists yet — building one "
                    f"would give per-function candidates."
                )
        return out


def _first_content_line(text: str) -> str:
    """First non-empty non-pure-comment line, for the summary."""
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        # Skip pure-comment lines for the summary preference; we still
        # fall back to them below if nothing else matches.
        if s.startswith(("#", "//", "/*", "*", "<!--", "--")):
            continue
        return s
    # Fallback: first non-empty line, even if it's a comment.
    for line in text.split("\n"):
        s = line.strip()
        if s:
            return s
    return ""


def _tags_for(rel_path: Path, ext: str, ingester_name: str) -> str:
    tags = {ingester_name, f"ext:{ext}"}
    for part in rel_path.parts[:-1]:
        tags.add(part.lower())
    return ",".join(sorted(tags))
