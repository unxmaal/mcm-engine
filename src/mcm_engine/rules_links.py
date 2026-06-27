"""Shared `[[slug]]` wikilink -> rule relation logic.

Both the `sync_rules` MCP tool (tools/rules.py) and the watcher's
`sync_once` (files/watcher.py, which is what stdio startup actually runs)
call into here, so the two paths can't drift. This module imports only
from `.backends` to stay free of import cycles (tools.rules and
files.watcher both sit above it).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .backends import EntityType, RelationRow

_WIKILINK_RE = re.compile(r"\[\[\s*([^\[\]]+?)\s*\]\]")


def extract_wikilinks(text: str) -> set[str]:
    """Return the set of ``[[slug]]`` targets in ``text`` (inner whitespace
    trimmed). Single brackets are ignored. The slug is a rule's filename
    stem — the identity links are resolved against."""
    return {m.group(1).strip() for m in _WIKILINK_RE.finditer(text)}


def build_wikilink_relations(storage: Any, project_root: Path) -> int:
    """Create rule->rule ``references`` relations from the ``[[slug]]``
    wikilinks in every rule file. Slug = filename stem. Resolution uses the
    full set of current rules, so link direction/order doesn't matter.

    Additive and idempotent — the relations UNIQUE constraint makes
    re-inserts no-ops, so this is safe to run on every sync. Returns the
    number of NEW relations created. Best-effort: archived rules,
    unreadable files, and unresolved or self links are skipped. (Removing a
    wikilink does not yet retract its relation — reconcile is a follow-up.)
    """
    try:
        rules = storage.list_rules_with_file_paths()
    except Exception:
        return 0

    slug_to_id: dict[str, int] = {}
    files: list[tuple[int, Path]] = []
    for r in rules:
        fp = r.file_path
        if not fp or getattr(r, "archived", False):
            continue
        full = Path(fp) if Path(fp).is_absolute() else project_root / fp
        slug_to_id[full.stem] = r.id
        files.append((r.id, full))

    created = 0
    for rule_id, full in files:
        try:
            content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for slug in extract_wikilinks(content):
            target_id = slug_to_id.get(slug)
            if target_id is None or target_id == rule_id:
                continue
            res = storage.insert_relation(RelationRow(
                id=0,
                source_type=EntityType.RULE, source_id=rule_id,
                target_type=EntityType.RULE, target_id=target_id,
                relation="references",
            ))
            if res is not None:
                created += 1
    return created
