"""Markdown-directory ingester.

Walks a directory tree, treats each ``.md`` file as one knowledge entry.
Topic = path relative to the source root, minus extension. Tags include
the folder hierarchy plus anything declared in YAML frontmatter. Summary
is taken from frontmatter ``description``/``summary`` if present, else
from the first non-heading body line.

Idempotency contract: the dispatcher upserts on ``(topic, kind)``, so
re-running the same ingest is safe — new files insert, existing topics
update with the latest content.

Skips directories listed in ``opts['skip']`` (comma-separated set; the
CLI defaults to ``.obsidian,.trash,.git``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from ..backends import KnowledgeRow
from . import IngestError, register


_DEFAULT_SKIP = frozenset({".obsidian", ".trash", ".git"})


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Pull YAML frontmatter (if any) off the top of a markdown file.

    Returns (frontmatter_dict, body_without_frontmatter). On malformed
    YAML, treats the whole file as body. Tolerant by design — we want
    to keep ingesting even if one file's frontmatter is broken.
    """
    if not text.startswith("---\n"):
        return {}, text
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        return {}, text
    front_text = text[4:end]
    body = text[end + 5:]
    try:
        import yaml
        front = yaml.safe_load(front_text) or {}
        if not isinstance(front, dict):
            return {}, text
        return front, body
    except Exception:
        # Pyyaml might not be present; or the YAML is bad. Either way:
        # don't fail the whole ingest, just treat as body-only.
        return {}, text


def _derive_summary(front: dict, body: str) -> str:
    """Prefer frontmatter description/summary; else first non-heading
    non-empty line. Capped at 300 chars."""
    for key in ("description", "summary"):
        v = front.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s[:300]
    return "(no summary)"


def _derive_tags(rel_path: Path, front: dict, ingester_name: str) -> str:
    """Tags = folder hierarchy + frontmatter tags + ingester name."""
    tags: set[str] = {ingester_name}
    for part in rel_path.parts[:-1]:
        tags.add(part.lower())
    ft = front.get("tags")
    if isinstance(ft, list):
        tags.update(str(t).strip().lower() for t in ft if str(t).strip())
    elif isinstance(ft, str):
        tags.update(t.strip().lower() for t in ft.split(",") if t.strip())
    return ",".join(sorted(tags))


@register
class MarkdownDirIngester:
    """Read a directory tree of .md files into KnowledgeRow records."""

    name = "markdown-dir"

    @classmethod
    def owned_extensions(cls) -> frozenset[str]:
        """markdown-dir owns .md — the catch-all text-dir skips it in union
        mode so a note isn't surfaced by both ingesters."""
        return frozenset({"md"})

    @classmethod
    def matches(cls, source: str) -> bool:
        """True if ``source`` is an existing directory containing at
        least one .md file (recursive)."""
        p = Path(source)
        if not p.is_dir():
            return False
        # Cheap probe: any .md anywhere under it?
        try:
            next(p.rglob("*.md"))
            return True
        except StopIteration:
            return False

    def stream(
        self, source: str, opts: dict[str, Any]
    ) -> Iterator[KnowledgeRow]:
        root = Path(source).expanduser().resolve()
        kind = opts.get("kind") or "knowledge"
        project = opts.get("project") or None
        skip = set(opts.get("skip") or _DEFAULT_SKIP)

        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue

            topic = str(rel.with_suffix(""))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise IngestError(f"read failed: {topic}", e) from e

            front, body = _parse_frontmatter(text)
            yield KnowledgeRow(
                id=0,
                topic=topic,
                kind=kind,
                summary=_derive_summary(front, body),
                detail=text,
                tags=_derive_tags(rel, front, self.name),
                project=project,
            )
