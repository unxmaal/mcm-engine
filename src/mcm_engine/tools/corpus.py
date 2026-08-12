"""Corpus-wide governance tools — scroll_entries (#104), get_entry (#112),
recall_entry (#103), recall_events (#110), find_duplicate_entries /
find_conflicting_entries (#113).

These serve the audit/governance consumer that must visit *every* stored
entry regardless of entity type (a risk scanner flagging secrets / PII /
mis-classified content), rather than the FTS retrieval path. `scroll_entries`
is the paged-read half and `get_entry` the point-read half; `recall_entry` is
the act half (remove a flagged entry by id, with an audit trail) and
`recall_events` reads that audit trail back; the `find_*_entries` pair extends
rule-only dedup/conflict detection to the other entity types.
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


def _entry_text(row, fields: tuple[str, ...]) -> str:
    """Whole-row scored text (all substantive fields joined) — the dedup unit."""
    return " ".join(str(getattr(row, f, "") or "") for f in fields).strip()


def _topic_body(row, fields: tuple[str, ...]) -> tuple[str, str]:
    """Split a row into (topic, body) for conflict detection: the headline
    column is the subject, the rest is the claim. Mirrors how rules split
    title vs content."""
    topic = str(getattr(row, fields[0], "") or "")
    body = " ".join(str(getattr(row, f, "") or "") for f in fields[1:]).strip()
    return topic, body


def _is_hidden(row) -> bool:
    """A row a hygiene sweep should ignore: soft-deleted (archived) or in a
    non-live status (superseded / recalled). Knowledge/negative/error carry
    none of these today and so are never hidden; once knowledge gains a
    status column (#111) its superseded rows fall out here automatically."""
    if getattr(row, "archived", False):
        return True
    return getattr(row, "status", "active") in ("superseded", "recalled")


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
    def get_entry(
        entity_type: EntityTypeLiteral,
        entry_id: int,
    ) -> str:
        """Point read of one entry by (entity_type, id) — read-only, same
        rendered shape as a single `scroll_entries` block, or NOT_FOUND.

        entity_type: one of knowledge, negative, error, rule. Required and
            never inferred — knowledge and rule id spaces overlap.
        entry_id: the id to fetch.

        Use this to confirm a target id's current content immediately before a
        mutating call (`supersede_rule`, `supersede_knowledge`, `link_knowledge`,
        `recall_entry`) — the guard whose absence has caused wrong-id supersedes
        and stray links. Cheaper than re-paging the corpus for a single id.
        Reaches superseded/recalled rows too (unlike search), so it doubles as
        an audit point-read.
        """
        tracker.record_call("get_entry")

        etype = EntityType(entity_type)
        row = storage.find_by_id(etype, entry_id)
        if row is None:
            return _with_nudge(
                f"NOT_FOUND: no {entity_type} with id={entry_id}.", tracker,
            )
        return _with_nudge(_render_entry(etype, row, _TEXT_FIELDS[etype]), tracker)

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

    @mcp.tool()
    def recall_events(
        after_id: int = 0,
        limit: int = 100,
        since: str = "",
    ) -> str:
        """Paged, read-only read of the recall audit trail (postgres backend
        only) — the consumable counterpart to `recall_entry`, so a client can
        learn from what was recalled and why without a direct SELECT on the
        recall_log table.

        after_id: keyset cursor over the recall-event id — pass the last id
            from the previous page (0 starts at the beginning).
        limit: max events this page; capped by MCM_SCROLL_PAGE_MAX (default
            100). The client pages by cursor.
        since: optional ISO-8601 timestamp; only events at/after it are
            returned (e.g. "2026-08-01" or "2026-08-01T12:00:00Z").

        Each event carries {id, entity_type, entity_id, principal, reason,
        recalled_at}. No content field — recalled content is gone from the
        engine by design; a consumer needing the original text keeps its own
        snapshot. Same rendered-text + cursor-footer shape as `scroll_entries`.
        """
        tracker.record_call("recall_events")

        if getattr(storage.identity, "kind", None) != "postgres":
            return _with_nudge(
                "recall_events requires the postgres storage backend.", tracker,
            )

        cap = _scroll_page_max()
        n = limit if limit > 0 else _DEFAULT_SCROLL_PAGE_MAX
        n = min(n, cap)

        since_clause = ""
        params: list = [after_id]
        if since:
            since_clause = "AND recalled_at >= %s::timestamptz "
            params.append(since)
        params.append(n)

        try:
            with storage.transaction():
                conn = storage._conn
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, entity_type, claim_id, principal, reason, "
                        "recalled_at FROM recall_log "
                        "WHERE id > %s " + since_clause +
                        "ORDER BY id LIMIT %s",
                        tuple(params),
                    )
                    rows = cur.fetchall()
        except Exception as e:
            return _with_nudge(
                f"recall_events failed: {type(e).__name__}: {e}", tracker,
            )

        if not rows:
            tail = f" at/after {since}" if since else ""
            return _with_nudge(
                f"No recall events with id > {after_id}{tail}. "
                f"End of recall log.",
                tracker,
            )

        def _cell(r, key, idx):
            return r[key] if hasattr(r, "keys") else r[idx]

        blocks = []
        for r in rows:
            rid = _cell(r, "id", 0)
            etype = _cell(r, "entity_type", 1)
            claim_id = _cell(r, "claim_id", 2)
            principal = _cell(r, "principal", 3)
            reason = _cell(r, "reason", 4)
            recalled_at = _cell(r, "recalled_at", 5)
            meta = [f"principal={principal}", f"recalled={recalled_at}"]
            if reason:
                meta.append(f"reason={reason}")
            blocks.append(
                f"#{rid} [recall] {etype} #{claim_id}\n  " + " | ".join(meta)
            )

        last_id = _cell(rows[-1], "id", 0)
        more = len(rows) == n
        footer = (
            f"--- page: {len(rows)} recall event"
            f"{'' if len(rows) == 1 else 's'}"
            f" (cap {cap}). next: recall_events(after_id={last_id})"
            f"{'' if more else ' — likely last page'}"
        )
        return _with_nudge("\n\n".join(blocks) + "\n\n" + footer, tracker)

    def _headline(row, fields: tuple[str, ...]) -> str:
        return str(getattr(row, fields[0], "") or "")

    @mcp.tool()
    def find_duplicate_entries(
        entity_type: EntityTypeLiteral,
        threshold: float = 0.9,
    ) -> str:
        """Surface NEAR-DUPLICATE entries of one type for review — the
        knowledge/negative/error counterpart to `find_duplicate_rules` (#113).
        Deterministic MinHash/LSH over each live entry's substantive text,
        embedding-free. READ-ONLY — never merges, supersedes, or deletes; a
        human or agent decides what (if anything) to reconcile (for knowledge,
        via `supersede_knowledge`).

        entity_type: one of knowledge, negative, error, rule.
        threshold: Jaccard similarity at/above which two entries cluster
            (default 0.9). Lower to catch looser paraphrases.
        """
        tracker.record_call("find_duplicate_entries")
        from ..dedup import find_near_duplicates

        etype = EntityType(entity_type)
        fields = _TEXT_FIELDS[etype]
        heads: dict[int, str] = {}
        items: list[tuple[int, str]] = []
        for row in storage.iter_entries(etype):
            if _is_hidden(row):
                continue
            heads[row.id] = _headline(row, fields)
            items.append((row.id, _entry_text(row, fields)))

        clusters = find_near_duplicates(items, threshold=threshold)
        if not clusters:
            return _with_nudge(
                f"No near-duplicate {entity_type} entries found.", tracker,
            )
        lines = [
            f"Found {len(clusters)} near-duplicate {entity_type} cluster(s) "
            f"(threshold={threshold}):"
        ]
        for i, cluster in enumerate(clusters, 1):
            lines.append(f"  cluster {i}:")
            for eid in cluster:
                lines.append(f"    #{eid} {heads.get(eid, '')}")
        return _with_nudge("\n".join(lines), tracker)

    @mcp.tool()
    def find_conflicting_entries(
        entity_type: EntityTypeLiteral,
        topic_threshold: float = 0.5,
        body_threshold: float = 0.4,
    ) -> str:
        """Surface CONFLICT candidates of one type — entries whose headline is
        TOPICALLY similar but whose bodies DIVERGE ("same subject, opposite
        story"), the inverse of a near-duplicate and the multi-type counterpart
        to `find_conflicting_rules` (#113). Deterministic, embedding-free.
        READ-ONLY — never supersedes/merges; a human or agent decides.

        entity_type: one of knowledge, negative, error, rule.
        topic_threshold: headline similarity at/above which two entries are the
            "same subject" (default 0.5).
        body_threshold: body similarity at/below which the claims are taken to
            diverge (default 0.4).
        """
        tracker.record_call("find_conflicting_entries")
        from ..dedup import find_conflicts

        etype = EntityType(entity_type)
        fields = _TEXT_FIELDS[etype]
        heads: dict[int, str] = {}
        items: list[tuple[int, str, str]] = []
        for row in storage.iter_entries(etype):
            if _is_hidden(row):
                continue
            heads[row.id] = _headline(row, fields)
            topic, body = _topic_body(row, fields)
            items.append((row.id, topic, body))

        pairs = find_conflicts(items, topic_threshold=topic_threshold,
                               body_threshold=body_threshold)
        if not pairs:
            return _with_nudge(
                f"No conflicting {entity_type} entries found.", tracker,
            )
        lines = [
            f"Found {len(pairs)} {entity_type} conflict candidate(s) "
            f"(topic>={topic_threshold}, body<={body_threshold}):"
        ]
        for a, b, label in pairs:
            lines.append(
                f"  [{label}] #{a} '{heads.get(a, '')}'  <->  "
                f"#{b} '{heads.get(b, '')}'"
            )
        lines.append("  Review; reconcile via supersede_knowledge / recall_entry "
                     "as appropriate.")
        return _with_nudge("\n".join(lines), tracker)

    return scroll_entries
