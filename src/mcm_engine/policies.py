"""Kind policies for the unified entry model (issue #181, 4.0 program #176).

The only module that is allowed to branch on an entry's kind. Everything a
kind does differently lives here as data on ``KindPolicy`` or as one of the
render / rank / governance helpers below; the read path treats entries
uniformly and consults a policy only where behavior genuinely diverges.

``MCM_ENTRY_MODEL`` selects the read path: ``legacy`` (default in 4.0) reads
the four legacy tables through the typed rows; ``unified`` reads ``entries``
through ``Entry``. Both run in one process so the characterization replay can
diff them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .backends import Entry, EntityType
from .entries import to_entry
from .refs import format_refs

ENTRY_MODELS = ("legacy", "unified")


def entry_model() -> str:
    v = (os.environ.get("MCM_ENTRY_MODEL") or "legacy").strip().lower()
    return v if v in ENTRY_MODELS else "legacy"


def use_unified() -> bool:
    return entry_model() == "unified"


@dataclass(frozen=True)
class KindPolicy:
    kind: str
    label: str
    tracks_hits: bool      # search bumps hit_count / last_hit_at
    composite_rank: bool   # full composite rank; else pinned-only
    governed: bool         # rule-only lifecycle: archive / recall / outcomes / related spreading


POLICIES: dict[str, KindPolicy] = {
    "knowledge": KindPolicy("knowledge", "KNOWLEDGE", True, True, False),
    "negative":  KindPolicy("negative", "NEGATIVE", False, False, False),
    "error":     KindPolicy("error", "ERROR", False, False, False),
    "rule":      KindPolicy("rule", "RULE", True, True, True),
}


def policy_for(kind: str | EntityType) -> KindPolicy:
    return POLICIES[kind.value if isinstance(kind, EntityType) else kind]


def read_entry(storage, etype: EntityType, entry_id: int) -> Optional[Entry]:
    """One entry by (kind, id) through whichever read path the flag selects."""
    if use_unified():
        return storage.find_entry(etype.value, entry_id)
    row = storage.find_by_id(etype, entry_id)
    return to_entry(etype, row) if row is not None else None


# ---- governance -----------------------------------------------------------------

def hidden(e: Entry, *, include_archived: bool) -> bool:
    """Whether a search hit is withheld. Recall is terminal (never shown);
    archived and superseded are soft states that include_archived reveals."""
    if e.kind == "rule":
        if e.status == "recalled":
            return True
        if e.facets.get("archived") and not include_archived:
            return True
    if e.kind in ("rule", "knowledge"):
        if e.status == "superseded" and not include_archived:
            return True
    return False


def rank_inputs(e: Entry) -> dict[str, Any]:
    """Kind-specific extra terms for ``compose_rank``."""
    if e.kind == "rule":
        return {
            "importance": int(e.facets.get("importance", 0) or 0),
            "scope": e.facets.get("scope") or "conditional",
        }
    return {}


# ---- render -----------------------------------------------------------------------

def render_hit(
    e: Entry, *, detail: str, tags: str, cap: Callable[[str, str, int], str],
) -> str:
    """The search-hit text for an entry. ``tags`` is the already-composed
    ``[STALE][EXPIRED][PINNED]`` suffix; ``cap`` truncates an inline body field
    in summary mode (issue #171)."""
    f = e.facets
    if e.kind == "knowledge":
        s = (f"[KNOWLEDGE/{(e.subkind or 'finding').upper()} #{e.id}]{tags} "
             f"{e.title}: {e.summary or ''}")
        if detail == "full":
            if e.body:
                s += f"\n  Detail: {e.body}"
            if e.tags:
                s += f"\n  Tags: {e.tags}"
            refs = format_refs(f.get("references"))
            if refs:
                s += f"\n{refs}"
        elif e.tags:
            s += f"\n  Tags: {e.tags}"
        return s
    if e.kind == "rule":
        s = f"[RULE #{e.id}]{tags} {e.title}"
        if f.get("category"):
            s += f" ({f['category']})"
        if e.summary:
            s += f"\n  {e.summary}"
        if detail == "full" and f.get("file_path"):
            s += f"\n  File: {f['file_path']}"
        return s
    if e.kind == "negative":
        s = f"[NEGATIVE #{e.id}]{tags} {e.title}: {e.summary or ''}"
        if f.get("why_failed"):
            s += f"\n  Why: {cap(f['why_failed'], 'negative', e.id)}"
        if f.get("correct_approach"):
            s += f"\n  Fix: {cap(f['correct_approach'], 'negative', e.id)}"
        return s
    s = f"[ERROR #{e.id}]{tags} {e.title}"
    if f.get("root_cause"):
        s += f"\n  Root cause: {cap(f['root_cause'], 'error', e.id)}"
    if f.get("fix"):
        s += f"\n  Fix: {cap(f['fix'], 'error', e.id)}"
    return s


def label(e: Entry) -> str:
    """Short one-line label for relation listings."""
    if e.kind == "knowledge":
        return f"[KNOWLEDGE] {e.title}: {(e.summary or '')[:60]}"
    if e.kind == "error":
        return f"[ERROR] {(e.title or '')[:80]}"
    if e.kind == "rule":
        return f"[RULE] {e.title}"
    return f"[NEGATIVE] {e.title}: {(e.summary or '')[:60]}"


def promote_body(e: Entry) -> tuple[str, str, str]:
    """(content, default keywords, default category) when an entry is promoted
    to a rule. Rules are not promotable."""
    f = e.facets
    if e.kind == "knowledge":
        content = e.summary or ""
        if e.body:
            content += f"\n\n{e.body}"
        return content, (e.tags or e.title), ""
    if e.kind == "negative":
        content = f"**What failed:** {e.summary or ''}"
        if f.get("why_failed"):
            content += f"\n\n**Why:** {f['why_failed']}"
        if f.get("correct_approach"):
            content += f"\n\n## Fix\n\n{f['correct_approach']}"
        return content, e.title, e.title
    if e.kind == "error":
        content = f"**Error:** {e.title}"
        if f.get("context"):
            content += f"\n\n**Context:** {f['context']}"
        if f.get("root_cause"):
            content += f"\n\n**Root cause:** {f['root_cause']}"
        if f.get("fix"):
            content += f"\n\n## Fix\n\n{f['fix']}"
        return content, e.title[:100], ""
    raise ValueError(f"cannot promote a {e.kind} to a rule")
