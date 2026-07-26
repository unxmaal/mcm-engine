"""Structured references on knowledge entries (c5 Phase 5).

A knowledge entry can carry pointers to the source of truth (a file, a code
symbol, a test, a URL) instead of restating it in prose. Stored as a JSON list
in the `refs_json` column (non-indexed, like rationale/alternatives) and mapped
to/from KnowledgeRow.references, a Python list of {type, target, note?} dicts.

Column name is `refs_json`, not `references`: the latter is a SQL reserved word.
"""
from __future__ import annotations

import json
from typing import Any, Literal

REF_TYPES = ("file", "symbol", "test", "url")
RefType = Literal["file", "symbol", "test", "url"]


def validate_refs(refs: Any) -> list[dict]:
    """Validate and normalize a references payload. None -> []. Raises
    ValueError on any malformed item so a bad write is rejected up front."""
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise ValueError("references must be a list of {type, target, note?} objects")
    out: list[dict] = []
    for i, r in enumerate(refs):
        if not isinstance(r, dict):
            raise ValueError(f"reference[{i}] is not an object")
        rtype = r.get("type")
        target = r.get("target")
        if rtype not in REF_TYPES:
            raise ValueError(
                f"reference[{i}].type {rtype!r} must be one of {REF_TYPES}")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"reference[{i}].target must be a non-empty string")
        item: dict = {"type": rtype, "target": target.strip()}
        note = r.get("note")
        if note:
            item["note"] = str(note)
        out.append(item)
    return out


def dump_refs(refs: list | None) -> str | None:
    """Serialize a validated refs list to the JSON string stored in refs_json.
    Empty/None serialize to None (SQL NULL)."""
    if not refs:
        return None
    return json.dumps(refs)


def load_refs(raw: str | None) -> list | None:
    """Deserialize the refs_json column back to a Python list (or None)."""
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, list) else None


def format_refs(refs: list | None) -> str:
    """One-line-per-ref rendering appended to search/read output. Empty -> ''."""
    if not refs:
        return ""
    lines = []
    for r in refs:
        line = f"  ref[{r.get('type')}]: {r.get('target')}"
        note = r.get("note")
        if note:
            line += f" ({note})"
        lines.append(line)
    return "\n".join(lines)
