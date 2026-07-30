"""Corpus-wide governance tools — scroll_entries (#104), recall_entry (#103).

These serve the audit/governance consumer that must visit *every* stored
entry regardless of entity type (a risk scanner flagging secrets / PII /
mis-classified content), rather than the FTS retrieval path. `scroll_entries`
is the read half (paged enumerate); `recall_entry` is the act half (remove a
flagged entry by id, with an audit trail).
"""
from __future__ import annotations

import hashlib
import os

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, EntityTypeLiteral
from ..tracker import SessionTracker
from ..wiring import coerce_context

_DEFAULT_SCROLL_PAGE_MAX = 100

# Physical table per entity kind. Local to the tool because recall_entry runs
# raw SQL against the Postgres backend (mirroring kb_recall), the same way the
# adapters keep their own _ENTITY_TABLE. Keyed on the EntityTypeLiteral value.
_ENTITY_TABLE_SQL: dict[str, str] = {
    "knowledge": "knowledge",
    "negative":  "negative_knowledge",
    "error":     "errors",
    "rule":      "rules",
}

# A short label column per type, for the confirmation message.
_LABEL_COLUMN: dict[str, str] = {
    "knowledge": "topic",
    "negative":  "category",
    "error":     "pattern",
    "rule":      "title",
}


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def _scroll_page_max() -> int:
    """Per-call ceiling on a scroll page (env MCM_SCROLL_PAGE_MAX, default
    100). One call can't outrun the transport; clients page by cursor."""
    try:
        v = int(os.environ.get("MCM_SCROLL_PAGE_MAX", "") or _DEFAULT_SCROLL_PAGE_MAX)
        return v if v > 0 else _DEFAULT_SCROLL_PAGE_MAX
    except (TypeError, ValueError):
        return _DEFAULT_SCROLL_PAGE_MAX


# The substantive text columns per entity type, in the order a scanner reads
# them. First entry is the row's headline (shown inline); the rest are the
# body a content scanner scores. Kept explicit rather than dataclass-derived
# so counter/id/timestamp noise never leaks into the scored content or the
# change-detection hash.
_TEXT_FIELDS: dict[EntityType, tuple[str, ...]] = {
    EntityType.KNOWLEDGE: ("topic", "summary", "detail", "rationale", "alternatives", "tags"),
    EntityType.NEGATIVE:  ("category", "what_failed", "why_failed", "correct_approach"),
    EntityType.ERROR:     ("pattern", "context", "root_cause", "fix", "tags"),
    EntityType.RULE:      ("title", "description", "content", "keywords", "category"),
}


def _content_hash(row, fields: tuple[str, ...]) -> str:
    """Stable 12-hex digest over a row's scored text, so a client can detect
    change across pages/runs without re-transferring the full body."""
    h = hashlib.sha256()
    for f in fields:
        val = getattr(row, f, None)
        h.update(b"\x00")
        if val:
            h.update(str(val).encode("utf-8", "replace"))
    return h.hexdigest()[:12]


def _render_entry(etype: EntityType, row, fields: tuple[str, ...]) -> str:
    headline = getattr(row, fields[0], "") or ""
    ts = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    classification = getattr(row, "source_classification", None)
    parts = [f"#{row.id} [{etype.value}] {headline}"]
    meta = [f"hash={_content_hash(row, fields)}"]
    if ts is not None:
        meta.append(f"updated={ts}")
    if classification:
        meta.append(f"class={classification}")
    if getattr(row, "status", "active") not in ("active", None):
        meta.append(f"status={row.status}")
    parts.append("  " + " | ".join(meta))
    for f in fields[1:]:
        val = getattr(row, f, None)
        if val:
            parts.append(f"  {f}: {val}")
    return "\n".join(parts)


