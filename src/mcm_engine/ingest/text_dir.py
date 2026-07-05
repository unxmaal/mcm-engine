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


# Content-sniff, not an allowlist (#51). We can't know which extension holds
# the net-new signal, so we surface ANY file that reads as text. The only
# extension gate is a DENYLIST of formats that are always binary — reading a
# huge .png or .mp4 to sniff it is wasteful and pointless. Lowercase, no dot.
_BINARY_EXTENSIONS = frozenset({
    # images
    "png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "ico", "webp",
    "heic", "heif", "avif", "psd",
    # audio / video
    "mp3", "wav", "flac", "ogg", "m4a", "aac", "opus",
    "mp4", "mkv", "mov", "avi", "webm", "wmv", "flv", "m4v",
    # archives / compressed
    "zip", "tar", "gz", "bz2", "xz", "zst", "7z", "rar", "lz4", "tgz",
    # compiled / binary artifacts
    "o", "a", "so", "dylib", "dll", "exe", "bin", "obj", "lib",
    "class", "jar", "pyc", "pyo", "wasm", "wat",
    # fonts
    "ttf", "otf", "woff", "woff2", "eot",
    # binary documents / databases
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "db", "sqlite", "sqlite3", "mdb",
    # disk / media images, misc binary
    "iso", "img", "dmg", "pkg", "deb", "rpm",
})

# Sniff at most this many bytes to classify a file as text vs binary.
_SNIFF_BYTES = 8192

# The classic "is this text?" byte set: printable ASCII plus the common
# whitespace/control chars, plus all high bytes (utf-8 / latin-1 payload).
# A file is text if <=30% of its sniffed bytes fall outside this set and it
# has no embedded NUL.
_TEXT_CHARS = bytes({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})

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
    def owned_extensions(cls) -> frozenset[str]:
        """text-dir is the catch-all; it owns no extension exclusively, so it
        never blocks a more-specific ingester in union mode (#53)."""
        return frozenset()

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
        """Classify by CONTENT, not extension (#51). A denylist rejects known
        binary formats cheaply; everything else is sniffed."""
        ext = path.suffix.lstrip(".").lower()
        if ext in _BINARY_EXTENSIONS:
            return False
        try:
            with open(path, "rb") as fh:
                chunk = fh.read(_SNIFF_BYTES)
        except OSError:
            return False
        return _looks_like_text(chunk)

    def stream(
        self, source: str, opts: dict[str, Any]
    ) -> Iterator[KnowledgeRow]:
        root = Path(source).expanduser().resolve()
        kind = opts.get("kind") or "knowledge"
        project = opts.get("project") or None
        skip = set(opts.get("skip") or _DEFAULT_SKIP_DIRS)
        # Union mode (#53): a more-specific ingester already owns these
        # extensions — leave them alone so no file is surfaced twice.
        exclude = {
            e.lstrip(".").lower() for e in (opts.get("exclude_extensions") or ())
        }

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
            if ext in exclude:
                continue
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


def _looks_like_text(chunk: bytes) -> bool:
    """Heuristic text/binary classifier over a sniffed byte prefix.

    Empty file -> not text (nothing to ingest). An embedded NUL is a strong
    binary signal. Otherwise a file is text if at most 30% of its bytes fall
    outside the printable/whitespace/high-byte set — which admits UTF-8 and
    Latin-1 prose while rejecting binary blobs.
    """
    if not chunk:
        return False
    if b"\x00" in chunk:
        return False
    nontext = chunk.translate(None, _TEXT_CHARS)
    return len(nontext) / len(chunk) <= 0.30


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