def register_corpus_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
) -> None:
    """Register scroll_entries (#104). recall_entry (#103) registers here too
    once its path lands."""
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage

    @mcp.tool()
    def scroll_entries(
        entity_type: EntityTypeLiteral,
        after_id: int = 0,
        limit: int = 100,
    ) -> str:
        """Paged, read-only enumerate over one entity type's full table, in
        id order — the corpus-audit counterpart to keyword `search`. Returns
        every entry so a scanner can visit the whole corpus; `search` only
        returns keyword matches.

        entity_type: one of knowledge, negative, error, rule.
        after_id: keyset cursor — pass the last id from the previous page (0
            starts at the beginning). Paging is stable under concurrent writes.
        limit: max entries this page; capped by MCM_SCROLL_PAGE_MAX (default
            100). O(n) total transfer across a full walk — the client pages.

        Each entry carries its id, headline, updated/created timestamp, a
        content hash (change detection), any source_classification, and the
        scored text body. The trailing line gives the next cursor.
        """
        # A bulk reader is read-only: record it as such so it resets the
        # store-reminder counter instead of tripping the write-loop blocks.
        tracker.record_call("scroll_entries")

        etype = EntityType(entity_type)
        cap = _scroll_page_max()
        n = limit if limit > 0 else _DEFAULT_SCROLL_PAGE_MAX
        n = min(n, cap)

        rows = storage.page_entries(etype, after_id=after_id, limit=n)
        fields = _TEXT_FIELDS[etype]

        if not rows:
            return (
                f"No {entity_type} entries with id > {after_id}. "
                f"End of corpus for this type."
            )

        blocks = [_render_entry(etype, r, fields) for r in rows]
        last_id = rows[-1].id
        more = len(rows) == n
        footer = (
            f"--- page: {len(rows)} {entity_type} entr"
            f"{'y' if len(rows) == 1 else 'ies'}"
            f" (cap {cap}). next: scroll_entries('{entity_type}', after_id={last_id})"
            f"{'' if more else ' — likely last page'}"
        )
        return "\n\n".join(blocks) + "\n\n" + footer

    @mcp.tool()
    def recall_entry(
        entity_type: EntityTypeLiteral,
        entry_id: int,
        reason: str = "",
        principal: str = "governance",
    ) -> str:
        """Remove one flagged entry by (entity_type, id), with an audit row in
        recall_log (postgres backend only) — the act half of the corpus-audit
        surface, generalizing kb_recall to all four entity types.

        entity_type MUST be given explicitly and is never inferred: knowledge
        and rule id spaces overlap, and mis-typing an id has silently destroyed
        live rules before. knowledge/negative/error are hard-deleted; a rule is
        moved to a terminal status='recalled' instead (a hard delete would be
        resurrected from its file by the next sync), invisible to search and to
        session_start but still inspectable for audit. Returns NOT_FOUND on a
        missing id, never a silent no-op; the recall_log row persists.
        """
        tracker.record_call("recall_entry")

        if getattr(storage.identity, "kind", None) != "postgres":
            return _with_nudge(
                "recall_entry requires the postgres storage backend.", tracker,
            )

        etype = entity_type  # the literal value doubles as the recall_log tag
        table = _ENTITY_TABLE_SQL[etype]
        label_col = _LABEL_COLUMN[etype]

        try:
            with storage.transaction():
                conn = storage._conn
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT id, {label_col} AS label FROM {table} WHERE id = %s",
                        (entry_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return _with_nudge(
                            f"NOT_FOUND: no {entity_type} with id={entry_id}.",
                            tracker,
                        )
                    label = row["label"] if hasattr(row, "keys") else row[1]

                    cur.execute(
                        "INSERT INTO recall_log (claim_id, entity_type, principal, reason) "
                        "VALUES (%s, %s, %s, %s)",
                        (entry_id, etype, principal or "governance", reason or None),
                    )
                    if etype == "rule":
                        # Terminal recall: keep the row (audit) but make it
                        # invisible and sync-proof. Never a hard delete.
                        cur.execute(
                            "UPDATE rules SET status = 'recalled', "
                            "updated_at = now() WHERE id = %s",
                            (entry_id,),
                        )
                        cur.execute(
                            "INSERT INTO rule_events (rule_id, event_type, actor, note) "
                            "VALUES (%s, %s, %s, %s)",
                            (entry_id, "recalled", principal or "governance",
                             reason or None),
                        )
                        verb = "Recalled (terminal status)"
                    else:
                        cur.execute(f"DELETE FROM {table} WHERE id = %s", (entry_id,))
                        verb = "Hard-deleted"
        except Exception as e:
            return _with_nudge(
                f"recall_entry failed: {type(e).__name__}: {e}", tracker,
            )

        return _with_nudge(
            f"{verb} {entity_type} #{entry_id} ('{label}'). "
            f"recall_log row written for principal={principal!r}.",
            tracker,
        )

    return scroll_entries
